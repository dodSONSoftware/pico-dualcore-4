# test_hardware.py - Tests for hardware detection
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import pathlib
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import hardware

# Mock the 'machine' module before importing system_information
# since it's MicroPython-specific and not available on the host
class MockMachine:
    freq = staticmethod(lambda: 125000000)

sys.modules['machine'] = MockMachine()

from system_information import SystemInformation


class FakeResetCauseMachine:
    """Fake ``machine`` module exposing reset-cause constants for host tests.

    ``reset_cause()`` returns the configured cause, or raises the configured
    error, so each test drives the production mapping path in hardware.py.
    """

    PWRON_RESET = 0
    HARD_RESET = 1
    WDT_RESET = 2
    DEEPSLEEP_RESET = 3
    SOFT_RESET = 4

    def __init__(self, cause=None, error=None):
        self._cause = cause
        self._error = error

    def reset_cause(self):
        if self._error is not None:
            raise self._error
        return self._cause


class MockInterCore:
    """Mock inter-core bus for testing."""
    def __init__(self, hardware=None):
        self.outbound_queue = None
        self.event_queue = None
        self.state_mailboxes = MockStateMailboxes(hardware)


class MockStateMailboxes:
    """Mock state mailboxes for testing."""
    def __init__(self, hardware=None):
        self._hardware = hardware

    def get_network_snapshot(self):
        return {"wifi_connected": False, "mqtt_connected": False}

    def get_utc_snapshot(self):
        return None

    def get_hardware(self):
        return self._hardware

    def set_hardware(self, hardware):
        self._hardware = hardware


class MockConfig:
    """Mock config for testing."""
    def __init__(self):
        self._config = {"read_loop_sec": 60}


class TestHardwareConstants:
    """Test hardware type and heap reserve constants."""

    def test_pico_w_type_constant(self):
        """Verify Pico W hardware type constant."""
        assert hardware.HARDWARE_TYPE_PICO_W == "pico_w"

    def test_pico_2_w_type_constant(self):
        """Verify Pico 2 W hardware type constant."""
        assert hardware.HARDWARE_TYPE_PICO_2_W == "pico_2_w"

    def test_pico_w_heap_reserve(self):
        """Verify Pico W minimum free-heap reserve."""
        assert hardware.PICO_W_MIN_FREE_HEAP_BYTES == 65536  # 64 KiB

    def test_pico_2_w_heap_reserve(self):
        """Verify Pico 2 W minimum free-heap reserve."""
        assert hardware.PICO_2_W_MIN_FREE_HEAP_BYTES == 131072  # 128 KiB

    def test_reset_cause_constants(self):
        """Verify the canonical reset-cause strings."""
        assert hardware.RESET_CAUSE_POWER_ON == "power_on_reset"
        assert hardware.RESET_CAUSE_HARD == "hard_reset"
        assert hardware.RESET_CAUSE_WATCHDOG == "watchdog_reset"
        assert hardware.RESET_CAUSE_DEEP_SLEEP == "deep_sleep_reset"
        assert hardware.RESET_CAUSE_SOFT == "soft_reset"
        assert hardware.RESET_CAUSE_UNKNOWN == "unknown"


class TestReadLastResetCause:
    """Test the production machine.reset_cause() -> canonical string mapping."""

    def _install(self, monkeypatch, fake):
        monkeypatch.setitem(sys.modules, "machine", fake)

    def test_power_on_reset(self, monkeypatch):
        self._install(monkeypatch, FakeResetCauseMachine(cause=FakeResetCauseMachine.PWRON_RESET))

        assert hardware.read_last_reset_cause() == "power_on_reset"

    def test_hard_reset(self, monkeypatch):
        self._install(monkeypatch, FakeResetCauseMachine(cause=FakeResetCauseMachine.HARD_RESET))

        assert hardware.read_last_reset_cause() == "hard_reset"

    def test_watchdog_reset(self, monkeypatch):
        self._install(monkeypatch, FakeResetCauseMachine(cause=FakeResetCauseMachine.WDT_RESET))

        assert hardware.read_last_reset_cause() == "watchdog_reset"

    def test_deep_sleep_reset(self, monkeypatch):
        self._install(monkeypatch, FakeResetCauseMachine(cause=FakeResetCauseMachine.DEEPSLEEP_RESET))

        assert hardware.read_last_reset_cause() == "deep_sleep_reset"

    def test_soft_reset(self, monkeypatch):
        self._install(monkeypatch, FakeResetCauseMachine(cause=FakeResetCauseMachine.SOFT_RESET))

        assert hardware.read_last_reset_cause() == "soft_reset"

    def test_unknown_integer_returns_unknown(self, monkeypatch):
        """An unrecognized cause value must safely become "unknown"."""
        self._install(monkeypatch, FakeResetCauseMachine(cause=99))

        assert hardware.read_last_reset_cause() == "unknown"

    def test_ordinary_exception_returns_unknown(self, monkeypatch):
        """An ordinary read failure must degrade to "unknown", not raise."""
        self._install(monkeypatch, FakeResetCauseMachine(error=RuntimeError("no cause")))

        assert hardware.read_last_reset_cause() == "unknown"

    def test_memory_error_propagates(self, monkeypatch):
        """Heap exhaustion must propagate, not be swallowed as "unknown"."""
        self._install(monkeypatch, FakeResetCauseMachine(error=MemoryError("heap")))

        with pytest.raises(MemoryError):
            hardware.read_last_reset_cause()


class TestDetectHardware:
    """Test detect_hardware() function."""

    def test_pico_w_detection(self, monkeypatch):
        """Pico W should be classified correctly."""
        monkeypatch.setattr(hardware.os, "uname", lambda: type("Uname", (), {"machine": "Raspberry Pi Pico W with RP2040"})())

        result = hardware.detect_hardware()

        assert result["hardware_type"] == "pico_w"
        assert result["machine"] == "Raspberry Pi Pico W with RP2040"
        assert result["minimum_free_heap_bytes"] == 65536

    def test_pico_2_w_detection(self, monkeypatch):
        """Pico 2 W should be classified correctly."""
        monkeypatch.setattr(hardware.os, "uname", lambda: type("Uname", (), {"machine": "Raspberry Pi Pico 2 W with RP2350"})())

        result = hardware.detect_hardware()

        assert result["hardware_type"] == "pico_2_w"
        assert result["machine"] == "Raspberry Pi Pico 2 W with RP2350"
        assert result["minimum_free_heap_bytes"] == 131072

    def test_unsupported_hardware_raises_error(self, monkeypatch):
        """Unsupported hardware should raise RuntimeError."""
        monkeypatch.setattr(hardware.os, "uname", lambda: type("Uname", (), {"machine": "UNKNOWN_BOARD"})())

        with pytest.raises(RuntimeError) as exc_info:
            hardware.detect_hardware()

        assert "Unsupported hardware" in str(exc_info.value)
        assert "UNKNOWN_BOARD" in str(exc_info.value)

    def test_pico_w_detection_old_format(self, monkeypatch):
        """Pico W should also be classified with old format machine string."""
        monkeypatch.setattr(hardware.os, "uname", lambda: type("Uname", (), {"machine": "RPI_PICO_W with RP2040"})())

        result = hardware.detect_hardware()

        assert result["hardware_type"] == "pico_w"
        assert result["machine"] == "RPI_PICO_W with RP2040"
        assert result["minimum_free_heap_bytes"] == 65536

    def test_pico_2_w_detection_old_format(self, monkeypatch):
        """Pico 2 W should also be classified with old format machine string."""
        monkeypatch.setattr(hardware.os, "uname", lambda: type("Uname", (), {"machine": "RPI_PICO2_W with RP2350"})())

        result = hardware.detect_hardware()

        assert result["hardware_type"] == "pico_2_w"
        assert result["machine"] == "RPI_PICO2_W with RP2350"
        assert result["minimum_free_heap_bytes"] == 131072

    def test_pico_w_detection_with_trailing_spaces(self, monkeypatch):
        """Pico W with trailing spaces should be classified correctly."""
        monkeypatch.setattr(hardware.os, "uname", lambda: type("Uname", (), {"machine": "Raspberry Pi Pico W with RP2040  "})())

        with pytest.raises(RuntimeError):
            hardware.detect_hardware()

    def test_get_minimum_free_heap_pico_w(self):
        """Verify get_minimum_free_heap returns correct value for Pico W."""
        assert hardware.get_minimum_free_heap("pico_w") == 65536

    def test_get_minimum_free_heap_pico_2_w(self):
        """Verify get_minimum_free_heap returns correct value for Pico 2 W."""
        assert hardware.get_minimum_free_heap("pico_2_w") == 131072

    def test_get_minimum_free_heap_unknown_raises_value_error(self):
        """get_minimum_free_heap should raise ValueError for unknown hardware."""
        with pytest.raises(ValueError) as exc_info:
            hardware.get_minimum_free_heap("unknown_hardware")

        assert "Unknown hardware type" in str(exc_info.value)


class TestIsSupportedHardware:
    """Test is_supported_hardware() function."""

    def test_pico_w_is_supported(self, monkeypatch):
        """Pico W should be reported as supported."""
        monkeypatch.setattr(hardware.os, "uname", lambda: type("Uname", (), {"machine": "RPI_PICO_W with RP2040"})())

        assert hardware.is_supported_hardware() is True

    def test_pico_2_w_is_supported(self, monkeypatch):
        """Pico 2 W should be reported as supported."""
        monkeypatch.setattr(hardware.os, "uname", lambda: type("Uname", (), {"machine": "RPI_PICO2_W with RP2350"})())

        assert hardware.is_supported_hardware() is True

    def test_unsupported_hardware_not_supported(self, monkeypatch):
        """Unsupported hardware should be reported as not supported."""
        monkeypatch.setattr(hardware.os, "uname", lambda: type("Uname", (), {"machine": "UNKNOWN_BOARD"})())

        assert hardware.is_supported_hardware() is False


class TestMachinePatterns:
    """Test machine string pattern constants."""

    def test_pico_w_patterns(self):
        """Verify Pico W machine patterns are defined."""
        assert isinstance(hardware._PICO_W_MACHINE_PATTERNS, tuple)
        assert len(hardware._PICO_W_MACHINE_PATTERNS) >= 2
        assert "Raspberry Pi Pico W with RP2040" in hardware._PICO_W_MACHINE_PATTERNS
        assert "RPI_PICO_W with RP2040" in hardware._PICO_W_MACHINE_PATTERNS

    def test_pico_2_w_patterns(self):
        """Verify Pico 2 W machine patterns are defined."""
        assert isinstance(hardware._PICO_2_W_MACHINE_PATTERNS, tuple)
        assert len(hardware._PICO_2_W_MACHINE_PATTERNS) >= 2
        assert "Raspberry Pi Pico 2 W with RP2350" in hardware._PICO_2_W_MACHINE_PATTERNS
        assert "RPI_PICO2_W with RP2350" in hardware._PICO_2_W_MACHINE_PATTERNS


class TestClassifyMachine:
    """Test the shared machine-string -> board classification policy in hardware.py."""

    def test_classify_machine_pico_w(self):
        """Pico W machine string classifies to pico_w with its 64 KiB reserve."""
        result = hardware.classify_machine("Raspberry Pi Pico W with RP2040")

        assert result == {
            "hardware_type": "pico_w",
            "minimum_free_heap_bytes": 65536,
        }

    def test_classify_machine_pico_2_w(self):
        """Pico 2 W machine string classifies to pico_2_w with its 128 KiB reserve."""
        result = hardware.classify_machine("Raspberry Pi Pico 2 W with RP2350")

        assert result == {
            "hardware_type": "pico_2_w",
            "minimum_free_heap_bytes": 131072,
        }

    def test_classify_machine_unknown(self):
        """An unrecognized machine string classifies to unknown with no reserve."""
        result = hardware.classify_machine("UNKNOWN_BOARD")

        assert result == {
            "hardware_type": "unknown",
            "minimum_free_heap_bytes": None,
        }

    def test_classify_machine_pico_w_old_format(self):
        """Pico W old-format machine string classifies correctly."""
        result = hardware.classify_machine("RPI_PICO_W with RP2040")

        assert result == {
            "hardware_type": "pico_w",
            "minimum_free_heap_bytes": 65536,
        }

    def test_classify_machine_pico_2_w_old_format(self):
        """Pico 2 W old-format machine string classifies correctly."""
        result = hardware.classify_machine("RPI_PICO2_W with RP2350")

        assert result == {
            "hardware_type": "pico_2_w",
            "minimum_free_heap_bytes": 131072,
        }


class TestGetMachineConsumesSharedClassifier:
    """system_information.get_machine() must report the shared classifier's result."""

    def _system_information(self):
        return SystemInformation(MockInterCore(), MockConfig())

    def test_get_machine_pico_2_w(self, monkeypatch):
        import system_information

        monkeypatch.setattr(
            system_information.os,
            "uname",
            lambda: type("Uname", (), {
                "machine": "Raspberry Pi Pico 2 W with RP2350",
                "version": "v1.23.0",
            })(),
        )

        result = self._system_information().get_machine()

        assert result["hardware_type"] == "pico_2_w"
        assert result["minimum_free_heap_bytes"] == 131072
        assert result["machine"] == "Raspberry Pi Pico 2 W with RP2350"

    def test_get_machine_pico_w(self, monkeypatch):
        import system_information

        monkeypatch.setattr(
            system_information.os,
            "uname",
            lambda: type("Uname", (), {
                "machine": "Raspberry Pi Pico W with RP2040",
                "version": "v1.23.0",
            })(),
        )

        result = self._system_information().get_machine()

        assert result["hardware_type"] == "pico_w"
        assert result["minimum_free_heap_bytes"] == 65536

    def test_get_machine_unknown(self, monkeypatch):
        import system_information

        monkeypatch.setattr(
            system_information.os,
            "uname",
            lambda: type("Uname", (), {
                "machine": "UNKNOWN_BOARD",
                "version": "v1.23.0",
            })(),
        )

        result = self._system_information().get_machine()

        assert result["hardware_type"] == "unknown"
        assert result["minimum_free_heap_bytes"] is None


class TestGetMachineLastResetCause:
    """get_machine() reports the startup snapshot's reset cause (never re-reads it)."""

    def test_reports_reset_cause_from_startup_snapshot(self):
        """The snapshot's last_reset_cause is carried into the machine section."""
        intercore = MockInterCore(hardware={
            "hardware_type": "pico_2_w",
            "machine": "Raspberry Pi Pico 2 W with RP2350",
            "minimum_free_heap_bytes": 131072,
            "last_reset_cause": "watchdog_reset",
        })

        result = SystemInformation(intercore, MockConfig()).get_machine()

        assert result["last_reset_cause"] == "watchdog_reset"

    def test_falls_back_to_unknown_when_field_unavailable(self):
        """A snapshot without the field reports "unknown" rather than failing."""
        intercore = MockInterCore(hardware={
            "hardware_type": "pico_w",
            "machine": "Raspberry Pi Pico W with RP2040",
            "minimum_free_heap_bytes": 65536,
        })

        result = SystemInformation(intercore, MockConfig()).get_machine()

        assert result["last_reset_cause"] == "unknown"

    def test_falls_back_to_unknown_when_snapshot_unavailable(self):
        """No published snapshot at all reports "unknown" rather than failing."""
        result = SystemInformation(MockInterCore(), MockConfig()).get_machine()

        assert result["last_reset_cause"] == "unknown"
