# devices/system_information/system_information_device.py - System information device
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

from devices.device import Device

# Import the authoritative list from system_information.py
from system_information import SYSTEM_INFORMATION_SECTIONS


class SystemInformationDevice(Device):
    """Device that collects and publishes system information."""

    def __init__(self, system_information):
        """Initialize the system information device."""
        self._system_information = system_information
        self._include = None
        self._initialized = False

    def _validate_config(self, config):
        """Validate device configuration."""
        # config must be a dictionary
        if not isinstance(config, dict):
            raise ValueError("system-information device config must be a dictionary")

        # include must exist and be a non-empty list
        if "include" not in config:
            raise ValueError("system-information device config must include 'include'")

        include = config["include"]
        if not isinstance(include, list):
            raise ValueError("system-information device include must be a list")
        if len(include) == 0:
            raise ValueError("system-information device include must not be empty")

        # Every entry must be a string
        for entry in include:
            if not isinstance(entry, str):
                raise ValueError(
                    "system-information device include entries must be strings, got: {}".format(
                        type(entry).__name__
                    )
                )

        # Every entry must be a supported section
        # Check for duplicates and unsupported sections
        seen = set()
        for entry in include:
            if entry not in SYSTEM_INFORMATION_SECTIONS:
                raise ValueError(
                    "system-information device include contains unsupported section: '{}'".format(
                        entry
                    )
                )
            if entry in seen:
                raise ValueError(
                    "system-information device include contains duplicate section: '{}'".format(
                        entry
                    )
                )
            seen.add(entry)

    def _get_section(self, section):
        """Get a system information section value."""
        if section == "network":
            return self._system_information.get_network()
        if section == "memory":
            return self._system_information.get_memory()
        if section == "runtime":
            return self._system_information.get_runtime()
        if section == "devices":
            return self._system_information.get_devices()
        if section == "cpu":
            return self._system_information.get_cpu()
        if section == "machine":
            return self._system_information.get_machine()
        if section == "communications":
            return self._system_information.get_communications()
        if section == "queues":
            return self._system_information.get_queues()
        if section == "device_status":
            return self._system_information.get_device_status()
        if section == "configuration":
            return self._system_information.get_configuration()
        if section == "capabilities":
            return self._system_information.get_capabilities()

        raise ValueError(
            "Unsupported system information section: {}".format(section)
        )

    def initialize(self, config):
        """Initialize the device with its configuration."""
        self._validate_config(config)
        self._include = tuple(config["include"])
        self._initialized = True

    def read(self):
        """Read system information for configured sections."""
        if not self._initialized:
            raise RuntimeError("System information device is not initialized")

        payload = {}

        for section in self._include:
            payload[section] = self._get_section(section)

        return payload
