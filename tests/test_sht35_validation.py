# test_sht35_validation.py - Pure SHT35 config validation
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Host-side tests for the pure ``sht35`` config validator: the complete
accept/reject contract (unknown keys, the required key, every field bound,
the address-candidate rules, the repeatability membership set, offsets shape
and ranges) and the driver's reuse of that same validator in ``initialize()``
(startup == write-time rules). Nothing here touches hardware -- a valid
config with no physical sensor still passes; the import chain reaches no
``machine`` module, so it runs on CPython."""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from devices.sht35.validation import (  # noqa: E402
    ALLOWED_CONFIG_KEYS,
    validate_config,
)
from devices.sht35.sht35_device import SHT35Device  # noqa: E402
from devices.device import DeviceValidationError  # noqa: E402


def _valid_config():
    """The minimal valid config: the one required key only."""
    return {"i2c_bus": 0}


def _full_config():
    return {
        "i2c_bus": 1,
        "i2c_sda_pin": 6,
        "i2c_scl_pin": 7,
        "i2c_freq_hz": 400000,
        "i2c_address_candidates": [68, 69],
        "repeatability": "high",
        "offsets": {
            "temperature_c": 0.5,
            "humidity_percent": -2.0,
        },
    }


# The two offset sub-keys, in a stable order for the parameterized tests.
_OFFSET_KEY_LIST = ("temperature_c", "humidity_percent")


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
        "i2c_address_candidates": [68],
        "repeatability": "medium",
        "offsets": {"temperature_c": 0},
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


# --- i2c_bus (required) ----------------------------------------------------


def test_missing_i2c_bus():
    config = _valid_config()
    del config["i2c_bus"]
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
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


def test_rejects_a_pin_that_cannot_route_to_the_selected_bus():
    # 2 is I2C1's SDA pin, not I2C0's: routable and in-range, but the wrong
    # controller -- a deterministic configuration error, not an operational
    # device failure at machine.I2C(...) construction.
    config = _valid_config()
    config["i2c_sda_pin"] = 2
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"


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


# --- i2c_address_candidates ------------------------------------------------


def test_rejects_an_empty_candidate_list():
    config = _valid_config()
    config["i2c_address_candidates"] = []
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"


def test_rejects_a_non_list_candidate_value():
    config = _valid_config()
    config["i2c_address_candidates"] = 68
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"


@pytest.mark.parametrize("bad", [67, 70, 0, 255, "68", 68.0])
def test_rejects_an_invalid_candidate_entry(bad):
    config = _valid_config()
    config["i2c_address_candidates"] = [bad]
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"


def test_rejects_a_duplicate_candidate_entry():
    config = _valid_config()
    config["i2c_address_candidates"] = [68, 68]
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"


def test_accepts_single_and_both_valid_candidates():
    for candidates in ([68], [69], [69, 68], [68, 69]):
        config = _valid_config()
        config["i2c_address_candidates"] = candidates
        assert validate_config(config) is None, candidates


# --- repeatability ----------------------------------------------------------


@pytest.mark.parametrize("bad", ["HIGH", "High", "normal", "continuous",
                                 1, 0, True, None, ["high"]])
def test_rejects_an_invalid_repeatability(bad):
    config = _valid_config()
    config["repeatability"] = bad
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"


@pytest.mark.parametrize("good", ["high", "medium", "low"])
def test_accepts_each_valid_repeatability(good):
    config = _valid_config()
    config["repeatability"] = good
    assert validate_config(config) is None


# --- offsets ----------------------------------------------------------------


def test_rejects_a_non_dict_offsets():
    config = _valid_config()
    config["offsets"] = [0, 0]
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"


def test_rejects_an_unknown_offset_key():
    config = _valid_config()
    config["offsets"] = {"dew_point_c": 5}
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "unknown_config_fields"
    assert "dew_point_c" in str(excinfo.value)


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
        ("temperature_c", 100.0),
        ("humidity_percent", 100.0),
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
        {"i2c_bus": 0, "i2c_address_candidates": [68, 68]},  # duplicate
        {"i2c_bus": 0, "repeatability": "continuous"},  # bad membership
        {"i2c_bus": 0, "offsets": {"bogus": 1}},  # unknown offset key
        {"i2c_bus": 0, "i2c_freq_hz": 2000000},  # above the wire ceiling
    ],
)
def test_initialize_rejects_the_same_invalid_configs(bad):
    device = SHT35Device(_DummyI2C())
    with pytest.raises(DeviceValidationError):
        device.initialize(bad)
