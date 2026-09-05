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

# MQTT 3.1.1 encodes Keep Alive as a 16-bit word: above 65535 s the
# CONNECT packet is unrepresentable. This key is reboot-required, so a
# rejected write would brick the MQTT channel used to fix it.
MAX_MQTT_KEEPALIVE_SEC = 65535

# mqtt_client.subscribe() encodes the SUBSCRIBE Remaining Length in one byte
# (valid through 127) and the body is topic + 5, so above 122 topic bytes the
# length byte gains the continuation bit and the subscription silently never
# completes. Both subscribed topics are the repair channel for a bad
# configuration, so the bound sits at the config boundary. (Publish uses full
# variable-length encoding; the same bound is applied to every topic for one
# simple policy: 1..N ASCII bytes, no NUL.)
MAX_MQTT_TOPIC_BYTES = 122

# The broker address feeds socket.connect(), which also resolves hostnames,
# so the bound is DNS's 253-byte hostname maximum rather than IPv6's 45
# characters. It is spliced into the read-config response and the connect
# logs, and it had no bound at all — a single long value alone pushed a
# "valid" configuration past the outbound wire ceiling.
MAX_MQTT_BROKER_ADDRESS_BYTES = 253

# The RP2 builds give time.ticks_* 30-bit values: ticks_diff only expresses
# deltas below half the period (2^29 - 1 ms, ~6.21 days) and ticks_add raises
# OverflowError at it. Any value that becomes a ticks_diff threshold or
# ticks_add delta must stay under this ceiling, or the threshold can never be
# reached (or the deadline/re-anchor raises).
MAX_TICKS_SAFE_INTERVAL_MS = (1 << 29) - 1

# Retries ride out a transient driver.initialize() failure; a device that
# fails all of them is broken, so a handful is enough (shipped: 3). The
# bound also keeps a misconfigured huge value from stalling startup for
# attempts x retry delay and growing retained init diagnostics on the
# startup-failure path where heap must stay flat.
MAX_DEVICE_INITIALIZATION_ATTEMPTS = 10

# Reconnect delays are bounded for operational liveness, not the ticks
# ceiling (they only drive sliced sleeps): a delay long enough to outlast any
# human diagnosis, or a sequence long enough to loop for days, makes recovery
# indistinguishable from a hang. The shipped sequences (3/5/10/20/40 s) are
# nowhere near either bound; above them is misconfiguration, not policy.
MAX_RECONNECT_DELAY_SEC = 600
MAX_RECONNECT_ATTEMPTS = 32

# Core 1 builds a per-device status structure before anything can be rejected
# at the serialized-size ceiling (the bounded startup-log fallback and the
# read-config response both carry it). The string fields are byte-bounded
# (device_factory's MAX_DEVICE_*_LENGTH, MAX_SOURCE_LENGTH, and
# MAX_MQTT_BROKER_ADDRESS_BYTES — UTF-8 bytes, since the ceiling is a wire
# bound), so with the device count at this maximum a worst-case valid
# configuration stays under MAX_OUTBOUND_MESSAGE_BYTES (16 KiB) serialized.
# The serialized-size invariant test in tests/test_config.py pins that for
# every currently supported device type.
MAX_DEVICES = 16


class ConfigError(Exception):
    """Configuration load/validation failure with a stable machine-readable
    code. ``str(err)`` stays the full human-readable message; ``code`` and
    ``unknown_fields`` let the command handler answer with a stable cause
    rather than parsing the message; ``details`` is a small flat key/value
    map the command handler merges as-is (e.g. expected/received schema
    versions). ``code`` is None only for errors raised outside this module."""

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

# The eight protocol channels. Topic identity is the inbound routing
# invariant (dispatch matches delivered topics by exact equality), so the
# set also drives the pairwise-distinctness rule below.
_MQTT_TOPIC_KEYS = (
    "mqtt_topic_telemetry",
    "mqtt_topic_log",
    "mqtt_topic_command",
    "mqtt_topic_command_response",
    "mqtt_topic_info_request",
    "mqtt_topic_info_response",
    "mqtt_topic_network_probe",
    "mqtt_topic_health",
)


def _unknown_config_paths(config):
    """All unknown configuration paths, sorted: top-level keys,
    device-definition keys, and (for a supported type) device-config keys,
    qualified ``devices[<id>].<key>`` / ``devices[<id>].config.<key>`` (the
    index when the entry has no usable id). Runs before any other check, so
    every unknown field is reported together in one sorted array."""
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
            # a known allowed-key set to check against.
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


def _require_mqtt_topic(config, key):
    """A configured topic must be 1..MAX_MQTT_TOPIC_BYTES ASCII bytes with no
    NUL (see the bound's comment); ASCII-only keeps UTF-8 byte length equal to
    character count, so the single-byte Remaining Length arithmetic in
    mqtt_client.subscribe() stays exact. Wildcards (+/#) are rejected: they
    are invalid in a PUBLISH Topic Name, and inbound dispatch matches
    delivered topics by exact equality against the configured name."""
    value = config[key]
    if not isinstance(value, str) or not value:
        raise ConfigError(
            "{} must be a non-empty string".format(key), code="invalid_value"
        )
    if len(value) > MAX_MQTT_TOPIC_BYTES or any(
        ch < "\u0001" or ch > "\u007f" or ch in "+#" for ch in value
    ):
        raise ConfigError(
            "{} must be 1-{} ASCII bytes with no NUL or wildcard (+/#)".format(
                key, MAX_MQTT_TOPIC_BYTES
            ),
            code="invalid_value",
        )


def _require_distinct_mqtt_topics(config):
    # A shared topic name is not "one channel, disambiguated by content":
    # inbound dispatch matches by exact equality and the first matching
    # branch wins, so the other channel's traffic is silently dropped —
    # equal command/info_response names make the entire command path
    # unreachable on a device that still reports healthy, and a name shared
    # with a locally published topic re-delivers every own publication
    # inbound. Each channel keeps its own name.
    seen = {}
    for key in _MQTT_TOPIC_KEYS:
        value = config[key]
        owner = seen.get(value)
        if owner is not None:
            raise ConfigError(
                "Duplicate MQTT topic '{}' used by {} and {}".format(
                    value, owner, key
                ),
                code="invalid_value",
            )
        seen[value] = key


def _require_positive_integer(config, key):
    value = config[key]
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(
            "{} must be a positive integer".format(key), code="invalid_value"
        )


def _require_ticks_safe_interval(config, key, ms_per_unit):
    """value * ms_per_unit must fit a MicroPython ticks delta (see MAX_TICKS_SAFE_INTERVAL_MS)."""
    value = config[key]
    max_value = MAX_TICKS_SAFE_INTERVAL_MS // ms_per_unit
    if value * ms_per_unit > MAX_TICKS_SAFE_INTERVAL_MS:
        raise ConfigError(
            "{} must be at most {} (MicroPython ticks intervals are limited "
            "to {} ms)".format(key, max_value, MAX_TICKS_SAFE_INTERVAL_MS),
            code="invalid_value",
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
    if len(delays) > MAX_RECONNECT_ATTEMPTS:
        raise ConfigError(
            "{} must contain at most {} entries".format(
                key, MAX_RECONNECT_ATTEMPTS
            ),
            code="invalid_value",
        )
    for index, delay in enumerate(delays):
        if isinstance(delay, bool) or not isinstance(delay, int) or delay < 0:
            raise ConfigError(
                "{}[{}] must be a non-negative integer".format(key, index),
                code="invalid_value",
            )
        if delay > MAX_RECONNECT_DELAY_SEC:
            raise ConfigError(
                "{}[{}] must be at most {} seconds".format(
                    key, index, MAX_RECONNECT_DELAY_SEC
                ),
                code="invalid_value",
            )


def _validate_devices(devices):
    if not isinstance(devices, list) or not devices:
        raise ConfigError("devices must be a non-empty list", code="invalid_value")
    if len(devices) > MAX_DEVICES:
        raise ConfigError(
            "devices must contain at most {} entries".format(MAX_DEVICES),
            code="invalid_value",
        )

    seen_ids = set()
    for index, device in enumerate(devices):
        if not isinstance(device, dict):
            raise ConfigError(
                "devices[{}] must be an object".format(index), code="invalid_value"
            )

        # Duplicate ids span the whole list, so they are checked here before
        # the per-device pure validation.
        device_id = device.get("id")
        if isinstance(device_id, str) and device_id:
            if device_id in seen_ids:
                raise ConfigError(
                    "Duplicate device id: {}".format(device_id), code="invalid_value"
                )
            seen_ids.add(device_id)

        # Generic shape, supported device_type, and device-specific config are
        # pure and live in device_factory's registry; a DeviceValidationError
        # maps onto a ConfigError with its stable code.
        try:
            validate_device_definition(device)
        except DeviceValidationError as err:
            raise ConfigError(str(err), code=err.code) from err


def validate_config(config):
    """Pure validation of a complete configuration dict (no filesystem, no
    hardware). Startup (``load_config``) and the write-config command both
    run candidates through this single path, so a booted config and a
    committed one are validated by exactly the same rules. Returns the
    validated dict; raises ``ConfigError`` (stable ``code``) on violation."""
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
        # A configuration invariant, not a change policy: the mismatch is
        # answered with both sides named so the sender can correct it.
        raise ConfigError(
            "Unsupported config_schema_version",
            code="invalid_config_schema_version",
            details={
                "expected": CONFIG_SCHEMA_VERSION,
                "received": schema_version,
            },
        )

    for key in ("source", "mqtt_broker_ip_address"):
        _require_non_empty_string(config, key)

    # Topics carry the MQTT single-byte Remaining Length bound (subscribe)
    # on top of the non-empty-string contract, and the eight channel names
    # must stay pairwise distinct (topic identity is the routing invariant).
    for key in _MQTT_TOPIC_KEYS:
        _require_mqtt_topic(config, key)
    _require_distinct_mqtt_topics(config)

    # source is the wire identity, spliced into every Core 0 envelope, so it
    # carries a protocol-scale bound: a huge identity would make even a tiny
    # envelope fail the outbound wire ceiling. Measured in UTF-8 bytes — the
    # ceiling is a wire bound, and 64 characters of 4-byte code points are
    # 256 bytes.
    if len(config["source"].encode("utf-8")) > MAX_SOURCE_LENGTH:
        raise ConfigError(
            "source must be at most {} bytes".format(MAX_SOURCE_LENGTH),
            code="invalid_value",
        )

    # The broker address had no length bound at all: one ~15 KiB value fits
    # a single inbound write-config packet, validates, and then makes the
    # read-config response unsendable forever. DNS's hostname maximum keeps
    # hostnames legal while bounding the wire contribution.
    if len(config["mqtt_broker_ip_address"].encode("utf-8")) > MAX_MQTT_BROKER_ADDRESS_BYTES:
        raise ConfigError(
            "mqtt_broker_ip_address must be at most {} bytes".format(
                MAX_MQTT_BROKER_ADDRESS_BYTES
            ),
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

    # Keep Alive is a 16-bit word on the wire; above the maximum the CONNECT
    # packet is unrepresentable. config.py is authoritative; mqtt_client.py
    # keeps its check only as defensive transport validation.
    if config["mqtt_keepalive_sec"] > MAX_MQTT_KEEPALIVE_SEC:
        raise ConfigError(
            "mqtt_keepalive_sec must be at most {}".format(MAX_MQTT_KEEPALIVE_SEC),
            code="invalid_value",
        )

    # See MAX_DEVICE_INITIALIZATION_ATTEMPTS: retries ride out transient
    # initialize() failures; a device that fails them all is broken.
    if config["device_initialization_attempts"] > MAX_DEVICE_INITIALIZATION_ATTEMPTS:
        raise ConfigError(
            "device_initialization_attempts must be at most {}".format(
                MAX_DEVICE_INITIALIZATION_ATTEMPTS
            ),
            code="invalid_value",
        )

    _require_nonnegative_integer(config, "device_initialization_retry_delay_ms")
    _require_nonnegative_integer(config, "mqtt_outbound_publish_delay_ms")

    # Values that become ticks_diff thresholds or ticks_add deltas are bounded
    # by the ticks ceiling; values that only drive sliced sleeps (reconnect
    # delays, probe timeout, sliced retry delay) are not — reconnect delays
    # get their own operational-liveness bounds instead. Runs after the type
    # checks so a wrong type still reports invalid_value, not a type error.
    _require_ticks_safe_interval(config, "read_loop_sec", 1000)
    _require_ticks_safe_interval(config, "health_interval_sec", 1000)
    _require_ticks_safe_interval(config, "network_snapshot_interval_sec", 1000)
    _require_ticks_safe_interval(config, "mqtt_broker_response_timeout_sec", 1000)
    _require_ticks_safe_interval(config, "datetime_sync_interval_min", 60 * 1000)
    _require_ticks_safe_interval(config, "mqtt_command_poll_ms", 1)
    _require_ticks_safe_interval(config, "mqtt_outbound_publish_delay_ms", 1)

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
    """Create the immutable-by-convention per-core startup configuration. The
    bus carries no configuration: its heap-reserve bound is a board property
    owned by hardware.py, passed to InterCore directly."""
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
