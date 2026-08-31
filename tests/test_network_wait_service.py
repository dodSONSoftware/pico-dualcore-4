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


class _StatusWLAN:
    """A Wi-Fi radio whose no-arg status() reports a fixed association state.

    isconnected() never becomes True, so the only thing that can end the
    20 s observation window early is the association state status() reports.
    The class attribute `status_value` lets a test drive any state -- a
    terminal failure (WRONG_PASSWORD / NO_AP_FOUND / CONNECT_FAIL), a still
    connecting one (CONNECTING), ... -- and assert whether the attempt bails
    early or waits the full window. Exposes the STAT_* names the firmware
    reads (like PM_NONE).
    """

    IF_STA = 0
    PM_NONE = 0
    STAT_IDLE = 0
    STAT_CONNECTING = 1
    STAT_WRONG_PASSWORD = 2
    STAT_NO_AP_FOUND = 3
    STAT_CONNECT_FAIL = 4
    STAT_GOT_IP = 5
    status_value = 1  # default: CONNECTING (non-terminal -> full window)

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

    def status(self, key=None):
        # No key -> the association state; a key -> a probe such as RSSI.
        return type(self).status_value if key is None else -50

    def ifconfig(self):
        return ("192.168.1.100", "255.255.255.0", "192.168.1.1", "8.8.8.8")


class _BareStatusWLAN:
    """A radio that reports a status() value but exposes no STAT_* names.

    `status_value` is expected to be a documented MicroPython association
    state (e.g. 2 = WRONG_PASSWORD). Exercises the firmware's fallback to the
    documented values when the firmware build does not expose the STAT_*
    names on the WLAN object or class.
    """

    IF_STA = 0
    PM_NONE = 0
    status_value = 2  # documented MicroPython WRONG_PASSWORD

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

    def status(self, key=None):
        return type(self).status_value if key is None else -50

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


# --- Wi-Fi terminal-failure early-exit -------------------------------------
#
# Wifi.connect() should stop observing a wrong password, a missing AP, or a
# known connect failure (the radio's terminal association states) instead of
# waiting out the full 20 s window before the next attempt. The still-
# connecting state is NOT terminal and must keep waiting the window. These
# fakes report a fixed no-arg status() state so the only thing that can end
# the window early is the state itself.


def _counting_service(ticks):
    """A Core 0 servicing hook that records each invocation (no reset)."""
    calls = []

    def service():
        calls.append(ticks.now_ms)

    service.calls = calls
    return service


def _assert_bailed_early(ticks, service):
    """Elapsed time is backoff-only: no attempt consumed its 20 s window."""
    # The last attempt sleeps no trailing backoff, so the sequence sleeps the
    # sum of every delay but the last one; nothing else may have slept.
    backoff_ms = sum(_RECONNECT_DELAYS_SEC[:-1]) * 1000           # 120 s
    observation_ms = len(_RECONNECT_DELAYS_SEC) * 200 * 100       # 200 s if all ran
    elapsed_ms = ticks.now_ms
    assert backoff_ms <= elapsed_ms < backoff_ms + observation_ms // 2
    # Core 0 was still serviced (at least the pre-break slice of every attempt).
    assert len(service.calls) >= len(_RECONNECT_DELAYS_SEC)


def test_wifi_terminal_failure_bails_early(ticks, monkeypatch):
    """A terminal WLAN status ends each attempt without the full 20 s window."""
    monkeypatch.setattr(wifi_mod.network, "WLAN", _StatusWLAN)
    _StatusWLAN.status_value = _StatusWLAN.STAT_WRONG_PASSWORD
    service = _counting_service(ticks)
    wifi = wifi_mod.Wifi("test-ssid", "test-password", list(_RECONNECT_DELAYS_SEC), service)

    assert wifi.connect() is False
    _assert_bailed_early(ticks, service)


def test_wifi_no_ap_bails_early(ticks, monkeypatch):
    """NO_AP_FOUND is terminal too: the attempt bails early, same as above."""
    monkeypatch.setattr(wifi_mod.network, "WLAN", _StatusWLAN)
    _StatusWLAN.status_value = _StatusWLAN.STAT_NO_AP_FOUND
    service = _counting_service(ticks)
    wifi = wifi_mod.Wifi("test-ssid", "test-password", list(_RECONNECT_DELAYS_SEC), service)

    assert wifi.connect() is False
    _assert_bailed_early(ticks, service)


def test_wifi_connecting_state_waits_full_window(ticks, monkeypatch):
    """A still-connecting state is NOT terminal: every attempt waits fully."""
    monkeypatch.setattr(wifi_mod.network, "WLAN", _StatusWLAN)
    _StatusWLAN.status_value = _StatusWLAN.STAT_CONNECTING
    service = _counting_service(ticks)
    wifi = wifi_mod.Wifi("test-ssid", "test-password", list(_RECONNECT_DELAYS_SEC), service)

    assert wifi.connect() is False

    # Every attempt ran the full 20 s observation window (200 slices), unlike
    # the terminal-failure case which bailed on the first slice.
    backoff_ms = sum(_RECONNECT_DELAYS_SEC[:-1]) * 1000
    observation_ms = len(_RECONNECT_DELAYS_SEC) * 200 * 100
    elapsed_ms = ticks.now_ms
    assert backoff_ms + observation_ms - 2000 <= elapsed_ms
    assert len(service.calls) >= len(_RECONNECT_DELAYS_SEC) * 200


def test_wifi_terminal_failure_via_documented_values(ticks, monkeypatch):
    """Even with no STAT_* names, a documented terminal value bails early."""
    monkeypatch.setattr(wifi_mod.network, "WLAN", _BareStatusWLAN)
    _BareStatusWLAN.status_value = 2  # documented MicroPython WRONG_PASSWORD
    service = _counting_service(ticks)
    wifi = wifi_mod.Wifi("test-ssid", "test-password", list(_RECONNECT_DELAYS_SEC), service)

    assert wifi.connect() is False
    _assert_bailed_early(ticks, service)
