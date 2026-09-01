# config.py - Configuration loading and validation
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import json

from command_protocol import MAX_SOURCE_LENGTH
from device_factory import (
    DEVICE_DEFINITION_KEYS,
    allowed_config_keys,
    validate_device_definition,
)
from devices.device import DeviceValidationError
from version import CONFIG_SCHEMA_VERSION


class ConfigError(Exception):
    """Configuration load/validation failure with a stable machine-readable code.

    ``str(err)`` remains the full human-readable message (startup prints and
    command response both read it); ``code`` and ``unknown_fields`` let the
    command handler respond with a stable cause rather than parsing the
    message; ``details`` is a small flat key/value map for error-specific
    structured fields (for example, expected/received schema versions) that
    the command handler merges as-is into the error object. ``code`` becomes
    None only for errors raised outside this module."""

    def __init__(self, message, code=None, unknown_fields=None, details=None):
        super().__init__(message)
        self.code = code
        self.unknown_fields = unknown_fields
        self.details = details


class WifiConfigError(Exception):
    pass


_REQUIRED_KEYS = (
    "config_schema_version",
    "source",
    "read_loop_sec",
    "device_initialization_attempts",
    "device_initialization_retry_delay_ms",
    "device_read_failure_threshold",
    "devices",
    "mqtt_broker_ip_address",
    "mqtt_keepalive_sec",
    "mqtt_command_poll_ms",
    "mqtt_outbound_publish_delay_ms",
    "mqtt_broker_response_timeout_sec",
    "datetime_sync_interval_min",
    "network_snapshot_interval_sec",
    "network_probe_timeout_sec",
    "mqtt_topic_telemetry",
    "mqtt_topic_log",
    "mqtt_topic_command",
    "mqtt_topic_command_response",
    "mqtt_topic_info_request",
    "mqtt_topic_info_response",
    "mqtt_topic_network_probe",
    "mqtt_topic_health",
    "health_interval_sec",
    "wifi_reconnect_delays_sec",
    "mqtt_reconnect_delays_sec",
)

_ALLOWED_KEYS = frozenset(_REQUIRED_KEYS)


def _unknown_config_paths(config):
    """All unknown configuration paths, sorted: top-level keys, device-definition keys, and (for a supported type) device-config keys, each qualified by device id.

    ``devices[<id>].<key>`` (the index when the entry has no usable id) so a
    caller knows exactly where each unknown field sits; device-config keys
    are qualified ``devices[<id>].config.<key>``. The scan runs before any
    other check, so every unknown field is reported together in one sorted
    array rather than one at a time."""
    unknown = set(config.keys()) - _ALLOWED_KEYS
    devices = config.get("devices")
    if isinstance(devices, list):
        for index, device in enumerate(devices):
            if not isinstance(device, dict):
                continue
            device_id = device.get("id")
            qualifier = device_id if isinstance(device_id, str) and device_id else str(index)
            unknown.update(
                "devices[{}].{}".format(qualifier, key)
                for key in set(device.keys()) - DEVICE_DEFINITION_KEYS
            )
            # The device config dict is driver-owned; only a supported type has
            # a known allowed-key set to check its keys against (an unsupported
            # type is a fail-fast error handled by _validate_devices).
            device_config = device.get("config")
            if isinstance(device_config, dict):
                allowed = allowed_config_keys(device.get("device_type"))
                if allowed is not None:
                    unknown.update(
                        "devices[{}].config.{}".format(qualifier, key)
                        for key in set(device_config) - allowed
                    )
    return sorted(unknown)


def _read_json(path, error_type):
    try:
        with open(path, "r") as handle:
            value = json.load(handle)
    except MemoryError:
        raise
    except (OSError, ValueError) as err:
        raise error_type("Unable to read {}: {}".format(path, err))

    if not isinstance(value, dict):
        raise error_type("{} must contain a JSON object".format(path))
    return value


def _require_non_empty_string(config, key):
    value = config[key]
    if not isinstance(value, str) or not value:
        raise ConfigError(
            "{} must be a non-empty string".format(key), code="invalid_value"
        )


def _require_positive_integer(config, key):
    value = config[key]
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(
            "{} must be a positive integer".format(key), code="invalid_value"
        )


def _require_nonnegative_integer(config, key):
    value = config[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError(
            "{} must be a non-negative integer".format(key), code="invalid_value"
        )


def _validate_delays(config, key):
    delays = config[key]
    if not isinstance(delays, list) or not delays:
        raise ConfigError(
            "{} must be a non-empty list".format(key), code="invalid_value"
        )
    for index, delay in enumerate(delays):
        if isinstance(delay, bool) or not isinstance(delay, int) or delay < 0:
            raise ConfigError(
                "{}[{}] must be a non-negative integer".format(key, index),
                code="invalid_value",
            )


def _validate_devices(devices):
    if not isinstance(devices, list) or not devices:
        raise ConfigError("devices must be a non-empty list", code="invalid_value")

    seen_ids = set()
    for index, device in enumerate(devices):
        if not isinstance(device, dict):
            raise ConfigError(
                "devices[{}] must be an object".format(index), code="invalid_value"
            )

        # Duplicate ids span the whole list (a candidate-level concern), so
        # they are checked here before the per-device pure validation.
        device_id = device.get("id")
        if isinstance(device_id, str) and device_id:
            if device_id in seen_ids:
                raise ConfigError(
                    "Duplicate device id: {}".format(device_id), code="invalid_value"
                )
            seen_ids.add(device_id)

        # Generic shape, supported device_type, and the device-specific config
        # are all pure and live in the device registry (device_factory); a
        # DeviceValidationError maps onto a ConfigError with its stable code.
        try:
            validate_device_definition(device)
        except DeviceValidationError as err:
            raise ConfigError(str(err), code=err.code) from err


def validate_config(config):
    """Pure validation of a complete configuration dict (no filesystem, no hardware).

    Startup (``load_config``) and the write-config command both run
    candidates through this single path, so a config the firmware boots from
    and a config a command may commit are validated by exactly the same
    rules. Returns the validated dict; raises ``ConfigError`` (with a stable
    ``code``) on the first violation."""
    if not isinstance(config, dict):
        raise ConfigError("Config must be a JSON object", code="invalid_value")

    unknown = _unknown_config_paths(config)
    if unknown:
        raise ConfigError(
            "Configuration contains unknown fields",
            code="unknown_config_fields",
            unknown_fields=unknown,
        )

    for key in _REQUIRED_KEYS:
        if key not in config:
            raise ConfigError(
                "Missing required config key: {}".format(key), code="missing_key"
            )

    schema_version = config["config_schema_version"]
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise ConfigError(
            "config_schema_version must be an integer", code="invalid_value"
        )
    if schema_version != CONFIG_SCHEMA_VERSION:
        # The schema version is a configuration invariant, not a change
        # policy: the mismatch is answered with both sides named so the
        # sender can correct the candidate.
        raise ConfigError(
            "Unsupported config_schema_version",
            code="invalid_config_schema_version",
            details={
                "expected": CONFIG_SCHEMA_VERSION,
                "received": schema_version,
            },
        )

    for key in (
        "source",
        "mqtt_broker_ip_address",
        "mqtt_topic_telemetry",
        "mqtt_topic_log",
        "mqtt_topic_command",
        "mqtt_topic_command_response",
        "mqtt_topic_info_request",
        "mqtt_topic_info_response",
        "mqtt_topic_network_probe",
        "mqtt_topic_health",
    ):
        _require_non_empty_string(config, key)

    # source is the device identity on the wire: it is spliced into every
    # Core 0 outbound envelope, so it carries a protocol-scale length bound
    # rather than an open-ended string (a huge identity would make even a
    # tiny envelope fail the outbound wire ceiling).
    if len(config["source"]) > MAX_SOURCE_LENGTH:
        raise ConfigError(
            "source must be at most {} characters".format(MAX_SOURCE_LENGTH),
            code="invalid_value",
        )

    for key in (
        "read_loop_sec",
        "device_initialization_attempts",
        "device_read_failure_threshold",
        "mqtt_keepalive_sec",
        "mqtt_command_poll_ms",
        "mqtt_broker_response_timeout_sec",
        "datetime_sync_interval_min",
        "network_snapshot_interval_sec",
        "network_probe_timeout_sec",
        "health_interval_sec",
    ):
        _require_positive_integer(config, key)

    _require_nonnegative_integer(config, "device_initialization_retry_delay_ms")
    _require_nonnegative_integer(config, "mqtt_outbound_publish_delay_ms")
    _validate_delays(config, "wifi_reconnect_delays_sec")
    _validate_delays(config, "mqtt_reconnect_delays_sec")
    _validate_devices(config["devices"])

    return config


def load_config(path="config.json"):
    try:
        config = _read_json(path, ConfigError)
    except MemoryError:
        raise
    except ConfigError as err:
        raise ConfigError(str(err), code="unreadable_file") from err
    return validate_config(config)


def load_wifi_config(path="config-secrets.json"):
    config = _read_json(path, WifiConfigError)
    unknown = set(config.keys()) - {"wifi_ssid", "wifi_password"}
    if unknown:
        raise WifiConfigError(
            "Unknown Wi-Fi config key(s): {}".format(", ".join(sorted(unknown)))
        )

    ssid = config.get("wifi_ssid")
    password = config.get("wifi_password")
    if not isinstance(ssid, str) or not ssid:
        raise WifiConfigError("wifi_ssid must be a non-empty string")
    if not isinstance(password, str):
        raise WifiConfigError("wifi_password must be a string")

    return {"wifi_ssid": ssid, "wifi_password": password}


def split_config(config):
    """Create the immutable-by-convention per-core startup configuration.

    The bus carries no configuration: its heap-reserve admission bound is a board property owned by hardware.py and passed to InterCore directly."""
    core0 = {
        "source": config["source"],
        "mqtt_broker_ip_address": config["mqtt_broker_ip_address"],
        "mqtt_topic_telemetry": config["mqtt_topic_telemetry"],
        "mqtt_topic_log": config["mqtt_topic_log"],
        "mqtt_topic_command": config["mqtt_topic_command"],
        "mqtt_topic_command_response": config["mqtt_topic_command_response"],
        "mqtt_topic_info_request": config["mqtt_topic_info_request"],
        "mqtt_topic_info_response": config["mqtt_topic_info_response"],
        "wifi_reconnect_delays_sec": config["wifi_reconnect_delays_sec"],
        "mqtt_reconnect_delays_sec": config["mqtt_reconnect_delays_sec"],
        "mqtt_keepalive_sec": config["mqtt_keepalive_sec"],
        "mqtt_command_poll_ms": config["mqtt_command_poll_ms"],
        "mqtt_outbound_publish_delay_ms": config["mqtt_outbound_publish_delay_ms"],
        "mqtt_broker_response_timeout_sec": config["mqtt_broker_response_timeout_sec"],
        "datetime_sync_interval_min": config["datetime_sync_interval_min"],
        "network_snapshot_interval_sec": config["network_snapshot_interval_sec"],
        "network_probe_timeout_sec": config["network_probe_timeout_sec"],
        "mqtt_topic_network_probe": config["mqtt_topic_network_probe"],
        "mqtt_topic_health": config["mqtt_topic_health"],
    }

    core1 = {
        "read_loop_sec": config["read_loop_sec"],
        "device_initialization_attempts": config["device_initialization_attempts"],
        "device_initialization_retry_delay_ms": config["device_initialization_retry_delay_ms"],
        "device_read_failure_threshold": config["device_read_failure_threshold"],
        "health_interval_sec": config["health_interval_sec"],
        "devices": config["devices"],
    }

    return core0, core1
