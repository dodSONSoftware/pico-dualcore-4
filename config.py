# config.py - Configuration loading and validation
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import json

from version import CONFIG_SCHEMA_VERSION


class ConfigError(Exception):
    pass


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
        raise ConfigError("{} must be a non-empty string".format(key))


def _require_positive_integer(config, key):
    value = config[key]
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError("{} must be a positive integer".format(key))


def _require_nonnegative_integer(config, key):
    value = config[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError("{} must be a non-negative integer".format(key))


def _validate_delays(config, key):
    delays = config[key]
    if not isinstance(delays, list) or not delays:
        raise ConfigError("{} must be a non-empty list".format(key))
    for index, delay in enumerate(delays):
        if isinstance(delay, bool) or not isinstance(delay, int) or delay < 0:
            raise ConfigError(
                "{}[{}] must be a non-negative integer".format(key, index)
            )


def _validate_devices(devices):
    if not isinstance(devices, list) or not devices:
        raise ConfigError("devices must be a non-empty list")

    seen_ids = set()
    for index, device in enumerate(devices):
        prefix = "devices[{}]".format(index)
        if not isinstance(device, dict):
            raise ConfigError("{} must be an object".format(prefix))

        for key in ("id", "device_type", "config"):
            if key not in device:
                raise ConfigError("{} missing required key: {}".format(prefix, key))

        device_id = device["id"]
        if not isinstance(device_id, str) or not device_id:
            raise ConfigError("{}.id must be a non-empty string".format(prefix))
        if device_id in seen_ids:
            raise ConfigError("Duplicate device id: {}".format(device_id))
        seen_ids.add(device_id)

        device_type = device["device_type"]
        if not isinstance(device_type, str) or not device_type:
            raise ConfigError("{}.device_type must be a non-empty string".format(prefix))
        if not isinstance(device["config"], dict):
            raise ConfigError("{}.config must be an object".format(prefix))

        for key in ("name", "sensor_type"):
            value = device.get(key)
            if value is not None and not isinstance(value, str):
                raise ConfigError("{}.{} must be a string".format(prefix, key))


def load_config(path="config.json"):
    config = _read_json(path, ConfigError)

    unknown = set(config.keys()) - _ALLOWED_KEYS
    if unknown:
        raise ConfigError(
            "Unknown config key(s): {}".format(", ".join(sorted(unknown)))
        )

    for key in _REQUIRED_KEYS:
        if key not in config:
            raise ConfigError("Missing required config key: {}".format(key))

    schema_version = config["config_schema_version"]
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise ConfigError("config_schema_version must be an integer")
    if schema_version != CONFIG_SCHEMA_VERSION:
        raise ConfigError(
            "config_schema_version must be {}".format(CONFIG_SCHEMA_VERSION)
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
