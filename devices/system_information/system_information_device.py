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

    # The sections that share one device status-snapshot source.
    _DEVICE_SECTIONS = ("devices", "device_status")

    def _get_section(self, section):
        """Get a system information section value (same dispatch as Core 1's
        full collection: a section is the ``get_<section>`` method)."""
        getter = getattr(
            self._system_information, "get_{}".format(section), None
        )
        if getter is None:
            raise ValueError(
                "Unsupported system information section: {}".format(section)
            )
        return getter()

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
        device_sections = None
        for section in self._include:
            if section in self._DEVICE_SECTIONS:
                # Both device sections share one snapshot source: take it
                # once when both are configured.
                if device_sections is None:
                    device_sections = self._system_information.get_device_sections()
                payload[section] = device_sections[section]
            else:
                payload[section] = self._get_section(section)

        return payload
