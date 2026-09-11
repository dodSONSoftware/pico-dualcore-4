# test_core1_liveness.py - Tests for Core 1 liveness heartbeat registration
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Host-side regression tests for the Core 1 liveness heartbeat.

Core 1 must refresh the activity stamp on a deadline so health messages report core_1_active: true no matter which phase the ~20ms main loop lands in on absolute tick time. An earlier implementation gated the refresh on now_ms % 5000 < 20: with a loop step that is not an exact divisor of that grid the stamps land in a coset that never enters [0, 20), starve, and healthy firmware reports core_1_inactive after the 60s threshold.

This test drives the real core1_main loop under exactly that phase geometry and asserts the health messages keep reporting Core 1 as active."""

import importlib
import json
import os as _real_os
import pathlib
import sys
import time as _real_time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from config import split_config  # noqa: E402
from intercore import InterCore, KIND_HEALTH  # noqa: E402


ROOT = pathlib.Path(__file__).resolve().parents[1]

# Loop geometry: 20ms sleep + 20ms of processing per iteration.
PROCESSING_MS = 20
LOOP_STEP_MS = 20 + PROCESSING_MS
# Boot offset 22ms: under the old ``now_ms % 5000 < 20`` gate the stamps
# would land at {22 + 40k} mod 5000, a coset that never enters [0, 20).
BOOT_TICKS_MS = 22
ACTIVITY_INTERVAL_MS = 5000
HEALTH_INTERVAL_MS = 60 * 1000
# Run just past the 120022ms health deadline (22 + 2 * 60000), by which
# point the old code's boot-time stamp is 120000ms stale (> 60000ms threshold).
STOP_AT_MS = BOOT_TICKS_MS + 2 * HEALTH_INTERVAL_MS + LOOP_STEP_MS

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
    """Controllable clock for driving the Core 1 loop on the host.

    sleep_ms adds PROCESSING_MS to model the ~20ms of per-iteration work, so the loop advances 40ms per iteration -- a step that is not a divisor of the 5000ms window and therefore exposed the phase-dependent now_ms % 5000 < 20 gate."""

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


def _core1_config():
    config = json.loads((ROOT / "tests" / "fixtures" / "config.json").read_text())
    _core0, core1_config= split_config(config)
    # Liveness is independent of the device set; keep startup fast.
    core1_config["devices"] = []
    return core1_config


def _drain_health_payloads(bus):
    """Drain the outbound queue and return the decoded health payloads in order."""
    payloads = []
    while True:
        entry = bus.outbound_queue.take()
        if entry is None:
            break
        if entry["kind"] == KIND_HEALTH:
            payloads.append(json.loads(entry["payload_bytes"].decode("utf-8")))
        bus.outbound_queue.complete_in_flight(entry)
    return payloads


def test_core1_activity_stamp_survives_hostile_loop_phase():
    """The liveness stamp must refresh on deadline regardless of loop phase.

    Drives the real core1_main loop for ~120s of simulated time with a 40ms loop step and a 22ms boot offset -- the geometry in which the old phase-gated registration never fired after boot."""
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
        core1 = _reload_core1_under_fakes()

        bus = InterCore(minimum_free_heap_bytes=65536)
        bus.state_mailboxes.set_network_snapshot(dict(_NETWORK_SNAPSHOT))

        with pytest.raises(LoopStop):
            core1.core1_main(bus, _core1_config(), BOOT_TICKS_MS, "test-runtime")
    finally:
        _restore()

    # Health kept flowing across the whole window: the immediate at-anchor
    # report at 22ms uptime plus the periodic anchor-based messages at
    # 60022ms and 120022ms.
    health_payloads = _drain_health_payloads(bus)
    assert len(health_payloads) == 3
    assert health_payloads[0]["uptime_ms"] <= LOOP_STEP_MS

    # The last health message is built ~120s after boot; the old phase-gated
    # code would see a 120000ms-stale stamp and flag core_1_inactive.
    last_payload = health_payloads[-1]["payload"]
    assert last_payload["core_1_active"] is True
    assert "core_1_inactive" not in last_payload["degraded_reasons"]
    assert last_payload["core_1_activity_age_ms"] <= ACTIVITY_INTERVAL_MS + LOOP_STEP_MS

    # The mailbox stamp itself is fresh within one interval plus one loop step.
    stamp = bus.state_mailboxes.get_core_1_activity_ms()
    assert stamp is not None
    assert fake_time.ticks_diff(fake_time.now_ms, stamp) <= ACTIVITY_INTERVAL_MS + LOOP_STEP_MS


# ---------------------------------------------------------------------------
# Test 2: a healthy-but-slow device read must not stale the heartbeat stamp
# ---------------------------------------------------------------------------
#
# The loop captures now_ms BEFORE the device read, then potentially spends a
# long time reading. The periodic heartbeat compared and stamped with that
# pre-read clock: a heartbeat boundary falling due DURING the read was missed
# on that pass (stamped later, with the old pre-read time) or written as a
# stale stamp -- and with a read longer than Core 0's 30 s watchdog bound,
# Core 0 saw a wedged Core 1 that was healthy the whole time and reset the
# board. The read-boundary skip below it in the loop already re-captures the
# clock for exactly this reason (skip_now_ms); the heartbeat must too.

SLOW_READ_MS = 6000  # longer than the 5000 ms heartbeat interval


class SlowReadDriver:
    """A healthy-but-slow device: read() succeeds, but its work takes
    SLOW_READ_MS of wall time (modeled by advancing the fake clock), enough
    to carry the next heartbeat boundary due in flight."""

    def __init__(self, fake_time, recorder):
        self._fake_time = fake_time
        self._recorder = recorder
        self.reads = 0

    def initialize(self, config):
        pass

    def read(self):
        self.reads += 1
        if self.reads != 2:
            # The initial at-anchor sample is read 1 (fast); the periodic
            # read boundary is read 2 and is the one under test here.
            return {"slow": self.reads}
        start = self._fake_time.ticks_ms()
        self._fake_time.now_ms += SLOW_READ_MS
        self._recorder["window"] = (start, self._fake_time.ticks_ms())
        return {"slow": 1}


def _core1_config_with_slow_read_device():
    config = json.loads((ROOT / "tests" / "fixtures" / "config.json").read_text())
    _core0, core1_config = split_config(config)
    core1_config["devices"] = [
        {"id": "probe", "device_type": "probe", "config": {}}
    ]
    return core1_config


def test_core1_slow_read_does_not_stale_the_heartbeat_stamp():
    """The heartbeat stamped after a slow device read must use a FRESH clock.

    Geometry: the initial at-anchor sample (read 1, fast) runs before the loop; the periodic telemetry boundary at anchor + read_loop_sec (22 + 20000) fires read 2, whose 6000 ms window carries the 25022 activity boundary due in flight. Pre-fix, the loop compared against the pre-read clock (no stamp that pass, the last stamp staying 5000 ms stale at the read's end). Post-fix the stamp written on the read's pass is the post-read clock."""
    # 20022 (read) + 6000 (read) = 26022; stop on the loop sleep after it.
    slow_stop_at_ms = BOOT_TICKS_MS + 20 * 1000 + SLOW_READ_MS
    fake_time = FakeTime(BOOT_TICKS_MS, slow_stop_at_ms)
    saved_modules = {name: sys.modules.get(name) for name in ("time", "machine", "os")}

    def _restore():
        for name, module in saved_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    recorder = {}
    try:
        _install_fakes(fake_time)
        core1 = _reload_core1_under_fakes()

        from intercore import InterCore
        import device_manager as dm

        bus = InterCore(minimum_free_heap_bytes=65536)
        bus.state_mailboxes.set_network_snapshot(dict(_NETWORK_SNAPSHOT))

        driver = SlowReadDriver(fake_time, recorder)
        saved_create = dm.create_device
        dm.create_device = lambda device_def, i2c_bus_factory=None: driver
        try:
            with pytest.raises(LoopStop):
                core1.core1_main(
                    bus, _core1_config_with_slow_read_device(), BOOT_TICKS_MS, "test-runtime"
                )
        finally:
            dm.create_device = saved_create
    finally:
        _restore()

    assert driver.reads == 2  # initial sample + the periodic slow read
    window_start, window_end = recorder["window"]
    # The scenario precondition: the read window carries a heartbeat boundary
    # (it is longer than the 5000 ms interval and starts on the 40 ms grid).
    assert window_end - window_start >= ACTIVITY_INTERVAL_MS

    # The heartbeat boundary that fell due during the read must have been
    # stamped AFTER the read finished -- with the fresh post-read clock --
    # not missed on that pass or written with the stale pre-read now_ms.
    stamp = bus.state_mailboxes.get_core_1_activity_ms()
    assert stamp is not None
    assert stamp >= window_end
