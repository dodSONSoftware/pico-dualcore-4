# test_device_status_count.py - Device status active-count semantics
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Host-side tests for the status snapshot's active-count semantics.

The documented contract: devices_configured = number of configured devices, devices_active = number of active/ready devices.

A device that has accumulated enough read failures is marked reinitialize_pending and intentionally remains in DeviceManager._active_devices (it must stay eligible for reinitialization). It is NOT currently ready, however, so the status snapshot must not count it as active: with one configured device that is reinit-pending, the snapshot must report active == 0 so the device_count_mismatch degradation reason can fire."""

import pathlib
import sys
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

# device_manager imports device_factory -> system_information, which imports
# MicroPython-only modules at load time. Install minimal fakes before import.
if "machine" not in sys.modules:
    machine_fake = types.ModuleType("machine")
    machine_fake.freq = staticmethod(lambda: 125000000)
    sys.modules["machine"] = machine_fake

import device_manager as dm  # noqa: E402


class FailingDriver:
    """Driver whose read() always fails (initialize succeeds)."""

    def initialize(self, config):
        pass

    def read(self):
        raise RuntimeError("simulated read failure")


def _make_manager(read_failure_threshold=3):
    manager_config = {
        "device_initialization_attempts": 1,
        "device_initialization_retry_delay_ms": 10,
        "device_read_failure_threshold": read_failure_threshold,
        "devices": [{"id": "dev1", "device_type": "test", "config": {}}],
    }
    return dm.DeviceManager(manager_config)


def _add_managed_device(manager, device_id="dev1"):
    managed = dm.ManagedDevice(
        device_id=device_id,
        device_type="test",
        driver=FailingDriver(),
    )
    manager._active_devices.append(managed)
    return managed


def test_ready_device_counts_as_active():
    manager = _make_manager()
    managed = _add_managed_device(manager)

    assert managed.state == dm.DEVICE_STATE_READY
    snapshot = manager.get_status_snapshot()
    devices = snapshot["devices"]

    assert devices["configured"] == 1
    assert devices["active"] == 1


def test_reinitialize_pending_device_does_not_count_as_active():
    manager = _make_manager(read_failure_threshold=3)
    managed = _add_managed_device(manager)

    # Drive the real failure path: three failed reads cross the threshold.
    for _ in range(3):
        result = manager.process_device(managed)
        assert result["status"] == dm.DEVICE_RESULT_READ_FAILED

    assert managed.state == dm.DEVICE_STATE_REINITIALIZE_PENDING
    # The device stays in the active list (eligible for reinit)...
    assert managed in manager._active_devices

    snapshot = manager.get_status_snapshot()
    devices = snapshot["devices"]

    assert devices["configured"] == 1
    assert devices["active"] == 0
    # configured - active is 1: the device_count_mismatch degradation input.
    assert devices["configured"] - devices["active"] == 1
    # The per-device state is still reported.
    assert snapshot["device_status"][0]["state"] == dm.DEVICE_STATE_REINITIALIZE_PENDING


class FailingInitDriver:
    """Driver whose initialize() always fails."""

    def initialize(self, config):
        raise RuntimeError("simulated initialization failure")


class OkDriver:
    """Driver whose initialize() succeeds."""

    def initialize(self, config):
        pass


def test_one_failing_device_does_not_stop_the_others():
    """A failed initialization is isolated to that device: the remaining
    configured device still initializes, the failure is recorded for the
    failing device only, and the snapshot shows configured 2 / active 1."""
    saved = dm.create_device
    dm.create_device = lambda device_def, i2c_bus_factory=None: (
        FailingInitDriver() if device_def["id"] == "dev1" else OkDriver()
    )
    try:
        manager = dm.DeviceManager({
            "device_initialization_attempts": 1,
            "device_initialization_retry_delay_ms": 10,
            "device_read_failure_threshold": 3,
            "devices": [
                {"id": "dev1", "device_type": "test", "config": {}},
                {"id": "dev2", "device_type": "test", "config": {}},
            ],
        })
        initialized = manager.initialize_devices()
    finally:
        dm.create_device = saved

    assert initialized == 1
    assert list(manager._failed_devices) == ["dev1"]
    assert [m.device_id for m in manager.get_active_devices()] == ["dev2"]

    snapshot = manager.get_status_snapshot()
    assert snapshot["devices"]["configured"] == 2
    assert snapshot["devices"]["active"] == 1


def test_recovered_device_counts_as_active_again():
    manager = _make_manager(read_failure_threshold=1)
    managed = _add_managed_device(manager)

    manager.process_device(managed)  # crosses the threshold -> pending
    assert managed.state == dm.DEVICE_STATE_REINITIALIZE_PENDING
    assert manager.get_status_snapshot()["devices"]["active"] == 0

    # A successful reinitialization returns the device to ready.
    managed.clear_reinitialize_pending()
    assert managed.state == dm.DEVICE_STATE_READY
    assert manager.get_status_snapshot()["devices"]["active"] == 1
