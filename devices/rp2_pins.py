# devices/rp2_pins.py - Shared Pico W / Pico 2 W board GPIO rule
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Shared pure rule for which GPIOs the Pico W / Pico 2 W boards expose.

The RP2040/RP2350 silicon has 30 user GPIOs (GP0-GP29), but the boards do
not bring out all of them: the Pico 2 W board guide documents GP23
(wireless power-on), GP24 (wireless SPI data/IRQ), GP25 (wireless SPI
chip-select) and GP29 (wireless SPI clock / VSYS ADC) as the CYW43
allocation, and the Pico W SDK board definition makes the same reservation —
the boards externally expose GP0-GP22 and GP26-GP28. A config naming a
reserved pin passes a pure silicon-mux or range check yet points at a pad
that is not wired for external use and is driven by the radio; and because
config is writable remotely and applied at reboot, such a configuration can
silently lose the device's network connectivity. So the reservation is a
deterministic configuration error rejected at the config boundary, never an
operational device failure.

Single source of the rule shared by every pin-carrying validator (the
1-Wire data line, the I2C SDA/SCL routing): host-importable (no
``machine``) so ``config.py``'s pure path, the drivers' ``initialize()``,
and the host tests share one set.
"""

from devices.device import DeviceValidationError

# The externally exposed GPIOs of the Pico W / Pico 2 W: the silicon's
# GP0-GP29 minus the four wireless-reserved pins (GP23/24/25/29 above).
USER_GPIO_PINS = (
    0, 1, 2, 3, 4, 5, 6, 7,
    8, 9, 10, 11, 12, 13, 14, 15,
    16, 17, 18, 19, 20, 21, 22,
    26, 27, 28,
)

# The CYW43 allocation the boards do not expose: the pin-to-function mapping
# kept with the rule so a rejection message can name what the pad does.
WIRELESS_RESERVED_PINS = (23, 24, 25, 29)


def _is_int(value):
    """True for a real int (bool is an int subclass and is excluded)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _pin_list(pins):
    """The pin set rendered as ``"a, b, c"`` for a message."""
    return ", ".join(str(pin) for pin in pins)


def validate_user_gpio_pin(key, pin):
    """Shared pure check that one config pin names an externally exposed
    GPIO (no hardware). ``key`` is the config key the message refers to
    (``pin``, ``i2c_sda_pin``, ``i2c_scl_pin``); ``pin`` must already be
    required by the caller. A non-int (bool excluded), a wireless-reserved
    pin (rejected with the CYW43 function of the pad named), and any other
    out-of-board pin (negative, GP30+, or otherwise not exposed) each raise
    ``DeviceValidationError`` (``code`` ``invalid_value``) on the first
    violation; returns ``None`` when the pin is exposed."""
    if not _is_int(pin):
        raise DeviceValidationError(
            "{} must be an integer GPIO".format(key),
            code="invalid_value",
        )
    if pin in WIRELESS_RESERVED_PINS:
        raise DeviceValidationError(
            "{} {} is reserved by the board's wireless subsystem (GP23 "
            "wireless power-on, GP24 wireless SPI data/IRQ, GP25 wireless "
            "SPI chip-select, GP29 wireless SPI clock / VSYS ADC); "
            "externally exposed GPIOs are {}".format(
                key, pin, _pin_list(USER_GPIO_PINS)
            ),
            code="invalid_value",
        )
    if pin not in USER_GPIO_PINS:
        raise DeviceValidationError(
            "{} must be an externally exposed GPIO: {}".format(
                key, _pin_list(USER_GPIO_PINS)
            ),
            code="invalid_value",
        )
    return None
