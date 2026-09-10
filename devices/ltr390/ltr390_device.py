# devices/ltr390/ltr390_device.py - LTR390 ambient light / UV device
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Lite-On LTR-390UV-01 (ambient light, ultraviolet) over I2C.

The sensor protocol (part identification, standby/mode control, data-ready
synchronization, raw reads, lux/UVI conversion) lives in the low-level
``LTR390`` class; ``LTR390Device`` is the ``Device`` adapter that applies the
application-layer policy the conversion keeps out -- the user offsets. The
I2C bus is injected (Core 1 owns it); this module imports no ``machine``
API, so it stays host-importable for the pure unit tests.

The most important architectural fact: ALS and UV are **sequential**.
``MAIN_CTRL`` selects which channel is actively converting, so a combined
sample is mode-switch, wait for a fresh conversion, read -- per channel --
and never a bare read of both data registers. The reference is the Lite-On
LTR-390UV-01 data sheet (DS86-2015-0004); where guidance disagrees with the
data sheet, the data sheet wins.
"""

import time

from devices.device import Device
from devices.ltr390.validation import (
    DEFAULT_GAIN,
    DEFAULT_MEASUREMENT_RATE_MS,
    DEFAULT_RESOLUTION_BITS,
    DEFAULT_WINDOW_FACTOR,
    validate_config,
)

try:
    from micropython import const
except ImportError:
    # Host (CPython) has no micropython.const; the identity keeps the module
    # importable for the pure unit tests (const() only affects MicroPython's
    # bytecode, not the values).
    def const(value):
        return value


# --- Register map (Lite-On LTR-390UV-01) -----------------------------------
_ADDRESS = const(0x53)          # fixed 7-bit address; no address-select pin
_REG_MAIN_CTRL = const(0x00)    # bit 4 reset, bit 3 UVS mode, bit 1 enable
_REG_MEAS_RATE = const(0x04)    # bits 6:4 ADC resolution, 2:0 measurement rate
_REG_GAIN = const(0x05)         # bits 2:0 gain
_REG_PART_ID = const(0x06)      # bits 7:4 part number, 3:0 silicon revision
_REG_MAIN_STATUS = const(0x07)  # bit 5 power-on, bit 4 interrupt, 3 data-ready
_REG_ALS_DATA = const(0x0D)     # 3-byte burst: 0x0D low, 0x0E mid, 0x0F high nibble
_REG_UVS_DATA = const(0x10)     # 3-byte burst: 0x10 low, 0x11 mid, 0x12 high nibble
_REG_INT_CFG = const(0x19)      # bits 5:4 source, bit 2 enable

_PART_ID_NIBBLE = const(0x0B)   # part number; the low nibble is the revision

# MAIN_CTRL values (reserved bits written as zero). The software-reset bit is
# deliberately never used: reset behavior has been observed to cause
# problematic I2C behavior on some hardware, so init explicitly writes every
# register it depends on instead.
_CTRL_STANDBY_ALS = const(0x00)
_CTRL_ALS_ACTIVE = const(0x02)
_CTRL_STANDBY_UVS = const(0x08)
_CTRL_UVS_ACTIVE = const(0x0A)

_STATUS_DATA_READY = const(0x08)

# Human-facing value -> register code (codes 5-7 are reserved on gain and
# resolution, so the config exposes the real values, not the encodings).
_GAIN_CODES = {1: 0, 3: 1, 6: 2, 9: 3, 18: 4}
_RESOLUTION_CODES = {20: 0, 19: 1, 18: 2, 17: 3, 16: 4, 13: 5}
_RATE_CODES = {25: 0, 50: 1, 100: 2, 200: 3, 500: 4, 1000: 5, 2000: 6}

# By register code: the actual gain multiplier and the ADC integration factor
# feeding the conversion equations. The 13-bit integration is 0.03125, not a
# one-step progression from 16-bit -- the resolution jumps three bits.
_GAIN_FACTORS = (1, 3, 6, 9, 18)
_INTEGRATION_FACTORS = (4.0, 2.0, 1.0, 0.5, 0.25, 0.03125)
_CONVERSION_TIME_MS = (400.0, 200.0, 100.0, 50.0, 25.0, 12.5)

# Bounded data-ready wait after a mode change: wake-up allowance (~5 ms
# typical, budgeted at 10) + the ADC conversion time + a guard margin.
_WAKE_ALLOWANCE_MS = const(10)
_READY_GUARD_MS = const(50)
_READY_POLL_MS = const(5)

_LUX_SCALE = 0.6
_UVI_REFERENCE_COUNTS = 2300.0   # counts per UVI at the reference operating point
_UVI_REFERENCE_GAIN = 18.0       # gain x18 / 20-bit (integration 4.0)
_UVI_REFERENCE_INTEGRATION = 4.0


def decode_raw20(data):
    """Decode a three-byte measurement burst: little-endian 20-bit, the upper
    nibble of the high byte reserved (masked off)."""
    return data[0] | (data[1] << 8) | ((data[2] & 0x0F) << 16)


def data_ready_timeout_ms(resolution_code):
    """The finite bound for waiting on a fresh conversion at the selected
    resolution (see ``_wait_for_data_ready``)."""
    return int(
        _WAKE_ALLOWANCE_MS + _CONVERSION_TIME_MS[resolution_code] + _READY_GUARD_MS
    )


class LTR390:
    """Low-level LTR390 protocol and conversion.

    Owns the register protocol, part identification, the sequential ALS/UV
    sampling, and the lux/UVI conversion only -- not offsets. The I2C object
    is injected.
    """

    def __init__(self, i2c, *, gain, resolution_bits, measurement_rate_ms, window_factor):
        # The conversion only ever compensates for attenuation: a factor below
        # 1.0 would amplify a measurement, which no optical window does.
        if window_factor < 1.0:
            raise ValueError("window_factor must be >= 1.0")

        self._i2c = i2c
        self._address = _ADDRESS
        self._gain_code = _GAIN_CODES[gain]
        self._resolution_code = _RESOLUTION_CODES[resolution_bits]
        self._rate_code = _RATE_CODES[measurement_rate_ms]
        self._gain_factor = _GAIN_FACTORS[self._gain_code]
        self._integration = _INTEGRATION_FACTORS[self._resolution_code]
        self._window_factor = window_factor
        self._timeout_ms = data_ready_timeout_ms(self._resolution_code)

        self._part_id = None
        self._revision_id = None

        # Reusable buffers: allocated once, not per measurement.
        self._read_buffer = bytearray(1)
        self._write_buffer = bytearray(1)
        self._data = bytearray(3)

    # --- Initialization sequence -------------------------------------------

    def init(self):
        """Run the bring-up: identify the part, clear any latched data-ready,
        then explicitly write every register this driver depends on, ending in
        standby. Re-runnable: the read-failure reinit path repeats this over
        the held bus, and a previously failed read can leave the sensor in any
        mode, so every register is rewritten (no software-reset bit)."""
        self._detect()
        self._clear_data_ready()
        self._write_u8(_REG_MAIN_CTRL, _CTRL_STANDBY_ALS)
        # Bits 7 and 3 are reserved and stay zero; only the configured fields.
        self._write_u8(_REG_MEAS_RATE, (self._resolution_code << 4) | self._rate_code)
        self._write_u8(_REG_GAIN, self._gain_code)
        # Interrupts are not used; write the register to a known disabled
        # state rather than relying on the reset value.
        self._write_u8(_REG_INT_CFG, 0x00)

    def _detect(self):
        part_id = self._read_u8(_REG_PART_ID)
        if (part_id >> 4) != _PART_ID_NIBBLE:
            raise OSError(
                "Unexpected LTR390 part ID: 0x{:02X}".format(part_id)
            )
        self._part_id = part_id
        # The low nibble is the silicon revision: record it, do not gate on it
        # (newer revisions must keep working).
        self._revision_id = part_id & 0x0F

    def _clear_data_ready(self):
        """Data-ready is per selected channel, and reading a channel's data
        register clears it: walk both channels in standby and discard one read
        each, so a stale latched bit (power-on state, a previous user, a
        half-finished read) cannot satisfy the first runtime freshness wait."""
        self._write_u8(_REG_MAIN_CTRL, _CTRL_STANDBY_ALS)
        self._read_raw20(_REG_ALS_DATA)
        self._write_u8(_REG_MAIN_CTRL, _CTRL_STANDBY_UVS)
        self._read_raw20(_REG_UVS_DATA)

    # --- Measurement --------------------------------------------------------

    def read(self):
        """One sequential sample -- ALS then UVS (the sensor converts only the
        selected channel, so each is a mode switch + fresh-conversion wait) --
        ending in standby. Returns ``(als_raw, uv_raw, lux, uvi)``."""
        als_raw = self._read_channel(_CTRL_ALS_ACTIVE, _REG_ALS_DATA)
        uv_raw = self._read_channel(_CTRL_UVS_ACTIVE, _REG_UVS_DATA)
        # Standby after each sample (~1 uA vs ~110 uA active), like the BME280
        # profile leaving the sensor asleep between reads.
        self._write_u8(_REG_MAIN_CTRL, _CTRL_STANDBY_ALS)
        return (
            als_raw,
            uv_raw,
            self._calculate_lux(als_raw),
            self._calculate_uvi(uv_raw),
        )

    def _read_channel(self, main_ctrl_value, data_register):
        self._write_u8(_REG_MAIN_CTRL, main_ctrl_value)
        # Every mode change restarts the conversion; the data-ready bit is
        # already clear (the last data read cleared it), so waiting for it set
        # is the freshness boundary -- reading before it would return the
        # previously selected channel's stale value.
        self._wait_for_data_ready()
        return self._read_raw20(data_register)

    def _wait_for_data_ready(self):
        """Bounded data-ready poll: 5 ms steps up to the configured-resolution
        timeout, never an unbounded or busy-spinning loop."""
        start = time.ticks_ms()
        while True:
            if self._read_u8(_REG_MAIN_STATUS) & _STATUS_DATA_READY:
                return
            if time.ticks_diff(time.ticks_ms(), start) >= self._timeout_ms:
                raise OSError("LTR390 data-ready timeout")
            time.sleep_ms(_READY_POLL_MS)

    def _read_raw20(self, register):
        """One coherent three-byte burst from a measurement register."""
        try:
            self._i2c.readfrom_mem_into(self._address, register, self._data)
        except MemoryError:
            raise
        except OSError as err:
            raise OSError(
                "LTR390 data read failed at register 0x{:02X}: {}".format(register, err)
            )
        return decode_raw20(self._data)

    # --- Conversion (floating point; lux / estimated UVI) ------------------

    def _calculate_lux(self, raw):
        """ALS counts to lux: ``0.6 * raw / (gain * integration) * window
        factor``. The raw count is not lux -- the scaling is the configured
        operating point, not a constant."""
        return (
            _LUX_SCALE
            * raw
            / (self._gain_factor * self._integration)
            * self._window_factor
        )

    def _calculate_uvi(self, raw):
        """UV counts to an *estimated* UV Index (not an official meteorological
        one): the ~2300 counts/UVI reference holds at x18 / 20-bit; any other
        operating point scales by its gain and integration ratios to that
        reference."""
        counts_per_uvi = (
            _UVI_REFERENCE_COUNTS
            * (self._gain_factor / _UVI_REFERENCE_GAIN)
            * (self._integration / _UVI_REFERENCE_INTEGRATION)
        )
        if counts_per_uvi <= 0:
            raise ArithmeticError("Invalid LTR390 UVI scaling")
        return raw / counts_per_uvi * self._window_factor

    # --- Register read/write helpers (reusable buffers) ---------------------

    def _read_u8(self, register):
        try:
            self._i2c.readfrom_mem_into(self._address, register, self._read_buffer)
        except MemoryError:
            raise
        except OSError as err:
            raise OSError(
                "LTR390 read failed at register 0x{:02X}: {}".format(register, err)
            )
        return self._read_buffer[0]

    def _write_u8(self, register, value):
        self._write_buffer[0] = value
        try:
            self._i2c.writeto_mem(self._address, register, self._write_buffer)
        except MemoryError:
            raise
        except OSError as err:
            raise OSError(
                "LTR390 write failed at register 0x{:02X}: {}".format(register, err)
            )


class LTR390Device(Device):
    """``Device`` adapter for the LTR390: applies the application-layer policy
    (user offsets on the converted channels) on top of the low-level sensor's
    raw and converted channels."""

    def __init__(self, i2c):
        self._i2c = i2c
        self._sensor = None
        self._offset_lux = 0.0
        self._offset_uv_index = 0.0
        self._initialized = False

    def initialize(self, config):
        """Validate (shared pure rules) then bring up the sensor. Re-runnable:
        the read-failure reinit path calls this again to re-identify and
        rewrite the register configuration over the held bus."""
        validate_config(config)

        offsets = config.get("offsets", {})
        self._offset_lux = offsets.get("lux", 0)
        self._offset_uv_index = offsets.get("uv_index", 0)

        self._sensor = LTR390(
            self._i2c,
            gain=config.get("gain", DEFAULT_GAIN),
            resolution_bits=config.get("resolution_bits", DEFAULT_RESOLUTION_BITS),
            measurement_rate_ms=config.get("measurement_rate_ms", DEFAULT_MEASUREMENT_RATE_MS),
            window_factor=config.get("window_factor", DEFAULT_WINDOW_FACTOR),
        )
        self._sensor.init()
        self._initialized = True

    def read(self):
        """One telemetry sample. Offsets are applied after the conversion
        (the spec keeps them out of the fundamental scaling); the raw counts
        are reported unmodified so saturation stays visible in telemetry."""
        if not self._initialized:
            raise RuntimeError("LTR390 device is not initialized")

        als_raw, uv_raw, lux, uv_index = self._sensor.read()

        return {
            "lux": lux + self._offset_lux,
            "uv_index": uv_index + self._offset_uv_index,
            "als_raw": als_raw,
            "uv_raw": uv_raw,
        }
