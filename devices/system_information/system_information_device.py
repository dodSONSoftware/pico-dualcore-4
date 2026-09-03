# devices/system_information/system_information_device.py - System information device
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

from devices.device import Device
from devices.system_information.validation import validate_config


class SystemInformationDevice(Device):
    """Device that collects and publishes system information."""

    def __init__(self, system_information):
        self._system_information = system_information
        self._include = None
        self._initialized = False

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

        raise ValueError(
            "Unsupported system information section: {}".format(section)
        )

    def initialize(self, config):
        """Initialize with the shared pure validation (same rules as startup)."""
        # Hardware is untouched until after the config is known-valid.
        validate_config(config)
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
