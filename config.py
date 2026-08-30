# config.py - Configuration loading, validation, change policies, persistence
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT
#
# Single source of truth for the configuration surface:
#   - CONFIG_CHANGE_POLICIES: the one authoritative mapping of every valid
#     top-level key to exactly one change policy (DYNAMIC / RECONFIGURE /
#     RESTART_REQUIRED / READ_ONLY); the required/allowed key set derives
#     from it (there is no second list);
#   - fail-fast validation shared by boot and runtime writes (validate_config);
#   - staged, power-loss-recoverable persistence (config.json.tmp staged
#     before activation; rename-based commit; config.json.bak; boot
#     recovery; a stale .tmp is never auto-promoted);
#   - ConfigState: the small lock-protected committed-configuration view
#     both cores may read (generation, checksum, reboot_required,
#     pending_restart_keys).
#
# MemoryError always propagates from these paths (fail-fast).

import _thread
import binascii
import hashlib
import json
import os

from observability import (
    REASON_CONFIG_INVALID_KEY,
    REASON_CONFIG_INVALID_VALUE,
    REASON_CONFIG_READ_ONLY_KEY,
)
from version import CONFIG_SCHEMA_VERSION


class ConfigError(Exception):
    pass


class WifiConfigError(Exception):
    pass


class ConfigPatchError(Exception):
    """A rejected write_config patch; ``code`` is the stable error code."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Change policy registry (single authoritative mapping)
# ---------------------------------------------------------------------------

CHANGE_DYNAMIC = "DYNAMIC"
CHANGE_RECONFIGURE = "RECONFIGURE"
CHANGE_RESTART_REQUIRED = "RESTART_REQUIRED"
CHANGE_READ_ONLY = "READ_ONLY"

# Every valid top-level key mapped to exactly one policy. DYNAMIC is active
# without a subsystem restart; RECONFIGURE reconfigures its owner and must
# prove operational; RESTART_REQUIRED is persisted only (never auto-reboots);
# READ_ONLY is never writable at runtime.
CONFIG_CHANGE_POLICIES = {
    "config_schema_version": CHANGE_READ_ONLY,
    "config_generation": CHANGE_READ_ONLY,
    "max_intercore_event_entries": CHANGE_RESTART_REQUIRED,
    "source": CHANGE_DYNAMIC,
    "read_loop_sec": CHANGE_DYNAMIC,
    "device_initialization_attempts": CHANGE_DYNAMIC,
    "device_initialization_retry_delay_ms": CHANGE_DYNAMIC,
    "device_read_failure_threshold": CHANGE_DYNAMIC,
    "health_interval_sec": CHANGE_DYNAMIC,
    "mqtt_command_poll_ms": CHANGE_DYNAMIC,
    "mqtt_post_outage_drain_rate_per_sec": CHANGE_DYNAMIC,
    "datetime_sync_interval_min": CHANGE_DYNAMIC,
    "network_snapshot_interval_sec": CHANGE_DYNAMIC,
    "network_probe_timeout_sec": CHANGE_DYNAMIC,
    "mqtt_topic_telemetry": CHANGE_DYNAMIC,
    "mqtt_topic_log": CHANGE_DYNAMIC,
    "mqtt_topic_command_response": CHANGE_DYNAMIC,
    "mqtt_topic_info_request": CHANGE_DYNAMIC,
    "mqtt_topic_network_probe": CHANGE_DYNAMIC,
    "mqtt_topic_health": CHANGE_DYNAMIC,
    "wifi_reconnect_delays_sec": CHANGE_DYNAMIC,
    "mqtt_reconnect_delays_sec": CHANGE_DYNAMIC,
    "network_diagnostics_interval_sec": CHANGE_DYNAMIC,
    "network_diagnostics_broker_latency_enabled": CHANGE_DYNAMIC,
    "devices": CHANGE_RECONFIGURE,
    "mqtt_broker_ip_address": CHANGE_RECONFIGURE,
    "mqtt_keepalive_sec": CHANGE_RECONFIGURE,
    "mqtt_broker_response_timeout_sec": CHANGE_RECONFIGURE,
    "mqtt_topic_command": CHANGE_RECONFIGURE,
    "mqtt_topic_info_response": CHANGE_RECONFIGURE,
}

_ALLOWED_KEYS = frozenset(CONFIG_CHANGE_POLICIES)
_REQUIRED_KEYS = tuple(CONFIG_CHANGE_POLICIES)


def _keys_with_policy(policy):
    return tuple(key for key, value in CONFIG_CHANGE_POLICIES.items() if value == policy)


_READ_ONLY_KEYS = _keys_with_policy(CHANGE_READ_ONLY)
_RESTART_REQUIRED_KEYS = _keys_with_policy(CHANGE_RESTART_REQUIRED)
_DYNAMIC_KEYS = _keys_with_policy(CHANGE_DYNAMIC)
_RECONFIGURE_KEYS = _keys_with_policy(CHANGE_RECONFIGURE)

# Owner partition of the writable policies.
CORE1_DYNAMIC_KEYS = (
    "read_loop_sec",
    "health_interval_sec",
    "device_initialization_attempts",
    "device_initialization_retry_delay_ms",
    "device_read_failure_threshold",
)
CORE1_RECONFIGURE_KEYS = ("devices",)
CORE0_MQTT_RECONFIGURE_KEYS = (
    "mqtt_broker_ip_address",
    "mqtt_keepalive_sec",
    "mqtt_broker_response_timeout_sec",
    "mqtt_topic_command",
    "mqtt_topic_info_response",
)
CORE0_DYNAMIC_KEYS = tuple(
    key for key in _DYNAMIC_KEYS if key not in CORE1_DYNAMIC_KEYS
)

# Well-known file names (the functions take explicit paths so the host tests
# can stage against a temporary directory).
CONFIG_FILE = "config.json"
CONFIG_STAGED_SUFFIX = ".tmp"
CONFIG_BACKUP_SUFFIX = ".bak"


# ---------------------------------------------------------------------------
# Validation (shared by boot and runtime writes)
# ---------------------------------------------------------------------------

_NON_EMPTY_STRING_KEYS = (
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
)

_POSITIVE_INTEGER_KEYS = (
    "read_loop_sec",
    "device_initialization_attempts",
    "device_read_failure_threshold",
    "mqtt_keepalive_sec",
    "mqtt_command_poll_ms",
    "mqtt_broker_response_timeout_sec",
    "datetime_sync_interval_min",
    "network_snapshot_interval_sec",
    "network_probe_timeout_sec",
    "max_intercore_event_entries",
    "health_interval_sec",
)

_NONNEGATIVE_INTEGER_KEYS = (
    "device_initialization_retry_delay_ms",
    "mqtt_post_outage_drain_rate_per_sec",
)


def _validate_diagnostics_interval_value(key, value):
    # 0 disables active diagnostics (passive RSSI sampling still runs); any
    # nonzero value must be a conservative interval in whole seconds. bool is
    # rejected explicitly because True/False are ints in Python.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(
            "{} must be 0 or an integer number of seconds".format(key)
        )
    if value != 0 and not 60 <= value <= 86400:
        raise ConfigError(
            "{} must be 0 (disabled) or between 60 and 86400 seconds".format(key)
        )


def _validate_delays_value(key, value):
    if not isinstance(value, list) or not value:
        raise ConfigError("{} must be a non-empty list".format(key))
    for index, delay in enumerate(value):
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


def validate_config_value(key, value):
    """Validate one top-level key's value.

    Shared by whole-config validation and patch validation so both use the
    exact same per-key rules. Raises ConfigError with a key-named message.
    """
    if key in _NON_EMPTY_STRING_KEYS:
        if not isinstance(value, str) or not value:
            raise ConfigError("{} must be a non-empty string".format(key))
    elif key in _POSITIVE_INTEGER_KEYS:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ConfigError("{} must be a positive integer".format(key))
    elif key in _NONNEGATIVE_INTEGER_KEYS:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ConfigError("{} must be a non-negative integer".format(key))
    elif key == "config_schema_version":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError("config_schema_version must be an integer")
        if value != CONFIG_SCHEMA_VERSION:
            raise ConfigError(
                "config_schema_version must be {}".format(CONFIG_SCHEMA_VERSION)
            )
    elif key == "config_generation":
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ConfigError(
                "config_generation must be a non-negative integer"
            )
    elif key == "network_diagnostics_interval_sec":
        _validate_diagnostics_interval_value(key, value)
    elif key == "network_diagnostics_broker_latency_enabled":
        if not isinstance(value, bool):
            raise ConfigError("{} must be true or false".format(key))
    elif key in ("wifi_reconnect_delays_sec", "mqtt_reconnect_delays_sec"):
        _validate_delays_value(key, value)
    elif key == "devices":
        _validate_devices(value)
    else:
        raise ConfigError("Unknown config key: {}".format(key))


def validate_config(config):
    """Fail-fast validation of a complete top-level configuration.

    This is the single shared validator: boot (load_config / boot recovery)
    and runtime writes (the merged write_config candidate) both run it, so
    a configuration that is valid at boot is valid at runtime and vice
    versa. Returns the config on success; raises ConfigError otherwise.
    """
    if not isinstance(config, dict):
        raise ConfigError("Configuration must contain a JSON object")

    unknown = set(config.keys()) - _ALLOWED_KEYS
    if unknown:
        raise ConfigError(
            "Unknown config key(s): {}".format(", ".join(sorted(unknown)))
        )

    for key in _REQUIRED_KEYS:
        if key not in config:
            raise ConfigError("Missing required config key: {}".format(key))

    for key in config:
        validate_config_value(key, config[key])

    return config


def validate_config_patch(patch):
    """Validate a partial top-level write_config patch.

    Returns the sorted list of patched keys on success. Raises
    ConfigPatchError with a stable .code on rejection:

    - ``invalid_config_key``   an unknown key
    - ``read_only_config_key`` config_schema_version / config_generation
    - ``invalid_config_value`` a value fails its key's validator

    Cross-field validity of the merged candidate is the caller's check
    (validate_config on the candidate -> ``invalid_config_combination``).
    An empty or non-object patch is a command-payload error handled by the
    command layer before this is called.
    """
    if not isinstance(patch, dict) or not patch:
        raise ConfigPatchError(REASON_CONFIG_INVALID_VALUE, "patch must be non-empty")

    keys = []
    for key in patch:
        if key not in _ALLOWED_KEYS:
            raise ConfigPatchError(
                REASON_CONFIG_INVALID_KEY,
                "Unknown config key: {}".format(key),
            )
        if CONFIG_CHANGE_POLICIES[key] == CHANGE_READ_ONLY:
            raise ConfigPatchError(
                REASON_CONFIG_READ_ONLY_KEY,
                "{} is read-only".format(key),
            )
        try:
            validate_config_value(key, patch[key])
        except MemoryError:
            raise
        except ConfigError as err:
            raise ConfigPatchError(REASON_CONFIG_INVALID_VALUE, str(err))
        keys.append(key)
    return sorted(keys)


# ---------------------------------------------------------------------------
# Loading (boot)
# ---------------------------------------------------------------------------

def _read_config_file(path):
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except MemoryError:
        raise
    except (OSError, ValueError) as err:
        raise ConfigError("Unable to read {}: {}".format(path, err))


def parse_config_bytes(data_bytes):
    try:
        value = json.loads(data_bytes)
    except MemoryError:
        raise
    except ValueError as err:
        raise ConfigError("Invalid configuration JSON: {}".format(err))
    if not isinstance(value, dict):
        raise ConfigError("Configuration must contain a JSON object")
    return value


def config_checksum(data_bytes):
    """SHA-256 hex digest of the exact configuration file bytes.

    The committed-configuration identity (``hashlib.sha256`` +
    ``binascii.hexlify``); it never covers config-secrets.json, which is a
    separate file that never enters config.json.
    """
    return binascii.hexlify(hashlib.sha256(data_bytes).digest()).decode("ascii")


def load_config(path="config.json"):
    config, _checksum = load_config_with_checksum(path)
    return config


def load_config_with_checksum(path="config.json"):
    """Load, validate, and checksum the configuration file at path.

    Returns (config, checksum) where checksum is SHA-256 over the exact file
    bytes. MemoryError propagates; anything else is a ConfigError.
    """
    data_bytes = _read_config_file(path)
    config = parse_config_bytes(data_bytes)
    validate_config(config)
    return config, config_checksum(data_bytes)


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


# ---------------------------------------------------------------------------
# Staged persistence, commit, boot recovery
# ---------------------------------------------------------------------------

def _remove_quiet(path):
    try:
        os.remove(path)
    except MemoryError:
        raise
    except OSError:
        pass


def _restore_backup(path, backup_path):
    try:
        os.rename(backup_path, path)
    except MemoryError:
        raise
    except OSError:
        pass


def stage_config_candidate(candidate, path=CONFIG_FILE, staged_path=None):
    """Serialize a complete candidate to the staged file and verify it.

    The staged file (``<path>.tmp``) is a candidate only: it is never the
    active file and is never auto-promoted. Returns the SHA-256 checksum of
    the staged bytes. Raises ConfigError on any verification failure (the
    staged file is removed); MemoryError propagates.
    """
    validate_config(candidate)
    if staged_path is None:
        staged_path = path + CONFIG_STAGED_SUFFIX

    try:
        data = json.dumps(candidate)
    except MemoryError:
        raise
    except ValueError as err:
        raise ConfigError(
            "Unable to serialize candidate configuration: {}".format(err)
        )
    payload = data.encode("utf-8")

    try:
        with open(staged_path, "wb") as handle:
            handle.write(payload)
            handle.flush()
            sync = getattr(os, "sync", None)
            if sync is not None:
                sync()
    except MemoryError:
        raise
    except (OSError, ValueError, AttributeError) as err:
        _remove_quiet(staged_path)
        raise ConfigError("Unable to stage configuration: {}".format(err))

    staged_bytes = _read_config_file(staged_path)
    staged = parse_config_bytes(staged_bytes)
    validate_config(staged)

    if config_checksum(staged_bytes) != config_checksum(payload):
        _remove_quiet(staged_path)
        raise ConfigError("Staged configuration failed read-back verification")
    return config_checksum(staged_bytes)


def commit_config_file(path=CONFIG_FILE, staged_path=None):
    """Promote the staged file to the active configuration, recoverably.

    Sequence: rename primary -> ``.bak``, rename staged -> primary, read /
    validate / checksum the new primary, then delete the backup. A crash at
    any point leaves either a valid primary (before the first rename or
    after verification) or a valid backup (between the promotion and the
    verification) -- never a partial JSON as the active file, and the active
    file is never the file currently being constructed.

    Returns the new checksum. Raises ConfigError on failure (with the
    backup restored first if the primary had already moved); MemoryError
    propagates.
    """
    if staged_path is None:
        staged_path = path + CONFIG_STAGED_SUFFIX
    backup_path = path + CONFIG_BACKUP_SUFFIX

    try:
        os.rename(path, backup_path)
    except MemoryError:
        raise
    except OSError as err:
        raise ConfigError(
            "Unable to move current configuration to backup: {}".format(err)
        )

    try:
        os.rename(staged_path, path)
    except MemoryError:
        _restore_backup(path, backup_path)
        raise
    except OSError as err:
        _restore_backup(path, backup_path)
        raise ConfigError(
            "Unable to promote staged configuration: {}".format(err)
        )

    try:
        _config, checksum = load_config_with_checksum(path)
    except MemoryError:
        raise
    except ConfigError:
        _restore_backup(path, backup_path)
        raise ConfigError(
            "Committed configuration failed verification; backup restored"
        )

    # The backup is now redundant; a failed deletion is harmless (boot
    # recovery treats it as a valid last-known-good copy).
    _remove_quiet(backup_path)
    return checksum


def recover_config_file(path=CONFIG_FILE):
    """Boot-time configuration recovery (before any core starts).

    A valid primary wins (stale ``.tmp`` / ``.bak`` removed; the staged file
    is never promoted). A missing or invalid primary is replaced by a valid
    backup (renamed into place) and reported via ``recovery_info``. If
    neither file is valid, startup fails with a clear ConfigError -- no
    default configuration is silently invented.

    Returns (config, checksum, recovery_info); recovery_info is None or
    ``{"reason": "configuration_primary_invalid"}``. MemoryError propagates.
    """
    backup_path = path + CONFIG_BACKUP_SUFFIX
    staged_path = path + CONFIG_STAGED_SUFFIX

    try:
        config, checksum = load_config_with_checksum(path)
    except MemoryError:
        raise
    except ConfigError:
        config = None
    if config is not None:
        _remove_quiet(staged_path)
        _remove_quiet(backup_path)
        return config, checksum, None

    try:
        backup_config, backup_checksum = load_config_with_checksum(backup_path)
    except MemoryError:
        raise
    except ConfigError:
        backup_config = None
    if backup_config is not None:
        try:
            os.rename(backup_path, path)
        except MemoryError:
            raise
        except OSError as err:
            raise ConfigError(
                "Unable to restore configuration from backup: {}".format(err)
            )
        _remove_quiet(staged_path)
        return (
            backup_config,
            backup_checksum,
            {"reason": "configuration_primary_invalid"},
        )

    raise ConfigError(
        "Configuration is unusable: the primary configuration and its "
        "backup are both invalid; refusing to start with an unvalidated "
        "configuration"
    )


# ---------------------------------------------------------------------------
# Committed-configuration state (shared by both cores)
# ---------------------------------------------------------------------------

class ConfigState:
    """Lock-protected committed-configuration view shared by both cores.

    Holds the committed snapshot, generation, checksum, and the
    RESTART_REQUIRED bookkeeping (reboot_required / pending_restart_keys).
    Owns no Wi-Fi, MQTT, or device state. At construction (boot)
    ``reboot_required`` is false.
    """

    def __init__(self, config, checksum):
        self._lock = _thread.allocate_lock()
        self._config = config
        self._generation = config["config_generation"]
        self._schema_version = config["config_schema_version"]
        self._checksum = checksum
        self._reboot_required = False
        self._pending_restart_keys = ()
        # The values active at THIS boot for the RESTART_REQUIRED keys --
        # the reference a committed value must return to before
        # reboot_required clears.
        self._active_restart_values = {
            key: config[key] for key in _RESTART_REQUIRED_KEYS
        }

    def snapshot(self):
        """Compact immutable view (read_config / system-information shape)."""
        with self._lock:
            return {
                "config_schema_version": self._schema_version,
                "config_generation": self._generation,
                "config_checksum_sha256": self._checksum,
                "reboot_required": self._reboot_required,
                "pending_restart_keys": list(self._pending_restart_keys),
            }

    def committed_config(self):
        """The committed configuration (immutability by convention)."""
        with self._lock:
            return self._config

    def generation(self):
        with self._lock:
            return self._generation

    def commit(self, config, checksum):
        """Swap in the committed snapshot after a successful transaction.

        Recomputes reboot_required / pending_restart_keys against the
        active-at-boot values for the RESTART_REQUIRED keys.
        """
        validate_config(config)
        with self._lock:
            pending = tuple(
                key
                for key in _RESTART_REQUIRED_KEYS
                if config[key] != self._active_restart_values.get(key)
            )
            self._config = config
            self._generation = config["config_generation"]
            self._checksum = checksum
            self._reboot_required = bool(pending)
            self._pending_restart_keys = pending


# ---------------------------------------------------------------------------
# Per-core startup split (unchanged contract)
# ---------------------------------------------------------------------------

def split_config(config):
    """Create the immutable-by-convention per-core startup configuration."""
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
        "mqtt_broker_response_timeout_sec": config["mqtt_broker_response_timeout_sec"],
        "mqtt_post_outage_drain_rate_per_sec": config[
            "mqtt_post_outage_drain_rate_per_sec"
        ],
        "datetime_sync_interval_min": config["datetime_sync_interval_min"],
        "network_snapshot_interval_sec": config["network_snapshot_interval_sec"],
        "network_probe_timeout_sec": config["network_probe_timeout_sec"],
        "network_diagnostics_interval_sec": config["network_diagnostics_interval_sec"],
        "network_diagnostics_broker_latency_enabled": config[
            "network_diagnostics_broker_latency_enabled"
        ],
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

    # The outbound queue's entry ceiling is no longer user-tunable (it is the
    # fixed 64-entry sanity guard in intercore.MAX_OUTBOUND_QUEUE_ENTRIES). The
    # bus config now only carries the inter-core event-queue size.
    bus_config = {
        "max_intercore_event_entries": config["max_intercore_event_entries"],
    }
    return core0, core1, bus_config
