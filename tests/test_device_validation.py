# test_device_validation.py - Whole-device pure validation contract
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Host-side tests for the whole-device pure validation path: the device_type
registry, the pure per-device validator, the pure system-information config
validator, the driver's reuse of it, and the config-level unknown-field
aggregation. None of these construct or touch hardware -- the import chain
reaches no ``machine`` module, so they run on plain CPython."""

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from config import ConfigError, validate_config
from device_factory import (
    allowed_config_keys,
    is_supported_device_type,
    supported_device_types,
    validate_device_definition,
)
from devices.device import DeviceValidationError
from devices.system_information.system_information_device import SystemInformationDevice
from devices.system_information.validation import (
    ALLOWED_CONFIG_KEYS,
    SYSTEM_INFORMATION_SECTIONS,
    validate_config as validate_system_information_config,
)


ROOT = pathlib.Path(__file__).resolve().parents[1]


def _base_config():
    return json.loads((ROOT / "config.json").read_text())


def _definition(device_type="system-information", config=None, **overrides):
    definition = {
        "id": "dev",
        "device_type": device_type,
        "config": {"include": ["memory"]} if config is None else config,
    }
    definition.update(overrides)
    return definition


# ---------------------------------------------------------------------------
# Registry: complete and host-importable
# ---------------------------------------------------------------------------


def test_registry_exposes_the_system_information_type():
    assert "system-information" in supported_device_types()
    assert is_supported_device_type("system-information") is True
    assert is_supported_device_type("bme280") is False
    assert allowed_config_keys("system-information") == ALLOWED_CONFIG_KEYS
    assert allowed_config_keys("bme280") is None


# ---------------------------------------------------------------------------
# Pure per-device validator: shape, supported type, dispatch -- no hardware
# ---------------------------------------------------------------------------


def test_pure_validator_accepts_a_valid_definition():
    validate_device_definition(_definition())


def test_pure_validator_never_constructs_the_driver(monkeypatch):
    """The pure path must not instantiate a sensor to validate its config."""

    def explode(self, *args, **kwargs):
        raise AssertionError("the pure validator must not construct a driver")

    monkeypatch.setattr(SystemInformationDevice, "__init__", explode)
    validate_device_definition(_definition())


def test_pure_validator_rejects_an_unsupported_device_type():
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_device_definition(_definition(device_type="bme280", config={}))
    assert excinfo.value.code == "unsupported_device_type"


@pytest.mark.parametrize("key", ["id", "device_type", "config"])
def test_pure_validator_requires_the_core_keys(key):
    definition = _definition()
    del definition[key]
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_device_definition(definition)
    assert excinfo.value.code == "missing_key"


def test_pure_validator_rejects_unknown_definition_fields():
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_device_definition(_definition(firmware="x"))
    assert excinfo.value.code == "unknown_config_fields"


def test_pure_validator_dispatches_to_the_device_validator():
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_device_definition(_definition(config={"include": ["nope"]}))
    assert excinfo.value.code == "invalid_value"


# ---------------------------------------------------------------------------
# Pure system-information config validator
# ---------------------------------------------------------------------------


def test_si_validator_accepts_any_valid_section_set():
    validate_system_information_config({"include": list(SYSTEM_INFORMATION_SECTIONS)})


def test_si_validator_rejects_unknown_config_keys():
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_system_information_config({"include": ["memory"], "bogus": 1})
    assert excinfo.value.code == "unknown_config_fields"


def test_si_validator_rejects_missing_include():
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_system_information_config({})
    assert excinfo.value.code == "missing_key"


@pytest.mark.parametrize(
    "bad",
    [
        {"include": []},
        {"include": "memory"},
        {"include": [1]},
        {"include": ["nope"]},
        {"include": ["memory", "memory"]},
    ],
)
def test_si_validator_rejects_bad_include(bad):
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_system_information_config(bad)
    assert excinfo.value.code == "invalid_value"


# ---------------------------------------------------------------------------
# The driver reuses the same pure validator (startup == write-time rules)
# ---------------------------------------------------------------------------


def test_initialize_reuses_the_pure_validator_and_applies_it():
    device = SystemInformationDevice(None)
    device.initialize({"include": ["memory"]})
    assert device._include == ("memory",)


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"include": []},
        {"include": ["nope"]},
        {"include": ["memory", "memory"]},
        {"include": ["memory"], "bogus": 1},
    ],
)
def test_initialize_rejects_the_same_invalid_configs(bad):
    device = SystemInformationDevice(None)
    with pytest.raises(DeviceValidationError):
        device.initialize(bad)


# ---------------------------------------------------------------------------
# config.validate_config(): the shared startup/write-config pure path
# ---------------------------------------------------------------------------


def test_config_rejects_an_unsupported_device_type():
    candidate = _base_config()
    candidate["devices"][0]["device_type"] = "bme280"
    candidate["devices"][0]["config"] = {"i2c_address": 0x76}
    with pytest.raises(ConfigError) as excinfo:
        validate_config(candidate)
    assert excinfo.value.code == "unsupported_device_type"


def test_config_rejects_an_invalid_include():
    candidate = _base_config()
    candidate["devices"][0]["config"]["include"] = ["nope"]
    with pytest.raises(ConfigError) as excinfo:
        validate_config(candidate)
    assert excinfo.value.code == "invalid_value"


def test_config_qualifies_unknown_device_config_keys():
    candidate = _base_config()
    device_id = candidate["devices"][0]["id"]
    candidate["devices"][0]["config"]["bogus"] = 1
    with pytest.raises(ConfigError) as excinfo:
        validate_config(candidate)
    assert excinfo.value.code == "unknown_config_fields"
    assert excinfo.value.unknown_fields == [
        "devices[{}].config.bogus".format(device_id)
    ]


def test_config_aggregates_definition_and_config_unknowns_together():
    candidate = _base_config()
    device_id = candidate["devices"][0]["id"]
    candidate["devices"][0]["extra"] = "x"
    candidate["devices"][0]["config"]["bogus"] = 1
    with pytest.raises(ConfigError) as excinfo:
        validate_config(candidate)
    assert excinfo.value.code == "unknown_config_fields"
    assert excinfo.value.unknown_fields == sorted(
        [
            "devices[{}].config.bogus".format(device_id),
            "devices[{}].extra".format(device_id),
        ]
    )


def test_config_accepts_a_valid_definition_with_absent_hardware():
    """A valid definition with no physical backing is a valid configuration:
    write-time validation checks the schema, and only boot-time initialization
    decides physical presence."""
    candidate = _base_config()
    candidate["devices"][0]["config"]["include"] = ["cpu"]
    # No SystemInformation source exists on the host; validation must still pass.
    assert validate_config(candidate) is candidate
