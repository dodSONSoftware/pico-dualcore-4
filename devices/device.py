# devices/device.py - Device interface for sensor drivers
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT


class DeviceValidationError(ValueError):
    """A pure device-definition or device-config validation failure.

    Raised by the pure validators (never by hardware), so ``config.py`` can
    map it onto its own ``ConfigError`` and the write-config response can carry
    a stable cause. ``code`` is one of ``invalid_value`` / ``missing_key`` /
    ``unknown_config_fields`` / ``unsupported_device_type``; ``str(err)`` stays
    the full human-readable message. A ``ValueError`` subclass so the existing
    per-device ``except Exception`` retry path in the device manager still
    catches it."""

    def __init__(self, message, code="invalid_value"):
        super().__init__(message)
        self.code = code


class Device:
    """Base interface for sensor devices."""

    def initialize(self, config):
        """Initialize device with config; validate hardware, configure registers."""
        raise NotImplementedError

    def read(self):
        """Read one telemetry sample as a JSON-safe dict."""
        raise NotImplementedError
