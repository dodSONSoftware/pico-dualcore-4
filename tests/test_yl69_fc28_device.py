# test_yl69_fc28_device.py - YL-69/FC-28 factory wiring and driver lifecycle
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Host-side tests for the YL-69 / FC-28 factory wiring and the driver's
lifecycle against a fake ADC. The factory tests check the registry and the
per-device ADC injection (Core 1 owns the ADC; the driver never creates
it). The driver tests run the real initialize/read sequence against a
canned ADC, with MicroPython's time APIs and the machine module shimmed so
the settle/sampling waits and the Pin construction are deterministic. The
calibration math is pinned by the guide's polarity vectors (dry above wet
and the reversed clone) and the clamped endpoints."""

import pathlib
import sys
import time as time_module
import types

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from device_factory import (  # noqa: E402
    allowed_config_keys,
    create_device,
)
from devices.yl69_fc28.validation import ALLOWED_CONFIG_KEYS  # noqa: E402
from devices.yl69_fc28.yl69_fc28_device import (  # noqa: E402
    Yl69Fc28,
    Yl69Fc28Device,
    relative_moisture_percent,
)
from message_protocol import is_json_safe  # noqa: E402


# --- Fakes -----------------------------------------------------------------


class FakeADC:
    """A canned ADC: read_u16() returns the queued values in order (a
    constant once exhausted). `error_after` makes the read after that many
    successful ones raise a bare RuntimeError (a machine failure outside
    OSError -- the driver must normalize it); `memory_error_after` the same
    for MemoryError (the heap-boundary escape)."""

    def __init__(self, values=None, constant=30000, error_after=None,
                 memory_error_after=None):
        self._values = list(values or [])
        self._constant = constant
        self._error_after = error_after
        self._memory_error_after = memory_error_after
        self.reads = 0

    def read_u16(self):
        self.reads += 1
        if self._memory_error_after is not None and \
                self.reads > self._memory_error_after:
            raise MemoryError
        if self._error_after is not None and self.reads > self._error_after:
            raise RuntimeError("fake ADC failure")
        if self._values:
            return self._values.pop(0)
        return self._constant


class _RecordingPin:
    """A recording machine.Pin: the mode and initial value are captured,
    value() writes are recorded, and value() reads return `_input_state`
    (tests set it to model the DO line)."""

    IN = "IN"
    OUT = "OUT"
    PULL_UP = "PULL_UP"

    instances = []

    def __init__(self, pin, mode=None, value=None):
        self.pin = pin
        self.mode = mode
        self.initial_value = value
        self.writes = []
        self._input_state = 0
        _RecordingPin.instances.append(self)

    def value(self, new_value=None):
        if new_value is None:
            return self._input_state
        self.writes.append(new_value)
        self._output_state = new_value


@pytest.fixture
def fake_machine(monkeypatch):
    """A fake machine module (the driver's only machine import, and a lazy
    one at that) with a recording Pin; restored at teardown."""
    _RecordingPin.instances = []
    machine = types.ModuleType("machine")
    machine.Pin = _RecordingPin
    monkeypatch.setitem(sys.modules, "machine", machine)
    return machine


class FakeTime:
    """Controllable monotonic clock (MicroPython ticks semantics); sleep_ms
    advances it so the settle/sampling waits are deterministic."""

    def __init__(self):
        self.now_ms = 0

    def ticks_ms(self):
        return self.now_ms

    def ticks_diff(self, now, prev):
        return now - prev

    def sleep_ms(self, ms):
        self.now_ms += int(ms)


@pytest.fixture
def fake_time(monkeypatch):
    t = FakeTime()
    monkeypatch.setattr(time_module, "sleep_ms", t.sleep_ms, raising=False)
    monkeypatch.setattr(time_module, "ticks_ms", t.ticks_ms, raising=False)
    monkeypatch.setattr(time_module, "ticks_diff", t.ticks_diff, raising=False)
    return t


# --- Fixtures / helpers -------------------------------------------------------


def _valid_config():
    return {"adc_pin": 26, "dry_raw": 52000, "wet_raw": 22000}


def _valid_definition(config=None):
    return {
        "id": "yl69-1",
        "device_type": "yl69_fc28",
        "name": "YL-69/FC-28 Soil Sensor",
        "config": config if config is not None else _valid_config(),
    }


def _initialized_device(config=None, adc=None):
    device = Yl69Fc28Device(adc if adc is not None else FakeADC())
    device.initialize(config if config is not None else _valid_config())
    return device


# --- Calibration math (pure) -----------------------------------------------------


def test_calibration_dry_endpoint():
    assert relative_moisture_percent(50000, 50000, 20000) == 0.0


def test_calibration_wet_endpoint():
    assert relative_moisture_percent(20000, 50000, 20000) == 100.0


def test_calibration_midpoint():
    assert relative_moisture_percent(35000, 50000, 20000) == 50.0


def test_calibration_reversed_clone():
    # A clone board with the inverted analog polarity (dry BELOW wet): the
    # same endpoints hold with the calibration captured from that board.
    assert relative_moisture_percent(20000, 20000, 50000) == 0.0
    assert relative_moisture_percent(50000, 20000, 50000) == 100.0


def test_calibration_clamps_below_dry():
    assert relative_moisture_percent(60000, 50000, 20000) == 0.0


def test_calibration_clamps_above_wet():
    assert relative_moisture_percent(10000, 50000, 20000) == 100.0


# --- Factory -----------------------------------------------------------------------


def test_registry_supports_yl69_fc28():
    assert allowed_config_keys("yl69_fc28") == ALLOWED_CONFIG_KEYS


def test_create_device_returns_a_yl69_fc28_device_and_calls_the_factory():
    calls = []

    def factory(pin):
        calls.append(pin)
        return FakeADC()

    device = create_device(
        _valid_definition({"adc_pin": 27, "dry_raw": 52000, "wet_raw": 22000}),
        adc_bus_factory=factory,
    )
    assert isinstance(device, Yl69Fc28Device)
    assert calls == [27]


def test_create_device_without_a_factory_raises():
    with pytest.raises(ValueError):
        create_device(_valid_definition())


# --- Initialization -----------------------------------------------------------------


def test_initialize_succeeds_with_a_valid_sensor(fake_time, fake_machine):
    device = _initialized_device()
    assert device._initialized is True
    # No digital/power pins configured: no Pins constructed.
    assert _RecordingPin.instances == []


def test_initialize_constructs_the_pins(fake_time, fake_machine):
    config = _valid_config()
    config["digital_pin"] = 15
    config["power_pin"] = 14
    _initialized_device(config)
    by_pin = {pin.pin: pin for pin in _RecordingPin.instances}
    # DO: a plain input (no MCU pull-up -- the board carries the LM393's,
    # and an MCU pull-up could back-feed an unpowered module through DO).
    assert by_pin[15].mode == _RecordingPin.IN
    assert by_pin[14].initial_value == 1  # off (active-low default)


def test_initialize_active_high_power_starts_off(fake_time, fake_machine):
    config = _valid_config()
    config["power_pin"] = 14
    config["power_active_low"] = False
    _initialized_device(config)
    power = next(pin for pin in _RecordingPin.instances if pin.pin == 14)
    assert power.initial_value == 0  # off (active-high)


def test_reinitialize_reruns_and_arms_power_off(fake_time, fake_machine):
    """The read-failure reinit path calls initialize() again; the Pins are
    rebuilt over the same GPIOs and the power line ends in the off state."""
    config = _valid_config()
    config["power_pin"] = 14
    device = Yl69Fc28Device(FakeADC())
    device.initialize(config)
    device.initialize(config)
    power_pins = [pin for pin in _RecordingPin.instances if pin.pin == 14]
    assert len(power_pins) == 2
    assert power_pins[-1].initial_value == 1  # off after the re-init


# --- Read ------------------------------------------------------------------------------


def test_read_before_initialize_raises():
    device = Yl69Fc28Device(FakeADC())
    with pytest.raises(RuntimeError):
        device.read()


def test_read_returns_the_three_channels(fake_time):
    result = _initialized_device().read()
    assert set(result) == {
        "raw", "relative_moisture_percent", "digital_state"
    }
    # The canned constant is 30000: (30000 - 52000) * 100 / (22000 - 52000)
    # = 73.333...
    assert result["raw"] == 30000
    assert result["relative_moisture_percent"] == pytest.approx(
        73.333333, abs=1e-4
    )
    assert result["digital_state"] is None
    assert is_json_safe(result)


def test_read_distinguishes_json_safe_none_and_states(fake_time, fake_machine):
    config = _valid_config()
    config["digital_pin"] = 15
    device = _initialized_device(config)
    digital = next(pin for pin in _RecordingPin.instances if pin.pin == 15)
    digital._input_state = 1
    assert device.read()["digital_state"] == 1
    digital._input_state = 0
    assert device.read()["digital_state"] == 0


def test_read_takes_the_median_of_the_samples(fake_time):
    # The first queued value is the discarded post-power-up/idle sample;
    # the median of the remaining five is 34800 (the 34721 spike neighbor
    # and the 35100 outlier do not win).
    adc = FakeADC(values=[1, 34721, 35000, 34000, 34800, 35100])
    config = _valid_config()
    config["sample_count"] = 5
    config["sample_delay_ms"] = 0
    result = _initialized_device(config, adc).read()
    assert result["raw"] == 34800
    # (34800 - 52000) * 100 / (22000 - 52000) = 57.333...
    assert result["relative_moisture_percent"] == pytest.approx(
        57.333333, abs=1e-4
    )
    assert adc.reads == 6  # 1 discard + 5 samples


def test_read_without_a_power_pin_does_not_settle(fake_time):
    before = fake_time.now_ms
    _initialized_device().read()
    # 9 samples at the 2 ms default delay (no settle: nothing switched on).
    assert fake_time.now_ms - before == 18


def test_read_with_switched_power_settles_then_off(fake_time, fake_machine):
    config = _valid_config()
    config["power_pin"] = 14
    device = _initialized_device(config)
    power = next(pin for pin in _RecordingPin.instances if pin.pin == 14)
    before = fake_time.now_ms
    device.read()
    # Settle 200 + 9 samples at 2 ms. Writes: init re-arms off, the read
    # goes on then off (active-low).
    assert fake_time.now_ms - before == 200 + 18
    assert power.writes == [1, 0, 1]


def test_read_active_high_power_sequence(fake_time, fake_machine):
    config = _valid_config()
    config["power_pin"] = 14
    config["power_active_low"] = False
    device = _initialized_device(config)
    power = next(pin for pin in _RecordingPin.instances if pin.pin == 14)
    device.read()
    assert power.writes == [0, 1, 0]  # init off, on, off (active-high)


def test_read_zero_settle_skips_the_settle_sleep(fake_time, fake_machine):
    config = _valid_config()
    config["power_pin"] = 14
    config["settle_ms"] = 0
    device = _initialized_device(config)
    before = fake_time.now_ms
    device.read()
    assert fake_time.now_ms - before == 18  # sampling only


def test_read_normalizes_a_machine_adc_failure_to_oserror(fake_time):
    # A machine failure outside OSError must not escape to Core 1's worker
    # boundary and reset the device over one flaky read.
    adc = FakeADC(error_after=2)
    device = _initialized_device(None, adc)
    with pytest.raises(OSError, match="ADC read failed"):
        device.read()


def test_read_memory_error_escapes(fake_time):
    adc = FakeADC(memory_error_after=0)
    device = _initialized_device(None, adc)
    with pytest.raises(MemoryError):
        device.read()


@pytest.mark.parametrize(
    "active_low,off_value", [(True, 1), (False, 0)]
)
def test_failed_read_leaves_the_power_off(fake_time, fake_machine,
                                          active_low, off_value):
    # The corrosion rule: a failed read must never leave the electrodes
    # energized -- the power-off is in the finally.
    config = _valid_config()
    config["power_pin"] = 14
    if not active_low:
        config["power_active_low"] = False
    adc = FakeADC(error_after=2)
    device = _initialized_device(config, adc)
    power = next(pin for pin in _RecordingPin.instances if pin.pin == 14)
    with pytest.raises(OSError):
        device.read()
    assert power.writes == [off_value, 1 - off_value, off_value]


def test_failed_read_memory_error_leaves_the_power_off(fake_time, fake_machine):
    config = _valid_config()
    config["power_pin"] = 14
    adc = FakeADC(memory_error_after=0)
    device = _initialized_device(config, adc)
    power = next(pin for pin in _RecordingPin.instances if pin.pin == 14)
    with pytest.raises(MemoryError):
        device.read()
    assert power.writes == [1, 0, 1]


def test_low_level_read_returns_the_tuple(fake_time):
    sensor = Yl69Fc28(FakeADC(), 52000, 22000, 0, 1, 0)
    raw, percent, digital = sensor.read()
    assert raw == 30000
    assert percent == pytest.approx(73.333333, abs=1e-4)
    assert digital is None
