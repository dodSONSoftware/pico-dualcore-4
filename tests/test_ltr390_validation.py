# test_ltr390_validation.py - Pure LTR390 config validation
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Host-side tests for the pure ``ltr390`` config validator: the complete
accept/reject contract (unknown keys, required keys, every field bound, the
rate-vs-conversion cross-field rule, window factor and offsets shape and
ranges) and the driver's reuse of that same validator in ``initialize()``
(startup == write-time rules). Nothing here touches hardware -- a valid config
with no physical sensor still passes; the import chain reaches no ``machine``
module, so it runs on CPython."""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from devices.ltr390.validation import (  # noqa: E402
    ALLOWED_CONFIG_KEYS,
    validate_config,
)
from devices.ltr390.ltr390_device import LTR390Device  # noqa: E402
from devices.device import DeviceValidationError  # noqa: E402


def _valid_config():
    """The minimal valid config: the one required key only."""
    return {"i2c_bus": 0}


def _full_config():
    return {
        "i2c_bus": 1,
        "i2c_sda_pin": 4,
        "i2c_scl_pin": 5,
        "i2c_freq_hz": 400000,
        "gain": 18,
        "resolution_bits": 20,
        "measurement_rate_ms": 1000,
        "window_factor": 1.5,
        "offsets": {
            "lux": 12.5,
            "uv_index": -0.5,
        },
    }


# The two offset sub-keys, in a stable order for the parameterized tests.
_OFFSET_KEY_LIST = ("lux", "uv_index")


def test_valid_minimal_config_passes():
    assert validate_config(_valid_config()) is None


def test_valid_full_config_passes():
    assert validate_config(_full_config()) is None


def test_every_allowed_key_is_accepted_on_its_own():
    """Each allowed key, set to a valid value alongside the required key, is
    recognized (this pins the ALLOWED set against accidental drift)."""
    values = {
        "i2c_bus": 0,
        "i2c_sda_pin": 4,
        "i2c_scl_pin": 5,
        "i2c_freq_hz": 400000,
        "gain": 3,
        "resolution_bits": 18,
        "measurement_rate_ms": 200,
        "window_factor": 1.0,
        "offsets": {"lux": 0},
    }
    for key in sorted(ALLOWED_CONFIG_KEYS):
        config = _valid_config()
        config[key] = values[key]
        assert validate_config(config) is None, key


# --- Shape -----------------------------------------------------------------


@pytest.mark.parametrize("bad", ["a list", 42, None, ["i2c_bus"]])
def test_rejects_a_non_dict_config(bad):
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(bad)
    assert excinfo.value.code == "invalid_value"


def test_rejects_an_unknown_config_key():
    config = _valid_config()
    config["bogus"] = 1
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "unknown_config_fields"
    assert "bogus" in str(excinfo.value)


# The LTR390 address is fixed at 0x53: a config that tries to override it is
# unknown, not an alternative address.
def test_rejects_an_address_key():
    config = _valid_config()
    config["i2c_address"] = 83
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "unknown_config_fields"
    assert "i2c_address" in str(excinfo.value)


# --- i2c_bus (required) ----------------------------------------------------


def test_missing_i2c_bus():
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config({})
    assert excinfo.value.code == "missing_key"


@pytest.mark.parametrize(
    "bad", [-1, 2, 3, "0", 0.0, True, None, [0]]
)
def test_rejects_an_out_of_range_i2c_bus(bad):
    config = _valid_config()
    config["i2c_bus"] = bad
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"


# --- i2c_sda_pin / i2c_scl_pin ---------------------------------------------


@pytest.mark.parametrize("key", ["i2c_sda_pin", "i2c_scl_pin"])
@pytest.mark.parametrize("bad", [-1, 30, 100, "4", True, 4.0])
def test_rejects_an_out_of_range_i2c_pin(key, bad):
    config = _valid_config()
    config[key] = bad
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"


def test_rejects_identical_i2c_pins():
    config = _valid_config()
    config["i2c_sda_pin"] = 4
    config["i2c_scl_pin"] = 4
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"


def test_accepts_distinct_i2c_pins():
    config = _valid_config()
    config["i2c_sda_pin"] = 4
    config["i2c_scl_pin"] = 5
    assert validate_config(config) is None


# --- i2c_freq_hz -----------------------------------------------------------


@pytest.mark.parametrize("bad", [0, 99999, 1000001, "400000", True, 400000.0])
def test_rejects_an_out_of_range_i2c_freq(bad):
    config = _valid_config()
    config["i2c_freq_hz"] = bad
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"


@pytest.mark.parametrize("good", [100000, 400000, 1000000])
def test_accepts_a_standard_i2c_freq(good):
    config = _valid_config()
    config["i2c_freq_hz"] = good
    assert validate_config(config) is None


# --- gain ------------------------------------------------------------------


@pytest.mark.parametrize("good", [1, 3, 6, 9, 18])
def test_accepts_a_valid_gain(good):
    config = _valid_config()
    config["gain"] = good
    assert validate_config(config) is None, good


@pytest.mark.parametrize("bad", [0, 2, 5, 12, "3", True, 3.0])
def test_rejects_an_invalid_gain(bad):
    config = _valid_config()
    config["gain"] = bad
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"


# --- resolution_bits -------------------------------------------------------


@pytest.mark.parametrize("good", [13, 16, 17, 18, 19, 20])
def test_accepts_a_valid_resolution(good):
    config = _valid_config()
    config["resolution_bits"] = good
    # Pair with the slowest rate: compatible with every resolution's
    # conversion time, so the cross-field rule cannot mask the membership check.
    config["measurement_rate_ms"] = 2000
    assert validate_config(config) is None, good


@pytest.mark.parametrize("bad", [12, 14, 15, 21, "18", True, 18.0])
def test_rejects_an_invalid_resolution(bad):
    config = _valid_config()
    config["resolution_bits"] = bad
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"


# --- measurement_rate_ms ---------------------------------------------------


@pytest.mark.parametrize("good", [25, 50, 100, 200, 500, 1000, 2000])
def test_accepts_a_valid_measurement_rate(good):
    # The conversion-time cross-field rule applies: pair each rate with the
    # resolution it is compatible with (13-bit, 12.5 ms conversion).
    config = _valid_config()
    config["measurement_rate_ms"] = good
    config["resolution_bits"] = 13
    assert validate_config(config) is None, good


@pytest.mark.parametrize("bad", [0, 30, 250, "100", True, 100.0])
def test_rejects_an_invalid_measurement_rate(bad):
    config = _valid_config()
    config["measurement_rate_ms"] = bad
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"


# --- Cross-field: rate vs ADC conversion time ------------------------------


@pytest.mark.parametrize(
    "resolution,too_fast_rate",
    [
        (20, 200),   # 400 ms conversion
        (20, 25),    # 400 ms conversion
        (19, 100),   # 200 ms conversion
        (18, 50),    # 100 ms conversion
        (17, 25),    # 50 ms conversion
        # 16-bit (25 ms) and 13-bit (12.5 ms) have no valid rate faster than
        # their conversion -- the fastest valid rate (25 ms) is already at or
        # above both, so there is no cross-field rejection to test there.
    ],
)
def test_rejects_a_rate_faster_than_the_conversion(resolution, too_fast_rate):
    config = _valid_config()
    config["resolution_bits"] = resolution
    config["measurement_rate_ms"] = too_fast_rate
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"
    assert "conversion" in str(excinfo.value)


@pytest.mark.parametrize(
    "resolution,rate",
    [
        (20, 2000),
        (19, 200),    # boundary equality
        (18, 100),    # boundary equality
        (17, 50),     # boundary equality
        (16, 25),     # boundary equality
        (13, 25),     # fastest rate, fastest conversion
    ],
)
def test_accepts_a_rate_matching_the_conversion(resolution, rate):
    config = _valid_config()
    config["resolution_bits"] = resolution
    config["measurement_rate_ms"] = rate
    assert validate_config(config) is None, (resolution, rate)


# --- window_factor ---------------------------------------------------------


@pytest.mark.parametrize("good", [1.0, 1.5, 2, 100.0])
def test_accepts_a_valid_window_factor(good):
    config = _valid_config()
    config["window_factor"] = good
    assert validate_config(config) is None, good


@pytest.mark.parametrize(
    "bad", [0.99, 0.5, -1, 100.1, 1000, "1.0", None, float("inf"), float("nan"), True]
)
def test_rejects_an_invalid_window_factor(bad):
    config = _valid_config()
    config["window_factor"] = bad
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"


# --- offsets ----------------------------------------------------------------


def test_rejects_a_non_dict_offsets():
    config = _valid_config()
    config["offsets"] = [0, 0]
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"


def test_rejects_an_unknown_offset_key():
    config = _valid_config()
    config["offsets"] = {"als_raw": 5}
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "unknown_config_fields"
    assert "als_raw" in str(excinfo.value)


@pytest.mark.parametrize("key", _OFFSET_KEY_LIST)
@pytest.mark.parametrize("bad", ["1", None, float("inf"), float("nan"), True])
def test_rejects_a_non_finite_offset(key, bad):
    config = _valid_config()
    config["offsets"] = {key: bad}
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"


@pytest.mark.parametrize(
    "key,bound",
    [
        ("lux", 50000.0),
        ("uv_index", 12.0),
    ],
)
def test_rejects_an_over_bound_offset(key, bound):
    config = _valid_config()
    config["offsets"] = {key: bound + 1}
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"
    # The bound is inclusive: exactly +/-bound is accepted.
    for edge in (bound, -bound):
        config = _valid_config()
        config["offsets"] = {key: edge}
        assert validate_config(config) is None, (key, edge)


# --- initialize() reuses the same pure validator ---------------------------


class _DummyI2C:
    """initialize() validates before touching the bus, so an invalid config
    raises before this is ever called; a valid one is not exercised here."""


@pytest.mark.parametrize(
    "bad",
    [
        {},  # missing i2c_bus
        {"i2c_bus": 2},  # bad bus
        {"i2c_bus": 0, "i2c_address": 83},  # fixed-address device: no address key
        {"i2c_bus": 0, "gain": 12},  # invalid gain
        {"i2c_bus": 0, "resolution_bits": 15},  # reserved resolution code
        {
            "i2c_bus": 0,
            "resolution_bits": 20,
            "measurement_rate_ms": 100,  # 400 ms conversion, 100 ms period
        },
        {"i2c_bus": 0, "window_factor": 0.5},  # attenuation only: >= 1.0
        {"i2c_bus": 0, "offsets": {"bogus": 1}},  # unknown offset key
        {"i2c_bus": 0, "i2c_sda_pin": 4, "i2c_scl_pin": 4},  # sda == scl
    ],
)
def test_initialize_rejects_the_same_invalid_configs(bad):
    device = LTR390Device(_DummyI2C())
    with pytest.raises(DeviceValidationError):
        device.initialize(bad)
