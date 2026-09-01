# test_health_scheduling.py - Normal-runtime-anchored health scheduling regression tests
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Host-side regression tests for normal-runtime-anchored health scheduling.

The health cadence is defined relative to the normal-runtime anchor, normal_runtime_start_ticks_ms -- captured exactly once, immediately after system_startup_completed has been successfully admitted to the outbound queue:

- boundaries fall at health_interval_sec multiples from the anchor;
- with a 13s startup and a 60s interval, the first health is at ~73s uptime -- not 60s (the old boot anchor) and not a second, independently captured post-admission delay;
- no immediate health message is generated after admission;
- boundaries missed while the loop was stalled are skipped, never replayed;
- a boundary due while the network is down (or MQTT disconnected) is skipped, and recovery does not trigger a catch-up health;
- the next deadline always advances from the previous anchor-based deadline, so per-iteration delay cannot accumulate into drift;
- a new runtime (reboot) gets a new anchor; mid-run events do not.

These tests drive the real core1_main loop under a controllable clock; the shared-anchor and telemetry-side tests live in test_scheduler_anchor.py."""

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


class LoopStop(Exception):
    """Raised by FakeTime to end the infinite Core 1 loop deterministically."""


class FakeTime:
    """Controllable clock for driving the Core 1 loop on the host.

    sleep_ms adds PROCESSING_MS to model the ~20ms of per-iteration work, so the loop advances 40ms per iteration.

    events is an ascending list of (tick_ms, callable) pairs; when the clock crosses a tick the callable runs once -- used to inject an MQTT outage or recovery into the state mailboxes mid-run.

    advance models a stalled loop: the clock jumps forward without a normal loop iteration."""

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

    def advance(self, ms):
        self.now_ms += ms

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


def _core1_config(health_interval_sec):
    config = json.loads((ROOT / "config.json").read_text())
    _core0, core1_config= split_config(config)
    # Scheduling is independent of the device set; keep startup fast.
    core1_config["devices"] = []
    core1_config["health_interval_sec"] = health_interval_sec
    return core1_config


def _run_core1(fake_time, bus, core1_config, boot_ticks_ms):
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
        _restore()


def _drain_outbound(bus):
    """Drain the outbound queue in admission order.

    Returns (health_payloads, other_entries).
    """
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


def test_no_immediate_health_after_startup_log_admission():
    """Startup log admission at 13s uptime must not queue a health message.

    With a 60s interval the first health boundary (anchor + 60s = 73s uptime) is still in the future -- the queue must contain only the startup log."""
    boot_ticks_ms = 100000
    startup_at_ms = boot_ticks_ms + 13000  # startup completes at 13s uptime

    bus = InterCore(minimum_free_heap_bytes=65536)
    bus.state_mailboxes.set_network_snapshot(dict(_ready_network_snapshot()))

    fake_time = FakeTime(startup_at_ms, startup_at_ms + LOOP_STEP_MS)
    _run_core1(fake_time, bus, _core1_config(60), boot_ticks_ms)

    health, others = _drain_outbound(bus)
    assert health == []
    assert len(others) == 1
    startup_log = json.loads(others[0]["payload_bytes"].decode("utf-8"))
    assert others[0]["kind"] == KIND_LOG
    assert startup_log["payload"]["event"] == "system_startup_completed"
    assert startup_log["uptime_ms"] == 13000


def test_first_health_at_anchor_plus_interval_not_boot_or_immediate():
    """First health is due 60s after normal-runtime start, not after boot.

    boot = 100000, interval = 60s, startup completes at 13s uptime: the first health must be at ~73000ms uptime -- not at 13000 (immediate), not at 60000 (the old boot anchor), and not at 13000 plus a second independently captured delay."""
    boot_ticks_ms = 100000
    startup_at_ms = boot_ticks_ms + 13000
    first_boundary_ms = startup_at_ms + 60000

    bus = InterCore(minimum_free_heap_bytes=65536)
    bus.state_mailboxes.set_network_snapshot(dict(_ready_network_snapshot()))

    fake_time = FakeTime(startup_at_ms, first_boundary_ms + LOOP_STEP_MS)
    _run_core1(fake_time, bus, _core1_config(60), boot_ticks_ms)

    health, others = _drain_outbound(bus)
    assert len(health) == 1
    assert len(others) == 1  # startup log only
    uptime_ms = health[0]["uptime_ms"]
    # Within one loop step of the normal-runtime-anchored boundary...
    assert 73000 <= uptime_ms <= 73000 + LOOP_STEP_MS
    # ...and far from the immediate (13000ms) and boot-anchored (60000ms)
    # wrong answers.
    assert uptime_ms > 60000 + LOOP_STEP_MS
    assert uptime_ms > 13000 + LOOP_STEP_MS


def test_fixed_cadence_from_normal_runtime_anchor():
    """Health deadlines fire at exactly anchor + 60/120/180/240 seconds."""
    boot_ticks_ms = 100000
    # Normal runtime starts at 13s uptime, off the 40ms loop grid by 0ms;
    # every boundary shares the same grid offset, so uptimes land within
    # one loop step of anchor + 60s*k.
    startup_at_ms = boot_ticks_ms + 13000
    last_boundary_ms = startup_at_ms + 240000

    bus = InterCore(minimum_free_heap_bytes=65536)
    bus.state_mailboxes.set_network_snapshot(dict(_ready_network_snapshot()))

    fake_time = FakeTime(startup_at_ms, last_boundary_ms + LOOP_STEP_MS)
    _run_core1(fake_time, bus, _core1_config(60), boot_ticks_ms)

    health, _others = _drain_outbound(bus)
    uptimes = [p["uptime_ms"] for p in health]
    # Four boundaries: 73s, 133s, 193s, 253s uptime (13s anchor + 60s*k).
    assert len(uptimes) == 4
    for expected, actual in zip((73000, 133000, 193000, 253000), uptimes):
        assert expected <= actual <= expected + LOOP_STEP_MS
        # And never on the old boot-anchored grid (60s, 120s, 180s, 240s).
        assert actual > expected - LOOP_STEP_MS


def test_no_cumulative_drift_across_intervals():
    """A late firing does not push the next deadline later.

    Each health firing lands 10ms after its boundary (the first grid tick past the deadline); the second firing must land 10ms after anchor + 120s, not anchor + 120s plus another 10ms of drift. Inter-arrival must be exactly the configured interval."""
    boot_ticks_ms = 100000
    startup_at_ms = boot_ticks_ms + 13010  # 13.01s uptime, off-grid
    second_boundary_ms = startup_at_ms + 120000

    bus = InterCore(minimum_free_heap_bytes=65536)
    bus.state_mailboxes.set_network_snapshot(dict(_ready_network_snapshot()))

    fake_time = FakeTime(startup_at_ms, second_boundary_ms + LOOP_STEP_MS)
    _run_core1(fake_time, bus, _core1_config(60), boot_ticks_ms)

    health, _others = _drain_outbound(bus)
    uptimes = [p["uptime_ms"] for p in health]
    assert len(uptimes) == 2

    # Both firings stay within one loop step of their anchor-based
    # boundaries...
    assert 73010 <= uptimes[0] <= 73010 + LOOP_STEP_MS
    assert 133010 <= uptimes[1] <= 133010 + LOOP_STEP_MS
    # ...and the interval between them is exactly the configured cadence,
    # i.e. the same boundary offset for both -- no accumulated drift.
    assert uptimes[1] - uptimes[0] == 60000


def test_missed_boundaries_after_stall_skipped_not_replayed():
    """Boundaries missed while the loop is stalled are skipped, never replayed.

    The loop stalls at 185s uptime and resumes at 305s (boundaries 193s and 253s missed). Exactly one health must be emitted after the stall, at the resume moment (current state) -- not a burst of catch-up reports."""
    boot_ticks_ms = 100000
    startup_at_ms = boot_ticks_ms + 13000
    stall_at_ms = boot_ticks_ms + 185000
    resume_at_ms = stall_at_ms + 120000  # 305s uptime

    bus = InterCore(minimum_free_heap_bytes=65536)
    bus.state_mailboxes.set_network_snapshot(dict(_ready_network_snapshot()))

    events = [(stall_at_ms, lambda: fake_time.advance(120000))]
    fake_time = FakeTime(startup_at_ms, resume_at_ms + LOOP_STEP_MS, events=events)
    _run_core1(fake_time, bus, _core1_config(60), boot_ticks_ms)

    health, _others = _drain_outbound(bus)
    uptimes = [p["uptime_ms"] for p in health]
    # 73s and 133s before the stall; exactly one report after it...
    assert 73000 <= uptimes[0] <= 73000 + LOOP_STEP_MS
    assert 133000 <= uptimes[1] <= 133000 + LOOP_STEP_MS
    # ...at the resume moment (current state, within one loop step)...
    assert len(uptimes) == 3
    assert 305000 <= uptimes[2] <= 305000 + LOOP_STEP_MS
    # ...and none replayed at the missed 193s / 253s boundaries.
    assert all(not (193000 <= u <= 253000 + LOOP_STEP_MS) for u in uptimes)


def test_mqtt_outage_skips_boundaries_and_does_not_replay_on_recovery():
    """Outage behavior: due boundaries while MQTT is down are skipped; after recovery, wait for the next anchor-relative boundary (no immediate recovery health, no replay of skipped reports).

    Timeline (interval 60s, normal runtime starts at 13s uptime):
      health at 73s and 133s uptime
      MQTT fails at 160s uptime
      193s boundary -> skipped
      MQTT restored at 240s uptime (no immediate health)
      next health at 253s uptime"""
    boot_ticks_ms = 100000
    startup_at_ms = boot_ticks_ms + 13000

    bus = InterCore(minimum_free_heap_bytes=65536)
    bus.state_mailboxes.set_network_snapshot(dict(_ready_network_snapshot()))

    def _mqtt_down():
        bus.state_mailboxes.set_network_snapshot({
            "ssid": "test-ssid",
            "ip_address": "192.168.1.100",
            "rssi": -50,
            "wifi_connected": True,
            "mqtt_connected": False,
            "network_stack_ready": False,
        })

    def _mqtt_up():
        bus.state_mailboxes.set_network_snapshot(dict(_ready_network_snapshot()))

    events = [
        (boot_ticks_ms + 160000, _mqtt_down),
        (boot_ticks_ms + 240000, _mqtt_up),
    ]
    next_boundary_after_recovery_ms = startup_at_ms + 240000
    fake_time = FakeTime(startup_at_ms, next_boundary_after_recovery_ms + LOOP_STEP_MS, events=events)
    _run_core1(fake_time, bus, _core1_config(60), boot_ticks_ms)

    health, _others = _drain_outbound(bus)
    uptimes = [p["uptime_ms"] for p in health]
    # 193s was skipped, and recovery at 240s did not trigger an immediate
    # catch-up health: next emission is at 253s.
    assert len(uptimes) == 3
    assert 73000 <= uptimes[0] <= 73000 + LOOP_STEP_MS
    assert 133000 <= uptimes[1] <= 133000 + LOOP_STEP_MS
    assert 253000 <= uptimes[2] <= 253000 + LOOP_STEP_MS


def test_new_runtime_creates_new_anchor():
    """A reboot -- a new runtime -- establishes a new normal-runtime anchor.

    First runtime: boot = 100000, startup at 13s uptime, interval 60s -> first health at ~73s uptime. Second runtime (fresh process state, boot = 200000, startup at 10s uptime) -> first health at ~70s uptime of the new runtime, not a continuation of the first runtime's boundaries."""
    def _run_once(boot_ticks_ms, startup_uptime_ms):
        startup_at_ms = boot_ticks_ms + startup_uptime_ms
        first_boundary_ms = startup_at_ms + 60000

        bus = InterCore(minimum_free_heap_bytes=65536)
        bus.state_mailboxes.set_network_snapshot(dict(_ready_network_snapshot()))

        fake_time = FakeTime(startup_at_ms, first_boundary_ms + LOOP_STEP_MS)
        _run_core1(fake_time, bus, _core1_config(60), boot_ticks_ms)

        health, _others = _drain_outbound(bus)
        assert len(health) == 1
        return health[0]["uptime_ms"]

    first_runtime_health_uptime = _run_once(100000, 13000)
    # anchor = 13s + 60s interval = 73s uptime of the first runtime.
    assert 73000 <= first_runtime_health_uptime <= 73000 + LOOP_STEP_MS

    second_runtime_health_uptime = _run_once(200000, 10000)
    # New anchor for the new runtime: 10s + 60s = 70s uptime -- independent
    # of the first runtime's 73s boundary.
    assert 70000 <= second_runtime_health_uptime <= 70000 + LOOP_STEP_MS
