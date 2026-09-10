# test_ltr390_compensation.py - LTR390 decode, part ID, and conversions
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Host-side tests for the LTR390's pure protocol and conversion logic: the
20-bit little-endian decode (including the reserved high nibble), the
PART_ID part-number-nibble check (never a hard-coded silicon revision), the
register construction, the bounded data-ready timeout, and the lux / UVI
conversions at the documented reference points. Nothing here performs a
measurement; the I2C object is a tiny canned fake for ``init()`` only."""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from devices.ltr390.ltr390_device import (  # noqa: E402
    LTR390,
    data_ready_timeout_ms,
    decode_raw20,
)


def _sensor(i2c=None, gain=3, resolution_bits=18, measurement_rate_ms=200, window_factor=1.0):
    """A sensor for pure-conversion checks: no I2C by default (no init/read is
    called); the register-construction tests inject the recording fake."""
    return LTR390(
        i2c,
        gain=gain,
        resolution_bits=resolution_bits,
        measurement_rate_ms=measurement_rate_ms,
        window_factor=window_factor,
    )


# --- 20-bit decode ---------------------------------------------------------


def test_decode_raw20_little_endian():
    # The doc's reference vector: low byte, middle byte, high nibble.
    assert decode_raw20(bytes((0x56, 0x34, 0x02))) == 0x23456


def test_decode_raw20_masks_the_reserved_high_nibble():
    # The upper nibble of the third byte is reserved: it must be masked off,
    # so a junked high nibble cannot change the decoded value.
    assert decode_raw20(bytes((0x56, 0x34, 0xF2))) == 0x23456
    assert decode_raw20(bytes((0xFF, 0xFF, 0xFF))) == 0xFFFFF


def test_decode_raw20_full_scale():
    assert decode_raw20(bytes((0xFF, 0xFF, 0x0F))) == 0xFFFFF


# --- PART_ID ---------------------------------------------------------------


class _PartIdI2C:
    """Answers only the PART_ID read (register 0x06), the data-register reads
    of the init clear sequence, and records nothing else."""

    def __init__(self, part_id):
        self.part_id = part_id

    def readfrom_mem_into(self, address, register, buffer):
        if register == 0x06:
            buffer[0] = self.part_id
        else:
            buffer[:] = b"\x00\x00\x00"

    def writeto_mem(self, address, register, data):
        pass


@pytest.mark.parametrize("part_id", [0xB2, 0xB3, 0xB7, 0xBF])
def test_detect_accepts_any_silicon_revision(part_id):
    sensor = _sensor(_PartIdI2C(part_id))
    sensor._detect()  # the nibble check, in isolation
    assert sensor._part_id == part_id
    assert sensor._revision_id == part_id & 0x0F


@pytest.mark.parametrize("part_id", [0xA2, 0xC2, 0x52, 0x00, 0x10])
def test_detect_rejects_a_wrong_part_number(part_id):
    sensor = LTR390(_PartIdI2C(part_id), gain=3, resolution_bits=18, measurement_rate_ms=200, window_factor=1.0)
    with pytest.raises(OSError) as excinfo:
        sensor._detect()
    assert "0x{:02X}".format(part_id) in str(excinfo.value)


# --- Register construction -------------------------------------------------


class _RecordingI2C:
    """Canned part ID + zero data, records every write so the init register
    set and its values can be asserted."""

    def __init__(self, part_id=0xB2):
        self.part_id = part_id
        self.writes = []

    def readfrom_mem_into(self, address, register, buffer):
        if register == 0x06:
            buffer[0] = self.part_id
        else:
            buffer[:] = b"\x00\x00\x00"

    def writeto_mem(self, address, register, data):
        self.writes.append((register, data[0]))


def test_init_writes_the_default_profile():
    """Defaults (x3 / 18-bit / 200 ms): MEAS_RATE = (code 2 << 4) | code 3,
    GAIN = code 1, INT_CFG disabled, ending in ALS standby."""
    i2c = _RecordingI2C()
    _sensor(i2c, gain=3, resolution_bits=18, measurement_rate_ms=200).init()
    assert (0x04, (2 << 4) | 3) in i2c.writes
    assert (0x05, 1) in i2c.writes
    assert (0x19, 0x00) in i2c.writes
    # The final MAIN_CTRL write of init is the ALS standby value.
    main_ctrl = [value for register, value in i2c.writes if register == 0x00]
    assert main_ctrl[-1] == 0x00


def test_init_writes_a_uv_sensitive_profile():
    """x18 / 20-bit / 1000 ms: MEAS_RATE = (code 0 << 4) | code 5, GAIN = 4."""
    i2c = _RecordingI2C()
    _sensor(i2c, gain=18, resolution_bits=20, measurement_rate_ms=1000).init()
    assert (0x04, (0 << 4) | 5) in i2c.writes
    assert (0x05, 4) in i2c.writes


# --- Bounded data-ready timeout --------------------------------------------


@pytest.mark.parametrize(
    "resolution_code,timeout_ms",
    [
        (0, 460),   # 20-bit: 10 + 400 + 50
        (1, 260),   # 19-bit: 10 + 200 + 50
        (2, 160),   # 18-bit: 10 + 100 + 50
        (3, 110),   # 17-bit: 10 + 50 + 50
        (4, 85),    # 16-bit: 10 + 25 + 50
        (5, 72),    # 13-bit: 10 + 12.5 + 50, truncated
    ],
)
def test_data_ready_timeout_scales_with_the_conversion_time(resolution_code, timeout_ms):
    assert data_ready_timeout_ms(resolution_code) == timeout_ms


# --- Lux conversion ---------------------------------------------------------


def test_lux_at_the_documented_reference_point():
    # Doc section 48: raw 10000, gain x3, 18-bit (INT 1.0), window 1.0 -> 2000 lux.
    assert _sensor()._calculate_lux(10000) == 2000.0


def test_lux_scales_with_gain_and_resolution():
    # 20-bit (INT 4.0) at x1: 0.6 * raw / 4.
    assert _sensor(gain=1, resolution_bits=20)._calculate_lux(10000) == 1500.0
    # 13-bit (INT 0.03125) at x18: 0.6 * raw / (18 * 0.03125) = raw / 0.9375.
    assert _sensor(gain=18, resolution_bits=13)._calculate_lux(9375) == pytest.approx(10000.0)


def test_lux_applies_the_window_factor():
    assert _sensor(window_factor=2.0)._calculate_lux(10000) == 4000.0


def test_lux_is_zero_at_zero_raw():
    assert _sensor()._calculate_lux(0) == 0.0


# --- UVI conversion ---------------------------------------------------------


def test_uvi_at_the_reference_operating_point():
    # x18 / 20-bit: 2300 counts per UVI directly.
    assert _sensor(gain=18, resolution_bits=20)._calculate_uvi(2300) == pytest.approx(1.0)


def test_uvi_scales_at_lower_gain_and_resolution():
    # Doc section 53: x18 / 18-bit (INT 1.0) -> 2300 * (18/18) * (1/4) = 575.
    assert _sensor(gain=18, resolution_bits=18)._calculate_uvi(575) == pytest.approx(1.0)
    # Doc section 54: x3 / 18-bit -> 2300 * (3/18) * (1/4) = 2300/24 counts/UVI,
    # so 2300 raw counts is 24 UVI exactly.
    assert _sensor(gain=3, resolution_bits=18)._calculate_uvi(2300) == pytest.approx(24.0)


def test_uvi_applies_the_window_factor():
    assert _sensor(gain=18, resolution_bits=20, window_factor=2.0)._calculate_uvi(2300) == pytest.approx(2.0)


def test_uvi_is_zero_at_zero_raw():
    assert _sensor()._calculate_uvi(0) == 0.0


# --- Driver guards ----------------------------------------------------------


@pytest.mark.parametrize("bad", [0.99, 0.5, 0.0, -1])
def test_the_driver_rejects_a_sub_unity_window_factor(bad):
    with pytest.raises(ValueError):
        LTR390(
            None,
            gain=3,
            resolution_bits=18,
            measurement_rate_ms=200,
            window_factor=bad,
        )
