# test_reboot_command.py - Core 0 reboot command contract
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""
Host-side tests for the reboot command under the strict command protocol contract.

Core 0 owns the reboot command and machine.reset() end to end: it is validated and executed on Core 0 and never crosses to Core 1. The shared protocol validation (message_type, schema version, case-insensitive target, command/command_id identity, command-ID debounce, payload-is-a-dict) runs first; then the reboot command's own contract applies -- the payload is exactly {} (any key is an unknown field), a single pending reboot is retained, and the success acknowledgement is published before the reset.
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


class _MachineReset(Exception):
    """Stand-in for machine.reset(): on hardware it never returns."""


class ResettingMachine:
    """Records the reset() call and, like real hardware, never returns."""

    def __init__(self):
        self.reset_calls = 0
        self.events = []

    def reset(self):
        self.reset_calls += 1
        self.events.append("reset")
        raise _MachineReset()


class RecordingMqtt:
    """Connected MQTT session that records each publish (topic, message)."""

    def __init__(self, events):
        self.events = events
        self.published = []

    def is_connected(self):
        return True

    def publish_qos1(self, topic, message):
        self.events.append("publish")
        self.published.append((topic, message))


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
        # Anything not explicitly faked falls through to the real time
        # module so host tooling keeps working.
        return getattr(_real_time, name)


_FAKE_TIME = FakeTime()
_MACHINE = ResettingMachine()
_DEBUG_MOCK = MagicMock()
_DEBUG_MOCK.DEBUG = False
_WIFI_MOCK = MagicMock()
_MQTT_MOCK = MagicMock()


def _install_mocks():
    sys.modules["time"] = _FAKE_TIME
    sys.modules["machine"] = _MACHINE
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
    config = json.loads((ROOT / "config.json").read_text())
    core0_config, _core1_config = split_config(config)
    return core0_config


class FakeWifi:
    """Fixed Wi-Fi state for reboot-command tests."""

    connected = True

    def ip_address(self):
        return "192.168.1.50"


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


class MockInterCore:
    def __init__(self):
        self.state_mailboxes = MagicMock()
        self.outbound_queue = MagicMock()
        self.event_queue = RecordingEventQueue()


@pytest.fixture
def make_core0():
    """Build a fresh Core0 with faked wifi/mqtt and a real config."""

    def _make():
        _FAKE_TIME.now_ms = 0
        _MACHINE.reset_calls = 0
        _MACHINE.events = []
        _install_mocks()
        importlib.reload(importlib.import_module("uptime"))
        core0_mod = importlib.import_module("core0")
        importlib.reload(core0_mod)

        config = _load_core0_config()
        instance = core0_mod.Core0(
            MockInterCore(),
            config,
            {"wifi_ssid": "test-ssid", "wifi_password": "test-password"},
            "test-runtime",
            0,
            MagicMock(),
                    ConfigManager("config.json"),
        )
        instance._wifi = FakeWifi()
        instance._mqtt = MagicMock()
        return instance

    return _make


def _command(target, command_id, payload={}):
    return {
        "message_type": "command",
        "message_schema_version": MESSAGE_SCHEMA_VERSION,
        "target": target,
        "command_id": command_id,
        "command": "reboot",
        "payload": payload,
    }


def _send(core0, doc):
    """Deliver one command document to Core 0's MQTT receive path."""
    core0._on_mqtt_message(core0._config["mqtt_topic_command"], json.dumps(doc))


def _queued_response(core0):
    assert core0._pending_core0_responses
    return core0._pending_core0_responses[-1]


# --- Targeting --------------------------------------------------------------


def test_source_target_any_casing_is_accepted(make_core0):
    """Casing variants of the configured source all establish a pending reboot."""
    for casing in ("Test-Pico-2", "test-pico-2", "TEST-PICO-2", "teSt-PICO-2"):
        core0 = make_core0()

        _send(core0, _command(casing, "rb-{}".format(casing)))

        assert core0._pending_reboot == {
            "command_id": "rb-{}".format(casing),
            "command": "reboot",
            "targeted": True,
        }
        assert core0._pending_core0_responses == []


def test_ip_target_is_accepted(make_core0):
    """The device's current IP address targets it, marked as a targeted command."""
    core0 = make_core0()
    assert core0._wifi.ip_address() == "192.168.1.50"

    _send(core0, _command("192.168.1.50", "rb-ip"))

    assert core0._pending_reboot == {
        "command_id": "rb-ip",
        "command": "reboot",
        "targeted": True,
    }
    assert core0._pending_core0_responses == []


def test_broadcast_target_is_accepted_non_targeted(make_core0):
    """The * broadcast targets this device, marked as a non-targeted command."""
    core0 = make_core0()

    _send(core0, _command("*", "rb-bcast"))

    assert core0._pending_reboot == {
        "command_id": "rb-bcast",
        "command": "reboot",
        "targeted": False,
    }
    assert core0._pending_core0_responses == []


def test_unrelated_target_is_ignored(make_core0):
    """A command addressed to another device does not touch this device's state."""
    core0 = make_core0()

    _send(core0, _command("Test-Pico-3", "rb-other"))

    assert core0._pending_reboot is None
    assert core0._pending_core0_responses == []
    assert core0._recent_command_ids == []


# --- Payload contract -------------------------------------------------------


def test_exact_empty_payload_succeeds(make_core0):
    """The exact {} payload is the only valid one: it arms the pending reboot."""
    core0 = make_core0()

    _send(core0, _command("Test-Pico-2", "rb-ok", payload={}))

    assert core0._pending_reboot == {
        "command_id": "rb-ok",
        "command": "reboot",
        "targeted": True,
    }
    assert core0._pending_core0_responses == []


def test_unknown_payload_fields_are_returned_sorted(make_core0):
    """Any payload key is unknown for reboot: all are named, sorted, in the error."""
    core0 = make_core0()

    _send(core0, _command(
        "Test-Pico-2",
        "rb-unknown",
        payload={"force": True, "delay_sec": 5, "verbose": False},
    ))

    assert core0._pending_reboot is None
    response = _queued_response(core0)
    assert response["command_id"] == "rb-unknown"
    assert response["success"] is False
    assert response["error"] == {
        "code": "unknown_fields",
        "message": "reboot payload contains unknown fields",
        "unknown_fields": ["delay_sec", "force", "verbose"],
    }


def test_non_object_payload_is_rejected(make_core0):
    """A payload that is not an object is a bounded invalid_payload failure."""
    for bad in (["not", "an", "object"], "a-string", 42, None, True):
        core0 = make_core0()

        _send(core0, _command("Test-Pico-2", "rb-nobj", payload=bad))

        assert core0._pending_reboot is None
        response = _queued_response(core0)
        assert response["success"] is False
        assert response["error"]["code"] == "invalid_payload"


def test_missing_payload_is_rejected(make_core0):
    """A command without a payload is a bounded invalid_payload failure."""
    core0 = make_core0()

    doc = _command("Test-Pico-2", "rb-missing")
    del doc["payload"]
    _send(core0, doc)

    assert core0._pending_reboot is None
    response = _queued_response(core0)
    assert response["success"] is False
    assert response["error"]["code"] == "invalid_payload"


# --- Debounce and pending ---------------------------------------------------


def test_duplicate_command_id_is_ignored(make_core0):
    """A repeated command_id is silently ignored: no second pending change, no response."""
    core0 = make_core0()

    _send(core0, _command("Test-Pico-2", "rb-dup"))
    first = dict(core0._pending_reboot)

    _send(core0, _command("Test-Pico-2", "rb-dup"))

    assert core0._pending_reboot == first
    assert core0._pending_core0_responses == []


def test_distinct_second_reboot_while_pending_is_rejected(make_core0):
    """A distinct valid reboot arriving while one is pending is rejected; the original stays pending."""
    core0 = make_core0()

    _send(core0, _command("Test-Pico-2", "rb-first"))
    _send(core0, _command("Test-Pico-2", "rb-second"))

    # The first request is still the pending one, unchanged...
    assert core0._pending_reboot == {
        "command_id": "rb-first",
        "command": "reboot",
        "targeted": True,
    }
    # ...and the distinct second one got the already-pending error.
    response = _queued_response(core0)
    assert response["command_id"] == "rb-second"
    assert response["success"] is False
    assert response["error"] == {
        "code": "reboot_already_pending",
        "message": "A reboot is already pending",
    }


# --- Execution: response before reset ---------------------------------------


def test_success_response_is_published_before_reset(make_core0):
    """The reboot acks with success before it resets, and never resets on a failed ack.

    The response is published first (with data.rebooting == True), only then does the reset run; a publish that fails keeps the reboot pending and skips the reset."""
    core0 = make_core0()
    core0._pending_reboot = {
        "command_id": "rb-order",
        "command": "reboot",
        "targeted": True,
    }

    recording = RecordingMqtt(_MACHINE.events)
    core0._mqtt = recording
    core0._intercore.outbound_queue.has_in_flight = lambda: False

    with pytest.raises(_MachineReset):
        core0._perform_reboot()

    # The acknowledgement went out before the reset...
    assert _MACHINE.events == ["publish", "reset"]
    assert _MACHINE.reset_calls == 1
    # ...and it is the success shape the protocol preserves.
    topic, message = recording.published[0]
    doc = json.loads(message)
    assert doc["message_type"] == "command_response"
    assert doc["payload"]["command_id"] == "rb-order"
    assert doc["payload"]["command"] == "reboot"
    assert doc["payload"]["success"] is True
    assert doc["payload"]["data"] == {"rebooting": True}
    # The pending flag is cleared once the acknowledgement has been published.
    assert core0._pending_reboot is None


def test_failed_ack_keeps_reboot_pending_and_skips_reset(make_core0):
    """If the success acknowledgement cannot be published, no reset happens and the reboot stays pending for a later pass."""
    core0 = make_core0()
    core0._pending_reboot = {
        "command_id": "rb-fail",
        "command": "reboot",
        "targeted": True,
    }
    core0._intercore.outbound_queue.has_in_flight = lambda: False

    def _exhaust(*args, **kwargs):
        raise RuntimeError("serialization failed")

    import core0 as core0_mod
    original = core0_mod.serialize_and_validate_message
    core0_mod.serialize_and_validate_message = _exhaust
    try:
        assert core0._perform_reboot() is False
    finally:
        core0_mod.serialize_and_validate_message = original

    assert core0._pending_reboot is not None  # still pending, not consumed
    assert _MACHINE.reset_calls == 0
    assert _MACHINE.events == []


# --- RAM-only state: a restart is a clean slate -----------------------------


def test_reboot_state_is_ram_only_and_cleared_by_restart(make_core0):
    """The pending flag and the debounce cache are RAM-only: a restart (a fresh process) starts clean, with no persistent flag to carry over."""
    core0 = make_core0()

    _send(core0, _command("Test-Pico-2", "rb-persist"))
    assert core0._pending_reboot is not None
    assert core0._recent_command_ids  # the accepted ID is retained in RAM

    # A restart is a fresh Core 0: no pending reboot, no retained IDs, and no
    # persistent needs-reboot flag is introduced to survive the reset.
    fresh = make_core0()
    assert fresh._pending_reboot is None
    assert fresh._recent_command_ids == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
