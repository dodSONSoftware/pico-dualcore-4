# test_observability.py - Tests for the stable event/reason vocabulary
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import observability as obs


def _vocabulary_constants(prefixes):
    names = []
    for name in dir(obs):
        if name.startswith(prefixes) and name.isupper():
            names.append(name)
    return names


def test_levels_are_exactly_info_warning_error():
    levels = {obs.LEVEL_INFO, obs.LEVEL_WARNING, obs.LEVEL_ERROR}
    assert levels == {"INFO", "WARNING", "ERROR"}


def test_all_event_and_reason_constants_are_finite_snake_case_strings():
    for name in _vocabulary_constants(("EVENT_", "REASON_", "BOOT_REASON_")):
        value = getattr(obs, name)
        assert isinstance(value, str), name
        assert value, name
        # Machine-readable canonical spelling: lowercase, digits, underscores.
        assert all(c.islower() or c.isdigit() or c == "_" for c in value), (name, value)


def test_event_and_reason_are_separate_concepts():
    # A failed Wi-Fi connection is EVENT + REASON, not an event named after
    # the reason: both canonical values exist and pair through the helper.
    event = obs.EVENT_WIFI_CONNECTION_ATTEMPT_FAILED
    reason = obs.REASON_WIFI_NO_AP_FOUND
    assert event == "wifi_connection_attempt_failed"
    assert reason == "wifi_no_ap_found"
    payload = obs.build_event_payload(obs.LEVEL_WARNING, event, reason)
    assert payload["event"] == "wifi_connection_attempt_failed"
    assert payload["reason_code"] == "wifi_no_ap_found"
    # No event constant is minted from a reason value of this shape.
    event_values = {getattr(obs, n) for n in _vocabulary_constants(("EVENT_",))}
    assert "wifi_no_ap_found" not in event_values


def test_command_identity_is_structured_data_not_event_names():
    # The command name never becomes part of the event string.
    event_values = {getattr(obs, n) for n in _vocabulary_constants(("EVENT_",))}
    assert "reboot_command" not in event_values
    assert "command_reboot" not in event_values
    payload = obs.build_event_payload(
        obs.LEVEL_WARNING,
        obs.EVENT_COMMAND_REJECTED,
        obs.REASON_COMMAND_INVALID_PAYLOAD,
        "Command rejected",
        {"command": "reboot", "command_id": "req-1"},
    )
    assert payload["event"] == "command_rejected"
    assert payload["data"]["command"] == "reboot"


def test_device_identity_is_structured_data_not_event_names():
    # One generic device vocabulary: the driver type is data, never the event.
    event_values = {getattr(obs, n) for n in _vocabulary_constants(("EVENT_",))}
    assert "system_information_read_failed" not in event_values
    assert "temperature_read_failed" not in event_values
    payload = obs.build_event_payload(
        obs.LEVEL_WARNING,
        obs.EVENT_DEVICE_READ_FAILED,
        obs.REASON_DEVICE_READ_EXCEPTION,
        "Device read failed",
        {"device_id": "dev-2", "device": "temperature"},
    )
    assert payload["event"] == "device_read_failed"
    assert payload["data"]["device"] == "temperature"


def test_success_events_use_none_and_recovery_events_use_recovery_reasons():
    assert obs.build_event_payload(obs.LEVEL_INFO, obs.EVENT_RUNTIME_STARTED)["reason_code"] == "none"
    assert obs.build_event_payload(obs.LEVEL_INFO, obs.EVENT_WIFI_CONNECTION_ESTABLISHED)["reason_code"] == "none"
    assert obs.build_event_payload(obs.LEVEL_INFO, obs.EVENT_MQTT_CONNECTION_ESTABLISHED)["reason_code"] == "none"
    # Recovery completions carry their recovery reason, not "none".
    wifi = obs.build_event_payload(
        obs.LEVEL_INFO, obs.EVENT_WIFI_RECONNECT_COMPLETED, obs.REASON_WIFI_RECONNECT_SUCCEEDED)
    assert wifi["reason_code"] == "wifi_reconnect_succeeded"
    mqtt = obs.build_event_payload(
        obs.LEVEL_INFO, obs.EVENT_MQTT_RECONNECT_COMPLETED, obs.REASON_MQTT_RECONNECT_SUCCEEDED)
    assert mqtt["reason_code"] == "mqtt_reconnect_succeeded"


def test_raw_exception_text_never_becomes_a_reason_code():
    # Simulate the two failure shapes the spec calls out: the reason code is
    # the canonical constant, the exception text stays human-readable in data.
    for err in (OSError("ETIMEDOUT"), ValueError("math domain error")):
        payload = obs.build_event_payload(
            obs.LEVEL_WARNING,
            obs.EVENT_DEVICE_READ_FAILED,
            obs.REASON_DEVICE_READ_EXCEPTION,
            "Device read failed",
            {"error": str(err)},
        )
        assert payload["reason_code"] == "device_read_exception"
        assert str(err) in payload["data"]["error"]

    # The finite vocabulary contains no exception-derived token.
    reason_values = {getattr(obs, n) for n in _vocabulary_constants(("REASON_",))}
    assert "ETIMEDOUT" not in reason_values
    assert "math domain error" not in reason_values


def test_minimal_payload_is_exactly_level_event_reason_code():
    payload = obs.build_event_payload(obs.LEVEL_ERROR, obs.EVENT_RUNTIME_CORE1_STALLED,
                                      obs.REASON_CORE1_HEARTBEAT_TIMEOUT)
    assert payload == {
        "level": "ERROR",
        "event": "runtime_core1_stalled",
        "reason_code": "core1_heartbeat_timeout",
    }
    assert "message" not in payload
    assert "data" not in payload
    assert "module" not in payload


def test_optional_keys_present_only_when_given():
    with_message = obs.build_event_payload(
        obs.LEVEL_INFO, obs.EVENT_UTC_SYNC_COMPLETED, obs.REASON_NONE, "UTC synchronized")
    assert set(with_message) == {"level", "event", "reason_code", "message"}

    with_data = obs.build_event_payload(
        obs.LEVEL_WARNING, obs.EVENT_DEVICE_READ_FAILED, obs.REASON_DEVICE_READ_EXCEPTION,
        "Device read failed", {"device_id": "dev-1", "error": "ETIMEDOUT"})
    assert set(with_data) == {"level", "event", "reason_code", "message", "data"}


def test_no_forced_empty_optional_keys():
    # data=None and message=None never force an empty key into the payload.
    payload = obs.build_event_payload(obs.LEVEL_INFO, obs.EVENT_RUNTIME_STARTED,
                                      obs.REASON_NONE, None, None)
    assert "message" not in payload
    assert "data" not in payload
    assert payload["reason_code"] == "none"


def test_reason_code_defaults_to_none():
    payload = obs.build_event_payload(obs.LEVEL_INFO, obs.EVENT_UTC_SYNC_COMPLETED)
    assert payload["reason_code"] == obs.REASON_NONE == "none"


def test_boot_reason_vocabulary_is_finite():
    boot_reasons = {
        obs.BOOT_REASON_POWER_ON,
        obs.BOOT_REASON_WATCHDOG_RECOVERY,
        obs.BOOT_REASON_SOFT_RESET,
        obs.BOOT_REASON_UNKNOWN,
    }
    assert boot_reasons == {"power_on", "watchdog_recovery", "soft_reset", "unknown"}
    # No "explicit_reboot" boot reason: the firmware writes no persisted
    # evidence, so it must not be minted as a boot reason value.
    assert "explicit_reboot" not in boot_reasons
