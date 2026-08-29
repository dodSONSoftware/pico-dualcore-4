# test_mqtt.py - Tests for MQTT keepalive (PINGREQ/PINGRESP) behavior
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import pathlib
import sys
import time as real_time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


class MockMachine:
    @staticmethod
    def unique_id():
        return b"\x01\x02\x03\x04\x05"


sys.modules['machine'] = MockMachine()

from mqtt_client import MQTTClient  # noqa: E402
from mqtt import Mqtt  # noqa: E402


class FakeTicks:
    """Controllable MicroPython-style monotonic clock."""

    def __init__(self):
        self.now_ms = 100000

    def ticks_ms(self):
        return self.now_ms

    def ticks_diff(self, end, start):
        return end - start

    def ticks_add(self, base, delta):
        return base + delta

    def sleep_ms(self, ms):
        pass

    def sleep(self, sec):
        pass


@pytest.fixture
def ticks(monkeypatch):
    fake = FakeTicks()
    for name in ("ticks_ms", "ticks_diff", "ticks_add"):
        monkeypatch.setattr(real_time, name, getattr(fake, name), raising=False)
    return fake


class HangDetected(Exception):
    """A read would block forever (infinite blocking, no data available).

    Models a MicroPython socket left in blocking mode with no timeout (the
    state ``setblocking(True)`` produces) while the peer stalls: in production
    that read never returns. The host suite cannot actually hang, so the mock
    raises a distinctive error -- a plain ``Exception``, deliberately NOT an
    ``OSError`` -- so a test that expects a bounded timeout fails when the
    code under test regresses to infinite blocking.
    """
    pass


class MockSocket:
    """In-memory broker socket with MicroPython blocking/timeout semantics.

    Records writes and serves scripted incoming bytes. ``setblocking(True)``
    is equivalent to ``settimeout(None)`` (infinite blocking) and
    ``setblocking(False)`` to non-blocking, matching MicroPython. A read with
    no data available returns b"" when non-blocking, raises OSError when a
    finite timeout is set (the timeout fired), and raises HangDetected when the
    socket is in infinite-blocking mode (how the production hang surfaces).
    """

    def __init__(self, incoming=b""):
        self.buffer = bytearray(incoming)
        self.written = bytearray()
        self.timeout_value = None  # None = infinite blocking (the default)
        self.nonblocking = False
        # When set, settimeout() raises this instead of installing a timeout,
        # so a test can model a socket/runtime error during timeout setup.
        self.settimeout_error = None

    def write(self, data, size=None):
        chunk = bytes(data[:size]) if size is not None else bytes(data)
        self.written += chunk
        return len(chunk)

    def read(self, n=None):
        if self.buffer:
            if n is None:
                data = bytes(self.buffer)
                self.buffer.clear()
                return data
            data = bytes(self.buffer[:n])
            del self.buffer[:n]
            return data
        # No data available: behavior depends on the socket mode.
        if self.nonblocking:
            return b""
        if self.timeout_value is None:
            raise HangDetected("read would block forever (infinite blocking)")
        raise OSError("read timeout")

    def settimeout(self, value):
        if self.settimeout_error is not None:
            raise self.settimeout_error
        self.timeout_value = value
        self.nonblocking = False

    def setblocking(self, flag):
        if flag:
            self.timeout_value = None
            self.nonblocking = False
        else:
            self.nonblocking = True

    def close(self):
        pass


class FakeClient:
    """Records Mqtt-level client interactions for scheduling tests."""

    def __init__(self):
        self.ping_calls = []
        self.publish_calls = []
        self.ping_error = None
        self.publish_error = None

    def ping(self, timeout_sec=None):
        self.ping_calls.append(timeout_sec)
        if self.ping_error is not None:
            raise self.ping_error

    def publish(self, *args, **kwargs):
        self.publish_calls.append((args, kwargs))
        if self.publish_error is not None:
            raise self.publish_error


def _mqtt(ticks, keepalive=30):
    config = {
        "mqtt_broker_ip_address": "10.0.0.1",
        "mqtt_topic_command": "iot/v3/command",
        "mqtt_topic_info_response": "iot/v3/info_response",
        "mqtt_keepalive_sec": keepalive,
        "mqtt_broker_response_timeout_sec": 4,
        "mqtt_reconnect_delays_sec": [1, 2],
    }
    return Mqtt(config, lambda topic, msg: None)


# ---------------------------------------------------------------------------
# MQTTClient.ping packet flow
# ---------------------------------------------------------------------------

def test_ping_sends_pingreq_and_waits_for_pingresp():
    client = MQTTClient("pico_test", "broker", keepalive=30)
    sock = MockSocket(incoming=b"\xd0\x00")  # PINGRESP
    client.sock = sock

    client.ping(timeout_sec=10)

    assert sock.written == b"\xc0\x00"
    assert sock.timeout_value is None  # restored after the wait


def test_ping_delivers_interleaved_publish_before_pingresp():
    seen = []
    client = MQTTClient("pico_test", "broker", keepalive=30)
    client.set_callback(lambda topic, msg: seen.append((topic, msg)))

    # PUBLISH (topic "t", payload "x") followed by PINGRESP
    publish = b"\x30\x04\x00\x01t\x78"
    sock = MockSocket(incoming=publish + b"\xd0\x00")
    client.sock = sock

    client.ping(timeout_sec=10)

    assert seen == [(b"t", b"x")]
    assert sock.buffer == b""


def test_ping_times_out_when_pingresp_never_arrives():
    client = MQTTClient("pico_test", "broker", keepalive=30)
    sock = MockSocket(incoming=b"")
    client.sock = sock

    with pytest.raises(OSError):
        client.ping(timeout_sec=10)

    assert sock.written == b"\xc0\x00"
    # Socket restored to the client's default (blocking) state.
    assert sock.timeout_value is None


# ---------------------------------------------------------------------------
# MQTTClient.publish QoS 1 PUBACK wait
# ---------------------------------------------------------------------------

def test_publish_qos1_waits_for_matching_puback_and_restores_timeout():
    client = MQTTClient("pico_test", "broker", keepalive=30)
    sock = MockSocket(incoming=b"\x40\x02\x00\x01")  # PUBACK for pid 1 (big-endian)
    client.sock = sock

    client.publish(b"t", b"x", qos=1, timeout_ms=4000)

    # PUBLISH frame with packet id 1, then no socket timeout left behind.
    assert bytes(sock.written) == b"\x32\x06\x00\x01t\x00\x01x"
    assert sock.timeout_value is None


def test_publish_qos1_times_out_when_puback_never_arrives():
    client = MQTTClient("pico_test", "broker", keepalive=30)
    sock = MockSocket(incoming=b"")
    client.sock = sock

    with pytest.raises(OSError):
        client.publish(b"t", b"x", qos=1, timeout_ms=4000)

    # The PUBLISH frame went out, the bounded wait gave up, and the socket
    # is restored to the client's default (blocking) state.
    assert bytes(sock.written) == b"\x32\x06\x00\x01t\x00\x01x"
    assert sock.timeout_value is None


def test_publish_qos1_ignores_non_matching_puback_and_keeps_waiting():
    client = MQTTClient("pico_test", "broker", keepalive=30)
    # PUBACK for pid 2 arrives first; the wait must skip it and take pid 1.
    sock = MockSocket(incoming=b"\x40\x02\x00\x02" + b"\x40\x02\x00\x01")
    client.sock = sock

    client.publish(b"t", b"x", qos=1, timeout_ms=4000)

    assert sock.buffer == b""
    assert sock.timeout_value is None


def test_publish_qos1_delivers_interleaved_publish_before_puback():
    seen = []
    client = MQTTClient("pico_test", "broker", keepalive=30)
    client.set_callback(lambda topic, msg: seen.append((topic, msg)))

    # PUBLISH (topic "t", payload "x") followed by PUBACK for pid 1 (big-endian)
    sock = MockSocket(incoming=b"\x30\x04\x00\x01t\x78" + b"\x40\x02\x00\x01")
    client.sock = sock

    client.publish(b"t", b"x", qos=1, timeout_ms=4000)

    assert seen == [(b"t", b"x")]
    assert sock.buffer == b""
    assert sock.timeout_value is None


# ---------------------------------------------------------------------------
# MQTTClient: a link that stalls after the first frame byte must time out
#
# These are the regression tests for wait_msg() clearing the caller's timeout
# with setblocking(True). In MicroPython that is settimeout(None), so once the
# broker sends the first byte and then stalls, the remaining reads block
# forever. The mock raises HangDetected (not OSError) to model that, so each
# test below fails against the old code and passes once the caller's timeout
# persists for the whole operation.
# ---------------------------------------------------------------------------

def test_publish_qos1_times_out_when_puback_stalls_after_opcode():
    """A PUBACK that stalls after its first byte must time out, not hang."""
    client = MQTTClient("pico_test", "broker", keepalive=30)
    sock = MockSocket(incoming=b"\x40")  # PUBACK opcode, then the link stalls
    client.sock = sock

    with pytest.raises(OSError):
        client.publish(b"t", b"x", qos=1, timeout_ms=4000)

    # The PUBLISH went out; the bounded wait gave up rather than blocking.
    assert bytes(sock.written) == b"\x32\x06\x00\x01t\x00\x01x"
    assert sock.timeout_value is None


def test_publish_qos1_times_out_when_puback_pid_stalls():
    """A PUBACK that stalls after its size byte must time out, not hang."""
    client = MQTTClient("pico_test", "broker", keepalive=30)
    sock = MockSocket(incoming=b"\x40\x02")  # opcode + size byte, pid stalls
    client.sock = sock

    with pytest.raises(OSError):
        client.publish(b"t", b"x", qos=1, timeout_ms=4000)

    assert bytes(sock.written) == b"\x32\x06\x00\x01t\x00\x01x"
    assert sock.timeout_value is None


def test_ping_times_out_when_pingresp_stalls_after_opcode():
    """A PINGRESP that stalls after its first byte must time out, not hang."""
    client = MQTTClient("pico_test", "broker", keepalive=30)
    sock = MockSocket(incoming=b"\xd0")  # PINGRESP opcode, then the link stalls
    client.sock = sock

    with pytest.raises(OSError):
        client.ping(timeout_sec=10)

    assert sock.written == b"\xc0\x00"
    assert sock.timeout_value is None


def test_subscribe_waits_for_suback_without_clearing_timeout():
    """subscribe() must not clear an established timeout on its SUBACK wait."""
    client = MQTTClient("pico_test", "broker", keepalive=30)
    client.set_callback(lambda topic, msg: None)
    # The connect path leaves a finite timeout in place; SUBACK for pid 1.
    sock = MockSocket(incoming=b"\x90\x03\x00\x01\x00")
    client.sock = sock
    sock.settimeout(4.0)

    client.subscribe(b"t", qos=1)

    assert sock.buffer == b""
    # The established finite timeout was NOT cleared by the SUBACK wait (the
    # old setblocking(True) turned it into infinite blocking). subscribe()
    # leaves the caller's timeout in place, as connect() expects.
    assert sock.timeout_value == 4.0


def test_check_msg_restores_blocking_mode_after_poll():
    """check_msg() must not leave the socket non-blocking after polling."""
    client = MQTTClient("pico_test", "broker", keepalive=30)
    seen = []
    client.set_callback(lambda topic, msg: seen.append((topic, msg)))

    # A full PUBLISH is available; check_msg polls it non-blocking.
    sock = MockSocket(incoming=b"\x30\x04\x00\x01t\x78")
    client.sock = sock

    client.check_msg()

    assert seen == [(b"t", b"x")]
    assert sock.buffer == b""
    # Polling must restore the blocking mode it temporarily cleared, so the
    # next operation's reads block (bounded by their own timeout) instead of
    # returning empty immediately.
    assert sock.nonblocking is False
    assert sock.timeout_value is None


# ---------------------------------------------------------------------------
# MQTTClient: a failed timeout establishment must fail the operation, not
# swallow it into an unbounded response wait
#
# Each of these operations installs a socket timeout that bounds its response
# wait (PUBACK / PINGRESP / CONNACK). If settimeout() errors while that
# timeout is being installed, the operation must fail immediately -- propagating
# into Core 0's recovery path -- instead of proceeding into a blocking wait
# with no timeout. The mock's settimeout_error makes settimeout() raise, and
# each test proves the response byte was never consumed, i.e. the wait was
# never entered.
# ---------------------------------------------------------------------------

def test_publish_qos1_fails_immediately_when_settimeout_raises():
    """A failed timeout install must abort publish() before the PUBACK wait."""
    client = MQTTClient("pico_test", "broker", keepalive=30)
    sock = MockSocket(incoming=b"\x40\x02\x00\x01")  # ready PUBACK: must stay queued
    sock.settimeout_error = OSError("settimeout failed")
    client.sock = sock

    with pytest.raises(OSError):
        client.publish(b"t", b"x", qos=1, timeout_ms=4000)

    # The PUBLISH frame went out, but the bounded wait was never entered: the
    # ready PUBACK is still queued and no mode was left behind.
    assert bytes(sock.written) == b"\x32\x06\x00\x01t\x00\x01x"
    assert sock.buffer == b"\x40\x02\x00\x01"
    assert sock.timeout_value is None


def test_ping_fails_immediately_when_settimeout_raises():
    """A failed timeout install must abort ping() before the PINGRESP wait."""
    client = MQTTClient("pico_test", "broker", keepalive=30)
    sock = MockSocket(incoming=b"\xd0\x00")  # ready PINGRESP: must stay queued
    sock.settimeout_error = OSError("settimeout failed")
    client.sock = sock

    with pytest.raises(OSError):
        client.ping(timeout_sec=10)

    # The failure happens before the PINGREQ write, and the PINGRESP wait is
    # never entered (the ready PINGRESP is still queued).
    assert sock.written == b""
    assert sock.buffer == b"\xd0\x00"
    assert sock.timeout_value is None


def test_connect_fails_when_settimeout_raises(monkeypatch):
    """A failed connection-timeout install must terminate the attempt before
    the unbounded CONNACK wait."""
    import mqtt_client
    sock = MockSocket()
    sock.settimeout_error = OSError("settimeout failed")
    monkeypatch.setattr(mqtt_client.socket, "socket", lambda *a, **k: sock)

    client = MQTTClient("pico_test", "broker")
    with pytest.raises(OSError):
        client.connect(timeout=4)

    # No bytes were exchanged and no CONNACK was read: the attempt died at
    # timeout establishment, before any I/O.
    assert sock.written == b""
    assert sock.buffer == b""


def test_subscribe_times_out_when_suback_never_arrives():
    """subscribe() relies on the caller's established timeout; a missing SUBACK
    must time out, not wait unbounded."""
    client = MQTTClient("pico_test", "broker", keepalive=30)
    client.set_callback(lambda topic, msg: None)
    sock = MockSocket(incoming=b"")  # no SUBACK
    client.sock = sock
    sock.settimeout(4.0)  # the finite timeout subscribe() relies on

    with pytest.raises(OSError):
        client.subscribe(b"t", qos=1)


# ---------------------------------------------------------------------------
# MQTTClient packet ID wrapping (1..65535, never 0, never above 65535)
# ---------------------------------------------------------------------------

def test_next_packet_id_increments_from_initial():
    client = MQTTClient("pico_test", "broker", keepalive=30)
    assert client.pid == 0
    assert client.next_packet_id() == 1
    assert client.next_packet_id() == 2
    assert client.next_packet_id() == 3


def test_next_packet_id_wraps_65534_to_65535():
    client = MQTTClient("pico_test", "broker", keepalive=30)
    client.pid = 65534
    assert client.next_packet_id() == 65535


def test_next_packet_id_wraps_65535_to_1():
    client = MQTTClient("pico_test", "broker", keepalive=30)
    client.pid = 65535
    assert client.next_packet_id() == 1


def test_next_packet_id_stays_within_valid_range():
    """The sequence never yields 0 or a value above 65535."""
    client = MQTTClient("pico_test", "broker", keepalive=30)
    client.pid = 65533
    seen = [client.next_packet_id() for _ in range(5)]
    assert seen == [65534, 65535, 1, 2, 3]
    for value in seen:
        assert 1 <= value <= 65535


def test_publish_qos1_uses_wrapped_packet_id():
    """publish() must draw a wrapped ID (65535 -> 1), never 0 or 65536."""
    client = MQTTClient("pico_test", "broker", keepalive=30)
    client.pid = 65535
    # PUBACK for the wrapped ID (1).
    sock = MockSocket(incoming=b"\x40\x02\x00\x01")
    client.sock = sock

    client.publish(b"t", b"x", qos=1, timeout_ms=4000)

    # PUBLISH frame carries the wrapped packet id 1, not 65536 or 0.
    assert bytes(sock.written) == b"\x32\x06\x00\x01t\x00\x01x"
    assert client.pid == 1


def test_subscribe_uses_wrapped_packet_id():
    """subscribe() must draw a wrapped ID (65535 -> 1), never 0 or 65536."""
    client = MQTTClient("pico_test", "broker", keepalive=30)
    client.set_callback(lambda topic, msg: None)
    client.pid = 65535
    # SUBACK for the wrapped ID (1), granted QoS 0.
    sock = MockSocket(incoming=b"\x90\x03\x00\x01\x00")
    client.sock = sock

    client.subscribe(b"t", qos=1)

    assert sock.buffer == b""
    assert client.pid == 1


def test_mqtt_get_next_packet_id_advances_and_wraps(ticks):
    """Mqtt.get_next_packet_id() consumes the ID via the shared helper."""
    mqtt = _mqtt(ticks)
    client = MQTTClient("pico_test", "broker", keepalive=30)
    client.pid = 65535
    mqtt._client = client

    assert mqtt.get_next_packet_id() == 1
    # The ID was advanced (not merely peeked), so it cannot be reused.
    assert client.pid == 1
    assert mqtt.get_next_packet_id() == 2


# ---------------------------------------------------------------------------
# Mqtt keepalive scheduling
# ---------------------------------------------------------------------------

def test_ping_due_is_false_when_not_connected(ticks):
    mqtt = _mqtt(ticks)
    ticks.now_ms += 10 * 60 * 1000
    assert mqtt.ping_due() is False


def test_ping_due_at_half_keepalive(ticks):
    mqtt = _mqtt(ticks, keepalive=30)
    mqtt._connected = True
    mqtt._client = FakeClient()

    # Broker tolerates 1.5 x keepalive (45 s); we ping at keepalive / 2 (15 s).
    ticks.now_ms += 14 * 1000
    assert mqtt.ping_due() is False
    ticks.now_ms += 2 * 1000
    assert mqtt.ping_due() is True


def test_ping_due_respects_zero_keepalive(ticks):
    mqtt = _mqtt(ticks, keepalive=0)
    mqtt._connected = True
    mqtt._client = FakeClient()
    ticks.now_ms += 10 * 60 * 1000
    assert mqtt.ping_due() is False


def test_successful_publish_defers_ping(ticks):
    mqtt = _mqtt(ticks, keepalive=30)
    mqtt._connected = True
    mqtt._client = FakeClient()

    ticks.now_ms += 60 * 1000
    assert mqtt.ping_due() is True

    # A PUBLISH is itself keepalive traffic: it resets the clock.
    mqtt.publish_qos1("iot/v3/telemetry", "{}")
    assert mqtt.ping_due() is False


def test_publish_qos1_binds_puback_wait_to_broker_response_timeout(ticks):
    mqtt = _mqtt(ticks)
    mqtt._connected = True
    client = FakeClient()
    mqtt._client = client

    mqtt.publish_qos1("iot/v3/telemetry", "{}")

    # A blackholed link must fail within the configured broker response
    # timeout so the Core 0 run loop's network recovery can fire.
    args, kwargs = client.publish_calls[0]
    assert kwargs.get("qos") == 1
    assert kwargs.get("timeout_ms") == 4 * 1000


def test_publish_qos1_timeout_marks_disconnected_and_raises(ticks):
    mqtt = _mqtt(ticks)
    mqtt._connected = True
    client = FakeClient()
    client.publish_error = OSError("PUBACK timeout")
    mqtt._client = client

    with pytest.raises(OSError):
        mqtt.publish_qos1("iot/v3/telemetry", "{}")

    assert mqtt.is_connected() is False
    assert mqtt._disconnect_count == 1


def test_ping_sends_pingreq_through_client_and_resets_clock(ticks):
    mqtt = _mqtt(ticks, keepalive=30)
    mqtt._connected = True
    client = FakeClient()
    mqtt._client = client

    ticks.now_ms += 60 * 1000
    assert mqtt.ping_due() is True

    mqtt.ping()

    # Wait is bounded by the cap (10 s), never the full keepalive window.
    assert client.ping_calls == [10]
    assert mqtt.ping_due() is False


def test_ping_raises_when_not_connected(ticks):
    mqtt = _mqtt(ticks)
    with pytest.raises(OSError):
        mqtt.ping()


def test_ping_failure_marks_disconnected(ticks):
    mqtt = _mqtt(ticks, keepalive=30)
    mqtt._connected = True
    client = FakeClient()
    client.ping_error = OSError("no PINGRESP")
    mqtt._client = client

    with pytest.raises(OSError):
        mqtt.ping()

    assert mqtt.is_connected() is False
    assert mqtt._disconnect_count == 1
