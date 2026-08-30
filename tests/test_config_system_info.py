# test_config_system_info.py - configuration section and runtime_configuration
# capability
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Host-side tests for the compact ``configuration`` system-information section
and the ``runtime_configuration`` capability.

The section reports only the committed-configuration bookkeeping (schema
version, firmware-managed generation, the SHA-256 checksum of the committed
config.json bytes, and the RESTART_REQUIRED state) -- never the full config
and never credentials. It is observational (invariant 9): it adds no degraded
reason, and ``null`` (absent shared state) is never converted to ``false``.
"""

import json
import pathlib
import sys
from unittest.mock import MagicMock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

sys.modules.setdefault("machine", MagicMock())

import config  # noqa: E402
import system_information as si  # noqa: E402
from version import CONFIG_SCHEMA_VERSION, FIRMWARE_VERSION  # noqa: E402
from devices.system_information.system_information_device import (  # noqa: E402
    SystemInformationDevice,
)


ROOT = pathlib.Path(__file__).resolve().parents[1]


def _base_config():
    return json.loads((ROOT / "config.json").read_text())


class _Intercore:
    def __init__(self, config_state):
        self.config_state = config_state
        self.state_mailboxes = MagicMock()


def _state(base=None, checksum="ab" * 32):
    return config.ConfigState(base or _base_config(), checksum)


def _make_si(config_state):
    return si.SystemInformation(_Intercore(config_state), None)


def test_configuration_is_a_system_information_section():
    assert "configuration" in si.SYSTEM_INFORMATION_SECTIONS
    # Auto-discovery getter naming contract (get_<section>).
    assert hasattr(si.SystemInformation, "get_configuration")


def test_configuration_section_is_compact_bookkeeping_not_the_full_config():
    state = _state()
    section = _make_si(state).get_configuration()

    # Exactly the bookkeeping fields -- never the full config, never any key
    # that carries a live setting or a credential.
    assert set(section) == {
        "config_schema_version",
        "config_generation",
        "config_checksum_sha256",
        "reboot_required",
        "pending_restart_keys",
    }
    assert section["config_schema_version"] == CONFIG_SCHEMA_VERSION
    assert section["config_generation"] == 0
    assert section["config_checksum_sha256"] == "ab" * 32
    assert section["reboot_required"] is False
    assert section["pending_restart_keys"] == []

    # The full configuration and its secret-free contents are NOT carried.
    base = _base_config()
    for key in ("source", "read_loop_sec", "devices", "mqtt_broker_ip_address"):
        assert key not in section, key
    blob = json.dumps(section)
    assert base["mqtt_broker_ip_address"] not in blob


def test_configuration_section_null_when_state_absent():
    # Older test doubles with no shared ConfigState: null, never false.
    section = _make_si(None).get_configuration()
    assert section["config_schema_version"] is None
    assert section["config_generation"] is None
    assert section["config_checksum_sha256"] is None
    assert section["reboot_required"] is None
    assert section["pending_restart_keys"] is None


def test_configuration_section_reflects_pending_restart():
    base = _base_config()
    state = _state()
    # Simulate a RESTART_REQUIRED commit: generation bumps, flag + key set.
    candidate = dict(base)
    candidate["config_generation"] = base["config_generation"] + 1
    candidate["max_intercore_event_entries"] = base["max_intercore_event_entries"] + 4
    state.commit(candidate, "cd" * 32)

    section = _make_si(state).get_configuration()
    assert section["config_generation"] == base["config_generation"] + 1
    assert section["config_checksum_sha256"] == "cd" * 32
    assert section["reboot_required"] is True
    assert section["pending_restart_keys"] == ["max_intercore_event_entries"]


def test_device_include_configuration_section():
    state = _state()
    device = SystemInformationDevice(_make_si(state))
    device.initialize({"include": ["configuration"]})
    payload = device.read()
    assert "configuration" in payload
    assert payload["configuration"]["config_schema_version"] == CONFIG_SCHEMA_VERSION
    assert payload["configuration"]["reboot_required"] is False


def test_device_include_rejects_unknown_section():
    device = SystemInformationDevice(_make_si(_state()))
    try:
        device.initialize({"include": ["not-a-section"]})
        raise AssertionError("expected ValueError for an unsupported section")
    except ValueError:
        pass


def test_capabilities_advertise_runtime_configuration():
    caps = _make_si(None).get_capabilities()
    assert "runtime_configuration" in caps["features"]
    assert caps["features"] == list(si.FIRMWARE_FEATURE_CAPABILITIES)
    # Still not advertised: capabilities the firmware does not implement.
    for absent in ("tls", "broker_failover", "watchdog"):
        assert absent not in caps["features"]
    # The runtime section still reports the (bumped) firmware version.
    runtime = _make_si(None).get_runtime()
    assert runtime["firmware_version"] == FIRMWARE_VERSION
