# devices/ltr390/validation.py - Pure LTR390 config validation
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Authoritative pure validation for the ``ltr390`` device config.
Kept host-importable (no ``machine``) so ``config.py``'s pure path, the
driver's ``initialize()``, and host tests all drive it. Single source of
truth for the config shape and the gain/resolution/rate/window bounds the
driver reads; the sensor protocol itself lives in ``ltr390_device``.

The LTR390 has a fixed 7-bit I2C address (0x53 / 83 -- no address-select
pin), so there is no address key in the config: a wrong-bus or miswired
sensor is an operational failure at ``initialize()``, not a schema choice.
"""

from devices.device import DeviceValidationError

# The complete set of keys a ltr390 device config may contain. Anything beyond
# this is unknown and reported as a qualified path.
ALLOWED_CONFIG_KEYS = frozenset((
    "i2c_bus",
    "i2c_sda_pin",
    "i2c_scl_pin",
    "i2c_freq_hz",
    "gain",
    "resolution_bits",
    "measurement_rate_ms",
    "window_factor",
    "offsets",
))

# The offset sub-keys and their physical bounds (fail fast on unit typos such
# as a raw-count value where lux is expected).
_OFFSET_KEYS = ("lux", "uv_index")
_OFFSET_BOUNDS = {
    "lux": 50000.0,
    "uv_index": 12.0,
}

# Register/parameter bounds (see ltr390_device for the wire meaning). Membership
# sets, not ranges: the reserved ADC/gain codes are configuration errors.
_VALID_GAINS = (1, 3, 6, 9, 18)
_VALID_RESOLUTIONS = (13, 16, 17, 18, 19, 20)
_VALID_MEASUREMENT_RATES = (25, 50, 100, 200, 500, 1000, 2000)
DEFAULT_GAIN = 3
DEFAULT_RESOLUTION_BITS = 18
DEFAULT_MEASUREMENT_RATE_MS = 200

# resolution_bits -> ADC conversion time (ms). A measurement period faster than
# the conversion of the selected resolution is not physically achievable, so
# the two are a cross-field pair.
_CONVERSION_TIME_MS = {
    20: 400,
    19: 200,
    18: 100,
    17: 50,
    16: 25,
    13: 12.5,
}

_MAX_I2C_BUS = 1
_MAX_GPIO = 29
DEFAULT_I2C_FREQ_HZ = 400000   # I2C fast mode; shared with the bus factory
_MIN_FREQ_HZ = 100000
_MAX_FREQ_HZ = 1000000
_MIN_WINDOW_FACTOR = 1.0
_MAX_WINDOW_FACTOR = 100.0
DEFAULT_WINDOW_FACTOR = 1.0


def _is_int(value):
    """True for a real int (bool is an int subclass and is excluded)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_finite_number(value):
    """True for a finite int or float (bool excluded); the offset/window-factor
    fields must survive the serializer's strict-JSON numeric check."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return value == value and value not in (float("inf"), float("-inf"))
    except (TypeError, ValueError):
        return False


def validate_config(config):
    """Pure validation of a ltr390 device config (no hardware): the keys in
    ``ALLOWED_CONFIG_KEYS`` with the documented bounds plus the timing
    cross-field rule; raises ``DeviceValidationError`` (a ``ValueError``)
    with a stable ``code`` on the first violation. A valid definition with no
    physical backing passes -- physical absence is an operational failure
    (``initialization_failed`` at boot), not a schema failure. Defaults are
    applied by the driver, not here."""
    if not isinstance(config, dict):
        raise DeviceValidationError(
            "ltr390 device config must be an object",
            code="invalid_value",
        )

    unknown = sorted(set(config) - ALLOWED_CONFIG_KEYS)
    if unknown:
        raise DeviceValidationError(
            "ltr390 device config contains unknown field(s): {}".format(
                ", ".join(unknown)
            ),
            code="unknown_config_fields",
        )

    # i2c_bus: required, one of the two RP2 I2C peripherals.
    if "i2c_bus" not in config:
        raise DeviceValidationError(
            "ltr390 device config missing required key: i2c_bus",
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

    # Gain / resolution / measurement rate: membership in the valid sets (the
    # register codes 5-7 for gain, 6-7 for resolution, and the duplicate 2000 ms
    # rate code are reserved, so a plain range check would accept garbage).
    # Defaults are applied for the cross-field rule, mirroring the driver.
    gain = config.get("gain", DEFAULT_GAIN)
    if not _is_int(gain) or gain not in _VALID_GAINS:
        raise DeviceValidationError(
            "gain must be one of {}".format(_VALID_GAINS),
            code="invalid_value",
        )

    resolution = config.get("resolution_bits", DEFAULT_RESOLUTION_BITS)
    if not _is_int(resolution) or resolution not in _VALID_RESOLUTIONS:
        raise DeviceValidationError(
            "resolution_bits must be one of {}".format(_VALID_RESOLUTIONS),
            code="invalid_value",
        )

    rate = config.get("measurement_rate_ms", DEFAULT_MEASUREMENT_RATE_MS)
    if not _is_int(rate) or rate not in _VALID_MEASUREMENT_RATES:
        raise DeviceValidationError(
            "measurement_rate_ms must be one of {}".format(_VALID_MEASUREMENT_RATES),
            code="invalid_value",
        )

    # Cross-field timing rule: the sensor cannot produce a conversion more
    # frequently than its ADC integration allows, so a period shorter than the
    # selected resolution's conversion time is a configuration error, not
    # undocumented timing behavior.
    if rate < _CONVERSION_TIME_MS[resolution]:
        raise DeviceValidationError(
            "measurement_rate_ms must be at least {} (the {}-bit ADC "
            "conversion time)".format(_CONVERSION_TIME_MS[resolution], resolution),
            code="invalid_value",
        )

    # window_factor: optional, an attenuation compensation >= 1.0 (a factor
    # below 1.0 would amplify a measurement, which no optical window does).
    window_factor = config.get("window_factor", DEFAULT_WINDOW_FACTOR)
    if not _is_finite_number(window_factor) or not (
        _MIN_WINDOW_FACTOR <= window_factor <= _MAX_WINDOW_FACTOR
    ):
        raise DeviceValidationError(
            "window_factor must be a finite number {}-{}".format(
                _MIN_WINDOW_FACTOR, _MAX_WINDOW_FACTOR
            ),
            code="invalid_value",
        )

    # offsets: optional; a flat object of the two converted channels, each a
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
