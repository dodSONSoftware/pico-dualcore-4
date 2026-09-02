# test_core1_startup_heartbeat.py - Core 1 startup liveness coverage
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Host-side regression tests for Core 1 startup heartbeat coverage.

Before the fix, core1_main() registered core_1_activity_ms only AFTER device initialization and startup-log admission, and Core 0's watchdog (_watch_core_1_heartbeat) is a no-op until the first stamp exists. A Core 1 that wedged inside a driver constructor or a device initialize() call was therefore indistinguishable from one that had not started at all, and the watchdog could never fire.

The fix:

1. core1_main() stamps the activity mailbox at the top of its body, before SystemInformation/DeviceManager construction and initialize_devices().
2. Core 1 passes DeviceManager an optional activity_refresh callback, invoked at each initialization progress boundary (before each device, before each initialize() attempt, and at each step boundary inside each retry sleep — the delay is unbounded in config and is slept in steps of at most 100 ms), so a legitimate long initialization does not age the stamp past the watchdog bound while a wedged driver call stops the refresh and is caught.
"""

import importlib
import json
import os as _real_os
import pathlib
import sys
import time as _real_time
import types

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

ROOT = pathlib.Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Test 1: core1_main registers the stamp before device initialization
# ---------------------------------------------------------------------------

# Loop geometry: 20ms sleep + 20ms of processing per iteration (matches the
# hostile-phase geometry in test_core1_liveness.py; the phase is
# irrelevant here, but a non-divisor step keeps the loop deterministic).
PROCESSING_MS = 20
LOOP_STEP_MS = 20 + PROCESSING_MS
BOOT_TICKS_MS = 22
# Enough simulated time for core1_main to finish initialization, admit the
# startup log, enter the run loop, and then trip LoopStop in its first
# loop sleep.
STOP_AT_MS = BOOT_TICKS_MS + 4 * LOOP_STEP_MS

_NETWORK_SNAPSHOT = {
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
    """Controllable clock for driving the Core 1 loop on the host."""

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
    """Import/reload the core1 chain with the fakes authoritative."""
    names = (
        "hardware",
        "system_information",
        "devices",
        "devices.system_information",
        "devices.system_information.system_information_device",
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


class ProbeDriver:
    """Driver that records the activity stamp as Core 1 sees it.

    stamp_at_initialize is None when the liveness stamp has not been registered yet at the moment initialize() runs -- exactly the pre-fix gap this test guards."""

    def __init__(self, bus):
        self._bus = bus
        self.stamp_at_initialize = "UNSET"

    def initialize(self, config):
        self.stamp_at_initialize = self._bus.state_mailboxes.get_core_1_activity_ms()

    def read(self):
        return {"probe": 1}


def _capturing_system_information(core1_mod, bus):
    """Wrap core1's SystemInformation to record the stamp at construction.

    A wedge inside the SystemInformation/DeviceManager constructors is the pre-fix gap that ONLY the early core1_main stamp covers (the DeviceManager refresh callback cannot fire before the manager is constructed)."""
    real_system_information = core1_mod.SystemInformation
    stamp_at_construction = ["UNSET"]

    def capturing(intercore, config):
        stamp_at_construction[0] = intercore.state_mailboxes.get_core_1_activity_ms()
        return real_system_information(intercore, config)

    core1_mod.SystemInformation = capturing
    return stamp_at_construction, real_system_information


def _core1_config_with_probe():
    from config import split_config

    config = json.loads((ROOT / "config.json").read_text())
    _core0, core1_config = split_config(config)
    core1_config["devices"] = [
        {"id": "probe", "device_type": "probe", "config": {}}
    ]
    return core1_config


def test_core1_registers_activity_stamp_before_device_initialization():
    """The liveness stamp must exist before any initialization work runs.

    Drives the real core1_main and captures the stamp (1) at SystemInformation construction and (2) inside the probe device's initialize(). Pre-fix, both captures were None and a wedge anywhere in startup was invisible to Core 0's watchdog."""
    fake_time = FakeTime(BOOT_TICKS_MS, STOP_AT_MS)
    saved_modules = {name: sys.modules.get(name) for name in ("time", "machine", "os")}

    def _restore():
        for name, module in saved_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    try:
        _install_fakes(fake_time)
        _reload_core1_under_fakes()

        from intercore import InterCore
        import device_manager as dm

        bus = InterCore(minimum_free_heap_bytes=65536)
        bus.state_mailboxes.set_network_snapshot(dict(_NETWORK_SNAPSHOT))

        core1 = sys.modules["core1"]
        probe = ProbeDriver(bus)
        stamp_at_construction, real_si = _capturing_system_information(core1, bus)
        saved_create = dm.create_device
        dm.create_device = lambda device_def, system_information: probe
        try:
            # Before Core 1 runs, the stamp must not exist (fresh mailbox).
            assert bus.state_mailboxes.get_core_1_activity_ms() is None

            with pytest.raises(LoopStop):
                core1.core1_main(bus, _core1_config_with_probe(), BOOT_TICKS_MS, "test-runtime")
        finally:
            dm.create_device = saved_create
            core1.SystemInformation = real_si
    finally:
        _restore()

    # The regression assertions: the activity stamp was already registered
    # before SystemInformation construction and while the driver's
    # initialize() ran -- a wedge at either point would now age the stamp
    # and be caught by Core 0's watchdog instead of being indistinguishable
    # from "Core 1 has not started yet".
    assert stamp_at_construction[0] is not None
    assert probe.stamp_at_initialize != "UNSET"
    assert probe.stamp_at_initialize is not None

    # The mailbox holds a fresh stamp after core1_main ran.
    stamp = bus.state_mailboxes.get_core_1_activity_ms()
    assert stamp is not None
    assert fake_time.ticks_diff(fake_time.now_ms, stamp) <= STOP_AT_MS


# ---------------------------------------------------------------------------
# Tests 2-3: DeviceManager activity_refresh callback semantics
# ---------------------------------------------------------------------------

# device_manager imports device_factory -> system_information, which imports
# MicroPython-only modules at load time. Install a minimal fake if no other
# test has already provided one.
if "machine" not in sys.modules:
    machine_fake = types.ModuleType("machine")
    machine_fake.freq = staticmethod(lambda: 125000000)
    sys.modules["machine"] = machine_fake

class _HostTimeShim:
    """time stand-in for host-side DeviceManager tests.

    CPython's time module has no sleep_ms; provide a no-op sleep_ms (the retry delay is not under test) and delegate everything else to the real host time module."""

    def sleep_ms(self, ms):
        pass

    def __getattr__(self, name):
        return getattr(_real_time, name)


def _host_time_device_manager():
    """device_manager bound to a host-safe time module.

    An earlier test reloads the core1 chain while sys.modules["time"] is FakeTime, which binds the fake into device_manager's import time for the rest of the session; the plain host time module has no sleep_ms. Either way these tests need a deterministic time with a working sleep_ms, so bind one explicitly (rebinding dm's namespace attribute mutates no shared module)."""
    if "device_manager" in sys.modules:
        dm = importlib.reload(sys.modules["device_manager"])
    else:
        dm = importlib.import_module("device_manager")
    dm.time = _HostTimeShim()
    return dm


def _manager_config(attempts=3, retry_delay_ms=10):
    return {
        "device_initialization_attempts": attempts,
        "device_initialization_retry_delay_ms": retry_delay_ms,
        "device_read_failure_threshold": 3,
        "devices": [{"id": "dev1", "device_type": "probe", "config": {}}],
    }


def test_device_manager_refreshes_between_attempts():
    """The refresh callback must fire at each progress boundary.

    A driver that fails twice then succeeds must observe a STRICTLY INCREASING refresh count at each attempt: the stamp is refreshed before the first attempt and again between attempts (covering the retry delay), not just once up front."""
    refresh_log = []

    def refresh():
        refresh_log.append(1)

    class FlakyDriver:
        def __init__(self):
            self.observed = []
            self.calls = 0

        def initialize(self, config):
            self.calls += 1
            self.observed.append(len(refresh_log))
            if self.calls < 3:
                raise RuntimeError("simulated transient init failure")

    dm = _host_time_device_manager()
    driver = FlakyDriver()
    saved_create = dm.create_device
    dm.create_device = lambda device_def, system_information: driver
    try:
        manager = dm.DeviceManager(
            _manager_config(attempts=3),
            activity_refresh=refresh,
        )
        initialized, failed = manager.initialize_devices()
    finally:
        dm.create_device = saved_create

    # Normal initialization behavior is preserved: the device came up after
    # exactly the attempts the FlakyDriver needed (3), none failed.
    assert initialized == 1
    assert failed == []
    assert driver.calls == 3

    # Refresh at every progress boundary, strictly increasing per attempt.
    assert len(driver.observed) == 3
    assert driver.observed[0] >= 1  # armed before the first attempt
    assert driver.observed[1] > driver.observed[0]  # between attempts 1 and 2
    assert driver.observed[2] > driver.observed[1]  # between attempts 2 and 3


def test_device_manager_reinitialization_refreshes_between_attempts():
    """Runtime reinitialization uses the same progress-boundary strategy as startup.

    The refresh fires before every initialize() attempt and before each retry sleep (so the sleep itself cannot age the stamp past Core 0's watchdog bound); a wedge inside driver.initialize() still stops the refreshes and is caught."""
    refresh_log = []

    def refresh():
        refresh_log.append(1)

    class FlakyDriver:
        def __init__(self):
            self.calls = 0
            self.observed = []

        def initialize(self, config):
            self.calls += 1
            self.observed.append(len(refresh_log))
            if self.calls < 3:
                raise RuntimeError("simulated transient init failure")

    class RecordingTime(_HostTimeShim):
        """Records the refresh count at each retry sleep."""

        def __init__(self):
            self.sleep_counts = []

        def sleep_ms(self, ms):
            self.sleep_counts.append(len(refresh_log))

    class ManagedDeviceFake:
        """Minimal ManagedDevice stand-in for _process_reinitialization."""

        def __init__(self, driver):
            self.driver = driver
            self.device_id = "dev1"
            self.device_type = "probe"
            self.clears = 0

        def clear_reinitialize_pending(self):
            self.clears += 1

    recording_time = RecordingTime()
    dm = _host_time_device_manager()
    driver = FlakyDriver()
    managed = ManagedDeviceFake(driver)
    manager = dm.DeviceManager(
        _manager_config(attempts=3),
        activity_refresh=refresh,
    )
    dm.time = recording_time
    try:
        result = manager._process_reinitialization(managed)
    finally:
        dm.time = _HostTimeShim()

    # Normal reinitialization behavior is preserved.
    assert result["status"] == dm.DEVICE_RESULT_REINITIALIZED
    assert result["reinitialization_attempts_used"] == 3
    assert managed.clears == 1

    # Before every attempt the stamp is current (strictly increasing refresh
    # count), and each retry sleep (10 ms here, one 100-ms-bounded step) ends
    # in a refresh: with 3 attempts and 2 failures that is 3 (attempts) +
    # 2 (one per sleep step) = 5 refreshes total, and each sleep runs after
    # the attempt-boundary refresh of the failed attempt.
    assert len(refresh_log) == 5
    assert driver.observed == [1, 3, 5]
    assert recording_time.sleep_counts == [1, 3]


def test_device_manager_retry_sleep_refreshes_in_steps():
    """A long configured retry delay must not run as one monolithic sleep.

    device_initialization_retry_delay_ms is a non-negative config value with no upper bound: a delay at or past Core 0's 30 s watchdog bound would previously age the liveness stamp into a false machine.reset() during a legitimate retry backoff. The delay is now slept in steps of at most 100 ms, the liveness stamp is refreshed at each step boundary, and the total slept is exactly the configured delay."""
    refresh_log = []

    def refresh():
        refresh_log.append(1)

    class FlakyDriver:
        def __init__(self):
            self.calls = 0

        def initialize(self, config):
            self.calls += 1
            if self.calls < 2:
                raise RuntimeError("simulated transient init failure")

    class StepTime(_HostTimeShim):
        """Records each sleep step and the refresh count at that moment."""

        def __init__(self):
            self.steps = []
            self.refresh_at_step = []

        def sleep_ms(self, ms):
            self.steps.append(ms)
            self.refresh_at_step.append(len(refresh_log))

    delay_ms = 2500
    step_time = StepTime()
    dm = _host_time_device_manager()
    driver = FlakyDriver()
    saved_create = dm.create_device
    dm.create_device = lambda device_def, system_information: driver
    dm.time = step_time
    try:
        manager = dm.DeviceManager(
            _manager_config(attempts=2, retry_delay_ms=delay_ms),
            activity_refresh=refresh,
        )
        initialized, failed = manager.initialize_devices()
    finally:
        dm.create_device = saved_create
        dm.time = _HostTimeShim()

    # Normal initialization behavior is preserved.
    assert initialized == 1
    assert failed == []

    # The delay is split into steps of at most 100 ms summing to the delay.
    assert len(step_time.steps) == delay_ms // 100
    assert all(step <= 100 for step in step_time.steps)
    assert sum(step_time.steps) == delay_ms

    # A refresh at each step boundary: 1 (device) + 1 (attempt 1) + 25 (sleep
    # steps) + 1 (attempt 2) = 28, with each step's refresh strictly after the
    # last recorded count -- no refresh goes missing inside the sleep.
    assert step_time.refresh_at_step == list(range(2, 2 + len(step_time.steps)))
    assert len(refresh_log) == 28


def test_device_manager_without_refresh_callback_is_unchanged():
    """The callback is optional; default behavior is unchanged."""
    class OkDriver:
        def initialize(self, config):
            pass

    dm = _host_time_device_manager()
    driver = OkDriver()
    saved_create = dm.create_device
    dm.create_device = lambda device_def, system_information: driver
    try:
        manager = dm.DeviceManager(_manager_config(attempts=1))
        assert manager._activity_refresh is None
        initialized, failed = manager.initialize_devices()
    finally:
        dm.create_device = saved_create

    assert initialized == 1
    assert failed == []


def test_device_manager_normal_read_refreshes_per_device():
    """A telemetry pass must refresh the stamp at each device boundary.

    Six legitimate reads of 6 s each (total 36 s > Core 0's 30 s watchdog bound) must not let the stamp age across the pass: the refresh fires before every device operation, not once after the whole pass. Pre-fix, only the post-pass loop refresh existed, so the last ~15 s of a legitimate pass aged the stamp past the bound into a false machine.reset()."""
    READ_MS = 6000
    WATCHDOG_MS = 30000
    device_count = 6  # 6 x 6000 ms = 36000 ms > WATCHDOG_MS

    refresh_log = []

    def refresh():
        refresh_log.append(1)

    class PassTime(_HostTimeShim):
        """Ticks-based pass clock; each read advances it by READ_MS.

        _process_normal_read records last_read_ms via time.ticks_ms(), so the bound time must provide the ticks API (the host time shim delegates to CPython, which has none)."""

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

        def advance_read(self):
            self.now_ms += READ_MS

    pass_time = PassTime()

    class SlowDriver:
        def __init__(self, observed):
            self.observed = observed

        def initialize(self, config):
            pass

        def read(self):
            # Record the refresh count at the moment this read begins, then
            # consume READ_MS of the simulated pass.
            self.observed.append(len(refresh_log))
            pass_time.advance_read()
            return {"slow": 1}

    observed = []
    dm = _host_time_device_manager()
    saved_create = dm.create_device
    saved_time = dm.time
    dm.create_device = (
        lambda device_def, system_information: SlowDriver(observed)
    )
    dm.time = pass_time
    try:
        config = _manager_config(attempts=1)
        config["devices"] = [
            {"id": "dev{}".format(i), "device_type": "probe", "config": {}}
            for i in range(device_count)
        ]
        manager = dm.DeviceManager(config, activity_refresh=refresh)
        initialized, failed = manager.initialize_devices()
        assert initialized == device_count
        assert failed == []

        # One telemetry pass, the same loop core1's _run_telemetry_read_pass
        # runs (initial and periodic passes share it).
        for managed_device in manager.get_active_devices():
            result = manager.process_device(managed_device)
            assert result["status"] == dm.DEVICE_RESULT_TELEMETRY
    finally:
        dm.create_device = saved_create
        dm.time = saved_time

    # The premise the watchdog bound was violated by: each read is under the
    # bound, the whole pass is over it.
    assert READ_MS < WATCHDOG_MS
    assert pass_time.now_ms > WATCHDOG_MS
    assert pass_time.now_ms == READ_MS * device_count

    # The regression assertion: a refresh armed before every read, strictly
    # later than the one before the previous read -- i.e. the stamp is
    # refreshed in between, so no two consecutive reads ever straddle an
    # unrefreshed gap of even 2 x READ_MS, let alone the watchdog bound.
    # (A wedge inside a single read still stops refreshes and ages the stamp;
    # that is the wedge the watchdog exists to catch.)
    assert observed[0] >= 1  # armed before the first read
    for i in range(1, device_count):
        assert observed[i] > observed[i - 1]
