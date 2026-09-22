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
device failure. The routable set is the mux group further intersected with the
externally exposed Pico W / Pico 2 W pins (``devices/rp2_pins``): the boards
reserve four of the silicon's GPIOs for the CYW43 wireless subsystem, so a
reserved pin is a configuration error even though the mux routes it.
Host-importable (no ``machine``) so ``config.py``'s pure path, the drivers'
``initialize()``, and the host tests share one set of rules.
"""

from devices.device import DeviceValidationError
from devices.rp2_pins import USER_GPIO_PINS, validate_user_gpio_pin

# The RP2's I2C mux table itself: each controller's SDA and SCL occupy one
# GPIO group of four (SDA even / SCL odd within the group), matching the
# port's ``((pin & 2) >> 1) == bus`` SDA/SCL check. The silicon's groups list
# GP23/24/25/29, but the Pico W / Pico 2 W boards reserve those for the CYW43
# and do not expose them (``rp2_pins``), so the routable sets are the groups
# intersected with the externally exposed pins -- derived, not restated.
_USER_GPIO_SET = frozenset(USER_GPIO_PINS)
_I2C_SDA_MUX = {
    0: (0, 4, 8, 12, 16, 20, 24, 28),
    1: (2, 6, 10, 14, 18, 22, 26),
}
_I2C_SCL_MUX = {
    0: (1, 5, 9, 13, 17, 21, 25, 29),
    1: (3, 7, 11, 15, 19, 23, 27),
}
_I2C_SDA_PINS = {
    bus: tuple(pin for pin in mux if pin in _USER_GPIO_SET)
    for bus, mux in _I2C_SDA_MUX.items()
}
_I2C_SCL_PINS = {
    bus: tuple(pin for pin in mux if pin in _USER_GPIO_SET)
    for bus, mux in _I2C_SCL_MUX.items()
}

# The port's default (sda, scl) when a config omits the pin args. On the
# supported Pico W / Pico 2 W boards machine.I2C(bus) with no pins routes to
# the Pico SDK's PICO_DEFAULT_I2C pair (controller 0: SDA GP4 / SCL GP5) for
# I2C0 and to the port's hardcoded I2C1 pair (SDA GP6 / SCL GP7) for I2C1
# (ports/rp2/machine_i2c.h, v1.28). Used only to resolve an omitted pin to the
# physical GPIO it drives so the cross-protocol GPIO-overlap check sees the
# line the runtime actually drives; it does not touch the same-bus identity
# rule, where an omitted pin stays distinct from an explicit one.
_I2C_DEFAULT_PINS = {
    0: (4, 5),
    1: (6, 7),
}


def _pin_list(pins):
    """The pin group rendered as ``"a, b, c"`` for a message."""
    return ", ".join(str(pin) for pin in pins)


def effective_rp2_i2c_pins(bus, sda, scl):
    """The physical (sda, scl) an I2C device drives, resolving any omitted pin
    to the port default for its bus (``_I2C_DEFAULT_PINS``). Pure and
    allocation-light; ``bus`` must already be a validated 0/1. An omitted pin
    is not routing knowledge for the same-bus identity rule (which keeps it
    ``None``); it resolves here only for the cross-protocol GPIO-overlap check,
    where the runtime still drives the default line even when the config names
    no pin."""
    default_sda, default_scl = _I2C_DEFAULT_PINS[bus]
    return (
        sda if sda is not None else default_sda,
        scl if scl is not None else default_scl,
    )


def validate_rp2_i2c_pins(bus, sda, scl):
    """Pure validation of one device's I2C pin routing (no hardware).

    ``bus`` must already be a validated 0/1 int (the caller enforces the bus
    key); the optional ``sda`` / ``scl`` pins (``None`` = rely on the
    controller's port-default pins) are each checked against the shared
    board rule (a real int naming an externally exposed Pico W / Pico 2 W
    GPIO, the wireless-reserved pins rejected), are required to differ from
    one another when both are set, and must each be a member of the selected
    controller's SDA / SCL pin group. Raises ``DeviceValidationError``
    (``code`` ``invalid_value``) on the first violation; returns ``None``
    when the routing is valid.

    A ``None`` pin skips the routing check: the port's default pins
    (``_I2C_DEFAULT_PINS``, each a member of its own controller's SDA/SCL mux
    group) always route to their own controller, so ``machine.I2C(bus)`` with
    no pin args can never fail the port's own check.
    """
    for key, value in (("i2c_sda_pin", sda), ("i2c_scl_pin", scl)):
        if value is None:
            continue
        validate_user_gpio_pin(key, value)
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
