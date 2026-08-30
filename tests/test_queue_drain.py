# test_queue_drain.py - Post-outage queue drain episode, metrics, and rate limit
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

# MicroPython ticks are 28-bit (they wrap at 0x10000000). The fake below
# implements the real wrap semantics so these tests exercise the same
# time.ticks_* arithmetic the firmware runs on the Pico.
_TICKS_MASK = 0x0FFFFFFF


class FakeTime:
    """Controllable stand-in for MicroPython's time module with genuine
    28-bit tick-wrap semantics. sleep_ms advances the clock, so a drain pass
    that slept would move the clock while a non-blocking gate would not."""

    def __init__(self):
        self.now_ms = 0

    def ticks_ms(self):
        return self.now_ms & _TICKS_MASK

    def ticks_diff(self, a, b):
        diff = (a - b) & _TICKS_MASK
        return diff - 0x10000000 if diff & 0x08000000 else diff

    def ticks_add(self, a, delta):
        return (a + delta) & _TICKS_MASK

    def sleep_ms(self, ms):
        self.now_ms = (self.now_ms + ms) & _TICKS_MASK

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
# time: later-collected modules import the real time module and wifi/mqtt at
# collection, and mocked entries in sys.modules would shadow them. They are
# installed inside the fixture below, which also imports/reloads core0 there.
from message_protocol import format_utc_epoch_ms  # noqa: E402
from observability import (  # noqa: E402
    EVENT_MQTT_CONNECTION_ESTABLISHED,
    LEVEL_INFO,
    REASON_NONE,
)
from version import MESSAGE_SCHEMA_VERSION  # noqa: E402


def _load_core0_config():
    from config import split_config

    config = json.loads((ROOT / "config.json").read_text())
    core0_config, _core1_config, _bus_config = split_config(config)
    return core0_config


class FakeLed:
    def __init__(self):
        self.states = []

    def set_connecting(self, value):
        self.states.append(bool(value))

    def telemetry_sent(self):
        pass


class FakeWifi:
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
    """Scripts connect/publish outcomes and echoes a UTC info_response."""

    def __init__(self, core0_instance):
        self.core0 = core0_instance
        self.connected = False
        self.connect_calls = 0
        self.mark_disconnected_calls = 0
        self.published = []
        # Scripted publish_qos1 outcomes, in call order: "ok" (default) or
        # "fail". A "fail" drops the session and raises -- the ambiguous QoS 1
        # case where the PUBLISH reached the broker but the PUBACK was lost.
        self.publish_script = []
        self._ping_due = False
        self.ping_calls = 0
        self._last_info_request = None
        self._utc_deliver = True
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
            "publish_attempt_count": 0,
            "publish_retry_count": 0,
            "puback_timeout_count": 0,
            "connection_failure_count": 0,
            "reconnect_success_count": 0,
            "last_reconnect_duration_ms": 0,
            "last_outage_duration_ms": 0,
        }

    def get_next_packet_id(self):
        return 1

    def ping_due(self):
        return self._ping_due

    def ping(self):
        self.ping_calls += 1

    def publish_qos1(self, topic, message, is_retry=False):
        outcome = self.publish_script.pop(0) if self.publish_script else "ok"
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

    def set_utc_snapshot(self, snapshot):
        self.utc_snapshot = snapshot

    def get_utc_snapshot(self):
        return getattr(self, "utc_snapshot", None)

    def set_network_snapshot(self, snapshot):
        self.network_snapshots.append(snapshot)

    def get_network_snapshot(self):
        return self.network_snapshots[-1] if self.network_snapshots else None

    def get_core_1_activity_ms(self):
        # Core 1 is not started in these tests: the heartbeat check is a no-op.
        return None


class MockInterCore:
    def __init__(self):
        self.state_mailboxes = RecordingMailboxes()
        self.outbound_queue = MagicMock()
        self.event_queue = MagicMock()
        # Core 0's publish boundary reads the shared MemoryStats; a no-op
        # stand-in keeps these tests focused on the drain behavior.
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

        instance = core0_mod.Core0(
            MockInterCore(),
            _load_core0_config(),
            {"wifi_ssid": "test-ssid", "wifi_password": "test-password"},
            "test-runtime",
            0,
            FakeLed(),
        )
        instance._wifi = FakeWifi()
        instance._mqtt = FakeMqtt(instance)
        return instance

    return _make


def _real_outbound_queue(instance):
    """Swap the fixture's mock queue for the real bounded queue."""
    from intercore import OutboundQueue, MemoryStats

    instance._intercore.outbound_queue = OutboundQueue(65536, MemoryStats(), 16)
    return instance._intercore.outbound_queue


def _queue_telemetry(instance, count):
    """Admit ``count`` telemetry entries (as Core 1 would during an outage)."""
    from intercore import KIND_TELEMETRY, RETENTION_PRIORITY_TELEMETRY
    from message_serializer import serialize_and_validate_message

    queue = instance._intercore.outbound_queue
    for index in range(count):
        message = {
            "message_type": "telemetry",
            "uptime_ms": index,
            "timestamp": None,
            "payload": {"index": index},
        }
        assert queue.put_with_kind(
            KIND_TELEMETRY,
            serialize_and_validate_message(message),
            RETENTION_PRIORITY_TELEMETRY,
        )


def _connect(instance):
    """Establish the steady-state (post-start) connection the tests drive."""
    instance._network_stack_ready = True
    instance._wifi.connected = True
    instance._mqtt.connected = True


def _recover(instance):
    """Drop the MQTT session and run the runtime recovery path."""
    instance._mqtt.mark_disconnected()
    instance._recover_network_if_needed()


def _run_gated_pass(instance):
    """One pass of the run loop's gated take/publish/ping block.

    Mirrors the production branch structure in run() exactly (gate decides
    whether take() runs; a publish consumes a slot when it begins; the PINGREQ
    branch is the fallback when no entry publishes), without the infinite
    loop. Returns what the pass did.
    """
    if not instance._mqtt.is_connected():
        return "disconnected"
    now_ms = _FAKE_TIME.ticks_ms()
    entry = None
    if instance._post_outage_queue_publish_allowed(now_ms):
        entry = instance._intercore.outbound_queue.take()
    if entry is not None:
        instance._advance_post_outage_queue_publish_deadline(now_ms)
        try:
            instance._publish_entry(entry)
        except MemoryError:
            raise
        except Exception:
            return "publish_failed"
        instance._intercore.outbound_queue.complete_in_flight(entry)
        instance._record_queue_drain_success()
        return "published"
    if instance._mqtt.ping_due():
        instance._mqtt.ping()
        return "ping"
    return "skipped"


def _assert_pass(instance, expected):
    assert _run_gated_pass(instance) == expected


# ---------------------------------------------------------------------------
# Episode lifecycle and metrics
# ---------------------------------------------------------------------------

def test_initial_metrics_are_safe_defaults(make_core0):
    """Before the first completed drain every drain field is false/0."""
    instance = make_core0()
    _real_outbound_queue(instance)

    assert instance._queue_drain_active is False
    assert instance._last_queue_drain_start_depth == 0
    assert instance._last_queue_drain_message_count == 0
    assert instance._last_queue_drain_duration_ms == 0
    assert instance._last_queue_drain_rate_per_sec == 0


def test_startup_does_not_start_drain_episode(make_core0):
    """start() is not a runtime reconnect: even with a backlog in the queue
    the initial connection must not open a drain episode."""
    instance = make_core0()
    _real_outbound_queue(instance)
    _queue_telemetry(instance, 2)

    instance.start()

    assert instance._network_stack_ready is True
    assert instance._queue_drain_active is False
    assert instance._queue_drain_started_ms is None
    assert instance._last_queue_drain_start_depth == 0
    assert instance._last_queue_drain_message_count == 0


def test_episode_starts_after_runtime_reconnect_with_backlog(make_core0):
    """A runtime reconnect that completes with a non-empty queue starts the
    episode, and the first entry is eligible immediately after the reconnect."""
    instance = make_core0()
    _real_outbound_queue(instance)
    _queue_telemetry(instance, 4)
    _connect(instance)

    _FAKE_TIME.now_ms = 1000
    _recover(instance)

    assert instance._mqtt.connected is True
    assert instance._queue_drain_active is True
    assert instance._queue_drain_started_ms == 1000
    assert instance._queue_drain_start_depth == 4
    assert instance._queue_drain_message_count == 0
    assert instance._queue_drain_next_publish_ms == 1000
    assert instance._post_outage_queue_publish_allowed(1000) is True


def test_empty_queue_reconnect_starts_no_episode_and_preserves_metrics(make_core0):
    """An empty queue at reconnect starts no episode and leaves the last
    completed metrics exactly as they were."""
    instance = make_core0()
    _real_outbound_queue(instance)
    _connect(instance)
    # A previously completed drain is on record.
    instance._last_queue_drain_start_depth = 9
    instance._last_queue_drain_message_count = 9
    instance._last_queue_drain_duration_ms = 450
    instance._last_queue_drain_rate_per_sec = 20

    _FAKE_TIME.now_ms = 1000
    _recover(instance)

    assert instance._queue_drain_active is False
    assert instance._queue_drain_started_ms is None
    assert instance._queue_drain_next_publish_ms is None
    assert instance._last_queue_drain_start_depth == 9
    assert instance._last_queue_drain_message_count == 9
    assert instance._last_queue_drain_duration_ms == 450
    assert instance._last_queue_drain_rate_per_sec == 20


def test_successful_drain_metrics_match_spec_example(make_core0):
    """Reconnect at t=1000 with depth 4; completions at 1000/1250/1500/1750
    give start depth 4, message count 4, duration 750 ms, and integer rate
    4 * 1000 // 750 = 5."""
    instance = make_core0()
    _real_outbound_queue(instance)
    _queue_telemetry(instance, 4)
    _connect(instance)

    _FAKE_TIME.now_ms = 1000
    _recover(instance)
    assert instance._queue_drain_active is True

    _assert_pass(instance, "published")  # t=1000
    _FAKE_TIME.now_ms = 1250
    _assert_pass(instance, "published")
    _FAKE_TIME.now_ms = 1500
    _assert_pass(instance, "published")
    _FAKE_TIME.now_ms = 1750
    _assert_pass(instance, "published")  # queue empty -> episode ends

    assert instance._queue_drain_active is False
    assert instance._queue_drain_next_publish_ms is None
    assert instance._last_queue_drain_start_depth == 4
    assert instance._last_queue_drain_message_count == 4
    assert instance._last_queue_drain_duration_ms == 750
    assert instance._last_queue_drain_rate_per_sec == 5
    assert len(instance._mqtt.published) == 4


def test_new_messages_produced_during_drain_are_counted(make_core0):
    """Core 1 keeps producing while the drain runs: the count ends above the
    start depth, and the episode ends when the queue finally empties."""
    instance = make_core0()
    _real_outbound_queue(instance)
    _queue_telemetry(instance, 4)
    _connect(instance)

    _FAKE_TIME.now_ms = 1000
    _recover(instance)

    _assert_pass(instance, "published")
    _FAKE_TIME.now_ms = 1200
    _assert_pass(instance, "published")
    # Two more entries land while the drain is in progress (4 - 2 + 2 = 4 left).
    _queue_telemetry(instance, 2)
    assert instance._intercore.outbound_queue.get_depth() == 4
    _FAKE_TIME.now_ms = 1400
    _assert_pass(instance, "published")
    _FAKE_TIME.now_ms = 1600
    _assert_pass(instance, "published")
    _FAKE_TIME.now_ms = 1800
    _assert_pass(instance, "published")
    _FAKE_TIME.now_ms = 2000
    _assert_pass(instance, "published")  # 6th completion empties the queue
    _FAKE_TIME.now_ms = 2200
    assert _run_gated_pass(instance) == "skipped"  # nothing left to drain

    assert instance._queue_drain_active is False
    assert instance._last_queue_drain_start_depth == 4
    assert instance._last_queue_drain_message_count == 6  # > start depth
    assert instance._last_queue_drain_duration_ms == 1000
    assert instance._last_queue_drain_rate_per_sec == 6  # 6 * 1000 // 1000


def test_mqtt_failure_mid_drain_interrupts_episode(make_core0):
    """An MQTT failure mid-drain cancels the unfinished episode (its partial
    count is discarded, the last COMPLETED metrics are preserved) and the next
    reconnect starts a fresh episode at the then-current depth."""
    instance = make_core0()
    _real_outbound_queue(instance)

    # Drain #1: two entries, completed.
    _queue_telemetry(instance, 2)
    _connect(instance)
    _FAKE_TIME.now_ms = 100
    _recover(instance)
    _assert_pass(instance, "published")
    _FAKE_TIME.now_ms = 300
    _assert_pass(instance, "published")
    assert instance._queue_drain_active is False
    assert instance._last_queue_drain_start_depth == 2
    assert instance._last_queue_drain_message_count == 2
    assert instance._last_queue_drain_duration_ms == 200
    assert instance._last_queue_drain_rate_per_sec == 10

    # Drain #2: three new entries; one completes, then the link fails again.
    _queue_telemetry(instance, 3)
    _FAKE_TIME.now_ms = 1000
    _recover(instance)
    assert instance._queue_drain_active is True
    assert instance._queue_drain_start_depth == 3
    _FAKE_TIME.now_ms = 1200
    _assert_pass(instance, "published")
    assert instance._queue_drain_active is True
    assert instance._queue_drain_message_count == 1  # partial, not frozen yet

    # Failure mid-drain: the partial count is cancelled, drain #1's metrics
    # stand, and a fresh episode opens at the then-current depth (2).
    _FAKE_TIME.now_ms = 2000
    _recover(instance)
    assert instance._queue_drain_active is True
    assert instance._queue_drain_start_depth == 2
    assert instance._last_queue_drain_start_depth == 2
    assert instance._last_queue_drain_message_count == 2
    assert instance._last_queue_drain_duration_ms == 200
    assert instance._last_queue_drain_rate_per_sec == 10


def test_failed_publish_consumes_drain_slot_and_entry_stays_retryable(make_core0):
    """A drain attempt that begins consumes its slot even when the attempt
    fails (the PUBACK is lost), and the entry stays in flight for the retry.
    A retry therefore needs a fresh slot, not an immediate one."""
    instance = make_core0()
    queue = _real_outbound_queue(instance)
    _queue_telemetry(instance, 1)
    _connect(instance)

    _FAKE_TIME.now_ms = 0
    _recover(instance)
    assert instance._queue_drain_active is True
    instance._drain_rate_per_sec = 2  # 500 ms slots
    instance._queue_drain_next_publish_ms = 0  # first slot due now

    instance._mqtt.publish_script = ["fail"]
    # The attempt begins (consuming the slot) but the PUBACK is lost.
    assert _run_gated_pass(instance) == "publish_failed"
    # The failed attempt consumed the slot (it began, so it counts) ...
    assert instance._queue_drain_next_publish_ms == 500
    # ... and the entry is still in flight, eligible for the retry.
    assert queue.has_in_flight()
    # A fresh slot is required for the retry: not due at 499, due at 500.
    assert instance._post_outage_queue_publish_allowed(499) is False
    assert instance._post_outage_queue_publish_allowed(500) is True


def test_in_flight_entry_participates_in_next_drain_after_interruption(make_core0):
    """An entry left in flight across a mid-drain failure is retried by the
    next drain episode (started at the then-current depth, which includes the
    in-flight entry), completing the drain once it succeeds."""
    instance = make_core0()
    queue = _real_outbound_queue(instance)
    _queue_telemetry(instance, 1)
    _connect(instance)

    _FAKE_TIME.now_ms = 0
    _recover(instance)
    instance._mqtt.publish_script = ["fail"]
    assert _run_gated_pass(instance) == "publish_failed"
    assert queue.has_in_flight()

    # The failure drops the connection; the run-loop recovery path interrupts
    # this episode and opens a fresh one at the then-current depth (1, the
    # in-flight entry).
    _FAKE_TIME.now_ms = 100
    _recover(instance)
    assert instance._queue_drain_active is True
    assert instance._queue_drain_start_depth == 1

    # The drain retries the SAME in-flight entry and completes the episode.
    _FAKE_TIME.now_ms = 300
    _assert_pass(instance, "published")
    assert instance._queue_drain_active is False
    assert instance._last_queue_drain_message_count == 1
    assert instance._last_queue_drain_duration_ms == 200


# ---------------------------------------------------------------------------
# Rate limiter: disabled, inert, slot timing, non-blocking
# ---------------------------------------------------------------------------

def test_disabled_rate_keeps_unlimited_drain(make_core0):
    """config.json ships the limit disabled (0): an active episode must
    preserve the current unlimited fast drain exactly."""
    instance = make_core0()
    _real_outbound_queue(instance)
    _queue_telemetry(instance, 3)
    _connect(instance)

    _FAKE_TIME.now_ms = 0
    _recover(instance)

    assert instance._queue_drain_active is True
    assert instance._drain_rate_per_sec == 0
    _assert_pass(instance, "published")
    # Mid-episode: with the limit disabled the gate is always open and an
    # explicit slot advance is a no-op -- no deadline is ever scheduled.
    assert instance._post_outage_queue_publish_allowed(_FAKE_TIME.ticks_ms()) is True
    instance._advance_post_outage_queue_publish_deadline(_FAKE_TIME.ticks_ms())
    assert instance._queue_drain_next_publish_ms == 0  # unchanged
    _assert_pass(instance, "published")
    _assert_pass(instance, "published")
    assert instance._queue_drain_active is False
    assert instance._last_queue_drain_message_count == 3
    # All three completions happened at t=0 -- no drain-introduced delay.
    assert _FAKE_TIME.now_ms == 0


def test_no_active_episode_is_never_gated(make_core0):
    """A positive rate with no drain episode active gates nothing: normal
    connected publishing is never limited by this setting."""
    instance = make_core0()
    _real_outbound_queue(instance)
    _connect(instance)
    instance._drain_rate_per_sec = 2

    assert instance._queue_drain_active is False
    assert instance._post_outage_queue_publish_allowed(0) is True
    assert instance._post_outage_queue_publish_allowed(999999) is True
    instance._advance_post_outage_queue_publish_deadline(0)
    assert instance._queue_drain_next_publish_ms is None  # limiter inert


def test_startup_work_drain_is_not_rate_limited(make_core0):
    """Startup connection-log work is Core 0's own publishing path, not the
    queue drain: even with an active episode and a tight rate it must not
    consult the drain slots (a pending slot stays exactly where it was)."""
    instance = make_core0()
    _real_outbound_queue(instance)
    _connect(instance)
    instance._queue_drain_active = True
    instance._drain_rate_per_sec = 1
    instance._queue_drain_next_publish_ms = 5000

    instance._queue_connection_log(
        LEVEL_INFO, EVENT_MQTT_CONNECTION_ESTABLISHED, REASON_NONE, "Connected", {})
    instance._queue_connection_log(
        LEVEL_INFO, EVENT_MQTT_CONNECTION_ESTABLISHED, REASON_NONE, "Reconnected", {})

    assert instance._drain_startup_mqtt_work() is True
    assert instance._queue_drain_next_publish_ms == 5000  # untouched
    assert len(instance._mqtt.published) == 2


def test_slot_spacing_follows_rate(make_core0):
    """Slots are spaced max(1, ceil(1000 / rate)) ms apart, with the first
    entry eligible immediately after the reconnect."""
    instance = make_core0()
    _real_outbound_queue(instance)
    _queue_telemetry(instance, 1)
    _connect(instance)
    _FAKE_TIME.now_ms = 0
    _recover(instance)
    instance._drain_rate_per_sec = 2

    # 2/s -> 500 ms spacing: not due at 499, due at 500.
    assert instance._post_outage_queue_publish_allowed(0) is True
    instance._advance_post_outage_queue_publish_deadline(0)
    assert instance._post_outage_queue_publish_allowed(499) is False
    assert instance._post_outage_queue_publish_allowed(500) is True
    instance._advance_post_outage_queue_publish_deadline(500)
    assert instance._post_outage_queue_publish_allowed(999) is False
    assert instance._post_outage_queue_publish_allowed(1000) is True

    # The interval table.
    for rate, interval in ((1, 1000), (2, 500), (3, 334), (5, 200), (10, 100)):
        instance._drain_rate_per_sec = rate
        instance._queue_drain_next_publish_ms = 0
        instance._advance_post_outage_queue_publish_deadline(0)
        assert instance._queue_drain_next_publish_ms == interval


def test_gate_is_non_blocking(make_core0):
    """Checking or consuming a slot never sleeps: the clock does not move
    while the drain defers work (the run loop advances it by its own 10 ms)."""
    instance = make_core0()
    _real_outbound_queue(instance)
    _queue_telemetry(instance, 1)
    _connect(instance)
    _FAKE_TIME.now_ms = 0
    _recover(instance)
    instance._drain_rate_per_sec = 1  # 1000 ms slot

    instance._advance_post_outage_queue_publish_deadline(0)
    assert _FAKE_TIME.now_ms == 0
    for _ in range(10):
        instance._post_outage_queue_publish_allowed(_FAKE_TIME.ticks_ms())
    assert _FAKE_TIME.now_ms == 0  # pure decision: no sleep, no clock advance


# ---------------------------------------------------------------------------
# Tick-wrap safety (28-bit tick boundary at 0x08000000)
# ---------------------------------------------------------------------------

def test_tick_wrap_safe_duration_and_rate(make_core0):
    """Episode timing must use time.ticks_*, so a drain crossing the 28-bit
    tick boundary (2^28 ms) measures correctly -- a raw unsigned
    now - started would go negative and clamp the duration to 1 ms, giving a
    wildly wrong (1000/s) rate."""
    instance = make_core0()
    _real_outbound_queue(instance)
    _queue_telemetry(instance, 1)
    _connect(instance)

    # Episode starts 32 ms below the wrap boundary (0x10000000).
    _FAKE_TIME.now_ms = 0x0FFFFFE0
    _recover(instance)
    assert instance._queue_drain_started_ms == 0x0FFFFFE0

    # Completion 80 ms after the start: 48 ms PAST the boundary (raw now
    # 0x10000030 wraps to 0x30).
    _FAKE_TIME.now_ms = 0x10000030
    _assert_pass(instance, "published")
    assert instance._queue_drain_active is False
    assert instance._last_queue_drain_duration_ms == 80
    assert instance._last_queue_drain_rate_per_sec == (1 * 1000) // 80  # 12


def test_tick_wrap_safe_slot_eligibility(make_core0):
    """The next-slot timestamp wraps like any tick value: the deadline math
    (ticks_add) and the due check (ticks_diff) must stay signed, or a naive
    unsigned now >= next would release the attempt early around the boundary."""
    instance = make_core0()
    _real_outbound_queue(instance)
    _queue_telemetry(instance, 1)
    _connect(instance)
    instance._drain_rate_per_sec = 2  # 500 ms slot

    # Arm the slot 256 ms below the wrap boundary.
    _FAKE_TIME.now_ms = 0x0FFFFF00
    _recover(instance)
    instance._queue_drain_next_publish_ms = 0x0FFFFF00
    instance._advance_post_outage_queue_publish_deadline(0x0FFFFF00)
    # ticks_add(0x0FFFFF00, 500) wraps: the deadline is a SMALL tick value.
    assert instance._queue_drain_next_publish_ms == 0xF4

    # 0xC0 ms AFTER the arm but still 308 ms BEFORE the wrapped deadline:
    # not yet due. (A naive unsigned now >= next would read
    # 0x0FFFFFC0 > 0xF4 and release the attempt early.)
    assert instance._post_outage_queue_publish_allowed(0x0FFFFFC0) is False
    # Past the wrapped deadline: due.
    assert instance._post_outage_queue_publish_allowed(0x10000100) is True


# ---------------------------------------------------------------------------
# Reporting: network snapshot and SystemInformation.get_queues()
# ---------------------------------------------------------------------------

def test_snapshot_carries_drain_metrics(make_core0):
    """The network snapshot (the Core 0 -> Core 1 transport) carries all five
    drain fields: safe defaults before the first drain, live state while an
    episode is active, and the frozen metrics after it completes."""
    instance = make_core0()
    _real_outbound_queue(instance)

    def latest():
        return instance._intercore.state_mailboxes.network_snapshots[-1]

    instance._publish_network_snapshot(force=True)
    assert latest()["outbound_queue_drain_active"] is False
    assert latest()["outbound_queue_last_drain_start_depth"] == 0
    assert latest()["outbound_queue_last_drain_message_count"] == 0
    assert latest()["outbound_queue_last_drain_duration_ms"] == 0
    assert latest()["outbound_queue_last_drain_rate_per_sec"] == 0

    _queue_telemetry(instance, 2)
    _connect(instance)
    _FAKE_TIME.now_ms = 100
    _recover(instance)
    # Mid-episode: active is true; the last-completed values are still 0.
    instance._publish_network_snapshot(force=True)
    assert latest()["outbound_queue_drain_active"] is True
    assert latest()["outbound_queue_last_drain_start_depth"] == 0

    _assert_pass(instance, "published")
    _FAKE_TIME.now_ms = 400
    _assert_pass(instance, "published")
    instance._publish_network_snapshot(force=True)
    assert latest()["outbound_queue_drain_active"] is False
    assert latest()["outbound_queue_last_drain_start_depth"] == 2
    assert latest()["outbound_queue_last_drain_message_count"] == 2
    assert latest()["outbound_queue_last_drain_duration_ms"] == 300
    assert latest()["outbound_queue_last_drain_rate_per_sec"] == 6  # 2 * 1000 // 300


def test_get_queues_reports_drain_fields_with_safe_defaults(make_core0):
    """SystemInformation.get_queues() carries the drain fields from the
    network snapshot, with false/0 defaults when the snapshot is absent."""
    from intercore import InterCore
    from system_information import SystemInformation

    bus = InterCore(outbound_max=16, event_max=4)
    info = SystemInformation(bus, {})

    # No snapshot yet (Core 0 has not published): safe defaults.
    queues = info.get_queues()
    assert queues["outbound_queue_drain_active"] is False
    assert queues["outbound_queue_last_drain_start_depth"] == 0
    assert queues["outbound_queue_last_drain_message_count"] == 0
    assert queues["outbound_queue_last_drain_duration_ms"] == 0
    assert queues["outbound_queue_last_drain_rate_per_sec"] == 0

    bus.state_mailboxes.set_network_snapshot({
        "outbound_queue_drain_active": True,
        "outbound_queue_last_drain_start_depth": 12,
        "outbound_queue_last_drain_message_count": 14,
        "outbound_queue_last_drain_duration_ms": 700,
        "outbound_queue_last_drain_rate_per_sec": 20,
    })
    queues = info.get_queues()
    assert queues["outbound_queue_drain_active"] is True
    assert queues["outbound_queue_last_drain_start_depth"] == 12
    assert queues["outbound_queue_last_drain_message_count"] == 14
    assert queues["outbound_queue_last_drain_duration_ms"] == 700
    assert queues["outbound_queue_last_drain_rate_per_sec"] == 20
    # The pre-existing queue fields are unaffected.
    assert queues["outbound_safety_max_entries"] == 16
    assert "outbound_pending" in queues
    assert "intercore_events_pending" in queues
