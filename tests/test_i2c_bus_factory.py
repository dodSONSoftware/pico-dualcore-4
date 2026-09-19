# test_i2c_bus_factory.py - Core 1 I2C bus factory
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""The bus factory's cache identity is the physical controller: on the RP2
port machine.I2C(bus) is the controller's own static object, and a second
construction reconfigures that controller's pins and clock. The factory must
therefore dedupe on the bus and RAISE on a same-bus request with a different
(sda, scl, freq) -- reconfiguring the controller would silently destabilize
the device already running on it instead of failing the new one
deterministically. The recording fake machine module pins that the conflict
raises without constructing a second I2C."""

import sys
import types
import pathlib

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


class _RecordingPin:
    def __init__(self, pin):
        self.pin = pin


class _RecordingI2C:
    instances = []

    def __init__(self, bus, **kwargs):
        self.bus = bus
        self.kwargs = kwargs
        _RecordingI2C.instances.append(self)


@pytest.fixture
def core1_with_recording_machine(monkeypatch):
    """core1 (host-importable only with a machine module present) plus a
    fake machine whose I2C records every construction; both are restored at
    teardown."""
    _RecordingI2C.instances = []
    machine = types.ModuleType("machine")
    machine.I2C = _RecordingI2C
    machine.Pin = _RecordingPin
    monkeypatch.setitem(sys.modules, "machine", machine)
    import core1
    return core1


def test_same_bus_and_setting_share_one_i2c(core1_with_recording_machine):
    factory = core1_with_recording_machine._build_i2c_bus_factory()
    first = factory(0, 0, 1, 400000)
    second = factory(0, 0, 1, 400000)
    assert first is second
    assert len(_RecordingI2C.instances) == 1
    instance = _RecordingI2C.instances[0]
    assert instance.bus == 0
    assert set(instance.kwargs) == {"freq", "sda", "scl"}
    assert instance.kwargs["freq"] == 400000
    assert instance.kwargs["sda"].pin == 0
    assert instance.kwargs["scl"].pin == 1


def test_same_bus_default_pins_and_freq_share_one_i2c(core1_with_recording_machine):
    # A config that relies on the bus's default pins passes sda/scl None:
    # two such devices on one bus still share the single construction.
    factory = core1_with_recording_machine._build_i2c_bus_factory()
    first = factory(1, None, None, 400000)
    second = factory(1, None, None, 400000)
    assert first is second
    assert len(_RecordingI2C.instances) == 1
    instance = _RecordingI2C.instances[0]
    assert instance.kwargs == {"freq": 400000}


def test_different_buses_build_separate_i2c(core1_with_recording_machine):
    # The conflict rule is per physical controller: distinct buses may carry
    # distinct pin and clock settings.
    factory = core1_with_recording_machine._build_i2c_bus_factory()
    first = factory(0, 0, 1, 400000)
    second = factory(1, 8, 9, 100000)
    assert first is not second
    assert len(_RecordingI2C.instances) == 2
    assert [i.bus for i in _RecordingI2C.instances] == [0, 1]


@pytest.mark.parametrize(
    "first,second",
    [
        # A different SDA pin would reconfigure the controller's pins.
        ((0, 0, 1, 400000), (0, 4, 1, 400000)),
        # A different SCL pin would reconfigure the controller's pins.
        ((0, 0, 1, 400000), (0, 0, 5, 400000)),
        # A different clock would reconfigure the controller's frequency.
        ((0, 0, 1, 400000), (0, 0, 1, 100000)),
        # Default pins vs explicit pins are a different setting as far as the
        # factory can tell (the port default is not resolved here): the
        # conflict raises rather than reconfiguring.
        ((0, None, None, 400000), (0, 0, 1, 400000)),
    ],
)
def test_same_bus_conflicting_setting_raises_without_reconfiguring(
    core1_with_recording_machine, first, second
):
    factory = core1_with_recording_machine._build_i2c_bus_factory()
    first_bus = factory(*first)
    with pytest.raises(ValueError, match="I2C bus 0 conflict"):
        factory(*second)
    # The controller is constructed exactly once: the first device's bus
    # object is untouched, and no second construction was even attempted.
    assert len(_RecordingI2C.instances) == 1
    assert _RecordingI2C.instances[0] is first_bus
    # The failure is deterministic and repeatable (a device-manager
    # reinitialization would fail the same way, staying visible).
    with pytest.raises(ValueError, match="I2C bus 0 conflict"):
        factory(*second)
