# devices/bme280/validation.py - Pure BME280 config validation
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Authoritative pure validation for the ``bme280`` device config.
Kept host-importable (no ``machine``) so ``config.py``'s pure path, the
driver's ``initialize()``, and host tests all drive it. Single source of
truth for the config shape and the oversampling/filter bounds the driver
reads; the sensor protocol itself lives in ``bme280_device``."""

from devices.device import DeviceValidationError

# The complete set of keys a bme280 device config may contain. Anything beyond
# this is unknown and reported as a qualified path.
ALLOWED_CONFIG_KEYS = frozenset((
    "i2c_bus",
    "i2c_sda_pin",
    "i2c_scl_pin",
    "i2c_freq_hz",
    "i2c_address_candidates",
    "temperature_oversampling",
    "pressure_oversampling",
    "humidity_oversampling",
    "iir_filter",
    "sea_level_pressure_pa",
    "offsets",
))

# The offset sub-keys and their physical bounds (fail fast on unit typos such as
# a hPa value where Pa is expected).
_OFFSET_KEYS = ("temperature_c", "humidity_percent", "pressure_pascal")
_OFFSET_BOUNDS = {
    "temperature_c": 100.0,
    "humidity_percent": 100.0,
    "pressure_pascal": 200000.0,
}

# The BME280 only ever sits at 0x76 (118) or 0x77 (119); a candidate outside this
# set is a configuration error, not a runtime miss.
_VALID_I2C_ADDRESSES = (118, 119)

# Register/parameter bounds (see bme280_device for the wire meaning).
_MAX_I2C_BUS = 1
_MAX_GPIO = 29
DEFAULT_I2C_FREQ_HZ = 400000   # I2C fast mode; shared with the bus factory
_MIN_FREQ_HZ = 100000
_MAX_FREQ_HZ = 1000000
_MAX_OVERSAMPLING = 5
_MAX_IIR_FILTER = 4
_MIN_SEA_LEVEL_PA = 30000.0
_MAX_SEA_LEVEL_PA = 115000.0


def _is_int(value):
    """True for a real int (bool is an int subclass and is excluded)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_finite_number(value):
    """True for a finite int or float (bool excluded); the offset/sea-level
    fields must survive the serializer's strict-JSON numeric check."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return value == value and value not in (float("inf"), float("-inf"))
    except (TypeError, ValueError):
        return False


def validate_config(config):
    """Pure validation of a bme280 device config (no hardware): the keys in
    ``ALLOWED_CONFIG_KEYS`` with the documented bounds plus the cross-field
    rules; raises ``DeviceValidationError`` (a ``ValueError``) with a stable
    ``code`` on the first violation. A valid definition with no physical
    backing passes -- physical absence is an operational failure
    (``initialization_failed`` at boot), not a schema failure. Defaults are
    applied by the driver, not here."""
    if not isinstance(config, dict):
        raise DeviceValidationError(
            "bme280 device config must be an object",
            code="invalid_value",
        )

    unknown = sorted(set(config) - ALLOWED_CONFIG_KEYS)
    if unknown:
        raise DeviceValidationError(
            "bme280 device config contains unknown field(s): {}".format(
                ", ".join(unknown)
            ),
            code="unknown_config_fields",
        )

    # i2c_bus: required, one of the two RP2 I2C peripherals.
    if "i2c_bus" not in config:
        raise DeviceValidationError(
            "bme280 device config missing required key: i2c_bus",
            code="missing_key",
        )
    bus = config["i2c_bus"]
    if not _is_int(bus) or not 0 <= bus <= _MAX_I2C_BUS:
        raise DeviceValidationError(
            "i2c_bus must be an integer 0-{}".format(_MAX_I2C_BUS),
            code="invalid_value",
        )

    # i2c_sda_pin / i2c_scl_pin: optional explicit pins; must be distinct GPIOs.
    sda = config.get("i2c_sda_pin")
    scl = config.get("i2c_scl_pin")
    for key, value in (("i2c_sda_pin", sda), ("i2c_scl_pin", scl)):
        if value is None:
            continue
        if not _is_int(value) or not 0 <= value <= _MAX_GPIO:
            raise DeviceValidationError(
                "{} must be an integer 0-{}".format(key, _MAX_GPIO),
                code="invalid_value",
            )
    if sda is not None and sda == scl:
        raise DeviceValidationError(
            "i2c_sda_pin and i2c_scl_pin must be different pins",
            code="invalid_value",
        )

    # i2c_freq_hz: optional, a standard/fast/high-speed I2C clock.
    freq = config.get("i2c_freq_hz", DEFAULT_I2C_FREQ_HZ)
    if not _is_int(freq) or not _MIN_FREQ_HZ <= freq <= _MAX_FREQ_HZ:
        raise DeviceValidationError(
            "i2c_freq_hz must be an integer {}-{}".format(_MIN_FREQ_HZ, _MAX_FREQ_HZ),
            code="invalid_value",
        )

    # i2c_address_candidates: optional ordered probe list, each a valid BME280
    # address, no duplicates.
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

    # Oversampling (0..5) and IIR filter (0..4); defaults applied for the rule.
    osrs = {}
    for key in (
        "temperature_oversampling",
        "pressure_oversampling",
        "humidity_oversampling",
    ):
        value = config.get(key, 1)
        if not _is_int(value) or not 0 <= value <= _MAX_OVERSAMPLING:
            raise DeviceValidationError(
                "{} must be an integer 0-{}".format(key, _MAX_OVERSAMPLING),
                code="invalid_value",
            )
        osrs[key] = value

    iir_filter = config.get("iir_filter", 0)
    if not _is_int(iir_filter) or not 0 <= iir_filter <= _MAX_IIR_FILTER:
        raise DeviceValidationError(
            "iir_filter must be an integer 0-{}".format(_MAX_IIR_FILTER),
            code="invalid_value",
        )

    # A fresh pressure or humidity compensation needs the temperature channel
    # (its t_fine feeds both); skipping temperature while leaving one on is a
    # configuration error, not a silent stale-compensation bug.
    if (
        osrs["temperature_oversampling"] == 0
        and (
            osrs["pressure_oversampling"] != 0
            or osrs["humidity_oversampling"] != 0
        )
    ):
        raise DeviceValidationError(
            "temperature_oversampling is required when pressure or humidity "
            "oversampling is enabled",
            code="invalid_value",
        )

    # At least one channel must be enabled: an all-zero profile would trigger a
    # forced conversion that reads only sentinels and reports nothing.
    if (
        osrs["temperature_oversampling"] == 0
        and osrs["pressure_oversampling"] == 0
        and osrs["humidity_oversampling"] == 0
    ):
        raise DeviceValidationError(
            "at least one channel must be enabled (oversampling 0 skips it)",
            code="invalid_value",
        )

    # sea_level_pressure_pa: required, a finite pressure in the sensor's range
    # (bounds catch a hPa value entered where Pa is expected).
    if "sea_level_pressure_pa" not in config:
        raise DeviceValidationError(
            "bme280 device config missing required key: sea_level_pressure_pa",
            code="missing_key",
        )
    sea_level = config["sea_level_pressure_pa"]
    if not _is_finite_number(sea_level) or not (
        _MIN_SEA_LEVEL_PA <= sea_level <= _MAX_SEA_LEVEL_PA
    ):
        raise DeviceValidationError(
            "sea_level_pressure_pa must be a finite number {}-{}".format(
                _MIN_SEA_LEVEL_PA, _MAX_SEA_LEVEL_PA
            ),
            code="invalid_value",
        )

    # offsets: optional; a flat object of the three physical channels, each a
    # finite number within its bound (absent sub-keys default to 0 in the driver).
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
