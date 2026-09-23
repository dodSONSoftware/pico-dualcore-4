# devices/sht35/sht35_device.py - SHT35 temperature/humidity device
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Sensirion SHT35-DIS (temperature, relative humidity) over I2C.

The sensor protocol (detection, the command-oriented single-shot
measurement cycle, CRC validation, raw-value conversion) lives in the
low-level ``SHT35`` class; ``SHT35Device`` is the ``Device`` adapter that
applies the application-layer policy -- the user offsets. The I2C bus is
injected (Core 1 owns it); this module imports no ``machine`` API, so it
stays host-importable. The reference is the Sensirion SHT3x-DIS data sheet
(Datasheet SHT3x-DIS, Version 7, December 2022); where guidance disagrees
with the data sheet, the data sheet wins.

The SHT3x-DIS is **command-oriented, not register-mapped**: a transaction is
send a 16-bit command, wait for the operation to complete, read the
response. The driver therefore uses ``writeto``/``readfrom_into`` (never
``*_mem``), and the clock-stretching commands are deliberately not used:
a deterministic wait after a no-stretching command is simple, portable, and
independent of controller clock-stretch timeout behavior.
"""

import time

from devices.device import Device
from devices.sht35.validation import (
    DEFAULT_I2C_ADDRESS_CANDIDATES,
    DEFAULT_REPEATABILITY,
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


# --- Command words (SHT3x-DIS, clock stretching disabled) -------------------
# The 16-bit values already carry their command-checking bits; a command-only
# transaction gets NO appended CRC byte (CRCs cover returned data words and
# written data words like ALERT limits, neither of which this driver uses).
_CMD_MEASURE_HIGH = const(0x2400)   # single-shot, high repeatability
_CMD_MEASURE_MEDIUM = const(0x240B)  # single-shot, medium repeatability
_CMD_MEASURE_LOW = const(0x2416)    # single-shot, low repeatability
_CMD_BREAK = const(0x3093)          # stop periodic acquisition -> idle
_CMD_HEATER_OFF = const(0x3066)
_CMD_SERIAL_NUMBER = const(0x3682)  # 32-bit electronic identification code

# Repeatability -> (command, conversion wait ms). The waits are conservative
# delays over the datasheet's maximum measurement times (high 15 ms /
# medium 6 ms / low 4 ms at module voltages), covering the slightly longer
# low-voltage maxima as well.
_SINGLE_SHOT = {
    "high": (_CMD_MEASURE_HIGH, 16),
    "medium": (_CMD_MEASURE_MEDIUM, 7),
    "low": (_CMD_MEASURE_LOW, 5),
}

# The serial-number response is available after at least 1 ms (data sheet).
_SERIAL_WAIT_MS = const(1)
# The break command needs a bounded settling allowance before the next
# transaction (the guide's reference driver waits 1 ms).
_BREAK_WAIT_MS = const(1)

# Sensirion CRC-8: polynomial 0x31 (x^8 + x^5 + x^4 + 1), initial value 0xFF,
# no input/output reflection, no final XOR.
_CRC_POLYNOMIAL = const(0x31)
_CRC_INITIAL = const(0xFF)

# Raw-ADC -> physical transfer functions (the 16-bit values are already
# calibrated, linearized, and compensated in the sensor).
_RAW_MAX = 65535.0
_TEMP_OFFSET_C = -45.0              # T[deg C] = -45 + 175 * raw / 65535
_TEMP_SPAN_C = 175.0
_RH_SPAN_PERCENT = 100.0            # RH[%]    = 100 * raw / 65535


def _crc8(data, offset, length):
    """The Sensirion CRC-8 over ``data[offset:offset+length]``; the published
    validation vector CRC(0xBE 0xEF) = 0x92 pins the implementation in the
    test suite."""
    crc = _CRC_INITIAL
    for index in range(offset, offset + length):
        crc ^= data[index]
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) ^ _CRC_POLYNOMIAL) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
    return crc


def convert_temperature_c(raw):
    """Raw 16-bit temperature counts to Celsius (the equation spans -45 to
    +130 deg C; the guaranteed measurement range -40 to +125 deg C is
    inside it, so no clamping is applied -- an out-of-range reading is a
    real condition, not a decode artifact)."""
    return _TEMP_OFFSET_C + _TEMP_SPAN_C * raw / _RAW_MAX


def convert_humidity_percent(raw):
    """Raw 16-bit humidity counts to %RH (the equation spans exactly 0 to
    100, so the result is in range by construction)."""
    return _RH_SPAN_PERCENT * raw / _RAW_MAX


class SHT35:
    """Low-level SHT35 protocol: detection, the single-shot measurement
    cycle, and the raw-value conversion.

    Owns the command protocol and CRC validation only -- not offsets. The
    I2C object is injected. Every operational failure (a NACKed command, a
    NACKed read, a CRC mismatch) raises ``OSError``; ``MemoryError`` escapes
    to the heap boundary.
    """

    def __init__(self, i2c, address_candidates, *, repeatability):
        # Membership is pure validation's job (initialize() runs the shared
        # validator first); the lookup is a direct one, like the LTR390's
        # gain/resolution/rate code tables.
        self._command, self._wait_ms = _SINGLE_SHOT[repeatability]
        self._i2c = i2c
        self._address_candidates = tuple(address_candidates)
        self._address = None
        self._serial = None

        # Reusable buffers: allocated once, not per measurement.
        self._command_buffer = bytearray(2)
        self._measurement = bytearray(6)

    # --- Initialization sequence -------------------------------------------

    def init(self):
        """Run the bring-up: detect (per candidate address: break any
        periodic/ART acquisition a previous user may have left, then bind
        the address whose serial-number probe is CRC-valid), then heater
        off (an ambient measurement precondition, written explicitly rather
        than assumed from the power-on state). No soft reset: like the
        LTR390 profile, every state the driver depends on is written
        explicitly, which also makes the sequence re-runnable over the held
        bus. Re-runnable: the read-failure reinit path repeats this."""
        self._detect()
        self._write_command(_CMD_HEATER_OFF)

    def _detect(self):
        """Pick the first candidate address that answers the serial-number
        command with two CRC-valid words. The SHT3x-DIS has no chip-ID
        register; the 32-bit electronic identification code is the
        protocol's strongest identification -- a CRC-protected response to
        an SHT3x-specific command, not a mere ACK (a different device at
        0x44/0x45 that answers with garbage fails the CRC and is skipped).
        Each candidate is first returned to idle with break: a powered
        sensor may still be in periodic/ART acquisition from a previous
        controller, and the known command state must be established before
        the probe depends on it (a NACKed break is a candidate failure,
        like a NACKed probe)."""
        last_error = None
        for address in self._address_candidates:
            self._address = address
            try:
                self._write_command(_CMD_BREAK)
                time.sleep_ms(_BREAK_WAIT_MS)
                self._serial_number()
            except MemoryError:
                raise
            except OSError as err:
                last_error = err
                continue
            return
        raise OSError(
            "SHT35 not found at candidates {}: {}".format(
                list(self._address_candidates), last_error
            )
        )

    def _serial_number(self):
        """Read the 32-bit electronic identification code (two CRC-protected
        16-bit words). A failed CRC is a bus-integrity failure, raised like
        any other probe failure; a CRC-valid zero is not gated on (the code
        identifies, the CRC authenticates)."""
        self._write_command(_CMD_SERIAL_NUMBER)
        time.sleep_ms(_SERIAL_WAIT_MS)
        self._read_into(self._measurement)
        self._verify_word_crc(self._measurement, 0)
        self._verify_word_crc(self._measurement, 3)
        self._serial = (
            (self._measurement[0] << 24)
            | (self._measurement[1] << 16)
            | (self._measurement[3] << 8)
            | self._measurement[4]
        )

    # --- Measurement --------------------------------------------------------

    def read(self):
        """Trigger one single-shot measurement at the configured
        repeatability, wait the bounded conversion window, and read the six
        response bytes (T MSB/LSB/CRC, RH MSB/LSB/CRC). Both CRCs must
        validate: a bus-integrity failure is an ``OSError``, never a
        reading. Returns ``(temperature_c, humidity_percent)``."""
        self._write_command(self._command)
        time.sleep_ms(self._wait_ms)
        self._read_into(self._measurement)
        self._verify_word_crc(self._measurement, 0)
        self._verify_word_crc(self._measurement, 3)

        d = self._measurement
        raw_temperature = (d[0] << 8) | d[1]
        raw_humidity = (d[3] << 8) | d[4]
        return (
            convert_temperature_c(raw_temperature),
            convert_humidity_percent(raw_humidity),
        )

    # --- CRC ----------------------------------------------------------------

    def _verify_word_crc(self, data, offset):
        """Validate the CRC byte at ``offset+2`` against the two data bytes
        immediately preceding it (each word is CRC-protected independently).
        A mismatch is a bus-integrity failure: the bytes transferred, but
        their integrity failed, so the affected word is never reported."""
        expected = data[offset + 2]
        calculated = _crc8(data, offset, 2)
        if calculated != expected:
            raise OSError(
                "SHT35 CRC mismatch at byte {}: expected 0x{:02X}, "
                "calculated 0x{:02X}".format(offset + 2, expected, calculated)
            )

    # --- Command/transaction helpers (reusable buffers) ----------------------

    def _write_command(self, command):
        """Transmit a 16-bit command MSB first; command-only transactions
        carry no CRC byte."""
        self._command_buffer[0] = (command >> 8) & 0xFF
        self._command_buffer[1] = command & 0xFF
        try:
            self._i2c.writeto(self._address, self._command_buffer)
        except MemoryError:
            raise
        except OSError as err:
            raise OSError(
                "SHT35 command 0x{:04X} failed: {}".format(command, err)
            )

    def _read_into(self, buffer):
        try:
            self._i2c.readfrom_into(self._address, buffer)
        except MemoryError:
            raise
        except OSError as err:
            raise OSError("SHT35 read failed: {}".format(err))


class SHT35Device(Device):
    """``Device`` adapter for the SHT35: applies the application-layer policy
    (user offsets on the converted channels) on top of the low-level
    sensor's protocol. Derived values (dew point and the like) stay out of
    the driver: they are application-level calculations, not sensor
    channels."""

    def __init__(self, i2c):
        self._i2c = i2c
        self._sensor = None
        self._offset_t = 0.0
        self._offset_h = 0.0
        self._initialized = False

    def initialize(self, config):
        """Validate (shared pure rules) then bring up the sensor. Re-runnable:
        the read-failure reinit path calls this again to re-detect and
        re-establish the known state over the held bus."""
        validate_config(config)

        candidates = tuple(config.get("i2c_address_candidates", DEFAULT_I2C_ADDRESS_CANDIDATES))
        offsets = config.get("offsets", {})
        self._offset_t = offsets.get("temperature_c", 0)
        self._offset_h = offsets.get("humidity_percent", 0)

        self._sensor = SHT35(
            self._i2c,
            candidates,
            repeatability=config.get("repeatability", DEFAULT_REPEATABILITY),
        )
        self._sensor.init()
        self._initialized = True

    def read(self):
        """One telemetry sample. Offsets are applied after the conversion
        (the factory calibration stays untouched)."""
        if not self._initialized:
            raise RuntimeError("SHT35 device is not initialized")

        temperature_c, humidity_percent = self._sensor.read()

        return {
            "temperature_c": temperature_c + self._offset_t,
            "humidity_percent": humidity_percent + self._offset_h,
        }
