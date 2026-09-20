# devices/rp2_i2c.py - Shared RP2 I2C pin-routing validation
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Shared pure validation of the RP2 I2C pin routing.

The I2C sensor validators (``bme280``, ``ltr390``) each carry the same
``i2c_bus`` / ``i2c_sda_pin`` / ``i2c_scl_pin`` keys, and on the RP2 the
controllers' pins are fixed by the GPIO mux table. The port enforces exactly
this mapping at ``machine.I2C(...)`` construction, so a config that passes the
pure validator must also be routable there: a deterministic pin-routing error
is a configuration error rejected at the config boundary, never an operational
device failure. Host-importable (no ``machine``) so ``config.py``'s pure path,
the drivers' ``initialize()``, and the host tests share one set of rules.

Board-specific pin reservations (e.g. a GPIO the Pico W hands to the CYW43
radio or its flash) are outside this validator: the routing is board-agnostic
and the pure config path does not branch on the detected board.
"""

from devices.device import DeviceValidationError

# The user GPIOs on the Pico W / Pico 2 W (GP0-GP29). The silicon's I2C1 mux
# also lists GP30/GP31, but those are not user GPIOs, so the sets below are the
# routable pins: the controller's SDA/SCL group intersected with 0-29. The four
# groups are the mux table itself -- each controller's SDA and SCL occupy one
# GPIO group of four (SDA even / SCL odd within the group), matching the port's
# ``((pin & 2) >> 1) == bus`` SDA/SCL check.
_MAX_GPIO = 29
_I2C_SDA_PINS = {
    0: (0, 4, 8, 12, 16, 20, 24, 28),
    1: (2, 6, 10, 14, 18, 22, 26),
}
_I2C_SCL_PINS = {
    0: (1, 5, 9, 13, 17, 21, 25, 29),
    1: (3, 7, 11, 15, 19, 23, 27),
}


def _is_int(value):
    """True for a real int (bool is an int subclass and is excluded)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _pin_list(pins):
    """The pin group rendered as ``"a, b, c"`` for a message."""
    return ", ".join(str(pin) for pin in pins)


def validate_rp2_i2c_pins(bus, sda, scl):
    """Pure validation of one device's I2C pin routing (no hardware).

    ``bus`` must already be a validated 0/1 int (the caller enforces the bus
    key); the optional ``sda`` / ``scl`` pins (``None`` = rely on the
    controller's port-default pins) are each checked to be a 0-29 GPIO, are
    required to differ from one another when both are set, and must each be a
    member of the selected controller's SDA / SCL pin group. Raises
    ``DeviceValidationError`` (``code`` ``invalid_value``) on the first
    violation; returns ``None`` when the routing is valid.

    A ``None`` pin skips the routing check: the port's default pins (hardcoded
    routable pairs, I2C0 8/9, I2C1 6/7) always route to their own controller,
    so ``machine.I2C(bus)`` with no pin args can never fail the port's own
    check.
    """
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
    if sda is not None and sda not in _I2C_SDA_PINS[bus]:
        raise DeviceValidationError(
            "i2c_sda_pin {} is not a valid SDA pin for i2c_bus {} "
            "(valid SDA pins: {})".format(sda, bus, _pin_list(_I2C_SDA_PINS[bus])),
            code="invalid_value",
        )
    if scl is not None and scl not in _I2C_SCL_PINS[bus]:
        raise DeviceValidationError(
            "i2c_scl_pin {} is not a valid SCL pin for i2c_bus {} "
            "(valid SCL pins: {})".format(scl, bus, _pin_list(_I2C_SCL_PINS[bus])),
            code="invalid_value",
        )
    return None
