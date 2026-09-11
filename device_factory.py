# device_factory.py - Device construction and pure validation
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

from devices.device import DeviceValidationError

# The supported device_type registry: device_type -> (validation package,
# validator attribute, allowed-config-keys attribute). Both attributes are
# resolved by import at first use, never at module import: the validation
# modules are pure (host-importable, no machine), but their source still
# lands on the shared heap, and a type a board does not configure must not be
# resident from startup -- Core 0 loads this module for config validation,
# before Core 1 (which alone constructs and runs drivers) exists. The single
# source of truth for which types the firmware supports and how each
# validates its device-specific config, shared by create_device()
# (construction) and the pure validation path in config.py. Adding a device
# type is adding one entry here plus its pure validator.
_DEVICE_REGISTRY = {
    "bme280": ("devices.bme280.validation", "validate_config", "ALLOWED_CONFIG_KEYS"),
    "ltr390": ("devices.ltr390.validation", "validate_config", "ALLOWED_CONFIG_KEYS"),
}

# The complete set of keys a device definition may carry; anything beyond is
# unknown and reported as a qualified path. Shared with config.py's
# aggregation so the definition shape has one source.
DEVICE_DEFINITION_KEYS = frozenset(("id", "device_type", "config", "name"))

# id and name are spliced into per-message payloads (telemetry identity
# fields, startup-log device lists, the read-config response), so they carry
# a length bound that keeps a worst-case valid message under
# MAX_OUTBOUND_MESSAGE_BYTES (16 KiB). 64 matches MAX_SOURCE_LENGTH. The
# ceiling is a wire bound in UTF-8 bytes, so the bounds are measured in UTF-8
# bytes, not characters — 64 characters of 4-byte code points are 256 bytes
# (and 192 serialized bytes under the serializer's escaped output). The bound
# belongs at this validation boundary — it must hold before Core 1 constructs
# per-device structures (including the startup log's bounded fallback), not
# where the message is serialized.
MAX_DEVICE_ID_LENGTH = 64
MAX_DEVICE_NAME_LENGTH = 64


def _validation_module(device_type):
    """The validation module for a supported device_type, imported on first
    use (an import cache hit afterwards); None for an unsupported type, which
    imports nothing. The dynamic import goes through the __import__ builtin,
    not the importlib module: the board's MicroPython (the README pins
    1.20+) ships no importlib, and __import__ with a non-empty fromlist
    returns the named leaf module on both CPython and MicroPython."""
    entry = _DEVICE_REGISTRY.get(device_type)
    if entry is None:
        return None
    return __import__(entry[0], fromlist=["__name__"])


def allowed_config_keys(device_type):
    """The device-specific config keys a supported device_type accepts, or None."""
    entry = _DEVICE_REGISTRY.get(device_type)
    if entry is None:
        return None
    return getattr(_validation_module(device_type), entry[2])


def validate_device_config(device_type, config):
    """Pure validation of one device's device-specific config (no hardware);
    dispatches to the registered validator (unsupported_device_type when the
    type is unknown, otherwise the validator's own code)."""
    entry = _DEVICE_REGISTRY.get(device_type)
    if entry is None:
        raise DeviceValidationError(
            "Unsupported device type: {}".format(device_type),
            code="unsupported_device_type",
        )
    getattr(_validation_module(device_type), entry[1])(config)


def validate_device_definition(device_definition):
    """Pure validation of one complete device definition (no hardware):
    generic shape, supported device_type, then the type's pure config
    validator. Raises DeviceValidationError (stable code) on the first
    violation. Never constructs hardware: a valid definition with no physical
    backing passes, leaving absence to the boot-time initialization outcome."""
    if not isinstance(device_definition, dict):
        raise DeviceValidationError("device definition must be an object", code="invalid_value")

    for key in ("id", "device_type", "config"):
        if key not in device_definition:
            raise DeviceValidationError(
                "device definition missing required key: {}".format(key),
                code="missing_key",
            )

    unknown = sorted(set(device_definition) - DEVICE_DEFINITION_KEYS)
    if unknown:
        raise DeviceValidationError(
            "device definition contains unknown field(s): {}".format(", ".join(unknown)),
            code="unknown_config_fields",
        )

    device_id = device_definition["id"]
    if not isinstance(device_id, str) or not device_id:
        raise DeviceValidationError(
            "device id must be a non-empty string", code="invalid_value"
        )
    if len(device_id.encode("utf-8")) > MAX_DEVICE_ID_LENGTH:
        raise DeviceValidationError(
            "device id must be at most {} bytes".format(MAX_DEVICE_ID_LENGTH),
            code="invalid_value",
        )

    device_type = device_definition["device_type"]
    if not isinstance(device_type, str) or not device_type:
        raise DeviceValidationError(
            "device_type must be a non-empty string", code="invalid_value"
        )

    if not isinstance(device_definition["config"], dict):
        raise DeviceValidationError("device config must be an object", code="invalid_value")

    # name is optional: absent (or None) stays valid, a present value must be
    # a string within the length bound.
    name = device_definition.get("name")
    if name is not None:
        if not isinstance(name, str):
            raise DeviceValidationError(
                "device name must be a string", code="invalid_value"
            )
        if len(name.encode("utf-8")) > MAX_DEVICE_NAME_LENGTH:
            raise DeviceValidationError(
                "device name must be at most {} bytes".format(
                    MAX_DEVICE_NAME_LENGTH
                ),
                code="invalid_value",
            )

    validate_device_config(device_type, device_definition["config"])


def create_device(device_definition, i2c_bus_factory=None):
    device_type = device_definition["device_type"]

    if device_type == "bme280":
        # The driver never owns the bus: Core 1 supplies a factory that builds
        # (and dedupes) one machine.I2C per (bus, sda, scl, freq). sda/scl are
        # None when the config relies on the bus's default pins. The bus is
        # created here (peripheral + pins only); the sensor protocol runs in
        # initialize(), which the retry/reinit machinery wraps.
        if i2c_bus_factory is None:
            raise ValueError("bme280 requires an i2c_bus_factory")
        cfg = device_definition["config"]
        i2c = i2c_bus_factory(
            cfg["i2c_bus"],
            cfg.get("i2c_sda_pin"),
            cfg.get("i2c_scl_pin"),
            cfg.get("i2c_freq_hz",
                    _validation_module(device_type).DEFAULT_I2C_FREQ_HZ),
        )
        # Imported here, not at module top: Core 0 pulls in this module for the
        # validator during config validation, and a module-top driver import
        # would load the float-heavy driver resident on the shared heap from
        # startup -- before Core 1 (which alone constructs and runs it) exists.
        # Deferring it to this Core 1 construction point keeps that ~8 KB off
        # the pre-spawn heap. (The validation module is no longer module-top
        # either: the registry resolves it by import on first use.)
        from devices.bme280.bme280_device import BME280Device
        return BME280Device(i2c)

    if device_type == "ltr390":
        # Same bus ownership as bme280: the driver never creates the bus; Core 1
        # hands this point a factory that builds (and dedupes) one machine.I2C
        # per (bus, sda, scl, freq), so two I2C devices on the same bus share
        # one object. The sensor protocol runs in initialize(), which the
        # retry/reinit machinery wraps.
        if i2c_bus_factory is None:
            raise ValueError("ltr390 requires an i2c_bus_factory")
        cfg = device_definition["config"]
        i2c = i2c_bus_factory(
            cfg["i2c_bus"],
            cfg.get("i2c_sda_pin"),
            cfg.get("i2c_scl_pin"),
            cfg.get("i2c_freq_hz",
                    _validation_module(device_type).DEFAULT_I2C_FREQ_HZ),
        )
        # Same lazy-import rationale as the bme280 branch above: Core 0 pulls
        # in this module for the validator during config validation, and a
        # module-top driver import would load the float-heavy driver resident
        # on the shared heap from startup -- before Core 1 (which alone
        # constructs and runs it) exists. The validation module resolves
        # through the registry, like the bme280 branch's does.
        from devices.ltr390.ltr390_device import LTR390Device
        return LTR390Device(i2c)

    raise ValueError("Unsupported device type: {}".format(device_type))
