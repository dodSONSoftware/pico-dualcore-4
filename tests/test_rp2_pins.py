# test_rp2_pins.py - Shared Pico W / Pico 2 W board GPIO rule
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Host-side tests for the shared board GPIO rule (``devices/rp2_pins.py``).

``USER_GPIO_PINS`` / ``WIRELESS_RESERVED_PINS`` are the single source of the
rule every pin-carrying validator shares: the Pico W / Pico 2 W externally
expose GP0-GP22 and GP26-GP28, and the four wireless-reserved pins (GP23
power-on, GP24 SPI data/IRQ, GP25 SPI chip-select, GP29 SPI clock / VSYS ADC)
are the CYW43 allocation the boards do not bring out. The expected sets below
are the board documentation themselves (an independent oracle, not a copy of
the module under test), so a drift in the pin rule fails here. Nothing touches
hardware -- the import chain reaches no ``machine`` module, so it runs on
CPython."""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from devices.device import DeviceValidationError  # noqa: E402
from devices.rp2_pins import (  # noqa: E402
    USER_GPIO_PINS,
    WIRELESS_RESERVED_PINS,
    validate_user_gpio_pin,
)


# The Pico W / Pico 2 W board exposure: the silicon's GP0-GP29 minus the
# CYW43 allocation. GP23/24/25/29 are wired to the wireless subsystem and not
# brought to the header; GP26 (ADC), 27, 28 are exposed.
EXPECTED_EXPOSED = frozenset((0, 1, 2, 3, 4, 5, 6, 7,
                              8, 9, 10, 11, 12, 13, 14, 15,
                              16, 17, 18, 19, 20, 21, 22,
                              26, 27, 28))
EXPECTED_RESERVED = frozenset((23, 24, 25, 29))


# --- The sets ---------------------------------------------------------------


def test_user_pins_are_exactly_the_board_exposed_gpios():
    assert set(USER_GPIO_PINS) == EXPECTED_EXPOSED
    # No duplicates in the source tuple (a duplicate would hide a wrong set
    # of the same cardinality).
    assert len(USER_GPIO_PINS) == len(EXPECTED_EXPOSED)


def test_reserved_pins_are_exactly_the_wireless_allocation():
    assert set(WIRELESS_RESERVED_PINS) == EXPECTED_RESERVED


def test_exposed_and_reserved_partition_the_silicon_gpios():
    # Together the two sets are the silicon's 30 user GPIOs, with no pin in
    # both: a pin the board does not expose is reserved, and nothing is left
    # in a gap.
    assert EXPECTED_EXPOSED | EXPECTED_RESERVED == frozenset(range(30))
    assert EXPECTED_EXPOSED & EXPECTED_RESERVED == frozenset()


# --- Accept: every exposed pin ----------------------------------------------


@pytest.mark.parametrize("pin", sorted(EXPECTED_EXPOSED))
def test_every_exposed_pin_is_accepted(pin):
    assert validate_user_gpio_pin("pin", pin) is None, pin


# --- Reject: wireless-reserved pins ------------------------------------------


@pytest.mark.parametrize("reserved", sorted(EXPECTED_RESERVED))
def test_wireless_reserved_pin_is_rejected_with_the_reason_named(reserved):
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_user_gpio_pin("pin", reserved)
    assert excinfo.value.code == "invalid_value"
    # The message names the key, the pad, and the wireless reservation -- a
    # remote config writer needs to see why, not just that it failed.
    message = str(excinfo.value)
    assert "pin" in message
    assert str(reserved) in message
    assert "wireless" in message


# --- Reject: type and range ---------------------------------------------------


@pytest.mark.parametrize("bad", [True, False, "4", 4.0, None, -1, 30, 100])
def test_a_non_gpio_pin_is_rejected(bad):
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_user_gpio_pin("pin", bad)
    assert excinfo.value.code == "invalid_value"


@pytest.mark.parametrize("bad", [-1, 30, 100])
def test_an_out_of_board_pin_is_rejected_naming_the_exposed_set(bad):
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_user_gpio_pin("i2c_sda_pin", bad)
    assert excinfo.value.code == "invalid_value"
    assert "i2c_sda_pin" in str(excinfo.value)
    assert "externally exposed" in str(excinfo.value)
