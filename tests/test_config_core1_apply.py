# test_config_core1_apply.py - Core 1 configuration apply / rollback / commit
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Host-side tests for the Core 1 half of a configuration transaction.

Two levels:

1. The ``DeviceManager`` contract that Core 1 drives --
   ``reconfigure_devices`` (retain unchanged instances, build + initialize
   candidates, atomic swap only when all succeed), ``rollback_devices``
   (restore the pre-reconfigure set), and ``apply_dynamic_limits`` (DYNAMIC
   lifecycle scalars).

2. The real ``core1_main`` mailbox handling under a controllable clock:
   a DYNAMIC apply rebases its next deadline from *now* (prospective, never
   a catch-up burst -- invariant 10's skip policy), a ROLLBACK restores the
   previous dynamics, and a failing device reconfigure surfaces an all-or-
   nothing failure result without touching the active set.
"""

import importlib
import json
import os as _real_os
import pathlib
import sys
import time as _real_time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from config import split_config  # noqa: E402
from intercore import InterCore, KIND_HEALTH, KIND_LOG  # noqa: E402


ROOT = pathlib.Path(__file__).resolve().parents[1]

# Loop geometry (mirrors test_health_scheduling): 20ms sleep + 20ms processing.
PROCESSING_MS = 20
LOOP_STEP_MS = 20 + PROCESSING_MS


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
    }


class LoopStop(Exception):
    """Raised by FakeTime to end the infinite Core 1 loop deterministically."""


class FakeTime:
    """Controllable clock for driving the Core 1 loop on the host.

    ``events`` is an ascending list of (tick_ms, callable) pairs; when the
    clock crosses a tick the callable runs once (used to post a config
    transaction request, or to act as Core 0 between requests).
    """

    def __init__(self, start_ms=0, stop_after_ms=float("inf"), events=None):
        self.now_ms = start_ms
        self.stop_after_ms = stop_after_ms
        self.events = list(events or [])

    def ticks_ms(self):
        return self.now_ms

    def ticks_diff(self, now, prev):
        return now - prev

    def ticks_add(self, base, delta):
        return base + delta

    def _fire_events(self):
        while self.events and self.now_ms >= self.events[0][0]:
            _tick, action = self.events.pop(0)
            action()

    def sleep_ms(self, ms):
        self.now_ms += ms + PROCESSING_MS
        self._fire_events()
        if self.now_ms >= self.stop_after_ms:
            raise LoopStop()

    def __getattr__(self, name):
        return getattr(_real_time, name)


class FakeMachine:
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


class FakeDriver:
    """Driver whose initialize() can be scripted to fail or succeed."""

    def __init__(self, fail=False):
        self.fail = fail
        self.initialized = False
        self.read_calls = 0

    def initialize(self, config):
        if self.fail:
            raise RuntimeError("simulated initialization failure")
        self.initialized = True

    def read(self):
        self.read_calls += 1
        return {"value": 1}


def _install_create_device(create_device):
    """Point device_manager.create_device at a test stand-in for the run."""
    dm_mod = sys.modules["device_manager"]
    saved = dm_mod.create_device
    dm_mod.create_device = create_device
    return lambda: setattr(sys.modules["device_manager"], "create_device", saved)


def _core1_config(health_interval_sec, devices=()):
    config = json.loads((ROOT / "config.json").read_text())
    _core0, core1_config, _bus = split_config(config)
    core1_config["devices"] = list(devices)
    core1_config["health_interval_sec"] = health_interval_sec
    return core1_config


def _run_core1(fake_time, bus, core1_config, boot_ticks_ms, restore=None):
    """Drive core1_main under the fakes until FakeTime stops the loop."""
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
        with pytest.raises(LoopStop):
            core1.core1_main(bus, core1_config, boot_ticks_ms, "test-runtime")
    finally:
        if restore is not None:
            restore()
        _restore()


def _drain_outbound(bus):
    health = []
    others = []
    while True:
        entry = bus.outbound_queue.take()
        if entry is None:
            break
        if entry["kind"] == KIND_HEALTH:
            health.append(json.loads(entry["payload_bytes"].decode("utf-8")))
        else:
            others.append(entry)
        bus.outbound_queue.complete_in_flight(entry)
    return health, others


# ---------------------------------------------------------------------------
# DeviceManager reconfigure / rollback / limits contract
# ---------------------------------------------------------------------------

class ReconfigEnv:
    """A real DeviceManager with scripted drivers (attempts=1, no retry sleep)."""

    def __init__(self, initial_device_ids, fail_ids=()):
        self._saved_modules = {
            name: sys.modules.get(name) for name in ("time", "machine", "os")
        }
        self._fake_time = FakeTime()
        _install_fakes(self._fake_time)
        self.core1 = _reload_core1_under_fakes()
        self.dm_mod = sys.modules["device_manager"]

        devices = [
            {"id": d, "device_type": "test", "config": {}} for d in initial_device_ids
        ]
        manager_config = {
            "device_initialization_attempts": 1,
            "device_initialization_retry_delay_ms": 10,
            "device_read_failure_threshold": 3,
            "devices": devices,
        }
        self.dm = self.dm_mod.DeviceManager(manager_config)

        def create_device(device_def, system_information=None):
            return FakeDriver(fail=device_def["id"] in fail_ids)

        self._restore_create_device = _install_create_device(create_device)

    def close(self):
        self._restore_create_device()
        for name, module in self._saved_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


@pytest.fixture
def reconfig():
    e = ReconfigEnv(["a", "b"])
    e.dm.initialize_devices()
    yield e
    e.close()


def test_reconfigure_retains_unchanged_and_builds_new(reconfig):
    """Unchanged instances are retained; new ids are built and the set swapped
    atomically; the dropped device retires."""
    dm = reconfig.dm
    active_before = {d.device_id: d for d in dm.get_active_devices()}
    assert set(active_before) == {"a", "b"}

    # a is unchanged (identical definition); c is new; b is dropped.
    token = dm.reconfigure_devices([
        {"id": "a", "device_type": "test", "config": {}},
        {"id": "c", "device_type": "test", "config": {}},
    ])

    active_after = {d.device_id: d for d in dm.get_active_devices()}
    assert set(active_after) == {"a", "c"}
    # a's ManagedDevice instance survived (runtime state retained)...
    assert active_after["a"] is active_before["a"]
    # ...and the token captures the previous set for a rollback.
    assert {d.device_id for d in token["active_devices"]} == {"a", "b"}


def test_reconfigure_failure_keeps_active_set():
    """Any candidate failing initialization discards all candidates and leaves
    the active set untouched (stricter than boot initialization)."""
    # c is scripted to fail initialization; a is unchanged.
    env = ReconfigEnv(["a", "b"], fail_ids=("c",))
    try:
        dm = env.dm
        dm.initialize_devices()
        active_before = [d.device_id for d in dm.get_active_devices()]
        assert set(active_before) == {"a", "b"}

        # The whole swap must be rejected: a candidate build failure tears
        # down nothing. (Catch the exception from the same (reloaded) module
        # object that raised it, so identity matches.)
        with pytest.raises(env.dm_mod.DeviceReconfigureError):
            dm.reconfigure_devices([
                {"id": "a", "device_type": "test", "config": {}},
                {"id": "c", "device_type": "test", "config": {}},
            ])

        active_after = [d.device_id for d in dm.get_active_devices()]
        assert active_after == active_before
        assert set(dm._devices_by_id) == {"a", "b"}
    finally:
        env.close()


def test_rollback_restores_active_set_and_config(reconfig):
    """rollback_devices restores the pre-reconfigure active set, id index,
    and device config (the reinitialization lookup source)."""
    dm = reconfig.dm
    before = [d.device_id for d in dm.get_active_devices()]
    config_before = dm._devices_config

    token = dm.reconfigure_devices([
        {"id": "a", "device_type": "test", "config": {}},
        {"id": "c", "device_type": "test", "config": {}},
    ])
    assert [d.device_id for d in dm.get_active_devices()] == ["a", "c"]

    dm.rollback_devices(token)
    assert [d.device_id for d in dm.get_active_devices()] == before
    assert dm._devices_config is config_before
    assert set(dm._devices_by_id) == set(before)


def test_apply_dynamic_limits_updates_scalars(reconfig):
    dm = reconfig.dm
    dm.apply_dynamic_limits({
        "device_initialization_attempts": 5,
        "device_initialization_retry_delay_ms": 250,
        "device_read_failure_threshold": 7,
    })
    assert dm._device_initialization_attempts == 5
    assert dm._device_initialization_retry_delay_ms == 250
    assert dm._device_read_failure_threshold == 7


# ---------------------------------------------------------------------------
# core1_main mailbox: DYNAMIC apply rebases the scheduler (no catch-up burst)
# ---------------------------------------------------------------------------

def test_core1_dynamic_apply_rebases_scheduler_no_catchup_burst():
    """A DYNAMIC interval change rebases the next deadline from *now*.

    interval 60s, normal runtime at 13s: first health at 73s. At 80s uptime
    an APPLY changes the interval to 10s; the next health is then at 90s
    (80s + 10s) -- not a catch-up burst for the skipped 10s multiples, and
    not on the old 60s grid (133s).
    """
    boot_ticks_ms = 100000
    startup_at_ms = boot_ticks_ms + 13000
    apply_at_ms = boot_ticks_ms + 80000
    stop_at_ms = boot_ticks_ms + 112000

    bus = InterCore(outbound_max=16, event_max=4)
    bus.state_mailboxes.set_network_snapshot(dict(_ready_network_snapshot()))

    def _apply():
        assert bus.config_transaction_mailbox.put_request({
            "transaction_id": 1,
            "action": "apply",
            "changes": {"health_interval_sec": 10},
        })

    fake_time = FakeTime(startup_at_ms, stop_at_ms,
                         events=[(apply_at_ms, _apply)])
    _run_core1(fake_time, bus, _core1_config(60), boot_ticks_ms)

    # The transaction finalized: a result is pending and nothing is in flight.
    result = bus.config_transaction_mailbox.take_result()
    assert result is not None
    assert result["success"] is True
    assert bus.config_transaction_mailbox.status() == {
        "pending_request": False, "in_flight": False, "pending_result": False}

    health, _others = _drain_outbound(bus)
    uptimes = [p["uptime_ms"] for p in health]
    # 73s (60s cadence, before the apply); then the rebased 10s cadence from
    # the apply point: 90s, 100s, 110s. No burst; no 60s-grid message at 133s.
    assert len(uptimes) == 4
    assert 73000 <= uptimes[0] <= 73000 + LOOP_STEP_MS
    assert 90000 <= uptimes[1] <= 90000 + LOOP_STEP_MS
    assert 100000 <= uptimes[2] <= 100000 + LOOP_STEP_MS
    assert 110000 <= uptimes[3] <= 110000 + LOOP_STEP_MS
    # Explicitly: nothing on the old 60s grid and no replayed 10s multiple.
    assert all(not (133000 - LOOP_STEP_MS <= u <= 133000 + LOOP_STEP_MS)
               for u in uptimes)


# ---------------------------------------------------------------------------
# core1_main mailbox: a failing device reconfigure is all-or-nothing
# ---------------------------------------------------------------------------

def test_core1_devices_reconfigure_failure_yields_failure_result():
    """A candidate device that fails initialization produces a failure result
    and leaves the (empty) active set untouched."""
    boot_ticks_ms = 100000
    startup_at_ms = boot_ticks_ms + 13000
    apply_at_ms = boot_ticks_ms + 20000
    stop_at_ms = boot_ticks_ms + 22000

    bus = InterCore(outbound_max=16, event_max=4)
    bus.state_mailboxes.set_network_snapshot(dict(_ready_network_snapshot()))

    # No initial devices; the candidate below will fail to initialize.
    def create_device(device_def, system_information=None):
        return FakeDriver(fail=True)

    restore = _install_create_device(create_device)

    def _apply():
        assert bus.config_transaction_mailbox.put_request({
            "transaction_id": 1,
            "action": "apply",
            "changes": {"devices": [
                {"id": "d1", "device_type": "test", "config": {}},
            ]},
        })

    fake_time = FakeTime(startup_at_ms, stop_at_ms,
                         events=[(apply_at_ms, _apply)])
    _run_core1(fake_time, bus, _core1_config(60), boot_ticks_ms, restore)

    result = bus.config_transaction_mailbox.take_result()
    assert result is not None
    assert result["success"] is False
    assert result["error"]
    assert bus.config_transaction_mailbox.status()["in_flight"] is False


# ---------------------------------------------------------------------------
# core1_main mailbox: ROLLBACK restores the previous dynamics
# ---------------------------------------------------------------------------

def test_core1_rollback_restores_dynamics():
    """After a DYNAMIC apply, a ROLLBACK instruction restores the previous
    interval: the next health returns to the 60s cadence (from the rollback
    point), not the applied 10s cadence.

    interval 60s, normal runtime at 13s: first health at 73s. At 80s an
    APPLY sets the interval to 10s (next health at 90s). At 95s a ROLLBACK
    restores 60s and rebases from the rollback point -- the next health is at
    155s (95s + 60s), so the 100s boundary that the 10s cadence would have
    produced is skipped, never replayed.
    """
    boot_ticks_ms = 100000
    startup_at_ms = boot_ticks_ms + 13000
    apply_at_ms = boot_ticks_ms + 80000
    rollback_at_ms = boot_ticks_ms + 95000
    stop_at_ms = boot_ticks_ms + 160000

    bus = InterCore(outbound_max=16, event_max=4)
    bus.state_mailboxes.set_network_snapshot(dict(_ready_network_snapshot()))

    apply_result = {}

    def _apply():
        assert bus.config_transaction_mailbox.put_request({
            "transaction_id": 1,
            "action": "apply",
            "changes": {"health_interval_sec": 10},
        })

    def _rollback():
        # Act as Core 0: consume the apply result (ends that transaction) so
        # the mailbox accepts the rollback request.
        apply_result["r"] = bus.config_transaction_mailbox.take_result()
        assert bus.config_transaction_mailbox.put_request({
            "transaction_id": 1,
            "action": "rollback",
        })

    fake_time = FakeTime(startup_at_ms, stop_at_ms,
                         events=[(apply_at_ms, _apply),
                                 (rollback_at_ms, _rollback)])
    _run_core1(fake_time, bus, _core1_config(60), boot_ticks_ms)

    assert apply_result["r"]["success"] is True
    # The rollback result is pending (Core 0 would consume it).
    rollback_result = bus.config_transaction_mailbox.take_result()
    assert rollback_result is not None
    assert rollback_result["success"] is True

    health, _others = _drain_outbound(bus)
    uptimes = [p["uptime_ms"] for p in health]
    # 73s (60s cadence), 90s (applied 10s cadence), 155s (restored 60s cadence
    # from the 95s rollback). The 100s boundary the 10s cadence would produce
    # is skipped after the rollback -- no replay, no burst.
    assert len(uptimes) == 3
    assert 73000 <= uptimes[0] <= 73000 + LOOP_STEP_MS
    assert 90000 <= uptimes[1] <= 90000 + LOOP_STEP_MS
    assert 155000 <= uptimes[2] <= 155000 + LOOP_STEP_MS
