# test_command_target_matching.py - Case-insensitive command target matching
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

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
import core1  # noqa: E402


def _load_core0_config():
    config = json.loads((ROOT / "tests" / "fixtures" / "config.json").read_text())
    core0_config, _core1_config= split_config(config)
    return core0_config


class FakeWifi:
    """Fixed Wi-Fi state for target-matching tests."""

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


class FullSystemInformation:
    def __getattr__(self, name):
        if not name.startswith("get_"):
            raise AttributeError(name)
        section = name[4:]
        from system_information import SYSTEM_INFORMATION_SECTIONS

        if section not in SYSTEM_INFORMATION_SECTIONS:
            raise AttributeError(name)
        return lambda: {"section": section}


@pytest.fixture
def make_core0():
    """Build a fresh Core0 with faked wifi/mqtt and a real config."""

    def _make():
        _FAKE_TIME.now_ms = 0
        _install_mocks()
        importlib.reload(importlib.import_module("uptime"))
        importlib.reload(importlib.import_module("network_wait"))
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
                    ConfigManager(str(ROOT / "tests" / "fixtures" / "config.json")),
        )
        instance._wifi = FakeWifi()
        instance._mqtt = MagicMock()
        return instance

    return _make


def _command(target):
    return {
        "message_type": "command",
        "message_schema_version": MESSAGE_SCHEMA_VERSION,
        "target": target,
        "command_id": "details-001",
        "command": "get-details",
        "payload": {},
    }


# --- Focused target-matching behavior -------------------------------------

def test_target_matches_case_insensitive_variants(make_core0):
    """Casing variants of the configured source all address this device."""
    core0 = make_core0()
    assert core0._config["source"] == "Test-Pico-2"

    assert core0._target_matches("Test-Pico-2")
    assert core0._target_matches("test-pico-2")
    assert core0._target_matches("TEST-PICO-2")
    assert core0._target_matches("teSt-PICO-2")


def test_target_mismatch_still_rejected(make_core0):
    """A different device name must not match, in any casing."""
    core0 = make_core0()

    assert not core0._target_matches("Test-Pico-3")
    assert not core0._target_matches("test-pico-3")
    assert not core0._target_matches("TEST-PICO-3")


def test_broadcast_target_still_matches(make_core0):
    core0 = make_core0()
    assert core0._target_matches("*")


def test_ip_target_still_matches(make_core0):
    """The existing IP-address targeting keeps working."""
    core0 = make_core0()
    assert core0._target_matches("192.168.1.50")


def test_non_string_target_does_not_match_or_raise(make_core0):
    """Non-string targets fail to match without raising (existing behavior)."""
    core0 = make_core0()

    for bad_target in (123, None, ["Test-Pico-2"], b"Test-Pico-2"):
        assert not core0._target_matches(bad_target)


# --- End-to-end command routing --------------------------------------------

def test_mixed_case_target_routes_command_and_preserves_source(make_core0):
    """A casing variant of the configured source is routed like an exact
    match, and the response envelope keeps the configured casing."""
    core0 = make_core0()
    assert core0._config["source"] == "Test-Pico-2"

    core0._on_mqtt_message(
        core0._config["mqtt_topic_command"],
        json.dumps(_command("teSt-PICO-2")),
    )

    # Core 0 accepted the command as explicitly targeted and routed it to
    # Core 1 instead of rejecting it with a Core 0 response.
    events = core0._intercore.event_queue.events
    assert len(events) == 1
    event = events[0]
    assert event["command"] == "get-details"
    assert event["command_id"] == "details-001"
    assert event["targeted"] is True
    assert event["payload"] == {}
    assert core0._pending_core0_responses == []

    # Core 1 executes the routed command and reports it targeted + successful.
    with patch.object(core1, "_message_time", return_value=(1234, None)):
        response = core1._process_intercore_event(
            core0._intercore,
            object(),
            FullSystemInformation(),
        )

    payload = response["message"]["payload"]
    assert payload["command_id"] == "details-001"
    assert payload["command"] == "get-details"
    assert payload["targeted"] is True
    assert payload["success"] is True

    # The published envelope carries the configured source casing verbatim.
    fragment = core0._envelope_fragment(1).decode("utf-8")
    envelope = json.loads("{" + fragment + "}")
    assert envelope["source"] == "Test-Pico-2"


def test_exact_case_target_still_routes(make_core0):
    core0 = make_core0()

    core0._on_mqtt_message(
        core0._config["mqtt_topic_command"],
        json.dumps(_command("Test-Pico-2")),
    )

    assert len(core0._intercore.event_queue.events) == 1
    assert core0._intercore.event_queue.events[0]["targeted"] is True


def test_different_target_still_ignored(make_core0):
    """A different device name is still ignored: no routing, no response."""
    core0 = make_core0()

    core0._on_mqtt_message(
        core0._config["mqtt_topic_command"],
        json.dumps(_command("Test-Pico-3")),
    )

    assert core0._intercore.event_queue.events == []
    assert core0._pending_core0_responses == []


def test_broadcast_target_still_routes_as_non_targeted(make_core0):
    """Broadcast commands route to Core 1 as before, marked non-targeted."""
    core0 = make_core0()

    core0._on_mqtt_message(
        core0._config["mqtt_topic_command"],
        json.dumps(_command("*")),
    )

    assert len(core0._intercore.event_queue.events) == 1
    assert core0._intercore.event_queue.events[0]["targeted"] is False
