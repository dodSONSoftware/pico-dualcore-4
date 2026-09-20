# test_ds18b20_validation.py - Pure DS18B20 config validation
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Host-side tests for the pure ``ds18b20`` config validator: the complete
accept/reject contract (unknown keys, the required pin/ROM pair, the ROM
form and family code, the conversion-wait bounds, offsets shape and range)
and the driver's reuse of that same validator in ``initialize()`` (startup
== write-time rules). Nothing here touches hardware -- a valid config with
no physical sensor still passes; the import chain reaches no ``machine``
module, so it runs on CPython."""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from devices.ds18b20.validation import (  # noqa: E402
    ALLOWED_CONFIG_KEYS,
    DEFAULT_CONVERSION_MS,
    validate_config,
)
from devices.ds18b20.ds18b20_device import DS18B20Device  # noqa: E402
from devices.device import DeviceValidationError  # noqa: E402


ROM_A = "28ff1ca26117048d"


def _valid_config():
    """The minimal valid config: the two required keys only."""
    return {"pin": 16, "rom": ROM_A}


def _full_config():
    return {
        "pin": 16,
        "rom": ROM_A,
        "conversion_ms": 1000,
        "offsets": {"temperature_c": 0.5},
    }


def test_valid_minimal_config_passes():
    assert validate_config(_valid_config()) is None


def test_valid_full_config_passes():
    assert validate_config(_full_config()) is None


def test_every_allowed_key_is_accepted_on_its_own():
    """Each allowed key, set to a valid value alongside the required pair, is
    recognized (this pins the ALLOWED set against accidental drift)."""
    values = {
        "pin": 0,
        "rom": ROM_A,
        "conversion_ms": 750,
        "offsets": {"temperature_c": 0},
    }
    for key in sorted(ALLOWED_CONFIG_KEYS):
        config = _valid_config()
        config[key] = values[key]
        assert validate_config(config) is None, key


def test_config_must_be_an_object():
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(["pin", 16])
    assert excinfo.value.code == "invalid_value"


def test_unknown_key_is_rejected_qualified():
    config = _valid_config()
    config["i2c_bus"] = 0
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "unknown_config_fields"
    assert "i2c_bus" in str(excinfo.value)


# --- pin -------------------------------------------------------------------


def test_missing_pin_is_rejected():
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config({"rom": ROM_A})
    assert excinfo.value.code == "missing_key"
    assert "pin" in str(excinfo.value)


@pytest.mark.parametrize("bad_pin", [True, False, "16", 16.0, None, -1, 30, 100])
def test_invalid_pin_is_rejected(bad_pin):
    config = _valid_config()
    config["pin"] = bad_pin
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"


@pytest.mark.parametrize("good_pin", [0, 16, 29])
def test_board_gpio_range_is_accepted(good_pin):
    # 1-Wire is software-timed: any of the board's 30 GPIOs is a legal data
    # line (there is no mux routing constraint the way I2C's groups have).
    config = _valid_config()
    config["pin"] = good_pin
    assert validate_config(config) is None


# --- rom -------------------------------------------------------------------


def test_missing_rom_is_rejected():
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config({"pin": 16})
    assert excinfo.value.code == "missing_key"
    assert "rom" in str(excinfo.value)


@pytest.mark.parametrize(
    "bad_rom",
    [
        "ff1ca26117048d",     # 15 chars (a nibble short)
        "28ff1ca26117048d1",  # 17 chars
        "28ff1ca26117048g",   # non-hex character
        "28-ff1ca26117048d",  # a dash is not hex
        None,
    ],
)
def test_malformed_rom_is_rejected(bad_rom):
    config = _valid_config()
    config["rom"] = bad_rom
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"


def test_non_string_rom_is_rejected():
    config = _valid_config()
    config["rom"] = 0x28FF1CA26117048D
    with pytest.raises(DeviceValidationError):
        validate_config(config)


@pytest.mark.parametrize(
    "wrong_family",
    [
        "10ff1ca26117048d",  # DS18S20 family (different sub-degree decode)
        "29ff1ca26117048d",  # some other 1-Wire family
        "8b28ff1ca2611704",  # the family byte must be first, not elsewhere
    ],
)
def test_wrong_family_code_is_rejected(wrong_family):
    config = _valid_config()
    config["rom"] = wrong_family
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"
    assert "28" in str(excinfo.value)


@pytest.mark.parametrize(
    "good_rom",
    [
        ROM_A,
        "28FF1CA26117048D",  # uppercase is the same ROM
        "28Ff1Ca26117048d",  # mixed case is the same ROM
    ],
)
def test_family_code_28_roms_pass_in_any_case(good_rom):
    config = _valid_config()
    config["rom"] = good_rom
    assert validate_config(config) is None


# --- conversion_ms -----------------------------------------------------------


def test_default_conversion_wait_is_the_twelve_bit_window():
    assert DEFAULT_CONVERSION_MS == 750
    # The minimal config (no key) is the 12-bit default: it passes.
    assert validate_config(_valid_config()) is None


@pytest.mark.parametrize("bad_wait", [True, "750", 750.0, None, 94, 188, 375, 749, 1001, 60000])
def test_invalid_conversion_wait_is_rejected(bad_wait):
    # 94/188/375 are the documented 9/10/11-bit conversion times: they race
    # the scratchpad unless the driver has configured and verified a lower
    # resolution, which this driver never does. 1001 is one past the ceiling:
    # no resolution's conversion window needs it (the 12-bit maximum is
    # 750 ms; the ceiling is clone/timing margin, not protocol headroom),
    # and the wait is one uninterrupted sleep that does not refresh Core 1's
    # liveness stamp, so the ceiling stays far below Core 0's 30 s Core 1
    # staleness timeout. 60000 was the former ceiling: schema-legal, yet a
    # guaranteed heartbeat-stale reset after every read.
    config = _valid_config()
    config["conversion_ms"] = bad_wait
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"


@pytest.mark.parametrize("good_wait", [750, 760, 1000])
def test_conversion_wait_bounds_are_accepted(good_wait):
    config = _valid_config()
    config["conversion_ms"] = good_wait
    assert validate_config(config) is None


# --- offsets ------------------------------------------------------------------


def test_offsets_must_be_an_object():
    config = _valid_config()
    config["offsets"] = 0.5
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"


def test_offsets_unknown_subkey_is_rejected():
    config = _valid_config()
    config["offsets"] = {"temperature_f": 0.5}
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "unknown_config_fields"
    assert "temperature_f" in str(excinfo.value)


@pytest.mark.parametrize("bad_value", [True, "0.5", None, float("nan"), float("inf"),
                                       float("-inf"), 100.1, -100.1])
def test_offsets_invalid_value_is_rejected(bad_value):
    config = _valid_config()
    config["offsets"] = {"temperature_c": bad_value}
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"


@pytest.mark.parametrize("good_value", [0, 0.5, -2.0, 100, -100])
def test_offsets_bound_values_are_accepted(good_value):
    config = _valid_config()
    config["offsets"] = {"temperature_c": good_value}
    assert validate_config(config) is None


# --- Driver reuse -------------------------------------------------------------


class _UnreachableBus:
    """The validator must reject before the bus is ever touched."""

    def scan(self):
        raise AssertionError("initialize must not reach the bus")


def test_initialize_reuses_the_pure_validator():
    """startup == write-time rules: a config the config boundary rejects is
    rejected by the driver's initialize() with the same stable error."""
    device = DS18B20Device(_UnreachableBus())
    with pytest.raises(DeviceValidationError) as excinfo:
        device.initialize({"pin": 99, "rom": ROM_A})
    assert excinfo.value.code == "invalid_value"
