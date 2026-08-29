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


class MockInterCore:
    """Mock inter-core bus for testing."""
    def __init__(self):
        self.outbound_queue = None
        self.event_queue = None
        self.state_mailboxes = MockStateMailboxes()


class MockStateMailboxes:
    """Mock state mailboxes for testing."""
    def get_network_snapshot(self):
        return {"wifi_connected": False, "mqtt_connected": False}

    def get_utc_snapshot(self):
        return None


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
