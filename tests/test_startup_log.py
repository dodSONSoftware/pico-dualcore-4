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
    return json.loads((ROOT / "tests" / "fixtures" / "config.json").read_text())


def test_system_information_sections(monkeypatch):
    """Verify SYSTEM_INFORMATION_SECTIONS includes all required sections."""
    # Mock machine module; monkeypatch restores the real modules at teardown,
    # so a later test file importing core1 cannot bind the mocks.
    monkeypatch.setitem(sys.modules, 'machine', MagicMock())
    monkeypatch.setitem(sys.modules, 'gc', MagicMock())
    monkeypatch.setitem(sys.modules, 'os', MagicMock())
    monkeypatch.setitem(sys.modules, 'sys', MagicMock())

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


def test_collect_system_information_includes_all_sections(monkeypatch):
    """Verify _collect_system_information_full includes all sections."""
    # Mock all required modules (restored at teardown by monkeypatch)
    monkeypatch.setitem(sys.modules, 'machine', MagicMock())
    monkeypatch.setitem(sys.modules, 'gc', MagicMock())
    monkeypatch.setitem(sys.modules, 'os', MagicMock())
    monkeypatch.setitem(sys.modules, 'sys', MagicMock())

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
            # Heap-governed queue: depth/high-watermark/counter metrics, no capacity.
            return {
                "outbound_pending": 0,
                "outbound_high_watermark": 0,
                "outbound_high_watermark_bytes": 0,
                "outbound_evicted": 0,
                "telemetry_evicted": 0,
                "outbound_rejected": 0,
                "outbound_queued_bytes": 0,
                "intercore_events_pending": 0,
                "intercore_events_high_watermark": 0,
                "intercore_events_rejected": 0,
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


def test_collect_system_information_takes_one_device_snapshot(monkeypatch):
    """devices and device_status must both come from a single
    get_device_sections() call, not from separate per-section getters."""
    if "core1" not in sys.modules:
        if "machine" not in sys.modules:
            monkeypatch.setitem(sys.modules, "machine", MagicMock())
        import importlib

        importlib.import_module("core1")
    from core1 import _collect_system_information_full

    class CountingSystemInformation:
        def __init__(self):
            self.device_section_calls = 0
            self.section_calls = []

        def get_device_sections(self):
            self.device_section_calls += 1
            return {
                "devices": {"configured": 1, "active": 1, "source": "shared-snapshot"},
                "device_status": [{"id": "d1", "source": "shared-snapshot"}],
            }

        def get_network(self):
            self.section_calls.append("network")
            return {"ssid": "test"}

    source = CountingSystemInformation()
    result = _collect_system_information_full(source)

    assert source.device_section_calls == 1
    assert result["devices"]["source"] == "shared-snapshot"
    assert result["device_status"][0]["source"] == "shared-snapshot"
    assert result["network"] == {"ssid": "test"}
    assert source.section_calls == ["network"]


def test_build_startup_log_structure(monkeypatch):
    """Verify _build_startup_log creates the correct message structure."""
    # Mock all required modules (restored at teardown by monkeypatch)
    monkeypatch.setitem(sys.modules, 'machine', MagicMock())
    monkeypatch.setitem(sys.modules, 'gc', MagicMock())
    monkeypatch.setitem(sys.modules, 'os', MagicMock())
    monkeypatch.setitem(sys.modules, 'sys', MagicMock())
    fake_time = MagicMock()
    fake_time.ticks_ms = MagicMock(return_value=6000)
    fake_time.ticks_diff = MagicMock(return_value=5000)
    fake_time.ticks_add = MagicMock(return_value=11000)
    fake_time.sleep_ms = MagicMock()
    monkeypatch.setitem(sys.modules, 'time', fake_time)

    fake_debug = MagicMock()
    fake_debug.DEBUG = False
    monkeypatch.setitem(sys.modules, 'debug', fake_debug)

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
            return {
                "pending": len(self._queue),
                "depth": len(self._queue) + (1 if self._in_flight is not None else 0),
                "in_flight": self._in_flight is not None,
                "queued_bytes": 0,
                "high_watermark": 0,
                "high_watermark_bytes": 0,
                "messages_evicted": self._messages_evicted,
                "telemetry_evicted": self._telemetry_evicted,
                "messages_rejected": self._messages_rejected,
                "serialization_rejected": 0,
                "oversized_rejected": 0,
            }

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


def test_startup_summary_uses_explicit_duration_ms(monkeypatch):
    """The startup summary names its duration explicitly (duration_ms), not the ambiguous 'uptime' key, while the envelope keeps device uptime as 'uptime_ms'.

    Drives the real _build_startup_log and verifies both."""
    # Mock modules (restored at teardown by monkeypatch)
    monkeypatch.setitem(sys.modules, 'machine', MagicMock())
    monkeypatch.setitem(sys.modules, 'gc', MagicMock())
    monkeypatch.setitem(sys.modules, 'os', MagicMock())
    monkeypatch.setitem(sys.modules, 'sys', MagicMock())
    fake_debug = MagicMock()
    fake_debug.DEBUG = False
    monkeypatch.setitem(sys.modules, 'debug', fake_debug)

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
    core0, core1 = split_config(config)

    assert "source" in core0, "core0 config should include source"
    assert "source" not in core1, "core1 config should NOT include source"


# ---------------------------------------------------------------------------
# Startup-log admission: permanent (ValueError) vs transient (False)
# ---------------------------------------------------------------------------

# 0.4.30 gave both admission paths one failure contract: False is the
# transient heap-pressure rejection (retrying may succeed); permanent
# rejections of the message itself raise ValueError (retrying can never
# succeed). The startup log previously swallowed that distinction: every
# failure returned False and core1_main unconditionally slept 100 ms and
# re-submitted the exact same object -- a meaningless retry for an
# oversized or invalid message. Now a permanent rejection fails fast with
# its actual reason and only a genuinely transient one is retried.


def _core1_module(monkeypatch):
    """The core1 module, importable on the host.

    The machine stub (needed only to import core1 on the host) is installed
    via monkeypatch so it is restored at teardown; core1 keeps its own bound
    reference either way, and later imports in other test files see the real
    modules.
    """
    if "core1" in sys.modules:
        return sys.modules["core1"]
    if "machine" not in sys.modules:
        monkeypatch.setitem(sys.modules, "machine", MagicMock())
    import importlib
    return importlib.import_module("core1")


_STARTUP_MESSAGE = {
    "message_type": "log",
    "uptime_ms": 1000,
    "timestamp": None,
    "payload": {"level": "info", "event": "system_startup_completed"},
}


class _ScriptedQueue:
    """put_with_kind() outcomes scripted per call; the last one sticks."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def put_with_kind(self, kind, payload_bytes, retention_priority):
        self.calls += 1
        outcome = self.outcomes[min(self.calls - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _ScriptedBus:
    def __init__(self, queue):
        self.outbound_queue = queue


class _RecordingTime:
    """time stand-in recording every sleep_ms() call."""

    def __init__(self):
        self.sleeps = []

    def sleep_ms(self, ms):
        self.sleeps.append(ms)


def test_try_queue_startup_log_transient_rejection_returns_false(monkeypatch):
    """A transient (heap-pressure) rejection returns False -- retryable."""
    core1 = _core1_module(monkeypatch)
    queue = _ScriptedQueue([False])
    admitted = core1._try_queue_startup_log(_ScriptedBus(queue), _STARTUP_MESSAGE, 0)
    assert admitted is False
    assert queue.calls == 1


def test_try_queue_startup_log_permanent_rejection_raises_valueerror(monkeypatch):
    """A permanent queue rejection raises ValueError -- not retryable."""
    core1 = _core1_module(monkeypatch)
    queue = _ScriptedQueue([ValueError("Message too large: 20000 > 16384")])
    with pytest.raises(ValueError):
        core1._try_queue_startup_log(_ScriptedBus(queue), _STARTUP_MESSAGE, 0)
    assert queue.calls == 1


def test_try_queue_startup_log_serialization_failure_raises_valueerror(monkeypatch):
    """A deterministic serialization failure is permanent: ValueError."""
    core1 = _core1_module(monkeypatch)
    from message_serializer import MessageTooLargeError

    def _too_large(message):
        raise MessageTooLargeError("Message size 20000 exceeds maximum 16384")

    monkeypatch.setattr(core1, "serialize_and_validate_message", _too_large)
    queue = _ScriptedQueue([True])
    with pytest.raises(ValueError):
        core1._try_queue_startup_log(_ScriptedBus(queue), _STARTUP_MESSAGE, 0)
    assert queue.calls == 0


def test_admit_startup_log_permanent_rejection_fails_fast_without_retry(monkeypatch):
    """A permanent rejection is never retried and never waits for queue space."""
    core1 = _core1_module(monkeypatch)
    queue = _ScriptedQueue([ValueError("Startup log too large")])
    fake_time = _RecordingTime()
    monkeypatch.setattr(core1, "time", fake_time)

    with pytest.raises(ValueError):
        core1._admit_startup_log(_ScriptedBus(queue), _STARTUP_MESSAGE)

    assert queue.calls == 1, "a permanent rejection must not be re-submitted"
    assert fake_time.sleeps == [], "a permanent rejection must not wait for queue space"


def test_admit_startup_log_retries_transient_rejection_once(monkeypatch):
    """A transient rejection is retried exactly once, after a short delay."""
    core1 = _core1_module(monkeypatch)
    queue = _ScriptedQueue([False, True])
    fake_time = _RecordingTime()
    monkeypatch.setattr(core1, "time", fake_time)

    admitted = core1._admit_startup_log(_ScriptedBus(queue), _STARTUP_MESSAGE)

    assert admitted is True
    assert queue.calls == 2
    assert fake_time.sleeps == [100]


def test_admit_startup_log_exhausted_transient_retry_returns_false(monkeypatch):
    """Both attempts transiently rejected: False, exactly two submissions."""
    core1 = _core1_module(monkeypatch)
    queue = _ScriptedQueue([False])
    fake_time = _RecordingTime()
    monkeypatch.setattr(core1, "time", fake_time)

    admitted = core1._admit_startup_log(_ScriptedBus(queue), _STARTUP_MESSAGE)

    assert admitted is False
    assert queue.calls == 2
    assert fake_time.sleeps == [100]


# ---------------------------------------------------------------------------
# Bounded fallback when the detailed startup log exceeds the outbound ceiling
# ---------------------------------------------------------------------------
#
# Device definitions carry no practical length or count bound for id, name,
# or sensor_type, so the detailed startup log can exceed the 16 KiB outbound
# ceiling on an otherwise valid configuration. The invariant this protects:
# failure to emit the verbose diagnostics must not keep the device from
# entering normal operation. Only the size rejection is answered by a
# different object -- the bounded fallback summary (statuses and device
# counts only, no ready_devices/failed_devices, no system_information);
# every other permanent rejection still fails fast with its actual reason.


_DETAIL_MESSAGE = {
    "message_type": "log",
    "uptime_ms": 1000,
    "timestamp": None,
    "payload": {
        "level": "info",
        "event": "system_startup_completed",
        "data": {
            "startup": {
                "duration_ms": 1000,
                "devices_configured": 1,
                "devices_ready": 1,
                "devices_failed": 0,
                "ready_devices": [
                    {"device": "d", "name": "n" * 5000, "sensor_type": "s"}
                ],
                "failed_devices": [],
            },
            "system_information": {"network": {"ssid": "test"}},
        },
    },
}


class _RecordingQueue:
    """Admits everything and records the decoded payload bytes."""

    def __init__(self):
        self.entries = []

    def put_with_kind(self, kind, payload_bytes, retention_priority):
        import json

        self.entries.append(json.loads(bytes(payload_bytes).decode("utf-8")))
        return True


def _too_large_for_detailed(message):
    """Serialization contract for the fallback tests: the detailed message (with per-device lists) exceeds the ceiling; the bounded fallback serializes."""
    import json

    from message_serializer import MessageTooLargeError

    startup = message.get("payload", {}).get("data", {}).get("startup", {})
    if "ready_devices" in startup:
        raise MessageTooLargeError("Message size 20000 exceeds maximum 16384")
    return json.dumps(message).encode("utf-8")


def test_build_startup_log_bounded_omits_unbounded_sections(monkeypatch):
    """The bounded fallback keeps only statuses and counts -- no device names, no system_information."""
    core1 = _core1_module(monkeypatch)

    class MockDeviceManager:
        def get_status_snapshot(self, now_ms=None):
            return {
                "devices": {"configured": 3, "active": 2, "initialization_failed": 1},
                "device_status": [
                    {"device": "a", "name": "x" * 5000, "sensor_type": "t", "state": "ready"},
                    {"device": "b", "name": "y" * 5000, "sensor_type": "t", "state": "init_failed"},
                ],
            }

    class MockInterCore:
        def __init__(self):
            # No UTC snapshot: the builder must leave the timestamp null
            # rather than failing on a missing snapshot.
            self.state_mailboxes = MagicMock()
            self.state_mailboxes.get_utc_snapshot = lambda: None

    saved_time = core1.time
    core1.time = MagicMock(ticks_ms=lambda: 1000)
    try:
        payload = core1._build_startup_log_bounded(MockInterCore(), MockDeviceManager(), 4321)
    finally:
        core1.time = saved_time

    data = payload["payload"]["data"]
    startup = data["startup"]
    assert startup["duration_ms"] == 4321
    assert startup["devices_configured"] == 3
    assert startup["devices_ready"] == 2
    assert startup["devices_failed"] == 1
    assert "ready_devices" not in startup
    assert "failed_devices" not in startup
    assert "system_information" not in data
    assert payload["message_type"] == "log"
    assert payload["payload"]["event"] == "system_startup_completed"


def test_try_queue_startup_log_too_large_raises_typed_error(monkeypatch):
    """The oversized case raises StartupLogTooLargeError (a ValueError subclass) so the caller can answer it with the bounded fallback."""
    core1 = _core1_module(monkeypatch)
    from message_serializer import MessageTooLargeError

    def _too_large(message):
        raise MessageTooLargeError("Message size 20000 exceeds maximum 16384")

    monkeypatch.setattr(core1, "serialize_and_validate_message", _too_large)
    queue = _ScriptedQueue([True])
    with pytest.raises(core1.StartupLogTooLargeError) as excinfo:
        core1._try_queue_startup_log(_ScriptedBus(queue), _DETAIL_MESSAGE, 0)
    assert isinstance(excinfo.value, ValueError)
    assert queue.calls == 0, "an oversized message must never reach the queue"


def test_admit_with_fallback_admits_bounded_summary_when_detailed_too_large(monkeypatch):
    """A size-only rejection is answered by the bounded fallback, which is then admitted."""
    core1 = _core1_module(monkeypatch)

    monkeypatch.setattr(core1, "serialize_and_validate_message", _too_large_for_detailed)
    queue = _RecordingQueue()
    fallback = {
        "message_type": "log",
        "uptime_ms": 1000,
        "timestamp": None,
        "payload": {
            "level": "info",
            "event": "system_startup_completed",
            "data": {"startup": {"duration_ms": 1000, "devices_configured": 1}},
        },
    }

    admitted = core1._admit_startup_log_with_fallback(
        _ScriptedBus(queue), _DETAIL_MESSAGE, lambda: fallback
    )

    assert admitted is True
    assert queue.entries == [fallback], "the admitted entry must be the bounded summary, not the detailed log"


def test_admit_with_fallback_non_size_permanent_rejection_escapes_without_fallback(monkeypatch):
    """A non-size permanent rejection cannot be answered by any fallback: it escapes and the fallback is never built."""
    core1 = _core1_module(monkeypatch)
    from message_serializer import NonStringKeyError

    def _invalid(message):
        raise NonStringKeyError("dict key is not a string")

    monkeypatch.setattr(core1, "serialize_and_validate_message", _invalid)
    built = []
    queue = _RecordingQueue()

    with pytest.raises(ValueError):
        core1._admit_startup_log_with_fallback(
            _ScriptedBus(queue), _DETAIL_MESSAGE, lambda: built.append(1) or _STARTUP_MESSAGE
        )

    assert built == [], "the fallback must not be built for a non-size rejection"
    assert queue.entries == [], "nothing is admitted when the rejection is not size-only"


def test_admit_with_fallback_transient_rejection_on_fallback_retried_once(monkeypatch):
    """A transient rejection of the bounded fallback keeps the normal single transient retry."""
    core1 = _core1_module(monkeypatch)

    monkeypatch.setattr(core1, "serialize_and_validate_message", _too_large_for_detailed)
    queue = _ScriptedQueue([False, True])
    fake_time = _RecordingTime()
    monkeypatch.setattr(core1, "time", fake_time)

    admitted = core1._admit_startup_log_with_fallback(
        _ScriptedBus(queue), _DETAIL_MESSAGE, lambda: _STARTUP_MESSAGE
    )

    assert admitted is True
    assert queue.calls == 2, "the fallback gets its own single transient retry"
    assert fake_time.sleeps == [100]
