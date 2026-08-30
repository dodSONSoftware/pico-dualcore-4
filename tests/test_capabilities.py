# test_capabilities.py - Capabilities section and runtime identity fields
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import pathlib
import sys
from unittest.mock import MagicMock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

sys.modules.setdefault("machine", MagicMock())

import device_factory  # noqa: E402
import system_information as si  # noqa: E402
from version import FIRMWARE_BUILD_COMMIT, FIRMWARE_VERSION, MESSAGE_SCHEMA_VERSION  # noqa: E402


class _Mailboxes:
    def __init__(self, hardware=None):
        self._hardware = hardware

    def get_hardware(self):
        return self._hardware

    def get_network_snapshot(self):
        return {"wifi_connected": True, "mqtt_connected": True}

    def get_utc_snapshot(self):
        return None


class _Intercore:
    def __init__(self, hardware=None):
        self.state_mailboxes = _Mailboxes(hardware)


def _make_si(hardware=None, runtime_id=None):
    return si.SystemInformation(_Intercore(hardware), None, runtime_id)


def test_supported_device_types_registry_is_the_single_source():
    assert device_factory.SUPPORTED_DEVICE_TYPES == ("system-information",)
    assert list(device_factory.supported_device_types()) == ["system-information"]


def test_capabilities_devices_are_type_names_not_instance_ids():
    caps = _make_si().get_capabilities()
    assert caps["devices"] == list(device_factory.SUPPORTED_DEVICE_TYPES)
    # Capability names, never configured instance ids.
    assert "device1" not in caps["devices"]


def test_capabilities_features_is_the_implemented_tuple_only():
    caps = _make_si().get_capabilities()
    assert caps["features"] == list(si.FIRMWARE_FEATURE_CAPABILITIES)
    assert set(caps["features"]) == {
        "health",
        "commands",
        "mqtt_qos1",
        "outage_buffering",
        "network_diagnostics",
        "heap_pressure_queue",
        "runtime_configuration",
    }
    # Not implemented: must not be advertised.
    for absent in ("tls", "broker_failover", "watchdog"):
        assert absent not in caps["features"]


def test_capabilities_is_a_system_information_section():
    assert "capabilities" in si.SYSTEM_INFORMATION_SECTIONS
    # The auto-discovery getter naming contract (get_<section>).
    assert hasattr(si.SystemInformation, "get_capabilities")


def test_capabilities_section_config_validation():
    from devices.system_information.system_information_device import SystemInformationDevice

    device = SystemInformationDevice(_make_si())
    device.initialize({"include": ["capabilities"]})
    payload = device.read()
    assert payload["capabilities"]["devices"] == ["system-information"]
    assert "features" in payload["capabilities"]

    # An unknown section is still rejected.
    device2 = SystemInformationDevice(_make_si())
    try:
        device2.initialize({"include": ["not-a-section"]})
        raise AssertionError("expected ValueError for an unsupported section")
    except ValueError:
        pass


def test_runtime_section_carries_identity_fields():
    runtime = _make_si(runtime_id="runtime_0123456789abcdef").get_runtime()
    assert runtime["firmware_version"] == FIRMWARE_VERSION
    assert runtime["firmware_build_commit"] == FIRMWARE_BUILD_COMMIT
    assert runtime["message_schema_version"] == MESSAGE_SCHEMA_VERSION
    assert runtime["runtime_id"] == "runtime_0123456789abcdef"


def test_runtime_section_runtime_id_null_when_not_provided():
    runtime = _make_si().get_runtime()
    assert runtime["runtime_id"] is None


def test_machine_section_reports_boot_reason():
    hardware_snapshot = {
        "hardware_type": "pico_w",
        "minimum_free_heap_bytes": 65536,
        "machine": "Raspberry Pi Pico W with RP2040",
        "last_reset_cause": "watchdog_reset",
        "boot_reason": "watchdog_recovery",
    }
    machine = _make_si(hardware=hardware_snapshot).get_machine()
    assert machine["last_reset_cause"] == "watchdog_reset"
    assert machine["boot_reason"] == "watchdog_recovery"


def test_machine_section_boot_reason_degrades_to_unknown():
    # No snapshot at all.
    assert _make_si().get_machine()["boot_reason"] == "unknown"
    # A snapshot without the key (an older firmware's snapshot shape).
    older = _make_si(hardware={"last_reset_cause": "power_on_reset"}).get_machine()
    assert older["boot_reason"] == "unknown"
