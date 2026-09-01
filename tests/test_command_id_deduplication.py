# test_command_id_deduplication.py - Core 0 duplicate command_id suppression
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""
Host-side tests for Core 0's duplicate command_id suppression.

Core 0 owns command ingress end to end, and that is where duplicate suppression belongs: it protects Core 0 commands (reboot) and Core 1 commands (get-details) alike. The device retains the _RECENT_COMMAND_ID_CAPACITY most recently accepted command IDs in a RAM-only FIFO and ignores any command whose ID is still present; a duplicate receipt re-executes nothing, queues no response, changes no pending reboot, and does not refresh the entry's FIFO position.
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
        # Anything not explicitly faked falls through to the real time
        # module so host tooling keeps working.
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
    config = json.loads((ROOT / "config.json").read_text())
    core0_config, _core1_config = split_config(config)
    return core0_config


class FakeWifi:
    """Fixed Wi-Fi state for deduplication tests."""

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


def _command(target, command_id, command="get-details", payload=None):
    return {
        "message_type": "command",
        "message_schema_version": MESSAGE_SCHEMA_VERSION,
        "target": target,
        "command_id": command_id,
        "command": command,
        "payload": payload if payload is not None else {},
    }


def _send(core0, doc):
    """Deliver one command document to Core 0's MQTT receive path."""
    core0._on_mqtt_message(core0._config["mqtt_topic_command"], json.dumps(doc))


# --- One-shot request identity ---------------------------------------------


def test_repeated_identical_command_is_forwarded_once(make_core0):
    """The same valid command sent repeatedly results in exactly one execution."""
    core0 = make_core0()
    command_id = "9f8c87fd7cb1680a502782425a5415e5"

    for _ in range(12):
        _send(core0, _command("Test-Pico-2", command_id))

    events = core0._intercore.event_queue.events
    assert len(events) == 1
    assert events[0]["command_id"] == command_id
    assert events[0]["command"] == "get-details"
    # Duplicates are silently ignored: no responses, no pending state.
    assert core0._pending_core0_responses == []
    assert core0._pending_reboot is None


def test_distinct_command_ids_are_all_accepted(make_core0):
    """Otherwise identical commands with distinct IDs are all processed."""
    core0 = make_core0()

    for i in range(5):
        _send(core0, _command("Test-Pico-2", "distinct-{}".format(i)))

    events = core0._intercore.event_queue.events
    assert len(events) == 5
    assert [event["command_id"] for event in events] == [
        "distinct-{}".format(i) for i in range(5)
    ]


def test_command_ids_are_case_sensitive(make_core0):
    """Command IDs differ by case: "Cmd-01" and "cmd-01" are distinct IDs,
    so neither debounces the other (matching and caching are exact-string)."""
    core0 = make_core0()

    _send(core0, _command("Test-Pico-2", "Cmd-Case-01"))
    _send(core0, _command("Test-Pico-2", "cmd-case-01"))

    events = core0._intercore.event_queue.events
    assert [event["command_id"] for event in events] == ["Cmd-Case-01", "cmd-case-01"]
    assert core0._recent_command_ids == ["Cmd-Case-01", "cmd-case-01"]


def test_same_id_with_different_payload_is_still_duplicate(make_core0):
    """command_id is the debounce key; the payload is not part of it."""
    core0 = make_core0()

    _send(core0, _command("Test-Pico-2", "dup-payload", payload={}))
    _send(core0, _command("Test-Pico-2", "dup-payload", payload={"option": "changed"}))

    events = core0._intercore.event_queue.events
    assert len(events) == 1
    assert events[0]["payload"] == {}
    assert core0._pending_core0_responses == []


def test_same_id_with_different_command_is_still_duplicate(make_core0):
    """The first accepted use of an ID owns it: a different command reusing
    the ID is ignored, even a Core 0 command like reboot."""
    core0 = make_core0()

    _send(core0, _command("Test-Pico-2", "dup-name", command="get-details"))
    _send(core0, _command("Test-Pico-2", "dup-name", command="reboot", payload={}))

    assert len(core0._intercore.event_queue.events) == 1
    assert core0._pending_reboot is None
    assert core0._pending_core0_responses == []


# --- Reboot -----------------------------------------------------------------


def test_repeated_reboot_is_processed_once(make_core0):
    """Only the first reboot request establishes the pending reboot; duplicate
    copies queue no response and do not mutate the pending request."""
    core0 = make_core0()

    for _ in range(5):
        _send(core0, _command("Test-Pico-2", "reboot-001", command="reboot", payload={}))

    assert core0._pending_reboot == {
        "command_id": "reboot-001",
        "command": "reboot",
        "targeted": True,
    }
    assert core0._pending_core0_responses == []
    assert core0._intercore.event_queue.events == []


# --- Cache hygiene ----------------------------------------------------------


def test_command_for_another_device_does_not_consume_cache(make_core0):
    """A non-matching target is dropped before the identity check, so the ID
    remains available to this device."""
    core0 = make_core0()

    _send(core0, _command("Test-Pico-3", "shared-id"))
    assert core0._recent_command_ids == []
    assert core0._intercore.event_queue.events == []

    _send(core0, _command("Test-Pico-2", "shared-id"))

    events = core0._intercore.event_queue.events
    assert len(events) == 1
    assert events[0]["command_id"] == "shared-id"


def test_fifo_capacity_is_enforced(make_core0):
    """Exactly the capacity of distinct IDs is retained; the oldest is evicted
    first, and an evicted ID can be accepted again."""
    core0 = make_core0()
    capacity = importlib.import_module("core0")._RECENT_COMMAND_ID_CAPACITY
    assert capacity == 16

    for i in range(capacity):
        _send(core0, _command("Test-Pico-2", "fill-{:02d}".format(i)))
    assert len(core0._recent_command_ids) == capacity

    # The oldest ID is still recent while it is present.
    _send(core0, _command("Test-Pico-2", "fill-00"))
    assert len(core0._intercore.event_queue.events) == capacity

    # One more distinct ID evicts the oldest.
    _send(core0, _command("Test-Pico-2", "fill-{:02d}".format(capacity)))
    assert core0._recent_command_ids[0] == "fill-01"
    assert "fill-00" not in core0._recent_command_ids

    # The evicted ID is accepted again.
    _send(core0, _command("Test-Pico-2", "fill-00"))
    events = core0._intercore.event_queue.events
    assert len(events) == capacity + 2
    assert events[-1]["command_id"] == "fill-00"


def test_duplicate_receipt_does_not_refresh_fifo_position(make_core0):
    """A duplicate receipt does not move the ID toward the newest end: the
    cache is the last accepted distinct IDs, not an LRU access order."""
    core0 = make_core0()
    capacity = importlib.import_module("core0")._RECENT_COMMAND_ID_CAPACITY

    for i in range(capacity):
        _send(core0, _command("Test-Pico-2", "pos-{:02d}".format(i)))

    # Duplicate the oldest while it is still cached: ignored, position kept.
    _send(core0, _command("Test-Pico-2", "pos-00"))
    assert core0._intercore.event_queue.events[0]["command_id"] == "pos-00"

    # The next distinct ID evicts pos-00 -- still the oldest. An LRU
    # implementation would have refreshed pos-00 and evicted pos-01 instead.
    _send(core0, _command("Test-Pico-2", "pos-{:02d}".format(capacity)))
    assert core0._recent_command_ids[0] == "pos-01"
    assert "pos-00" not in core0._recent_command_ids

    # Evicted, so it is accepted again.
    _send(core0, _command("Test-Pico-2", "pos-00"))
    assert core0._intercore.event_queue.events[-1]["command_id"] == "pos-00"


def test_invalid_command_identity_does_not_enter_cache(make_core0):
    """A command_id that is missing, empty, or over-long is dropped before the
    debounce stage: no response (a standard response requires a bounded ID)
    and no cache entry."""
    core0 = make_core0()

    missing = _command("Test-Pico-2", "n/a")
    del missing["command_id"]
    core0._on_mqtt_message(core0._config["mqtt_topic_command"], json.dumps(missing))

    _send(core0, _command("Test-Pico-2", ""))

    import command_protocol
    _send(core0, _command("Test-Pico-2", "y" * (command_protocol.MAX_COMMAND_ID_LENGTH + 1)))

    assert core0._recent_command_ids == []
    assert core0._intercore.event_queue.events == []
    assert core0._pending_core0_responses == []

    # The device keeps accepting normally-sized traffic afterwards.
    _send(core0, _command("Test-Pico-2", "fresh-id"))
    assert len(core0._intercore.event_queue.events) == 1


def test_invalid_command_claims_its_bounded_id(make_core0):
    """A bounded command_id claims a debounce entry BEFORE deeper validation:
    a malformed command (non-string) is answered with a bounded error once,
    and the first response is owed by the claimed ID."""
    core0 = make_core0()

    non_string = _command("Test-Pico-2", "n/a")
    non_string["command"] = 42
    core0._on_mqtt_message(core0._config["mqtt_topic_command"], json.dumps(non_string))

    assert core0._recent_command_ids == ["n/a"]
    assert core0._intercore.event_queue.events == []
    response = core0._pending_core0_responses[-1]
    assert response["command_id"] == "n/a"
    assert response["success"] is False
    assert response["error"]["code"] == "invalid_command"
    # The non-string name is never echoed into the response.
    assert "command" not in response


# --- Target matching --------------------------------------------------------


def test_target_matching_unchanged_and_dedup_applies_after_it(make_core0):
    """Case-insensitive target matching is unchanged; dedup applies only after
    the target is known to match this device."""
    for casing in ("Test-Pico-2", "test-pico-2", "TEST-PICO-2", "teSt-PICO-2"):
        core0 = make_core0()

        _send(core0, _command(casing, "case-001"))
        events = core0._intercore.event_queue.events
        assert len(events) == 1
        assert events[0]["targeted"] is True
        assert events[0]["command_id"] == "case-001"

        # A second copy for the same device with the same ID is suppressed.
        _send(core0, _command(casing, "case-001"))
        assert len(core0._intercore.event_queue.events) == 1
        assert core0._pending_core0_responses == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
