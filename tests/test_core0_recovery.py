# test_core0_recovery.py - Tests for Core 0 network recovery and startup dedup
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import importlib
import json
import pathlib
import sys
import time as _real_time
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

ROOT = pathlib.Path(__file__).resolve().parents[1]


class FakeTime:
    """Controllable stand-in for MicroPython's time module.

    sleep_ms advances the clock so bounded wait loops terminate
    deterministically in tests.
    """

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
# time: later-collected modules (e.g. tests/test_mqtt.py) import the real
# wifi/mqtt modules and the real time module at collection, and mocked
# entries in sys.modules would shadow them. They are installed inside the
# fixture below, which also imports/reloads core0 there. core0 binds
# wifi/mqtt/time from sys.modules at import time, so the reload is what
# makes this module's fakes authoritative at test time, regardless of which
# test module imported core0 first.
from config import split_config  # noqa: E402
from message_protocol import format_utc_epoch_ms  # noqa: E402
from version import MESSAGE_SCHEMA_VERSION  # noqa: E402


def _load_core0_config():
    config = json.loads((ROOT / "config.json").read_text())
    core0_config, _core1_config, _bus_config = split_config(config)
    return core0_config


class FakeLed:
    """Records every set_connecting() call in order."""

    def __init__(self):
        self.states = []

    def set_connecting(self, value):
        self.states.append(bool(value))

    def telemetry_sent(self):
        pass


class FakeWifi:
    """Scripts a successful connect and records calls."""

    def __init__(self):
        self.connected = False
        self.connect_calls = 0

    def is_connected(self):
        return self.connected

    def connect(self):
        self.connected = True
        self.connect_calls += 1
        return True

    def snapshot(self, mqtt_connected):
        return {
            "ssid": "test-ssid",
            "ip_address": "192.168.1.100",
            "rssi": -50,
            "wifi_connect_count": self.connect_calls,
        }


class FakeMqtt:
    """Scripts successful connect/probe and echoes a UTC info_response."""

    def __init__(self, core0_instance):
        self.core0 = core0_instance
        self.connected = False
        self.connect_calls = 0
        self.mark_disconnected_calls = 0
        self.published = []
        self._last_info_request = None
        self._utc_deliver = True
        # Number of initial startup probe publishes that fail before
        # succeeding. 0 (default) keeps the existing always-success behavior.
        self.fail_probes_times = 0
        self._probe_publishes = 0

    def is_connected(self):
        return self.connected

    def connect(self):
        self.connected = True
        self.connect_calls += 1
        return True

    def mark_disconnected(self):
        self.mark_disconnected_calls += 1
        self.connected = False

    def status(self):
        return {
            "connected": self.connected,
            "connect_count": self.connect_calls,
            "disconnect_count": 0,
        }

    def get_next_packet_id(self):
        return 1

    def publish_qos1(self, topic, message):
        self.published.append((topic, message))
        doc = json.loads(message)
        if doc.get("message_type") == "info_request":
            self._last_info_request = doc

    def publish_qos1_with_packet_id(self, topic, message, packet_id, timeout_ms=None):
        # Simulate the startup probes: fail the first fail_probes_times
        # publishes (dropping the session, as the real client does on a failed
        # QoS 1 publish), then deliver a matching PUBACK.
        self._probe_publishes += 1
        if self._probe_publishes <= self.fail_probes_times:
            self.mark_disconnected()
            return False
        return True

    def check_msg(self):
        # Deliver a valid UTC answer to the most recent request, the way the
        # broker would, so the startup UTC wait terminates deterministically.
        if self._utc_deliver and self._last_info_request is not None:
            self._utc_deliver = False
            request = self._last_info_request
            utc_epoch_ms = 1750000000000
            response = {
                "message_type": "info_response",
                "message_schema_version": MESSAGE_SCHEMA_VERSION,
                "source": "server",
                "target": self.core0._config["source"],
                "request_type": "utc_time",
                "request_id": request["request_id"],
                "payload": {
                    "timestamp": format_utc_epoch_ms(utc_epoch_ms),
                    "utc_epoch_ms": utc_epoch_ms,
                },
            }
            self.core0._on_mqtt_message(
                self.core0._config["mqtt_topic_info_response"],
                json.dumps(response),
            )


class RecordingMailboxes:
    """State mailboxes that record every published snapshot."""

    def __init__(self):
        self.network_snapshots = []
        self.utc_snapshots = []

    def set_network_snapshot(self, snapshot):
        self.network_snapshots.append(snapshot)

    def set_utc_snapshot(self, snapshot):
        self.utc_snapshots.append(snapshot)

    def get_utc_snapshot(self):
        if self.utc_snapshots:
            return self.utc_snapshots[-1]
        return None


class MockInterCore:
    def __init__(self):
        self.state_mailboxes = RecordingMailboxes()
        self.outbound_queue = MagicMock()
        self.event_queue = MagicMock()


@pytest.fixture
def make_core0():
    """Build a fresh Core0 with faked wifi/mqtt/LED and a controllable clock."""

    def _make():
        _FAKE_TIME.now_ms = 0
        _install_mocks()
        # core0 imports uptime; rebind its time to the fake before reloading core0.
        importlib.reload(importlib.import_module("uptime"))
        core0_mod = importlib.import_module("core0")
        importlib.reload(core0_mod)

        led = FakeLed()
        instance = core0_mod.Core0(
            MockInterCore(),
            _load_core0_config(),
            {"wifi_ssid": "test-ssid", "wifi_password": "test-password"},
            "test-runtime",
            0,
            led,
        )
        instance._wifi = FakeWifi()
        instance._mqtt = FakeMqtt(instance)
        return instance

    return _make


def test_recovery_after_wifi_drop_reconnects_stops_led_and_restores_ready(make_core0):
    """A Wi-Fi outage must be re-established, stop the LED, and re-flag ready."""
    instance = make_core0()
    wifi, mqtt, led = instance._wifi, instance._mqtt, instance._led_manager

    # Steady state after start(), then a Wi-Fi outage (MQTT session is stale).
    instance._network_stack_ready = True
    wifi.connected = True
    mqtt.connected = True
    wifi.connected = False

    instance._recover_network_if_needed()

    assert wifi.connected is True
    assert mqtt.connected is True
    assert mqtt.mark_disconnected_calls == 1
    # LED flashed while re-establishing and stopped once recovery completed.
    assert led.states == [True, False]
    # Readiness is cleared for the outage and restored after recovery.
    assert instance._network_stack_ready is True
    snapshots = instance._intercore.state_mailboxes.network_snapshots
    assert snapshots[0]["network_stack_ready"] is False
    assert snapshots[-1]["network_stack_ready"] is True


def test_recovery_after_mqtt_drop_reconnects_stops_led_and_restores_ready(make_core0):
    """An MQTT-only outage re-establishes MQTT without touching Wi-Fi."""
    instance = make_core0()
    wifi, mqtt, led = instance._wifi, instance._mqtt, instance._led_manager

    instance._network_stack_ready = True
    wifi.connected = True
    mqtt.connected = False

    instance._recover_network_if_needed()

    assert mqtt.connected is True
    assert wifi.connect_calls == 0
    assert mqtt.mark_disconnected_calls == 0
    assert led.states == [True, False]
    assert instance._network_stack_ready is True
    snapshots = instance._intercore.state_mailboxes.network_snapshots
    assert snapshots[0]["network_stack_ready"] is False
    assert snapshots[-1]["network_stack_ready"] is True


def test_recovery_is_noop_when_connected(make_core0):
    """A healthy link must not re-establish, flash the LED, or publish."""
    instance = make_core0()

    instance._network_stack_ready = True
    instance._wifi.connected = True
    instance._mqtt.connected = True

    instance._recover_network_if_needed()

    assert instance._wifi.connect_calls == 0
    assert instance._mqtt.connect_calls == 0
    assert instance._led_manager.states == []
    assert instance._intercore.state_mailboxes.network_snapshots == []
    assert instance._network_stack_ready is True


def test_start_delegates_to_establish_network_and_stops_led(make_core0):
    """start() must reuse establish_network() and finish with the LED off."""
    instance = make_core0()
    calls = {"count": 0}
    original = instance.establish_network

    def spy():
        calls["count"] += 1
        return original()

    instance.establish_network = spy
    instance.start()

    assert calls["count"] == 1
    assert instance._led_manager.states == [True, True, False]
    assert instance._network_stack_ready is True
    # start() must not reintroduce per-connection bookkeeping state.
    assert not hasattr(instance, "_wifi_connected")
    assert not hasattr(instance, "_mqtt_connected")
    # The startup UTC snapshot reached the state mailboxes.
    assert instance._intercore.state_mailboxes.utc_snapshots


def test_start_recovers_from_transient_probe_failure(make_core0):
    """A transient probe failure must be re-established and retried, not fatal.

    Previously a single failed probe raised and halted startup (Core 1 never
    started until a reset). Now the network is re-established and the whole
    verification pass retried; start() returns once a clean pass succeeds and
    Core 1 gating (network_stack_ready, UTC snapshot) still holds.
    """
    instance = make_core0()
    mqtt = instance._mqtt
    mqtt.fail_probes_times = 1  # first probe publish fails, then succeeds

    establish_calls = {"count": 0}
    original = instance.establish_network

    def spy():
        establish_calls["count"] += 1
        return original()

    instance.establish_network = spy

    # Must self-heal and complete rather than raise.
    instance.start()

    assert instance._network_stack_ready is True
    # One establish_network() for initial connect, one more for recovery.
    assert establish_calls["count"] == 2
    # The failed probe dropped the session; recovery left it connected.
    assert mqtt.connected is True
    # Core 1 gating: the startup UTC snapshot reached the state mailboxes.
    assert instance._intercore.state_mailboxes.utc_snapshots


def test_publish_utc_snapshot_has_no_force_argument(make_core0):
    """_publish_utc_snapshot takes no force parameter (it has no throttle)."""
    instance = make_core0()
    snapshot = {
        "timestamp": "2025-06-15T15:06:40Z",
        "utc_epoch_ms": 1750000000000,
        "ticks_ms": 0,
    }
    instance._utc_snapshot = snapshot

    instance._publish_utc_snapshot()

    assert instance._intercore.state_mailboxes.utc_snapshots[-1] is snapshot
    with pytest.raises(TypeError):
        instance._publish_utc_snapshot(force=True)


def _real_outbound_queue(instance):
    """Swap the fixture's mock queue for the real bounded queue."""
    from intercore import OutboundQueue

    instance._intercore.outbound_queue = OutboundQueue(16)
    return instance._intercore.outbound_queue


def test_publish_entry_splices_core0_envelope_and_keeps_body_intact(make_core0):
    """The wire frame is the queued message with Core 0's envelope spliced in.

    Core 0 must not decode, parse, or re-serialize the payload: the message
    body must appear in the published frame exactly as the sender queued it,
    and the five envelope members must be Core 0's own values (its runtime_id
    and configured source, not anything a sender might have embedded).
    """
    from intercore import KIND_HEALTH, RETENTION_PRIORITY_HEALTH
    from message_serializer import serialize_and_validate_message
    from version import FIRMWARE_VERSION

    instance = make_core0()
    queue = _real_outbound_queue(instance)
    message = {
        "message_type": "health",
        "uptime_ms": 1234,
        "timestamp": None,
        "payload": {"status": "healthy", "degraded_reasons": []},
    }
    assert queue.put_with_kind(
        KIND_HEALTH, serialize_and_validate_message(message), RETENTION_PRIORITY_HEALTH
    )

    instance._publish_entry(queue.take())

    topic, frame = instance._mqtt.published[-1]
    assert topic == instance._config["mqtt_topic_health"]
    doc = json.loads(frame)
    assert doc["sequence"] == 0
    assert doc["runtime_id"] == "test-runtime"
    assert doc["source"] == instance._config["source"]
    assert doc["firmware_version"] == FIRMWARE_VERSION
    assert doc["message_schema_version"] == MESSAGE_SCHEMA_VERSION
    # Removing the spliced envelope restores exactly what the sender queued:
    # proof the body was carried through, not parsed and rebuilt.
    for key in ("sequence", "runtime_id", "source", "firmware_version",
                "message_schema_version"):
        del doc[key]
    assert doc == message
    assert instance._next_sequence == 1


def test_publish_entry_sequence_increments_per_published_entry(make_core0):
    from intercore import KIND_HEALTH, RETENTION_PRIORITY_HEALTH
    from message_serializer import serialize_and_validate_message

    instance = make_core0()
    queue = _real_outbound_queue(instance)
    for i in range(2):
        message = {"message_type": "health", "uptime_ms": i, "timestamp": None,
                   "payload": {"id": i}}
        assert queue.put_with_kind(
            KIND_HEALTH, serialize_and_validate_message(message), RETENTION_PRIORITY_HEALTH
        )
        instance._publish_entry(queue.take())

    sequences = [json.loads(frame)["sequence"] for _topic, frame in instance._mqtt.published]
    assert sequences == [0, 1]
    assert instance._next_sequence == 2


def test_publish_entry_rejects_payload_that_is_not_a_json_object(make_core0):
    """A non-object payload cannot be spliced; the entry fails, nothing is sent."""
    from intercore import KIND_HEALTH

    instance = make_core0()
    entry = {"payload_bytes": b"[1, 2, 3]", "kind": KIND_HEALTH}

    with pytest.raises(ValueError):
        instance._publish_entry(entry)

    assert instance._mqtt.published == []
    assert instance._next_sequence == 0
