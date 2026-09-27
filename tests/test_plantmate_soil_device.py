# test_plantmate_soil_device.py - PlantMate factory wiring and driver lifecycle
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Host-side tests for the PlantMate factory wiring and the driver's
lifecycle against a fake ADC. The factory tests check the registry and the
per-device ADC injection (Core 1 owns the ADC; the driver never creates
it). The driver tests run the real initialize/read sequence against a
canned ADC, with MicroPython's time APIs shimmed so the sampling waits are
deterministic (no machine module is needed: the continuously powered
probe builds no Pins). The calibration math is pinned by the guide's
polarity vectors (dry above wet and the reversed installation) and the
clamped endpoints."""

import pathlib
import sys
import time as time_module

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from device_factory import (  # noqa: E402
    allowed_config_keys,
    create_device,
)
from devices.plantmate_soil.validation import ALLOWED_CONFIG_KEYS  # noqa: E402
from devices.plantmate_soil.plantmate_soil_device import (  # noqa: E402
    PlantMateSoil,
    PlantMateSoilDevice,
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


class FakeTime:
    """Controllable monotonic clock (MicroPython ticks semantics); sleep_ms
    advances it so the sampling waits are deterministic."""

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
    return {"adc_pin": 26, "dry_raw": 50000, "wet_raw": 28000}


def _valid_definition(config=None):
    return {
        "id": "plantmate-1",
        "device_type": "plantmate_soil",
        "name": "PlantMate Soil Sensor",
        "config": config if config is not None else _valid_config(),
    }


def _initialized_device(config=None, adc=None):
    device = PlantMateSoilDevice(adc if adc is not None else FakeADC())
    device.initialize(config if config is not None else _valid_config())
    return device


# --- Calibration math (pure) -----------------------------------------------------


def test_calibration_dry_endpoint():
    assert relative_moisture_percent(50000, 50000, 28000) == 0.0


def test_calibration_wet_endpoint():
    assert relative_moisture_percent(28000, 50000, 28000) == 100.0


def test_calibration_midpoint():
    assert relative_moisture_percent(39000, 50000, 28000) == 50.0


def test_calibration_reversed_polarity():
    # The guide's field calibration captures the points from the actual
    # installation: an installation whose wet reads above dry is legal,
    # and the same endpoints hold with those captured points.
    assert relative_moisture_percent(28000, 28000, 50000) == 0.0
    assert relative_moisture_percent(50000, 28000, 50000) == 100.0


def test_calibration_guide_example():
    # The guide's worked example: dry 50000 / wet 28000, raw 32784 is
    # ~78.2 percent relative moisture.
    assert relative_moisture_percent(32784, 50000, 28000) == pytest.approx(
        78.254545, abs=1e-4
    )


def test_calibration_clamps_below_dry():
    assert relative_moisture_percent(60000, 50000, 28000) == 0.0


def test_calibration_clamps_above_wet():
    assert relative_moisture_percent(10000, 50000, 28000) == 100.0


# --- Factory -----------------------------------------------------------------------


def test_registry_supports_plantmate_soil():
    assert allowed_config_keys("plantmate_soil") == ALLOWED_CONFIG_KEYS


def test_create_device_returns_a_plantmate_soil_device_and_calls_the_factory():
    calls = []

    def factory(pin):
        calls.append(pin)
        return FakeADC()

    device = create_device(
        _valid_definition({"adc_pin": 27, "dry_raw": 50000, "wet_raw": 28000}),
        adc_bus_factory=factory,
    )
    assert isinstance(device, PlantMateSoilDevice)
    assert calls == [27]


def test_create_device_without_a_factory_raises():
    with pytest.raises(ValueError):
        create_device(_valid_definition())


# --- Initialization -----------------------------------------------------------------


def test_initialize_succeeds_with_a_valid_sensor():
    device = _initialized_device()
    assert device._initialized is True


def test_reinitialize_is_idempotent(fake_time):
    """The read-failure reinit path calls initialize() again; rebuilding
    over the same injected ADC channel must succeed and keep reading."""
    device = PlantMateSoilDevice(FakeADC())
    device.initialize(_valid_config())
    device.initialize(_valid_config())
    assert device.read()["raw"] == 30000


# --- Read ------------------------------------------------------------------------------


def test_read_before_initialize_raises():
    device = PlantMateSoilDevice(FakeADC())
    with pytest.raises(RuntimeError):
        device.read()


def test_read_returns_the_two_channels(fake_time):
    result = _initialized_device().read()
    assert set(result) == {"raw", "relative_moisture_percent"}
    # The canned constant is 30000: (30000 - 50000) * 100 / (28000 - 50000)
    # = 90.909...
    assert result["raw"] == 30000
    assert result["relative_moisture_percent"] == pytest.approx(
        90.909090, abs=1e-4
    )
    assert is_json_safe(result)


def test_read_takes_the_median_of_the_samples(fake_time):
    # No discard (the yl69_fc28 discard follows its power-up, which this
    # continuously powered probe has no equivalent of): all five queued
    # values are sampled, and the median is 34800 (the 34721 spike neighbor
    # and the 35100 outlier do not win).
    adc = FakeADC(values=[34721, 35000, 34000, 34800, 35100])
    config = _valid_config()
    config["sample_count"] = 5
    config["sample_delay_ms"] = 0
    result = _initialized_device(config, adc).read()
    assert result["raw"] == 34800
    # (34800 - 50000) * 100 / (28000 - 50000) = 69.090909...
    assert result["relative_moisture_percent"] == pytest.approx(
        69.090909, abs=1e-4
    )
    assert adc.reads == 5  # 5 samples, no discard


def test_read_samples_only_the_configured_count(fake_time):
    config = _valid_config()
    config["sample_count"] = 3
    config["sample_delay_ms"] = 0
    adc = FakeADC(values=[30000, 31000, 32000, 1])
    _initialized_device(config, adc).read()
    assert adc.reads == 3  # the fourth queued value is never drawn


def test_read_default_timing_is_sampling_only(fake_time):
    before = fake_time.now_ms
    _initialized_device().read()
    # 9 samples at the 2 ms default delay; no settle (the probe is
    # continuously powered -- nothing to settle after).
    assert fake_time.now_ms - before == 18


def test_read_custom_timing_is_sampling_only(fake_time):
    config = _valid_config()
    config["sample_count"] = 101
    config["sample_delay_ms"] = 50
    before = fake_time.now_ms
    _initialized_device(config).read()
    # 101 samples at 50 ms: the validator's worst-case liveness window.
    assert fake_time.now_ms - before == 101 * 50


def test_read_zero_delay_skips_the_sleeps(fake_time):
    config = _valid_config()
    config["sample_delay_ms"] = 0
    before = fake_time.now_ms
    _initialized_device(config).read()
    assert fake_time.now_ms - before == 0


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


def test_low_level_read_returns_the_tuple(fake_time):
    sensor = PlantMateSoil(FakeADC(), 50000, 28000, 1, 0)
    raw, percent = sensor.read()
    assert raw == 30000
    assert percent == pytest.approx(90.909090, abs=1e-4)
