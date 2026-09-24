# test_adc_bus_factory.py - Core 1 ADC bus factory
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""The ADC factory's cache identity is the pin: the pin is the whole
configuration (the channel follows from it, and the RP2's three ADC
channels on GP26/27/28 coexist on the one ADC peripheral), so a second
request for the same pin returns the shared object -- a dedupe, not a
multidrop bus (validate_config rejects two devices on one ADC pin). Unlike
the I2C factory there is no conflict raise: the pin carries no
reconfigurable setting. The recording fake machine module pins the dedupe
and the lazy construction."""

import sys
import types
import pathlib

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


class _RecordingPin:
    instances = []

    def __init__(self, pin):
        self.pin = pin
        _RecordingPin.instances.append(self)


class _RecordingADC:
    instances = []

    def __init__(self, pin):
        self.pin = pin
        _RecordingADC.instances.append(self)


@pytest.fixture
def core1_with_recording_machine(monkeypatch):
    """core1 (host-importable only with a machine module present) plus a
    fake machine module that records every construction; all are restored
    at teardown."""
    _RecordingPin.instances = []
    _RecordingADC.instances = []
    machine = types.ModuleType("machine")
    machine.Pin = _RecordingPin
    machine.ADC = _RecordingADC
    monkeypatch.setitem(sys.modules, "machine", machine)
    import core1
    return core1


def test_building_the_factory_constructs_nothing(core1_with_recording_machine):
    """Lazy construction: a config with no yl69_fc28 device allocates no
    ADC and imports nothing -- the machine import happens inside the
    closure, at the first request."""
    core1_with_recording_machine._build_adc_bus_factory()
    assert _RecordingPin.instances == []
    assert _RecordingADC.instances == []


def test_same_pin_shares_one_adc(core1_with_recording_machine):
    # Dedupe: a second request for the same pin returns the one
    # construction (two devices on one ADC pin is a config error, so this
    # only ever serves the one device that owns the pin).
    factory = core1_with_recording_machine._build_adc_bus_factory()
    first = factory(26)
    second = factory(26)
    assert first is second
    assert len(_RecordingPin.instances) == 1
    assert len(_RecordingADC.instances) == 1
    assert _RecordingPin.instances[0].pin == 26
    assert _RecordingADC.instances[0].pin.pin == 26


def test_different_pins_build_separate_adcs(core1_with_recording_machine):
    # Distinct pins are distinct ADC channels (GP26/27/28 are three
    # channels of the one ADC peripheral).
    factory = core1_with_recording_machine._build_adc_bus_factory()
    first = factory(26)
    second = factory(27)
    assert first is not second
    assert len(_RecordingPin.instances) == 2
    assert len(_RecordingADC.instances) == 2
    assert [adc.pin.pin for adc in _RecordingADC.instances] == [26, 27]
