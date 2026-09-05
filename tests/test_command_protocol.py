# test_command_protocol.py - Global command protocol boundary tests
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""
Host-side tests for the global command protocol boundary (Core 0 ingress).

Covers the staged validation contract: the global message_schema_version gate (every decoded inbound object, before command and info_response handling, with no response and no debounce entry when it fails), case-insensitive IP target matching, the target/command/command_id length bounds, the per-command broadcast policy (* accepted for reboot/get-details/read-config, write-config * silently ignored), unknown command-envelope fields returned together and sorted, bounded invalid_command / unsupported_command answers, the read-config / write-config command contracts (read-config payload {} answered with the committed config and reboot state; write-config payload exactly {"config": <complete candidate configuration>}, executed on Core 0), and the debounce semantics (the first bounded targeted ID is cached before deeper validation; a malformed duplicate is suppressed after the first response).
"""

import importlib
import json
import pathlib
import sys
import time as _real_time
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.modules.setdefault("machine", MagicMock())

ROOT = pathlib.Path(__file__).resolve().parents[1]


class FakeTime:
    """Controllable stand-in for MicroPython's time module.

    sleep_ms advances the clock so bounded wait loops terminate deterministically in tests."""

    def __init__(self):
        self.now_ms = 0

    def ticks_ms(self):
        return self.now_ms

    def ticks_diff(self, now, prev):
        return now - prev

    def ticks_add(self, base, delta):
        return base + delta

    def sleep_ms(self, ms):
        self.now_ms += ms

    def sleep(self, secs):
        self.sleep_ms(int(secs * 1000))

    def __getattr__(self, name):
        return getattr(_real_time, name)


_FAKE_TIME = FakeTime()
_MACHINE_MOCK = MagicMock()
_DEBUG_MOCK = MagicMock()
_DEBUG_MOCK.DEBUG = False
_WIFI_MOCK = MagicMock()
_MQTT_MOCK = MagicMock()


def _install_mocks():
    sys.modules["time"] = _FAKE_TIME
    sys.modules["machine"] = _MACHINE_MOCK
    sys.modules["debug"] = _DEBUG_MOCK
    sys.modules["wifi"] = _WIFI_MOCK
    sys.modules["mqtt"] = _MQTT_MOCK


# NOTE: the MicroPython stand-ins above must NOT be installed at collection
# time: other test modules import the real wifi/mqtt/time modules at
# collection, and mocked entries in sys.modules would shadow them. They are
# installed inside the fixture below, which also imports/reloads core0 there.
import os  # noqa: E402
import tempfile  # noqa: E402

from config import split_config  # noqa: E402
from config_manager import ConfigManager  # noqa: E402
from version import MESSAGE_SCHEMA_VERSION  # noqa: E402
import command_protocol  # noqa: E402


def _full_config():
    return json.loads((ROOT / "tests" / "fixtures" / "config.json").read_text())


def _load_core0_config():
    config = _full_config()
    core0_config, _core1_config = split_config(config)
    return core0_config


class FakeWifi:
    """Fixed Wi-Fi state for protocol-boundary tests (IP configurable for the
    case-insensitivity test)."""

    connected = True

    def __init__(self, ip_address="192.168.1.50"):
        self._ip_address = ip_address

    def ip_address(self):
        return self._ip_address


class RecordingEventQueue:
    """Event queue that records every admitted command event."""

    def __init__(self):
        self.events = []

    def put(self, event):
        self.events.append(event)
        return True

    def take(self):
        if self.events:
            return self.events.pop(0)
        return None


class FakeConfigUpdateLane:
    """Records posted config-update requests; the test posts a result to
    drive Core 0's acknowledgement resolution."""

    def __init__(self):
        self.requests = []
        self._result = None

    def post_request(self, request):
        self.requests.append(request)

    def take_request(self):
        if self.requests:
            return self.requests.pop(0)
        return None

    def post_result(self, result):
        self._result = result

    def take_result_for(self, generation):
        if self._result is not None and self._result.get("generation") == generation:
            result = self._result
            self._result = None
            return result
        return None


class MockInterCore:
    def __init__(self):
        self.state_mailboxes = MagicMock()
        self.outbound_queue = MagicMock()
        self.event_queue = RecordingEventQueue()
        self.config_update_lane = FakeConfigUpdateLane()


@pytest.fixture
def make_core0():
    """Build a fresh Core0 with faked wifi/mqtt and a real config."""

    def _make(ip_address="192.168.1.50"):
        _FAKE_TIME.now_ms = 0
        _install_mocks()
        importlib.reload(importlib.import_module("uptime"))
        core0_mod = importlib.import_module("core0")
        importlib.reload(core0_mod)

        config = _load_core0_config()
        # A real committed config for the config-command tests: the manager
        # owns its own file, isolated from the repository's config.json.
        manager_dir = tempfile.mkdtemp()
        with open(os.path.join(manager_dir, "config.json"), "w") as handle:
            handle.write(json.dumps(_full_config()))
        manager = ConfigManager(os.path.join(manager_dir, "config.json"))
        instance = core0_mod.Core0(
            MockInterCore(),
            config,
            {"wifi_ssid": "test-ssid", "wifi_password": "test-password"},
            "test-runtime",
            0,
            MagicMock(),
            manager,
        )
        instance._wifi = FakeWifi(ip_address)
        instance._mqtt = MagicMock()
        return instance

    return _make


def _command(target="Test-Pico-2", command_id="proto-001", command="get-details",
             payload={}, schema_version=MESSAGE_SCHEMA_VERSION, **extra):
    doc = {
        "message_type": "command",
        "target": target,
        "command_id": command_id,
        "command": command,
        "payload": payload,
    }
    if schema_version is not None:
        doc["message_schema_version"] = schema_version
    doc.update(extra)
    return doc


def _send(core0, doc):
    """Deliver one command document to Core 0's MQTT receive path."""
    core0._on_mqtt_message(core0._config["mqtt_topic_command"], json.dumps(doc))


def _responses(core0):
    return list(core0._pending_core0_responses)


def _last_response(core0):
    assert core0._pending_core0_responses
    return core0._pending_core0_responses[-1]


# --- Global message_schema_version gate -----------------------------------


def test_wrong_schema_version_is_ignored_no_response_no_cache(make_core0):
    """A wrong or missing message_schema_version is ignored for the whole
    message: no response, no event, and no debounce-cache entry."""
    core0 = make_core0()

    _send(core0, _command(schema_version=MESSAGE_SCHEMA_VERSION - 1))
    assert core0._intercore.event_queue.events == []
    assert _responses(core0) == []
    assert core0._recent_command_ids == []

    missing = _command(command_id="proto-missing")
    del missing["message_schema_version"]
    core0._on_mqtt_message(core0._config["mqtt_topic_command"], json.dumps(missing))
    assert core0._intercore.event_queue.events == []
    assert _responses(core0) == []
    assert core0._recent_command_ids == []

    # A wrong-typed version is ignored too: the gate compares against an
    # integer, so a string or float that value-compares equal still fails.
    for wrong_type in (str(MESSAGE_SCHEMA_VERSION), float(MESSAGE_SCHEMA_VERSION)):
        wrong_typed = _command(command_id="proto-typed")
        wrong_typed["message_schema_version"] = wrong_type
        core0._on_mqtt_message(core0._config["mqtt_topic_command"], json.dumps(wrong_typed))
        assert core0._intercore.event_queue.events == []
        assert _responses(core0) == []
        assert core0._recent_command_ids == []

    # A supported-version message with the same ID is still processed: the
    # gate did not poison the ID.
    _send(core0, _command(schema_version=MESSAGE_SCHEMA_VERSION))
    assert len(core0._intercore.event_queue.events) == 1


def test_gate_runs_before_command_envelope_validation(make_core0):
    """The gate precedes the envelope unknown-field check: a wrong-version
    document with an unknown field gets no response at all (the gate), not
    the unknown_fields error the envelope check would answer with."""
    core0 = make_core0()

    _send(core0, _command(schema_version=1, foo="bar"))
    assert _responses(core0) == []
    assert core0._recent_command_ids == []


def test_wrong_schema_version_info_response_does_not_touch_utc_state(make_core0):
    """The gate applies to inbound info_response too: a wrong-version answer
    to our pending request is not interpreted and does not alter the pending
    request state (a malformed-but-supported-version answer would clear it)."""
    core0 = make_core0()
    core0._pending_utc_request_id = "test-runtime_1"

    doc = {
        "message_type": "info_response",
        "message_schema_version": MESSAGE_SCHEMA_VERSION - 1,
        "source": "server",
        "target": core0._config["source"],
        "request_type": "utc_time",
        "request_id": "test-runtime_1",
        "payload": {
            "timestamp": "2026-01-01T00:00:00Z",
            "utc_epoch_ms": 1767225600000,
        },
    }
    core0._on_mqtt_message(core0._config["mqtt_topic_info_response"], json.dumps(doc))

    assert core0._utc_snapshot is None
    assert core0._pending_utc_request_id == "test-runtime_1"
    assert core0._utc_last_attempt_ms is None


# --- Target policy ----------------------------------------------------------


def test_ip_target_is_case_insensitive_string_equivalent(make_core0):
    """IP matching uses the same case-insensitive string comparison as source
    matching (a casing variant of the device's IP addresses it)."""
    core0 = make_core0(ip_address="ABCD:EF01::7")

    _send(core0, _command(target="abcd:ef01::7", command_id="proto-ip"))

    assert len(core0._intercore.event_queue.events) == 1
    assert core0._intercore.event_queue.events[0]["targeted"] is True
    assert _responses(core0) == []


def test_target_length_boundaries(make_core0):
    """A target is a non-empty string of at most 128 characters before
    matching; over-long targets are treated as non-matching (ignored, no
    response, no cache entry)."""
    assert command_protocol.is_bounded_target("x" * command_protocol.MAX_TARGET_LENGTH)
    assert not command_protocol.is_bounded_target("x" * (command_protocol.MAX_TARGET_LENGTH + 1))
    assert not command_protocol.is_bounded_target("")
    assert not command_protocol.is_bounded_target(None)

    core0 = make_core0()
    _send(core0, _command(target="x" * (command_protocol.MAX_TARGET_LENGTH + 1)))
    assert core0._intercore.event_queue.events == []
    assert _responses(core0) == []
    assert core0._recent_command_ids == []


# --- Broadcast policy per command ------------------------------------------


def test_broadcast_accepted_for_reboot_get_details_read_config(make_core0):
    """The * broadcast is valid for reboot, get-details, and read-config: each
    is addressed to this device (non-targeted), not silently dropped."""
    core0 = make_core0()
    _send(core0, _command(target="*", command_id="proto-bc-reboot", command="reboot"))
    assert core0._pending_reboot == {
        "command_id": "proto-bc-reboot",
        "command": "reboot",
        "targeted": False,
    }

    core0 = make_core0()
    _send(core0, _command(target="*", command_id="proto-bc-details", command="get-details"))
    assert len(core0._intercore.event_queue.events) == 1
    assert core0._intercore.event_queue.events[0]["targeted"] is False

    core0 = make_core0()
    _send(core0, _command(target="*", command_id="proto-bc-read", command="read-config"))
    response = _last_response(core0)
    assert response["targeted"] is False
    assert response["success"] is True
    assert response["data"]["reboot_required"] is False


def test_write_config_broadcast_is_silently_ignored(make_core0):
    """A recognized write-config command for * is silently ignored: no
    configuration validation, no filesystem operation, no response -- but the
    compatible ID remains claimed by the debounce cache."""
    core0 = make_core0()

    _send(core0, _command(target="*", command_id="proto-bc-write", command="write-config",
                          payload={"config": _full_config()}))

    assert core0._intercore.event_queue.events == []
    assert _responses(core0) == []
    assert core0._recent_command_ids == ["proto-bc-write"]

    # A duplicate copy is suppressed like any claimed ID.
    _send(core0, _command(target="*", command_id="proto-bc-write", command="write-config"))
    assert core0._intercore.event_queue.events == []
    assert _responses(core0) == []


def test_write_config_targeted_is_executed(make_core0):
    """A targeted write-config carries exactly {"config": <complete candidate
    configuration>} as its payload and is executed on Core 0: validated,
    committed, applied -- the boundary no longer answers it not_implemented."""
    core0 = make_core0()
    candidate = _full_config()
    candidate["read_loop_sec"] = 30

    _send(core0, _command(command_id="proto-write", command="write-config",
                          payload={"config": candidate}))

    # read_loop_sec belongs to Core 1: the internal config-update request on
    # the dedicated lane carries it (not a user-command event).
    assert core0._intercore.event_queue.events == []
    assert core0._intercore.config_update_lane.requests == [
        {"generation": 1, "read_loop_sec": 30}
    ]
    # Core 1 applies and acknowledges: the transaction commits and the response
    # is released.
    gen = core0._pending_config_update["generation"]
    core0._intercore.config_update_lane.post_result(
        {"generation": gen, "success": True})
    core0._resolve_pending_config_update()

    response = _last_response(core0)
    assert response["command_id"] == "proto-write"
    assert response["command"] == "write-config"
    assert response["targeted"] is True
    assert response["success"] is True
    assert response["data"]["configuration_changed"] is True
    assert response["data"]["classification"] == "HOT_RELOADED"
    assert response["data"]["reboot_required"] is False


def test_read_config_targeted_returns_committed_config(make_core0):
    """A targeted read-config returns the committed (PERSISTED) configuration
    and the derived reboot state."""
    core0 = make_core0()

    _send(core0, _command(command_id="proto-read", command="read-config", payload={}))

    assert core0._intercore.event_queue.events == []
    response = _last_response(core0)
    assert response["command_id"] == "proto-read"
    assert response["command"] == "read-config"
    assert response["targeted"] is True
    assert response["success"] is True
    assert response["data"]["config"] == _full_config()
    assert response["data"]["reboot_required"] is False


# --- Envelope unknown fields ------------------------------------------------


def test_unknown_envelope_fields_are_returned_together_sorted(make_core0):
    """Unknown top-level fields are all named in one deterministic sorted
    array (v3 command contract), with the bounded identifying fields
    preserved."""
    core0 = make_core0()

    _send(core0, _command(command_id="proto-unk", foo=1, bar="x", another=3.5))

    assert core0._intercore.event_queue.events == []
    response = _last_response(core0)
    assert response["command_id"] == "proto-unk"
    assert response["command"] == "get-details"
    assert response["success"] is False
    assert response["error"] == {
        "code": "unknown_fields",
        "message": "Message contains unknown fields",
        "unknown_fields": ["another", "bar", "foo"],
    }


def test_malformed_duplicate_is_suppressed_after_first_response(make_core0):
    """The first bounded targeted ID is cached before deeper validation: the
    malformed message answers once, and repeated copies are silently ignored
    instead of generating repeated validation responses."""
    core0 = make_core0()

    for _ in range(3):
        _send(core0, _command(command_id="proto-mal", foo=1))

    assert core0._intercore.event_queue.events == []
    assert len(core0._pending_core0_responses) == 1
    assert core0._pending_core0_responses[0]["error"]["code"] == "unknown_fields"


# --- Bounded command name ---------------------------------------------------


def test_non_string_or_empty_command_is_answered_invalid_command(make_core0):
    """A command that is not a non-empty string is a bounded invalid_command
    error, is never echoed, and its bounded ID still claims the debounce
    entry."""
    core0 = make_core0()
    bad = _command(command_id="proto-nsc")
    bad["command"] = 42
    core0._on_mqtt_message(core0._config["mqtt_topic_command"], json.dumps(bad))

    assert core0._recent_command_ids == ["proto-nsc"]
    response = _last_response(core0)
    assert response["command_id"] == "proto-nsc"
    assert response["error"]["code"] == "invalid_command"
    assert "command" not in response

    core0 = make_core0()
    _send(core0, _command(command_id="proto-empty", command=""))
    response = _last_response(core0)
    assert response["error"]["code"] == "invalid_command"
    assert "command" not in response


# --- command_protocol helpers ------------------------------------------------


def test_protocol_string_bounds_are_inclusive():
    """The bounds are inclusive: exactly-at-bound is valid, one-over is not."""
    assert command_protocol.is_bounded_command_id("i" * command_protocol.MAX_COMMAND_ID_LENGTH)
    assert not command_protocol.is_bounded_command_id("i" * (command_protocol.MAX_COMMAND_ID_LENGTH + 1))
    assert command_protocol.is_bounded_command("c" * command_protocol.MAX_COMMAND_LENGTH)
    assert not command_protocol.is_bounded_command("c" * (command_protocol.MAX_COMMAND_LENGTH + 1))
    assert not command_protocol.is_bounded_command_id("")
    assert not command_protocol.is_bounded_command(None)
    assert not command_protocol.is_bounded_command_id(42)


def test_broadcast_policy_constants():
    """write-config is the only supported command that rejects the *
    broadcast target."""
    assert command_protocol.BROADCAST_EXCLUDED_COMMANDS == frozenset(
        (command_protocol.COMMAND_WRITE_CONFIG,)
    )


def test_supported_registry_membership():
    """The complete supported set is the four named commands; only
    get-details is Core 1-owned."""
    for command in ("reboot", "get-details", "read-config", "write-config"):
        assert command_protocol.is_supported_command(command)
    assert not command_protocol.is_supported_command("set-brightness")
    assert command_protocol.CORE1_OWNED_COMMANDS == (command_protocol.COMMAND_GET_DETAILS,)


def test_unknown_field_names_is_sorted_and_complete():
    """unknown_field_names reports every offending name, sorted, and nothing
    else (an all-known object yields an empty list)."""
    assert command_protocol.unknown_field_names(
        ("foo", "bar", "message_type"),
        command_protocol.COMMAND_ENVELOPE_KEYS,
    ) == ["bar", "foo"]
    assert command_protocol.unknown_field_names(
        command_protocol.COMMAND_ENVELOPE_KEYS,
        command_protocol.COMMAND_ENVELOPE_KEYS,
    ) == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
