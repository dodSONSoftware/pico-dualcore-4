# devices/bme280/bme280_device.py - BME280 temperature/pressure/humidity device
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Bosch BME280 (temperature, barometric pressure, relative humidity) over I2C.

The sensor protocol (chip identification, reset, calibration, Bosch
compensation) lives in the low-level ``BME280`` class; ``BME280Device`` is the
``Device`` adapter that applies the application-layer policy the spec keeps
out of the fundamental compensation -- user offsets and the derived altitude.
The I2C bus is injected (Core 1 owns it); this module imports no ``machine``
API, so it stays host-importable for the pure unit tests.

The reference is the Bosch BME280 data sheet (BST-BME280-DS001) and SensorAPI;
where guidance disagrees with the datasheet, the datasheet wins.
"""

import time

from devices.device import Device
from devices.bme280.validation import validate_config

try:
    from micropython import const
except ImportError:
    # Host (CPython) has no micropython.const; the identity keeps the module
    # importable for the pure unit tests (const() only affects MicroPython's
    # bytecode, not the values).
    def const(value):
        return value


# --- Register map (Bosch BME280) -------------------------------------------
_REG_CALIB_0 = const(0x88)      # 26-byte block: 0x88-0xA1 (dig_T*, dig_P*, dig_H1)
_REG_CHIP_ID = const(0xD0)      # chip ID, must read 0x60
_REG_RESET = const(0xE0)        # software reset (write 0xB6)
_REG_CALIB_26 = const(0xE1)     # 7-byte block: 0xE1-0xE7 (dig_H2..dig_H6)
_REG_CTRL_HUM = const(0xF2)     # bits 2:0 osrs_h
_REG_STATUS = const(0xF3)       # bit 3 measuring, bit 0 im_update
_REG_CTRL_MEAS = const(0xF4)    # bits 7:5 osrs_t, 4:2 osrs_p, 1:0 mode
_REG_CONFIG = const(0xF5)       # bits 7:5 t_sb, 4:2 filter, 0 spi3w_en
_REG_DATA = const(0xF7)         # 8-byte burst: press[3], temp[3], hum[2]

_CHIP_ID = const(0x60)
_RESET_COMMAND = const(0xB6)
_STATUS_MEASURING = const(0x08)
_STATUS_IM_UPDATE = const(0x01)

_MODE_SLEEP = const(0)
_MODE_FORCED = const(1)
_MODE_NORMAL = const(3)

# Oversampling register code -> actual multiplier (code 3 -> x4, code 5 -> x16).
_OVERSAMPLING_MULTIPLIER = (0, 1, 2, 4, 8, 16)

# Raw-ADC sentinels the datasheet leaves in the data registers of a channel
# whose oversampling is 0 (skipped). Never fed to the compensation equations.
_SENTINEL_20 = const(0x80000)   # temperature / pressure (20-bit)
_SENTINEL_16 = const(0x8000)    # humidity (16-bit)

# Bosch max-conversion-time model constants (ms).
_MEASURE_BASE_MS = 1.25
_MEASURE_PER_SAMPLE_MS = 2.3
_MEASURE_PH_OVERHEAD_MS = 0.575
_NVM_WAIT_TIMEOUT_MS = 100

# config-register standby (t_sb) -- normal-mode only, irrelevant in forced mode.
_STANDBY = const(0)


# --- Integer conversion helpers (explicit; no struct on constrained builds) --


def _u16_le(data, offset):
    return data[offset] | (data[offset + 1] << 8)


def _s16_le(data, offset):
    value = _u16_le(data, offset)
    if value & 0x8000:
        value -= 0x10000
    return value


def _s8(value):
    if value & 0x80:
        value -= 0x100
    return value


def _s12(value):
    if value & 0x800:
        value -= 0x1000
    return value


def _clamp_humidity(value):
    """Clamp relative humidity to the 0-100 %RH range the datasheet specifies.
    Out-of-range input is a real condition (condensation, contamination), so
    clamp rather than raise."""
    if value > 100.0:
        return 100.0
    if value < 0.0:
        return 0.0
    return value


class BME280:
    """Low-level BME280 protocol and Bosch compensation.

    Owns the register protocol, calibration decode, and the three compensation
    equations only -- not offsets or altitude. The I2C object is injected.
    """

    def __init__(self, i2c, address_candidates, *, osrs_t, osrs_p, osrs_h, filter_code):
        self._i2c = i2c
        self._address_candidates = tuple(address_candidates)
        self._osrs_t = osrs_t
        self._osrs_p = osrs_p
        self._osrs_h = osrs_h
        self._filter_code = filter_code
        self._address = None
        self._t_fine = 0

        # Reusable buffers: allocated once, not per measurement.
        self._read_buffer = bytearray(1)
        self._write_buffer = bytearray(1)
        self._data = bytearray(8)
        self._calib_block1 = bytearray(26)
        self._calib_block2 = bytearray(7)

        # Calibration coefficients (0 until _read_calibration runs).
        self._dig_t1 = self._dig_t2 = self._dig_t3 = 0
        self._dig_p1 = self._dig_p2 = self._dig_p3 = 0
        self._dig_p4 = self._dig_p5 = self._dig_p6 = self._dig_p7 = 0
        self._dig_p8 = self._dig_p9 = 0
        self._dig_h1 = self._dig_h2 = self._dig_h3 = 0
        self._dig_h4 = self._dig_h5 = self._dig_h6 = 0

    # --- Initialization sequence -------------------------------------------

    def init(self):
        """Run the full bring-up: detect, reset, wait for the NVM copy, read and
        validate calibration, then configure (leaving the sensor asleep for
        forced-mode reads). Re-runnable: the reinit path reuses the bus and
        repeats this, because a soft reset does not preserve configuration."""
        self._detect()
        self._reset()
        self._wait_for_nvm()
        self._read_calibration()
        self._validate_calibration()
        self._configure(_MODE_SLEEP)

    def _detect(self):
        """Pick the first candidate address that answers with the BME280 chip ID
        (a BMP280 shares these addresses, so a mere ACK is not enough)."""
        last_error = None
        for address in self._address_candidates:
            self._address = address
            try:
                chip_id = self._read_u8(_REG_CHIP_ID)
            except MemoryError:
                raise
            except OSError as err:
                last_error = err
                continue
            if chip_id == _CHIP_ID:
                return
            last_error = OSError(
                "unexpected chip ID 0x{:02X} at address {}".format(chip_id, address)
            )
        raise OSError(
            "BME280 not found at candidates {}: {}".format(
                list(self._address_candidates), last_error
            )
        )

    def _reset(self):
        self._write_u8(_REG_RESET, _RESET_COMMAND)
        # The datasheet gives ~2 ms for the sensor to restart before the NVM
        # calibration copy status is meaningful.
        time.sleep_ms(2)

    def _wait_for_nvm(self, timeout_ms=_NVM_WAIT_TIMEOUT_MS):
        """Wait for the NVM calibration copy to complete (im_update clear) with a
        finite timeout -- never an unbounded spin."""
        start = time.ticks_ms()
        while True:
            if not (self._read_u8(_REG_STATUS) & _STATUS_IM_UPDATE):
                return
            if time.ticks_diff(time.ticks_ms(), start) >= timeout_ms:
                raise OSError("BME280 NVM copy timeout")
            time.sleep_ms(2)

    def _read_calibration(self):
        """Read the two calibration blocks and decode every coefficient.
        dig_H4/dig_H5 share register 0xE5 and are the easiest decode to get
        wrong; the little-endian signed 16/12/8-bit unpacking is explicit."""
        try:
            self._i2c.readfrom_mem_into(self._address, _REG_CALIB_0, self._calib_block1)
            self._i2c.readfrom_mem_into(self._address, _REG_CALIB_26, self._calib_block2)
        except MemoryError:
            raise
        except OSError as err:
            raise OSError("BME280 calibration read failed: {}".format(err))

        b1 = self._calib_block1  # 0x88-0xA1, index = register - 0x88
        self._dig_t1 = _u16_le(b1, 0x88 - 0x88)
        self._dig_t2 = _s16_le(b1, 0x8A - 0x88)
        self._dig_t3 = _s16_le(b1, 0x8C - 0x88)
        self._dig_p1 = _u16_le(b1, 0x8E - 0x88)
        self._dig_p2 = _s16_le(b1, 0x90 - 0x88)
        self._dig_p3 = _s16_le(b1, 0x92 - 0x88)
        self._dig_p4 = _s16_le(b1, 0x94 - 0x88)
        self._dig_p5 = _s16_le(b1, 0x96 - 0x88)
        self._dig_p6 = _s16_le(b1, 0x98 - 0x88)
        self._dig_p7 = _s16_le(b1, 0x9A - 0x88)
        self._dig_p8 = _s16_le(b1, 0x9C - 0x88)
        self._dig_p9 = _s16_le(b1, 0x9E - 0x88)
        self._dig_h1 = b1[0xA1 - 0x88]  # unsigned 8-bit

        b2 = self._calib_block2  # 0xE1-0xE7, index = register - 0xE1
        self._dig_h2 = _s16_le(b2, 0xE1 - 0xE1)
        self._dig_h3 = b2[0xE3 - 0xE1]  # unsigned 8-bit
        e4 = b2[0xE4 - 0xE1]
        e5 = b2[0xE5 - 0xE1]
        e6 = b2[0xE6 - 0xE1]
        # 0xE5 holds dig_H4's high nibble (low) and dig_H5's low nibble (high).
        self._dig_h4 = _s12((e4 << 4) | (e5 & 0x0F))
        self._dig_h5 = _s12((e6 << 4) | (e5 >> 4))
        self._dig_h6 = _s8(b2[0xE7 - 0xE1])  # signed 8-bit

    def _validate_calibration(self):
        # dig_P1 feeds the pressure divisor; a zero (blank NVM) would divide by
        # zero on every read, so fail at init instead.
        if self._dig_p1 == 0:
            raise OSError("Invalid BME280 calibration: dig_P1 is zero")

    def _configure(self, mode):
        """Apply the measurement configuration. The write order is fixed:
        ctrl_hum latches only on a subsequent ctrl_meas write, so ctrl_meas must
        be written last (reversing it silently drops the humidity oversampling)."""
        self._write_u8(_REG_CTRL_HUM, self._osrs_h & 0x07)
        # t_sb (standby) is normal-mode only; spi3w_en is 0 for I2C.
        self._write_u8(_REG_CONFIG, ((_STANDBY << 5) | (self._filter_code << 2)) & 0x3C)
        self._write_u8(
            _REG_CTRL_MEAS,
            ((self._osrs_t << 5) | (self._osrs_p << 2) | mode) & 0xFF,
        )

    # --- Measurement --------------------------------------------------------

    def _measurement_time_ms(self):
        """Bosch maximum conversion time for the configured oversampling (not an
        arbitrary sleep): base + per-channel terms, the pressure/humidity
        overheads only for enabled channels, rounded up with a small guard."""
        factors = _OVERSAMPLING_MULTIPLIER
        delay_ms = _MEASURE_BASE_MS
        if self._osrs_t:
            delay_ms += _MEASURE_PER_SAMPLE_MS * factors[self._osrs_t]
        if self._osrs_p:
            delay_ms += _MEASURE_PER_SAMPLE_MS * factors[self._osrs_p] + _MEASURE_PH_OVERHEAD_MS
        if self._osrs_h:
            delay_ms += _MEASURE_PER_SAMPLE_MS * factors[self._osrs_h] + _MEASURE_PH_OVERHEAD_MS
        return int(delay_ms + 0.999) + 1

    def read(self):
        """Trigger one forced conversion and return
        ``(temperature_c, pressure_pa, humidity_percent)``; a skipped channel is
        ``None``. Temperature is compensated first because pressure and humidity
        both consume its ``t_fine``."""
        self._write_u8(
            _REG_CTRL_MEAS,
            ((self._osrs_t << 5) | (self._osrs_p << 2) | _MODE_FORCED) & 0xFF,
        )
        time.sleep_ms(self._measurement_time_ms())

        # One coherent sample: all three channels from a single 8-byte burst so
        # the values cannot straddle a measurement update.
        try:
            self._i2c.readfrom_mem_into(self._address, _REG_DATA, self._data)
        except MemoryError:
            raise
        except OSError as err:
            raise OSError("BME280 data read failed: {}".format(err))

        d = self._data
        adc_p = (d[0] << 12) | (d[1] << 4) | (d[2] >> 4)
        adc_t = (d[3] << 12) | (d[4] << 4) | (d[5] >> 4)
        adc_h = (d[6] << 8) | d[7]

        temperature_c = None
        if adc_t != _SENTINEL_20:
            temperature_c = self._compensate_temperature(adc_t)

        pressure_pa = None
        if adc_p != _SENTINEL_20 and temperature_c is not None:
            pressure_pa = self._compensate_pressure(adc_p)

        humidity_percent = None
        if adc_h != _SENTINEL_16 and temperature_c is not None:
            humidity_percent = self._compensate_humidity(adc_h)

        return temperature_c, pressure_pa, humidity_percent

    # --- Bosch compensation (floating point; Pa / deg C / %RH) --------------

    def _compensate_temperature(self, adc_t):
        var1 = (adc_t / 16384.0 - self._dig_t1 / 1024.0) * self._dig_t2
        var2 = (
            (adc_t / 131072.0 - self._dig_t1 / 8192.0)
            * (adc_t / 131072.0 - self._dig_t1 / 8192.0)
            * self._dig_t3
        )
        # t_fine is the integer intermediate the pressure and humidity equations
        # depend on, so it is derived here, not separately.
        self._t_fine = int(var1 + var2)
        return (var1 + var2) / 5120.0

    def _compensate_pressure(self, adc_p):
        var1 = (self._t_fine / 2.0) - 64000.0
        var2 = (var1 * var1 * self._dig_p6) / 32768.0
        var2 = var2 + (var1 * self._dig_p5 * 2.0)
        var2 = (var2 / 4.0) + self._dig_p4 * 65536.0
        var1 = ((self._dig_p3 * var1 * var1) / 524288.0 + (self._dig_p2 * var1)) / 524288.0
        var1 = (1.0 + var1 / 32768.0) * self._dig_p1
        if var1 == 0:
            raise ArithmeticError("Invalid BME280 pressure calibration")
        pressure = 1048576.0 - adc_p
        pressure = (pressure - var2 / 4096.0) * 6250.0 / var1
        var1 = (self._dig_p9 * pressure * pressure) / 2147483648.0
        var2 = (pressure * self._dig_p8) / 32768.0
        pressure = pressure + (var1 + var2 + self._dig_p7) / 16.0
        return pressure

    def _compensate_humidity(self, adc_h):
        var_h = self._t_fine - 76800.0
        var_h = (
            adc_h
            - (self._dig_h4 * 64.0 + self._dig_h5 / 16384.0 * var_h)
        ) * (
            self._dig_h2 / 65536.0
            * (
                1.0
                + self._dig_h6 / 67108864.0
                * var_h
                * (1.0 + self._dig_h3 / 67108864.0 * var_h)
            )
        )
        var_h = var_h * (1.0 - self._dig_h1 * var_h / 524288.0)
        return _clamp_humidity(var_h)

    # --- Register read/write helpers (reusable buffers) ---------------------

    def _read_u8(self, register):
        try:
            self._i2c.readfrom_mem_into(self._address, register, self._read_buffer)
        except MemoryError:
            raise
        except OSError as err:
            raise OSError(
                "BME280 read failed at register 0x{:02X}: {}".format(register, err)
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
                "BME280 write failed at register 0x{:02X}: {}".format(register, err)
            )


class BME280Device(Device):
    """``Device`` adapter for the BME280: applies the application-layer policy
    (user offsets, derived altitude) on top of the low-level sensor's raw
    compensated channels."""

    def __init__(self, i2c):
        self._i2c = i2c
        self._sensor = None
        self._offset_t = 0.0
        self._offset_p = 0.0
        self._offset_h = 0.0
        self._sea_level_pa = 101325.0
        self._initialized = False

    def initialize(self, config):
        """Validate (shared pure rules) then bring up the sensor. Re-runnable:
        the read-failure reinit path calls this again to re-detect, re-reset,
        and re-read calibration over the held bus."""
        validate_config(config)

        candidates = tuple(config.get("i2c_address_candidates", (118, 119)))
        offsets = config.get("offsets", {})
        self._offset_t = offsets.get("temperature_c", 0)
        self._offset_p = offsets.get("pressure_pascal", 0)
        self._offset_h = offsets.get("humidity_percent", 0)
        self._sea_level_pa = config["sea_level_pressure_pa"]

        self._sensor = BME280(
            self._i2c,
            candidates,
            osrs_t=config.get("temperature_oversampling", 1),
            osrs_p=config.get("pressure_oversampling", 1),
            osrs_h=config.get("humidity_oversampling", 1),
            filter_code=config.get("iir_filter", 0),
        )
        self._sensor.init()
        self._initialized = True

    def read(self):
        """One telemetry sample. Offsets are applied after Bosch compensation
        (the spec keeps them out of the factory calibration); altitude is derived
        from the offset-adjusted pressure and stays out of the low-level class."""
        if not self._initialized:
            raise RuntimeError("BME280 device is not initialized")

        temperature_c, pressure_pa, humidity_percent = self._sensor.read()

        if temperature_c is not None:
            temperature_c = temperature_c + self._offset_t
        if pressure_pa is not None:
            pressure_pa = pressure_pa + self._offset_p
        if humidity_percent is not None:
            humidity_percent = humidity_percent + self._offset_h

        altitude_m = None
        if pressure_pa is not None:
            altitude_m = 44330.0 * (
                1.0 - (pressure_pa / self._sea_level_pa) ** 0.1903
            )

        return {
            "temperature_c": temperature_c,
            "pressure_pa": pressure_pa,
            "humidity_percent": humidity_percent,
            "altitude_m": altitude_m,
        }
