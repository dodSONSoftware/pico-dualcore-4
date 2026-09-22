# devices/sht35/validation.py - Pure SHT35 config validation
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Authoritative pure validation for the ``sht35`` device config.
Kept host-importable (no ``machine``) so ``config.py``'s pure path, the
driver's ``initialize()``, and host tests all drive it. Single source of
truth for the config shape and the address/repeatability bounds the driver
reads; the sensor protocol itself lives in ``sht35_device``."""

from devices.device import DeviceValidationError
from devices.rp2_i2c import validate_rp2_i2c_pins

# The complete set of keys a sht35 device config may contain. Anything beyond
# this is unknown and reported as a qualified path.
ALLOWED_CONFIG_KEYS = frozenset((
    "i2c_bus",
    "i2c_sda_pin",
    "i2c_scl_pin",
    "i2c_freq_hz",
    "i2c_address_candidates",
    "repeatability",
    "offsets",
))

# The offset sub-keys and their physical bounds (fail fast on unit typos such
# as a raw-count value where Celsius is expected).
_OFFSET_KEYS = ("temperature_c", "humidity_percent")
_OFFSET_BOUNDS = {
    "temperature_c": 100.0,
    "humidity_percent": 100.0,
}

# The SHT3x-DIS offers exactly two 7-bit addresses (ADDR strap low = 0x44,
# high = 0x45). The default probe list is the same two addresses -- the
# driver tries them in order and binds the first CRC-valid responder -- and
# any configured candidate must be one of them too, so both share this
# single source of truth (config.py's cross-device check and the driver read
# the default from here).
DEFAULT_I2C_ADDRESS_CANDIDATES = (68, 69)
_VALID_I2C_ADDRESSES = DEFAULT_I2C_ADDRESS_CANDIDATES

# Register/parameter bounds (see sht35_device for the wire meaning).
_MAX_I2C_BUS = 1
DEFAULT_I2C_FREQ_HZ = 400000   # I2C fast mode; shared with the bus factory
_MIN_FREQ_HZ = 100000
# The SHT3x-DIS specifies I2C clocking up to 1 MHz, so the device cap is the
# RP2 controller's own ceiling (the ltr390 caps below it at fast mode).
_MAX_FREQ_HZ = 1000000

# Membership set, not a range: the SHT3x-DIS defines exactly these three
# single-shot repeatability levels. "high" is the guide's recommended default
# (the extra conversion time is on the order of milliseconds).
_VALID_REPEATABILITIES = ("high", "medium", "low")
DEFAULT_REPEATABILITY = "high"


def _is_int(value):
    """True for a real int (bool is an int subclass and is excluded)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_finite_number(value):
    """True for a finite int or float (bool excluded); the offset fields must
    survive the serializer's strict-JSON numeric check."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return value == value and value not in (float("inf"), float("-inf"))
    except (TypeError, ValueError):
        return False


def validate_config(config):
    """Pure validation of a sht35 device config (no hardware): the keys in
    ``ALLOWED_CONFIG_KEYS`` with the documented bounds; raises
    ``DeviceValidationError`` (a ``ValueError``) with a stable ``code`` on the
    first violation. A valid definition with no physical backing passes --
    physical absence is an operational failure (``initialization_failed`` at
    boot), not a schema failure. Defaults are applied by the driver, not
    here."""
    if not isinstance(config, dict):
        raise DeviceValidationError(
            "sht35 device config must be an object",
            code="invalid_value",
        )

    unknown = sorted(set(config) - ALLOWED_CONFIG_KEYS)
    if unknown:
        raise DeviceValidationError(
            "sht35 device config contains unknown field(s): {}".format(
                ", ".join(unknown)
            ),
            code="unknown_config_fields",
        )

    # i2c_bus: required, one of the two RP2 I2C peripherals.
    if "i2c_bus" not in config:
        raise DeviceValidationError(
            "sht35 device config missing required key: i2c_bus",
            code="missing_key",
        )
    bus = config["i2c_bus"]
    if not _is_int(bus) or not 0 <= bus <= _MAX_I2C_BUS:
        raise DeviceValidationError(
            "i2c_bus must be an integer 0-{}".format(_MAX_I2C_BUS),
            code="invalid_value",
        )

    # i2c_sda_pin / i2c_scl_pin: optional explicit pins; the shared RP2 routing
    # validator checks type, range, distinctness, and membership in the
    # selected controller's SDA/SCL group (a pin the RP2 mux cannot route to
    # this controller is a configuration error, not an operational one).
    validate_rp2_i2c_pins(
        bus, config.get("i2c_sda_pin"), config.get("i2c_scl_pin")
    )

    # i2c_freq_hz: optional, a standard/fast/high-speed I2C clock (the
    # SHT3x-DIS specifies up to 1 MHz).
    freq = config.get("i2c_freq_hz", DEFAULT_I2C_FREQ_HZ)
    if not _is_int(freq) or not _MIN_FREQ_HZ <= freq <= _MAX_FREQ_HZ:
        raise DeviceValidationError(
            "i2c_freq_hz must be an integer {}-{}".format(_MIN_FREQ_HZ, _MAX_FREQ_HZ),
            code="invalid_value",
        )

    # i2c_address_candidates: optional ordered probe list, each one of the
    # two SHT3x-DIS addresses, no duplicates.
    candidates = config.get("i2c_address_candidates")
    if candidates is None:
        pass
    else:
        if not isinstance(candidates, list) or not candidates:
            raise DeviceValidationError(
                "i2c_address_candidates must be a non-empty list",
                code="invalid_value",
            )
        seen = set()
        for entry in candidates:
            if not _is_int(entry) or entry not in _VALID_I2C_ADDRESSES:
                raise DeviceValidationError(
                    "i2c_address_candidates entries must be one of {}".format(
                        _VALID_I2C_ADDRESSES
                    ),
                    code="invalid_value",
                )
            if entry in seen:
                raise DeviceValidationError(
                    "i2c_address_candidates contains duplicate address: {}".format(entry),
                    code="invalid_value",
                )
            seen.add(entry)

    # repeatability: optional, membership in the three single-shot levels.
    # Strings only (a boolean or number is not a repeatability level).
    repeatability = config.get("repeatability", DEFAULT_REPEATABILITY)
    if not isinstance(repeatability, str) or repeatability not in _VALID_REPEATABILITIES:
        raise DeviceValidationError(
            "repeatability must be one of {}".format(_VALID_REPEATABILITIES),
            code="invalid_value",
        )

    # offsets: optional; a flat object of the two channels, each a finite
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
