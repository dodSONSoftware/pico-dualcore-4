#!/usr/bin/env python3
# release.py - Build a deployable artifact for the clean rebuild
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import pathlib
import sys
import tarfile
from version import FIRMWARE_VERSION

ROOT = pathlib.Path(__file__).resolve().parent

# Required files for deployment (Python files and config)
REQUIRED_FILES = {
    "config.json",
    "main.py",
    "core0.py",
    "core1.py",
    "command_protocol.py",
    "config.py",
    "config_manager.py",
    "intercore.py",
    "message_protocol.py",
    "message_serializer.py",
    "wifi.py",
    "mqtt.py",
    "mqtt_client.py",
    "system_information.py",
    "uptime.py",
    "device_manager.py",
    "device_factory.py",
    "led_manager.py",
    "debug.py",
    "version.py",
    "hardware.py",
}

# Required package files (directories with __init__.py)
REQUIRED_PACKAGES = {
    "devices/__init__.py",
    "devices/device.py",
    "devices/system_information/__init__.py",
    "devices/system_information/validation.py",
    "devices/system_information/system_information_device.py",
}

FILES = REQUIRED_FILES | REQUIRED_PACKAGES


def main():
    if len(sys.argv) > 2:
        raise SystemExit("Usage: python3 release.py [OUTPUT_DIR]")
    output_dir = pathlib.Path(sys.argv[1]) if len(sys.argv) == 2 else ROOT / "releases"
    output_dir.mkdir(parents=True, exist_ok=True)
    artifact = output_dir / "sensor-firmware-{}.tar.gz".format(FIRMWARE_VERSION)
    with tarfile.open(artifact, "w:gz") as archive:
        for name in FILES:
            path = ROOT / name
            if not path.exists():
                raise SystemExit("Missing required file: {}".format(name))
            archive.add(path, arcname=name)
    print("Artifact: {}".format(artifact))


if __name__ == "__main__":
    main()
