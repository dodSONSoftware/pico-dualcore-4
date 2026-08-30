# device_factory.py - Device construction
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

from devices.system_information.system_information_device import SystemInformationDevice

# The single source of truth for the device types this firmware can build.
# Capabilities report the supported type NAMES from this registry -- never
# configured instance ids, and never a filesystem scan.
SUPPORTED_DEVICE_TYPES = ("system-information",)


def supported_device_types():
    """Return the device types this firmware can build."""
    return SUPPORTED_DEVICE_TYPES


def create_device(device_definition, system_information=None):
    device_type = device_definition["device_type"]

    if device_type == "system-information":
        if system_information is None:
            raise ValueError("system-information requires system_information")
        return SystemInformationDevice(system_information)

    raise ValueError("Unsupported device type: {}".format(device_type))
