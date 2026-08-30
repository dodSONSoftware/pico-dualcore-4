# test_wifi_diagnostics.py - Host-side tests for the passive Wi-Fi quality
# diagnostics owned by wifi.py (RSSI statistics, reconnect/DHCP durations,
# stable status reasons, reconnect trigger, association details).
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import importlib
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


class FakeTime:
    """Monotonic fake clock; sleep_ms advances it like real firmware time."""

    def __init__(self):
        self.now_ms = 0

    def ticks_ms(self):
        return self.now_ms

    def ticks_diff(self, a, b):
        return a - b

    def ticks_add(self, a, ms):
        return a + ms

    def sleep_ms(self, ms):
        self.now_ms += ms

    def sleep(self, sec):
        self.now_ms += int(sec * 1000)


_FAKE_TIME = FakeTime()


class FakeWLAN:
    """Scripted WLAN instance; state is class-level because every connect()
    attempt creates a fresh instance (network.WLAN(...))."""

    IF_STA = 0
    PM_NONE = 0
    # Arbitrary local code values: the firmware must map from the port's
    # constants, never from fixed numeric values.
    STAT_IDLE = 0
    STAT_CONNECTING = 1
    STAT_WRONG_PASSWORD = 2
    STAT_NO_AP_FOUND = 3
    STAT_CONNECT_FAIL = 4
    STAT_GOT_IP = 5

    # Script state (shared across instances).
    connected = False
    status_code = 0
    rssi = -55
    rssi_unavailable = False
    dhcp_ready = True
    dhcp_ready_after_polls = None
    has_dhcp4_supported = True
    association = {}
    connect_fails = 0
    connect_advances_ms = 0
    scans = []
    _dhcp_polls = 0

    @classmethod
    def reset(cls):
        cls.connected = False
        cls.status_code = cls.STAT_IDLE
        cls.rssi = -55
        cls.rssi_unavailable = False
        cls.dhcp_ready = True
        cls.dhcp_ready_after_polls = None
        cls.has_dhcp4_supported = True
        cls.association = {}
        cls.connect_fails = 0
        cls.connect_advances_ms = 0
        cls.scans = []
        cls._dhcp_polls = 0

    def __init__(self, iface):
        pass

    def active(self, value):
        pass

    def config(self, *args, **kwargs):
        if args:
            key = args[0]
            if key in type(self).association:
                return type(self).association[key]
            raise ValueError("config key not available: {}".format(key))
        if "pm" in kwargs:
            return None
        raise ValueError("unsupported config call")

    def connect(self, ssid, password):
        cls = type(self)
        if cls.connect_fails > 0:
            cls.connect_fails -= 1
            cls.status_code = cls.STAT_CONNECT_FAIL
            cls.connected = False
            raise OSError("association failed (scripted)")
        if cls.connect_advances_ms:
            _FAKE_TIME.now_ms += cls.connect_advances_ms
        cls.connected = True
        cls.status_code = cls.STAT_GOT_IP

    def isconnected(self):
        return type(self).connected

    def ifconfig(self):
        return ("192.168.1.1.10", "255.255.255.0", "192.168.1.1", "10.10.10.53")

    def ipconfig(self, *args):
        if args and args[0] == "has_dhcp4":
            cls = type(self)
            if not cls.has_dhcp4_supported:
                raise ValueError("has_dhcp4 unsupported")
            if not cls.dhcp_ready and cls.dhcp_ready_after_polls is not None:
                cls._dhcp_polls += 1
                if cls._dhcp_polls >= cls.dhcp_ready_after_polls:
                    cls.dhcp_ready = True
            return cls.dhcp_ready
        raise ValueError("unsupported ipconfig query")

    def status(self, *args):
        if args and args[0] == "rssi":
            if type(self).rssi_unavailable is True:
                raise ValueError("rssi unavailable (scripted)")
            if type(self).rssi_unavailable == "memory":
                raise MemoryError("heap exhausted (scripted)")
            return type(self).rssi
        return type(self).status_code

    def scan(self):
        type(self).scans.append("scan")
        return []


class _FakeNetwork:
    WLAN = FakeWLAN


@pytest.fixture
def wifi_env():
    FakeWLAN.reset()
    _FAKE_TIME.now_ms = 0
    saved = {name: sys.modules.get(name) for name in ("time", "network", "wifi")}
    # Another test module may have left a mock at sys.modules["wifi"]: drop it
    # so the real module is (re)imported bound to our fake time/network.
    sys.modules.pop("wifi", None)
    sys.modules["time"] = _FAKE_TIME
    sys.modules["network"] = _FakeNetwork()
    wifi_module = importlib.import_module("wifi")
    importlib.reload(wifi_module)
    try:
        yield wifi_module
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def _make_wifi(env, delays=(1,)):
    return env.Wifi("ssid", "password", list(delays))


def test_rssi_min_max_count_and_average_direction(wifi_env):
    wifi = _make_wifi(wifi_env)
    assert wifi.connect() is True
    last = None
    for value in (-55, -60, -48, -72, -58):
        FakeWLAN.rssi = value
        _FAKE_TIME.now_ms += 11000
        last = wifi.snapshot(True)
    assert last["rssi_min_dbm"] == -72
    assert last["rssi_max_dbm"] == -48
    assert last["rssi_sample_count"] == 5
    average = last["rssi_moving_average_dbm"]
    # Weighted toward the recent samples: inside the observed range, and
    # lower than the first sample because the later samples are lower.
    assert -72 < average < -48
    assert average < -55


def test_rssi_failed_read_does_not_count_or_substitute_zero(wifi_env):
    wifi = _make_wifi(wifi_env)
    assert wifi.connect() is True
    FakeWLAN.rssi = -60
    _FAKE_TIME.now_ms += 11000
    assert wifi.snapshot(True)["rssi_sample_count"] == 1

    FakeWLAN.rssi_unavailable = True
    _FAKE_TIME.now_ms += 11000
    snap = wifi.snapshot(True)
    assert snap["rssi"] is None
    assert snap["rssi_sample_count"] == 1
    assert snap["rssi_min_dbm"] == -60
    assert snap["rssi_max_dbm"] == -60
    assert snap["rssi_moving_average_dbm"] == -60

    FakeWLAN.rssi_unavailable = False
    FakeWLAN.rssi = -70
    _FAKE_TIME.now_ms += 11000
    snap = wifi.snapshot(True)
    assert snap["rssi_sample_count"] == 2
    assert snap["rssi_min_dbm"] == -70


def test_rssi_sampling_gated_to_ten_seconds(wifi_env):
    wifi = _make_wifi(wifi_env)
    assert wifi.connect() is True
    FakeWLAN.rssi = -55
    _FAKE_TIME.now_ms += 11000
    assert wifi.snapshot(True)["rssi_sample_count"] == 1

    # Within the 10 s gate: reported as the current rssi but not sampled.
    FakeWLAN.rssi = -60
    _FAKE_TIME.now_ms += 5000
    snap = wifi.snapshot(True)
    assert snap["rssi"] == -60
    assert snap["rssi_sample_count"] == 1

    # Past the gate: sampled.
    FakeWLAN.rssi = -48
    _FAKE_TIME.now_ms += 6000
    snap = wifi.snapshot(True)
    assert snap["rssi_sample_count"] == 2
    assert snap["rssi_max_dbm"] == -48


def test_rssi_statistics_survive_reconnect(wifi_env):
    wifi = _make_wifi(wifi_env)
    FakeWLAN.rssi = -60
    assert wifi.connect() is True
    _FAKE_TIME.now_ms += 11000
    wifi.snapshot(True)
    FakeWLAN.rssi = -50
    _FAKE_TIME.now_ms += 11000
    assert wifi.snapshot(True)["rssi_sample_count"] == 2

    # Reconnect: lifetime statistics are NOT reset.
    FakeWLAN.connected = False
    wifi.snapshot(False)
    FakeWLAN.connected = True
    FakeWLAN.rssi = -70
    assert wifi.connect() is True
    _FAKE_TIME.now_ms += 11000
    snap = wifi.snapshot(True)
    assert snap["rssi_sample_count"] == 3
    assert snap["rssi_min_dbm"] == -70
    assert snap["rssi_max_dbm"] == -50


def test_rssi_memory_error_propagates(wifi_env):
    wifi = _make_wifi(wifi_env)
    assert wifi.connect() is True
    FakeWLAN.rssi_unavailable = "memory"
    with pytest.raises(MemoryError):
        wifi.snapshot(True)


def test_initial_connect_not_counted_as_reconnect(wifi_env):
    wifi = _make_wifi(wifi_env)
    assert wifi.connect() is True
    snap = wifi.snapshot(True)
    assert snap["last_reconnect_duration_ms"] == 0

    # Even a failed first boot attempt (the device has never connected) must
    # not start a reconnect timer.
    FakeWLAN.reset()
    FakeWLAN.connect_fails = 1
    wifi = _make_wifi(wifi_env)
    assert wifi.connect() is False
    assert wifi._reconnect_started_ms is None
    assert wifi.connect() is True
    assert wifi.snapshot(True)["last_reconnect_duration_ms"] == 0


def test_reconnect_duration_spans_failed_attempts_and_backoff(wifi_env):
    wifi = _make_wifi(wifi_env, delays=(1, 1))
    assert wifi.connect() is True  # initial connect at t=0
    _FAKE_TIME.now_ms = 1000
    FakeWLAN.connected = False
    wifi.snapshot(False)  # down transition at t=1000 starts the timer

    # One scripted failed attempt + 1 s backoff; the successful attempt
    # lands at t=9000.
    FakeWLAN.connect_fails = 1
    FakeWLAN.connect_advances_ms = 4000
    _FAKE_TIME.now_ms = 4000
    assert wifi.connect() is True
    snap = wifi.snapshot(True)
    assert snap["last_reconnect_duration_ms"] == 8000


def test_dhcp_acquisition_duration_is_connect_to_ip_ready_not_isolated_dora(wifi_env):
    """_last_dhcp_acquisition_duration_ms spans wlan.connect() through
    IP-ready (association + auth + DHCP). It is NOT an isolated DHCP DORA
    exchange — that is all this port authoritatively exposes."""
    FakeWLAN.dhcp_ready = False
    # Poll 1 is the one-shot feature-detection call; readiness then becomes
    # true on the 21st readiness check -> 20 x 100 ms of waiting.
    FakeWLAN.dhcp_ready_after_polls = 22
    wifi = _make_wifi(wifi_env)
    assert wifi.connect() is True
    assert wifi.snapshot(True)["last_dhcp_acquisition_duration_ms"] == 2000


def test_status_reason_maps_every_wlan_constant_to_a_stable_string(wifi_env):
    wifi = _make_wifi(wifi_env)
    assert wifi.connect() is True
    # The reason is captured on the detected transition: observe each
    # failed status on a down transition, then recover (an up transition
    # records "got_ip") before the next observation.
    mapping = (
        ("idle", FakeWLAN.STAT_IDLE),
        ("connecting", FakeWLAN.STAT_CONNECTING),
        ("wrong_password", FakeWLAN.STAT_WRONG_PASSWORD),
        ("no_ap_found", FakeWLAN.STAT_NO_AP_FOUND),
        ("connect_fail", FakeWLAN.STAT_CONNECT_FAIL),
    )
    for expected, code in mapping:
        FakeWLAN.status_code = code
        FakeWLAN.connected = False
        assert wifi.snapshot(False)["last_status_reason"] == expected
        FakeWLAN.status_code = FakeWLAN.STAT_GOT_IP
        FakeWLAN.connected = True
        assert wifi.snapshot(True)["last_status_reason"] == "got_ip"


def test_status_reason_unknown_for_unmapped_code(wifi_env):
    wifi = _make_wifi(wifi_env)
    assert wifi.connect() is True
    FakeWLAN.status_code = 99  # not a port constant
    FakeWLAN.connected = False
    snap = wifi.snapshot(False)
    assert snap["last_status_reason"] == "unknown"


def test_association_unsupported_reports_false_and_never_scans(wifi_env):
    wifi = _make_wifi(wifi_env)
    assert wifi.connect() is True
    snap = wifi.snapshot(True)
    assert snap["association_details_supported"] is False
    assert snap["bssid"] is None
    assert snap["channel"] is None
    assert FakeWLAN.scans == []  # REQUIRED: scan() must never be called


def test_association_supported_reports_bssid_and_channel_refreshed_after_reconnect(wifi_env):
    FakeWLAN.association = {
        "channel": 6,
        "bssid": b"\xaa\xbb\xcc\xdd\xee\xff",
    }
    wifi = _make_wifi(wifi_env)
    assert wifi.connect() is True
    snap = wifi.snapshot(True)
    assert snap["association_details_supported"] is True
    assert snap["bssid"] == "aa:bb:cc:dd:ee:ff"
    assert snap["channel"] == 6
    assert FakeWLAN.scans == []

    # The association may change after a reconnect: values are refreshed.
    FakeWLAN.association = {
        "channel": 11,
        "bssid": "11:22:33:44:55:66",  # already-formatted string also accepted
    }
    FakeWLAN.connected = False
    wifi.snapshot(False)
    assert wifi.connect() is True
    snap = wifi.snapshot(True)
    assert snap["bssid"] == "11:22:33:44:55:66"
    assert snap["channel"] == 11


def test_reconnect_trigger_only_records_known_firmware_values(wifi_env):
    wifi = _make_wifi(wifi_env)
    assert wifi.snapshot(False)["last_reconnect_trigger"] == "unknown"
    wifi.note_reconnect_trigger("wifi_disconnected")
    assert wifi.snapshot(False)["last_reconnect_trigger"] == "wifi_disconnected"
    wifi.note_reconnect_trigger("bogus_80211_reason")
    assert wifi.snapshot(False)["last_reconnect_trigger"] == "wifi_disconnected"
    wifi.note_reconnect_trigger("network_probe_failure")
    assert wifi.snapshot(False)["last_reconnect_trigger"] == "network_probe_failure"


def test_gateway_and_dns_accessors_read_the_interface_configuration(wifi_env):
    wifi = _make_wifi(wifi_env)
    assert wifi.gateway_address() is None  # not connected
    assert wifi.dns_address() is None
    assert wifi.connect() is True
    assert wifi.gateway_address() == "192.168.1.1"
    assert wifi.dns_address() == "10.10.10.53"
