# devices/device.py - Device interface for sensor drivers
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT


class DeviceValidationError(ValueError):
    """A pure validation failure (never raised by hardware), mapped by
    ``config.py`` onto ``ConfigError`` with a stable ``code`` (``invalid_value``
    / ``missing_key`` / ``unknown_config_fields`` / ``unsupported_device_type``).
    Outside the device manager's operational (``OSError``) failure domain: a
    driver ``initialize()`` that reaches its validator means the
    config-boundary validation was bypassed, so the error escapes the retry
    path to Core 1's recovery boundary instead of retrying the same fault."""

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
