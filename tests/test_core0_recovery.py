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
from observability import (  # noqa: E402
    EVENT_MQTT_CONNECTION_ESTABLISHED,
    LEVEL_INFO,
    REASON_NONE,
)
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
        self.reconnect_triggers = []

    def is_connected(self):
        return self.connected

    def connect(self):
        self.connected = True
        self.connect_calls += 1
        return True

    def note_reconnect_trigger(self, trigger):
        self.reconnect_triggers.append(trigger)

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
        # Scripted publish_qos1 outcomes, in call order: "ok" (default) or
        # "fail". A "fail" records the frame (the PUBLISH reached the broker),
        # drops the session, and raises -- the ambiguous QoS 1 case where the
        # frame was delivered but the PUBACK was lost. Empty means always ok.
        self.publish_script = []
        # Logical retry classification received from Core 0 -- the point of the
        # retry-classification tests is that Core 0 tells Mqtt which attempts
        # are retries of the same logical message.
        self.publish_qos1_is_retry = []
        self._publish_attempt_count = 0
        self._publish_retry_count = 0

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
            "publish_attempt_count": self._publish_attempt_count,
            "publish_retry_count": self._publish_retry_count,
            "puback_timeout_count": 0,
            "connection_failure_count": 0,
            "reconnect_success_count": 0,
            "last_reconnect_duration_ms": 0,
            "last_outage_duration_ms": 0,
        }

    def get_next_packet_id(self):
        return 1

    def publish_qos1(self, topic, message, is_retry=False):
        # Mirror the real Mqtt's publish accounting so status() is consistent.
        self._publish_attempt_count += 1
        if is_retry:
            self._publish_retry_count += 1
        self.publish_qos1_is_retry.append(is_retry)
        outcome = self.publish_script.pop(0) if self.publish_script else "ok"
        # The frame is transmitted either way: even a "fail" means the PUBLISH
        # reached the broker; the failure is only that the PUBACK never came.
        self.published.append((topic, message))
        doc = json.loads(message)
        if doc.get("message_type") == "info_request":
            self._last_info_request = doc
        if outcome == "fail":
            # A failed QoS 1 publish drops the session, as the real client does.
            self.mark_disconnected()
            raise RuntimeError("PUBACK lost (simulated ambiguous QoS 1 failure)")

    def publish_qos1_with_packet_id(
        self, topic, message, packet_id, timeout_ms=None, is_retry=False
    ):
        self._publish_attempt_count += 1
        if is_retry:
            self._publish_retry_count += 1
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
        # The runtime-recovery path reads the queue depth to decide whether to
        # open a post-outage drain episode; an empty queue (depth 0) starts no
        # episode, which is what these recovery tests are focused on.
        self.outbound_queue.get_depth.return_value = 0
        self.event_queue = MagicMock()
        # Core 0's publish boundary reads the shared MemoryStats (observe the
        # free heap + optional headroom collect) and the board reserve. Both
        # return values are unused in the publish path, so a no-op stand-in
        # keeps these tests focused on the publish/sequence behavior.
        self.memory_stats = MagicMock()
        self.minimum_free_heap_bytes = 65536


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
    from intercore import OutboundQueue, MemoryStats

    # Real queue + real MemoryStats (the healthy conftest heap means the
    # pressure-relief path is a no-op, so admission exercises the entry/priority
    # path these tests target). The 16-entry ceiling matches the old behavior.
    instance._intercore.outbound_queue = OutboundQueue(65536, MemoryStats(), 16)
    return instance._intercore.outbound_queue


def test_publish_entry_splices_core0_envelope_and_keeps_body_intact(make_core0):
    """The wire frame is the queued message with Core 0's envelope spliced in.

    Core 0 must not decode, parse, or re-serialize the payload: the message
    body must appear in the published frame exactly as the sender queued it,
    and the six envelope members must be Core 0's own values (its runtime_id
    and configured source, not anything a sender might have embedded).
    """
    from intercore import KIND_HEALTH, RETENTION_PRIORITY_HEALTH
    from message_serializer import serialize_and_validate_message
    from version import FIRMWARE_BUILD_COMMIT, FIRMWARE_VERSION

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
    assert doc["firmware_build_commit"] == FIRMWARE_BUILD_COMMIT
    # Removing the spliced envelope restores exactly what the sender queued:
    # proof the body was carried through, not parsed and rebuilt.
    for key in ("sequence", "runtime_id", "source", "firmware_version",
                "message_schema_version", "firmware_build_commit"):
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
        entry = queue.take()
        instance._publish_entry(entry)
        # A successful publish completes the in-flight entry (as the run loop
        # does), so the next take() returns the NEXT queued entry -- two
        # different messages, and therefore two different sequences.
        assert queue.complete_in_flight(entry)

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


def test_sequence_not_reused_across_ambiguous_qos1_failure_and_reconnect(make_core0):
    """A PUBACK lost after delivery must not let a different message reuse the
    in-flight entry's sequence, and a retry must keep it.

    Reproduces the field capture: telemetry A publishes, the PUBACK is lost so
    the entry stays in flight, the link drops and reconnects (publishing an
    mqtt_connection_established log), and telemetry A is then retried. Before
    the fix the connection log took telemetry A's sequence (a collision) and the
    retry took a new one. Now the connection log gets a fresh number and the
    retry reuses the in-flight entry's number, so two different logical messages
    never share a sequence and a retry preserves its logical identity.
    """
    from intercore import KIND_TELEMETRY, RETENTION_PRIORITY_TELEMETRY
    from message_serializer import serialize_and_validate_message

    instance = make_core0()
    mqtt = instance._mqtt
    instance._network_stack_ready = True
    instance._wifi.connected = True
    mqtt.connected = True
    queue = _real_outbound_queue(instance)

    # Telemetry A is queued and taken in flight.
    telemetry = {
        "message_type": "telemetry",
        "uptime_ms": 1411267,
        "timestamp": None,
        "payload": {"value": 42},
    }
    assert queue.put_with_kind(
        KIND_TELEMETRY, serialize_and_validate_message(telemetry), RETENTION_PRIORITY_TELEMETRY
    )
    entry = queue.take()
    assert queue.has_in_flight()

    # Script the broker: accept telemetry A (PUBACK lost -> fail), then accept
    # the connection log and the telemetry A retry.
    mqtt.publish_script = ["fail", "ok", "ok"]

    # Attempt 1: telemetry A transmits, the PUBACK is lost.
    with pytest.raises(RuntimeError):
        instance._publish_entry(entry)
    # The entry stays in flight (QoS 1 must not drop it) and carries its
    # claimed number so a retry can reuse it.
    assert queue.has_in_flight()
    telemetry_seq = json.loads(mqtt.published[0][1])["sequence"]
    assert entry["_wire_sequence"] == telemetry_seq

    # The failed publish dropped the link; recovery reconnects and queues the
    # mqtt_connection_established log.
    instance._recover_network_if_needed()
    assert mqtt.connected is True
    assert len(instance._pending_connection_logs) == 1

    # The connection log publishes and must take a FRESH sequence (not A's).
    instance._service_pending_connection_log()
    log_seq = json.loads(mqtt.published[-1][1])["sequence"]
    assert log_seq != telemetry_seq  # no collision with the in-flight message

    # Telemetry A is retried and must PRESERVE its original sequence.
    retried = queue.take()
    assert retried is entry  # the same in-flight entry
    instance._publish_entry(retried)
    retry_seq = json.loads(mqtt.published[-1][1])["sequence"]
    assert retry_seq == telemetry_seq  # same logical message, same number

    # The two distinct logical messages have distinct sequences overall, and the
    # counter advanced past both claimed numbers (nothing is handed out twice).
    assert telemetry_seq != log_seq
    assert instance._next_sequence == telemetry_seq + 2


def test_command_response_retry_preserves_sequence_across_intervening_message(make_core0):
    """A Core 0 command response re-published after an ambiguous failure keeps
    the sequence it first claimed, even when an intervening message consumed a
    number -- instead of silently shifting to a new one.

    And, because (runtime_id, sequence) is now a unique event identity, the
    retry must be the SAME document, not just the same identity: the serialized
    bytes are frozen on the first attempt and re-published verbatim, so two
    frames carrying one logical message are byte-identical. (Before the fix the
    retry rebuilt the message with a newer uptime_ms/timestamp, so a deduplicator
    keying on (runtime_id, sequence) could drop one of two differing documents.)
    """
    instance = make_core0()
    mqtt = instance._mqtt
    # fail (response attempt 1), ok (intervening connection log), ok (retry).
    mqtt.publish_script = ["fail", "ok", "ok"]

    response = {
        "command_id": "req-1",
        "command": "reboot",
        "success": True,
        "targeted": False,
        "data": {"rebooting": True},
    }
    instance._pending_core0_responses.append(response)

    # Attempt 1: transmits, PUBACK lost -> fails; the response stays pending.
    with pytest.raises(RuntimeError):
        instance._service_pending_core0_response()
    first_frame = mqtt.published[0][1]
    first_seq = json.loads(first_frame)["sequence"]
    assert instance._pending_core0_responses  # still queued for retry

    # An intervening connection log publishes and consumes the next number.
    instance._queue_connection_log(
        LEVEL_INFO, EVENT_MQTT_CONNECTION_ESTABLISHED, REASON_NONE,
        "Connected to MQTT broker", {},
    )
    instance._service_pending_connection_log()
    log_seq = json.loads(mqtt.published[-1][1])["sequence"]
    assert log_seq != first_seq

    # Advance the clock before the retry so a rebuild WOULD change the
    # document: the retry must NOT pick up the newer uptime, which is only
    # possible if it reuses the frozen bytes from attempt 1 instead of
    # re-serializing. (Without this, both attempts saw the same clock and the
    # old rebuild-then-reserialize path happened to produce identical frames.)
    _FAKE_TIME.now_ms = 5000

    # Attempt 2: the retry must preserve the response's ORIGINAL sequence and
    # re-publish the SAME serialized document (byte-identical frame).
    instance._service_pending_core0_response()
    retry_frame = mqtt.published[-1][1]
    retry_seq = json.loads(retry_frame)["sequence"]
    assert retry_seq == first_seq
    assert first_frame == retry_frame  # same logical message: same identity AND content
    assert not instance._pending_core0_responses  # consumed on success


# ---------------------------------------------------------------------------
# Logical retry classification (owned by Core 0, passed to Mqtt)
#
# Core 0 is the owner of logical message identity: it knows whether the same
# logical message was attempted before, and must tell Mqtt explicitly rather
# than letting it be inferred from a packet id, topic, payload, or connection
# count. These tests drive the real Core 0 publish paths and assert the
# (publish_attempt_count, publish_retry_count) they hand to Mqtt, plus that the
# retry preserves the existing logical sequence identity.
# ---------------------------------------------------------------------------

def test_queue_retry_classified_and_sequence_preserved(make_core0):
    """A queued telemetry entry whose first publish failed is a RETRY when the
    same in-flight entry is taken again after a reconnect -- and it keeps its
    sequence. A different message published across the same window is an
    attempt, never a retry."""
    from intercore import KIND_TELEMETRY, RETENTION_PRIORITY_TELEMETRY
    from message_serializer import serialize_and_validate_message

    instance = make_core0()
    mqtt = instance._mqtt
    instance._network_stack_ready = True
    instance._wifi.connected = True
    mqtt.connected = True
    queue = _real_outbound_queue(instance)

    telemetry = {
        "message_type": "telemetry",
        "uptime_ms": 1411267,
        "timestamp": None,
        "payload": {"value": 42},
    }
    assert queue.put_with_kind(
        KIND_TELEMETRY, serialize_and_validate_message(telemetry), RETENTION_PRIORITY_TELEMETRY
    )
    entry = queue.take()
    assert queue.has_in_flight()

    # Attempt 1 of telemetry A: a fresh attempt (not a retry); the PUBACK is
    # lost so it fails and the entry stays in flight.
    mqtt.publish_script = ["fail", "ok", "ok"]
    with pytest.raises(RuntimeError):
        instance._publish_entry(entry)
    assert mqtt._publish_attempt_count == 1
    assert mqtt._publish_retry_count == 0
    assert mqtt.publish_qos1_is_retry == [False]
    assert queue.has_in_flight()
    telemetry_seq = json.loads(mqtt.published[0][1])["sequence"]

    # Recovery reconnects and publishes the connection log: a NEW logical
    # message, so an attempt -- never classified as a retry of A.
    instance._recover_network_if_needed()
    assert mqtt.connected is True
    assert len(instance._pending_connection_logs) == 1
    instance._service_pending_connection_log()
    assert mqtt._publish_attempt_count == 2
    assert mqtt._publish_retry_count == 0
    assert mqtt.publish_qos1_is_retry[-1] is False

    # The retry of the SAME in-flight entry is a retry (is_retry=True) and
    # preserves its original sequence.
    retried = queue.take()
    assert retried is entry  # the same logical object
    instance._publish_entry(retried)
    assert mqtt._publish_attempt_count == 3
    assert mqtt._publish_retry_count == 1
    assert mqtt.publish_qos1_is_retry == [False, False, True]
    assert json.loads(mqtt.published[-1][1])["sequence"] == telemetry_seq


def test_command_response_retry_classified_and_marker_on_container(make_core0):
    """A Core 0 command response that fails then retries: the first attempt is
    not a retry, the second (same logical response) is. The retry marker lives
    on the persistent response container, not the per-attempt entry, so the
    classification survives across the entry rebuild on each attempt."""
    instance = make_core0()
    mqtt = instance._mqtt
    mqtt.publish_script = ["fail", "ok"]

    response = {
        "command_id": "req-1",
        "command": "reboot",
        "success": True,
        "targeted": False,
        "data": {"rebooting": True},
    }
    instance._pending_core0_responses.append(response)

    # Attempt 1: a fresh logical message (not a retry); fails.
    with pytest.raises(RuntimeError):
        instance._service_pending_core0_response()
    assert mqtt._publish_attempt_count == 1
    assert mqtt._publish_retry_count == 0
    assert mqtt.publish_qos1_is_retry == [False]
    # The marker is owned by the persistent container, not the per-attempt entry.
    assert response["_publish_attempted"] is True

    # Attempt 2: the retry of the same logical response is classified as a
    # retry, sourced from the container marker.
    instance._service_pending_core0_response()
    assert mqtt._publish_attempt_count == 2
    assert mqtt._publish_retry_count == 1
    assert mqtt.publish_qos1_is_retry == [False, True]
    assert not instance._pending_core0_responses


def test_pending_reboot_retry_classified(make_core0):
    """The pending reboot response follows the same retry classification as a
    command response: first attempt not a retry, retry of the same pending
    reboot classified as a retry, with the marker on the persistent request."""
    instance = make_core0()
    mqtt = instance._mqtt
    _real_outbound_queue(instance)  # an empty real queue: has_in_flight() is False
    mqtt.publish_script = ["fail", "ok"]

    reboot_request = {
        "command_id": "reboot-1",
        "command": "reboot",
        "success": True,
        "targeted": False,
    }
    instance._pending_reboot = reboot_request

    # Attempt 1: not a retry; fails (the reboot stays pending).
    assert instance._perform_reboot() is False
    assert mqtt._publish_attempt_count == 1
    assert mqtt._publish_retry_count == 0
    assert mqtt.publish_qos1_is_retry == [False]
    assert instance._pending_reboot is reboot_request
    assert reboot_request["_publish_attempted"] is True

    # Attempt 2: the retry of the same pending reboot is classified as a retry.
    assert instance._perform_reboot() is True
    assert mqtt._publish_attempt_count == 2
    assert mqtt._publish_retry_count == 1
    assert mqtt.publish_qos1_is_retry == [False, True]
    assert instance._pending_reboot is None


def test_new_utc_requests_are_attempts_not_retries(make_core0):
    """Each UTC request is a fresh logical message (new request_id): a retry
    counter must not count them. Request #2 is a new attempt, never a retry of
    request #1's failed delivery."""
    instance = make_core0()
    mqtt = instance._mqtt

    # Request #1: a fresh attempt (Core 0 does not classify it as a retry).
    instance._utc_send_request()
    assert mqtt._publish_attempt_count == 1
    assert mqtt._publish_retry_count == 0
    first_counter = instance._utc_request_counter
    assert mqtt.publish_qos1_is_retry == [False]

    # Request #2: a NEW logical message (a new request_id) -- even if request
    # #1's response was never delivered, this is an attempt, not a retry.
    instance._utc_send_request()
    assert mqtt._publish_attempt_count == 2
    assert mqtt._publish_retry_count == 0
    assert instance._utc_request_counter == first_counter + 1
    assert mqtt.publish_qos1_is_retry == [False, False]
