# test_uptime.py - Regression tests for accumulated boot-relative uptime
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Host-side regression tests for the accumulated-uptime fix.

MicroPython's time.ticks_diff is only guaranteed correct when the two samples are less than half a tick period apart. On the 30-bit millisecond counter of the 32-bit RP2 builds that is 2**29 ms, about 6.21 days (the full 2**30 period is about 12.4 days), so a device running longer than that half-period violates the guarantee: a single ticks_diff(now, boot_ticks_ms) returns a negative or wrapped value even though the device is alive and well. The fix accumulates deltas between consecutive samples (each a recent pair) into a running total, so every individual ticks_diff stays within its guaranteed window and the total keeps increasing across the tick-counter wrap.

These tests drive the PRODUCTION uptime module (not a copy) under a clock that faithfully models MicroPython's 30-bit ticks -- including the signed two's-complement result of ticks_diff, the OverflowError ticks_add raises at half the period, and the counter wrapping past 2**30 -- plus production-path checks: Core 1's _message_time with a recent UTC snapshot across a counter wrap, the stale-snapshot case where the snapshot is older than half a tick period (Core 1's timestamp and Core 0's _utc_sync_due refresh), and the per-device read-age fields on the same shared boot base."""

import importlib
import os as _real_os
import pathlib
import sys
import time as _real_time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

TICKS_PERIOD = 1 << 30  # 32-bit MicroPython builds (RP2): 30-bit tick values
HALF_PERIOD = 1 << 29


class WrappingFakeTime:
    """Model MicroPython's 30-bit monotonic ticks (32-bit RP2 builds).

    ticks_diff returns the signed two's-complement difference (correct only within half a period), ticks_add rejects deltas at or beyond half a period, and the counter wraps past 2**30."""

    def __init__(self, start_ms):
        self.now_ms = start_ms % TICKS_PERIOD

    def advance(self, ms):
        self.now_ms = (self.now_ms + ms) % TICKS_PERIOD

    def ticks_ms(self):
        return self.now_ms

    def ticks_diff(self, now, prev):
        diff = (now - prev) % TICKS_PERIOD
        if diff >= HALF_PERIOD:
            diff -= TICKS_PERIOD
        return diff

    def ticks_add(self, base, delta):
        # MicroPython raises when the delta reaches half the period, so
        # ticks_diff can still round-trip it (delta = +-TICKS_PERIOD/2 is
        # both endpoints of the legal range).
        if abs(delta) >= HALF_PERIOD:
            raise OverflowError("ticks interval overflow")
        return (base + delta) % TICKS_PERIOD

    def sleep_ms(self, ms):
        self.advance(ms)

    def __getattr__(self, name):
        # Anything not explicitly faked falls through to the real time
        # module so host tooling keeps working.
        return getattr(_real_time, name)


def _save_time():
    return sys.modules.get("time")


def _restore_time(saved):
    if saved is None:
        sys.modules.pop("time", None)
    else:
        sys.modules["time"] = saved


def _uptime_under_fake(fake):
    """Return the production uptime module with its time bound to fake.

    uptime binds time at import time, so it is reloaded (or imported fresh) after the fake is installed to make the fake authoritative."""
    sys.modules["time"] = fake
    if "uptime" in sys.modules:
        return importlib.reload(sys.modules["uptime"])
    import uptime

    return uptime


def test_uptime_advances_with_normal_progression():
    """Uptime tracks elapsed time for ordinary, non-wrapping progression."""
    fake = WrappingFakeTime(1000)
    saved = _save_time()
    try:
        uptime = _uptime_under_fake(fake)
        state = uptime.create_uptime_state(1000)
        assert uptime.current_uptime_ms(state) == 0
        fake.advance(500)
        assert uptime.current_uptime_ms(state) == 500
        fake.advance(1000)
        assert uptime.current_uptime_ms(state) == 1500
    finally:
        _restore_time(saved)


def test_uptime_stays_increasing_across_wrap_and_half_period():
    """Uptime keeps increasing once elapsed time exceeds half a tick period.

    The counter wraps past 2**30 (now ends up numerically below boot) and the total elapsed (3 * 2**28) exceeds half a period, the regime where a single ticks_diff(now, boot) is wrong. Each step is taken and sampled before the next (as the run loops do), so every individual ticks_diff compares recent samples."""
    step = 1 << 28  # ~3.1 days; < half a period, so a single diff is valid
    boot = 3 * step  # at 3/4 of the counter, so the steps cross the 2**30 wrap
    fake = WrappingFakeTime(boot)
    saved = _save_time()
    try:
        uptime = _uptime_under_fake(fake)
        state = uptime.create_uptime_state(boot)
        values = [uptime.current_uptime_ms(state)]
        for _ in range(3):
            fake.advance(step)
            values.append(uptime.current_uptime_ms(state))

        # Monotonically increasing across the wrap and the half-period line.
        assert values == [0, step, 2 * step, 3 * step]
        assert fake.now_ms < boot  # the counter genuinely wrapped

        # The old one-shot ticks_diff(now, boot) is wrong in this regime.
        assert fake.ticks_diff(fake.now_ms, boot) != values[-1]
    finally:
        _restore_time(saved)


def test_uptime_never_decreases_under_repeated_sampling():
    """Repeated sampling never yields a lower total than a prior sample."""
    fake = WrappingFakeTime(HALF_PERIOD - 100)
    saved = _save_time()
    try:
        uptime = _uptime_under_fake(fake)
        state = uptime.create_uptime_state(fake.now_ms)
        previous = uptime.current_uptime_ms(state)
        for _ in range(20):
            fake.advance(100)  # crosses the half-period and the 2**30 wrap
            current = uptime.current_uptime_ms(state)
            assert current >= previous
            previous = current
    finally:
        _restore_time(saved)


# --- Core 1 integration: uptime and UTC timestamp survive a wrap ---------


def test_ticks_add_rejects_half_period_deltas():
    """The fake's ticks_add matches MicroPython: +-half the period overflows.

    A delta of exactly HALF_PERIOD is ambiguous for ticks_diff, so MicroPython raises OverflowError at both endpoints; the legal range is +-HALF_PERIOD - 1. Production deltas are config-bounded below this, so only a bug could reach it."""
    fake = WrappingFakeTime(0)
    assert fake.ticks_add(0, HALF_PERIOD - 1) == HALF_PERIOD - 1
    assert fake.ticks_add(0, -(HALF_PERIOD - 1)) == TICKS_PERIOD - HALF_PERIOD + 1
    assert fake.ticks_diff(fake.ticks_add(0, HALF_PERIOD - 1), 0) == HALF_PERIOD - 1
    with pytest.raises(OverflowError):
        fake.ticks_add(0, HALF_PERIOD)
    with pytest.raises(OverflowError):
        fake.ticks_add(0, -HALF_PERIOD)
    with pytest.raises(OverflowError):
        fake.ticks_add(0, TICKS_PERIOD)


class _Uname:
    sysname = "MicroPython"
    nodename = "pico"
    release = "v1.23.0"
    version = "v1.23.0"
    machine = "Raspberry Pi Pico W with RP2040"


class FakeMachine:
    """Minimal MicroPython ``machine`` stand-in (CPU frequency source)."""

    @staticmethod
    def freq():
        return 125000000


class FakeOs:
    """``os`` stand-in reporting a Pico W machine string from uname()."""

    def uname(self):
        return _Uname()

    def __getattr__(self, name):
        return getattr(_real_os, name)


def _reload_core1_under_fakes(fake_time):
    """Import/reload the core1 chain with the fakes authoritative.

    core1 (and its local dependencies, including uptime) bind time/machine/os from sys.modules at import time, so the whole chain is reloaded before core1 itself."""
    sys.modules["time"] = fake_time
    sys.modules["machine"] = FakeMachine()
    sys.modules["os"] = FakeOs()
    names = (
        "hardware",
        "uptime",
        "system_information",
        "device_manager",
        "device_factory",
        "devices",
    )
    for name in names:
        if name in sys.modules:
            importlib.reload(sys.modules[name])
    core1 = sys.modules.get("core1")
    if core1 is None:
        core1 = importlib.import_module("core1")
    else:
        core1 = importlib.reload(core1)
    return core1


def test_core1_message_time_uptime_and_timestamp_survive_wrap():
    """Core 1's uptime and UTC timestamp both stay correct across a wrap.

    The UTC snapshot is taken at boot (a recent sample, as in production where it is refreshed periodically). The tick counter then wraps past 2**30. Uptime must keep increasing and the timestamp must track utc_epoch_ms + elapsed_since_snapshot -- both of which rely on recent-sample diffs, not a diff against the boot tick.

    This covers the recent-snapshot case only; a UTC snapshot older than half a tick period (where the old one-shot ticks_diff against the sync tick was wrong) is covered by test_utc_aging_survives_elapsed_beyond_half_period."""
    from intercore import InterCore  # noqa: E402

    boot = TICKS_PERIOD - 1000  # near the top of the 30-bit counter
    fake = WrappingFakeTime(boot)
    saved_time = _save_time()
    saved_machine = sys.modules.get("machine")
    saved_os = sys.modules.get("os")

    def _restore():
        _restore_time(saved_time)
        for name, mod in (("machine", saved_machine), ("os", saved_os)):
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod

    try:
        from message_protocol import format_utc_epoch_ms  # noqa: E402

        core1 = _reload_core1_under_fakes(fake)
        uptime = sys.modules["uptime"]

        bus = InterCore(minimum_free_heap_bytes=65536)
        epoch_ms = 1_700_000_000_000
        bus.state_mailboxes.set_utc_snapshot({
            "utc_epoch_ms": epoch_ms,
            "sync_uptime_ms": 0,
            "runtime_start_epoch_ms": epoch_ms,
            "timestamp": "2023-11-14T22:13:20Z",
        })

        state = uptime.create_uptime_state(boot)
        samples = [core1._message_time(bus, state)]
        for _ in range(3):
            fake.advance(500)  # crosses the 2**30 wrap partway through
            samples.append(core1._message_time(bus, state))

        # Uptime increased across the wrap (500ms steps from boot).
        assert [s[0] for s in samples] == [0, 500, 1000, 1500]
        # The timestamp tracks utc_epoch_ms + elapsed_since_snapshot. Both are
        # recent-sample computations, so they stay correct across the wrap --
        # compare against the production formatter for the expected value.
        expected_ts = [format_utc_epoch_ms(epoch_ms + i * 500) for i in range(4)]
        assert [s[1] for s in samples] == expected_ts
    finally:
        _restore()


def test_utc_aging_survives_elapsed_beyond_half_period():
    """Regression (P1): a UTC snapshot older than half a tick period must still
    yield a correct timestamp AND must not block the refresh that repairs the clock.

    The old code computed elapsed as ``ticks_diff(now, snapshot_ticks)``. MicroPython
    only guarantees that within half a tick period, so after a long network/server
    outage the one-shot diff wraps: the timestamp goes wrong (can go negative) and
    ``_utc_sync_due()`` computes the wrong delta, which can suppress the very
    resync that would fix the clock. The fix measures elapsed from the accumulated
    uptime (each delta between recent samples), which is correct for any duration.

    The clock advances well past half a period, but in steps small enough that every
    individual ``ticks_diff`` stays within its guaranteed window -- the regime the run
    loops actually run, since they sample continuously."""
    from unittest.mock import MagicMock  # noqa: E402
    from intercore import InterCore  # noqa: E402

    boot = 0
    fake = WrappingFakeTime(boot)
    epoch_ms = 1_700_000_000_000

    saved = {
        name: sys.modules.get(name)
        for name in ("time", "machine", "os", "debug", "wifi", "mqtt")
    }

    def _restore():
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod

    try:
        # Reuse the Core-1-under-fakes loader: installs the wrapping fake time and
        # machine/os and reloads the core1 chain (including uptime) so they are
        # authoritative, then adds the stand-ins core0 additionally needs.
        core1 = _reload_core1_under_fakes(fake)
        uptime = sys.modules["uptime"]

        debug_mock = MagicMock()
        debug_mock.DEBUG = False
        sys.modules["debug"] = debug_mock
        sys.modules["wifi"] = MagicMock()
        sys.modules["mqtt"] = MagicMock()
        core0 = (
            importlib.import_module("core0")
            if "core0" not in sys.modules
            else importlib.reload(sys.modules["core0"])
        )
        from message_protocol import format_utc_epoch_ms  # noqa: E402

        bus = InterCore(minimum_free_heap_bytes=65536)

        # Snapshot taken at boot: sync_uptime 0, runtime-start epoch == sync epoch.
        snapshot = {
            "utc_epoch_ms": epoch_ms,
            "sync_uptime_ms": 0,
            "runtime_start_epoch_ms": epoch_ms,
            "timestamp": "2023-11-14T22:13:20Z",
        }
        bus.state_mailboxes.set_utc_snapshot(snapshot)
        core0_instance = core0.Core0(
            bus,
            {"datetime_sync_interval_min": 60, "wifi_reconnect_delays_sec": [1]},
            {"wifi_ssid": "x", "wifi_password": "y"},
            "rt", boot, MagicMock(), MagicMock(),
        )
        core0_instance._utc_snapshot = snapshot

        # A quarter period per step: a valid single diff. Four steps cross BOTH
        # the half-period line and the 2**30 wrap -- the regime where a one-shot
        # ticks_diff(now, boot) is wrong but the accumulated total is not.
        step = HALF_PERIOD // 2
        core1_state = uptime.create_uptime_state(boot)
        timestamps = []
        for _ in range(4):
            fake.advance(step)
            # Sample both cores at the same instant, as their run loops do.
            _uptime_ms, ts = core1._message_time(bus, core1_state)
            core0_instance._uptime_ms()
            timestamps.append(ts)

        # Core 1's timestamp tracked the accumulated elapsed (positive, correct)
        # across the half-period line and the counter wrap.
        assert timestamps == [
            format_utc_epoch_ms(epoch_ms + (i + 1) * step) for i in range(4)
        ]
        # Core 0 flags the refresh as due (far past the sync interval) -- the old
        # one-shot diff would have computed a small/wrapped elapsed and suppressed
        # this, blocking the resync that repairs the clock.
        assert core0_instance._utc_sync_due() is True
        # The one-shot diff the old code relied on is wrong in this regime.
        assert fake.ticks_diff(fake.now_ms, boot) != (4 * step)
    finally:
        _restore()


def test_device_read_age_survives_elapsed_beyond_half_period():
    """Regression (P3): a device's read-age fields stay correct when the
    device remains failed/reinitializing for longer than half a tick period.

    ``last_read_ms`` / ``last_successful_read_ms`` are stored as boot-relative
    accumulated uptime -- the same single source of truth Core 1 uses for its
    UTC timestamp and uptime fields -- and the age is a plain subtraction on
    that base. So a device stuck in failure/reinit past half a tick period
    (where ``ticks_diff(now, last_read_ticks)`` wraps and corrupts the value)
    still reports a positive, correct read age. The regression is
    observability-only: it does not affect recovery decisions."""
    boot = 0
    fake = WrappingFakeTime(boot)
    saved = {
        name: sys.modules.get(name)
        for name in ("time", "machine", "os")
    }

    def _restore():
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod

    try:
        _reload_core1_under_fakes(fake)  # loads device_manager under the fakes
        uptime = sys.modules["uptime"]
        device_manager_mod = sys.modules["device_manager"]

        config = {
            "device_initialization_attempts": 1,
            "device_initialization_retry_delay_ms": 0,
            "device_read_failure_threshold": 3,
            "devices": [
                {
                    "id": "dev",
                    "device_type": "probe",
                    "config": {},
                }
            ],
        }

        state = uptime.create_uptime_state(boot)
        manager = device_manager_mod.DeviceManager(config, uptime_state=state)

        class StubDriver:
            def initialize(self, device_config):
                pass

            def read(self):
                return {"probe": 1}

        managed = device_manager_mod.ManagedDevice(
            device_id="dev",
            device_type="probe",
            driver=StubDriver(),
        )
        manager._active_devices.append(managed)

        # One successful read records its timestamps on the accumulated-uptime
        # base (== 0 at this instant, since boot is the anchor).
        result = manager.process_device(managed)
        assert result["status"] == device_manager_mod.DEVICE_RESULT_TELEMETRY
        assert managed.last_read_ms == 0
        assert managed.last_successful_read_ms == 0

        # The device then stays failed/reinitializing for well past half a
        # tick period. Advance in steps small enough that each individual
        # ticks_diff stays within its guaranteed window -- the regime the
        # uptime accumulator (and Core 1's run loop) actually runs.
        step = HALF_PERIOD // 2
        for _ in range(4):
            fake.advance(step)
            uptime.current_uptime_ms(state)
        elapsed = 4 * step  # 2 * HALF_PERIOD: beyond the half-period line

        snapshot = manager.get_status_snapshot(now_ms=fake.ticks_ms())
        dev_status = snapshot["device_status"][0]
        # The accumulated-uptime age is the full, positive elapsed time.
        assert dev_status["last_read_age_ms"] == elapsed
        assert dev_status["last_successful_read_age_ms"] == elapsed

        # The one-shot ticks_diff the old code relied on is wrong in this
        # regime (the read tick and the now tick are far more than half a
        # period apart), so a raw-tick implementation could not reproduce the
        # age above.
        assert fake.ticks_diff(fake.now_ms, boot) != elapsed
    finally:
        _restore()
