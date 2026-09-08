# test_bme280_validation.py - Pure BME280 config validation
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Host-side tests for the pure ``bme280`` config validator: the complete
accept/reject contract (unknown keys, required keys, every field bound, the
two oversampling cross-field rules, offsets shape and ranges) and the driver's
reuse of that same validator in ``initialize()`` (startup == write-time rules).
Nothing here touches hardware -- a valid config with no physical sensor still
passes; the import chain reaches no ``machine`` module, so it runs on CPython."""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from devices.bme280.validation import (  # noqa: E402
    ALLOWED_CONFIG_KEYS,
    validate_config,
)
from devices.bme280.bme280_device import BME280Device  # noqa: E402
from devices.device import DeviceValidationError  # noqa: E402


def _valid_config():
    """The minimal valid config: the two required keys only."""
    return {"i2c_bus": 0, "sea_level_pressure_pa": 101325}


def _full_config():
    return {
        "i2c_bus": 1,
        "i2c_sda_pin": 4,
        "i2c_scl_pin": 5,
        "i2c_freq_hz": 400000,
        "i2c_address_candidates": [118, 119],
        "temperature_oversampling": 2,
        "pressure_oversampling": 4,
        "humidity_oversampling": 1,
        "iir_filter": 3,
        "sea_level_pressure_pa": 101325,
        "offsets": {
            "temperature_c": 0.5,
            "humidity_percent": -2.0,
            "pressure_pascal": 1234,
        },
    }


# The three offset sub-keys, in a stable order for the parameterized tests.
_OFFSET_KEY_LIST = ("temperature_c", "humidity_percent", "pressure_pascal")


def test_valid_minimal_config_passes():
    assert validate_config(_valid_config()) is None


def test_valid_full_config_passes():
    assert validate_config(_full_config()) is None


def test_every_allowed_key_is_accepted_on_its_own():
    """Each allowed key, set to a valid value alongside the required pair, is
    recognized (this pins the ALLOWED set against accidental drift)."""
    values = {
        "i2c_bus": 0,
        "i2c_sda_pin": 4,
        "i2c_scl_pin": 5,
        "i2c_freq_hz": 400000,
        "i2c_address_candidates": [118],
        "temperature_oversampling": 1,
        "pressure_oversampling": 1,
        "humidity_oversampling": 1,
        "iir_filter": 0,
        "sea_level_pressure_pa": 101325,
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


# --- i2c_sda_pin / i2c_scl_pin -----------------------------------------------------


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


# --- i2c_address_candidates ------------------------------------------------


def test_rejects_an_empty_candidate_list():
    config = _valid_config()
    config["i2c_address_candidates"] = []
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"


def test_rejects_a_non_list_candidate_value():
    config = _valid_config()
    config["i2c_address_candidates"] = 118
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"


@pytest.mark.parametrize("bad", [117, 120, 0, 255, "118", 118.0])
def test_rejects_an_invalid_candidate_entry(bad):
    config = _valid_config()
    config["i2c_address_candidates"] = [bad]
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"


def test_rejects_a_duplicate_candidate_entry():
    config = _valid_config()
    config["i2c_address_candidates"] = [118, 118]
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"


def test_accepts_single_and_both_valid_candidates():
    for candidates in ([118], [119], [119, 118], [118, 119]):
        config = _valid_config()
        config["i2c_address_candidates"] = candidates
        assert validate_config(config) is None, candidates


# --- Oversampling and IIR filter -------------------------------------------


@pytest.mark.parametrize(
    "key",
    ["temperature_oversampling", "pressure_oversampling", "humidity_oversampling"],
)
@pytest.mark.parametrize("bad", [-1, 6, 7, "1", True, 1.0])
def test_rejects_an_out_of_range_oversampling(key, bad):
    config = _valid_config()
    config[key] = bad
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"


@pytest.mark.parametrize("bad", [-1, 5, 9, "0", True, 0.0])
def test_rejects_an_out_of_range_iir_filter(bad):
    config = _valid_config()
    config["iir_filter"] = bad
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"


# --- Cross-field oversampling rules ----------------------------------------


def test_rejects_pressure_without_temperature():
    """t_fine rule: a fresh pressure compensation needs the temperature channel."""
    config = _valid_config()
    config["temperature_oversampling"] = 0
    config["pressure_oversampling"] = 2
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"


def test_rejects_humidity_skipped_with_temperature_off():
    """Humidity also consumes t_fine, so temperature-off + humidity-on is
    rejected by the t_fine rule too (temperature can never be the sole skip --
    that always trips either this rule or the all-skip rule)."""
    config = _valid_config()
    config["temperature_oversampling"] = 0
    config["humidity_oversampling"] = 1
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"


def test_rejects_an_all_skipped_profile():
    config = _valid_config()
    config["temperature_oversampling"] = 0
    config["pressure_oversampling"] = 0
    config["humidity_oversampling"] = 0
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"


def test_accepts_a_single_enabled_channel():
    config = _valid_config()
    config["temperature_oversampling"] = 1
    config["pressure_oversampling"] = 0
    config["humidity_oversampling"] = 0
    assert validate_config(config) is None


# --- sea_level_pressure_pa (required) --------------------------------------


def test_missing_sea_level_pressure():
    config = _valid_config()
    del config["sea_level_pressure_pa"]
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "missing_key"


@pytest.mark.parametrize(
    "bad", [0, 29999, 115001, "101325", None, float("inf"), float("nan"), True]
)
def test_rejects_an_out_of_range_sea_level_pressure(bad):
    config = _valid_config()
    config["sea_level_pressure_pa"] = bad
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"


def test_accepts_boundary_sea_level_pressures():
    for value in (30000, 115000, 101325.5):
        config = _valid_config()
        config["sea_level_pressure_pa"] = value
        assert validate_config(config) is None, value


# --- offsets ----------------------------------------------------------------


def test_rejects_a_non_dict_offsets():
    config = _valid_config()
    config["offsets"] = [0, 0, 0]
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"


def test_rejects_an_unknown_offset_key():
    config = _valid_config()
    config["offsets"] = {"altitude_m": 5}
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "unknown_config_fields"
    assert "altitude_m" in str(excinfo.value)


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
        ("pressure_pascal", 200000.0),
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
        {"i2c_bus": 0},  # missing sea_level_pressure_pa
        {"sea_level_pressure_pa": 101325},  # missing i2c_bus
        {"i2c_bus": 2, "sea_level_pressure_pa": 101325},  # bad bus
        {
            "i2c_bus": 0,
            "sea_level_pressure_pa": 101325,
            "i2c_address_candidates": [118, 118],  # duplicate
        },
        {
            "i2c_bus": 0,
            "sea_level_pressure_pa": 101325,
            "temperature_oversampling": 0,
            "pressure_oversampling": 2,  # t_fine rule
        },
        {
            "i2c_bus": 0,
            "sea_level_pressure_pa": 101325,
            "offsets": {"bogus": 1},  # unknown offset key
        },
    ],
)
def test_initialize_rejects_the_same_invalid_configs(bad):
    device = BME280Device(_DummyI2C())
    with pytest.raises(DeviceValidationError):
        device.initialize(bad)
