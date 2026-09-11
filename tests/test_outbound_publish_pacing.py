# test_outbound_publish_pacing.py - Tests for the Core 0 outbound publish pacing gate
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Host-side regression tests for mqtt_outbound_publish_delay_ms.

Core 0 paces its outbound application PUBLISHes: after one QoS 1 publish completes (PUBACK received), no other application PUBLISH may begin until the configured interval elapses; the first publish after idle is immediate; 0 disables pacing. The interval is state, not a sleep: while the gate is closed the run loop keeps servicing the Core 1 watchdog and command polling. PINGREQ is never paced, and startup may wait for its slot.

The tests drive the real Core 0 code with a controllable wrapping tick clock, fake wifi/mqtt that record the clock time of every publish, and the real heap-governed outbound queue."""

import importlib
import json
import pathlib
import sys
import time as _real_time
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

ROOT = pathlib.Path(__file__).resolve().parents[1]

TICKS_PERIOD = 1 << 30  # 32-bit MicroPython builds (RP2): 30-bit tick values
HALF_PERIOD = 1 << 29


class LoopStop(Exception):
    """Raised by FakeTime to end an infinite Core 0 loop deterministically."""


class _MachineReset(Exception):
    """Stand-in for machine.reset(): on hardware it never returns."""


class ResettingMachine:
    """Records reset() calls and, like real hardware, never returns."""

    def __init__(self):
        self.reset_calls = 0

    def reset(self):
        self.reset_calls += 1
        raise _MachineReset()


class FakeTime:
    """Controllable MicroPython time stand-in with 30-bit tick wrap.

    sleep_ms advances the clock (and records every sleep), so any blocking sleep the implementation inserted would show up in sleep_calls."""

    def __init__(self):
        self._abs_ms = 0
        self.stop_after_ms = None
        self.sleep_calls = []

    def ticks_ms(self):
        return self._abs_ms % TICKS_PERIOD

    def ticks_diff(self, a, b):
        # MicroPython semantics: signed difference, correct across the wrap.
        return ((a - b) + (TICKS_PERIOD // 2)) % TICKS_PERIOD - (TICKS_PERIOD // 2)

    def ticks_add(self, base, delta):
        # MicroPython raises when the delta reaches half the period, so
        # ticks_diff can still round-trip it.
        if abs(delta) >= HALF_PERIOD:
            raise OverflowError("ticks interval overflow")
        return (base + delta) % TICKS_PERIOD

    def sleep_ms(self, ms):
        self.sleep_calls.append(ms)
        self._abs_ms += ms
        if self.stop_after_ms is not None and self._abs_ms >= self.stop_after_ms:
            raise LoopStop()

    def sleep(self, secs):
        self.sleep_ms(int(secs * 1000))

    def gmtime(self, secs):
        return _real_time.gmtime(secs)

    def __getattr__(self, name):
        # Anything not explicitly faked falls through to the real time
        # module so host tooling keeps working.
        return getattr(_real_time, name)


_FAKE_TIME = FakeTime()
_MACHINE = ResettingMachine()
_DEBUG_MOCK = MagicMock()
_DEBUG_MOCK.DEBUG = False


def _install_mocks():
    sys.modules["time"] = _FAKE_TIME
    sys.modules["machine"] = _MACHINE
    sys.modules["debug"] = _DEBUG_MOCK
    sys.modules["wifi"] = MagicMock()
    sys.modules["mqtt"] = MagicMock()


# NOTE: the MicroPython stand-ins above must NOT be installed at collection
# time (see tests/test_core0_recovery.py for the rationale); they are
# installed inside the fixture, which also imports/reloads core0 under them.
from config import split_config  # noqa: E402
from config_manager import ConfigManager  # noqa: E402
from intercore import (  # noqa: E402
    InterCore,
    KIND_TELEMETRY,
    RETENTION_PRIORITY_TELEMETRY,
)
from message_protocol import format_utc_epoch_ms  # noqa: E402
from message_serializer import serialize_and_validate_message  # noqa: E402
from version import MESSAGE_SCHEMA_VERSION  # noqa: E402


def _load_core0_config(delay_ms=100):
    config = json.loads((ROOT / "tests" / "fixtures" / "config.json").read_text())
    config["mqtt_outbound_publish_delay_ms"] = delay_ms
    core0_config, _core1_config = split_config(config)
    return core0_config


class FakeLed:
    def set_connecting(self, value):
        pass

    def telemetry_sent(self):
        pass


class FakeWifi:
    """A steady-state connected Wi-Fi link; pacing is network-agnostic."""

    def __init__(self):
        self.connected = True

    def is_connected(self):
        return self.connected

    def connect(self):
        return True

    def snapshot(self, mqtt_connected):
        return {
            "ssid": "test-ssid",
            "ip_address": "192.168.1.100",
            "rssi": -50,
            "wifi_connect_count": 1,
        }


class FakeMqtt:
    """A connected MQTT session that records every publish with its clock time."""

    def __init__(self, core0_instance):
        self.core0 = core0_instance
        self.connected = True
        self.published = []  # (topic, message, now_ms)
        self.check_msg_calls = 0
        # Scripted check_msg() outcome: an exception to raise, or None (ok).
        self.check_msg_error = None
        self.ping_calls = 0
        # Scripted failures: the next N publish_qos1 calls raise, the way a
        # lost PUBACK fails the exchange.
        self.fail_publishes = 0
        # Topics whose publish_qos1 ALWAYS raises (a persistent lost PUBACK),
        # so one entry's retries fail without failing other topics' publishes.
        self.fail_topics = set()
        self._last_info_request = None
        self._utc_deliver = False

    def is_connected(self):
        return self.connected

    def connect(self):
        return True

    def mark_disconnected(self):
        pass

    def status(self):
        return {"connected": self.connected, "connect_count": 1, "disconnect_count": 0}

    def publish_qos1(self, topic, message, splice_fragment=None):
        if self.fail_publishes:
            self.fail_publishes -= 1
            # A lost PUBACK is a socket timeout in the real client: a
            # transport failure (OSError), not a programming error.
            raise OSError("PUBACK timeout (simulated)")
        if topic in self.fail_topics:
            raise OSError("PUBACK timeout (simulated)")
        if splice_fragment is not None:
            # Mirror the client's segmented spliced write: the wire bytes
            # are the body minus its closing brace, comma, fragment, brace.
            message = message[:-1] + b"," + splice_fragment + b"}"
        self.published.append((topic, message, _FAKE_TIME.ticks_ms()))
        doc = json.loads(message)
        if doc.get("message_type") == "info_request":
            self._last_info_request = doc

    def publish_qos1_with_packet_id(self, topic, message, packet_id, timeout_ms=None):
        # Simulate a successful PUBACK match for the startup probes.
        self.published.append((topic, message, _FAKE_TIME.ticks_ms()))
        return True

    def get_next_packet_id(self):
        return 1

    def check_msg(self):
        if self.check_msg_error is not None:
            raise self.check_msg_error
        self.check_msg_calls += 1
        if self._utc_deliver and self._last_info_request is not None:
            self._utc_deliver = False
            request = self._last_info_request
            utc_epoch_ms = 1767225600000
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

    def ping_due(self):
        return False

    def ping(self):
        self.ping_calls += 1


@pytest.fixture
def make_core0():
    """Build a fresh Core0 with fake wifi/mqtt and a controllable, wrapping clock."""

    def _make(delay_ms=100):
        _FAKE_TIME._abs_ms = 0
        _FAKE_TIME.stop_after_ms = None
        _FAKE_TIME.sleep_calls = []
        _MACHINE.reset_calls = 0
        _install_mocks()
        # core0 (and uptime) bind time from sys.modules at import time;
        # reload in dependency order so this module's fakes are authoritative.
        importlib.reload(importlib.import_module("uptime"))
        importlib.reload(importlib.import_module("network_wait"))
        core0_mod = importlib.import_module("core0")
        importlib.reload(core0_mod)

        instance = core0_mod.Core0(
            InterCore(minimum_free_heap_bytes=65536),
            _load_core0_config(delay_ms),
            {"wifi_ssid": "test-ssid", "wifi_password": "test-password"},
            "test-runtime",
            0,
            FakeLed(),
                    ConfigManager(str(ROOT / "tests" / "fixtures" / "config.json")),
        )
        instance._wifi = FakeWifi()
        instance._mqtt = FakeMqtt(instance)
        return instance

    return _make


def _queue_telemetry(instance, value):
    """Admit one telemetry message to the real outbound queue (FIFO order)."""
    message = {
        "message_type": "telemetry",
        "uptime_ms": 0,
        "timestamp": None,
        "payload": {"value": value},
    }
    assert instance._intercore.outbound_queue.put_with_kind(
        KIND_TELEMETRY,
        serialize_and_validate_message(message),
        RETENTION_PRIORITY_TELEMETRY,
    )


def _utc_synchronized(instance):
    """Mark UTC as already acquired so the run loop stays on the outbound path."""
    instance._utc_snapshot = {
        "utc_epoch_ms": 1767225600000,
        "sync_uptime_ms": 0,
        "runtime_start_epoch_ms": 1767225600000,
    }


def _run_to(instance, stop_ms):
    """Run the Core 0 service loop until the fake clock reaches stop_ms."""
    _FAKE_TIME.stop_after_ms = stop_ms
    with pytest.raises(LoopStop):
        instance.run()


# --- The pacing gate itself ----------------------------------------------


def test_first_publish_is_immediately_eligible(make_core0):
    """No publish has completed yet: the gate is open, no delay before publish #1."""
    instance = make_core0()
    assert instance._last_mqtt_publish_completed_ms is None
    assert instance._mqtt_publish_ready() is True


def test_gate_opens_exactly_at_configured_delay(make_core0):
    """The delay is a minimum gap from COMPLETION: 99 ms is early, 100 is due."""
    instance = make_core0(delay_ms=100)
    _FAKE_TIME._abs_ms = 1000
    instance._note_mqtt_publish_completed()

    assert instance._mqtt_publish_ready() is False  # t=1000: 0 ms elapsed
    _FAKE_TIME._abs_ms = 1099
    assert instance._mqtt_publish_ready() is False  # 99 ms elapsed
    _FAKE_TIME._abs_ms = 1100
    assert instance._mqtt_publish_ready() is True   # 100 ms elapsed


def test_zero_delay_disables_pacing(make_core0):
    instance = make_core0(delay_ms=0)
    _FAKE_TIME._abs_ms = 5000
    instance._note_mqtt_publish_completed()
    assert instance._mqtt_publish_ready() is True


def test_gate_is_correct_across_tick_wraparound(make_core0):
    """The gate must use ticks_diff, not integer subtraction, past the wrap."""
    instance = make_core0(delay_ms=100)

    # A publish completes just before the 30-bit tick boundary...
    _FAKE_TIME._abs_ms = TICKS_PERIOD - 50
    instance._note_mqtt_publish_completed()

    # ...and the clock then wraps. 99 ms after the completion (a plain
    # now - last would be about -2**30 here) the gate is still closed...
    _FAKE_TIME._abs_ms = TICKS_PERIOD + 49
    assert instance._mqtt_publish_ready() is False
    # ...and it opens at 100 ms despite the wrap.
    _FAKE_TIME._abs_ms = TICKS_PERIOD + 50
    assert instance._mqtt_publish_ready() is True


# --- Completion recording: one source of truth for every publish path ----


def test_successful_queue_publish_records_completion(make_core0):
    instance = make_core0(delay_ms=100)
    _queue_telemetry(instance, 1)
    entry = instance._intercore.outbound_queue.take()
    assert instance._last_mqtt_publish_completed_ms is None

    instance._publish_entry(entry)

    assert instance._last_mqtt_publish_completed_ms == _FAKE_TIME.ticks_ms()
    assert instance._mqtt_publish_ready() is False
    instance._intercore.outbound_queue.complete_in_flight(entry)


def test_utc_request_publish_records_completion(make_core0):
    """The UTC request path updates the SAME pacing timestamp, not a new one."""
    instance = make_core0(delay_ms=100)

    instance._utc_send_request()

    assert instance._last_mqtt_publish_completed_ms == _FAKE_TIME.ticks_ms()
    assert instance._mqtt_publish_ready() is False


def test_network_probe_success_records_completion(make_core0):
    """A matched-probe PUBACK is a completed outbound publish: it paces too."""
    instance = make_core0(delay_ms=100)

    assert instance._perform_network_probe() is True

    assert instance._last_mqtt_publish_completed_ms == _FAKE_TIME.ticks_ms()
    assert instance._mqtt_publish_ready() is False


def test_failed_publish_records_no_completion(make_core0):
    """A publish without a PUBACK is not a completed publish: it paces nothing."""
    instance = make_core0(delay_ms=100)
    instance._mqtt.fail_publishes = 1
    _queue_telemetry(instance, 1)
    entry = instance._intercore.outbound_queue.take()

    with pytest.raises(OSError):
        instance._publish_entry(entry)

    assert instance._last_mqtt_publish_completed_ms is None
    assert instance._mqtt_publish_ready() is True


# --- Runtime behavior: progressive drain, FIFO, responsiveness ------------


def test_backlog_drains_progressively_fifo_not_in_a_burst(make_core0):
    """Queued entries publish one per interval in FIFO order, not back-to-back."""
    instance = make_core0(delay_ms=100)
    for value in range(3):
        _queue_telemetry(instance, value)
    _utc_synchronized(instance)

    _run_to(instance, 260)

    published = instance._mqtt.published
    values = [json.loads(m)["payload"]["value"] for _t, m, _now in published]
    times = [now for _t, _m, now in published]
    assert values == [0, 1, 2]  # FIFO order unchanged
    assert times[0] == 0       # first entry publishes immediately
    assert times[1] == 100     # second waits the full interval (not +99)
    assert times[2] == 200     # third the same
    assert all(b - a >= 100 for a, b in zip(times, times[1:]))


def test_run_loop_stays_responsive_while_gate_closed(make_core0):
    """A closed gate holds back publishes; it must not stall Core 0.

    A blocking sleep for the configured delay would show up as a >10 ms sleep and would stop the watchdog and command polling for the duration."""
    instance = make_core0(delay_ms=100)
    _queue_telemetry(instance, 1)
    _utc_synchronized(instance)
    # A publish just completed: the gate is closed for the next 100 ms.
    instance._last_mqtt_publish_completed_ms = _FAKE_TIME.ticks_ms()

    heartbeat_checks = {"count": 0}
    original_watch = instance._watch_core_1_heartbeat

    def counting_watch():
        heartbeat_checks["count"] += 1
        original_watch()

    instance._watch_core_1_heartbeat = counting_watch

    _run_to(instance, 160)

    published = instance._mqtt.published
    assert len(published) == 1
    assert published[0][2] == 100  # nothing at t=0..90; publish at the reopen
    # The loop kept passing while the gate was closed: the Core 1 watchdog
    # ran on every pass...
    assert heartbeat_checks["count"] >= 15
    # ...and MQTT command polling kept its own cadence...
    assert instance._mqtt.check_msg_calls >= 1
    # ...and the only sleeps in run() are the normal 10 ms steps: no blocking
    # sleep of the configured delay anywhere in the runtime path.
    assert max(_FAKE_TIME.sleep_calls) == 10


def test_run_loop_lets_a_message_handling_bug_escape(make_core0):
    """A programming failure inside inbound message handling is NOT an MQTT
    outage: it must escape run() to the top-level recovery boundary
    (main.py's controlled reset), instead of the loop swallowing it, marking
    the link down, reconnecting, and the broker redelivering the same QoS 1
    message into the same fault -- hiding the real defect."""
    instance = make_core0()
    _utc_synchronized(instance)
    instance._mqtt.check_msg_error = RuntimeError("bug in message handling")

    # If the loop swallowed the bug, it would keep running until the stop
    # mark (a LoopStop, which _run_to itself expects and would fail this
    # test); the bug must instead escape run() here.
    with pytest.raises(RuntimeError):
        _run_to(instance, 250)


def test_run_loop_treats_check_msg_transport_failure_as_outage(make_core0):
    """The contrast: a genuine transport failure on the receive path is an
    outage -- the run loop survives it (the next pass recovers the link)
    instead of escaping to the reset boundary."""
    instance = make_core0()
    _utc_synchronized(instance)
    instance._mqtt.check_msg_error = OSError("link stalled mid-packet")

    # _run_to raises if the loop died on anything other than the stop mark:
    # a swallowed OSError keeps the loop alive until it.
    _run_to(instance, 250)

    # The loop kept running across the failed polls (at t=100 and t=200)
    # and the machine was never reset for it.
    assert _MACHINE.reset_calls == 0


def test_core0_response_then_queued_telemetry_paced(make_core0):
    """A Core 0 command response and the next queued message are paced too."""
    instance = make_core0(delay_ms=100)
    _utc_synchronized(instance)
    instance._pending_core0_responses.append({
        "command_id": "cmd-1",
        "command": "reboot",
        "success": True,
        "targeted": False,
        "data": {},
    })
    _queue_telemetry(instance, 8)

    _run_to(instance, 220)

    published = instance._mqtt.published
    assert published[0][0] == instance._config["mqtt_topic_command_response"]
    assert published[1][0] == instance._config["mqtt_topic_telemetry"]
    assert published[0][2] == 0
    assert published[1][2] >= 100


def test_queued_message_then_utc_request_paced(make_core0):
    """The UTC request PUBLISH also waits behind a just-completed publish."""
    instance = make_core0(delay_ms=100)
    _queue_telemetry(instance, 9)
    # No snapshot yet: the request is due, so the gate is the only thing
    # that can hold it back once the telemetry has published.
    instance._mqtt._utc_deliver = True

    _run_to(instance, 220)

    published = instance._mqtt.published
    types = [json.loads(m).get("message_type") for _t, m, _n in published]
    assert types == ["telemetry", "info_request"]
    times = [n for _t, _m, n in published]
    assert times[0] == 0
    assert times[1] >= 100


def test_reboot_holds_for_publish_slot_then_resets(make_core0):
    """A pending reboot waits for its response slot: no early reset, no bypass.

    While the gate is closed the reboot stays pending and Core 0 keeps looping; once it opens the response publishes and the reboot sequence proceeds."""
    instance = make_core0(delay_ms=100)
    _utc_synchronized(instance)
    instance._pending_reboot = {
        "command_id": "rb-1",
        "command": "reboot",
        "targeted": False,
    }
    # A publish just completed: the gate is closed for the next 100 ms.
    instance._last_mqtt_publish_completed_ms = _FAKE_TIME.ticks_ms()

    # Response (when the gate reopens) + the existing 5 s grace sleep, then
    # machine.reset().
    with pytest.raises(_MachineReset):
        instance.run()

    published = instance._mqtt.published
    assert len(published) == 1
    assert published[0][0] == instance._config["mqtt_topic_command_response"]
    # The response waited for the slot (published at the interval boundary,
    # not at t=0 while the gate was closed)...
    assert published[0][2] == 100
    # ...and the reset only happens after it, with the pending flag cleared.
    assert _MACHINE.reset_calls == 1
    assert instance._pending_reboot is None


def test_reboot_holds_during_outage_then_acks_after_reconnect(make_core0):
    """A pending reboot survives an outage with zero publish attempts.

    The gate is the same conjunction as every other run-loop publish path:
    a down session is never attempted (it would fail fast, so the response
    would be rebuilt only to be thrown away), and the acknowledgement goes
    out on the first pass after the link returns. Recovery itself is
    suppressed: with the fake's always-success connect() it would clear the
    outage on the first pass, and the recovery path has its own suite."""
    instance = make_core0(delay_ms=100)
    _utc_synchronized(instance)
    instance._pending_reboot = {
        "command_id": "rb-1",
        "command": "reboot",
        "targeted": False,
    }
    instance._mqtt.connected = False
    instance._recover_network_if_needed = lambda: None

    attempts = {"n": 0}
    original_publish = instance._mqtt.publish_qos1

    def counting_publish(topic, message, splice_fragment=None):
        attempts["n"] += 1
        return original_publish(topic, message, splice_fragment=splice_fragment)

    instance._mqtt.publish_qos1 = counting_publish

    # Outage passes: nothing is attempted, the reboot stays held.
    _run_to(instance, 100)
    assert attempts["n"] == 0
    assert instance._pending_reboot is not None

    # Link returns: the ack publishes on the first pass and the reboot
    # proceeds (response + the existing 5 s grace sleep, then reset()).
    del instance._recover_network_if_needed
    instance._mqtt.connected = True
    _FAKE_TIME.stop_after_ms = None  # _run_to left the stop marker armed
    with pytest.raises(_MachineReset):
        instance.run()

    assert attempts["n"] == 1
    assert _MACHINE.reset_calls == 1
    assert instance._pending_reboot is None


# --- Startup: the same interval, and a bounded wait is allowed ------------


def test_startup_probe_is_immediate_when_gate_open(make_core0):
    """The first startup publish is not delayed by an unnecessary wait."""
    instance = make_core0(delay_ms=100)

    assert instance._perform_network_probe() is True

    assert instance._mqtt.published[0][2] == 0


def test_startup_probe_waits_for_publish_slot(make_core0):
    """A probe right after a completed publish honors the interval."""
    instance = make_core0(delay_ms=100)
    instance._last_mqtt_publish_completed_ms = _FAKE_TIME.ticks_ms()

    assert instance._perform_network_probe() is True

    assert instance._mqtt.published[0][2] >= 100


def test_startup_utc_request_respects_preceding_publish(make_core0):
    """The startup UTC request does not start inside the interval either."""
    instance = make_core0(delay_ms=100)
    # Probe #2 just completed: the gate is closed for the next 100 ms.
    instance._last_mqtt_publish_completed_ms = _FAKE_TIME.ticks_ms()
    instance._mqtt._utc_deliver = True

    assert instance._synchronize_utc_required() is True

    published = instance._mqtt.published
    assert len(published) == 1
    assert json.loads(published[0][1]).get("message_type") == "info_request"
    assert published[0][2] >= 100
    assert instance._utc_snapshot is not None


def test_startup_contract_keeps_five_second_stabilization(make_core0):
    """The contract order and the 5 s stabilization step are unchanged.

    Pacing only constrains WHEN a publish may begin: probe #1, the 5 s
    stabilization, probe #2, then the UTC request."""
    instance = make_core0(delay_ms=100)
    instance._mqtt._utc_deliver = True

    assert instance._verify_startup_contract() is True

    # The 5 s stabilization step is still there...
    assert 5000 in _FAKE_TIME.sleep_calls
    # ...and the contract order holds: probe #1 completes at t=0, and
    # probe #2 starts only after the stabilization.
    published = instance._mqtt.published
    types = [json.loads(m).get("message_type") for _t, m, _n in published]
    assert types == ["network_probe", "network_probe", "info_request"]
    times = [n for _t, _m, n in published]
    assert times[0] == 0
    assert times[1] >= 5000
