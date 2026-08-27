# devices/device.py - Device interface for sensor drivers
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

class Device:
    """Base interface for sensor devices."""

    def initialize(self, config):
        """Initialize device with config; validate hardware, configure registers."""
        raise NotImplementedError

    def read(self):
        """Read one telemetry sample as a JSON-safe dict."""
        raise NotImplementedError
