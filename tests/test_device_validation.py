# test_device_validation.py - Whole-device pure validation contract
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Host-side tests for the whole-device pure validation path: the device_type
registry, the pure per-device validator dispatch, the protocol-scale length
bounds, and the config-level unknown-field aggregation. None of these
construct or touch hardware -- the import chain reaches no ``machine``
module, so they run on plain CPython."""

import ast
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import device_factory
from config import ConfigError, validate_config
from device_factory import (
    MAX_DEVICE_ID_LENGTH,
    MAX_DEVICE_NAME_LENGTH,
    allowed_config_keys,
    validate_device_definition,
)
from devices.bme280.bme280_device import BME280Device
from devices.bme280.validation import (
    ALLOWED_CONFIG_KEYS as BME280_ALLOWED_CONFIG_KEYS,
)
from devices.device import DeviceValidationError


ROOT = pathlib.Path(__file__).resolve().parents[1]


def _base_config():
    return json.loads((ROOT / "tests" / "fixtures" / "config.json").read_text())


def _definition(device_type="bme280", config=None, **overrides):
    definition = {
        "id": "dev",
        "device_type": device_type,
        "config": (
            {"i2c_bus": 0, "sea_level_pressure_pa": 101325}
            if config is None else config
        ),
    }
    definition.update(overrides)
    return definition


# ---------------------------------------------------------------------------
# Registry: complete and host-importable
# ---------------------------------------------------------------------------


def test_registry_exposes_the_supported_types():
    assert allowed_config_keys("bme280") == BME280_ALLOWED_CONFIG_KEYS
    assert allowed_config_keys("ltr390") is not None
    assert allowed_config_keys("acme-9000") is None


def test_registry_resolves_the_validation_modules_lazily():
    """The registry entries name the real modules: resolving a supported type
    lands on the same module objects a direct import returns, an unsupported
    type imports nothing, and the allowed-keys attribute is the module's own
    key set (the validator and config.py's aggregation share one source)."""
    from devices.bme280 import validation as bme280_validation
    from devices.ltr390 import validation as ltr390_validation

    assert device_factory._validation_module("bme280") is bme280_validation
    assert device_factory._validation_module("ltr390") is ltr390_validation
    assert device_factory._validation_module("acme-9000") is None
    assert allowed_config_keys("bme280") is bme280_validation.ALLOWED_CONFIG_KEYS


def test_registry_imports_no_validation_package_at_module_top():
    """A module-top import of a validation package would make that type's
    validator resident from startup: only the registry strings may name the
    packages."""
    tree = ast.parse((ROOT / "device_factory.py").read_text())
    top_level_modules = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level_modules.extend(item.name for item in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            top_level_modules.append(node.module)
    assert not any(
        name.startswith("devices.bme280") or name.startswith("devices.ltr390")
        for name in top_level_modules
    )
    # The firmware may only module-top import modules the board's MicroPython
    # (README floor: 1.20) ships: importlib does not, and a module-top import
    # of it ImportError'd the Pico W at startup (0.4.101 hardware catch — the
    # host CPython suite cannot see it).
    assert "importlib" not in top_level_modules


# ---------------------------------------------------------------------------
# Pure per-device validator: shape, supported type, dispatch -- no hardware
# ---------------------------------------------------------------------------


def test_pure_validator_accepts_a_valid_definition():
    validate_device_definition(_definition())


def test_pure_validator_never_constructs_the_driver(monkeypatch):
    """The pure path must not instantiate a sensor to validate its config."""

    def explode(self, *args, **kwargs):
        raise AssertionError("the pure validator must not construct a driver")

    monkeypatch.setattr(BME280Device, "__init__", explode)
    validate_device_definition(_definition())


def test_pure_validator_rejects_an_unsupported_device_type():
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_device_definition(_definition(device_type="acme-9000", config={}))
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
        validate_device_definition(_definition(config={
            "i2c_bus": 0,
            "sea_level_pressure_pa": 101325,
            "i2c_address_candidates": [120],
        }))
    assert excinfo.value.code == "invalid_value"


# ---------------------------------------------------------------------------
# Length bounds: id, name stay protocol-scale
# ---------------------------------------------------------------------------


def test_pure_validator_binds_the_id_length():
    """The bound is inclusive and measured in UTF-8 bytes (the ceiling is a
    wire bound): 64 bytes accepts, 65 bytes rejects with the stable code —
    and a 64-character id of 4-byte code points is 256 bytes and rejects."""
    validate_device_definition(_definition(id="i" * MAX_DEVICE_ID_LENGTH))

    # The worst serialized form: 4-byte code points at exactly 64 UTF-8 bytes.
    validate_device_definition(_definition(id="\U0001F600" * (MAX_DEVICE_ID_LENGTH // 4)))

    with pytest.raises(DeviceValidationError) as excinfo:
        validate_device_definition(_definition(id="i" * (MAX_DEVICE_ID_LENGTH + 1)))
    assert excinfo.value.code == "invalid_value"
    assert str(excinfo.value) == "device id must be at most {} bytes".format(
        MAX_DEVICE_ID_LENGTH
    )

    with pytest.raises(DeviceValidationError, match="device id must be at most"):
        validate_device_definition(_definition(id="\U0001F600" * MAX_DEVICE_ID_LENGTH))


@pytest.mark.parametrize(
    "key,max_length",
    [
        ("name", MAX_DEVICE_NAME_LENGTH),
    ],
)
def test_pure_validator_binds_the_optional_field_lengths(key, max_length):
    """Optional fields keep their absent/null contract: present values are
    byte-bounded (the ceiling is a wire bound), missing or null ones stay
    valid."""
    validate_device_definition(_definition(**{key: "v" * max_length}))
    validate_device_definition(_definition(**{key: None}))
    definition = _definition(**{key: "present"})
    del definition[key]
    validate_device_definition(definition)

    with pytest.raises(DeviceValidationError) as excinfo:
        validate_device_definition(_definition(**{key: "v" * (max_length + 1)}))
    assert excinfo.value.code == "invalid_value"
    assert str(excinfo.value) == "device {} must be at most {} bytes".format(
        key, max_length
    )

    # 64 characters of 4-byte code points is 256 bytes: over the bound even
    # though it fits a character-based reading of the same number.
    with pytest.raises(DeviceValidationError, match="must be at most"):
        validate_device_definition(_definition(**{key: "\U0001F600" * max_length}))


def test_config_level_rejects_an_overlong_device_id():
    """The shared validate_config() path (startup and write-config) enforces
    the pure validator's bound, not just the file-loaded path."""
    config = _base_config()
    config["devices"][0]["id"] = "i" * (MAX_DEVICE_ID_LENGTH + 1)
    with pytest.raises(ConfigError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"


def test_worst_case_bounded_device_sections_stay_under_the_message_ceiling():
    """The bound arithmetic: with MAX_DEVICES entries of maximum-length fields,
    the per-device sections of a message (startup-log ready/failed lists,
    telemetry identity fields, the read-config device section) stay under half
    of MAX_OUTBOUND_MESSAGE_BYTES, leaving margin for the envelope, the fixed
    top-level configuration, and the non-config growth (driver failure
    reasons) that no config bound can pin."""
    from config import MAX_DEVICES
    from message_serializer import MAX_OUTBOUND_MESSAGE_BYTES

    entry = {
        "device": "bme280",
        "name": "n" * MAX_DEVICE_NAME_LENGTH,
    }
    worst_case_entry = len(json.dumps(entry).encode("utf-8"))
    device_sections = MAX_DEVICES * worst_case_entry
    assert device_sections < MAX_OUTBOUND_MESSAGE_BYTES // 2


# ---------------------------------------------------------------------------
# config.validate_config(): the shared startup/write-config pure path
# ---------------------------------------------------------------------------


def test_config_rejects_an_unsupported_device_type():
    candidate = _base_config()
    candidate["devices"][0]["device_type"] = "acme-9000"
    candidate["devices"][0]["config"] = {"some_key": 1}
    with pytest.raises(ConfigError) as excinfo:
        validate_config(candidate)
    assert excinfo.value.code == "unsupported_device_type"


def test_config_rejects_an_invalid_device_config_value():
    candidate = _base_config()
    candidate["devices"][0]["config"]["sea_level_pressure_pa"] = 1
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
    candidate["devices"][0]["config"]["i2c_address_candidates"] = [119]
    # No BME280 exists on the host; validation must still pass.
    assert validate_config(candidate) is candidate
