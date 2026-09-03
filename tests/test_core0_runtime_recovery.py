# test_core0_runtime_recovery.py - Tests for main()'s operational runtime recovery boundary
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Host-side tests for the Core 0 runtime recovery boundary in main().

Core 0's fail-fast policy (a MemoryError re-raised through every generic handler) is local: continuing to allocate on an exhausted heap is unsafe. Without a system-level policy, a Core 0 that terminated (a MemoryError, an unexpected exception escaping start()/run()) left the board with no networking, no Core 1 supervision, and no recovery.

The boundary in main() splits startup into two phases with different contracts:

- Deterministic startup validation (hardware, config, Core 0 construction) fails fast and stays visible: a misconfigured or unsupported board must stay down with a diagnosable error, not reboot forever.
- Operational runtime (Core0.start() through Core0.run()) converts an unrecoverable Core 0 exception into a controlled machine.reset() instead of application termination.

Covers: MemoryError from start() and run() (board reset, no exception escapes), ordinary Exception from run() (board reset), Core 1 never starts when start() fails (gating preserved), and deterministic startup failure (exception stays visible, NO reset)."""

import importlib
import pathlib
import sys
import time as _real_time
import types
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

ROOT = pathlib.Path(__file__).resolve().parents[1]


class _FakeTime:
    """Controllable stand-in for MicroPython's time module."""

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
        # Anything not explicitly faked falls through to the real time
        # module so host tooling keeps working.
        return getattr(_real_time, name)


_FAKE_TIME = _FakeTime()


def _fake_core1_module(core1_main=None):
    """A stand-in core1 module; record whether Core 1 was actually started."""
    fake = types.ModuleType("core1")
    fake.core1_main = core1_main if core1_main is not None else (lambda *args: None)
    return fake


@pytest.fixture
def host_boot(monkeypatch):
    """Boot main() on the host with deterministic startup passing.

    Installs the MicroPython stand-ins, reloads core0 and main under them, and fakes detect_hardware() to a supported Pico W so the validation phase passes. Deterministic-failure tests re-patch detect_hardware / load_config to raise.

    The stand-ins are installed inside the fixture (not at collection time): later-collected modules import the real wifi/mqtt/time at collection, and mocked sys.modules entries would shadow them."""
    machine = MagicMock(name="machine")
    sys.modules["time"] = _FAKE_TIME
    sys.modules["machine"] = machine
    debug_mock = MagicMock(name="debug")
    debug_mock.DEBUG = False
    sys.modules["debug"] = debug_mock
    sys.modules["wifi"] = MagicMock(name="wifi")
    sys.modules["mqtt"] = MagicMock(name="mqtt")

    importlib.reload(importlib.import_module("uptime"))
    core0_mod = importlib.import_module("core0")
    importlib.reload(core0_mod)

    main_mod = importlib.import_module("main")
    importlib.reload(main_mod)

    def _pico_w():
        return {
            "hardware_type": "pico_w",
            "machine": "Raspberry Pi Pico W with RP2040",
            "preferred_free_heap_bytes": 64 * 1024,
            "minimum_free_heap_bytes": 48 * 1024,
        }

    monkeypatch.setattr(main_mod, "detect_hardware", _pico_w)
    return machine, main_mod


def test_memory_error_in_core0_start_resets_the_board(host_boot, monkeypatch):
    """A MemoryError in the operational phase must reset the board, not escape."""
    machine, main_mod = host_boot
    core0_mod = sys.modules["core0"]

    def _exhaust(self):
        raise MemoryError

    monkeypatch.setattr(core0_mod.Core0, "start", _exhaust)

    # The boundary converts the MemoryError into a controlled reset: main()
    # returns (on hardware machine.reset() never returns at all).
    main_mod.main()

    machine.reset.assert_called_once()


def test_memory_error_in_core0_run_resets_the_board(host_boot, monkeypatch):
    """A MemoryError escaping the run loop must reset the board, not terminate."""
    machine, main_mod = host_boot
    core0_mod = sys.modules["core0"]

    monkeypatch.setattr(core0_mod.Core0, "start", lambda self: None)

    def _exhaust(self):
        raise MemoryError

    monkeypatch.setattr(core0_mod.Core0, "run", _exhaust)
    monkeypatch.setitem(sys.modules, "core1", _fake_core1_module())

    main_mod.main()

    machine.reset.assert_called_once()


def test_unexpected_exception_in_core0_run_resets_the_board(host_boot, monkeypatch):
    """Any unrecoverable Core 0 exception (not just OOM) resets the board."""
    machine, main_mod = host_boot
    core0_mod = sys.modules["core0"]

    monkeypatch.setattr(core0_mod.Core0, "start", lambda self: None)

    def _fail(self):
        raise RuntimeError("unexpected parser failure")

    monkeypatch.setattr(core0_mod.Core0, "run", _fail)
    monkeypatch.setitem(sys.modules, "core1", _fake_core1_module())

    main_mod.main()

    machine.reset.assert_called_once()


def test_core1_never_starts_when_core0_start_fails(host_boot, monkeypatch):
    """Core 1 gating holds: a failed start() must not start the Core 1 thread."""
    machine, main_mod = host_boot
    core0_mod = sys.modules["core0"]
    started = {"core1_main": 0}

    def _exhaust(self):
        raise MemoryError

    monkeypatch.setattr(core0_mod.Core0, "start", _exhaust)

    def _core1_main(*args):
        started["core1_main"] += 1

    monkeypatch.setitem(sys.modules, "core1", _fake_core1_module(_core1_main))

    main_mod.main()

    assert started["core1_main"] == 0
    machine.reset.assert_called_once()


def test_deterministic_startup_failure_stays_visible(host_boot, monkeypatch):
    """Unsupported hardware must stay down with a diagnosable error, not reboot.

    Hardware detection is deterministic startup validation, OUTSIDE the recovery boundary; rebooting here would loop a misconfigured board forever."""
    machine, main_mod = host_boot

    def _unsupported():
        raise RuntimeError("Unsupported hardware: x86_64")

    monkeypatch.setattr(main_mod, "detect_hardware", _unsupported)

    with pytest.raises(RuntimeError):
        main_mod.main()

    machine.reset.assert_not_called()


def test_config_rejection_stays_visible(host_boot, monkeypatch):
    """A fail-fast config rejection must stay visible, not reboot forever.

    Config loading is deterministic validation, outside the boundary: the exception escapes to the operator instead of becoming a reboot loop."""
    machine, main_mod = host_boot
    import config_manager

    def _reject(path):
        raise ValueError("unknown top-level key: 'bogus'")

    # Startup now loads the config through the configuration manager's
    # recovery path: patch the symbol that path actually calls.
    monkeypatch.setattr(config_manager, "load_config", _reject)

    with pytest.raises(ValueError):
        main_mod.main()

    machine.reset.assert_not_called()
