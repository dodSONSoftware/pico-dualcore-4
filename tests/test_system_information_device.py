# test_system_information_device.py - System-information driver read() section batching
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Host-side tests for the system-information driver's read() section dispatch.

The ``devices`` and ``device_status`` sections share one snapshot source
(``get_device_sections``): read() must take it at most once per read no
matter how many device sections are configured, route every other section
through its own ``get_<section>`` getter, and keep the configured section
order in the payload."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from devices.system_information.system_information_device import (  # noqa: E402
    SystemInformationDevice,
)


class CountingSystemInformation:
    """SystemInformation stand-in that counts shared-snapshot fetches and
    returns distinct values so a test can tell the sections' sources apart."""

    def __init__(self):
        self.device_section_calls = 0
        self.section_calls = []

    def get_device_sections(self):
        self.device_section_calls += 1
        return {
            "devices": {"configured": 2, "active": 2, "source": "shared-snapshot"},
            "device_status": [{"id": "d1", "source": "shared-snapshot"}],
        }

    def get_network(self):
        self.section_calls.append("network")
        return {"ssid": "test", "source": "direct"}

    def get_memory(self):
        self.section_calls.append("memory")
        return {"heap_free_bytes": 1, "source": "direct"}


def _initialized_device(include):
    source = CountingSystemInformation()
    device = SystemInformationDevice(source)
    device.initialize({"include": list(include)})
    return source, device


def test_read_with_both_device_sections_takes_one_snapshot():
    """Both device sections configured: the shared snapshot is fetched
    exactly once and both sections come from that one fetch."""
    source, device = _initialized_device(["devices", "device_status", "network"])

    payload = device.read()

    assert source.device_section_calls == 1
    assert payload["devices"]["source"] == "shared-snapshot"
    assert payload["device_status"][0]["source"] == "shared-snapshot"
    assert payload["network"]["source"] == "direct"
    assert list(payload) == ["devices", "device_status", "network"]
    assert source.section_calls == ["network"]


def test_read_with_one_device_section_takes_one_snapshot():
    source, device = _initialized_device(["memory", "device_status"])

    payload = device.read()

    assert source.device_section_calls == 1
    assert payload["device_status"][0]["source"] == "shared-snapshot"
    assert source.section_calls == ["memory"]


def test_read_without_device_sections_never_takes_the_snapshot():
    source, device = _initialized_device(["network", "memory"])

    payload = device.read()

    assert source.device_section_calls == 0
    assert payload["network"]["source"] == "direct"
    assert payload["memory"]["source"] == "direct"
