# test_network_wait_service.py - Core 1 watchdog serviced inside Wi-Fi/MQTT waits
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Host-side regression tests: the Core 1 watchdog fires *inside* network waits.

Wifi.connect() and Mqtt.connect() contain the longest monolithic waits on
Core 0 (a 20 s per-attempt Wi-Fi observation window, retry backoffs of up
to 40 s, whole sequences repeated forever). They now invoke an optional
Core 0 servicing hook at every 100 ms wait slice. These tests pin the
module-level contract, where the hook models the Core 1 heartbeat
watchdog:

* the hook is invoked repeatedly during the waits (not skipped);
* a stale heartbeat -- modeled as an exception, since on hardware
  machine.reset() never returns -- propagates out of connect() *inside*
  the first backoff cycle, long before the reconnect sequence has been
  exhausted;
* the exception is a BaseException so the connect loops' own
  ``except Exception`` handlers cannot swallow it (the same modeling
  test_mqtt.py uses for HangDetected).
"""

import pathlib
import sys
import time as real_time
import types
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


class StaleHeartbeatReset(BaseException):
    """Stand-in for machine.reset(): on hardware it never returns.

    Deriving from BaseException -- like HangDetected in test_mqtt.py --
    means the code under test's ``except Exception`` handlers (the Wi-Fi
    and MQTT connect retry loops) cannot swallow it: a regression that
    stops servicing the wait fails here instead of looping on.
    """
    pass


class FakeTicks:
    """Controllable MicroPython-style monotonic clock (sleeps advance it)."""

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


class _FakeWLAN:
    """A Wi-Fi radio whose association never completes."""

    IF_STA = 0
    PM_NONE = 0

    def __init__(self, interface):
        pass

    def active(self, value):
        pass

    def config(self, **kwargs):
        raise AttributeError("config unsupported")

    def connect(self, ssid, password):
        pass

    def isconnected(self):
        return False

    def status(self, key):
        return -50

    def ifconfig(self):
        return ("192.168.1.100", "255.255.255.0", "192.168.1.1", "8.8.8.8")


class _FailingClient:
    """A broker that never answers: every connect() attempt fails."""

    attempts = 0

    def __init__(self, client_id, broker, keepalive=0):
        type(self).attempts += 1

    def set_callback(self, callback):
        pass

    def connect(self, timeout=None):
        raise OSError("broker unreachable")

    def subscribe(self, topic, qos=0):
        pass


# The MicroPython stand-ins must be present before wifi/mqtt are imported
# (they bind `import time`/`network`/`machine` at import time, as do the
# other host suites in tests/).
_NETWORK_MODULE = types.ModuleType("network")
_NETWORK_MODULE.WLAN = _FakeWLAN
sys.modules["network"] = _NETWORK_MODULE
sys.modules["machine"] = types.SimpleNamespace(
    unique_id=lambda: b"\x01\x02\x03\x04\x05"
)
_DEBUG_MOCK = MagicMock()
_DEBUG_MOCK.DEBUG = False
sys.modules["debug"] = _DEBUG_MOCK

import mqtt as mqtt_mod  # noqa: E402
import wifi as wifi_mod  # noqa: E402

_RECONNECT_DELAYS_SEC = [5, 5, 5, 5, 10, 10, 20, 20, 40, 40]
_TIMEOUT_MS = 30000


@pytest.fixture
def ticks(monkeypatch):
    fake = FakeTicks()
    for name in ("ticks_ms", "ticks_diff", "ticks_add", "sleep_ms", "sleep"):
        monkeypatch.setattr(real_time, name, getattr(fake, name), raising=False)
    return fake


def _make_watchdog(ticks):
    """The service hook: a Core 1 heartbeat that goes stale at the timeout."""
    stamp_ms = ticks.now_ms
    calls = []

    def service():
        calls.append(ticks.now_ms)
        if ticks.now_ms - stamp_ms >= _TIMEOUT_MS:
            raise StaleHeartbeatReset()

    service.calls = calls
    return service


def test_wifi_connect_services_wait_and_watchdog_fires_inside_wait(ticks):
    """A stale heartbeat resets inside Wifi.connect(), before the sequence ends."""
    _FailingClient.attempts = 0
    service = _make_watchdog(ticks)
    wifi = wifi_mod.Wifi("test-ssid", "test-password", list(_RECONNECT_DELAYS_SEC), service)

    with pytest.raises(StaleHeartbeatReset):
        wifi.connect()

    # The hook was serviced throughout the waits (every 100 ms slice),
    # not skipped.
    assert len(service.calls) > 100
    # The reset fired at the 30 s staleness mark -- during the second
    # attempt's observation window -- long before the full sequence
    # (10 x 20 s of polling plus 160 s of backoffs) had even come close.
    elapsed_ms = ticks.now_ms
    assert elapsed_ms >= _TIMEOUT_MS
    assert elapsed_ms < len(_RECONNECT_DELAYS_SEC) * 200 * 100 + sum(_RECONNECT_DELAYS_SEC) * 1000


def test_mqtt_connect_services_wait_and_watchdog_fires_inside_wait(ticks, monkeypatch):
    """A stale heartbeat resets inside Mqtt.connect(), mid-backoff."""
    _FailingClient.attempts = 0
    monkeypatch.setattr(mqtt_mod, "MQTTClient", _FailingClient)
    service = _make_watchdog(ticks)
    mqtt = mqtt_mod.Mqtt(
        {
            "mqtt_broker_ip_address": "10.0.0.1",
            "mqtt_topic_command": "iot/v3/command",
            "mqtt_topic_info_response": "iot/v3/info-response",
            "mqtt_keepalive_sec": 30,
            "mqtt_broker_response_timeout_sec": 4,
            "mqtt_reconnect_delays_sec": list(_RECONNECT_DELAYS_SEC),
        },
        lambda message: None,
        service,
    )

    with pytest.raises(StaleHeartbeatReset):
        mqtt.connect()

    # The reset fired at the 30 s mark -- inside the backoff after the
    # fifth failed attempt -- with half the attempts never made and
    # 130 s of backoff never slept.
    elapsed_ms = ticks.now_ms
    assert elapsed_ms >= _TIMEOUT_MS
    assert elapsed_ms < sum(_RECONNECT_DELAYS_SEC) * 1000
    assert _FailingClient.attempts < len(_RECONNECT_DELAYS_SEC)
    assert len(service.calls) > 50
