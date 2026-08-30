# test_network_diagnostics.py - Host-side tests for the bounded reachability
# probes (network_diagnostics.py) and the Core 0 staged diagnostics
# scheduling (core0.py).
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

from config import split_config  # noqa: E402


class FakeTime:
    """Controllable stand-in for MicroPython's time module."""

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
        return getattr(_real_time, name)


_FAKE_TIME = FakeTime()
_MACHINE_MOCK = MagicMock()
_DEBUG_MOCK = MagicMock()
_DEBUG_MOCK.DEBUG = False
_WIFI_MOCK = MagicMock()
_MQTT_MOCK = MagicMock()

_AF_INET = 1
_SOCK_DGRAM = 2
_SOCK_RAW = 3


class _ProbeScript:
    """Scripted outcomes for the fake sockets (one probe at a time)."""

    def __init__(self):
        self.raw_reply = None  # bytes, None (timeout), or "memory"
        self.raw_delay_ms = 0
        self.udp_reply = None  # bytes, None (timeout), or "memory"
        self.udp_delay_ms = 0
        self.raw_sends = []
        self.udp_sends = []
        self.forbidden = []  # socket-module attributes the firmware must not need
        self.sockets = []


def _words_sum(data):
    total = 0
    for index in range(0, len(data), 2):
        total += (data[index] << 8) + (data[index + 1] if index + 1 < len(data) else 0)
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return total & 0xFFFF


class _FakeSocket:
    def __init__(self, script, mode):
        self._script = script
        self._mode = mode
        self.timeout_value = None
        self.closed = False
        script.sockets.append(self)

    def settimeout(self, value):
        # A finite, positive deadline (never None / blocking mode).
        assert isinstance(value, (int, float)) and value > 0
        self.timeout_value = value

    def sendto(self, data, addr):
        if self._mode == _SOCK_RAW:
            # A valid 24-byte echo request with the fixed identifier.
            assert len(data) == 24
            assert data[0] == 8 and data[1] == 0
            assert data[4] == 0x4E and data[5] == 0x44
            assert _words_sum(bytes(data)) == 0xFFFF
            self._script.raw_sends.append((bytes(data), addr))
        else:
            assert data[0] == 0x4E and data[1] == 0x44  # fixed transaction id
            assert addr[1] == 53  # DNS, and only DNS
            self._script.udp_sends.append((bytes(data), addr))

    def recvfrom(self, size):
        script = self._script
        if self._mode == _SOCK_RAW:
            reply, delay = script.raw_reply, script.raw_delay_ms
        else:
            reply, delay = script.udp_reply, script.udp_delay_ms
        if reply == "memory":
            raise MemoryError("heap exhausted (scripted)")
        if reply is None:
            raise OSError("recv timeout (scripted)")
        _FAKE_TIME.now_ms += delay
        return (reply, ("192.168.1.1", 0))

    def close(self):
        self.closed = True


class _FakeSocketModule:
    """Bare stand-in: only AF_INET/SOCK_DGRAM/socket exist. Any other
    attribute (e.g. getaddrinfo) is recorded as a violation."""

    AF_INET = _AF_INET
    SOCK_DGRAM = _SOCK_DGRAM

    def __init__(self, script):
        self._script = script

    def socket(self, family, mode, proto=0):
        return _FakeSocket(self._script, mode)

    def __getattr__(self, name):
        self._script.forbidden.append(name)
        raise AttributeError(name)


class _RawSocketModule(_FakeSocketModule):
    SOCK_RAW = _SOCK_RAW


class FakeLed:
    def __init__(self):
        self.states = []

    def set_connecting(self, value):
        self.states.append(bool(value))

    def telemetry_sent(self):
        pass


class FakeWifi:
    """Connected-state stand-in exposing the new diagnostics accessors."""

    def __init__(self):
        self.connected = False
        self.connect_calls = 0
        self.reconnect_triggers = []
        self.gateway = "192.168.1.1"
        self.dns = "10.10.10.53"

    def is_connected(self):
        return self.connected

    def connect(self):
        self.connected = True
        self.connect_calls += 1
        return True

    def ip_address(self):
        return "192.168.1.100" if self.connected else None

    def gateway_address(self):
        return self.gateway if self.connected else None

    def dns_address(self):
        return self.dns if self.connected else None

    def note_reconnect_trigger(self, trigger):
        self.reconnect_triggers.append(trigger)

    def snapshot(self, mqtt_connected):
        return {
            "ssid": "test-ssid" if self.connected else None,
            "ip_address": "192.168.1.100" if self.connected else None,
            "rssi": -50 if self.connected else None,
            "rssi_min_dbm": -70,
            "rssi_max_dbm": -40,
            "rssi_moving_average_dbm": -52,
            "rssi_sample_count": 3,
            "last_reconnect_duration_ms": 1200,
            "last_dhcp_acquisition_duration_ms": 450,
            "last_status_reason": "got_ip",
            "last_reconnect_trigger": "unknown",
            "association_details_supported": False,
            "bssid": None,
            "channel": None,
            "wifi_connect_count": self.connect_calls,
        }


class FakeMqtt:
    """Connected-state stand-in with a scriptable bounded probe exchange."""

    def __init__(self, core0_instance):
        self.core0 = core0_instance
        self.connected = False
        self.connect_calls = 0
        self.mark_disconnected_calls = 0
        self.published = []
        self.broker_probe_result = True
        self.broker_probe_delay_ms = 15
        self._packet_id = 0

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
        self._packet_id += 1
        return self._packet_id

    def publish_qos1_with_packet_id(
        self, topic, message, packet_id, timeout_ms=None, is_retry=False
    ):
        _FAKE_TIME.now_ms += self.broker_probe_delay_ms
        self.published.append((topic, message))
        return self.broker_probe_result


class RecordingMailboxes:
    def __init__(self):
        self.network_snapshots = []

    def set_network_snapshot(self, snapshot):
        self.network_snapshots.append(snapshot)


class MockInterCore:
    def __init__(self):
        self.state_mailboxes = RecordingMailboxes()
        self.outbound_queue = MagicMock()
        self.outbound_queue.has_in_flight.return_value = False
        self.outbound_queue.status.return_value = {"pending": 0}
        # The runtime-recovery path reads the queue depth to decide whether to
        # open a post-outage drain episode; an empty queue starts no episode.
        self.outbound_queue.get_depth.return_value = 0
        self.event_queue = MagicMock()
        self.memory_stats = MagicMock()
        self.minimum_free_heap_bytes = 65536


def _load_core0_config():
    config = json.loads((ROOT / "config.json").read_text())
    core0_config, _core1_config, _bus_config = split_config(config)
    return core0_config


# A valid ICMP echo reply for the fixed identifier (type 0 / code 0).
_ICMP_ECHO_REPLY = bytes([0, 0, 0, 0, 0x4E, 0x44, 0, 1])
# A valid DNS reply: matching transaction id, QR bit set, RCODE NXDOMAIN (3).
# NXDOMAIN proves the server answered (the name is intentionally not found).
_DNS_NXDOMAIN_REPLY = b"\x4e\x44\x81\x83\x00\x01\x00\x00\x00\x00\x00\x02"


@pytest.fixture
def make_core0():
    """Build a fresh Core0 with faked wifi/mqtt/LED, a controllable clock,
    and a scripted fake socket for the reachability probes.

    The factory is a plain function returning (instance, script); the
    sys.modules stand-ins are installed per test and restored after it.
    """
    _FAKE_TIME.now_ms = 0
    saved = {
        name: sys.modules.get(name)
        for name in ("time", "machine", "debug", "wifi", "mqtt", "socket")
    }
    sys.modules["time"] = _FAKE_TIME
    sys.modules["machine"] = _MACHINE_MOCK
    sys.modules["debug"] = _DEBUG_MOCK
    sys.modules["wifi"] = _WIFI_MOCK
    sys.modules["mqtt"] = _MQTT_MOCK

    def _make(config_overrides=None, raw_supported=False):
        script = _ProbeScript()
        socket_module = (
            _RawSocketModule(script) if raw_supported else _FakeSocketModule(script)
        )
        sys.modules["socket"] = socket_module
        importlib.reload(importlib.import_module("uptime"))
        importlib.reload(importlib.import_module("network_diagnostics"))
        core0_mod = importlib.import_module("core0")
        importlib.reload(core0_mod)

        config = _load_core0_config()
        if config_overrides:
            config.update(config_overrides)
        instance = core0_mod.Core0(
            MockInterCore(),
            config,
            {"wifi_ssid": "test-ssid", "wifi_password": "test-password"},
            "test-runtime",
            0,
            FakeLed(),
        )
        instance._wifi = FakeWifi()
        instance._mqtt = FakeMqtt(instance)
        return (instance, script)

    yield _make
    for name, module in saved.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


def _set_stable(instance):
    instance._network_stack_ready = True
    instance._wifi.connected = True
    instance._mqtt.connected = True


def _probe_module():
    return sys.modules["network_diagnostics"]


def _probe_topics(instance):
    return [
        topic
        for topic, _ in instance._mqtt.published
        if topic == instance._config["mqtt_topic_network_probe"]
    ]


# --- Bounded probe helpers -------------------------------------------------


def test_gateway_probe_unsupported_reports_not_testable_and_never_fakes_udp(make_core0):
    instance, script = make_core0(raw_supported=False)
    module = _probe_module()
    assert module.icmp_echo_supported() is False
    assert module.probe_gateway("192.168.1.1") == (False, None)
    # Not tested, not unreachable — and never faked with a UDP send either.
    assert script.raw_sends == []
    assert script.udp_sends == []


def test_gateway_probe_success_reports_bounded_latency(make_core0):
    instance, script = make_core0(raw_supported=True)
    script.raw_reply = _ICMP_ECHO_REPLY
    script.raw_delay_ms = 12
    module = _probe_module()
    assert module.probe_gateway("192.168.1.1") == (True, 12)
    assert len(script.raw_sends) == 1  # exactly one echo request
    assert all(sock.closed for sock in script.sockets)


def test_gateway_probe_timeout_returns_not_reachable(make_core0):
    instance, script = make_core0(raw_supported=True)
    module = _probe_module()
    assert module.probe_gateway("192.168.1.1") == (False, None)
    assert all(sock.closed for sock in script.sockets)


def test_gateway_probe_rejects_mismatched_identifier(make_core0):
    instance, script = make_core0(raw_supported=True)
    script.raw_reply = bytes([0, 0, 0, 0, 0x00, 0x00, 0, 1])
    module = _probe_module()
    assert module.probe_gateway("192.168.1.1") == (False, None)


def test_dns_probe_nxdomain_reply_proves_the_server_reachable(make_core0):
    instance, script = make_core0()
    script.udp_reply = _DNS_NXDOMAIN_REPLY
    script.udp_delay_ms = 8
    module = _probe_module()
    assert module.probe_dns_server("10.10.10.53") == (True, 8)
    assert len(script.udp_sends) == 1  # exactly one query
    assert all(sock.closed for sock in script.sockets)


def test_dns_probe_rejects_mismatched_transaction_id(make_core0):
    instance, script = make_core0()
    script.udp_reply = b"\x00\x01\x81\x83\x00\x01"
    module = _probe_module()
    assert module.probe_dns_server("10.10.10.53") == (False, None)


def test_dns_probe_timeout_returns_not_reachable(make_core0):
    instance, script = make_core0()
    module = _probe_module()
    assert module.probe_dns_server("10.10.10.53") == (False, None)
    assert all(sock.closed for sock in script.sockets)


def test_dns_probe_never_uses_getaddrinfo(make_core0):
    instance, script = make_core0()
    script.udp_reply = _DNS_NXDOMAIN_REPLY
    module = _probe_module()
    module.probe_dns_server("10.10.10.53")
    assert "getaddrinfo" not in script.forbidden


def test_probe_memory_error_propagates(make_core0):
    instance, script = make_core0()
    script.udp_reply = "memory"
    module = _probe_module()
    with pytest.raises(MemoryError):
        module.probe_dns_server("10.10.10.53")


# --- Core 0 staged scheduling ----------------------------------------------


def test_diagnostics_disabled_when_interval_is_zero(make_core0):
    instance, script = make_core0(
        config_overrides={"network_diagnostics_interval_sec": 0},
        raw_supported=True,
    )
    _set_stable(instance)
    for _ in range(5):
        _FAKE_TIME.now_ms += 100000
        instance._service_network_diagnostics()

    assert instance._netdiag_run_count == 0
    assert script.raw_sends == []
    assert script.udp_sends == []
    # Still reported as "not tested" (null), never as "unreachable".
    assert instance._gateway_reachability_supported is False
    assert instance._gateway_reachable is None
    assert instance._gateway_last_latency_ms is None

    instance._publish_network_snapshot(force=True)
    snap = instance._intercore.state_mailboxes.network_snapshots[-1]
    assert snap["network_diagnostics_run_count"] == 0
    assert snap["network_diagnostics_last_run_age_ms"] is None


def test_one_probe_stage_per_run_loop_pass_and_cycle_counted_once(make_core0):
    instance, script = make_core0(
        config_overrides={"network_diagnostics_interval_sec": 60},
        raw_supported=True,
    )
    _set_stable(instance)
    instance._service_network_diagnostics()  # first pass arms the deadline
    assert instance._netdiag_run_count == 0
    assert script.raw_sends == []

    _FAKE_TIME.now_ms = 60000
    instance._service_network_diagnostics()  # pass 2: GATEWAY stage only
    assert instance._netdiag_run_count == 0
    assert len(script.raw_sends) == 1
    assert script.udp_sends == []

    instance._service_network_diagnostics()  # pass 3: DNS stage -> complete
    assert instance._netdiag_run_count == 1
    assert len(script.udp_sends) == 1
    # Broker latency is disabled by default: no QoS 1 probe on this path.
    assert _probe_topics(instance) == []


def test_diagnostics_deferred_when_unstable_and_partial_cycle_discarded(make_core0):
    instance, script = make_core0(
        config_overrides={"network_diagnostics_interval_sec": 60},
        raw_supported=True,
    )
    _set_stable(instance)
    instance._service_network_diagnostics()  # arms the deadline
    instance._service_network_diagnostics()  # not due yet
    assert script.raw_sends == []
    assert instance._netdiag_run_count == 0

    _FAKE_TIME.now_ms = 60000
    instance._wifi.connected = False  # due, but the network is down
    instance._service_network_diagnostics()
    assert script.raw_sends == []
    assert instance._netdiag_run_count == 0

    # Stable again: the gateway stage runs on the next pass.
    instance._wifi.connected = True
    instance._service_network_diagnostics()
    assert len(script.raw_sends) == 1
    assert instance._netdiag_run_count == 0

    # Mid-cycle instability: the partial cycle is discarded, not counted.
    instance._mqtt.connected = False
    instance._service_network_diagnostics()
    assert instance._netdiag_run_count == 0
    assert script.udp_sends == []

    # After a full interval the cycle starts over from the gateway stage.
    instance._mqtt.connected = True
    _FAKE_TIME.now_ms += 60000
    instance._service_network_diagnostics()
    assert len(script.raw_sends) == 2
    assert instance._netdiag_run_count == 0


def test_broker_round_trip_recorded_only_when_idle_and_enabled(make_core0):
    instance, script = make_core0(
        config_overrides={
            "network_diagnostics_interval_sec": 60,
            "network_diagnostics_broker_latency_enabled": True,
        },
        raw_supported=True,
    )
    _set_stable(instance)
    instance._mqtt.broker_probe_delay_ms = 15

    instance._service_network_diagnostics()  # arms
    _FAKE_TIME.now_ms = 60000
    instance._service_network_diagnostics()  # GATEWAY
    instance._service_network_diagnostics()  # DNS
    instance._service_network_diagnostics()  # BROKER (idle) -> complete

    assert instance._netdiag_run_count == 1
    assert instance._mqtt_broker_last_round_trip_ms == 15
    assert len(_probe_topics(instance)) == 1


def test_broker_stage_skipped_when_the_qos1_path_is_busy(make_core0):
    overrides = {
        "network_diagnostics_interval_sec": 60,
        "network_diagnostics_broker_latency_enabled": True,
    }

    def _run_until_broker_stage(busy_setup):
        instance, script = make_core0(
            config_overrides=overrides, raw_supported=True
        )
        _set_stable(instance)
        base = _FAKE_TIME.now_ms  # the shared clock may already be advanced
        instance._service_network_diagnostics()  # arms at base
        _FAKE_TIME.now_ms = base + 60000
        instance._service_network_diagnostics()  # GATEWAY
        instance._service_network_diagnostics()  # DNS
        busy_setup(instance)
        instance._service_network_diagnostics()  # BROKER -> busy: skip + complete
        return instance, script

    # An in-flight QoS 1 entry: never delay real traffic.
    instance, script = _run_until_broker_stage(
        lambda inst: inst._intercore.outbound_queue.__setattr__(
            "has_in_flight", lambda: True)
    )
    assert instance._netdiag_run_count == 1
    assert instance._mqtt_broker_last_round_trip_ms is None
    assert _probe_topics(instance) == []
    assert script.raw_sends and script.udp_sends  # earlier stages did run

    # Pending Core 0 responses: likewise skipped.
    instance, script = _run_until_broker_stage(
        lambda inst: setattr(inst, "_pending_core0_responses", [{"response_id": 1}])
    )
    assert instance._netdiag_run_count == 1
    assert instance._mqtt_broker_last_round_trip_ms is None
    assert _probe_topics(instance) == []


def test_published_snapshot_carries_the_diagnostics_fields(make_core0):
    instance, script = make_core0(
        config_overrides={"network_diagnostics_interval_sec": 60},
        raw_supported=True,
    )
    script.raw_reply = _ICMP_ECHO_REPLY
    script.raw_delay_ms = 12
    script.udp_reply = _DNS_NXDOMAIN_REPLY
    script.udp_delay_ms = 8

    _set_stable(instance)
    instance._service_network_diagnostics()  # arms
    _FAKE_TIME.now_ms = 60000
    instance._service_network_diagnostics()  # GATEWAY (reply at +12 ms)
    instance._service_network_diagnostics()  # DNS (reply at +8 ms) -> complete
    assert instance._netdiag_run_count == 1

    instance._publish_network_snapshot(force=True)
    snap = instance._intercore.state_mailboxes.network_snapshots[-1]

    # Active diagnostics (canonical names, null = not tested / unsupported).
    assert snap["gateway_reachability_supported"] is True
    assert snap["gateway_reachable"] is True
    assert snap["gateway_last_latency_ms"] == 12
    assert snap["dns_reachable"] is True
    assert snap["dns_last_latency_ms"] == 8
    assert snap["mqtt_broker_latency_enabled"] is False
    assert snap["mqtt_broker_last_round_trip_ms"] is None
    assert snap["network_diagnostics_run_count"] == 1
    # Present after a completed cycle (computed at snapshot time); the exact
    # value depends on the probe delays advancing the fake clock.
    assert isinstance(snap["network_diagnostics_last_run_age_ms"], int)

    # Passive Wi-Fi quality diagnostics ride the same snapshot.
    assert snap["wifi_rssi_min_dbm"] == -70
    assert snap["wifi_rssi_max_dbm"] == -40
    assert snap["wifi_rssi_moving_average_dbm"] == -52
    assert snap["wifi_rssi_sample_count"] == 3
    assert snap["wifi_last_reconnect_duration_ms"] == 1200
    assert snap["wifi_last_dhcp_acquisition_duration_ms"] == 450
    assert snap["wifi_last_status_reason"] == "got_ip"
    assert snap["wifi_last_reconnect_trigger"] == "unknown"
    assert snap["wifi_association_details_supported"] is False
    assert snap["wifi_bssid"] is None
    assert snap["wifi_channel"] is None


def test_recovery_records_the_wifi_disconnected_trigger(make_core0):
    instance, script = make_core0()
    instance._network_stack_ready = True
    instance._wifi.connected = True
    instance._mqtt.connected = True
    instance._wifi.connected = False  # Wi-Fi outage (MQTT session now stale)

    instance._recover_network_if_needed()

    assert instance._wifi.reconnect_triggers == ["wifi_disconnected"]
    assert instance._wifi.connected is True
    assert instance._mqtt.connected is True
