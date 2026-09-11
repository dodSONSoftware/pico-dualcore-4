# test_scheduler_anchor.py - Shared normal-runtime scheduling anchor tests
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Host-side regression tests for the shared normal-runtime scheduling anchor.

All periodic Core 1 work (telemetry and health) must derive its fixed boundaries from one anchor, normal_runtime_start_ticks_ms, captured exactly once, after the startup log completes (the system_startup_completed event log admitted):

- a one-shot initial sample is emitted at the anchor moment (one telemetry read pass and one health report, after startup-log admission, before the run loop)
- telemetry boundaries fall at anchor + n * read_loop_sec
- health boundaries fall at anchor + n * health_interval_sec
- the two schedulers share the epoch but stay independent: neither deadline derives from the other, and neither scheduler is moved by the other's execution or skipping
- the anchor is captured only after successful startup-log admission; an admission failure creates no anchor and no periodic work
- Wi-Fi/MQTT reconnects and UTC resynchronization do not reset the anchor
- a new runtime (reboot) creates a new anchor
- telemetry outage buffering (bounded queue, retention) is unchanged
- boot_ticks_ms remains the boot-lifetime reference only

These tests drive the real core1_main loop under a controllable clock; health-only cadence tests live in test_health_scheduling.py."""

import ast
import gc
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
    KIND_COMMAND_RESPONSE,
    KIND_HEALTH,
    KIND_LOG,
    KIND_TELEMETRY,
    RETENTION_PRIORITY_CRITICAL,
)


ROOT = pathlib.Path(__file__).resolve().parents[1]

# Loop geometry: 20ms sleep + 20ms of processing per iteration, so the loop
# advances 40ms per iteration.
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
        "wifi_connect_count": 1,
        "wifi_disconnect_count": 0,
        "mqtt_connect_count": 1,
        "mqtt_disconnect_count": 0,
    }


def _down_network_snapshot():
    return {
        "ssid": "test-ssid",
        "ip_address": "192.168.1.100",
        "rssi": -50,
        "wifi_connected": True,
        "mqtt_connected": False,
        "network_stack_ready": False,
    }


class LoopStop(Exception):
    """Raised by FakeTime to end the infinite Core 1 loop deterministically."""


class FakeTime:
    """Controllable clock for driving the Core 1 loop on the host.

    sleep_ms adds PROCESSING_MS to model the ~20ms of per-iteration work, so the loop advances 40ms per iteration.

    events is an ascending list of (tick_ms, callable) pairs; when the clock crosses a tick the callable runs once -- used to inject outages, recoveries, or UTC resynchronizations into the state mailboxes mid-run."""

    def __init__(self, start_ms, stop_after_ms, events=None):
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
        # Anything not explicitly faked falls through to the real time
        # module so host tooling keeps working.
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
    # Reload order follows the import dependency chain (a module must be
    # reloaded before the module that binds from it, or the binder keeps a
    # stale reference -- e.g. device_manager would keep an old create_device
    # and instances would be built from a stale driver class).
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


class ProbeDriver:
    """Fake telemetry driver for the probe device; never touches hardware."""

    def initialize(self, config):
        pass

    def read(self):
        return {"probe": 1}


def _core1_config():
    """Core 1 config with a single probe device as the telemetry source.

    Scheduling is independent of the fixture's device set: _run_core1 binds
    a fake create_device for the probe, so no I2C device (the fixture's real
    devices need a hardware bus) is ever constructed here."""
    config = json.loads((ROOT / "tests" / "fixtures" / "config.json").read_text())
    _core0, core1_config = split_config(config)
    core1_config["devices"] = [
        {"id": "probe", "device_type": "probe", "config": {}}
    ]
    return core1_config


def _run_core1(fake_time, bus, core1_config, boot_ticks_ms, pre_run=None):
    """Drive core1_main under the fakes until FakeTime stops the loop.

    pre_run, if given, runs after the fake imports are in place and before core1_main -- used to patch a device read mid-harness."""
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
        dm_mod.create_device = lambda device_def, i2c_bus_factory=None: ProbeDriver()
        if pre_run is not None:
            pre_run()

        try:
            with pytest.raises(LoopStop):
                core1.core1_main(bus, core1_config, boot_ticks_ms, "test-runtime")
        finally:
            dm_mod.create_device = saved_create_device
    finally:
        _restore()


def _slow_periodic_read(fake_time, delay_ms):
    """Return a pre_run hook making the periodic (in-loop) device read consume delay_ms.

    The initial at-anchor sample is read 1 and stays fast; read 2 is the first read that fires from the run loop, which is the one under test."""
    state = {"calls": 0}

    def _pre_run():
        dm_mod = sys.modules["device_manager"]

        class _SlowDriver(ProbeDriver):
            def read(self):
                state["calls"] += 1
                if state["calls"] == 2:
                    fake_time.sleep_ms(delay_ms)
                return {"slow": 1}

        dm_mod.create_device = lambda device_def, i2c_bus_factory=None: _SlowDriver()

    return _pre_run


def _drain_outbound(bus):
    """Drain the outbound queue in admission order.

    Returns (telemetry_payloads, health_payloads, other_entries).
    """
    telemetry = []
    health = []
    others = []
    while True:
        entry = bus.outbound_queue.take()
        if entry is None:
            break
        if entry["kind"] == KIND_TELEMETRY:
            telemetry.append(json.loads(entry["payload_bytes"].decode("utf-8")))
        elif entry["kind"] == KIND_HEALTH:
            health.append(json.loads(entry["payload_bytes"].decode("utf-8")))
        else:
            others.append(entry)
        bus.outbound_queue.complete_in_flight(entry)
    return telemetry, health, others


def test_telemetry_and_health_share_the_same_anchor():
    """Both first deadlines derive from one anchor, captured at admission.

    boot = 100000, normal runtime starts at 13s uptime, read_loop = 20s, health interval = 60s. The initial telemetry sample and initial health report land at the anchor (~13s uptime), and the first periodic deadlines are ~33s and ~73s uptime -- exactly 20s and 60s after the same anchor, whose value is observable in the startup log's uptime_ms (13000)."""
    boot_ticks_ms = 100000
    startup_at_ms = boot_ticks_ms + 13000

    bus = InterCore(minimum_free_heap_bytes=65536)
    bus.state_mailboxes.set_network_snapshot(dict(_ready_network_snapshot()))

    fake_time = FakeTime(startup_at_ms, startup_at_ms + 60000 + LOOP_STEP_MS)
    _run_core1(fake_time, bus, _core1_config(), boot_ticks_ms)

    telemetry, health, others = _drain_outbound(bus)
    # The startup event log is the only log admitted at startup.
    assert len(others) == 1
    startup_log = json.loads(others[0]["payload_bytes"].decode("utf-8"))
    assert others[0]["kind"] == KIND_LOG
    assert startup_log["payload"]["event"] == "system_startup_completed"
    # Under the fake clock no time elapses between admission and the
    # anchor capture, so the startup log's uptime IS the anchor offset.
    anchor_uptime = startup_log["uptime_ms"]
    assert anchor_uptime == 13000

    assert len(telemetry) >= 2
    assert len(health) == 2

    # The initial sample (telemetry and health) lands at the anchor...
    assert 0 <= telemetry[0]["uptime_ms"] - anchor_uptime <= LOOP_STEP_MS
    assert 0 <= health[0]["uptime_ms"] - anchor_uptime <= LOOP_STEP_MS

    # Each first periodic deadline is its interval after the SAME anchor...
    first_telemetry_uptime = telemetry[1]["uptime_ms"]
    first_health_uptime = health[1]["uptime_ms"]
    assert 20000 <= first_telemetry_uptime - anchor_uptime <= 20000 + LOOP_STEP_MS
    assert 60000 <= first_health_uptime - anchor_uptime <= 60000 + LOOP_STEP_MS
    # ...so the gap between them is exactly the interval difference.
    # If the schedulers captured two independent post-admission "nows",
    # this gap could differ by the initialization offset.
    gap = first_health_uptime - first_telemetry_uptime
    assert 40000 - LOOP_STEP_MS <= gap <= 40000 + LOOP_STEP_MS


def test_telemetry_first_deadline_from_anchor_not_boot():
    """Telemetry is immediate at the anchor, then due read_loop after normal-runtime start.

    boot = 100000, normal runtime starts at 13s uptime, read_loop = 20s: the initial telemetry sample must be at ~13000ms uptime (the anchor), and the first periodic read at ~33000ms -- not at 20000ms (the old boot anchor)."""
    boot_ticks_ms = 100000
    startup_at_ms = boot_ticks_ms + 13000

    bus = InterCore(minimum_free_heap_bytes=65536)
    bus.state_mailboxes.set_network_snapshot(dict(_ready_network_snapshot()))

    fake_time = FakeTime(startup_at_ms, startup_at_ms + 20000 + LOOP_STEP_MS)
    _run_core1(fake_time, bus, _core1_config(), boot_ticks_ms)

    telemetry, health, _others = _drain_outbound(bus)
    assert len(telemetry) == 2
    # The initial health sample also lands at the anchor (13s uptime)...
    assert len(health) == 1
    assert 13000 <= health[0]["uptime_ms"] <= 13000 + LOOP_STEP_MS
    # ...and the initial telemetry sample is at the anchor too...
    assert 13000 <= telemetry[0]["uptime_ms"] <= 13000 + LOOP_STEP_MS
    # First periodic read: within one loop step of anchor + 20s (33s uptime)...
    uptime_ms = telemetry[1]["uptime_ms"]
    assert 33000 <= uptime_ms <= 33000 + LOOP_STEP_MS
    # ...and far from the boot-anchored (20000ms) wrong answer.
    assert uptime_ms > 20000 + LOOP_STEP_MS


def test_fixed_cadence_telemetry_and_health_from_one_anchor():
    """Telemetry at 33/53/73/93/113/133s and health at 73/133s uptime.

    boot = 100000, normal runtime starts at 13s uptime, read_loop = 20s, health interval = 60s. Both schedules stay on their anchor-based grids; at 73s uptime both are due together and both are processed (no artificial offset between them)."""
    boot_ticks_ms = 100000
    startup_at_ms = boot_ticks_ms + 13000
    last_boundary_ms = startup_at_ms + 120000  # health boundary at 133s uptime

    bus = InterCore(minimum_free_heap_bytes=65536)
    bus.state_mailboxes.set_network_snapshot(dict(_ready_network_snapshot()))

    fake_time = FakeTime(startup_at_ms, last_boundary_ms + LOOP_STEP_MS)
    _run_core1(fake_time, bus, _core1_config(), boot_ticks_ms)

    telemetry, health, _others = _drain_outbound(bus)
    telemetry_uptimes = [m["uptime_ms"] for m in telemetry]
    health_uptimes = [p["uptime_ms"] for p in health]

    # Immediate at-anchor sample, then the periodic grid.
    assert 13000 <= telemetry_uptimes[0] <= 13000 + LOOP_STEP_MS
    for expected, actual in zip((33000, 53000, 73000, 93000, 113000, 133000), telemetry_uptimes[1:]):
        assert expected <= actual <= expected + LOOP_STEP_MS
    assert len(telemetry_uptimes) == 7

    assert 13000 <= health_uptimes[0] <= 13000 + LOOP_STEP_MS
    for expected, actual in zip((73000, 133000), health_uptimes[1:]):
        assert expected <= actual <= expected + LOOP_STEP_MS
    assert len(health_uptimes) == 3

    # Coincidence is expected: both are due at the 73s boundary.
    assert any(73000 <= u <= 73000 + LOOP_STEP_MS for u in telemetry_uptimes)
    assert any(73000 <= u <= 73000 + LOOP_STEP_MS for u in health_uptimes)


def test_health_outage_does_not_move_telemetry_deadlines():
    """A skipped health interval leaves the telemetry schedule untouched.

    MQTT outage 160s -> 240s uptime skips the 193s health boundary. Telemetry must still land on every 33+20k boundary through the outage (telemetry may queue during outages), and health resumes at its own 253s boundary -- nothing moved, nothing replayed."""
    boot_ticks_ms = 100000
    startup_at_ms = boot_ticks_ms + 13000

    bus = InterCore(minimum_free_heap_bytes=65536)
    bus.state_mailboxes.set_network_snapshot(dict(_ready_network_snapshot()))

    events = [
        (boot_ticks_ms + 160000, lambda: bus.state_mailboxes.set_network_snapshot(dict(_down_network_snapshot()))),
        (boot_ticks_ms + 240000, lambda: bus.state_mailboxes.set_network_snapshot(dict(_ready_network_snapshot()))),
    ]
    fake_time = FakeTime(startup_at_ms, startup_at_ms + 240000 + LOOP_STEP_MS, events=events)
    _run_core1(fake_time, bus, _core1_config(), boot_ticks_ms)

    telemetry, health, _others = _drain_outbound(bus)
    telemetry_uptimes = [m["uptime_ms"] for m in telemetry]
    health_uptimes = [p["uptime_ms"] for p in health]

    # The immediate at-anchor sample, plus every telemetry boundary,
    # through the outage.
    assert 13000 <= telemetry_uptimes[0] <= 13000 + LOOP_STEP_MS
    for expected in (33000, 53000, 73000, 93000, 113000, 133000, 153000, 173000, 193000, 213000, 233000, 253000):
        assert any(expected <= u <= expected + LOOP_STEP_MS for u in telemetry_uptimes[1:])
    assert len(telemetry_uptimes) == 13

    # Health: the immediate at-anchor report, then its own boundaries;
    # 193s skipped, no catch-up at 240s.
    assert 13000 <= health_uptimes[0] <= 13000 + LOOP_STEP_MS
    assert len(health_uptimes) == 4
    for expected in (73000, 133000, 253000):
        assert any(expected <= u <= expected + LOOP_STEP_MS for u in health_uptimes[1:])
    assert not any(193000 <= u <= 240000 for u in health_uptimes)


def test_telemetry_delay_does_not_move_health_deadlines():
    """A late telemetry execution leaves the health schedule untouched.

    The initial at-anchor sample is fast; the first periodic telemetry read (due at 33s uptime) takes 35s, stalling the loop until ~68s uptime. The scheduler skips the elapsed 33s/53s boundaries and advances directly to the next future one (73s) -- it does NOT replay the missed boundaries as catch-up reads. Health must still land on its anchor-based grid -- the immediate report plus 73s and 133s in this window -- one per boundary, no drift, no burst."""
    boot_ticks_ms = 100000
    startup_at_ms = boot_ticks_ms + 13000

    bus = InterCore(minimum_free_heap_bytes=65536)
    bus.state_mailboxes.set_network_snapshot(dict(_ready_network_snapshot()))

    fake_time = FakeTime(startup_at_ms, startup_at_ms + 120000 + LOOP_STEP_MS)
    _run_core1(fake_time, bus, _core1_config(), boot_ticks_ms,
               pre_run=_slow_periodic_read(fake_time, 35000))

    telemetry, health, _others = _drain_outbound(bus)
    telemetry_uptimes = [m["uptime_ms"] for m in telemetry]
    health_uptimes = [p["uptime_ms"] for p in health]

    # The immediate sample is at the anchor...
    assert telemetry_uptimes and 13000 <= telemetry_uptimes[0] <= 13000 + LOOP_STEP_MS
    # ...and the delayed periodic read fired well past its 33s boundary...
    assert telemetry_uptimes[1] > 33000 + LOOP_STEP_MS
    # ...and the slow read did NOT trigger a catch-up burst: no two samples
    # land within a burst window (a replayed boundary would fire within one or
    # two loop steps of the slow read), the scheduler skipped straight ahead
    # to the next future boundary (~73s, 5s after the slow read finished).
    for a, b in zip(telemetry_uptimes, telemetry_uptimes[1:]):
        assert b - a > 1000, (
            "catch-up burst after the slow read: samples at {} and {} "
            "are nearly simultaneous".format(a, b)
        )
    # ...but the health schedule is exactly on its anchor-based grid: the
    # immediate report at the anchor, then the 73s and 133s boundaries...
    assert len(health_uptimes) == 3
    assert 13000 <= health_uptimes[0] <= 13000 + LOOP_STEP_MS
    assert 73000 <= health_uptimes[1] <= 73000 + LOOP_STEP_MS
    assert 133000 <= health_uptimes[2] <= 133000 + LOOP_STEP_MS
    # ...with no accumulated drift from the telemetry delay.
    assert health_uptimes[2] - health_uptimes[1] == 60000


def test_reconnects_and_utc_resync_do_not_reset_anchor():
    """Wi-Fi reconnect, MQTT reconnect, and UTC resync keep the anchor.

    Normal runtime starts at 13s uptime, health interval = 60s: with the anchor fixed at 13s, the health boundaries are 73s, 133s, and 193s uptime. Re-anchoring on any recovery would shift them (e.g. a 50s recovery would produce 110s, 170s, 230s)."""
    boot_ticks_ms = 100000
    startup_at_ms = boot_ticks_ms + 13000

    bus = InterCore(minimum_free_heap_bytes=65536)
    bus.state_mailboxes.set_network_snapshot(dict(_ready_network_snapshot()))

    def _utc_resync():
        bus.state_mailboxes.set_utc_snapshot({
            "utc_epoch_ms": 1735000000000,
            "sync_uptime_ms": 0,
            "runtime_start_epoch_ms": 1735000000000,
        })

    events = [
        (boot_ticks_ms + 40000, lambda: bus.state_mailboxes.set_network_snapshot(dict(_down_network_snapshot()))),
        (boot_ticks_ms + 50000, lambda: bus.state_mailboxes.set_network_snapshot(dict(_ready_network_snapshot()))),
        (boot_ticks_ms + 100000, lambda: bus.state_mailboxes.set_network_snapshot(dict(_down_network_snapshot()))),
        (boot_ticks_ms + 110000, lambda: bus.state_mailboxes.set_network_snapshot(dict(_ready_network_snapshot()))),
        (boot_ticks_ms + 140000, _utc_resync),
    ]
    fake_time = FakeTime(startup_at_ms, startup_at_ms + 180000 + LOOP_STEP_MS, events=events)
    _run_core1(fake_time, bus, _core1_config(), boot_ticks_ms)

    telemetry, health, _others = _drain_outbound(bus)
    telemetry_uptimes = [m["uptime_ms"] for m in telemetry]
    health_uptimes = [p["uptime_ms"] for p in health]

    # Health stayed on the ORIGINAL anchor's grid: the immediate report at
    # the 13s anchor, then 73/133/193s uptime.
    assert 13000 <= health_uptimes[0] <= 13000 + LOOP_STEP_MS
    assert len(health_uptimes) == 4
    for expected in (73000, 133000, 193000):
        assert any(expected <= u <= expected + LOOP_STEP_MS for u in health_uptimes[1:])
    # And no boundary anywhere a re-anchored scheduler would put one.
    for shifted in (110000, 170000, 230000):
        assert not any(shifted <= u <= shifted + LOOP_STEP_MS for u in health_uptimes)

    # Telemetry kept running across the outages on its own anchor grid.
    for expected in (33000, 133000, 193000):
        assert any(expected <= u <= expected + LOOP_STEP_MS for u in telemetry_uptimes)


def test_startup_log_admission_failure_blocks_normal_runtime():
    """If the startup log is never admitted, no anchor exists and no periodic scheduling begins.

    The free heap is below the reserve with nothing to collect (memory pressure), and the queue holds only CRITICAL entries (command responses), so the less-important INFO-priority startup log cannot be admitted -- no eviction is permitted -- and is rejected on both attempts; core1_main must halt and the queue must contain no telemetry or health."""
    boot_ticks_ms = 100000
    startup_at_ms = boot_ticks_ms + 13000

    bus = InterCore(minimum_free_heap_bytes=65536)
    bus.state_mailboxes.set_network_snapshot(dict(_ready_network_snapshot()))
    for _ in range(16):
        admitted = bus.outbound_queue.put_with_kind(
            KIND_COMMAND_RESPONSE, b"{}", RETENTION_PRIORITY_CRITICAL
        )
        assert admitted

    # Memory pressure for the admission attempts that follow: the free heap
    # is below the reserve and unrecoverable (nothing to collect).
    saved_mem_free = gc.mem_free
    gc.mem_free = lambda: 0

    fake_time = FakeTime(startup_at_ms, startup_at_ms + 10 * LOOP_STEP_MS)

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
        with pytest.raises(RuntimeError):
            core1.core1_main(bus, _core1_config(), boot_ticks_ms, "test-runtime")
    finally:
        _restore()
        gc.mem_free = saved_mem_free

    telemetry, health, others = _drain_outbound(bus)
    assert telemetry == []
    assert health == []
    # Only the pre-filled placeholders: no startup log, no periodic work.
    assert len(others) == 16
    assert all(entry["payload_bytes"] == b"{}" for entry in others)


def test_telemetry_buffers_during_outage_health_skipped():
    """Outage semantics are preserved under the shared anchor.

    MQTT outage 160s -> 240s uptime: telemetry keeps entering the bounded queue (existing retention/eviction rules), the 193s health boundary is skipped, and recovery at 240s triggers no immediate health -- the next health is at its own 253s boundary."""
    boot_ticks_ms = 100000
    startup_at_ms = boot_ticks_ms + 13000

    bus = InterCore(minimum_free_heap_bytes=65536)
    bus.state_mailboxes.set_network_snapshot(dict(_ready_network_snapshot()))

    events = [
        (boot_ticks_ms + 160000, lambda: bus.state_mailboxes.set_network_snapshot(dict(_down_network_snapshot()))),
        (boot_ticks_ms + 240000, lambda: bus.state_mailboxes.set_network_snapshot(dict(_ready_network_snapshot()))),
    ]
    fake_time = FakeTime(startup_at_ms, startup_at_ms + 240000 + LOOP_STEP_MS, events=events)
    _run_core1(fake_time, bus, _core1_config(), boot_ticks_ms)

    telemetry, health, _others = _drain_outbound(bus)
    telemetry_uptimes = [m["uptime_ms"] for m in telemetry]
    health_uptimes = [p["uptime_ms"] for p in health]

    # Telemetry buffered through the outage: the 173/193/213/233s reads
    # (while MQTT was down) are still in the bounded queue.
    for expected in (173000, 193000, 213000, 233000):
        assert any(expected <= u <= expected + LOOP_STEP_MS for u in telemetry_uptimes)
    # The immediate at-anchor sample plus the full 13s-anchored cadence up to 253s.
    assert 13000 <= telemetry_uptimes[0] <= 13000 + LOOP_STEP_MS
    for expected in (33000, 53000, 73000, 93000, 113000, 133000, 153000, 253000):
        assert any(expected <= u <= expected + LOOP_STEP_MS for u in telemetry_uptimes[1:])
    assert len(telemetry_uptimes) == 13

    # Health: the immediate report at the anchor, 73s and 133s before the
    # outage, 253s after it -- the 193s boundary skipped, no catch-up at the
    # 240s recovery.
    assert 13000 <= health_uptimes[0] <= 13000 + LOOP_STEP_MS
    assert len(health_uptimes) == 4
    for expected in (73000, 133000, 253000):
        assert any(expected <= u <= expected + LOOP_STEP_MS for u in health_uptimes[1:])
    assert not any(193000 <= u <= 240000 for u in health_uptimes)


def test_core1_uses_one_shared_normal_runtime_anchor():
    """Structural check: exactly one anchor, captured once from
    time.ticks_ms() before the run loop, and both deadlines initialize
    from it and advance from their own previous deadline.

    The deadlines live in the schedulers holder (schedulers["next_*_ms"]);
    this core1_main body only writes the anchor-derived init and the
    in-loop advances.
    """
    tree = ast.parse((ROOT / "core1.py").read_text())
    func = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "core1_main")

    def _name_assignments(name):
        return [n for n in ast.walk(func)
                if isinstance(n, ast.Assign)
                and all(isinstance(t, ast.Name) and t.id == name for t in n.targets)]

    def _holder_assignments(key):
        return [n for n in ast.walk(func)
                if isinstance(n, ast.Assign)
                and len(n.targets) == 1
                and isinstance(n.targets[0], ast.Subscript)
                and isinstance(n.targets[0].value, ast.Name)
                and n.targets[0].value.id == "schedulers"
                and isinstance(n.targets[0].slice, ast.Constant)
                and n.targets[0].slice.value == key]

    def _holder_refs(node, key):
        return [s for s in ast.walk(node)
                if isinstance(s, ast.Subscript)
                and isinstance(s.value, ast.Name)
                and s.value.id == "schedulers"
                and isinstance(s.slice, ast.Constant)
                and s.slice.value == key]

    def _ticks_calls(node):
        return [c for c in ast.walk(node)
                if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                and c.func.attr == "ticks_ms"]

    # The anchor is captured exactly once...
    anchor_assigns = _name_assignments("normal_runtime_start_ticks_ms")
    assert len(anchor_assigns) == 1
    # ...from time.ticks_ms()...
    assert len(_ticks_calls(anchor_assigns[0].value)) == 1
    # ...before the ``while True`` run loop (never re-captured inside it).
    main_loop = next(n for n in ast.walk(func)
                     if isinstance(n, ast.While)
                     and isinstance(n.test, ast.Constant)
                     and n.test.value is True)
    assert main_loop
    assert anchor_assigns[0].lineno < main_loop.lineno

    # Both schedulers initialize from the shared anchor (the re-grid helper
    # is called with the anchor)...
    for deadline in ("next_read_ms", "next_health_ms"):
        inits = [a for a in _holder_assignments(deadline)
                 if isinstance(a.value, ast.Call)
                 and isinstance(a.value.func, ast.Name)
                 and a.value.func.id == "_regrid_next_boundary"
                 and any(isinstance(x, ast.Name) and x.id == "normal_runtime_start_ticks_ms"
                         for x in a.value.args)]
        assert inits, "{} must initialize from normal_runtime_start_ticks_ms".format(deadline)

    # ...and every deadline assignment (init and in-loop advance) derives
    # from a named value -- never from a fresh time.ticks_ms() sample.
    for deadline in ("next_read_ms", "next_health_ms"):
        for assign in _holder_assignments(deadline):
            assert not _ticks_calls(assign.value), \
                "{} must never derive from a fresh ticks_ms() sample".format(deadline)

    # Both deadlines advance from their own previous deadline (fixed
    # cadence), and neither scheduler reads the other's deadline.
    for deadline in ("next_read_ms", "next_health_ms"):
        other = "next_health_ms" if deadline == "next_read_ms" else "next_read_ms"
        advances = [a for a in _holder_assignments(deadline)
                    if isinstance(a.value, ast.Call)
                    and isinstance(a.value.func, ast.Attribute)
                    and a.value.func.attr == "ticks_add"
                    and _holder_refs(a.value, deadline)]
        assert advances, "{} must advance from its previous deadline".format(deadline)
        for assign in _holder_assignments(deadline):
            assert not _holder_refs(assign.value, other), \
                "{} must not derive from {}".format(deadline, other)
