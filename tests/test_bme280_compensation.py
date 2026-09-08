# test_bme280_compensation.py - BME280 decode and compensation (pure, no hardware)
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Host-side unit tests for the BME280 byte decoding and Bosch compensation.

These exercise the deterministic, hardware-free core: the integer decode
helpers, the two-block calibration decode (the H4/H5 shared-register trap),
the register-timing model, and the three compensation equations. The
compensation test compares the driver against an independent in-test
re-derivation of the Bosch floating-point equations -- it catches
transcription/precedence errors; the authoritative cross-check is the
real-hardware reference vector captured during bring-up (spec 85.2), which is
filled into the device test suite. No time APIs are used, so no shims are
needed."""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from devices.bme280.bme280_device import (  # noqa: E402
    BME280,
    _clamp_humidity,
    _s12,
    _s16_le,
    _s8,
    _u16_le,
)


class DummyI2C:
    """Never called in these tests; the compensation methods read only the
    cached calibration attributes, so any object satisfies the constructor."""


def _sensor(osrs_t=1, osrs_p=1, osrs_h=1, filter_code=0):
    return BME280(
        DummyI2C(), (118,), osrs_t=osrs_t, osrs_p=osrs_p, osrs_h=osrs_h, filter_code=filter_code
    )


# --- Integer decode helpers ------------------------------------------------


def test_u16_little_endian():
    assert _u16_le(bytes([0x34, 0x12]), 0) == 0x1234
    assert _u16_le(bytes([0x00, 0x00]), 0) == 0
    assert _u16_le(bytes([0xFF, 0xFF]), 0) == 0xFFFF
    assert _u16_le(bytes([0xAA, 0x55, 0x11]), 1) == 0x1155


def test_s16_little_endian_sign_extension():
    assert _s16_le(bytes([0x00, 0x80]), 0) == -32768
    assert _s16_le(bytes([0xFF, 0x7F]), 0) == 32767
    assert _s16_le(bytes([0x01, 0x00]), 0) == 1
    assert _s16_le(bytes([0x00, 0x00]), 0) == 0


def test_s8_sign_extension():
    assert _s8(0x7F) == 127
    assert _s8(0x80) == -128
    assert _s8(0x00) == 0
    assert _s8(0xFF) == -1


def test_s12_sign_extension():
    assert _s12(0x7FF) == 2047
    assert _s12(0x800) == -2048
    assert _s12(0x000) == 0


# --- Calibration decode (H4/H5 share register 0xE5) ------------------------


class FakeCalibI2C:
    """Returns two canned calibration blocks for _read_calibration."""

    def __init__(self, block1, block2):
        self._block1 = bytes(block1)
        self._block2 = bytes(block2)

    def readfrom_mem_into(self, address, register, buffer):
        if register == 0x88:
            buffer[:] = self._block1[: len(buffer)]
        elif register == 0xE1:
            buffer[:] = self._block2[: len(buffer)]


def test_calibration_decode_including_h4_h5():
    block1 = bytearray(26)
    block1[0] = 0x9A
    block1[1] = 0x44  # dig_T1 = 0x449A = 17562 (unsigned)
    block1[2] = 0xEF
    block1[3] = 0xB2  # dig_T2 = 0xB2EF = -19729 (signed)
    block1[25] = 0xE6  # dig_H1 = 230 (unsigned)

    block2 = bytearray(7)
    block2[3] = 0x8B  # e4 -> dig_H4 low byte
    block2[4] = 0x02  # e5 -> dig_H4 high nibble (0), dig_H5 low nibble (2)
    block2[5] = 0x01  # e6 -> dig_H5 high byte

    i2c = FakeCalibI2C(block1, block2)
    sensor = BME280(i2c, (118,), osrs_t=1, osrs_p=1, osrs_h=1, filter_code=0)
    sensor._read_calibration()

    assert sensor._dig_t1 == 17562
    assert sensor._dig_t2 == -19729
    assert sensor._dig_h1 == 230
    # dig_H4 = sign12((0x8B << 4) | (0x02 & 0x0F)) = sign12(0x8B2) = -1870
    assert sensor._dig_h4 == -1870
    # dig_H5 = sign12((0x01 << 4) | (0x02 >> 4)) = sign12(0x10) = 16
    assert sensor._dig_h5 == 16


# --- Compensation against an independent re-derivation ---------------------

# A plausible factory calibration and raw ADC set (20-bit T/P, 16-bit H).
CAL = {
    "t1": 17570, "t2": -19041, "t3": -1302,
    "p1": 36781, "p2": -10685, "p3": 3024, "p4": 2857, "p5": -14193,
    "p6": 13505, "p7": -13997, "p8": 482, "p9": 3152,
    "h1": 230, "h2": -5071, "h3": 11, "h4": 1183, "h5": -1200, "h6": -9,
}
ADC_T = 0x0166A  # 5738
ADC_P = 0x6586C  # 415148
ADC_H = 0x2E8C   # 11916


def _ref_temperature(adc_t):
    c = CAL
    var1 = (adc_t / 16384.0 - c["t1"] / 1024.0) * c["t2"]
    var2 = ((adc_t / 131072.0 - c["t1"] / 8192.0) ** 2) * c["t3"]
    t_fine = int(var1 + var2)
    return (var1 + var2) / 5120.0, t_fine


def _ref_pressure(adc_p, t_fine):
    c = CAL
    var1 = t_fine / 2.0 - 64000.0
    var2 = var1 * var1 * c["p6"] / 32768.0
    var2 = var2 + var1 * c["p5"] * 2.0
    var2 = var2 / 4.0 + c["p4"] * 65536.0
    var1 = (c["p3"] * var1 * var1 / 524288.0 + c["p2"] * var1) / 524288.0
    var1 = (1.0 + var1 / 32768.0) * c["p1"]
    pressure = 1048576.0 - adc_p
    pressure = (pressure - var2 / 4096.0) * 6250.0 / var1
    var1 = c["p9"] * pressure * pressure / 2147483648.0
    var2 = pressure * c["p8"] / 32768.0
    pressure = pressure + (var1 + var2 + c["p7"]) / 16.0
    return pressure


def _ref_humidity(adc_h, t_fine):
    c = CAL
    var_h = t_fine - 76800.0
    var_h = (adc_h - (c["h4"] * 64.0 + c["h5"] / 16384.0 * var_h)) * (
        c["h2"] / 65536.0
        * (1.0 + c["h6"] / 67108864.0 * var_h * (1.0 + c["h3"] / 67108864.0 * var_h))
    )
    var_h = var_h * (1.0 - c["h1"] * var_h / 524288.0)
    return _clamp_humidity(var_h)


def _sensor_with_calibration():
    sensor = _sensor()
    for name, value in CAL.items():
        setattr(sensor, "_dig_" + name, value)
    return sensor


def test_temperature_matches_reference():
    sensor = _sensor_with_calibration()
    expected, t_fine = _ref_temperature(ADC_T)
    result = sensor._compensate_temperature(ADC_T)
    assert abs(result - expected) < 1e-6
    assert sensor._t_fine == t_fine  # t_fine is stored for P/H


def test_pressure_matches_reference():
    sensor = _sensor_with_calibration()
    _, t_fine = _ref_temperature(ADC_T)
    sensor._t_fine = t_fine
    expected = _ref_pressure(ADC_P, t_fine)
    result = sensor._compensate_pressure(ADC_P)
    assert abs(result - expected) < 1e-6
    # Pressure comes out in Pa and lands in the sensor's 300-1100 hPa range.
    assert 30000.0 <= result <= 110000.0


def test_humidity_matches_reference():
    sensor = _sensor_with_calibration()
    _, t_fine = _ref_temperature(ADC_T)
    sensor._t_fine = t_fine
    expected = _ref_humidity(ADC_H, t_fine)
    result = sensor._compensate_humidity(ADC_H)
    assert abs(result - expected) < 1e-6
    assert 0.0 <= result <= 100.0


# --- Edge cases ------------------------------------------------------------


def test_pressure_divide_by_zero_guard():
    sensor = _sensor()
    sensor._dig_p1 = 0  # blank-NVM pressure coefficient
    sensor._t_fine = 0
    with pytest.raises(ArithmeticError):
        sensor._compensate_pressure(ADC_P)


def test_humidity_clamp_helper():
    assert _clamp_humidity(150.0) == 100.0
    assert _clamp_humidity(-5.0) == 0.0
    assert _clamp_humidity(42.5) == 42.5
    assert _clamp_humidity(0.0) == 0.0
    assert _clamp_humidity(100.0) == 100.0


def test_measurement_time_from_oversampling():
    assert _sensor(osrs_t=1, osrs_p=1, osrs_h=1)._measurement_time_ms() == 11
    assert _sensor(osrs_t=1, osrs_p=0, osrs_h=0)._measurement_time_ms() == 5
    assert _sensor(osrs_t=0, osrs_p=0, osrs_h=1)._measurement_time_ms() == 6
