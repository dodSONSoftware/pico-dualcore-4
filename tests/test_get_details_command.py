# test_get_details_command.py - Tests for the get-details command
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Host-side tests for the get-details command under the strict command protocol contract.

get-details is owned by Core 1 (the authoritative SystemInformation instance and device-manager state live there), but Core 0 owns the command protocol boundary. Core 0 validates the command against its supported-command registry (case-insensitive source/IP/* target), enforces the {} payload contract (any key is an unknown field, named sorted), suppresses a duplicate command_id before an event is queued, answers unknown commands itself (not Core 1), and reports the event-queue memory-pressure failure. Core 1 executes the dispatched get-details event and returns the full system-information snapshot.
"""

import importlib
import json
import pathlib
import sys
import time as _real_time
from unittest.mock import MagicMock, patch

import pytest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.modules.setdefault("machine", MagicMock())

ROOT = pathlib.Path(__file__).resolve().parents[1]


# --- Core 1 execution harness ---------------------------------------------

import core1  # noqa: E402
from intercore import KIND_COMMAND_RESPONSE  # noqa: E402
from system_information import SYSTEM_INFORMATION_SECTIONS  # noqa: E402


class EventQueue:
    def __init__(self, event):
        self._event = event

    def take(self):
        event = self._event
        self._event = None
        return event


class InterCore:
    def __init__(self, event):
        self.event_queue = EventQueue(event)


class FullSystemInformation:
    def __getattr__(self, name):
        if not name.startswith("get_"):
            raise AttributeError(name)
        section = name[4:]
        if section not in SYSTEM_INFORMATION_SECTIONS:
            raise AttributeError(name)
        return lambda: {"section": section}


def _event(payload=None):
    return {
        "command_id": "details-001",
        "command": "get-details",
        "payload": {} if payload is None else payload,
        "targeted": True,
    }


# --- Core 0 dispatch harness ----------------------------------------------


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
from config import split_config  # noqa: E402
from config_manager import ConfigManager  # noqa: E402
from version import MESSAGE_SCHEMA_VERSION  # noqa: E402


def _load_core0_config():
    config = json.loads((ROOT / "tests" / "fixtures" / "config.json").read_text())
    core0_config, _core1_config = split_config(config)
    return core0_config


class FakeWifi:
    """Fixed Wi-Fi state for get-details dispatch tests."""

    connected = True

    def ip_address(self):
        return "192.168.1.50"


class RecordingEventQueue:
    """Event queue that records every admitted command event.

    admit=False simulates a heap-governed rejection (memory pressure)."""

    def __init__(self, admit=True):
        self.events = []
        self.admit = admit

    def put(self, event):
        if not self.admit:
            return False
        self.events.append(event)
        return True

    def take(self):
        if self.events:
            return self.events.pop(0)
        return None


class MockInterCore:
    def __init__(self, admit=True):
        self.state_mailboxes = MagicMock()
        self.outbound_queue = MagicMock()
        self.event_queue = RecordingEventQueue(admit)


@pytest.fixture
def make_core0():
    """Build a fresh Core0 with faked wifi/mqtt and a real config.

    admit=False makes the event queue reject every put (memory pressure)."""

    def _make(admit=True):
        _FAKE_TIME.now_ms = 0
        _install_mocks()
        importlib.reload(importlib.import_module("uptime"))
        core0_mod = importlib.import_module("core0")
        importlib.reload(core0_mod)

        config = _load_core0_config()
        instance = core0_mod.Core0(
            MockInterCore(admit),
            config,
            {"wifi_ssid": "test-ssid", "wifi_password": "test-password"},
            "test-runtime",
            0,
            MagicMock(),
                    ConfigManager(str(ROOT / "tests" / "fixtures" / "config.json")),
        )
        instance._wifi = FakeWifi()
        instance._mqtt = MagicMock()
        return instance

    return _make


def _command(target, command_id, command="get-details", payload={}):
    return {
        "message_type": "command",
        "message_schema_version": MESSAGE_SCHEMA_VERSION,
        "target": target,
        "command_id": command_id,
        "command": command,
        "payload": payload,
    }


def _send(core0, doc):
    """Deliver one command document to Core 0's MQTT receive path."""
    core0._on_mqtt_message(core0._config["mqtt_topic_command"], json.dumps(doc))


def _queued_response(core0):
    assert core0._pending_core0_responses
    return core0._pending_core0_responses[-1]


# --- Core 1 execution: full snapshot, unrestricted by include --------------


def test_get_details_returns_every_system_information_section():
    intercore = InterCore(_event())

    with patch.object(core1, "_message_time", return_value=(1234, "2026-08-30T21:00:00.000Z")):
        response = core1._process_intercore_event(
            intercore,
            object(),
            FullSystemInformation(),
        )

    assert response["kind"] == KIND_COMMAND_RESPONSE
    message = response["message"]
    assert message["message_type"] == "command_response"
    assert message["payload"]["command_id"] == "details-001"
    assert message["payload"]["command"] == "get-details"
    assert message["payload"]["success"] is True

    data = message["payload"]["data"]
    assert tuple(data) == SYSTEM_INFORMATION_SECTIONS
    for section in SYSTEM_INFORMATION_SECTIONS:
        assert data[section] == {"section": section}


def test_system_information_unavailable_is_preserved():
    """If Core 1 cannot provide the snapshot, it reports the existing failure."""
    intercore = InterCore(_event())

    with patch.object(core1, "_message_time", return_value=(1234, None)):
        response = core1._process_intercore_event(
            intercore,
            object(),
            None,
        )

    payload = response["message"]["payload"]
    assert payload["success"] is False
    assert payload["error"] == {
        "code": "system_information_unavailable",
        "message": "System information is unavailable",
    }


# --- Core 0 dispatch: target matching -------------------------------------


def test_dispatch_targets_source_ip_and_broadcast_case_insensitive(make_core0):
    """source (any casing), the current IP, and * all dispatch the event;
    targeted is true for source/IP and false for broadcast."""
    for casing in ("Test-Pico-2", "test-pico-2", "TEST-PICO-2", "teSt-PICO-2"):
        core0 = make_core0()

        _send(core0, _command(casing, "gd-src-{}".format(casing)))

        events = core0._intercore.event_queue.events
        assert len(events) == 1
        assert events[0]["targeted"] is True
        assert core0._pending_core0_responses == []

    core0 = make_core0()
    assert core0._wifi.ip_address() == "192.168.1.50"
    _send(core0, _command("192.168.1.50", "gd-ip"))
    assert len(core0._intercore.event_queue.events) == 1
    assert core0._intercore.event_queue.events[0]["targeted"] is True

    core0 = make_core0()
    _send(core0, _command("*", "gd-bcast"))
    assert len(core0._intercore.event_queue.events) == 1
    assert core0._intercore.event_queue.events[0]["targeted"] is False


def test_dispatch_unrelated_target_is_ignored(make_core0):
    """A command addressed to another device dispatches nothing and answers nothing."""
    core0 = make_core0()

    _send(core0, _command("Test-Pico-3", "gd-other"))

    assert core0._intercore.event_queue.events == []
    assert core0._pending_core0_responses == []
    assert core0._recent_command_ids == []


# --- Core 0 dispatch: payload contract ------------------------------------


def test_dispatch_requires_empty_payload(make_core0):
    """The exact {} payload dispatches the validated bounded event, payload {}."""
    core0 = make_core0()

    _send(core0, _command("Test-Pico-2", "gd-ok", payload={}))

    events = core0._intercore.event_queue.events
    assert len(events) == 1
    assert events[0] == {
        "command_id": "gd-ok",
        "command": "get-details",
        "payload": {},
        "targeted": True,
    }
    assert core0._pending_core0_responses == []


def test_dispatch_unknown_payload_fields_are_returned_sorted(make_core0):
    """Any payload key is unknown for get-details: all are named, sorted, in one error."""
    core0 = make_core0()

    _send(core0, _command(
        "Test-Pico-2",
        "gd-unknown",
        payload={"include": ["memory"], "sections": ["cpu"], "compact": True},
    ))

    assert core0._intercore.event_queue.events == []
    response = _queued_response(core0)
    assert response["command_id"] == "gd-unknown"
    assert response["success"] is False
    assert response["error"] == {
        "code": "unknown_fields",
        "message": "get-details payload contains unknown fields",
        "unknown_fields": ["compact", "include", "sections"],
    }


# --- Core 0 dispatch: duplicate suppression --------------------------------


def test_dispatch_duplicate_command_id_is_suppressed(make_core0):
    """A repeated command_id dispatches exactly one event, before any admission."""
    core0 = make_core0()

    for _ in range(3):
        _send(core0, _command("Test-Pico-2", "gd-dup"))

    events = core0._intercore.event_queue.events
    assert len(events) == 1
    assert events[0]["command_id"] == "gd-dup"
    assert core0._pending_core0_responses == []


# --- Core 0 dispatch: memory pressure -------------------------------------


def test_dispatch_event_queue_memory_pressure_error(make_core0):
    """A heap-governed event-queue rejection is answered with the existing failure."""
    core0 = make_core0(admit=False)

    _send(core0, _command("Test-Pico-2", "gd-mem"))

    assert core0._intercore.event_queue.events == []
    response = _queued_response(core0)
    assert response["command_id"] == "gd-mem"
    assert response["success"] is False
    assert response["error"] == {
        "code": "intercore_event_queue_memory_pressure",
        "message": "Insufficient free heap to queue the Core 1 event",
    }


# --- Core 0 dispatch: unknown command -------------------------------------


def test_unknown_command_is_answered_by_core0_not_core1(make_core0):
    """A command outside the supported registry is answered by Core 0 and never
    dispatched to Core 1, which no longer acts as the generic fallback."""
    core0 = make_core0()

    _send(core0, _command(
        "Test-Pico-2",
        "gd-unsupported",
        command="set-brightness",
        payload={},
    ))

    # Core 0 answers it and dispatches nothing...
    assert core0._intercore.event_queue.events == []
    response = _queued_response(core0)
    assert response["command_id"] == "gd-unsupported"
    assert response["command"] == "set-brightness"
    assert response["success"] is False
    assert response["error"] == {
        "code": "unsupported_command",
        "message": "Unsupported command",
    }

    # ...and Core 1, handed such an event directly, no longer builds a generic
    # unsupported_command response (it is not the fallback anymore).
    intercore = InterCore({
        "command_id": "gd-unsupported",
        "command": "set-brightness",
        "payload": {},
        "targeted": True,
    })
    assert core1._process_intercore_event(
        intercore, object(), FullSystemInformation()
    ) is None


# --- Core 0 dispatch: length bounds ---------------------------------------


def test_overlong_command_name_is_answered_bounded_and_never_echoed(make_core0):
    """An over-long command is answered with a bounded invalid_command error and
    is never echoed into the response (not even partially); the bounded ID
    still claims its debounce entry. Exactly-at-bound is still accepted."""
    import command_protocol
    max_name = command_protocol.MAX_COMMAND_LENGTH

    core0 = make_core0()
    overlong = "x" * (max_name + 1)
    _send(core0, _command(
        "Test-Pico-2", "gd-long-cmd", command=overlong, payload={},
    ))
    assert core0._intercore.event_queue.events == []
    assert core0._recent_command_ids == ["gd-long-cmd"]
    response = _queued_response(core0)
    assert response["command_id"] == "gd-long-cmd"
    assert response["success"] is False
    assert response["error"]["code"] == "invalid_command"
    # The over-long name is never reproduced in the response...
    assert overlong not in json.dumps(response)
    # ...not even a prefix of it.
    assert response.get("command") != overlong[:max_name]

    # Exactly-at-bound is still a valid (here: unsupported) command name.
    core0 = make_core0()
    _send(core0, _command("Test-Pico-2", "gd-bound", command="c" * max_name, payload={}))
    response = _queued_response(core0)
    assert response["error"]["code"] == "unsupported_command"
    assert response["command"] == "c" * max_name


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
