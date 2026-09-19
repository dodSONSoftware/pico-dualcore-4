# test_rp2_i2c_pins.py - Shared RP2 I2C pin-routing validation
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Host-side tests for the shared pure RP2 I2C pin-routing validator.

``validate_rp2_i2c_pins(bus, sda, scl)`` is the single source of the routing
rules both I2C sensor validators share: a pin is only valid for the SDA or SCL
role of the controller its GPIO mux group actually belongs to. The expected
pin sets below are the RP2 datasheet / port mapping themselves (an independent
oracle, not a copy of the module under test), so a drift in the routing table
fails here. Nothing touches hardware -- the import chain reaches no ``machine``
module, so it runs on CPython."""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from devices.device import DeviceValidationError  # noqa: E402
from devices.rp2_i2c import validate_rp2_i2c_pins  # noqa: E402


# The RP2 I2C mux table over the GP0-GP29 user pins: each controller's SDA and
# SCL occupy one GPIO group of four (SDA even / SCL odd within the group). The
# silicon's I2C1 group also lists GP30/GP31, but those are not user GPIOs, so
# they are deliberately absent from these sets.
EXPECTED_SDA = {
    0: (0, 4, 8, 12, 16, 20, 24, 28),
    1: (2, 6, 10, 14, 18, 22, 26),
}
EXPECTED_SCL = {
    0: (1, 5, 9, 13, 17, 21, 25, 29),
    1: (3, 7, 11, 15, 19, 23, 27),
}


# --- Accept: every valid (bus, sda, scl) combination ------------------------


def test_every_valid_sda_scl_pair_per_bus_is_accepted():
    for bus in (0, 1):
        for sda in EXPECTED_SDA[bus]:
            for scl in EXPECTED_SCL[bus]:
                assert validate_rp2_i2c_pins(bus, sda, scl) is None, (bus, sda, scl)


def test_a_single_pin_with_the_other_left_to_the_port_default_is_accepted():
    for bus in (0, 1):
        for sda in EXPECTED_SDA[bus]:
            assert validate_rp2_i2c_pins(bus, sda, None) is None, (bus, sda)
        for scl in EXPECTED_SCL[bus]:
            assert validate_rp2_i2c_pins(bus, None, scl) is None, (bus, scl)


def test_both_pins_absent_is_accepted():
    for bus in (0, 1):
        assert validate_rp2_i2c_pins(bus, None, None) is None, bus


# --- Reject: the routing that was previously accepted -----------------------


def test_i2c0_pins_are_rejected_on_i2c1_and_vice_versa():
    # The original latent bug: bus 1 carrying I2C0's 4/5 pair passed the old
    # range-only check but cannot route to I2C1. The mirror (bus 0 with
    # I2C1's 2/3 pair) is rejected too.
    for bus, sda, scl in ((1, 4, 5), (0, 2, 3)):
        with pytest.raises(DeviceValidationError) as excinfo:
            validate_rp2_i2c_pins(bus, sda, scl)
        assert excinfo.value.code == "invalid_value"


def test_a_pin_outside_the_sda_group_is_rejected_for_sda():
    for bus in (0, 1):
        scl = EXPECTED_SCL[bus][0]
        for pin in range(30):
            if pin in EXPECTED_SDA[bus] or pin == scl:
                continue  # a valid SDA, or the identical-pins case (tested below)
            with pytest.raises(DeviceValidationError) as excinfo:
                validate_rp2_i2c_pins(bus, pin, scl)
            assert excinfo.value.code == "invalid_value"
            assert "i2c_sda_pin" in str(excinfo.value)
            assert "i2c_bus {}".format(bus) in str(excinfo.value)


def test_a_pin_outside_the_scl_group_is_rejected_for_scl():
    for bus in (0, 1):
        sda = EXPECTED_SDA[bus][0]
        for pin in range(30):
            if pin in EXPECTED_SCL[bus] or pin == sda:
                continue
            with pytest.raises(DeviceValidationError) as excinfo:
                validate_rp2_i2c_pins(bus, sda, pin)
            assert excinfo.value.code == "invalid_value"
            assert "i2c_scl_pin" in str(excinfo.value)
            assert "i2c_bus {}".format(bus) in str(excinfo.value)


def test_an_sda_pin_offered_as_scl_is_rejected():
    # 4 is I2C0's SDA pin, so it is not a valid I2C0 SCL even though it is a
    # routable, in-range GPIO (the port rejects it the same way). The value is
    # distinct from the SDA so this is the routing check, not the identical
    # one.
    for bus, sda, bad_scl in ((0, 0, 4), (1, 2, 6)):
        with pytest.raises(DeviceValidationError) as excinfo:
            validate_rp2_i2c_pins(bus, sda, bad_scl)
        assert excinfo.value.code == "invalid_value"
        assert "i2c_scl_pin" in str(excinfo.value)


# --- Reject: type and range -------------------------------------------------


@pytest.mark.parametrize("bad", [-1, 30, 100, "4", True, 4.0])
def test_rejects_a_non_gpio_sda(bad):
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_rp2_i2c_pins(0, bad, 5)
    assert excinfo.value.code == "invalid_value"


@pytest.mark.parametrize("bad", [-1, 30, 100, "4", True, 4.0])
def test_rejects_a_non_gpio_scl(bad):
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_rp2_i2c_pins(0, 4, bad)
    assert excinfo.value.code == "invalid_value"


def test_rejects_identical_pins():
    # A pin cannot be both roles of one controller (SDA and SCL differ in
    # parity), so an identical pair is rejected before the routing check.
    with pytest.raises(DeviceValidationError) as excinfo:
        validate_rp2_i2c_pins(0, 4, 4)
    assert excinfo.value.code == "invalid_value"
    assert "different pins" in str(excinfo.value)
