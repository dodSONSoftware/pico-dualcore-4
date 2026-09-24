# devices/yl69_fc28/validation.py - Pure YL-69 / FC-28 config validation
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Authoritative pure validation for the ``yl69_fc28`` device config.
Kept host-importable (no ``machine``) so ``config.py``'s pure path, the
driver's ``initialize()``, and host tests all drive it. Single source of
truth for the config shape and the pin/filter/calibration bounds the driver
reads; the measurement cycle itself lives in ``yl69_fc28_device``.

The sensor has no chip identity to name in the config (no I2C address, no
1-Wire ROM): the probe is a plain resistive divider on an LM393 comparator
board, so the config is the wiring plus the installation-specific
calibration. ``dry_raw`` and ``wet_raw`` are therefore REQUIRED -- the
published value is a percentage of this installation's measured dry-to-wet
span, and a config without that span would publish a number with no
moisture meaning (the guide's primary rule: never treat a raw reading as
an absolute water percentage).
"""

from devices.device import DeviceValidationError
from devices.rp2_pins import validate_user_gpio_pin

# The complete set of keys a yl69_fc28 device config may contain. Anything
# beyond this is unknown and reported as a qualified path.
ALLOWED_CONFIG_KEYS = frozenset((
    "adc_pin",
    "digital_pin",
    "power_pin",
    "power_active_low",
    "settle_ms",
    "sample_count",
    "sample_delay_ms",
    "dry_raw",
    "wet_raw",
))

# The RP2's three ADC input channels are on GP26/GP27/GP28, and all three
# are externally exposed on the Pico W / Pico 2 W -- so the ADC-capable set
# is exactly this triple (the board rule in rp2_pins is subsumed by
# membership).
ADC_CAPABLE_PINS = (26, 27, 28)

# Power-settle window (ms) after the switched power comes on. 200 ms is the
# guide's conservative start (the module's actual requirement is much
# shorter, but the window costs <0.5% duty at minute intervals). The ceiling
# is a liveness bound, not a sensor one: the settle is one uninterrupted
# sleep inside read() that does not refresh Core 1's liveness stamp, so a
# value anywhere near Core 0's 30 s Core 1 staleness timeout would be
# schema-legal yet reset the device after every read, including after a
# remotely written config.
DEFAULT_SETTLE_MS = 200
_MAX_SETTLE_MS = 2000

# Median filter: an odd count (the median is the middle sample; an even
# count has no single middle). 9 at 2 ms is the guide's recommended default
# (a few tens of milliseconds). The ceiling keeps the sampling window
# bounded against the same liveness budget: at the maximum delay below,
# 101 samples cost at most ~5 s of sleeps, plus the settle ceiling, leaving
# the worst-case read well under the 30 s Core 1 staleness timeout.
DEFAULT_SAMPLE_COUNT = 9
_MAX_SAMPLE_COUNT = 101

# Inter-sample gap (ms). Tight: it multiplies by sample_count inside the
# read, so a large value would stretch the read past the liveness budget
# (101 samples at a 2 s delay would be ~3.5 minutes of sleeps in one read).
DEFAULT_SAMPLE_DELAY_MS = 2
_MAX_SAMPLE_DELAY_MS = 50

# Calibration values live in the ADC's own units: read_u16() returns
# 0-65535 (MicroPython scales the 12-bit result into the 16-bit range), and
# the dry/wet points are captured with that same API, so the bounds are the
# API's range, not the silicon's 12-bit range.
_RAW_MIN = 0
_RAW_MAX = 65535


def _is_int(value):
    """True for a real int (bool is an int subclass and is excluded)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_raw_calibration(key, config):
    """One calibration endpoint: a required int in read_u16() range."""
    if key not in config:
        raise DeviceValidationError(
            "yl69_fc28 device config missing required key: {}".format(key),
            code="missing_key",
        )
    value = config[key]
    if not _is_int(value) or not (_RAW_MIN <= value <= _RAW_MAX):
        raise DeviceValidationError(
            "{} must be an integer {}-{} (the ADC read_u16() range; capture "
            "the point with the same API)".format(key, _RAW_MIN, _RAW_MAX),
            code="invalid_value",
        )


def _reject_pin_overlap(config, first_key, second_key):
    """Within-device rule: each of the three pins carries a distinct
    physical signal (AO / DO / power-switch control), so no two of them may
    name the same GPIO. Runs after both keys are validated, so the values
    are already known-good ints."""
    first = config[first_key]
    second = config[second_key]
    if first == second:
        raise DeviceValidationError(
            "{} ({}) and {} ({}) must be distinct GPIOs: one GPIO cannot "
            "carry two signals".format(first_key, first, second_key, second),
            code="invalid_value",
        )


def validate_config(config):
    """Pure validation of a yl69_fc28 device config (no hardware): the keys
    in ``ALLOWED_CONFIG_KEYS`` with the documented bounds; raises
    ``DeviceValidationError`` (a ``ValueError``) with a stable ``code`` on
    the first violation. A valid definition with no physical backing passes
    -- there is no chip to probe, so a wiring fault surfaces later as an
    implausible reading, not a schema or initialization failure. Defaults
    are applied by the driver, not here."""
    if not isinstance(config, dict):
        raise DeviceValidationError(
            "yl69_fc28 device config must be an object",
            code="invalid_value",
        )

    unknown = sorted(set(config) - ALLOWED_CONFIG_KEYS)
    if unknown:
        raise DeviceValidationError(
            "yl69_fc28 device config contains unknown field(s): {}".format(
                ", ".join(unknown)
            ),
            code="unknown_config_fields",
        )

    # adc_pin: required, one of the board's three ADC-capable GPIOs.
    if "adc_pin" not in config:
        raise DeviceValidationError(
            "yl69_fc28 device config missing required key: adc_pin",
            code="missing_key",
        )
    adc_pin = config["adc_pin"]
    # A real int (bool excluded): the membership test alone would accept
    # 26.0, since 26.0 == 26.
    if not isinstance(adc_pin, int) or isinstance(adc_pin, bool) \
            or adc_pin not in ADC_CAPABLE_PINS:
        raise DeviceValidationError(
            "adc_pin must be one of {} (the Pico W / Pico 2 W's "
            "ADC-capable GPIOs; every other GPIO has no ADC input)"
            .format(", ".join(str(pin) for pin in ADC_CAPABLE_PINS)),
            code="invalid_value",
        )

    # digital_pin / power_pin: optional, externally exposed GPIOs (the
    # shared board rule rejects the four wireless-reserved pins, a
    # "valid" config that loses network connectivity after a reboot).
    if "digital_pin" in config:
        validate_user_gpio_pin("digital_pin", config["digital_pin"])
    if "power_pin" in config:
        validate_user_gpio_pin("power_pin", config["power_pin"])

    # Within-device distinctness (each pin is a distinct physical signal).
    if "digital_pin" in config:
        _reject_pin_overlap(config, "adc_pin", "digital_pin")
    if "power_pin" in config:
        _reject_pin_overlap(config, "adc_pin", "power_pin")
        if "digital_pin" in config:
            _reject_pin_overlap(config, "digital_pin", "power_pin")

    # power_active_low: optional, a real bool (1/0 are ints, not
    # polarities). The guide's P-channel arrangement is active-low by
    # construction; the key exists so a load-switch or N-channel variant
    # can be wired without a code change.
    power_active_low = config.get("power_active_low", True)
    if not isinstance(power_active_low, bool):
        raise DeviceValidationError(
            "power_active_low must be a boolean",
            code="invalid_value",
        )

    # settle_ms: optional, bounds and rationale above.
    settle_ms = config.get("settle_ms", DEFAULT_SETTLE_MS)
    if not _is_int(settle_ms) or not 0 <= settle_ms <= _MAX_SETTLE_MS:
        raise DeviceValidationError(
            "settle_ms must be an integer 0-{}".format(_MAX_SETTLE_MS),
            code="invalid_value",
        )

    # sample_count: optional, a positive odd integer (the median is the
    # middle sample).
    sample_count = config.get("sample_count", DEFAULT_SAMPLE_COUNT)
    if (
        not _is_int(sample_count)
        or not 1 <= sample_count <= _MAX_SAMPLE_COUNT
        or sample_count % 2 == 0
    ):
        raise DeviceValidationError(
            "sample_count must be an odd integer 1-{} (the median is the "
            "middle sample)".format(_MAX_SAMPLE_COUNT),
            code="invalid_value",
        )

    # sample_delay_ms: optional, the inter-sample gap (multiplied by
    # sample_count inside the read, hence the tight ceiling).
    sample_delay_ms = config.get("sample_delay_ms", DEFAULT_SAMPLE_DELAY_MS)
    if not _is_int(sample_delay_ms) or not 0 <= sample_delay_ms <= _MAX_SAMPLE_DELAY_MS:
        raise DeviceValidationError(
            "sample_delay_ms must be an integer 0-{}".format(_MAX_SAMPLE_DELAY_MS),
            code="invalid_value",
        )

    # dry_raw / wet_raw: required (the percentage is meaningless without
    # both points), in the read_u16() range, and distinct -- a zero span
    # would make the interpolation divide by nothing useful (every reading
    # would clamp to an endpoint).
    _validate_raw_calibration("dry_raw", config)
    _validate_raw_calibration("wet_raw", config)
    if config["dry_raw"] == config["wet_raw"]:
        raise DeviceValidationError(
            "dry_raw and wet_raw must differ (a zero calibration span "
            "cannot map readings to a moisture scale; recapture the "
            "points in more different conditions)",
            code="invalid_value",
        )
