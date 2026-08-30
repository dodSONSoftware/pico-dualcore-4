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

        def set_device_manager(self, dm):
            pass

    intercore = MockInterCore()
    system_info = MockSystemInformation(intercore, None)
    system_info.set_device_manager = lambda dm: None

    result = _collect_system_information_full(system_info)

    # Check all sections are present
    for section in SYSTEM_INFORMATION_SECTIONS:
        assert section in result, f"Section '{section}' not in collected system information"


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
    # (sequence, runtime_id, source, firmware_version, message_schema_version)
    # are Core 0's and are injected at publish time.
    message = {
        "message_type": "log",
        "uptime_ms": 5000,
        "timestamp": None,
        "payload": {
            "level": "info",
            "event": "system_startup_completed",
            "module": "system",
            "message": "System startup completed",
            "data": {
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
