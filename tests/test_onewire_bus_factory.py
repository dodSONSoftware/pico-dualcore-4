# test_onewire_bus_factory.py - Core 1 1-Wire bus factory
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""The bus factory's cache identity is the data pin: two DS18B20 sensors on
one pin (multidrop) share one ds18x20.DS18X20 -- and one conversion window --
while distinct pins are distinct physical buses. Unlike the I2C factory there
is no conflict raise: a 1-Wire pin carries no reconfigurable setting, so a
second device on the same pin simply shares the bus. The recording fake
machine/onewire/ds18x20 modules pin the dedupe and the lazy construction."""

import sys
import types
import pathlib

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


class _RecordingPin:
    def __init__(self, pin):
        self.pin = pin


class _RecordingOneWire:
    instances = []

    def __init__(self, pin):
        self.pin = pin
        _RecordingOneWire.instances.append(self)


class _RecordingDS18X20:
    instances = []

    def __init__(self, ow):
        self.ow = ow
        _RecordingDS18X20.instances.append(self)


@pytest.fixture
def core1_with_recording_machine(monkeypatch):
    """core1 (host-importable only with a machine module present) plus fake
    machine/onewire/ds18x20 modules that record every construction; all are
    restored at teardown."""
    _RecordingOneWire.instances = []
    _RecordingDS18X20.instances = []
    machine = types.ModuleType("machine")
    machine.Pin = _RecordingPin
    monkeypatch.setitem(sys.modules, "machine", machine)
    onewire = types.ModuleType("onewire")
    onewire.OneWire = _RecordingOneWire
    monkeypatch.setitem(sys.modules, "onewire", onewire)
    ds18x20 = types.ModuleType("ds18x20")
    ds18x20.DS18X20 = _RecordingDS18X20
    monkeypatch.setitem(sys.modules, "ds18x20", ds18x20)
    import core1
    return core1


def test_building_the_factory_constructs_nothing(core1_with_recording_machine):
    """Lazy construction: a config with no 1-Wire device allocates no bus and
    imports nothing -- the machine/onewire/ds18x20 imports happen inside the
    closure, at the first request."""
    core1_with_recording_machine._build_onewire_bus_factory()
    assert _RecordingOneWire.instances == []
    assert _RecordingDS18X20.instances == []


def test_same_pin_shares_one_bus(core1_with_recording_machine):
    # Multidrop: two sensors on one pin share the single construction.
    factory = core1_with_recording_machine._build_onewire_bus_factory()
    first = factory(16)
    second = factory(16)
    assert first is second
    assert len(_RecordingOneWire.instances) == 1
    assert len(_RecordingDS18X20.instances) == 1
    assert _RecordingOneWire.instances[0].pin.pin == 16
    assert _RecordingDS18X20.instances[0].ow is _RecordingOneWire.instances[0]


def test_different_pins_build_separate_buses(core1_with_recording_machine):
    # Distinct pins are distinct physical buses.
    factory = core1_with_recording_machine._build_onewire_bus_factory()
    first = factory(16)
    second = factory(17)
    assert first is not second
    assert len(_RecordingOneWire.instances) == 2
    assert [ow.pin.pin for ow in _RecordingOneWire.instances] == [16, 17]
