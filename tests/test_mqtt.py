# test_mqtt.py - Tests for MQTT keepalive (PINGREQ/PINGRESP) behavior
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import errno
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

from mqtt_client import (  # noqa: E402
    MAX_INBOUND_PACKET_BYTES,
    MQTTClient,
    MQTTException,
    MQTTPubackTimeout,
)
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
    for name in ("ticks_ms", "ticks_diff", "ticks_add", "sleep"):
        monkeypatch.setattr(real_time, name, getattr(fake, name), raising=False)
    return fake


@pytest.fixture
def mock_select(monkeypatch):
    """Route check_msg()'s readiness poll at the readiness model (see above)."""
    import mqtt_client
    MockPoller.created_count = 0
    MockPoller.poll_calls = 0
    MockPoller.ipoll_calls = 0
    monkeypatch.setattr(mqtt_client, "select", MockSelect)


class HangDetected(BaseException):
    """A read would block forever (infinite blocking, no data available).

    Models a MicroPython socket left in blocking mode with no timeout (the
    state ``setblocking(True)`` produces) while the peer stalls: in production
    that read never returns. The host suite cannot actually hang, so the mock
    raises a distinctive error, deliberately NOT an ``OSError``. It derives
    from ``BaseException`` so the code under test's ``except Exception``
    handlers (e.g. Mqtt.connect()'s retry loop) cannot swallow it: a test
    that expects a bounded timeout fails hard when the code under test
    regresses to infinite blocking, even through a retry loop.
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
        self.closed = False
        # When set, a write attempted in infinite-blocking mode (no finite
        # timeout installed) would stall forever on a real blackholed link;
        # with a finite timeout installed the write is bounded and completes
        # (in-memory). Makes "the timeout is active before byte 1 of the
        # frame" observable: code that writes before installing its timeout
        # raises HangDetected instead.
        self.write_requires_timeout = False
        # When set, every write represents a stalled send on a blackholed
        # link: infinite-blocking -> HangDetected (the production wedge);
        # finite timeout -> OSError (the timeout fired, a bounded failure).
        self.write_stalls = False

    def write(self, data, size=None):
        if self.write_stalls:
            if self.timeout_value is None:
                raise HangDetected("write would block forever (no timeout)")
            # A bounded write failure: MicroPython raises OSError(ETIMEDOUT, ...)
            # when a finite-timeout socket operation times out. (A write-phase
            # timeout is an ordinary publish failure, never a PUBACK timeout.)
            raise OSError(errno.ETIMEDOUT, "write timeout (bounded by installed timeout)")
        if self.write_requires_timeout and self.timeout_value is None:
            raise HangDetected("write would block forever (no timeout)")
        # MicroPython sockets accept str writes (encoded); CPython does not,
        # so the mock encodes before storing.
        if isinstance(data, str):
            data = data.encode("utf-8")
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
        # A bounded read failure: MicroPython raises OSError(ETIMEDOUT, ...)
        # when a finite-timeout socket operation times out. This errno is what
        # mqtt_client's is_socket_timeout() keys on to distinguish a PUBACK-wait
        # timeout from a reset/closed/protocol error.
        raise OSError(errno.ETIMEDOUT, "read timeout")

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

    def connect(self, addr):
        # In-memory: no transport to stall. On the real socket this call is
        # bounded by the timeout installed before it.
        pass

    def close(self):
        self.closed = True


class MockPoller:
    """Models MicroPython's select.poll(): register a stream, poll non-blocking.

    Readiness is modeled from the MockSocket's buffer: a stream is readable
    exactly when it still has bytes to serve — the same "a packet has started
    arriving" condition check_msg() tests for. (The real socket object is what
    check_msg() registers; MockSocket has no fd, so it is modeled here.)

    Class-level counters let a test assert that check_msg() builds one poller
    per socket and reuses it (created_count), and that it polls through the
    allocation-free ipoll() path (ipoll_calls) rather than the list-returning
    poll() path (poll_calls).
    """

    created_count = 0
    poll_calls = 0
    ipoll_calls = 0

    def __init__(self):
        MockPoller.created_count += 1
        self._streams = []

    def register(self, obj, eventmask=None):
        self._streams.append(obj)

    def poll(self, timeout=-1):
        MockPoller.poll_calls += 1
        return [(obj, 1) for obj in self._streams if obj.buffer]

    def ipoll(self, timeout=-1, flags=0):
        # MicroPython's allocation-free variant: poll and return an iterator
        # that yields one (obj, event) tuple per ready stream instead of
        # materializing a result list (as the device's real ipoll() does).
        MockPoller.ipoll_calls += 1
        return iter((obj, 1) for obj in self._streams if obj.buffer)


class MockSelect:
    """Stand-in for the MicroPython select module behind check_msg's readiness poll."""

    POLLIN = 1

    @staticmethod
    def poll():
        return MockPoller()


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
    """PUBLISH frame sent, PUBACK never arrives: a dedicated PUBACK timeout.

    The frame is fully written, the firmware is waiting for its PUBACK, and the
    wait expires -- exactly the condition MQTTPubackTimeout (and thus the
    puback_timeout_count metric) exists to name. A plain OSError is no longer
    the right type here.
    """
    client = MQTTClient("pico_test", "broker", keepalive=30)
    sock = MockSocket(incoming=b"")
    client.sock = sock

    with pytest.raises(MQTTPubackTimeout):
        client.publish(b"t", b"x", qos=1, timeout_ms=4000)

    # The PUBLISH frame went out, the bounded wait gave up, and the socket
    # is restored to the client's default (blocking) state.
    assert bytes(sock.written) == b"\x32\x06\x00\x01t\x00\x01x"
    assert sock.timeout_value is None


def test_publish_qos1_timeout_active_before_first_publish_byte():
    """The QoS 1 timeout must be active before byte 1 of the PUBLISH frame.

    A link that stalls on a write in infinite-blocking mode would wedge
    Core 0 inside sock.write() forever — and with Core 0 wedged, its Core 1
    heartbeat check never runs, so no recovery path remains. The mock stalls
    any write attempted without a finite timeout (HangDetected, a
    BaseException that escapes the code under test's handlers), so the
    publish completing at all proves the timeout was installed before the
    first write and restored after the exchange.
    """
    client = MQTTClient("pico_test", "broker", keepalive=30)
    sock = MockSocket(incoming=b"\x40\x02\x00\x01")  # PUBACK for pid 1
    sock.write_requires_timeout = True
    client.sock = sock

    client.publish(b"t", b"x", qos=1, timeout_ms=4000)

    assert bytes(sock.written) == b"\x32\x06\x00\x01t\x00\x01x"
    # The socket was restored to the client's default (blocking) state.
    assert sock.timeout_value is None


def test_publish_qos1_write_stall_surfaces_as_bounded_error():
    """A stalled PUBLISH write must fail bounded, not wedge Core 0.

    A blackholed link whose send buffer stops draining wedges Core 0 inside
    sock.write() unless a finite timeout is active: with the timeout in place
    the failure is a bounded OSError that propagates through
    Mqtt.publish_qos1's disconnect into network recovery. The old code
    (timeout installed only after the writes) surfaces this as HangDetected
    instead.
    """
    client = MQTTClient("pico_test", "broker", keepalive=30)
    sock = MockSocket(incoming=b"")
    sock.write_stalls = True
    client.sock = sock

    with pytest.raises(OSError):
        client.publish(b"t", b"x", qos=1, timeout_ms=4000)

    # The stalled write was bounded and the socket left in its default state.
    assert sock.written == b""
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
    """A PUBACK that stalls after its first byte is a PUBACK-wait timeout.

    The frame is already fully written and the firmware is mid-way through
    reading its matching PUBACK when the link stalls: that is the PUBACK-wait
    phase timing out, so it is a MQTTPubackTimeout (counted in
    puback_timeout_count), not a generic OSError.
    """
    client = MQTTClient("pico_test", "broker", keepalive=30)
    sock = MockSocket(incoming=b"\x40")  # PUBACK opcode, then the link stalls
    client.sock = sock

    with pytest.raises(MQTTPubackTimeout):
        client.publish(b"t", b"x", qos=1, timeout_ms=4000)

    # The PUBLISH went out; the bounded wait gave up rather than blocking.
    assert bytes(sock.written) == b"\x32\x06\x00\x01t\x00\x01x"
    assert sock.timeout_value is None


def test_publish_qos1_times_out_when_puback_pid_stalls():
    """A PUBACK that stalls after its size byte is a PUBACK-wait timeout.

    The frame is fully written and the firmware is reading the PID bytes of its
    matching PUBACK when the link stalls: the PUBACK-wait phase timed out, so it
    is a MQTTPubackTimeout (counted in puback_timeout_count), not a generic
    OSError.
    """
    client = MQTTClient("pico_test", "broker", keepalive=30)
    sock = MockSocket(incoming=b"\x40\x02")  # opcode + size byte, pid stalls
    client.sock = sock

    with pytest.raises(MQTTPubackTimeout):
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


def test_check_msg_restores_blocking_mode_after_poll(mock_select):
    """check_msg() parses a ready packet and leaves the socket in blocking mode."""
    client = MQTTClient("pico_test", "broker", keepalive=30)
    seen = []
    client.set_callback(lambda topic, msg: seen.append((topic, msg)))

    # A full PUBLISH is available; the readiness poll sees it has started.
    sock = MockSocket(incoming=b"\x30\x04\x00\x01t\x78")
    client.sock = sock

    client.check_msg(4.0)

    assert seen == [(b"t", b"x")]
    assert sock.buffer == b""
    # The parse ran under a finite timeout and was restored to normal blocking
    # mode (not non-blocking, not infinite-blocking), so the next operation's
    # bounded wait is intact.
    assert sock.nonblocking is False
    assert sock.timeout_value is None


def test_check_msg_returns_none_when_no_packet_started(mock_select):
    """check_msg() returns None immediately when no packet has started."""
    client = MQTTClient("pico_test", "broker", keepalive=30)
    seen = []
    client.set_callback(lambda topic, msg: seen.append((topic, msg)))

    sock = MockSocket(incoming=b"")  # nothing arrived
    client.sock = sock

    assert client.check_msg(4.0) is None

    # The readiness poll consumed nothing and did not perturb the socket mode,
    # so the caller's next operation still sees its own bounded wait.
    assert seen == []
    assert sock.buffer == b""
    assert sock.written == b""
    assert sock.nonblocking is False
    assert sock.timeout_value is None


def test_check_msg_times_out_on_stalled_packet_instead_of_short_reading(mock_select):
    """A packet that has started but stalled must time out, not short-read.

    In non-blocking mode (the old behavior) the remaining reads return b"" and
    a corrupt empty frame is delivered to the callback. Under a finite timeout
    (the fix) the stalled read raises a bounded error instead, surfacing into
    network recovery.
    """
    client = MQTTClient("pico_test", "broker", keepalive=30)
    seen = []
    client.set_callback(lambda topic, msg: seen.append((topic, msg)))

    # PUBLISH opcode + remaining-length + 2 topic-length bytes, then the topic
    # body itself never arrives (a link that fragmented or stalled mid-packet).
    sock = MockSocket(incoming=b"\x30\x05\x00\x01")
    client.sock = sock

    with pytest.raises(OSError):
        client.check_msg(4.0)

    # No corrupt frame was delivered, and the socket was restored to blocking.
    assert seen == []
    assert sock.nonblocking is False
    assert sock.timeout_value is None


def test_check_msg_reuses_one_poller_and_uses_ipoll(mock_select):
    """check_msg() builds the readiness poller once per socket and reuses it.

    Rebuilding select.poll() (and a result list) on every ~100 ms call was
    pure GC churn on the hot path — ~350,000 poll objects over a 10-hour run.
    The fix creates the poller once and polls through the allocation-free
    ipoll() where available (MicroPython), so a whole connection produces a
    single poller and never the list-returning poll() path.
    """
    client = MQTTClient("pico_test", "broker", keepalive=30)
    client.set_callback(lambda topic, msg: None)
    sock = MockSocket(incoming=b"")
    client.sock = sock

    for _ in range(5):
        assert client.check_msg(4.0) is None

    # One poller for all five polls (not one per call), and every poll took
    # the allocation-free ipoll path rather than the list-returning poll path.
    assert MockPoller.created_count == 1
    assert MockPoller.ipoll_calls == 5
    assert MockPoller.poll_calls == 0


def test_check_msg_recreates_poller_when_socket_changes(mock_select):
    """A reconnect installs a new socket object, so the poller is rebuilt.

    A poller still registered on the old (closed) socket would poll the wrong
    stream. When client.sock is replaced, check_msg() must build a fresh
    poller bound to the new socket instead of reusing the stale one.
    """
    client = MQTTClient("pico_test", "broker", keepalive=30)
    client.set_callback(lambda topic, msg: None)

    old_sock = MockSocket(incoming=b"")
    client.sock = old_sock
    assert client.check_msg(4.0) is None
    assert MockPoller.created_count == 1

    # Reconnect: a brand-new socket object replaces the old one.
    client.sock = MockSocket(incoming=b"")
    assert client.check_msg(4.0) is None

    # The stale poller was discarded; a fresh one was built for the new socket.
    assert MockPoller.created_count == 2


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
    """A failed timeout install must abort publish() before byte 1 goes out."""
    client = MQTTClient("pico_test", "broker", keepalive=30)
    sock = MockSocket(incoming=b"\x40\x02\x00\x01")  # ready PUBACK: must stay queued
    sock.settimeout_error = OSError("settimeout failed")
    client.sock = sock

    with pytest.raises(OSError):
        client.publish(b"t", b"x", qos=1, timeout_ms=4000)

    # The timeout is installed before any PUBLISH write, so a failed install
    # aborts before byte 1 of the frame: nothing was transmitted, the ready
    # PUBACK is still queued, and no mode was left behind.
    assert sock.written == b""
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
# Inbound packet size limit: an oversized broker frame must drop the
# connection, not request a sock.read() that could exhaust Pico RAM
#
# A server-side bug (no hostile traffic required) can publish an oversized
# command/info response. Before the fix, wait_msg() handed the broker's
# remaining-length value straight to sock.read(sz). Now the packet is
# rejected before its payload is allocated, the socket is closed, and the
# failure surfaces into Mqtt's disconnect + Core 0's recovery path.
# ---------------------------------------------------------------------------

def test_wait_msg_rejects_oversized_inbound_packet():
    """A PUBLISH over the limit must fail before its payload is read."""
    client = MQTTClient("pico_test", "broker", keepalive=30)
    seen = []
    client.set_callback(lambda topic, msg: seen.append((topic, msg)))

    # Remaining length 32768 (> the 16 KiB limit); the bytes that "would be"
    # the frame follow and must never be consumed, buffered, or delivered.
    sock = MockSocket(incoming=b"\x30\x80\x80\x02" + b"\x00\x01t" + b"x" * 32760)
    client.sock = sock

    with pytest.raises(MQTTException):
        client.wait_msg()

    # No corrupt frame reached the callback, and the dead stream was dropped
    # so Core 0's recovery path reconnects instead of reading the garbage.
    assert seen == []
    assert sock.closed


def test_wait_msg_accepts_packet_at_inbound_limit():
    """A packet whose remaining length equals the limit is still delivered."""
    client = MQTTClient("pico_test", "broker", keepalive=30)
    seen = []
    client.set_callback(lambda topic, msg: seen.append((topic, msg)))

    # Remaining length == limit: topic "t" takes 2 + 1 bytes, leaving
    # limit - 3 payload bytes. 16384 encodes as the 4-byte varint \x80\x80\x01.
    payload = b"x" * (MAX_INBOUND_PACKET_BYTES - 3)
    sock = MockSocket(incoming=b"\x30\x80\x80\x01\x00\x01t" + payload)
    client.sock = sock

    client.wait_msg()

    assert seen == [(b"t", payload)]
    assert sock.closed is False


def test_wait_msg_rejects_remaining_length_longer_than_four_bytes():
    """More than four remaining-length bytes is a protocol violation."""
    client = MQTTClient("pico_test", "broker", keepalive=30)
    client.set_callback(lambda topic, msg: None)

    # PUBLISH opcode then five continuation-length bytes: the old unbounded
    # loop would keep reading the stream forever; the cap must fail instead.
    sock = MockSocket(incoming=b"\x30" + b"\xff" * 5)
    client.sock = sock

    with pytest.raises(MQTTException):
        client.wait_msg()

    # The corrupt stream was dropped the same way as an oversized one.
    assert sock.closed


def test_check_msg_oversized_packet_marks_disconnected(ticks, mock_select):
    """An oversized inbound frame must drop the connection for recovery."""
    mqtt = _mqtt(ticks)
    client = MQTTClient("pico_test", "broker", keepalive=30)
    client.set_callback(lambda topic, msg: None)
    sock = MockSocket(incoming=b"\x30\x80\x80\x02" + b"\x00\x01t" + b"x" * 32760)
    client.sock = sock
    mqtt._client = client
    mqtt._connected = True

    with pytest.raises(MQTTException):
        mqtt.check_msg()

    # The Mqtt layer marked the disconnect (Core 0's recovery reconnects)
    # and the socket was dropped rather than read.
    assert mqtt.is_connected() is False
    assert mqtt._disconnect_count == 1
    assert sock.closed


# ---------------------------------------------------------------------------
# Mqtt.connect() — the whole handshake is bounded by the broker response timeout
#
# Regression coverage for the lifecycle bug: Mqtt.connect() used to call
# MQTTClient.connect() with no timeout, leaving the socket in infinite-
# blocking mode, so a broker that accepted the TCP connection and then went
# silent wedged Core 0 forever on the CONNACK or SUBACK wait. The low-level
# unit tests above could not catch it because they install the timeout
# manually. These drive the real Mqtt.connect() end-to-end: a link that goes
# silent mid-handshake must fail the attempt within
# mqtt_broker_response_timeout_sec (OSError) — never as a hang (HangDetected,
# a BaseException that escapes Mqtt.connect()'s except Exception).
# ---------------------------------------------------------------------------

def _mock_broker_socket(monkeypatch, sock):
    """Route MQTTClient.connect() at the in-memory broker socket."""
    import mqtt_client
    monkeypatch.setattr(mqtt_client.socket, "socket", lambda *a, **k: sock)
    monkeypatch.setattr(
        mqtt_client.socket,
        "getaddrinfo",
        lambda *a, **k: [(None, None, None, ("127.0.0.1", 1883))],
    )


def test_mqtt_connect_times_out_when_connack_never_arrives(ticks, monkeypatch):
    """TCP connects, the broker never sends CONNACK: fail within the timeout."""
    sock = MockSocket(incoming=b"")  # link up, then silent
    _mock_broker_socket(monkeypatch, sock)
    mqtt = _mqtt(ticks)

    # A regression to unbounded blocking would raise HangDetected here
    # instead of returning False.
    assert mqtt.connect() is False
    assert mqtt.is_connected() is False
    assert mqtt._connect_count == 0


def test_mqtt_connect_times_out_when_suback_never_arrives(ticks, monkeypatch):
    """CONNACK and the first SUBACK arrive; the second SUBACK never does."""
    connack = b"\x20\x02\x00\x00"
    suback_first = b"\x90\x03\x00\x01\x00"  # SUBACK for pid 1 (command topic)
    sock = MockSocket(incoming=connack + suback_first)
    _mock_broker_socket(monkeypatch, sock)
    mqtt = _mqtt(ticks)

    # The CONNACK wait was bounded, the first SUBACK was granted, and the
    # second SUBACK wait timed out instead of blocking forever.
    assert mqtt.connect() is False
    assert mqtt.is_connected() is False
    assert mqtt._connect_count == 0


def test_mqtt_connect_subscribes_both_topics_and_restores_blocking(ticks, monkeypatch):
    """A complete handshake subscribes both topics and restores normal blocking."""
    incoming = (
        b"\x20\x02\x00\x00"       # CONNACK
        b"\x90\x03\x00\x01\x00"   # SUBACK pid 1 (command topic)
        b"\x90\x03\x00\x02\x00"   # SUBACK pid 2 (info response topic)
    )
    sock = MockSocket(incoming=incoming)
    _mock_broker_socket(monkeypatch, sock)
    mqtt = _mqtt(ticks)

    assert mqtt.connect() is True
    assert mqtt.is_connected() is True
    assert mqtt._connect_count == 1

    written = bytes(sock.written)
    assert b"iot/v3/command" in written
    assert b"iot/v3/info_response" in written
    assert sock.buffer == b""
    # The handshake's finite timeout is gone: the socket is back in normal
    # blocking mode for the run loop (each later operation bounds its own
    # wait).
    assert sock.timeout_value is None


# ---------------------------------------------------------------------------
# Mqtt reconnect cleanup: disposing an already-failed client must never
# write to the stalled socket
#
# Regression for the stale-cleanup hang that partially defeated the 0.4.16
# bounded-publish fix. A QoS 1 publish that stalls on a blackholed link
# fails bounded (the PUBLISH write or the PUBACK wait), but its finally
# restores the socket to infinite-blocking mode and the failed socket stays
# attached to the Mqtt. The next reconnect attempt used to dispose of that
# client by sending an MQTT DISCONNECT frame: a write on an already-failed
# link, in infinite-blocking mode, with no timeout. On a real blackholed
# link that write wedges Core 0 forever -- and with Core 0 wedged, its Core
# 1 heartbeat watchdog never runs either, so no recovery path remains. The
# cleanup must close the TCP socket directly and attempt no write at all.
# ---------------------------------------------------------------------------

def test_reconnect_cleanup_closes_stalled_socket_without_writing(ticks, monkeypatch):
    """After a publish failure, the reconnect cleanup must close without writing.

    write_stalls models the blackholed link: any send attempted in
    infinite-blocking mode (the state the failed socket is in after the
    publish's bounded exchange unwinds its timeout) raises HangDetected.
    HangDetected is a BaseException that escapes Mqtt.connect()'s
    ``except Exception``, so a regression to a DISCONNECT write fails this
    test hard instead of returning a silent connect failure.
    """
    mqtt = _mqtt(ticks)

    # An established session on a blackholed link.
    old_client = MQTTClient("pico_test", "broker", keepalive=30)
    old_sock = MockSocket(incoming=b"")  # link up, but no PUBACK ever arrives
    old_sock.write_stalls = True  # every send on this link stalls
    old_client.sock = old_sock
    mqtt._client = old_client
    mqtt._connected = True

    # The publish fails bounded (the 0.4.16 path) and the Mqtt layer marks
    # the disconnect, leaving the failed socket attached in infinite-blocking
    # mode -- the exact state the old cleanup then wrote a DISCONNECT into.
    with pytest.raises(OSError):
        mqtt.publish_qos1("iot/v3/telemetry", "{}")

    assert mqtt.is_connected() is False
    assert mqtt._disconnect_count == 1
    assert old_sock.timeout_value is None  # infinite-blocking again
    assert old_sock.written == b""  # the stalled frame write never got out

    # The reconnect now runs against a fresh broker socket; disposing the
    # old client must close its socket without attempting any write.
    new_sock = MockSocket(incoming=(
        b"\x20\x02\x00\x00"       # CONNACK
        b"\x90\x03\x00\x01\x00"   # SUBACK pid 1 (command topic)
        b"\x90\x03\x00\x02\x00"   # SUBACK pid 2 (info response topic)
    ))
    _mock_broker_socket(monkeypatch, new_sock)

    # Old code raised HangDetected here (DISCONNECT write into the stalled
    # socket); the fix closes the socket directly and the reconnect proceeds.
    assert mqtt.connect() is True
    assert mqtt.is_connected() is True

    # The old socket was closed directly and received no DISCONNECT frame
    # (b"\xe0\x00") or any other write: it is exactly as empty as the
    # failed publish left it.
    assert old_sock.closed is True
    assert old_sock.written == b""
    # The new session owns a fresh client and socket.
    assert mqtt._client is not old_client
    assert mqtt._client.sock is new_sock


# ---------------------------------------------------------------------------
# Mqtt-layer reliability metrics (the single source of truth)
#
# These drive the real Mqtt object and pin the seven runtime-lifetime metrics
# it owns: the five integer counters and the two last-duration scalars. The
# low-level client (MQTTClient) and the Core 0 layer both defer to Mqtt for
# these, so the semantics live here. The retry classification (which attempt is
# a retry of the same logical message) is the one thing Core 0 owns and passes
# in; the tests below pin that Mqtt honors it without inferring it from a
# packet id, topic, payload, time, or connection count.
# ---------------------------------------------------------------------------

_OK_HANDSHAKE = (
    b"\x20\x02\x00\x00"       # CONNACK
    b"\x90\x03\x00\x01\x00"   # SUBACK pid 1 (command topic)
    b"\x90\x03\x00\x02\x00"   # SUBACK pid 2 (info response topic)
)


def _mqtt_with_delays(ticks, delays):
    config = {
        "mqtt_broker_ip_address": "10.0.0.1",
        "mqtt_topic_command": "iot/v3/command",
        "mqtt_topic_info_response": "iot/v3/info_response",
        "mqtt_keepalive_sec": 30,
        "mqtt_broker_response_timeout_sec": 4,
        "mqtt_reconnect_delays_sec": delays,
    }
    return Mqtt(config, lambda topic, msg: None)


def _mock_broker_socket_script(monkeypatch, socks):
    """Route each MQTTClient.connect() at the next scripted broker socket."""
    import mqtt_client
    iterator = iter(socks)
    monkeypatch.setattr(
        mqtt_client.socket, "socket", lambda *a, **k: next(iterator)
    )
    monkeypatch.setattr(
        mqtt_client.socket,
        "getaddrinfo",
        lambda *a, **k: [(None, None, None, ("127.0.0.1", 1883))],
    )


def test_publish_attempt_and_retry_counting(ticks):
    """Attempts and retries are counted per actual low-level publish; the retry
    flag comes from Core 0 and is honored, not inferred here."""
    mqtt = _mqtt(ticks)
    mqtt._connected = True
    mqtt._client = FakeClient()

    # First attempt of message A.
    mqtt.publish_qos1("iot/v3/telemetry", "A")
    assert mqtt._publish_attempt_count == 1
    assert mqtt._publish_retry_count == 0

    # Retry of the SAME logical message A (Core 0 passes is_retry=True).
    mqtt.publish_qos1("iot/v3/telemetry", "A", is_retry=True)
    assert mqtt._publish_attempt_count == 2
    assert mqtt._publish_retry_count == 1

    # A different logical message B: an attempt, but not a retry of A.
    mqtt.publish_qos1("iot/v3/telemetry", "B")
    assert mqtt._publish_attempt_count == 3
    assert mqtt._publish_retry_count == 1


def test_puback_timeout_counted_at_mqtt_layer(ticks):
    """A PUBACK wait that expires after the frame was written is the one and
    only thing counted as a PUBACK timeout (precisely, not approximated)."""
    mqtt = _mqtt(ticks)
    client = MQTTClient("pico_test", "broker", keepalive=30)
    sock = MockSocket(incoming=b"")  # frame goes out, PUBACK never arrives
    client.sock = sock
    mqtt._client = client
    mqtt._connected = True

    with pytest.raises(MQTTPubackTimeout):
        mqtt.publish_qos1("iot/v3/telemetry", "{}")

    assert mqtt._publish_attempt_count == 1
    assert mqtt._publish_retry_count == 0
    assert mqtt._puback_timeout_count == 1
    assert mqtt.is_connected() is False


def test_write_timeout_is_not_puback_timeout(ticks):
    """A stalled PUBLISH frame WRITE is a publish failure, never a PUBACK
    timeout: the frame never left, so no PUBACK was ever owed."""
    mqtt = _mqtt(ticks)
    client = MQTTClient("pico_test", "broker", keepalive=30)
    sock = MockSocket(incoming=b"")
    sock.write_stalls = True  # the PUBLISH frame write itself stalls
    client.sock = sock
    mqtt._client = client
    mqtt._connected = True

    with pytest.raises(OSError):
        mqtt.publish_qos1("iot/v3/telemetry", "{}")

    assert mqtt._publish_attempt_count == 1
    assert mqtt._publish_retry_count == 0
    assert mqtt._puback_timeout_count == 0  # the key distinction
    assert mqtt.is_connected() is False


def test_connection_failure_counting(ticks, monkeypatch):
    """Three failed connection attempts then a success: 3 failures, and the
    first successful connection is NOT a reconnect."""
    _mock_broker_socket_script(
        monkeypatch,
        [MockSocket(incoming=b""), MockSocket(incoming=b""),
         MockSocket(incoming=b""), MockSocket(incoming=_OK_HANDSHAKE)],
    )
    mqtt = _mqtt_with_delays(ticks, [1, 2, 3, 4])

    assert mqtt.connect() is True

    assert mqtt._connect_count == 1
    assert mqtt._connection_failure_count == 3
    assert mqtt._reconnect_success_count == 0  # initial connection
    assert mqtt._last_reconnect_duration_ms == 0
    assert mqtt._last_outage_duration_ms == 0


def test_reconnect_success_counting(ticks, monkeypatch):
    """initial + two reconnects -> connect_count 3, reconnect_success 2."""
    import mqtt_client
    monkeypatch.setattr(
        mqtt_client.socket,
        "socket",
        lambda *a, **k: MockSocket(incoming=_OK_HANDSHAKE),
    )
    monkeypatch.setattr(
        mqtt_client.socket,
        "getaddrinfo",
        lambda *a, **k: [(None, None, None, ("127.0.0.1", 1883))],
    )
    mqtt = _mqtt(ticks)

    # Initial connection: not a reconnect.
    assert mqtt.connect() is True
    assert mqtt._connect_count == 1
    assert mqtt._reconnect_success_count == 0

    # Session lost -> reconnect #1.
    mqtt.mark_disconnected()
    assert mqtt.connect() is True
    assert mqtt._connect_count == 2
    assert mqtt._reconnect_success_count == 1

    # Session lost again -> reconnect #2.
    mqtt.mark_disconnected()
    assert mqtt.connect() is True
    assert mqtt._connect_count == 3
    assert mqtt._reconnect_success_count == 2


def test_repeated_mark_disconnected_does_not_restart_outage_timer(ticks):
    """Several recovery paths can mark the SAME outage; the outage clock starts
    at the first connected->disconnected transition and is never restarted."""
    mqtt = _mqtt(ticks)
    mqtt._connect_count = 1  # a connection had previously succeeded
    mqtt._connected = True

    ticks.now_ms = 1000
    mqtt.mark_disconnected()
    assert mqtt._outage_started_ms == 1000

    ticks.now_ms = 3000
    mqtt.mark_disconnected()  # a later path notices the same loss
    assert mqtt._outage_started_ms == 1000

    ticks.now_ms = 5000
    mqtt.mark_disconnected()
    assert mqtt._outage_started_ms == 1000


class _ClockAdvanceSocket(MockSocket):
    """Serves a scripted handshake, advancing the fake clock to a target.

    Models the real elapsed time between the last failed reconnect attempt and
    the successful handshake (backoff sleeps plus the successful connect),
    which a static fake clock cannot express mid-call. The first read -- the
    CONNACK, i.e. recovery completing -- is when the clock jumps to the success
    moment, so the durations measured on success span the whole recovery, not
    just the final handshake.
    """

    def __init__(self, ticks, target_ms, incoming):
        super().__init__(incoming=incoming)
        self._ticks = ticks
        self._target_ms = target_ms

    def read(self, n=None):
        self._ticks.now_ms = self._target_ms
        return super().read(n)


def test_reconnect_and_outage_duration_span_recovery(ticks, monkeypatch):
    """Reconnect duration spans its first attempt to success (not restarted per
    attempt); outage duration spans the connected->disconnected transition,
    including the Wi-Fi recovery time before the MQTT reconnect work began."""
    mqtt = _mqtt_with_delays(ticks, [1, 2, 3, 4])

    script = [
        MockSocket(incoming=_OK_HANDSHAKE),  # initial connect
        MockSocket(incoming=b""),            # reconnect attempt 1 (fails)
        MockSocket(incoming=b""),            # reconnect attempt 2 (fails)
        # Successful handshake; the clock jumps to the success moment (17000)
        # on its CONNACK read.
        _ClockAdvanceSocket(ticks, target_ms=17000, incoming=_OK_HANDSHAKE),
    ]
    _mock_broker_socket_script(monkeypatch, script)

    # Initial connection (not a reconnect): both durations stay zero.
    assert mqtt.connect() is True
    assert mqtt._connect_count == 1
    assert mqtt._reconnect_success_count == 0
    assert mqtt._last_reconnect_duration_ms == 0
    assert mqtt._last_outage_duration_ms == 0

    # The session is lost: the outage is detected at t=1000.
    ticks.now_ms = 1000
    mqtt.mark_disconnected()
    assert mqtt._outage_started_ms == 1000

    # Wi-Fi recovery (not MQTT reconnect work) takes time; the MQTT reconnect
    # work itself begins at t=5000.
    ticks.now_ms = 5000

    # Two failed reconnect attempts (with backoff between them), then the
    # successful handshake completes at t=17000.
    assert mqtt.connect() is True

    assert mqtt._connect_count == 2
    assert mqtt._reconnect_success_count == 1
    # Reconnect spans its first attempt (t=5000) to the success (t=17000) --
    # NOT restarted by the two failed attempts in between.
    assert mqtt._last_reconnect_duration_ms == 12000
    # Outage spans the connected->disconnected transition (t=1000) to the
    # success (t=17000), including the 4000ms of Wi-Fi recovery before the MQTT
    # reconnect work began.
    assert mqtt._last_outage_duration_ms == 16000


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
