# test_startup_log.py - Tests for one-time startup log generation
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import pathlib
import sys
from unittest.mock import MagicMock, patch

import pytest

# Add paths for imports
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from config import load_config, split_config


ROOT = pathlib.Path(__file__).resolve().parents[1]


def _base_config():
    """Load the base configuration."""
    import json
    return json.loads((ROOT / "config.json").read_text())


def test_system_information_sections():
    """Verify SYSTEM_INFORMATION_SECTIONS includes all required sections."""
    # Mock machine module
    sys.modules['machine'] = MagicMock()
    sys.modules['gc'] = MagicMock()
    sys.modules['os'] = MagicMock()
    sys.modules['sys'] = MagicMock()

    from system_information import SYSTEM_INFORMATION_SECTIONS

    required_sections = (
        "network",
        "memory",
        "runtime",
        "devices",
        "cpu",
        "machine",
        "communications",
        "queues",
        "device_status",
        "capabilities",
    )

    for section in required_sections:
        assert section in SYSTEM_INFORMATION_SECTIONS, \
            f"Required section '{section}' not in SYSTEM_INFORMATION_SECTIONS"


def test_collect_system_information_includes_all_sections():
    """Verify _collect_system_information_full includes all sections."""
    # Mock all required modules
    sys.modules['machine'] = MagicMock()
    sys.modules['gc'] = MagicMock()
    sys.modules['os'] = MagicMock()
    sys.modules['sys'] = MagicMock()

    from system_information import SYSTEM_INFORMATION_SECTIONS
    from core1 import _collect_system_information_full

    class MockInterCore:
        def __init__(self):
            self.state_mailboxes = MagicMock()

    class MockSystemInformation:
        def __init__(self, intercore, config):
            pass

        def get_network(self):
            return {"ssid": "test-ssid", "ip_address": "192.168.1.100"}

        def get_memory(self):
            return {"heap_alloc_bytes": 100000, "heap_free_bytes": 50000, "heap_total_bytes": 150000}

        def get_runtime(self):
            return {"read_loop_sec": 20, "start_time": "2026-01-01T00:00:00Z"}

        def get_devices(self):
            return {"configured": 1, "active": 1}

        def get_device_status(self):
            return [{"id": "device1", "state": "ready"}]

        def get_cpu(self):
            return {"frequency_hz": 133000000}

        def get_machine(self):
            return {"hardware_type": "pico_w", "machine": "Raspberry Pi Pico W", "version": "v1.22.1"}

        def get_communications(self):
            return {"wifi_connected": True, "mqtt_connected": True}

        def get_queues(self):
            return {"pending": 0, "max": 16, "high_watermark": 0}

        def get_configuration(self):
            return {
                "config_schema_version": 9,
                "config_generation": 0,
                "config_checksum_sha256": "0" * 64,
                "reboot_required": False,
                "pending_restart_keys": [],
            }

        def get_capabilities(self):
            return {
                "devices": ["system-information"],
                "features": [
                    "health",
                    "commands",
                    "mqtt_qos1",
                    "outage_buffering",
                    "network_diagnostics",
                    "heap_pressure_queue",
                ],
            }

        def set_device_manager(self, dm):
            pass

    intercore = MockInterCore()
    system_info = MockSystemInformation(intercore, None)
    system_info.set_device_manager = lambda dm: None

    result = _collect_system_information_full(system_info)

    # Check all sections are present
    for section in SYSTEM_INFORMATION_SECTIONS:
        assert section in result, f"Section '{section}' not in collected system information"


def test_communications_reports_mqtt_reliability_metrics():
    """get_communications() carries all seven MQTT reliability metrics from the
    Core 0 network snapshot, and an older/incomplete snapshot (no metric keys)
    falls back to integer zero, never null."""
    # Mock machine module
    sys.modules['machine'] = MagicMock()
    sys.modules['gc'] = MagicMock()
    sys.modules['os'] = MagicMock()
    sys.modules['sys'] = MagicMock()

    from system_information import SystemInformation

    def make_si(snapshot):
        class _Mailboxes:
            def get_network_snapshot(self):
                return snapshot

        class _Intercore:
            def __init__(self):
                self.state_mailboxes = _Mailboxes()

        return SystemInformation(_Intercore(), None)

    # A fully-populated snapshot: exact values carried through.
    snapshot = {
        "wifi_connected": True,
        "mqtt_connected": True,
        "mqtt_publish_attempt_count": 3,
        "mqtt_publish_retry_count": 1,
        "mqtt_puback_timeout_count": 2,
        "mqtt_connection_failure_count": 5,
        "mqtt_reconnect_success_count": 1,
        "mqtt_last_reconnect_duration_ms": 12000,
        "mqtt_last_outage_duration_ms": 16000,
    }
    comms = make_si(snapshot).get_communications()
    assert comms["mqtt_publish_attempt_count"] == 3
    assert comms["mqtt_publish_retry_count"] == 1
    assert comms["mqtt_puback_timeout_count"] == 2
    assert comms["mqtt_connection_failure_count"] == 5
    assert comms["mqtt_reconnect_success_count"] == 1
    assert comms["mqtt_last_reconnect_duration_ms"] == 12000
    assert comms["mqtt_last_outage_duration_ms"] == 16000
    for field, value in (
        ("mqtt_publish_attempt_count", 3),
        ("mqtt_publish_retry_count", 1),
        ("mqtt_puback_timeout_count", 2),
        ("mqtt_connection_failure_count", 5),
        ("mqtt_reconnect_success_count", 1),
        ("mqtt_last_reconnect_duration_ms", 12000),
        ("mqtt_last_outage_duration_ms", 16000),
    ):
        assert isinstance(comms[field], int)
        assert comms[field] is not None

    # An older/incomplete snapshot (no metric keys) falls back to integer zero.
    empty = make_si({"wifi_connected": True, "mqtt_connected": True}).get_communications()
    for field in (
        "mqtt_publish_attempt_count",
        "mqtt_publish_retry_count",
        "mqtt_puback_timeout_count",
        "mqtt_connection_failure_count",
        "mqtt_reconnect_success_count",
        "mqtt_last_reconnect_duration_ms",
        "mqtt_last_outage_duration_ms",
    ):
        assert empty[field] == 0
        assert isinstance(empty[field], int)
        assert empty[field] is not None


def test_network_section_reports_wifi_diagnostics():
    """get_network() carries the passive Wi-Fi quality fields and the
    gateway/DNS/broker reachability view from the Core 0 network snapshot.
    Diagnostic fields are null-tolerant: an older/incomplete snapshot
    (no key) reports null, never a fabricated value."""
    sys.modules['machine'] = MagicMock()
    sys.modules['gc'] = MagicMock()
    sys.modules['os'] = MagicMock()
    sys.modules['sys'] = MagicMock()

    from system_information import SystemInformation

    def make_si(snapshot):
        class _Mailboxes:
            def get_network_snapshot(self):
                return snapshot

        class _Intercore:
            def __init__(self):
                self.state_mailboxes = _Mailboxes()

        return SystemInformation(_Intercore(), None)

    snapshot = {
        "wifi_connected": True,
        "mqtt_connected": True,
        "ssid": "test-ssid",
        "ip_address": "192.168.1.100",
        "rssi": -50,
        "netmask": "255.255.255.0",
        "gateway": "192.168.1.1",
        "dns": "10.10.10.53",
        "wifi_rssi_min_dbm": -90,
        "wifi_rssi_max_dbm": -42,
        "wifi_rssi_moving_average_dbm": -63,
        "wifi_rssi_sample_count": 12,
        "wifi_bssid": "aa:bb:cc:dd:ee:ff",
        "wifi_channel": 6,
        "wifi_association_details_supported": True,
        "gateway_reachability_supported": True,
        "gateway_reachable": True,
        "gateway_last_latency_ms": 12,
        "dns_reachable": True,
        "dns_last_latency_ms": 8,
        "mqtt_broker_last_round_trip_ms": 900,
        "network_diagnostics_last_run_age_ms": 4200,
    }
    net = make_si(snapshot).get_network()
    # The section's rssi key is reported under the canonical name.
    assert net["wifi_rssi_dbm"] == -50
    assert net["wifi_rssi_min_dbm"] == -90
    assert net["wifi_rssi_max_dbm"] == -42
    assert net["wifi_rssi_moving_average_dbm"] == -63
    assert net["wifi_rssi_sample_count"] == 12
    assert net["dns_server"] == "10.10.10.53"
    assert net["wifi_bssid"] == "aa:bb:cc:dd:ee:ff"
    assert net["wifi_channel"] == 6
    assert net["wifi_association_details_supported"] is True
    assert net["gateway_reachability_supported"] is True
    assert net["gateway_reachable"] is True
    assert net["gateway_last_latency_ms"] == 12
    assert net["dns_reachable"] is True
    assert net["dns_last_latency_ms"] == 8
    assert net["mqtt_broker_last_round_trip_ms"] == 900
    assert net["network_diagnostics_last_run_age_ms"] == 4200

    # An older/incomplete snapshot: diagnostic fields are null, never a value.
    older = make_si({"wifi_connected": True, "mqtt_connected": True}).get_network()
    for field in (
        "wifi_rssi_min_dbm",
        "wifi_rssi_max_dbm",
        "wifi_rssi_moving_average_dbm",
        "wifi_rssi_sample_count",
        "wifi_bssid",
        "wifi_channel",
        "wifi_association_details_supported",
        "gateway_reachability_supported",
        "gateway_reachable",
        "gateway_last_latency_ms",
        "dns_reachable",
        "dns_last_latency_ms",
        "mqtt_broker_last_round_trip_ms",
        "network_diagnostics_last_run_age_ms",
    ):
        assert older[field] is None


def test_communications_reports_wifi_diagnostics_history():
    """get_communications() carries the reconnect/DHCP history fields with
    safe defaults (integer zero, "unknown", False) for an older/incomplete
    snapshot, and the exact values when the snapshot provides them."""
    sys.modules['machine'] = MagicMock()
    sys.modules['gc'] = MagicMock()
    sys.modules['os'] = MagicMock()
    sys.modules['sys'] = MagicMock()

    from system_information import SystemInformation

    def make_si(snapshot):
        class _Mailboxes:
            def get_network_snapshot(self):
                return snapshot

        class _Intercore:
            def __init__(self):
                self.state_mailboxes = _Mailboxes()

        return SystemInformation(_Intercore(), None)

    snapshot = {
        "wifi_connected": True,
        "mqtt_connected": True,
        "wifi_last_reconnect_duration_ms": 8000,
        "wifi_last_dhcp_acquisition_duration_ms": 4200,
        "wifi_last_status_reason": "got_ip",
        "wifi_last_reconnect_trigger": "wifi_disconnected",
        "network_diagnostics_run_count": 3,
        "mqtt_broker_latency_enabled": True,
    }
    comms = make_si(snapshot).get_communications()
    assert comms["wifi_last_reconnect_duration_ms"] == 8000
    assert comms["wifi_last_dhcp_acquisition_duration_ms"] == 4200
    assert comms["wifi_last_status_reason"] == "got_ip"
    assert comms["wifi_last_reconnect_trigger"] == "wifi_disconnected"
    assert comms["network_diagnostics_run_count"] == 3
    assert comms["mqtt_broker_latency_enabled"] is True

    # Older/incomplete snapshot: safe defaults, never null or a fabricated value.
    older = make_si({"wifi_connected": True, "mqtt_connected": True}).get_communications()
    assert older["wifi_last_reconnect_duration_ms"] == 0
    assert older["wifi_last_dhcp_acquisition_duration_ms"] == 0
    assert older["wifi_last_status_reason"] == "unknown"
    assert older["wifi_last_reconnect_trigger"] == "unknown"
    assert older["network_diagnostics_run_count"] == 0
    assert older["mqtt_broker_latency_enabled"] is False


def test_build_startup_log_structure():
    """Verify _build_startup_log creates the correct message structure."""
    # Mock all required modules
    sys.modules['machine'] = MagicMock()
    sys.modules['gc'] = MagicMock()
    sys.modules['os'] = MagicMock()
    sys.modules['sys'] = MagicMock()
    sys.modules['time'] = MagicMock()
    sys.modules['time'].ticks_ms = MagicMock(return_value=6000)
    sys.modules['time'].ticks_diff = MagicMock(return_value=5000)
    sys.modules['time'].ticks_add = MagicMock(return_value=11000)
    sys.modules['time'].sleep_ms = MagicMock()

    sys.modules['debug'] = MagicMock()
    sys.modules['debug'].DEBUG = False

    # Mock device_manager
    class MockDeviceManager:
        def get_status_snapshot(self, now_ms=None):
            return {
                "devices": {
                    "configured": 1,
                    "active": 1,
                    "initialization_failed": 0,
                },
                "device_status": [],
            }

    class MockStateMailboxes:
        def get_network_snapshot(self):
            return {"ip_address": "192.168.1.100"}

    class MockOutboundQueue:
        def __init__(self):
            self._lock = MagicMock()
            self._queue = []
            self._in_flight = None
            self._max_entries = 16
            self._messages_rejected = 0
            self._messages_evicted = 0
            self._telemetry_evicted = 0

        def put_with_kind(self, kind, payload_bytes, retention_priority):
            if not isinstance(payload_bytes, (bytes, bytearray)):
                raise ValueError("payload_bytes must be bytes")
            self._queue.append({
                "kind": kind,
                "retention_priority": retention_priority,
                "payload_bytes": bytes(payload_bytes),
            })
            return True

        def status(self):
            return {"pending": 0, "max": 16}

    class MockInterCore:
        def __init__(self):
            self.state_mailboxes = MockStateMailboxes()
            self.outbound_queue = MockOutboundQueue()

    # The message carries only Core 1's own fields: the envelope keys
    # (sequence, runtime_id, source, firmware_version, message_schema_version,
    # firmware_build_commit) are Core 0's and are injected at publish time.
    # The payload is the stable log shape: level, event, reason_code are
    # always present; module is not part of the vocabulary.
    message = {
        "message_type": "log",
        "uptime_ms": 5000,
        "timestamp": None,
        "payload": {
            "level": "INFO",
            "event": "runtime_started",
            "reason_code": "none",
            "message": "System startup completed",
            "data": {
                "last_reset_cause": "power_on_reset",
                "boot_reason": "power_on",
                "startup": {
                    "duration_ms": 5000,
                    "hardware": {"status": "ready"},
                    "wifi": {"status": "ready"},
                    "mqtt": {"status": "ready"},
                    "subscriptions": {
                        "status": "ready",
                    },
                    "utc": {"status": "synchronized"},
                    "core_0": {"status": "running"},
                    "core_1": {"status": "running"},
                    "devices_configured": 1,
                    "devices_ready": 1,
                    "devices_failed": 0,
                },
                "system_information": {
                    "network": {"ssid": "test"},
                },
            },
        },
    }

    from core1 import _try_queue_startup_log
    from intercore import RETENTION_PRIORITY_INFO, KIND_LOG

    intercore = MockInterCore()
    device_manager = MockDeviceManager()

    # Test queue admission
    admitted = _try_queue_startup_log(
        intercore, message, RETENTION_PRIORITY_INFO
    )
    assert admitted, "Startup log should be admitted to queue"

    # Verify message was added to queue
    assert len(intercore.outbound_queue._queue) == 1, "Queue should have 1 entry"
    entry = intercore.outbound_queue._queue[0]
    # Core 1 names only the kind; Core 0 resolves the log topic at publish time
    assert entry["kind"] == KIND_LOG, "Queue entry should use the log kind"
    assert "topic" not in entry, "Queue entry must not carry a hardcoded topic"
    assert entry["retention_priority"] == RETENTION_PRIORITY_INFO, "Queue entry should have INFO priority"


def test_startup_summary_uses_explicit_duration_ms():
    """The startup summary names its duration explicitly (duration_ms).

    Drives the real _build_startup_log and verifies the ambiguous 'uptime' key
    is gone from the startup summary, while the envelope keeps device uptime as
    'uptime_ms'.
    """
    sys.modules['machine'] = MagicMock()
    sys.modules['gc'] = MagicMock()
    sys.modules['os'] = MagicMock()
    sys.modules['sys'] = MagicMock()
    sys.modules['debug'] = MagicMock()
    sys.modules['debug'].DEBUG = False

    import core1

    class MockDeviceManager:
        def get_status_snapshot(self, now_ms=None):
            return {
                "devices": {"configured": 1, "active": 1, "initialization_failed": 0},
                "device_status": [],
            }

    class MockInterCore:
        def __init__(self):
            # No UTC snapshot: the builder must leave the timestamp null
            # rather than failing on a missing snapshot.
            self.state_mailboxes = MagicMock()
            self.state_mailboxes.get_utc_snapshot = lambda: None
            self.outbound_queue = MagicMock()

    # core1 may already be imported under a host time lacking MicroPython
    # ticks_*; point its time at a MicroPython-style fake for the call only.
    saved_time = core1.time
    core1.time = MagicMock(ticks_ms=lambda: 6000)
    try:
        payload = core1._build_startup_log(
            MockInterCore(),
            boot_ticks_ms=1000,
            device_manager=MockDeviceManager(),
            config=_base_config(),
            startup_duration_ms=12782,
            system_information=MagicMock(),
        )
    finally:
        core1.time = saved_time

    startup = payload["payload"]["data"]["startup"]
    assert startup["duration_ms"] == 12782
    assert "uptime" not in startup
    # The envelope still carries device uptime under its explicit key.
    assert payload["uptime_ms"] == 12782


def test_split_config_core1_does_not_include_source():
    """Verify split_config does not include source in core1 config."""
    config = _base_config()
    core0, core1, bus = split_config(config)

    assert "source" in core0, "core0 config should include source"
    assert "source" not in core1, "core1 config should NOT include source"
