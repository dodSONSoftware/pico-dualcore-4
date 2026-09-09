# test_system_information_self_status.py - System-information self-status reconciliation
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Host-side regression tests for the system-information device's own device_status entry.

The system-information read can include the ``device_status`` section, whose snapshot is captured while the manager has already incremented this device's ``read_count`` but has not yet committed its success (``successful_read_count`` / ``last_successful_read_ms``) — so a continuously successful device would report ``successful_read_count == read_count - 1`` and a ``last_successful_read_age_ms`` of one full read interval in the very telemetry sample its own successful read produced.

After the successful read state is committed, DeviceManager reconciles only this device's own entry in the payload (fresh from the authoritative ``ManagedDevice.get_status_snapshot()``), leaving every other device's entry exactly as the original read captured it.

These tests drive the real ``DeviceManager.process_device()`` path with a fake system-information-style driver and a controllable clock; they assert the externally visible telemetry, not that a helper ran. The pre-fix one-cycle lag (``read_count == N``, ``successful_read_count == N - 1``) fails Test 1 and Test 2."""

import pathlib
import sys
import time as time_module
import types

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

# device_manager imports device_factory -> system_information, which imports
# MicroPython-only modules at load time. Install minimal fakes before import.
if "machine" not in sys.modules:
    machine_fake = types.ModuleType("machine")
    machine_fake.freq = staticmethod(lambda: 125000000)
    sys.modules["machine"] = machine_fake

import device_manager as dm  # noqa: E402


class FakeClock:
    """Controllable monotonic clock (MicroPython ticks semantics) for the host."""

    def __init__(self, now_ms=100000):
        self.now_ms = now_ms

    def ticks_ms(self):
        return self.now_ms

    def ticks_diff(self, now, prev):
        return now - prev

    def advance(self, ms):
        self.now_ms += ms


@pytest.fixture
def fake_time(monkeypatch):
    clock = FakeClock()
    # CPython's time module has no ticks_*; raising=False adds them and
    # removes them at teardown.
    monkeypatch.setattr(time_module, "ticks_ms", clock.ticks_ms, raising=False)
    monkeypatch.setattr(time_module, "ticks_diff", clock.ticks_diff, raising=False)
    return clock


def _make_manager(device_ids):
    manager_config = {
        "device_initialization_attempts": 1,
        "device_initialization_retry_delay_ms": 10,
        "device_read_failure_threshold": 3,
        "devices": [
            {"id": device_id, "device_type": "system-information", "config": {}}
            for device_id in device_ids
        ],
    }
    return dm.DeviceManager(manager_config)


class SelfStatusDriver:
    """Driver that behaves like the system-information device for the
    ``device_status`` section: read() captures the manager's snapshot of its
    own ManagedDevice at read time. Because DeviceManager increments
    read_count before calling the driver, the captured snapshot sees the
    current read attempt but not yet the successful-read commit — the same
    stale self-entry the real device produced."""

    def __init__(self, managed_holder):
        self._managed_holder = managed_holder

    def initialize(self, config):
        pass

    def read(self):
        managed = self._managed_holder[0]
        return {
            "device_status": [
                managed.get_status_snapshot(now_ms=time_module.ticks_ms())
            ]
        }


def _add_system_information_device(manager, device_id):
    managed = dm.ManagedDevice(
        device_id=device_id,
        device_type="system-information",
        driver=None,
    )
    manager._active_devices.append(managed)
    return managed


def test_successful_read_reports_current_self_status(fake_time):
    """One successful system-information read reports its own newly committed
    success (equal counts, near-zero success age) in that same telemetry sample."""
    manager = _make_manager(["sysinfo"])
    managed = _add_system_information_device(manager, "sysinfo")
    managed.driver = SelfStatusDriver([managed])

    result = manager.process_device(managed)

    assert result["status"] == dm.DEVICE_RESULT_TELEMETRY
    self_status = result["telemetry"]["device_status"][0]

    assert self_status["read_count"] == 1
    assert self_status["successful_read_count"] == 1
    assert self_status["last_read_age_ms"] is not None
    assert self_status["last_successful_read_age_ms"] is not None
    # Under the controlled clock both timestamps were committed this read.
    assert self_status["last_read_age_ms"] == 0
    assert self_status["last_successful_read_age_ms"] == 0


def test_repeated_successful_reads_stay_synchronized(fake_time):
    """After three consecutive successful reads the self-entry reports equal
    counts and the current read's success age — not the one-cycle lag
    (successful_read_count == read_count - 1, one read interval of success age)."""
    manager = _make_manager(["sysinfo"])
    managed = _add_system_information_device(manager, "sysinfo")
    managed.driver = SelfStatusDriver([managed])

    self_status = None
    for _ in range(3):
        result = manager.process_device(managed)
        assert result["status"] == dm.DEVICE_RESULT_TELEMETRY
        self_status = result["telemetry"]["device_status"][0]
        # Simulate the 20-second read loop between cycles.
        fake_time.advance(20000)

    assert self_status["read_count"] == 3
    assert self_status["successful_read_count"] == 3
    # The self-entry reflects the read that produced this telemetry sample.
    assert self_status["last_read_age_ms"] == 0
    assert self_status["last_successful_read_age_ms"] == 0


def test_other_device_entries_are_not_rewritten(fake_time):
    """Reconciliation updates only the system-information device's own entry;
    the other device's entry is left exactly as the original read captured it."""
    manager = _make_manager(["sysinfo", "other"])
    managed = _add_system_information_device(manager, "sysinfo")

    other = dm.ManagedDevice(
        device_id="other",
        device_type="test",
        driver=None,
    )
    manager._active_devices.append(other)
    # Give the other device a distinct history so any rewrite would be visible.
    other.read_count = 5
    other.successful_read_count = 4
    other.last_read_ms = fake_time.ticks_ms() - 20000
    other.last_successful_read_ms = fake_time.ticks_ms() - 40000

    other_entry = other.get_status_snapshot(now_ms=time_module.ticks_ms())

    class OtherEntryDriver:
        def initialize(self, config):
            pass

        def read(self):
            return {
                "device_status": [
                    managed.get_status_snapshot(now_ms=time_module.ticks_ms()),
                    other_entry,
                ]
            }

    managed.driver = OtherEntryDriver()

    result = manager.process_device(managed)
    assert result["status"] == dm.DEVICE_RESULT_TELEMETRY
    entries = result["telemetry"]["device_status"]

    # The system-information entry reflects the newly committed success...
    self_status = entries[0]
    assert self_status["id"] == "sysinfo"
    assert self_status["read_count"] == 1
    assert self_status["successful_read_count"] == 1
    # ...while the other device's entry is untouched (same object, original
    # history and ages), not regenerated.
    assert entries[1] is other_entry
    assert entries[1]["read_count"] == 5
    assert entries[1]["successful_read_count"] == 4
    assert entries[1]["last_read_age_ms"] == 20000
    assert entries[1]["last_successful_read_age_ms"] == 40000


def test_no_device_status_section_is_a_noop(fake_time):
    """A system-information read that does not include device_status succeeds
    normally and the section is not added implicitly."""
    manager = _make_manager(["sysinfo"])
    managed = _add_system_information_device(manager, "sysinfo")

    class QueuesOnlyDriver:
        def initialize(self, config):
            pass

        def read(self):
            return {"queues": {"outbound_pending": 0}}

    managed.driver = QueuesOnlyDriver()

    result = manager.process_device(managed)

    assert result["status"] == dm.DEVICE_RESULT_TELEMETRY
    assert managed.read_count == 1
    assert managed.successful_read_count == 1
    assert "device_status" not in result["telemetry"]
    assert result["telemetry"] == {"queues": {"outbound_pending": 0}}


def test_invalid_read_is_not_counted_as_successful(fake_time):
    """Invalid telemetry from a system-information driver is still a read
    failure: no success bookkeeping, and nothing to reconcile."""
    manager = _make_manager(["sysinfo"])
    managed = _add_system_information_device(manager, "sysinfo")

    class EmptyDriver:
        def initialize(self, config):
            pass

        def read(self):
            return {}

    managed.driver = EmptyDriver()

    result = manager.process_device(managed)

    assert result["status"] == dm.DEVICE_RESULT_READ_FAILED
    assert managed.read_count == 1
    assert managed.successful_read_count == 0
    assert managed.last_read_ms is not None
    assert managed.last_successful_read_ms is None
    assert "telemetry" not in result


def test_non_system_information_device_unchanged(fake_time):
    """The reconciliation is a no-op for ordinary devices: the payload is
    returned exactly as the driver produced it."""
    manager = _make_manager(["dev1"])
    managed = dm.ManagedDevice(
        device_id="dev1",
        device_type="test",
        driver=None,
    )
    manager._active_devices.append(managed)

    payload = {
        "device_status": [
            managed.get_status_snapshot(now_ms=time_module.ticks_ms())
        ]
    }

    class PayloadDriver:
        def initialize(self, config):
            pass

        def read(self):
            return payload

    managed.driver = PayloadDriver()

    result = manager.process_device(managed)

    assert result["status"] == dm.DEVICE_RESULT_TELEMETRY
    # The stale-looking self-entry is left as captured: the special case
    # applies only to device_type == "system-information".
    assert result["telemetry"]["device_status"][0]["successful_read_count"] == 0
    assert managed.successful_read_count == 1
