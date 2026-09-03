# devices/system_information/validation.py - Pure system-information config validation
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Authoritative pure validation for the ``system-information`` device config.
Kept host-importable (no ``machine``) so ``config.py``'s pure path, the
driver's ``initialize()``, and host tests all drive it. Single source of
truth for the reportable sections and the config shape: the driver and the
Core 1 data source both read the section list from here."""

from devices.device import DeviceValidationError

# The sections a system-information device may report. Pure data (no hardware),
# owned here so the pure validator, the driver, and the data source all agree.
SYSTEM_INFORMATION_SECTIONS = (
    "network",
    "memory",
    "runtime",
    "devices",
    "cpu",
    "machine",
    "communications",
    "queues",
    "device_status",
)

# The complete set of keys a system-information device config may contain.
# Anything beyond this is unknown and reported as a qualified path.
ALLOWED_CONFIG_KEYS = frozenset(("include",))


def validate_config(config):
    """Pure validation of a system-information device config (no hardware):
    accepts the ``include`` list and nothing else; raises
    ``DeviceValidationError`` (a ``ValueError``) with a stable ``code`` on the
    first violation. Touches no hardware: a valid definition with no physical
    backing passes — physical absence is an operational failure
    (``initialization_failed`` at boot), not a schema failure."""
    if not isinstance(config, dict):
        raise DeviceValidationError(
            "system-information device config must be an object",
            code="invalid_value",
        )

    unknown = sorted(set(config) - ALLOWED_CONFIG_KEYS)
    if unknown:
        raise DeviceValidationError(
            "system-information device config contains unknown field(s): {}".format(
                ", ".join(unknown)
            ),
            code="unknown_config_fields",
        )

    if "include" not in config:
        raise DeviceValidationError(
            "system-information device config missing required key: include",
            code="missing_key",
        )

    include = config["include"]
    if not isinstance(include, list) or not include:
        raise DeviceValidationError(
            "system-information device include must be a non-empty list",
            code="invalid_value",
        )

    seen = set()
    for entry in include:
        if not isinstance(entry, str):
            raise DeviceValidationError(
                "system-information device include entries must be strings, got: {}".format(
                    type(entry).__name__
                ),
                code="invalid_value",
            )
        if entry not in SYSTEM_INFORMATION_SECTIONS:
            raise DeviceValidationError(
                "system-information device include contains unsupported section: '{}'".format(
                    entry
                ),
                code="invalid_value",
            )
        if entry in seen:
            raise DeviceValidationError(
                "system-information device include contains duplicate section: '{}'".format(
                    entry
                ),
                code="invalid_value",
            )
        seen.add(entry)
