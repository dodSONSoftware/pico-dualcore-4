# test_device_exception_taxonomy.py - DeviceManager exception taxonomy at the recovery boundary
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Host-side tests for the DeviceManager operational-failure-domain contract.

Only OSError is a recoverable device failure: every driver normalizes its hardware/I2C failures to it, so each of the manager's recovery paths (driver construction, boot initialization, the normal read, the runtime reinitialization, and the boot-failure late-retry pass) catches OSError beside the MemoryError re-raise. A contract violation -- the manager's own TypeError for a read() that is not a non-empty JSON-safe dict, a ValueError from a driver that reached its validator despite the config-boundary validation (a DeviceValidationError), a RuntimeError/ArithmeticError state defect, or the I2C bus factory's same-bus conflict -- is a firmware defect that reinitializing cannot repair: it escapes DeviceManager to Core 1's worker boundary (the dead worker is recovered by Core 0's heartbeat watchdog) instead of being reclassified as a failed sensor and retried into the same deterministic fault."""

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
BOOT_TICKS_MS = 100000

# device_manager imports device_factory -> system_information, which imports
# MicroPython-only modules at load time. Install a minimal fake before import.
if "machine" not in sys.modules:
    machine_fake = types.ModuleType("machine")
    machine_fake.freq = staticmethod(lambda: 125000000)
    sys.modules["machine"] = machine_fake

import device_manager as dm  # noqa: E402
from config import split_config  # noqa: E402
from devices.device import DeviceValidationError  # noqa: E402
from intercore import InterCore  # noqa: E402


class _HostTicks:
    """MicroPython ticks shim: CPython's time has no ticks_ms/sleep_ms
    contract, so the manager-level tests bind this to the module (the same
    pattern test_core1_startup_heartbeat applies per test)."""

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

    def __getattr__(self, name):
        return getattr(_real_time, name)


@pytest.fixture(autouse=True)
def _hermetic_clock():
    """Bind a host ticks clock to the manager module for the test, and
    restore the prior binding on exit: other test modules leave different
    time bindings on the shared (reloaded) module object, and the read-path
    tests need ticks_ms regardless of which module ran last."""
    saved = dm.time
    dm.time = _HostTicks()
    yield
    dm.time = saved


# ---------------------------------------------------------------------------
# Manager-level tests (no Core 1 loop)
# ---------------------------------------------------------------------------

class FakeDriver:
    """Driver with switchable initialize/read behaviors; records init calls."""

    def __init__(self, init_error=None, read_error=None, read_result=None):
        self.init_error = init_error
        self.read_error = read_error
        self.read_result = read_result
        self.initialize_calls = 0

    def initialize(self, config):
        self.initialize_calls += 1
        if self.init_error is not None:
            raise self.init_error

    def read(self):
        if self.read_error is not None:
            raise self.read_error
        return self.read_result


_DEV = [{"id": "dev1", "device_type": "test", "config": {}}]


def _manager(devices, attempts=1, threshold=1):
    # Zero retry delay: the OSError retry paths sleep between attempts, and
    # a host test must not pay real time for the taxonomy under test.
    return dm.DeviceManager({
        "device_initialization_attempts": attempts,
        "device_initialization_retry_delay_ms": 0,
        "device_read_failure_threshold": threshold,
        "devices": devices,
    })


def _managed(manager, driver):
    managed = dm.ManagedDevice(device_id="dev1", device_type="test", driver=driver)
    manager._active_devices.append(managed)
    return managed


# --- the normal read path ---------------------------------------------------

def test_oserror_read_is_a_read_failure():
    """OSError is the operational domain: it is a sensor condition, counted
    and retried toward reinitialization."""
    manager = _manager(_DEV, threshold=1)
    managed = _managed(manager, FakeDriver(read_error=OSError("simulated I2C bus drop")))

    result = manager.process_device(managed)

    assert result["status"] == dm.DEVICE_RESULT_READ_FAILED
    assert managed.consecutive_read_failures == 1
    assert managed.state == dm.DEVICE_STATE_REINITIALIZE_PENDING


@pytest.mark.parametrize("error", [
    TypeError("simulated driver contract violation"),
    ValueError("simulated validator bypassed"),
    RuntimeError("simulated driver state defect"),
    ArithmeticError("simulated compensation defect"),
    MemoryError("simulated heap exhaustion"),
])
def test_contract_error_read_escapes(error):
    """A non-OSError from read() is never reclassified as a sensor failure:
    it escapes the manager with its actual type, and the device state is
    untouched (no failure counted, no reinit requested)."""
    manager = _manager(_DEV, threshold=1)
    managed = _managed(manager, FakeDriver(read_error=error))

    with pytest.raises(type(error)):
        manager.process_device(managed)

    assert managed.consecutive_read_failures == 0
    assert managed.total_read_failures == 0
    assert managed.successful_read_count == 0
    assert managed.state == dm.DEVICE_STATE_READY


def test_non_dict_read_return_escapes():
    """The manager's own contract check (read() must return a non-empty
    dict) raises a TypeError that is a firmware defect, not a read failure."""
    manager = _manager(_DEV, threshold=1)
    managed = _managed(manager, FakeDriver(read_result="42"))

    with pytest.raises(TypeError):
        manager.process_device(managed)

    assert managed.consecutive_read_failures == 0


def test_non_json_safe_read_return_escapes():
    """A read() return that is not JSON-safe (e.g. an object value) trips
    the manager's own contract check and escapes."""
    manager = _manager(_DEV, threshold=1)
    managed = _managed(manager, FakeDriver(read_result={"value": object()}))

    with pytest.raises(TypeError):
        manager.process_device(managed)

    assert managed.consecutive_read_failures == 0


# --- the boot initialization path -------------------------------------------

def test_oserror_init_is_retried_and_recorded():
    """OSError is the operational domain: the full attempt budget is spent
    and the device is recorded for the late-retry pass."""
    manager = _manager(_DEV, attempts=3)
    driver = FakeDriver(init_error=OSError("simulated missing chip"))
    saved = dm.create_device
    dm.create_device = lambda device_def, i2c_bus_factory=None: driver
    try:
        initialized = manager.initialize_devices()
    finally:
        dm.create_device = saved

    assert initialized == 0
    assert driver.initialize_calls == 3
    record = manager._failed_devices["dev1"]
    assert record["state"] == dm.DEVICE_STATE_INITIALIZATION_FAILED
    assert record["initialization_attempts_used"] == 3


@pytest.mark.parametrize("error", [
    ValueError("simulated validator bypassed"),
    DeviceValidationError("simulated validator bypassed", code="invalid_value"),
    RuntimeError("simulated driver state defect"),
    MemoryError("simulated heap exhaustion"),
])
def test_contract_error_init_escapes_without_retry(error):
    """A non-OSError from initialize() is deterministic: the retry budget
    is not burned re-hitting the same fault, and no failed-device record is
    written (there is nothing to retry later)."""
    manager = _manager(_DEV, attempts=3)
    driver = FakeDriver(init_error=error)
    saved = dm.create_device
    dm.create_device = lambda device_def, i2c_bus_factory=None: driver
    try:
        with pytest.raises(type(error)):
            manager.initialize_devices()
    finally:
        dm.create_device = saved

    assert driver.initialize_calls == 1
    assert manager._failed_devices == {}


# --- the driver construction path --------------------------------------------

def test_oserror_construction_is_recorded():
    """An OSError from create_device() (e.g. the bus construction) is an
    operational failure: recorded with the honest 0 attempts."""
    manager = _manager(_DEV, attempts=3)

    def _explode(device_def, i2c_bus_factory=None):
        raise OSError("simulated bus construction failure")

    saved = dm.create_device
    dm.create_device = _explode
    try:
        initialized = manager.initialize_devices()
    finally:
        dm.create_device = saved

    assert initialized == 0
    record = manager._failed_devices["dev1"]
    assert record["initialization_attempts_used"] == 0
    assert record["failure_reason"] == "simulated bus construction failure"


@pytest.mark.parametrize("error", [
    ValueError("simulated I2C bus same-bus conflict"),
    RuntimeError("simulated factory defect"),
])
def test_contract_error_construction_escapes(error):
    """A non-OSError from create_device() (the factory's unsupported-type
    ValueError, the I2C bus factory's same-bus conflict) is a
    configuration/programming error: recording it as a failed device would
    hide it behind the retry machinery, so it escapes."""
    manager = _manager(_DEV, attempts=3)

    def _explode(device_def, i2c_bus_factory=None):
        raise error

    saved = dm.create_device
    dm.create_device = _explode
    try:
        with pytest.raises(type(error)):
            manager.initialize_devices()
    finally:
        dm.create_device = saved


# --- the runtime reinitialization path ----------------------------------------

def test_oserror_reinit_is_retried_and_stays_pending():
    """OSError is the operational domain: the reinit attempt budget is spent
    and the device stays reinitialize_pending for the next cycle."""
    manager = _manager(_DEV, attempts=3)
    driver = FakeDriver(init_error=OSError("simulated bus drop"))
    managed = _managed(manager, driver)
    managed.mark_reinitialize_pending()

    result = manager.process_device(managed)

    assert result["status"] == dm.DEVICE_RESULT_REINITIALIZATION_FAILED
    assert driver.initialize_calls == 3
    assert managed.state == dm.DEVICE_STATE_REINITIALIZE_PENDING


@pytest.mark.parametrize("error", [
    ValueError("simulated validator bypassed"),
    RuntimeError("simulated driver state defect"),
    MemoryError("simulated heap exhaustion"),
])
def test_contract_error_reinit_escapes_without_retry(error):
    """A non-OSError from a reinit initialize() is deterministic: the reinit
    attempt budget is not burned, and the device is left pending (the worker
    is dying, so no further cycle will run)."""
    manager = _manager(_DEV, attempts=3)
    driver = FakeDriver(init_error=error)
    managed = _managed(manager, driver)
    managed.mark_reinitialize_pending()

    with pytest.raises(type(error)):
        manager.process_device(managed)

    assert driver.initialize_calls == 1
    assert managed.state == dm.DEVICE_STATE_REINITIALIZE_PENDING


# --- the boot-failure late-retry path ------------------------------------------

def test_late_retry_oserror_is_recorded():
    """An OSError from a late initialize() updates the failure record
    (cumulative attempt, last error) for the next pass."""
    driver = FakeDriver(init_error=OSError("simulated missing chip"))
    manager = _manager(_DEV, attempts=1)
    saved = dm.create_device
    dm.create_device = lambda device_def, i2c_bus_factory=None: driver
    try:
        manager.initialize_devices()
        result = manager.retry_failed_devices()[0]
    finally:
        dm.create_device = saved

    assert result["status"] == dm.DEVICE_RESULT_BOOT_RECOVERY_FAILED
    record = manager._failed_devices["dev1"]
    assert record["initialization_attempts_used"] == 2
    assert record["failure_reason"] == "simulated missing chip"


@pytest.mark.parametrize("error", [
    ValueError("simulated I2C bus same-bus conflict"),
    RuntimeError("simulated factory defect"),
])
def test_late_retry_contract_error_construction_escapes(error):
    """A non-OSError from a late create_device() escapes instead of being
    recorded for the next pass: re-probing a deterministic configuration
    error on every steady-state interval would only hide it."""
    manager = _manager(_DEV, attempts=1)

    def _explode(device_def, i2c_bus_factory=None):
        raise error

    saved = dm.create_device
    try:
        dm.create_device = lambda device_def, i2c_bus_factory=None: FakeDriver(
            init_error=OSError("simulated missing chip")
        )
        manager.initialize_devices()
        dm.create_device = _explode
        with pytest.raises(type(error)):
            manager.retry_failed_devices()
    finally:
        dm.create_device = saved


# ---------------------------------------------------------------------------
# Core 1 loop-level test (real core1_main under a controllable clock)
# ---------------------------------------------------------------------------

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
        self.now_ms += ms
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


class ContractViolatingDriver:
    """Driver that initializes but violates the read() return contract."""

    def initialize(self, config):
        pass

    def read(self):
        raise TypeError("simulated driver contract violation")


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


def test_contract_violation_terminates_the_core1_worker(capsys):
    """The impact scenario at the loop level: a read() contract violation
    no longer spins read -> TypeError -> sensor failure -> reinit forever.
    It escapes core1_main and kills the worker (Core 0's heartbeat watchdog
    then recovers the board); the loop is never entered, so the failure is
    visible from the very first at-anchor telemetry sample."""
    bus = InterCore(minimum_free_heap_bytes=65536)
    bus.state_mailboxes.set_network_snapshot(dict(_ready_network_snapshot()))

    fake_time = FakeTime(BOOT_TICKS_MS, BOOT_TICKS_MS + 30000)
    config = json.loads((ROOT / "tests" / "fixtures" / "config.json").read_text())
    _core0, core1_config = split_config(config)
    core1_config["devices"] = [
        {"id": "probe", "device_type": "probe", "config": {}}
    ]

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
        dm_mod.create_device = lambda device_def, i2c_bus_factory=None: (
            ContractViolatingDriver()
        )

        try:
            # The TypeError must win over the clock's LoopStop: if the
            # violation were reclassified as a sensor failure, the loop
            # would run to the stop boundary instead.
            with pytest.raises(TypeError):
                core1.core1_main(bus, core1_config, BOOT_TICKS_MS, "test-runtime")
        finally:
            dm_mod.create_device = saved_create_device
    finally:
        _restore()

    out = capsys.readouterr().out
    assert "[ERROR] Core 1 stopped" in out
