# test_network_wait_service.py - Core 1 watchdog serviced inside Wi-Fi/MQTT waits
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Host-side regression tests: the Core 1 watchdog fires *inside* network waits.

Wifi.connect() and Mqtt.connect() contain the longest monolithic waits on Core 0 (a 20 s per-attempt Wi-Fi observation window, retry backoffs of up to 40 s, whole sequences repeated forever). They now invoke an optional Core 0 servicing hook at every 100 ms wait slice; the hook models the Core 1 heartbeat watchdog. These tests pin:

* the hook is invoked repeatedly during the waits (not skipped);
* a stale heartbeat -- modeled as an exception, since on hardware machine.reset() never returns -- propagates out of connect() inside the first backoff cycle, long before the reconnect sequence has been exhausted;
* the exception is a BaseException so the connect loops' own except-Exception handlers cannot swallow it (the same modeling test_mqtt.py uses for HangDetected)."""

import pathlib
import sys
import time as real_time
import types
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


class StaleHeartbeatReset(BaseException):
    """Stand-in for machine.reset(): on hardware it never returns.

    Deriving from BaseException means the code under test's except-Exception handlers (the Wi-Fi and MQTT connect retry loops) cannot swallow it: a regression that stops servicing the wait fails here instead of looping on."""
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

    def status(self, key=None):
        # No key -> the association state; a key -> a probe such as RSSI
        # (the real WLAN API accepts both, as _StatusWLAN below does).
        return -50

    def ifconfig(self):
        return ("192.168.1.100", "255.255.255.0", "192.168.1.1", "8.8.8.8")


class _StatusWLAN:
    """A Wi-Fi radio whose no-arg status() reports a fixed association state.

    isconnected() never becomes True, so the only thing that can end the 20 s observation window early is the association state status() reports. The status_value attribute lets a test drive any state (terminal failure, still connecting, ...) and assert whether the attempt bails early or waits the full window. The STAT_* constants the firmware compares against live on the fake network module, as they do in MicroPython."""

    IF_STA = 0
    PM_NONE = 0
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


class _FailingClient:
    """A broker that never answers: every connect() attempt fails."""

    attempts = 0

    def __init__(self, client_id, broker, keepalive=0):
        type(self).attempts += 1
        # A real MQTTClient always has a sock attribute (None until
        # connected); _close_old_client() reads it on the next attempt.
        self.sock = None

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
# The STAT_* association states live on the network module in MicroPython,
# and the firmware reads them from there. Pinned to the real Pico W/Pico 2 W
# values: on that platform 3 is STAT_GOT_IP (success), not a failure.
_NETWORK_MODULE.STAT_IDLE = 0
_NETWORK_MODULE.STAT_CONNECTING = 1
_NETWORK_MODULE.STAT_WRONG_PASSWORD = -3
_NETWORK_MODULE.STAT_NO_AP_FOUND = -2
_NETWORK_MODULE.STAT_CONNECT_FAIL = -1
_NETWORK_MODULE.STAT_GOT_IP = 3
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


# --- Zero-delay waits --------------------------------------------------------
#
# Configuration validation permits zero-valued reconnect delays. A zero delay
# is a configured *immediate* retry: it must wait nothing. The previous
# implementation's max(int(delay * 10), 1) floor made it wait 100 ms and
# service the hook once anyway, disagreeing with the configuration.


def test_wifi_zero_delay_waits_nothing_and_services_nothing(ticks):
    """A zero-second delay sleeps no slice and drops no watchdog check."""
    service = _counting_service(ticks)
    wifi = wifi_mod.Wifi("test-ssid", "test-password", [0, 0], service)

    wifi._sleep_interruptible(0)

    assert ticks.now_ms == 0
    assert service.calls == []


def test_mqtt_zero_delay_waits_nothing_and_services_nothing(ticks, monkeypatch):
    """A zero-second delay sleeps no slice and drops no watchdog check."""
    monkeypatch.setattr(mqtt_mod, "MQTTClient", _FailingClient)
    service = _counting_service(ticks)
    mqtt = mqtt_mod.Mqtt(
        {
            "mqtt_broker_ip_address": "10.0.0.1",
            "mqtt_topic_command": "iot/v3/command",
            "mqtt_topic_info_response": "iot/v3/info-response",
            "mqtt_keepalive_sec": 30,
            "mqtt_broker_response_timeout_sec": 4,
            "mqtt_reconnect_delays_sec": [0, 0],
        },
        lambda message: None,
        service,
    )

    mqtt._sleep_interruptible(0)

    assert ticks.now_ms == 0
    assert service.calls == []


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
    _StatusWLAN.status_value = wifi_mod.network.STAT_WRONG_PASSWORD
    service = _counting_service(ticks)
    wifi = wifi_mod.Wifi("test-ssid", "test-password", list(_RECONNECT_DELAYS_SEC), service)

    assert wifi.connect() is False
    _assert_bailed_early(ticks, service)


def test_wifi_no_ap_bails_early(ticks, monkeypatch):
    """NO_AP_FOUND is terminal too: the attempt bails early, same as above."""
    monkeypatch.setattr(wifi_mod.network, "WLAN", _StatusWLAN)
    _StatusWLAN.status_value = wifi_mod.network.STAT_NO_AP_FOUND
    service = _counting_service(ticks)
    wifi = wifi_mod.Wifi("test-ssid", "test-password", list(_RECONNECT_DELAYS_SEC), service)

    assert wifi.connect() is False
    _assert_bailed_early(ticks, service)


def test_wifi_connecting_state_waits_full_window(ticks, monkeypatch):
    """A still-connecting state is NOT terminal: every attempt waits fully."""
    monkeypatch.setattr(wifi_mod.network, "WLAN", _StatusWLAN)
    _StatusWLAN.status_value = wifi_mod.network.STAT_CONNECTING
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


def test_wifi_got_ip_is_not_a_failure(ticks, monkeypatch):
    """STAT_GOT_IP is the Pico W *success* state (3): it is never terminal.

    The removed literal fallback (2, 3, 4) would have classified 3 as a
    failure and bailed the attempt early -- a connected radio being treated
    as one that had failed. It must instead wait the full observation window
    without an early exit (isconnected() on this fake never becomes True)."""
    monkeypatch.setattr(wifi_mod.network, "WLAN", _StatusWLAN)
    _StatusWLAN.status_value = wifi_mod.network.STAT_GOT_IP
    service = _counting_service(ticks)
    wifi = wifi_mod.Wifi("test-ssid", "test-password", list(_RECONNECT_DELAYS_SEC), service)

    assert wifi.connect() is False

    # No attempt bailed early: every one ran its full 20 s window, so the
    # elapsed time includes the whole observation budget, not just backoff.
    backoff_ms = sum(_RECONNECT_DELAYS_SEC[:-1]) * 1000
    observation_ms = len(_RECONNECT_DELAYS_SEC) * 200 * 100
    elapsed_ms = ticks.now_ms
    assert elapsed_ms >= backoff_ms + observation_ms - 2000
    assert len(service.calls) >= len(_RECONNECT_DELAYS_SEC) * 200


# --- Exception taxonomy on the Wi-Fi boundary -------------------------------
#
# Wi-Fi follows the taxonomy the MQTT boundary already established
# (MQTT_TRANSPORT_ERRORS): only a transport failure (OSError) is a link
# condition to retry, MemoryError escapes, and a programming failure
# (a deterministic AttributeError/TypeError, an unexpected API
# incompatibility) must escape to main.py's controlled-reset boundary.
# Before the fix, Wifi.connect()'s broad except-Exception converted a
# programming failure into just another retryable attempt -- Core0.
# establish_network() would then retry the same deterministic fault forever,
# and the observation helpers (is_connected, _current_status, snapshot)
# would mask the fault as False/None instead of surfacing it. The one
# deliberate broad catch is the optional PM_NONE power-management probe,
# whose compatibility fallback has no portable exception type.


class _TaxonomyWLAN:
    """A radio whose calls raise per-test-configured exceptions.

    Class attributes let a test pick the failure type (or absence) for one
    call; connect() associates when it is not told to fail, and isconnected()
    reports that association. config() fails by default (as the other fakes
    do), pinning the PM_NONE probe's tolerated broad catch."""

    IF_STA = 0
    PM_NONE = 0
    associated = False
    config_error = None
    connect_error = None
    isconnected_error = None
    status_error = None
    ifconfig_error = None

    def __init__(self, interface):
        pass

    def active(self, value):
        pass

    def config(self, **kwargs):
        error = type(self).config_error
        raise error if error is not None else AttributeError("PM unsupported")

    def connect(self, ssid, password):
        if type(self).connect_error is not None:
            raise type(self).connect_error
        type(self).associated = True

    def isconnected(self):
        if type(self).isconnected_error is not None:
            raise type(self).isconnected_error
        return type(self).associated

    def status(self, key=None):
        if type(self).status_error is not None:
            raise type(self).status_error
        return -50

    def ifconfig(self):
        if type(self).ifconfig_error is not None:
            raise type(self).ifconfig_error
        return ("192.168.1.100", "255.255.255.0", "192.168.1.1", "8.8.8.8")


def _wifi_wlan(monkeypatch, associated=False, **errors):
    """Install _TaxonomyWLAN with a single no-backoff attempt; return a Wifi.

    The class attributes are reset wholesale so tests never leak failures
    into each other, and the WLAN instance is pre-bound so the observation
    helpers can be exercised without a prior connect()."""
    _TaxonomyWLAN.associated = associated
    _TaxonomyWLAN.config_error = None
    _TaxonomyWLAN.connect_error = None
    _TaxonomyWLAN.isconnected_error = None
    _TaxonomyWLAN.status_error = None
    _TaxonomyWLAN.ifconfig_error = None
    for name, error in errors.items():
        setattr(_TaxonomyWLAN, name, error)
    monkeypatch.setattr(wifi_mod.network, "WLAN", _TaxonomyWLAN)
    wifi = wifi_mod.Wifi("test-ssid", "test-password", [0])
    wifi._wlan = _TaxonomyWLAN(0)
    return wifi


def test_wifi_connect_transport_failure_still_retries(monkeypatch):
    """An OSError from the radio is a link condition: the attempt fails and
    the sequence completes as a clean False (establish_network retries)."""
    wifi = _wifi_wlan(monkeypatch, connect_error=OSError("association refused"))
    assert wifi.connect() is False


def test_wifi_connect_memory_error_escapes(monkeypatch):
    """A MemoryError is not retried: it reaches the recovery boundary."""
    wifi = _wifi_wlan(monkeypatch, connect_error=MemoryError())
    with pytest.raises(MemoryError):
        wifi.connect()


def test_wifi_connect_programming_failure_escapes(monkeypatch):
    """A deterministic driver/programming failure must escape, not be
    swallowed and retried into the same fault forever."""
    wifi = _wifi_wlan(monkeypatch, connect_error=AttributeError("wlan.api.rename"))
    with pytest.raises(AttributeError):
        wifi.connect()


def test_wifi_pm_none_probe_failure_is_a_compatibility_fallback(monkeypatch):
    """Whatever type the optional PM_NONE probe raises, the attempt
    continues and a workable radio still connects (the one broad catch)."""
    wifi = _wifi_wlan(monkeypatch, config_error=RuntimeError("PM unsupported"))
    assert wifi.connect() is True


def test_wifi_is_connected_observation_taxonomy(monkeypatch):
    """A transient driver state error (OSError) reads as 'not connected',
    but a programming failure escapes instead of masking itself as False."""
    wifi = _wifi_wlan(monkeypatch, isconnected_error=OSError("link down"))
    assert wifi.is_connected() is False

    wifi = _wifi_wlan(monkeypatch, isconnected_error=TypeError("bad isconnected"))
    with pytest.raises(TypeError):
        wifi.is_connected()


def test_wifi_current_status_observation_taxonomy(monkeypatch):
    """An OSError from status() reads as 'unknown' (timeout governs); a
    programming failure escapes instead of being masked as None."""
    wifi = _wifi_wlan(monkeypatch, status_error=OSError("status unavailable"))
    assert wifi._current_status() is None

    wifi = _wifi_wlan(monkeypatch, status_error=AttributeError("no status"))
    with pytest.raises(AttributeError):
        wifi._current_status()


def test_wifi_snapshot_ifconfig_taxonomy(monkeypatch):
    """On a connected radio: an OSError from ifconfig leaves the snapshot
    fields unset; a programming failure escapes instead of being swallowed."""
    wifi = _wifi_wlan(monkeypatch, associated=True, ifconfig_error=OSError("ifconfig refused"))
    snapshot = wifi.snapshot(False)
    assert snapshot["wifi_connected"] is True
    assert snapshot["ip_address"] is None
    assert snapshot["rssi"] is not None

    wifi = _wifi_wlan(monkeypatch, associated=True, ifconfig_error=TypeError("bad ifconfig"))
    with pytest.raises(TypeError):
        wifi.snapshot(False)
