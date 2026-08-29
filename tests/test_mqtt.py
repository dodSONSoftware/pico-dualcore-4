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


class MockSocket:
    """In-memory broker socket: records writes, serves scripted incoming bytes.

    Reads raise OSError when the buffer is empty to simulate a socket timeout.
    """

    def __init__(self, incoming=b""):
        self.buffer = bytearray(incoming)
        self.written = bytearray()
        self.timeout_value = None

    def write(self, data, size=None):
        chunk = bytes(data[:size]) if size is not None else bytes(data)
        self.written += chunk
        return len(chunk)

    def read(self, n=None):
        if not self.buffer:
            raise OSError("read timeout")
        if n is None:
            data = bytes(self.buffer)
            self.buffer.clear()
            return data
        data = bytes(self.buffer[:n])
        del self.buffer[:n]
        return data

    def settimeout(self, value):
        self.timeout_value = value

    def setblocking(self, flag):
        pass

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
