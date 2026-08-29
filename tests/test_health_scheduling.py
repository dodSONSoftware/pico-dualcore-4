# test_health_scheduling.py - Boot-anchored health scheduling regression tests
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Host-side regression tests for boot-anchored health scheduling.

The health cadence is defined relative to firmware boot, not to startup
completion:

- boundaries fall at ``health_interval_sec`` multiples from ``boot_ticks_ms``
  (a 60s interval means 60, 120, 180, 240 seconds of uptime);
- no immediate health message is generated after ``system_startup_completed``;
- boundaries missed while startup was running are skipped, never replayed;
- at a due boundary with the network down (or MQTT disconnected) the
  boundary is skipped, and recovery does not trigger a catch-up health;
- the next deadline always advances from the previous boot-based deadline,
  so per-iteration processing delay cannot accumulate into drift;
- no health is generated before the startup log is admitted.

These tests drive the real ``core1_main`` loop under a controllable clock,
the same way ``test_core1_liveness.py`` does.
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

    ``sleep_ms`` adds PROCESSING_MS to model the ~20ms of per-iteration
    work the loop does between sleeps, so the loop advances 40ms per
    iteration.

    ``events`` is an ascending list of (tick_ms, callable) pairs. When the
    clock crosses a tick, the callable runs once -- used to inject an MQTT
    outage or recovery into the state mailboxes mid-run.
    """

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

    core1 binds time/machine/os from sys.modules at import time, so any
    module already cached (possibly imported under host or other-test
    stand-ins) is reloaded in dependency order before core1 itself.
    """
    names = (
        "hardware",
        "system_information",
        "device_manager",
        "device_factory",
        "devices",
        "devices.system_information",
        "devices.system_information.system_information_device",
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
    _core0, core1_config, _bus = split_config(config)
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
    """Startup log admission at 14s uptime must not queue a health message.

    With a 60s interval and startup completing at 14s, the first health
    boundary (60s) is still in the future -- the queue must contain only
    the startup log.
    """
    boot_ticks_ms = 100000
    startup_at_ms = boot_ticks_ms + 14000  # startup completes at 14s uptime

    bus = InterCore(outbound_max=16, event_max=4)
    bus.state_mailboxes.set_network_snapshot(dict(_ready_network_snapshot()))

    fake_time = FakeTime(startup_at_ms, startup_at_ms + LOOP_STEP_MS)
    _run_core1(fake_time, bus, _core1_config(60), boot_ticks_ms)

    health, others = _drain_outbound(bus)
    assert health == []
    assert len(others) == 1
    startup_log = json.loads(others[0]["payload_bytes"].decode("utf-8"))
    assert others[0]["kind"] == KIND_LOG
    assert startup_log["payload"]["event"] == "system_startup_completed"
    assert startup_log["uptime_ms"] == 14000


def test_first_health_anchored_to_boot_not_startup_completion():
    """First health is due 60s after boot, not 60s after startup completion.

    boot = 100000, interval = 60s, startup completes at 14s uptime.
    The first health must be at ~60000ms uptime -- not at 14000 (immediate)
    and not at 74000 (startup_completion + 60s).
    """
    boot_ticks_ms = 100000
    startup_at_ms = boot_ticks_ms + 14000
    first_boundary_ms = boot_ticks_ms + 60000  # 160000

    bus = InterCore(outbound_max=16, event_max=4)
    bus.state_mailboxes.set_network_snapshot(dict(_ready_network_snapshot()))

    fake_time = FakeTime(startup_at_ms, first_boundary_ms + LOOP_STEP_MS)
    _run_core1(fake_time, bus, _core1_config(60), boot_ticks_ms)

    health, others = _drain_outbound(bus)
    assert len(health) == 1
    assert len(others) == 1  # startup log only
    uptime_ms = health[0]["uptime_ms"]
    # Within one loop step of the boot-anchored boundary, and far from both
    # the immediate (14000ms) and startup-relative (74000ms) wrong answers.
    assert first_boundary_ms - boot_ticks_ms <= uptime_ms <= first_boundary_ms - boot_ticks_ms + LOOP_STEP_MS


def test_exact_boot_relative_cadence():
    """Health deadlines fire at exactly 60/120/180/240 seconds from boot."""
    boot_ticks_ms = 100000
    # Startup completes immediately at boot, aligned to the 40ms loop grid.
    startup_at_ms = boot_ticks_ms
    last_boundary_ms = boot_ticks_ms + 240000

    bus = InterCore(outbound_max=16, event_max=4)
    bus.state_mailboxes.set_network_snapshot(dict(_ready_network_snapshot()))

    fake_time = FakeTime(startup_at_ms, last_boundary_ms + LOOP_STEP_MS)
    _run_core1(fake_time, bus, _core1_config(60), boot_ticks_ms)

    health, _others = _drain_outbound(bus)
    uptimes = [p["uptime_ms"] for p in health]
    # Every boundary is on the loop grid, so uptimes land exactly on them.
    assert uptimes == [60000, 120000, 180000, 240000]


def test_no_cumulative_drift_across_intervals():
    """A late firing does not push the next deadline later.

    Startup completes at 14.01s uptime, off the 40ms loop grid. Each health
    firing lands 10ms after its boundary (the first grid tick past the
    deadline); the second firing must land 10ms after *120000*, not
    60000+10 plus another 10ms of drift. Inter-arrival must be exactly
    the configured interval.
    """
    boot_ticks_ms = 100000
    startup_at_ms = boot_ticks_ms + 14010  # 14.01s uptime, off-grid
    second_boundary_ms = boot_ticks_ms + 120000

    bus = InterCore(outbound_max=16, event_max=4)
    bus.state_mailboxes.set_network_snapshot(dict(_ready_network_snapshot()))

    fake_time = FakeTime(startup_at_ms, second_boundary_ms + LOOP_STEP_MS)
    _run_core1(fake_time, bus, _core1_config(60), boot_ticks_ms)

    health, _others = _drain_outbound(bus)
    uptimes = [p["uptime_ms"] for p in health]
    assert len(uptimes) == 2

    # Both firings stay within one loop step of their boot-anchored
    # boundaries...
    assert 60000 <= uptimes[0] <= 60000 + LOOP_STEP_MS
    assert 120000 <= uptimes[1] <= 120000 + LOOP_STEP_MS
    # ...and the interval between them is exactly the configured cadence,
    # i.e. the same boundary offset for both -- no accumulated drift.
    assert uptimes[1] - uptimes[0] == 60000


def test_startup_longer_than_first_boundary_skips_missed_interval():
    """A 10s interval with startup at 14s: the 10s report is skipped, not
    emitted after the fact; the first health lands at 20s uptime.

    The startup log is admitted before any health and carries the smaller
    uptime, preserving the startup-log-before-health ordering.
    """
    boot_ticks_ms = 100000
    startup_at_ms = boot_ticks_ms + 14000
    second_boundary_ms = boot_ticks_ms + 20000

    bus = InterCore(outbound_max=16, event_max=4)
    bus.state_mailboxes.set_network_snapshot(dict(_ready_network_snapshot()))

    fake_time = FakeTime(startup_at_ms, second_boundary_ms + LOOP_STEP_MS)
    _run_core1(fake_time, bus, _core1_config(10), boot_ticks_ms)

    health, others = _drain_outbound(bus)
    assert len(health) == 1
    assert health[0]["uptime_ms"] == 20000

    # Startup gate preserved: startup log admitted, before the first health.
    assert len(others) == 1
    startup_log = json.loads(others[0]["payload_bytes"].decode("utf-8"))
    assert startup_log["payload"]["event"] == "system_startup_completed"
    assert startup_log["uptime_ms"] < health[0]["uptime_ms"]


def test_multiple_missed_intervals_no_catchup_burst():
    """Boundaries missed before the scheduler resumes are skipped, never
    replayed.

    interval = 60s; the scheduler resumes at 185s uptime (boundaries 60,
    120, 180 all missed). Exactly one health must be emitted, at the next
    future boundary (240s uptime) -- not a burst of three catch-up reports.
    """
    boot_ticks_ms = 100000
    resume_at_ms = boot_ticks_ms + 185000
    next_boundary_ms = boot_ticks_ms + 240000

    bus = InterCore(outbound_max=16, event_max=4)
    bus.state_mailboxes.set_network_snapshot(dict(_ready_network_snapshot()))

    fake_time = FakeTime(resume_at_ms, next_boundary_ms + LOOP_STEP_MS)
    _run_core1(fake_time, bus, _core1_config(60), boot_ticks_ms)

    health, _others = _drain_outbound(bus)
    assert len(health) == 1
    assert health[0]["uptime_ms"] == 240000


def test_mqtt_outage_skips_boundaries_and_does_not_replay_on_recovery():
    """Outage behavior: due boundaries while MQTT is down are skipped; after
    recovery, wait for the next boot-relative boundary (no immediate
    recovery health, no replay of skipped reports).

    Timeline (interval 60s, boot 100000):
      health at 60s and 120s uptime
      MQTT fails at 150s uptime
      180s and 240s boundaries -> skipped
      MQTT restored at 250s uptime
      next health at 300s uptime
    """
    boot_ticks_ms = 100000
    startup_at_ms = boot_ticks_ms
    next_boundary_after_recovery_ms = boot_ticks_ms + 300000

    bus = InterCore(outbound_max=16, event_max=4)
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
        (boot_ticks_ms + 150000, _mqtt_down),
        (boot_ticks_ms + 250000, _mqtt_up),
    ]
    fake_time = FakeTime(startup_at_ms, next_boundary_after_recovery_ms + LOOP_STEP_MS, events=events)
    _run_core1(fake_time, bus, _core1_config(60), boot_ticks_ms)

    health, _others = _drain_outbound(bus)
    uptimes = [p["uptime_ms"] for p in health]
    # 180s and 240s reports were skipped, and recovery at 250s did not
    # trigger an immediate catch-up health: next emission is at 300s.
    assert uptimes == [60000, 120000, 300000]
