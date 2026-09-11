# test_reinit_suppression.py - Core 1 reinitialization-failure log suppression
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Host-side tests for reinitialization-failure warning suppression.

Drives the real device_manager decision and the real core1 warning path together: the first failed reinit logs a warning, repeats for the same device are suppressed, a successful reinit clears the suppression, and a later independent failure logs the warning again."""

import importlib
import json
import os as _real_os
import pathlib
import sys
import time as _real_time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from config import split_config  # noqa: E402
from intercore import InterCore  # noqa: E402


ROOT = pathlib.Path(__file__).resolve().parents[1]
BOOT_TICKS_MS = 100000

# Substring that identifies the reinitialization-failure warning.
_WARNING = "reinitialization failed"


class FakeTime:
    """Controllable clock (MicroPython ticks + sleep_ms) for the host."""

    def __init__(self, now_ms=BOOT_TICKS_MS):
        self.now_ms = now_ms

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

    core1 (and its imports) bind time/machine/os from sys.modules at import time, so any cached module is reloaded in dependency order first."""
    names = (
        "hardware",
        "system_information",
        "device_manager",
        "device_factory",
        "devices",
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
    """Driver whose initialize() can be switched between failing and succeeding."""

    def __init__(self):
        self.initialize_should_fail = True
        self.initialize_calls = 0

    def initialize(self, config):
        self.initialize_calls += 1
        if self.initialize_should_fail:
            raise RuntimeError("simulated initialization failure")

    def read(self):
        return {"value": 1}


class ReinitEnv:
    """Builds the real DeviceManager + ManagedDevice and a core1 to route results.

    Uses device_initialization_attempts=1 so a failed reinit does not sleep between retries, keeping the host test deterministic."""

    def __init__(self):
        self._fake_time = FakeTime()
        self._saved_modules = {
            name: sys.modules.get(name) for name in ("time", "machine", "os")
        }
        _install_fakes(self._fake_time)
        self.core1 = _reload_core1_under_fakes()
        self.dm_mod = sys.modules["device_manager"]

        manager_config = {
            "device_initialization_attempts": 1,
            "device_initialization_retry_delay_ms": 10,
            "device_read_failure_threshold": 3,
            "devices": [{"id": "dev1", "device_type": "test", "config": {}}],
        }
        self.dm = self.dm_mod.DeviceManager(manager_config)

        self.driver = FakeDriver()
        self.md = self.dm_mod.ManagedDevice(
            device_id="dev1",
            device_type="test",
            driver=self.driver,
        )

        raw = json.loads((ROOT / "tests" / "fixtures" / "config.json").read_text())
        _core0, core1_config= split_config(raw)
        self.config = core1_config
        self.bus = InterCore(minimum_free_heap_bytes=65536)
        self.uptime_state = self.core1.create_uptime_state(BOOT_TICKS_MS)

    def process_and_handle(self):
        """Run one device cycle through the real device_manager + core1 paths."""
        result = self.dm.process_device(self.md)
        self.core1._handle_device_result(
            self.bus, self.uptime_state, result
        )
        return result

    def close(self):
        for name, module in self._saved_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


@pytest.fixture
def env():
    e = ReinitEnv()
    yield e
    e.close()


def test_first_failure_logs(env, capsys):
    """The first failed reinit emits the existing warning and marks it logged."""
    env.driver.initialize_should_fail = True
    env.md.mark_reinitialize_pending()

    result = env.process_and_handle()
    out = capsys.readouterr().out

    assert result["status"] == "reinitialization_failed"
    assert result["log_failure_warning"] is True
    assert _WARNING in out
    # Now marked logged, so repeats are suppressed.
    assert env.md.should_suppress_reinit_failure() is True


def test_subsequent_failure_is_suppressed(env, capsys):
    """Repeated failed reinits for the same device do not re-warn."""
    env.driver.initialize_should_fail = True
    env.md.mark_reinitialize_pending()

    # First failure logs and sets the suppression flag; the device stays
    # reinit-pending, so the next cycle retries reinitialization.
    env.process_and_handle()
    capsys.readouterr()  # discard the first warning

    result = env.process_and_handle()
    out = capsys.readouterr().out

    assert result["status"] == "reinitialization_failed"
    assert result["log_failure_warning"] is False
    assert _WARNING not in out


def test_recovery_clears_suppression(env, capsys):
    """A successful reinit clears the suppression flag."""
    env.driver.initialize_should_fail = True
    env.md.mark_reinitialize_pending()
    env.process_and_handle()  # first failure -> flag set
    capsys.readouterr()

    # Recovery: initialize now succeeds.
    env.driver.initialize_should_fail = False
    result = env.process_and_handle()

    assert result["status"] == "reinitialized"
    assert env.md.should_suppress_reinit_failure() is False


def test_new_failure_after_recovery_logs_again(env, capsys):
    """An independent failure after a recovery is allowed to warn again."""
    env.driver.initialize_should_fail = True
    env.md.mark_reinitialize_pending()
    env.process_and_handle()  # first failure -> flag set
    capsys.readouterr()

    # Recovery clears the suppression.
    env.driver.initialize_should_fail = False
    env.process_and_handle()
    assert env.md.should_suppress_reinit_failure() is False
    capsys.readouterr()

    # The device goes bad again: reinit is pending and initialize fails.
    env.driver.initialize_should_fail = True
    env.md.mark_reinitialize_pending()
    result = env.process_and_handle()
    out = capsys.readouterr().out

    assert result["status"] == "reinitialization_failed"
    assert result["log_failure_warning"] is True
    assert _WARNING in out
