# device_factory.py - Device construction and pure validation
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

from devices.device import DeviceValidationError
from devices.system_information.system_information_device import SystemInformationDevice
from devices.system_information.validation import ALLOWED_CONFIG_KEYS, validate_config

# The supported device_type registry: device_type -> (pure config validator,
# allowed config keys). The single source of truth for which device types the
# firmware supports and how each validates its device-specific config. It is
# shared by create_device() (construction, which additionally needs the runtime
# system_information source) and the pure validation path in config.py. Adding
# a device type is adding one entry here plus its pure validator.
_DEVICE_REGISTRY = {
    "system-information": (validate_config, ALLOWED_CONFIG_KEYS),
}

# The complete set of keys a device definition may carry; anything beyond these
# is unknown and reported as a qualified path. Shared with config.py's
# aggregation so the definition shape has exactly one source.
DEVICE_DEFINITION_KEYS = frozenset(("id", "device_type", "config", "name", "sensor_type"))

# id, name, and sensor_type are spliced into per-message payloads (the
# telemetry message's identity fields, the startup log's ready/failed device
# lists, and the read-config response's whole configuration), so they carry a
# length bound that keeps a worst-case valid message under
# MAX_OUTBOUND_MESSAGE_BYTES (16 KiB): with MAX_DEVICES entries the device
# sections stay in low single-digit KB. 64 matches MAX_SOURCE_LENGTH, the
# other wire identity string. The bound belongs at this validation boundary —
# it must hold before Core 1 constructs per-device structures (including in
# the startup log's bounded fallback), not where the message is serialized.
MAX_DEVICE_ID_LENGTH = 64
MAX_DEVICE_NAME_LENGTH = 64
MAX_SENSOR_TYPE_LENGTH = 64


def supported_device_types():
    """The registered device types, sorted (stable for diagnostics)."""
    return tuple(sorted(_DEVICE_REGISTRY))


def is_supported_device_type(device_type):
    """True if device_type has a registered validator and constructor."""
    return device_type in _DEVICE_REGISTRY


def allowed_config_keys(device_type):
    """The device-specific config keys a supported device_type accepts, or None."""
    entry = _DEVICE_REGISTRY.get(device_type)
    return entry[1] if entry is not None else None


def validate_device_config(device_type, config):
    """Pure validation of one device's device-specific config (no hardware).

    Dispatches to the registered validator; raises DeviceValidationError with
    unsupported_device_type when the type is unknown, otherwise the validator's
    own code."""
    entry = _DEVICE_REGISTRY.get(device_type)
    if entry is None:
        raise DeviceValidationError(
            "Unsupported device type: {}".format(device_type),
            code="unsupported_device_type",
        )
    entry[0](config)


def validate_device_definition(device_definition):
    """Pure validation of one complete device definition (no hardware).

    Checks the generic device-definition shape, that the device_type is
    supported, and dispatches to the type's pure config validator. Raises
    DeviceValidationError with a stable code on the first violation. Never
    constructs or initializes a hardware resource: a valid definition with no
    physical backing passes, leaving physical absence to the boot-time
    initialization outcome."""
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
    if len(device_id) > MAX_DEVICE_ID_LENGTH:
        raise DeviceValidationError(
            "device id must be at most {} characters".format(MAX_DEVICE_ID_LENGTH),
            code="invalid_value",
        )

    device_type = device_definition["device_type"]
    if not isinstance(device_type, str) or not device_type:
        raise DeviceValidationError(
            "device_type must be a non-empty string", code="invalid_value"
        )

    if not isinstance(device_definition["config"], dict):
        raise DeviceValidationError("device config must be an object", code="invalid_value")

    # name and sensor_type are optional: absent (or None) stays valid, a
    # present value must be a string within the length bound.
    for key, max_length in (
        ("name", MAX_DEVICE_NAME_LENGTH),
        ("sensor_type", MAX_SENSOR_TYPE_LENGTH),
    ):
        value = device_definition.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            raise DeviceValidationError(
                "device {} must be a string".format(key), code="invalid_value"
            )
        if len(value) > max_length:
            raise DeviceValidationError(
                "device {} must be at most {} characters".format(key, max_length),
                code="invalid_value",
            )

    validate_device_config(device_type, device_definition["config"])


def create_device(device_definition, system_information=None):
    device_type = device_definition["device_type"]

    if device_type == "system-information":
        if system_information is None:
            raise ValueError("system-information requires system_information")
        return SystemInformationDevice(system_information)

    raise ValueError("Unsupported device type: {}".format(device_type))
