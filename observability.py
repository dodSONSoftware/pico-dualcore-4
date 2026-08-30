# observability.py - Stable diagnostic event and reason vocabulary
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT
#
# Finite, machine-readable vocabularies for the diagnostic log contract:
# level, event, and reason_code values, plus the one small helper that builds
# the log payload shape. Pure module-level string constants -- no classes, no
# registries, no runtime validation, no per-event state. The human-readable
# message is NOT part of this vocabulary: consumers query event/reason_code,
# never the message.
#
# Event names describe what happened (domain + subject + state/outcome);
# reason codes describe why. The two stay separate concepts: a failed Wi-Fi
# connection is event wifi_connection_attempt_failed with reason
# wifi_no_ap_found, not event wifi_no_ap_found.
#
# Not every constant below is emitted today; the emitted set and its
# level/reason pairing are specified in ARCHITECTURE.md (Observability
# contract). Reserved names keep the vocabulary stable so a future design
# that emits them cannot mint a competing spelling.


# ---------------------------------------------------------------------------
# Levels
# ---------------------------------------------------------------------------

LEVEL_INFO = "INFO"
LEVEL_WARNING = "WARNING"
LEVEL_ERROR = "ERROR"

# ---------------------------------------------------------------------------
# Reason codes (finite canonical vocabulary)
# ---------------------------------------------------------------------------

REASON_NONE = "none"
REASON_UNKNOWN = "unknown"
REASON_INTERNAL_ERROR = "internal_error"
REASON_INVALID_STATE = "invalid_state"
REASON_TIMEOUT = "timeout"

# Wi-Fi (only values the firmware can actually evidence)
REASON_WIFI_DISCONNECTED = "wifi_disconnected"
REASON_WIFI_WRONG_PASSWORD = "wifi_wrong_password"
REASON_WIFI_NO_AP_FOUND = "wifi_no_ap_found"
REASON_WIFI_CONNECT_FAILED = "wifi_connect_failed"
REASON_WIFI_CONNECTION_TIMEOUT = "wifi_connection_timeout"
REASON_WIFI_NETWORK_PROBE_FAILED = "wifi_network_probe_failed"
REASON_WIFI_RECOVERY_ESCALATION = "wifi_recovery_escalation"
REASON_WIFI_RECONNECT_SUCCEEDED = "wifi_reconnect_succeeded"

# MQTT
REASON_MQTT_CONNECT_FAILED = "mqtt_connect_failed"
REASON_MQTT_CONNACK_REJECTED = "mqtt_connack_rejected"
REASON_MQTT_SUBSCRIPTION_FAILED = "mqtt_subscription_failed"
REASON_MQTT_PUBLISH_FAILED = "mqtt_publish_failed"
REASON_MQTT_PUBLISH_WRITE_TIMEOUT = "mqtt_publish_write_timeout"
REASON_MQTT_PUBACK_TIMEOUT = "mqtt_puback_timeout"
REASON_MQTT_PING_TIMEOUT = "mqtt_ping_timeout"
REASON_MQTT_PROTOCOL_ERROR = "mqtt_protocol_error"
REASON_MQTT_SOCKET_ERROR = "mqtt_socket_error"
REASON_MQTT_CONNECTION_LOST = "mqtt_connection_lost"
REASON_MQTT_RECONNECT_SUCCEEDED = "mqtt_reconnect_succeeded"

# Devices
REASON_DEVICE_INITIALIZATION_EXCEPTION = "device_initialization_exception"
REASON_DEVICE_INITIALIZATION_FAILED = "device_initialization_failed"
REASON_DEVICE_READ_EXCEPTION = "device_read_exception"
REASON_DEVICE_READ_FAILURE_THRESHOLD_REACHED = "device_read_failure_threshold_reached"
REASON_DEVICE_REINITIALIZATION_FAILED = "device_reinitialization_failed"
REASON_DEVICE_REINITIALIZATION_SUCCEEDED = "device_reinitialization_succeeded"
REASON_DEVICE_CONFIGURATION_INVALID = "device_configuration_invalid"

# Commands
REASON_COMMAND_INVALID_ENVELOPE = "command_invalid_envelope"
REASON_COMMAND_INVALID_TARGET = "command_invalid_target"
REASON_COMMAND_UNKNOWN = "command_unknown"
REASON_COMMAND_INVALID_PAYLOAD = "command_invalid_payload"
REASON_COMMAND_EXECUTION_FAILED = "command_execution_failed"
REASON_COMMAND_DUPLICATE = "command_duplicate"
REASON_COMMAND_COMPLETED = "command_completed"

# Configuration transactions (read_config / write_config). The rejection
# codes double as command response error codes; the transaction codes name
# the failed stage.
REASON_CONFIG_INVALID_KEY = "invalid_config_key"
REASON_CONFIG_READ_ONLY_KEY = "read_only_config_key"
REASON_CONFIG_INVALID_VALUE = "invalid_config_value"
REASON_CONFIG_INVALID_COMBINATION = "invalid_config_combination"
REASON_CONFIG_UPDATE_IN_PROGRESS = "configuration_update_in_progress"
REASON_CONFIG_STAGE_FAILED = "configuration_stage_failed"
REASON_CONFIG_RECONFIGURE_FAILED = "configuration_reconfigure_failed"
REASON_CONFIG_PERSISTENCE_FAILED = "configuration_persistence_failed"
REASON_CONFIG_ROLLBACK_FAILED = "configuration_rollback_failed"
REASON_CONFIG_PRIMARY_INVALID = "configuration_primary_invalid"

# Runtime / reset causes (values mirror the RESET_CAUSE_* constants in
# hardware.py -- the reset-cause vocabulary lives there, these are its
# reason-code faces, not a second mapping)
REASON_POWER_ON_RESET = "power_on_reset"
REASON_HARD_RESET = "hard_reset"
REASON_SOFT_RESET = "soft_reset"
REASON_WATCHDOG_RESET = "watchdog_reset"
REASON_DEEP_SLEEP_RESET = "deep_sleep_reset"
REASON_UNKNOWN_RESET = "unknown_reset"
REASON_CORE1_HEARTBEAT_TIMEOUT = "core1_heartbeat_timeout"
REASON_FATAL_MEMORY_ERROR = "fatal_memory_error"
REASON_FATAL_CONFIGURATION_ERROR = "fatal_configuration_error"
REASON_EXPLICIT_REBOOT_COMMAND = "explicit_reboot_command"

# Boot reasons (derived from the reset cause by hardware.derive_boot_reason)
BOOT_REASON_POWER_ON = "power_on"
BOOT_REASON_WATCHDOG_RECOVERY = "watchdog_recovery"
BOOT_REASON_SOFT_RESET = "soft_reset"
BOOT_REASON_UNKNOWN = "unknown"

# ---------------------------------------------------------------------------
# Events (finite canonical vocabulary)
# ---------------------------------------------------------------------------

# Runtime / startup
EVENT_RUNTIME_BOOT_STARTED = "runtime_boot_started"
EVENT_RUNTIME_STARTED = "runtime_started"
EVENT_RUNTIME_REBOOT_REQUESTED = "runtime_reboot_requested"
EVENT_RUNTIME_REBOOTING = "runtime_rebooting"
EVENT_RUNTIME_CORE1_STARTED = "runtime_core1_started"
EVENT_RUNTIME_CORE1_STALLED = "runtime_core1_stalled"
EVENT_RUNTIME_FATAL_ERROR = "runtime_fatal_error"

# Configuration
EVENT_CONFIGURATION_LOADED = "configuration_loaded"
EVENT_CONFIGURATION_REJECTED = "configuration_rejected"
EVENT_CONFIGURATION_UPDATE_STARTED = "configuration_update_started"
EVENT_CONFIGURATION_UPDATE_COMPLETED = "configuration_update_completed"
EVENT_CONFIGURATION_UPDATE_FAILED = "configuration_update_failed"
EVENT_CONFIGURATION_ROLLBACK_COMPLETED = "configuration_rollback_completed"
EVENT_CONFIGURATION_ROLLBACK_FAILED = "configuration_rollback_failed"
EVENT_CONFIGURATION_RECOVERED = "configuration_recovered"

# Wi-Fi
EVENT_WIFI_CONNECTION_ATTEMPT_STARTED = "wifi_connection_attempt_started"
EVENT_WIFI_CONNECTION_ATTEMPT_FAILED = "wifi_connection_attempt_failed"
EVENT_WIFI_CONNECTION_ESTABLISHED = "wifi_connection_established"
EVENT_WIFI_CONNECTION_LOST = "wifi_connection_lost"
EVENT_WIFI_RECONNECT_STARTED = "wifi_reconnect_started"
EVENT_WIFI_RECONNECT_COMPLETED = "wifi_reconnect_completed"

# MQTT
EVENT_MQTT_CONNECTION_ATTEMPT_STARTED = "mqtt_connection_attempt_started"
EVENT_MQTT_CONNECTION_ATTEMPT_FAILED = "mqtt_connection_attempt_failed"
EVENT_MQTT_CONNECTION_ESTABLISHED = "mqtt_connection_established"
EVENT_MQTT_CONNECTION_LOST = "mqtt_connection_lost"
EVENT_MQTT_RECONNECT_STARTED = "mqtt_reconnect_started"
EVENT_MQTT_RECONNECT_COMPLETED = "mqtt_reconnect_completed"
EVENT_MQTT_PUBLISH_FAILED = "mqtt_publish_failed"
EVENT_MQTT_PUBACK_TIMEOUT = "mqtt_puback_timeout"
EVENT_MQTT_SUBSCRIPTION_FAILED = "mqtt_subscription_failed"
EVENT_MQTT_PROTOCOL_ERROR = "mqtt_protocol_error"

# Devices (one generic vocabulary for all driver types; the device identity
# is structured data, never part of the event name)
EVENT_DEVICE_INITIALIZATION_COMPLETED = "device_initialization_completed"
EVENT_DEVICE_INITIALIZATION_FAILED = "device_initialization_failed"
EVENT_DEVICE_READ_FAILED = "device_read_failed"
EVENT_DEVICE_REINITIALIZATION_STARTED = "device_reinitialization_started"
EVENT_DEVICE_REINITIALIZATION_COMPLETED = "device_reinitialization_completed"
EVENT_DEVICE_REINITIALIZATION_FAILED = "device_reinitialization_failed"

# Commands (the command name is structured data, never part of the event)
EVENT_COMMAND_RECEIVED = "command_received"
EVENT_COMMAND_COMPLETED = "command_completed"
EVENT_COMMAND_FAILED = "command_failed"
EVENT_COMMAND_REJECTED = "command_rejected"

# UTC / time
EVENT_UTC_SYNC_STARTED = "utc_sync_started"
EVENT_UTC_SYNC_COMPLETED = "utc_sync_completed"
EVENT_UTC_SYNC_FAILED = "utc_sync_failed"

# Queue / memory (transitions only; routine activity stays in counters)
EVENT_OUTBOUND_QUEUE_PRESSURE_DETECTED = "outbound_queue_pressure_detected"
EVENT_OUTBOUND_QUEUE_MESSAGE_EVICTED = "outbound_queue_message_evicted"
EVENT_OUTBOUND_QUEUE_MESSAGE_REJECTED = "outbound_queue_message_rejected"
EVENT_MEMORY_PRESSURE_DETECTED = "memory_pressure_detected"
EVENT_MEMORY_PRESSURE_RECOVERED = "memory_pressure_recovered"


def build_event_payload(level, event, reason_code=REASON_NONE, message=None, data=None):
    """
    Build one diagnostic log payload.

    The payload is the log message's "payload" member: level, event, and
    reason_code are always present; message and data are present only when
    given (an empty optional key is never forced, so a minimal event is
    {"level", "event", "reason_code"}).

    Args:
        level: one of LEVEL_INFO / LEVEL_WARNING / LEVEL_ERROR
        event: an EVENT_* constant
        reason_code: a REASON_* constant (default REASON_NONE)
        message: optional human-readable explanation (operators, not machines)
        data: optional focused structured values (a JSON-safe dict)

    Returns:
        dict: the log payload
    """
    payload = {
        "level": level,
        "event": event,
        "reason_code": reason_code,
    }
    if message is not None:
        payload["message"] = message
    if data is not None:
        payload["data"] = data
    return payload
