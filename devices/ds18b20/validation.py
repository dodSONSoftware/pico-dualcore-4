# devices/ds18b20/validation.py - Pure DS18B20 config validation
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Authoritative pure validation for the ``ds18b20`` device config.
Kept host-importable (no ``machine``) so ``config.py``'s pure path, the
driver's ``initialize()``, and host tests all drive it. Single source of
truth for the config shape and the pin/ROM/conversion-wait bounds the driver
reads; the sensor protocol itself lives in ``ds18b20_device``.

The DS18B20 is identified by its factory-programmed 64-bit ROM, not by any
bus address: the ROM is the config key, and its first byte is the 1-Wire
family code -- 0x28 for the DS18B20 (a different family, such as the
DS18S20's 0x10, decodes its temperature register differently and is a
configuration error, not a runtime miss). A scan-list position is never an
identity: the bus rescans after a fault, and only the ROM survives that.
"""

from devices.device import DeviceValidationError
from devices.rp2_pins import validate_user_gpio_pin

# The complete set of keys a ds18b20 device config may contain. Anything beyond
# this is unknown and reported as a qualified path.
ALLOWED_CONFIG_KEYS = frozenset((
    "pin",
    "rom",
    "conversion_ms",
    "offsets",
))

# The offset sub-key and its physical bound (fail fast on unit typos such as
# a Fahrenheit value where Celsius is expected).
_OFFSET_KEYS = ("temperature_c",)
_OFFSET_BOUNDS = {
    "temperature_c": 100.0,
}

# 1-Wire is software-timed on any regular GPIO: there is no mux routing
# constraint the way I2C's SDA/SCL groups have, so the data line is only
# bounded by the board rule -- the externally exposed Pico W / Pico 2 W
# GPIOs (``rp2_pins``). The four wireless-reserved pins are not exposed for
# external use and are driven by the CYW43, so the shared validator rejects
# them at the config boundary (a data line on one of them is a
# "valid" configuration that loses network connectivity after a reboot).

# ROM: exactly 8 bytes = 16 hex characters, family code 0x28 first. Case is
# not a semantic difference, so both cases are accepted and the driver
# normalizes to the canonical lowercase form.
_ROM_LENGTH = 16
_FAMILY_CODE = "28"
_HEX_CHARS = frozenset("0123456789abcdefABCDEF")

# Conversion wait (ms). The power-on resolution is 12 bit with a 750 ms
# maximum conversion time, and this driver never configures or verifies the
# sensor's resolution -- so the wait must cover the 12-bit window: a shorter
# wait races the scratchpad and can return the previous value (85 °C after a
# power cycle, which is also a valid real temperature and indistinguishable
# from a stale read). The ceiling is tight because no resolution's conversion
# window comes close to it (the 12-bit maximum is 750 ms; the slack is
# clone/timing margin, not protocol headroom) -- and the wait is one
# uninterrupted sleep inside read() that does not refresh Core 1's liveness
# stamp, so a value anywhere near Core 0's 30 s Core 1 staleness timeout
# would be schema-legal yet reset the device after every read (the former
# 60000 ceiling permitted exactly that, including after a remotely written
# config).
_MIN_CONVERSION_MS = 750
DEFAULT_CONVERSION_MS = 750
_MAX_CONVERSION_MS = 1000


def _is_int(value):
    """True for a real int (bool is an int subclass and is excluded)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_finite_number(value):
    """True for a finite int or float (bool excluded); the offset field must
    survive the serializer's strict-JSON numeric check."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return value == value and value not in (float("inf"), float("-inf"))
    except (TypeError, ValueError):
        return False


def validate_config(config):
    """Pure validation of a ds18b20 device config (no hardware): the keys in
    ``ALLOWED_CONFIG_KEYS`` with the documented bounds; raises
    ``DeviceValidationError`` (a ``ValueError``) with a stable ``code`` on the
    first violation. A valid definition with no physical backing passes --
    physical absence (the ROM not present on the bus) is an operational
    failure (``initialization_failed`` at boot), not a schema failure.
    Defaults are applied by the driver, not here."""
    if not isinstance(config, dict):
        raise DeviceValidationError(
            "ds18b20 device config must be an object",
            code="invalid_value",
        )

    unknown = sorted(set(config) - ALLOWED_CONFIG_KEYS)
    if unknown:
        raise DeviceValidationError(
            "ds18b20 device config contains unknown field(s): {}".format(
                ", ".join(unknown)
            ),
            code="unknown_config_fields",
        )

    # pin: required, an externally exposed GPIO (the 1-Wire timing is
    # software-driven, not fixed by a mux table; the board rule bounds it).
    if "pin" not in config:
        raise DeviceValidationError(
            "ds18b20 device config missing required key: pin",
            code="missing_key",
        )
    validate_user_gpio_pin("pin", config["pin"])

    # rom: required, 16 hex characters, family code 0x28 first -- a different
    # family decodes its temperature register differently and would read
    # silently wrong values, so the check is fail-fast, not courtesy.
    if "rom" not in config:
        raise DeviceValidationError(
            "ds18b20 device config missing required key: rom",
            code="missing_key",
        )
    rom = config["rom"]
    if not isinstance(rom, str) or len(rom) != _ROM_LENGTH:
        raise DeviceValidationError(
            "rom must be a 16-character hexadecimal string (the 64-bit ROM)",
            code="invalid_value",
        )
    if any(char not in _HEX_CHARS for char in rom):
        raise DeviceValidationError(
            "rom must be a 16-character hexadecimal string (the 64-bit ROM)",
            code="invalid_value",
        )
    if rom.lower()[:2] != _FAMILY_CODE:
        raise DeviceValidationError(
            "rom must start with the DS18B20 family code 28 (got {}...)"
            .format(rom[:2]),
            code="invalid_value",
        )

    # conversion_ms: optional, the conversion-completion wait (bounds and
    # rationale: see _MIN_CONVERSION_MS above).
    conversion_ms = config.get("conversion_ms", DEFAULT_CONVERSION_MS)
    if not _is_int(conversion_ms) or not (
        _MIN_CONVERSION_MS <= conversion_ms <= _MAX_CONVERSION_MS
    ):
        raise DeviceValidationError(
            "conversion_ms must be an integer {}-{} ({} is the 12-bit "
            "maximum conversion time; the driver does not configure a lower "
            "resolution)".format(
                _MIN_CONVERSION_MS, _MAX_CONVERSION_MS, _MIN_CONVERSION_MS
            ),
            code="invalid_value",
        )

    # offsets: optional; a flat object of the one converted channel, a finite
    # number within its bound (absent sub-keys default to 0 in the driver).
    offsets = config.get("offsets")
    if offsets is None:
        return
    if not isinstance(offsets, dict):
        raise DeviceValidationError(
            "offsets must be an object",
            code="invalid_value",
        )
    unknown_offsets = sorted(set(offsets) - set(_OFFSET_KEYS))
    if unknown_offsets:
        raise DeviceValidationError(
            "offsets contains unknown field(s): {}".format(", ".join(unknown_offsets)),
            code="unknown_config_fields",
        )
    for key, value in offsets.items():
        if not _is_finite_number(value):
            raise DeviceValidationError(
                "offsets.{} must be a finite number".format(key),
                code="invalid_value",
            )
        if abs(value) > _OFFSET_BOUNDS[key]:
            raise DeviceValidationError(
                "offsets.{} must be within +/-{}".format(
                    key, _OFFSET_BOUNDS[key]
                ),
                code="invalid_value",
            )
