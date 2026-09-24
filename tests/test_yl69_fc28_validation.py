# test_yl69_fc28_validation.py - Pure YL-69/FC-28 config validation
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Host-side tests for the pure ``yl69_fc28`` config validator: the complete
accept/reject contract (unknown keys, the ADC-capable pin set, the optional
DO/power pins and their within-device distinctness, the filtering bounds,
the required dry/wet calibration) and the driver's reuse of that same
validator in ``initialize()`` (startup == write-time rules). Nothing here
touches hardware -- a valid config with no physical sensor still passes;
the import chain reaches no ``machine`` module, so it runs on CPython."""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from devices.yl69_fc28.validation import (  # noqa: E402
    ADC_CAPABLE_PINS,
    ALLOWED_CONFIG_KEYS,
    DEFAULT_SAMPLE_COUNT,
    DEFAULT_SAMPLE_DELAY_MS,
    DEFAULT_SETTLE_MS,
    validate_config,
)
from devices.yl69_fc28.yl69_fc28_device import Yl69Fc28Device  # noqa: E402
from devices.device import DeviceValidationError  # noqa: E402


def _valid_config():
    """The minimal valid config: the required keys only."""
    return {"adc_pin": 26, "dry_raw": 52000, "wet_raw": 22000}


def _full_config():
    return {
        "adc_pin": 27,
        "digital_pin": 15,
        "power_pin": 14,
        "power_active_low": False,
        "settle_ms": 0,
        "sample_count": 101,
        "sample_delay_ms": 50,
        "dry_raw": 0,
        "wet_raw": 65535,
    }


def test_valid_minimal_config_passes():
    assert validate_config(_valid_config()) is None


def test_valid_full_config_passes():
    assert validate_config(_full_config()) is None


def test_every_allowed_key_is_accepted_on_its_own():
    """Each allowed key, set to a valid value alongside the required keys, is
    recognized (this pins the ALLOWED set against accidental drift)."""
    values = {
        "adc_pin": 28,
        "digital_pin": 15,
        "power_pin": 14,
        "power_active_low": False,
        "settle_ms": 0,
        "sample_count": 1,
        "sample_delay_ms": 0,
        "dry_raw": 100,
        "wet_raw": 900,
    }
    for key in sorted(ALLOWED_CONFIG_KEYS):
        config = _valid_config()
        config[key] = values[key]
        assert validate_config(config) is None, key


def test_config_must_be_an_object():
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(["adc_pin", 26])
    assert excinfo.value.code == "invalid_value"


def test_unknown_key_is_rejected_qualified():
    config = _valid_config()
    config["i2c_bus"] = 0
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "unknown_config_fields"
    assert "i2c_bus" in str(excinfo.value)


# --- adc_pin -----------------------------------------------------------------


def test_missing_adc_pin_is_rejected():
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config({"dry_raw": 52000, "wet_raw": 22000})
    assert excinfo.value.code == "missing_key"
    assert "adc_pin" in str(excinfo.value)


@pytest.mark.parametrize(
    "bad_pin", [True, False, "26", 26.0, None, -1, 0, 15, 22, 23, 24, 25, 29, 30, 100]
)
def test_non_adc_capable_pin_is_rejected(bad_pin):
    # Only GP26/27/28 have an ADC input on the Pico W / Pico 2 W; every
    # other exposed GPIO (and the wireless-reserved pads) is rejected.
    config = _valid_config()
    config["adc_pin"] = bad_pin
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"


@pytest.mark.parametrize("good_pin", ADC_CAPABLE_PINS)
def test_adc_capable_pins_are_accepted(good_pin):
    config = _valid_config()
    config["adc_pin"] = good_pin
    assert validate_config(config) is None


def test_adc_capable_set_is_the_three_exposed_channels():
    assert ADC_CAPABLE_PINS == (26, 27, 28)


# --- digital_pin / power_pin ---------------------------------------------------


def test_optional_pins_absent_is_valid():
    assert validate_config(_valid_config()) is None


@pytest.mark.parametrize("bad_pin", [True, "15", 15.0, None, -1, 23, 24, 25, 29, 30])
def test_invalid_digital_pin_is_rejected(bad_pin):
    config = _valid_config()
    config["digital_pin"] = bad_pin
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"


@pytest.mark.parametrize("bad_pin", [True, "14", 14.0, None, -1, 23, 24, 25, 29, 30])
def test_invalid_power_pin_is_rejected(bad_pin):
    config = _valid_config()
    config["power_pin"] = bad_pin
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"


@pytest.mark.parametrize("good_pin", [0, 15, 22, 27])
def test_exposed_gpio_digital_pin_is_accepted(good_pin):
    config = _valid_config()
    config["digital_pin"] = good_pin
    assert validate_config(config) is None


@pytest.mark.parametrize("good_pin", [0, 14, 22, 28])
def test_exposed_gpio_power_pin_is_accepted(good_pin):
    config = _valid_config()
    config["power_pin"] = good_pin
    assert validate_config(config) is None


@pytest.mark.parametrize(
    "config",
    [
        # Each of the three pins carries a distinct physical signal (AO /
        # DO / power-switch control): no two may name the same GPIO.
        {"adc_pin": 26, "digital_pin": 26, "dry_raw": 52000, "wet_raw": 22000},
        {"adc_pin": 26, "power_pin": 26, "dry_raw": 52000, "wet_raw": 22000},
        {"adc_pin": 26, "digital_pin": 15, "power_pin": 15,
         "dry_raw": 52000, "wet_raw": 22000},
    ],
)
def test_within_device_pin_overlap_is_rejected(config):
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"
    assert "distinct" in str(excinfo.value)


def test_distinct_pins_pass():
    config = _valid_config()
    config["digital_pin"] = 15
    config["power_pin"] = 14
    assert validate_config(config) is None


# --- power_active_low ---------------------------------------------------------


@pytest.mark.parametrize("bad_value", [True, False])
def test_power_active_low_bools_are_accepted(bad_value):
    config = _valid_config()
    config["power_pin"] = 14
    config["power_active_low"] = bad_value
    assert validate_config(config) is None


@pytest.mark.parametrize("bad_value", [0, 1, "true", None, "yes"])
def test_power_active_low_non_bool_is_rejected(bad_value):
    config = _valid_config()
    config["power_pin"] = 14
    config["power_active_low"] = bad_value
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"


# --- settle_ms -----------------------------------------------------------------


@pytest.mark.parametrize("bad_value", [True, "200", 200.0, None, -1, 2001, 60000])
def test_invalid_settle_is_rejected(bad_value):
    config = _valid_config()
    config["settle_ms"] = bad_value
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"


@pytest.mark.parametrize("good_value", [0, 1, 200, 2000])
def test_settle_bounds_are_accepted(good_value):
    config = _valid_config()
    config["settle_ms"] = good_value
    assert validate_config(config) is None


def test_default_settle_is_the_guide_value():
    assert DEFAULT_SETTLE_MS == 200
    assert validate_config(_valid_config()) is None


# --- sample_count ----------------------------------------------------------------


@pytest.mark.parametrize("bad_value", [True, "9", 9.0, None, 0, 2, 8, 10, 102, 1000])
def test_invalid_sample_count_is_rejected(bad_value):
    # 2/8/10 are even (the median is the middle sample: an even count has
    # none); 0 is below the range; 102 is one past the ceiling.
    config = _valid_config()
    config["sample_count"] = bad_value
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"


@pytest.mark.parametrize("good_value", [1, 9, 99, 101])
def test_sample_count_bounds_are_accepted(good_value):
    config = _valid_config()
    config["sample_count"] = good_value
    assert validate_config(config) is None


def test_default_sample_count_is_the_guide_value():
    assert DEFAULT_SAMPLE_COUNT == 9
    assert validate_config(_valid_config()) is None


# --- sample_delay_ms ---------------------------------------------------------------


@pytest.mark.parametrize("bad_value", [True, "2", 2.0, None, -1, 51, 60000])
def test_invalid_sample_delay_is_rejected(bad_value):
    # 51 is one past the ceiling: the delay multiplies by sample_count
    # inside the read, so a large value would stretch one read past the
    # Core 1 liveness budget.
    config = _valid_config()
    config["sample_delay_ms"] = bad_value
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"


@pytest.mark.parametrize("good_value", [0, 1, 2, 50])
def test_sample_delay_bounds_are_accepted(good_value):
    config = _valid_config()
    config["sample_delay_ms"] = good_value
    assert validate_config(config) is None


def test_default_sample_delay_is_the_guide_value():
    assert DEFAULT_SAMPLE_DELAY_MS == 2
    assert validate_config(_valid_config()) is None


# --- dry_raw / wet_raw ------------------------------------------------------------


@pytest.mark.parametrize("key", ["dry_raw", "wet_raw"])
def test_missing_calibration_point_is_rejected(key):
    config = _valid_config()
    del config[key]
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "missing_key"
    assert key in str(excinfo.value)


@pytest.mark.parametrize("key", ["dry_raw", "wet_raw"])
@pytest.mark.parametrize(
    "bad_value", [True, "52000", 52000.0, None, -1, 65536, 100000]
)
def test_calibration_out_of_adc_range_is_rejected(key, bad_value):
    # 65536 is one past read_u16()'s range: the points are captured with the
    # same API the driver reads with, so a value the API cannot produce
    # cannot be a real measurement.
    config = _valid_config()
    config[key] = bad_value
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"


@pytest.mark.parametrize("key", ["dry_raw", "wet_raw"])
@pytest.mark.parametrize("good_value", [0, 1, 22000, 52000, 65534, 65535])
def test_calibration_adc_range_values_are_accepted(key, good_value):
    config = _valid_config()
    config[key] = good_value
    if key == "dry_raw":
        config["wet_raw"] = 22000 if good_value != 22000 else 22001
    else:
        config["dry_raw"] = 52000 if good_value != 52000 else 51999
    assert validate_config(config) is None


def test_equal_calibration_points_are_rejected():
    # A zero span cannot map readings to a scale (every reading would
    # clamp to an endpoint).
    config = _valid_config()
    config["dry_raw"] = 40000
    config["wet_raw"] = 40000
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"
    assert "differ" in str(excinfo.value)


def test_reversed_polarity_is_valid():
    # Clone boards can invert the analog polarity: wet above dry is a legal
    # calibration, the driver's interpolation is sign-agnostic.
    config = _valid_config()
    config["dry_raw"] = 22000
    config["wet_raw"] = 52000
    assert validate_config(config) is None


# --- Driver reuse ---------------------------------------------------------------------


class _UnreachableAdc:
    """The validator must reject before the ADC is ever touched."""

    def read_u16(self):
        raise AssertionError("initialize must not reach the ADC")


def test_initialize_reuses_the_pure_validator():
    """startup == write-time rules: a config the config boundary rejects is
    rejected by the driver's initialize() with the same stable error."""
    device = Yl69Fc28Device(_UnreachableAdc())
    with pytest.raises(DeviceValidationError) as excinfo:
        device.initialize({"adc_pin": 15, "dry_raw": 52000, "wet_raw": 22000})
    assert excinfo.value.code == "invalid_value"
