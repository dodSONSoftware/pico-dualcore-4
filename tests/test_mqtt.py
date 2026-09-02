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

from mqtt_client import (  # noqa: E402
    MAX_INBOUND_PACKET_BYTES,
    MQTTClient,
    MQTTException,
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
    for name in ("ticks_ms", "ticks_diff", "ticks_add", "sleep_ms", "sleep"):
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

    Models a MicroPython socket left in blocking mode while the peer stalls. Deliberately NOT an OSError: it derives from BaseException so the code under test's except-Exception handlers cannot swallow it -- a test expecting a bounded timeout fails hard on a regression to infinite blocking."""
    pass


class MockSocket:
    """In-memory broker socket with MicroPython blocking/timeout semantics.

    setblocking(True) == settimeout(None) (infinite blocking); a read with no data returns b"" when non-blocking, raises OSError on a finite timeout, and raises HangDetected in infinite-blocking mode (how the production hang surfaces)."""

    def __init__(self, incoming=b""):
        self.buffer = bytearray(incoming)
        self.written = bytearray()
        self.timeout_value = None  # None = infinite blocking (the default)
        self.nonblocking = False
        # When set, settimeout() raises this instead of installing a timeout,
        # so a test can model a socket/runtime error during timeout setup.
        self.settimeout_error = None
        # When set, the restore of normal blocking mode (settimeout(None))
        # raises this instead: models a runtime that fails to restore socket
        # state after a bounded exchange, while installs still succeed.
        self.restore_error = None
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
            raise OSError("write timeout (bounded by installed timeout)")
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
        if n == 0:
            # Real sockets return b"" for read(0); the "no data" branches
            # below must not fire for a zero-length request.
            return b""
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
        if value is None and self.restore_error is not None:
            raise self.restore_error
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

    Readiness is modeled from the MockSocket's buffer (readable exactly when bytes remain). Class-level counters let a test assert one poller per socket (created_count) and the allocation-free ipoll() path (ipoll_calls) over the list-returning poll() path (poll_calls)."""

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
        self.check_msg_error = None

    def ping(self, timeout_sec=None):
        self.ping_calls.append(timeout_sec)
        if self.ping_error is not None:
            raise self.ping_error

    def publish(self, *args, **kwargs):
        self.publish_calls.append((args, kwargs))
        if self.publish_error is not None:
            raise self.publish_error

    def check_msg(self, timeout_sec=None):
        if self.check_msg_error is not None:
            raise self.check_msg_error


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


def test_publish_qos1_timeout_active_before_first_publish_byte():
    """The QoS 1 timeout must be active before byte 1 of the PUBLISH frame.

    The mock stalls any write attempted without a finite timeout (HangDetected), so the publish completing at all proves the timeout was installed before the first write and restored after the exchange."""
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

    With a finite timeout the failure is a bounded OSError that propagates through Mqtt.publish_qos1's disconnect into network recovery; the old code (timeout installed only after the writes) surfaces this as HangDetected."""
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

    In non-blocking mode (the old behavior) a corrupt empty frame is delivered to the callback; under a finite timeout the stalled read raises a bounded error instead, surfacing into network recovery."""
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

    Rebuilding select.poll() on every ~100 ms call was GC churn on the hot path; the fix polls through the allocation-free ipoll() where available, so a whole connection produces a single poller."""
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

    A poller still registered on the old (closed) socket would poll the wrong stream."""
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


def test_ping_fails_when_blocking_restore_raises():
    """A failed restore to blocking mode must fail ping, not be swallowed.

    The PINGRESP arrives, but the socket cannot be returned to blocking mode, which subsequent operations assume -- the failure must propagate into recovery."""
    client = MQTTClient("pico_test", "broker", keepalive=30)
    sock = MockSocket(incoming=b"\xd0\x00")  # ready PINGRESP: the wait succeeds
    sock.restore_error = OSError("restore failed")
    client.sock = sock

    with pytest.raises(OSError):
        client.ping(timeout_sec=10)


def test_publish_qos1_fails_when_blocking_restore_raises():
    """A failed restore to blocking mode must fail the publish, not be swallowed.

    The PUBACK arrives, but the socket cannot be returned to blocking mode: the publish must surface that failure into recovery instead of reporting success."""
    client = MQTTClient("pico_test", "broker", keepalive=30)
    sock = MockSocket(incoming=b"\x40\x02\x00\x01")  # ready PUBACK: the exchange completes
    sock.restore_error = OSError("restore failed")
    client.sock = sock

    with pytest.raises(OSError):
        client.publish(b"t", b"x", qos=1, timeout_ms=4000)


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
# Inbound packet internal consistency: a frame whose declared field lengths
# exceed its own remaining length must be dropped, not read
#
# The remaining-length cap above bounds the *total*, but the topic length
# (and the QoS 1/2 packet id) are declared by the frame itself. A corrupt
# stream — no hostile broker required — can therefore carry remaining
# length 2 with a 65535-byte topic length; the old code handed that
# declaration straight to sock.read() (a ~64 KiB allocation on a 256 KB
# device) and let the payload size go negative. Every variable-sized read
# is now validated against the bytes actually left in the frame.
# ---------------------------------------------------------------------------

def test_wait_msg_rejects_topic_length_exceeding_remaining_length():
    """A topic length larger than the frame's remaining length must abort
    before the topic is read, not request an oversized allocation."""
    client = MQTTClient("pico_test", "broker", keepalive=30)
    seen = []
    client.set_callback(lambda topic, msg: seen.append((topic, msg)))

    # Remaining length 2 declaring a 65535-byte topic: the frame claims far
    # more topic bytes than it carries. Those bytes are never present and
    # must never be read.
    sock = MockSocket(incoming=b"\x30\x02\xff\xff")
    client.sock = sock

    with pytest.raises(MQTTException):
        client.wait_msg()

    # No corrupt frame reached the callback, and the dead stream was
    # dropped so Core 0's recovery path reconnects instead of reading it.
    assert seen == []
    assert sock.closed


def test_wait_msg_rejects_qos1_publish_missing_packet_id():
    """A QoS 1 frame with no bytes left for the packet id must abort."""
    client = MQTTClient("pico_test", "broker", keepalive=30)
    client.set_callback(lambda topic, msg: None)

    # QoS 1 PUBLISH (0x32) with an empty topic: the packet id is required
    # by the QoS level but absent from the frame.
    sock = MockSocket(incoming=b"\x32\x02\x00\x00")
    client.sock = sock

    with pytest.raises(MQTTException):
        client.wait_msg()

    assert sock.closed


def test_wait_msg_rejects_frame_too_short_for_topic_length():
    """A remaining length below the 2-byte topic field is a violation."""
    client = MQTTClient("pico_test", "broker", keepalive=30)
    client.set_callback(lambda topic, msg: None)

    sock = MockSocket(incoming=b"\x30\x01")
    client.sock = sock

    with pytest.raises(MQTTException):
        client.wait_msg()

    assert sock.closed


def test_wait_msg_accepts_qos1_publish_with_empty_topic_and_payload():
    """A frame whose remaining length is fully consumed by its declared
    fields (empty topic, packet id, empty payload) is valid and delivered,
    with the PUBACK answering the consumed packet id."""
    client = MQTTClient("pico_test", "broker", keepalive=30)
    seen = []
    client.set_callback(lambda topic, msg: seen.append((topic, msg)))

    # Remaining length 4: topic length (2) + packet id (2). This is the
    # exact boundary — zero payload bytes remain after the fields.
    sock = MockSocket(incoming=b"\x32\x04\x00\x00\x01\x02")
    client.sock = sock

    client.wait_msg()

    assert seen == [(b"", b"")]
    assert sock.written == b"\x40\x02\x01\x02"
    assert sock.closed is False


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


def test_mqtt_connect_fails_when_blocking_restore_raises(ticks, monkeypatch):
    """A failed restore to blocking mode after the handshake must fail the connection attempt, not mark a broken link healthy.

    CONNACK and both SUBACKs arrive, but settimeout(None) fails: the attempt fails (and the retry loop's cleanup closes the socket) instead of reporting a connect success."""
    incoming = (
        b"\x20\x02\x00\x00"       # CONNACK
        b"\x90\x03\x00\x01\x00"   # SUBACK pid 1 (command topic)
        b"\x90\x03\x00\x02\x00"   # SUBACK pid 2 (info response topic)
    )
    sock = MockSocket(incoming=incoming)
    sock.restore_error = OSError("restore failed")
    _mock_broker_socket(monkeypatch, sock)
    mqtt = _mqtt(ticks)

    assert mqtt.connect() is False
    assert mqtt.is_connected() is False
    assert mqtt._connect_count == 0
    # The broken client is closed by the retry-loop cleanup, not kept as a
    # healthy session in a socket mode the later bounded waits cannot assume.
    assert sock.closed is True


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

    write_stalls models the blackholed link: any send in infinite-blocking mode raises HangDetected, a BaseException that escapes Mqtt.connect()'s except-Exception, so a regression to a DISCONNECT write fails this test hard."""
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


def test_publish_qos1_programming_failure_propagates_without_disconnect(ticks):
    """A non-transport failure during a publish is a bug, not an outage: it
    propagates WITHOUT marking the session down, so it reaches the
    top-level recovery boundary (main.py's controlled reset) instead of
    being reclassified as a network failure and retried into the same fault."""
    mqtt = _mqtt(ticks)
    mqtt._connected = True
    client = FakeClient()
    client.publish_error = ValueError("bug in frame construction")
    mqtt._client = client

    with pytest.raises(ValueError):
        mqtt.publish_qos1("iot/v3/telemetry", "{}")

    assert mqtt.is_connected() is True
    assert mqtt._disconnect_count == 0


def test_check_msg_transport_failure_marks_disconnected_and_raises(ticks):
    """A stalled or corrupt inbound stream is a link condition: it fails the
    poll, marks the session down, and propagates into network recovery."""
    mqtt = _mqtt(ticks)
    mqtt._connected = True
    client = FakeClient()
    client.check_msg_error = OSError("link stalled mid-packet")
    mqtt._client = client

    with pytest.raises(OSError):
        mqtt.check_msg()

    assert mqtt.is_connected() is False
    assert mqtt._disconnect_count == 1


def test_check_msg_callback_programming_failure_propagates_without_disconnect(ticks):
    """check_msg() delivers a ready packet to the message callback, so a bug
    in that callback is a programming failure, not a link condition: it must
    propagate WITHOUT marking the session down (to the top-level recovery
    boundary), instead of the loop reconnecting and the broker redelivering
    the same QoS 1 message into the same fault."""
    mqtt = _mqtt(ticks)
    mqtt._connected = True
    client = FakeClient()
    client.check_msg_error = RuntimeError("bug in the message callback")
    mqtt._client = client

    with pytest.raises(RuntimeError):
        mqtt.check_msg()

    assert mqtt.is_connected() is True
    assert mqtt._disconnect_count == 0


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


# ---------------------------------------------------------------------------
# Explicit protocol checks — MicroPython strips assert statements at bytecode
# optimization >= 1, so no protocol behavior may depend on them. Each of the
# old runtime asserts in mqtt_client.py must now raise the same failure
# explicitly, in both the default build and an optimized one.
# ---------------------------------------------------------------------------

def test_connect_rejects_malformed_connack(monkeypatch):
    """A CONNACK with the wrong opcode or length fails the attempt.

    The old `assert resp[0] == 0x20 and resp[1] == 0x02` vanished under
    bytecode optimization and the handshake would continue on a corrupt
    stream; it must now raise."""
    sock = MockSocket(incoming=b"\x30\x02\x00\x00")  # wrong opcode
    _mock_broker_socket(monkeypatch, sock)
    client = MQTTClient("pico_test", "broker", keepalive=30)

    with pytest.raises(MQTTException):
        client.connect(timeout=4.0)


def test_connect_rejects_keepalive_above_maximum(monkeypatch):
    """A keepalive over the 65535-second MQTT maximum is rejected, not silently
    encoded (the old assert was invisible under bytecode optimization)."""
    _mock_broker_socket(monkeypatch, MockSocket(incoming=b""))
    client = MQTTClient("pico_test", "broker", keepalive=65536)

    with pytest.raises(MQTTException):
        client.connect(timeout=4.0)


def test_subscribe_rejects_suback_with_wrong_packet_id():
    """A SUBACK carrying a different packet id fails the subscribe."""
    client = MQTTClient("pico_test", "broker", keepalive=30)
    client.set_callback(lambda topic, msg: None)
    # SUBACK for pid 0x99, but the client drew pid 1.
    sock = MockSocket(incoming=b"\x90\x03\x99\x00\x00")
    client.sock = sock

    with pytest.raises(MQTTException):
        client.subscribe(b"t", qos=1)


def test_subscribe_without_callback_raises():
    """Subscribing with no callback set fails instead of relying on an assert."""
    client = MQTTClient("pico_test", "broker", keepalive=30)
    sock = MockSocket()
    client.sock = sock

    with pytest.raises(MQTTException, match="callback"):
        client.subscribe(b"t", qos=1)

    assert sock.written == b""


def test_publish_qos1_rejects_puback_with_bad_length():
    """A PUBACK whose remaining length is not 2 aborts the exchange and closes
    the socket (the stream is corrupt from there on)."""
    client = MQTTClient("pico_test", "broker", keepalive=30)
    sock = MockSocket(incoming=b"\x40\x03\x01\x02\x03")  # length 3, not 2
    client.sock = sock

    with pytest.raises(MQTTException):
        client.publish(b"t", b"x", qos=1, timeout_ms=4000)

    assert sock.closed is True


def test_publish_qos2_is_rejected_before_any_write():
    """QoS 2 is not supported: the rejection must happen before a single byte
    is transmitted, in every build (the old `assert 0` was unreachable under
    bytecode optimization and the frame would simply go out unacked)."""
    client = MQTTClient("pico_test", "broker", keepalive=30)
    sock = MockSocket()
    client.sock = sock

    with pytest.raises(MQTTException, match="QoS 2"):
        client.publish(b"t", b"x", qos=2)

    assert sock.written == b""


def test_ping_rejects_pingresp_with_payload():
    """A PINGRESP is exactly 2 bytes; a non-zero remaining length is a corrupt
    stream and must fail the ping, not continue."""
    client = MQTTClient("pico_test", "broker", keepalive=30)
    sock = MockSocket(incoming=b"\xd0\x01\x00")
    client.sock = sock

    with pytest.raises(MQTTException):
        client.ping(timeout_sec=10)

    assert sock.closed is True


def test_wait_msg_rejects_inbound_qos2_before_callback():
    """An inbound QoS 2 PUBLISH is outside this client's protocol profile: it
    must be rejected at the wire layer, before the callback (which feeds the
    command protocol) ever sees the frame, and in every build (the old
    `assert 0` was build-dependent and the callback had already run)."""
    client = MQTTClient("pico_test", "broker", keepalive=30)
    callback_calls = []
    client.set_callback(lambda topic, msg: callback_calls.append((topic, msg)))
    # QoS 2 PUBLISH (op 0x34): topic "t", packet id 1, empty payload.
    # Remaining length 5 = topic length (2) + topic (1) + packet id (2).
    sock = MockSocket(incoming=b"\x34\x05\x00\x01t\x01\x00")
    client.sock = sock

    with pytest.raises(MQTTException, match="QoS 2"):
        client.wait_msg()

    assert callback_calls == []
    assert sock.closed is True


def test_wait_msg_rejects_invalid_qos3_publish_before_callback():
    """QoS 3 is invalid for PUBLISH by the MQTT spec: it must be rejected at
    the wire layer, before the callback (the old code delivered the frame and
    only noticed nothing to reject)."""
    client = MQTTClient("pico_test", "broker", keepalive=30)
    callback_calls = []
    client.set_callback(lambda topic, msg: callback_calls.append((topic, msg)))
    # QoS 3 PUBLISH (op 0x36): same frame shape as the QoS 2 case.
    sock = MockSocket(incoming=b"\x36\x05\x00\x01t\x01\x00")
    client.sock = sock

    with pytest.raises(MQTTException, match="Invalid PUBLISH QoS"):
        client.wait_msg()

    assert callback_calls == []
    assert sock.closed is True


def test_publish_size_above_remaining_length_maximum_is_rejected():
    """A publish whose frame exceeds MQTT's 2097151-byte remaining length is
    rejected instead of underflowing the length encoding loop."""
    client = MQTTClient("pico_test", "broker", keepalive=30)
    sock = MockSocket()
    client.sock = sock
    # sz = 2 + len(topic) + len(msg); with an empty topic, 2097150 payload
    # bytes puts sz exactly over the maximum.
    msg = b"x" * 2097150

    with pytest.raises(MQTTException):
        client.publish(b"", msg, qos=0)

    assert sock.written == b""


def test_set_last_will_validates_parameters():
    """Last-will parameter validation is explicit: QoS 2 (unsupported by this
    profile) and an empty topic are rejected; QoS 0/1 still configure."""
    client = MQTTClient("pico_test", "broker", keepalive=30)

    with pytest.raises(ValueError):
        client.set_last_will(b"t", b"m", qos=2)
    with pytest.raises(ValueError):
        client.set_last_will(b"", b"m", qos=0)

    client.set_last_will(b"lwt", b"offline", qos=1)
    assert client.lw_topic == b"lwt"
    assert client.lw_qos == 1
