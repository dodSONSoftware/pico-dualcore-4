# device_factory.py - Device construction and pure validation
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

from devices.device import DeviceValidationError
from devices.bme280 import validation as bme280_validation
from devices.ltr390 import validation as ltr390_validation

# The supported device_type registry: device_type -> (pure config validator,
# allowed config keys). The single source of truth for which types the
# firmware supports and how each validates its device-specific config, shared
# by create_device() (construction) and the pure validation path in config.py.
# Adding a device type is adding one entry here plus its pure validator.
_DEVICE_REGISTRY = {
    "bme280": (
        bme280_validation.validate_config,
        bme280_validation.ALLOWED_CONFIG_KEYS,
    ),
    "ltr390": (
        ltr390_validation.validate_config,
        ltr390_validation.ALLOWED_CONFIG_KEYS,
    ),
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


def allowed_config_keys(device_type):
    """The device-specific config keys a supported device_type accepts, or None."""
    entry = _DEVICE_REGISTRY.get(device_type)
    return entry[1] if entry is not None else None


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
    entry[0](config)


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
            cfg.get("i2c_freq_hz", bme280_validation.DEFAULT_I2C_FREQ_HZ),
        )
        # Imported here, not at module top: Core 0 pulls in this module for the
        # validator during config validation, and a module-top driver import
        # would load the float-heavy driver resident on the shared heap from
        # startup -- before Core 1 (which alone constructs and runs it) exists.
        # Deferring it to this Core 1 construction point keeps that ~8 KB off
        # the pre-spawn heap. The validator (line 6) stays module-level: config
        # validation needs it on Core 0.
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
            cfg.get("i2c_freq_hz", ltr390_validation.DEFAULT_I2C_FREQ_HZ),
        )
        # Same lazy-import rationale as the bme280 branch above: Core 0 pulls
        # in this module for the validator during config validation, and a
        # module-top driver import would load the float-heavy driver resident
        # on the shared heap from startup -- before Core 1 (which alone
        # constructs and runs it) exists. The validator (line 7) stays
        # module-level: config validation needs it on Core 0.
        from devices.ltr390.ltr390_device import LTR390Device
        return LTR390Device(i2c)

    raise ValueError("Unsupported device type: {}".format(device_type))
