# test_boot_failure_retry.py - Boot-failure late recovery (steady-state retry)
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Host-side tests for the steady-state retry of boot-failed devices.

A device that exhausts its boot initialization attempts must not stay failed until reboot:

- a boot-failed device is retried at a slow steady-state interval (the configured initialization retry delay with a 1 s floor) by one bounded initialize() attempt per device per pass
- a recovered device is promoted to the normal active lifecycle with a cumulative attempt count, and the failure record is cleared
- a failed late attempt updates the failure record (cumulative attempts, last error) without re-running the attempts x delay inner loop
- a driver-construction failure updates the record without incrementing the attempt count (no initialize() ran)
- a MemoryError from a late initialize() propagates to the recovery boundary
- the first steady-state retry failure is warned once, repeats are suppressed, and the Core 1 loop runs no retry pass at all before the first interval boundary or once no boot-failed devices remain
- a zero configured delay still gets the 1 s floor (no per-tick probing, no degenerate scheduler)"""

import importlib
import json
import os as _real_os
import pathlib
import sys
import time as _real_time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from config import split_config  # noqa: E402
from intercore import (  # noqa: E402
    InterCore,
    KIND_LOG,
    KIND_TELEMETRY,
)


ROOT = pathlib.Path(__file__).resolve().parents[1]
BOOT_TICKS_MS = 100000

# Loop geometry: 20ms sleep + 20ms of processing per iteration, so the loop
# advances 40ms per iteration.
PROCESSING_MS = 20
LOOP_STEP_MS = 20 + PROCESSING_MS


# ---------------------------------------------------------------------------
# DeviceManager-level tests (no Core 1 loop; attempts=1 so no retry sleep)
# ---------------------------------------------------------------------------

class FlipDriver:
    """Driver whose initialize() fails for the first fail_times calls."""

    def __init__(self, fail_times):
        self.fail_times = fail_times
        self.raise_memory_error = False
        self.initialize_calls = 0

    def initialize(self, config):
        self.initialize_calls += 1
        if self.raise_memory_error:
            raise MemoryError("simulated heap exhaustion")
        if self.initialize_calls <= self.fail_times:
            raise RuntimeError("simulated initialization failure")

    def read(self):
        return {"value": 1}


def _unit_manager(devices, attempts=1):
    """A fresh DeviceManager under host imports (reloaded so any cached
    fake-time binding from an earlier test cannot leak in)."""
    saved = sys.modules.get("device_manager")
    dm_mod = importlib.import_module("device_manager")
    if saved is not None:
        dm_mod = importlib.reload(saved)

    config = {
        "device_initialization_attempts": attempts,
        "device_initialization_retry_delay_ms": 10,
        "device_read_failure_threshold": 3,
        "devices": devices,
    }
    return dm_mod, dm_mod.DeviceManager(config), saved


@pytest.fixture
def unit():
    dm_mod, dm, saved = _unit_manager(
        [{"id": "dev1", "device_type": "test", "config": {}}]
    )

    driver = FlipDriver(fail_times=99)
    saved_create_device = dm_mod.create_device
    dm_mod.create_device = lambda device_def, i2c_bus_factory=None: driver

    yield dm_mod, dm, driver

    dm_mod.create_device = saved_create_device
    if saved is None:
        sys.modules.pop("device_manager", None)
    else:
        sys.modules["device_manager"] = saved


def test_boot_failure_is_recorded_and_device_is_inactive(unit):
    """Baseline: an exhausted boot failure is recorded, not active, and the
    late-retry gate sees it."""
    _dm_mod, dm, _driver = unit

    initialized = dm.initialize_devices()

    assert initialized == 0
    assert dm.has_failed_devices() is True
    assert dm.get_active_devices() == []
    record = dm._failed_devices["dev1"]
    assert record["state"] == "initialization_failed"
    assert record["initialization_attempts_used"] == 1


def test_late_retry_recovers_device(unit):
    """One initialize() attempt per pass; recovery promotes the device to the
    normal active lifecycle with the cumulative attempt count."""
    _dm_mod, dm, driver = unit
    driver.fail_times = 3  # boot: 1, pass 1: 2, pass 2: 3, pass 3: 4 (success)
    dm.initialize_devices()

    result = dm.retry_failed_devices()[0]
    assert result["status"] == "boot_recovery_failed"
    assert result["initialization_attempts_used"] == 2
    assert result["log_failure_warning"] is True

    result = dm.retry_failed_devices()[0]
    assert result["status"] == "boot_recovery_failed"
    assert result["initialization_attempts_used"] == 3
    # Repeat failures for the same device are suppressed until recovery.
    assert result["log_failure_warning"] is False

    result = dm.retry_failed_devices()[0]
    assert result["status"] == "boot_recovered"
    assert result["initialization_attempts_used"] == 4

    # Promoted: active, READY, cumulative attempts, failure record cleared.
    active = dm.get_active_devices()
    assert len(active) == 1
    assert active[0].device_id == "dev1"
    assert active[0].state == "ready"
    assert active[0].initialization_attempts_used == 4
    assert active[0].consecutive_read_failures == 0
    assert dm.has_failed_devices() is False
    assert dm.get_device_counts() == {
        "configured": 1,
        "active": 1,
        "initialization_failed": 0,
    }
    # Nothing left to retry: the pass is a no-op.
    assert dm.retry_failed_devices() == []


def test_late_retry_construction_failure_keeps_attempt_count(unit):
    """A create_device() failure on a late pass records the error without
    incrementing the attempt count (no initialize() ran)."""
    dm_mod, dm, _driver = unit
    dm.initialize_devices()

    def _failing_create(device_def, i2c_bus_factory=None):
        raise RuntimeError("simulated construction failure")

    dm_mod.create_device = _failing_create

    result = dm.retry_failed_devices()[0]
    assert result["status"] == "boot_recovery_failed"
    record = dm._failed_devices["dev1"]
    assert record["initialization_attempts_used"] == 1
    assert record["failure_reason"] == "simulated construction failure"
    assert dm.has_failed_devices() is True


def test_late_retry_memory_error_propagates(unit):
    """A MemoryError from a late initialize() escapes to the recovery
    boundary; the failure record is left for the next pass."""
    _dm_mod, dm, driver = unit
    dm.initialize_devices()
    driver.raise_memory_error = True

    with pytest.raises(MemoryError):
        dm.retry_failed_devices()

    record = dm._failed_devices["dev1"]
    assert record["initialization_attempts_used"] == 1
    assert dm.has_failed_devices() is True


def test_late_retry_skips_active_devices_and_keeps_config_order():
    """The pass touches only boot-failed devices (an active device's driver
    is never re-initialized), and a recovery lands in configuration order."""
    devices = [
        {"id": "dev_a", "device_type": "test", "config": {}},
        {"id": "dev_b", "device_type": "test", "config": {}},
    ]
    dm_mod, dm, saved = _unit_manager(devices)

    driver_a = FlipDriver(fail_times=0)   # healthy at boot
    driver_b = FlipDriver(fail_times=3)   # boot: 1, pass 3: 4 (success)
    saved_create_device = dm_mod.create_device
    dm_mod.create_device = lambda device_def, i2c_bus_factory=None: (
        driver_a if device_def["id"] == "dev_a" else driver_b
    )

    try:
        initialized = dm.initialize_devices()
        assert initialized == 1
        assert [d.device_id for d in dm.get_active_devices()] == ["dev_a"]

        for _ in range(3):
            dm.retry_failed_devices()

        assert [d.device_id for d in dm.get_active_devices()] == [
            "dev_a",
            "dev_b",
        ]
        assert driver_a.initialize_calls == 1  # never re-initialized
        assert dm.has_failed_devices() is False
    finally:
        dm_mod.create_device = saved_create_device
        if saved is None:
            sys.modules.pop("device_manager", None)
        else:
            sys.modules["device_manager"] = saved


# ---------------------------------------------------------------------------
# Core 1 loop-level tests (real core1_main under a controllable clock)
# ---------------------------------------------------------------------------

def _ready_network_snapshot():
    return {
        "ssid": "test-ssid",
        "ip_address": "192.168.1.100",
        "netmask": "255.255.255.0",
        "gateway": "192.168.1.1",
        "dns": "192.168.1.1",
        "rssi": -50,
        "wifi_connected": True,
        "mqtt_connected": True,
        "network_stack_ready": True,
        "wifi_connect_count": 1,
        "wifi_disconnect_count": 0,
        "mqtt_connect_count": 1,
        "mqtt_disconnect_count": 0,
    }


class LoopStop(Exception):
    """Raised by FakeTime to end the infinite Core 1 loop deterministically."""


class FakeTime:
    """Controllable clock for driving the Core 1 loop on the host.

    sleep_ms adds PROCESSING_MS to model the ~20ms of per-iteration work, so the loop advances 40ms per iteration."""

    def __init__(self, start_ms, stop_after_ms):
        self.now_ms = start_ms
        self.stop_after_ms = stop_after_ms

    def ticks_ms(self):
        return self.now_ms

    def ticks_diff(self, now, prev):
        return now - prev

    def ticks_add(self, base, delta):
        return base + delta

    def sleep_ms(self, ms):
        self.now_ms += ms + PROCESSING_MS
        if self.now_ms >= self.stop_after_ms:
            raise LoopStop()

    def __getattr__(self, name):
        return getattr(_real_time, name)


class FakeMachine:
    """Minimal MicroPython ``machine`` stand-in (CPU frequency source)."""

    @staticmethod
    def freq():
        return 125000000


class _Uname:
    sysname = "MicroPython"
    nodename = "pico"
    release = "v1.23.0"
    version = "v1.23.0"
    machine = "Raspberry Pi Pico W with RP2040"


class FakeOs:
    """``os`` stand-in reporting a Pico W machine string from uname()."""

    def uname(self):
        return _Uname()

    def __getattr__(self, name):
        return getattr(_real_os, name)


def _install_fakes(fake_time):
    sys.modules["time"] = fake_time
    sys.modules["machine"] = FakeMachine()
    sys.modules["os"] = FakeOs()


def _reload_core1_under_fakes():
    """Import/reload the core1 chain with the fakes authoritative.

    core1 binds time/machine/os from sys.modules at import time, so any cached module (possibly imported under host or other-test stand-ins) is reloaded in dependency order before core1 itself."""
    names = (
        "hardware",
        "system_information",
        "devices",
        "device_factory",
        "device_manager",
        "uptime",
        "core1",
    )
    for name in names[:-1]:
        if name in sys.modules:
            importlib.reload(sys.modules[name])
    core1 = sys.modules.get("core1")
    if core1 is None:
        core1 = importlib.import_module("core1")
    else:
        core1 = importlib.reload(core1)
    return core1


class FlakyDriver:
    """Boot-failing driver; becomes healthy after healthy_after_call
    initialize() calls (None: never). Records the tick of every call."""

    def __init__(self, fake_time, healthy_after_call):
        self._fake_time = fake_time
        self.healthy_after_call = healthy_after_call
        self.initialize_ticks = []

    def initialize(self, config):
        self.initialize_ticks.append(self._fake_time.ticks_ms())
        if (
            self.healthy_after_call is None
            or len(self.initialize_ticks) < self.healthy_after_call
        ):
            raise RuntimeError("simulated initialization failure")

    def read(self):
        return {"probe": 1}


def _core1_config(retry_delay_ms=None):
    """Core 1 config with a single probe device; the fixture's device set is
    replaced (the fixture's real devices need a hardware bus)."""
    config = json.loads((ROOT / "tests" / "fixtures" / "config.json").read_text())
    _core0, core1_config = split_config(config)
    core1_config["devices"] = [
        {"id": "probe", "device_type": "probe", "config": {}}
    ]
    if retry_delay_ms is not None:
        core1_config["device_initialization_retry_delay_ms"] = retry_delay_ms
    return core1_config


def _run_core1(fake_time, bus, core1_config, boot_ticks_ms, driver):
    """Drive core1_main under the fakes until FakeTime stops the loop.

    The probe device's driver is the shared flaky instance: every
    initialize() call (boot and late passes) goes through it."""
    saved_modules = {name: sys.modules.get(name) for name in ("time", "machine", "os")}

    def _restore():
        for name, module in saved_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    try:
        _install_fakes(fake_time)
        core1 = _reload_core1_under_fakes()
        dm_mod = sys.modules["device_manager"]
        saved_create_device = dm_mod.create_device
        dm_mod.create_device = lambda device_def, i2c_bus_factory=None: driver

        try:
            with pytest.raises(LoopStop):
                core1.core1_main(bus, core1_config, boot_ticks_ms, "test-runtime")
        finally:
            dm_mod.create_device = saved_create_device
    finally:
        _restore()


def _drain_outbound(bus):
    """Drain the outbound queue in admission order.

    Returns (telemetry_payloads, other_entries); the startup log is the
    log entry under test here."""
    telemetry = []
    others = []
    while True:
        entry = bus.outbound_queue.take()
        if entry is None:
            break
        if entry["kind"] == KIND_TELEMETRY:
            telemetry.append(json.loads(entry["payload_bytes"].decode("utf-8")))
        else:
            others.append(entry)
        bus.outbound_queue.complete_in_flight(entry)
    return telemetry, others


def _anchor_abs_ticks(others):
    """The normal-runtime anchor in absolute ticks, read off the startup
    log's uptime (the fake clock makes uptime the exact anchor offset)."""
    startup_log = json.loads(others[0]["payload_bytes"].decode("utf-8"))
    assert others[0]["kind"] == KIND_LOG
    assert startup_log["payload"]["event"] == "system_startup_completed"
    return BOOT_TICKS_MS + startup_log["uptime_ms"]


def test_boot_failed_device_retries_at_interval_and_recovers(capsys):
    """The boot-failed probe is retried one attempt per interval boundary:
    one failed pass at anchor + 1 s (warned once), a successful pass at
    anchor + 2 s (INFO), and no further initialize() calls. The recovered
    device then telemetry on its own read boundary (the at-anchor sample
    saw no active devices, so there is exactly one telemetry in the run)."""
    startup_at_ms = BOOT_TICKS_MS + 13000

    bus = InterCore(minimum_free_heap_bytes=65536)
    bus.state_mailboxes.set_network_snapshot(dict(_ready_network_snapshot()))

    fake_time = FakeTime(startup_at_ms, startup_at_ms + 22000 + 2 * LOOP_STEP_MS)
    # Fixture: 3 boot attempts; calls 4 and 5 are the first two late passes.
    driver = FlakyDriver(fake_time, healthy_after_call=5)
    _run_core1(fake_time, bus, _core1_config(), BOOT_TICKS_MS, driver)
    out = capsys.readouterr().out

    telemetry, others = _drain_outbound(bus)
    anchor = _anchor_abs_ticks(others)
    ticks = driver.initialize_ticks

    # 3 boot attempts, then exactly two late passes (one per boundary).
    assert len(ticks) == 5
    # The first late attempt is at the first 1 s boundary, not before...
    assert 1000 <= ticks[3] - anchor <= 1000 + LOOP_STEP_MS
    # ...and the recovery at the second boundary.
    assert 2000 <= ticks[4] - anchor <= 2000 + LOOP_STEP_MS
    # And nothing after recovery: the loop stops retrying.
    assert all(t <= ticks[4] for t in ticks)

    # First (only) late failure warned once; the recovery logged once.
    assert out.count("boot initialization retry failed") == 1
    assert out.count("recovered from boot failure") == 1

    # One telemetry: the recovered device's first read boundary (anchor + 20 s).
    assert len(telemetry) == 1
    assert 20000 <= telemetry[0]["uptime_ms"] - (anchor - BOOT_TICKS_MS) <= (
        20000 + LOOP_STEP_MS
    )


def test_no_late_retry_before_the_first_boundary(capsys):
    """No retry pass runs before anchor + interval: a run that ends inside
    the first interval makes no late initialize() calls and logs no retry."""
    startup_at_ms = BOOT_TICKS_MS + 13000

    bus = InterCore(minimum_free_heap_bytes=65536)
    bus.state_mailboxes.set_network_snapshot(dict(_ready_network_snapshot()))

    # The boot pass itself sleeps 2 x 250 ms under the fake clock, so the
    # anchor lands ~540 ms past startup_at_ms; stopping at startup_at_ms +
    # 1400 ends the run inside the first 1 s interval.
    fake_time = FakeTime(startup_at_ms, startup_at_ms + 1400)
    driver = FlakyDriver(fake_time, healthy_after_call=None)
    _run_core1(fake_time, bus, _core1_config(), BOOT_TICKS_MS, driver)
    out = capsys.readouterr().out

    # Only the 3 boot attempts ran.
    assert len(driver.initialize_ticks) == 3
    assert "boot initialization retry failed" not in out
    assert "recovered from boot failure" not in out


def test_zero_retry_delay_gets_the_one_second_floor(capsys):
    """device_initialization_retry_delay_ms is legal at 0; the steady-state
    interval still floors at 1 s (no per-tick probing, no degenerate
    scheduler). With the floor, the failed late pass is at anchor + 1 s
    and the recovery at anchor + 2 s."""
    startup_at_ms = BOOT_TICKS_MS + 13000

    bus = InterCore(minimum_free_heap_bytes=65536)
    bus.state_mailboxes.set_network_snapshot(dict(_ready_network_snapshot()))

    # Zero delay: the boot attempts do not sleep, so the anchor lands at
    # startup_at_ms (plus loop geometry).
    fake_time = FakeTime(startup_at_ms, startup_at_ms + 2600 + 2 * LOOP_STEP_MS)
    driver = FlakyDriver(fake_time, healthy_after_call=5)
    _run_core1(fake_time, bus, _core1_config(retry_delay_ms=0), BOOT_TICKS_MS, driver)
    out = capsys.readouterr().out

    _telemetry, others = _drain_outbound(bus)
    anchor = _anchor_abs_ticks(others)
    ticks = driver.initialize_ticks

    assert len(ticks) == 5
    assert 1000 <= ticks[3] - anchor <= 1000 + LOOP_STEP_MS
    assert 2000 <= ticks[4] - anchor <= 2000 + LOOP_STEP_MS
    assert out.count("recovered from boot failure") == 1
