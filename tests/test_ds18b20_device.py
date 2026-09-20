# test_ds18b20_device.py - DS18B20 factory wiring and driver lifecycle
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Host-side tests for the DS18B20 factory wiring and the driver's lifecycle
against a fake 1-Wire bus. The factory tests check the registry and the
per-device bus injection (Core 1 owns the bus; the driver never creates it).
The driver tests run the real init/read protocol against a canned DS18X20
(ROM inventory, conversion and read recording) with MicroPython's time APIs
shimmed so the conversion wait is deterministic. The range gate and the
85 °C power-on value's status as a valid temperature are pinned here; the
pure config contract lives in test_ds18b20_validation."""

import pathlib
import sys
import time as time_module

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from device_factory import (  # noqa: E402
    allowed_config_keys,
    create_device,
)
from devices.ds18b20.validation import (  # noqa: E402
    ALLOWED_CONFIG_KEYS,
)
from devices.ds18b20.ds18b20_device import (  # noqa: E402
    DS18B20Device,
)
from message_protocol import is_json_safe  # noqa: E402


ROM_A = "28ff1ca26117048d"
ROM_B = "28ff8b236117032a"


# --- Fakes -----------------------------------------------------------------


class FakeDS18X20:
    """A canned 1-Wire bus: a fixed ROM inventory, a recorded convert/read
    protocol, a per-read result (a float, None, or an exception to raise),
    and an optional exception to raise from convert_temp(). The ROMs are the
    bytearrays scan() yields, in scan order."""

    def __init__(
        self,
        roms=None,
        temperature=22.4375,
        read_error=None,
        convert_error=None,
        release_error=None,
        acquire_error=None,
    ):
        if roms is None:
            roms = (ROM_A,)
        self._roms = [bytearray(bytes.fromhex(rom)) for rom in roms]
        self.temperature = temperature
        self.read_error = read_error
        self.convert_error = convert_error
        self.release_error = release_error
        self.acquire_error = acquire_error
        self.events = []  # ordered protocol events: "scan", "convert", "release", "acquire", "read"
        self.read_roms = []  # the ROMs passed to read_temp
        self.released = False  # the pin state release()/acquire() toggles

    def scan(self):
        self.events.append("scan")
        return self._roms

    def convert_temp(self):
        self.events.append("convert")
        if self.convert_error is not None:
            raise self.convert_error

    def release(self):
        self.events.append("release")
        if self.release_error is not None:
            raise self.release_error
        self.released = True

    def acquire(self):
        self.events.append("acquire")
        if self.acquire_error is not None:
            raise self.acquire_error
        self.released = False

    def read_temp(self, rom):
        self.events.append("read")
        self.read_roms.append(bytes(rom))
        if self.read_error is not None:
            raise self.read_error
        return self.temperature


class FakeTime:
    """Controllable monotonic clock (MicroPython ticks semantics); sleep_ms
    advances it so the conversion wait is deterministic."""

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
    # CPython's time module has no ticks_*/sleep_ms; raising=False adds them and
    # removes them at teardown. The driver reads them off the shared module at
    # call time, so the patch is visible without any reload.
    monkeypatch.setattr(time_module, "sleep_ms", t.sleep_ms, raising=False)
    monkeypatch.setattr(time_module, "ticks_ms", t.ticks_ms, raising=False)
    monkeypatch.setattr(time_module, "ticks_diff", t.ticks_diff, raising=False)
    return t


def _valid_definition(config=None):
    return {
        "id": "ds18b20-1",
        "device_type": "ds18b20",
        "name": "DS18B20 Temperature Sensor",
        "config": config if config is not None else {"pin": 16, "rom": ROM_A},
    }


def _initialized_device(config=None, ds=None):
    device = DS18B20Device(ds if ds is not None else FakeDS18X20())
    device.initialize(config or {"pin": 16, "rom": ROM_A})
    return device


# --- Factory ----------------------------------------------------------------


def test_registry_supports_ds18b20():
    assert allowed_config_keys("ds18b20") == ALLOWED_CONFIG_KEYS


def test_create_device_returns_a_ds18b20_device_and_passes_the_pin():
    calls = []

    def factory(pin):
        calls.append(pin)
        return FakeDS18X20()

    device = create_device(
        _valid_definition({"pin": 22, "rom": ROM_A}),
        onewire_bus_factory=factory,
    )
    assert isinstance(device, DS18B20Device)
    assert calls == [22]


def test_create_device_without_an_onewire_factory_raises():
    with pytest.raises(ValueError, match="onewire_bus_factory"):
        create_device(_valid_definition())


# --- Initialization ---------------------------------------------------------


def test_initialize_succeeds_when_the_rom_is_present(fake_time):
    ds = FakeDS18X20()
    device = _initialized_device(ds=ds)
    assert device._initialized is True
    assert ds.events.count("scan") == 1


def test_initialize_fails_when_the_rom_is_absent(fake_time):
    # A valid config with no physical backing: an operational failure
    # (OSError -> initialization_failed at boot), never a schema failure.
    device = DS18B20Device(FakeDS18X20(roms=[ROM_B]))
    with pytest.raises(OSError, match="not found"):
        device.initialize({"pin": 16, "rom": ROM_A})


def test_initialize_fails_when_the_bus_is_empty(fake_time):
    device = DS18B20Device(FakeDS18X20(roms=[]))
    with pytest.raises(OSError, match="0 device\\(s\\) present"):
        device.initialize({"pin": 16, "rom": ROM_A})


def test_initialize_matches_by_rom_not_scan_position(fake_time):
    """The scan order is not a permanent identity: the configured ROM must be
    matched wherever it sits in the scan list, even behind another device
    (reading the first-found ROM would measure the wrong physical sensor)."""
    ds = FakeDS18X20(roms=[ROM_B, ROM_A])
    device = _initialized_device(ds=ds)
    assert device._initialized is True


def test_reinitialize_rescans_over_the_held_bus(fake_time):
    """The read-failure reinit path calls initialize() again over the held
    bus: the rescan must repeat (a disconnected sensor is found again by its
    ROM)."""
    ds = FakeDS18X20()
    device = DS18B20Device(ds)
    config = {"pin": 16, "rom": ROM_A}
    device.initialize(config)
    device.initialize(config)
    assert ds.events.count("scan") == 2


# --- Read -------------------------------------------------------------------


def test_read_before_initialize_raises():
    device = DS18B20Device(FakeDS18X20())
    with pytest.raises(RuntimeError):
        device.read()


def test_read_runs_convert_wait_read_in_order(fake_time):
    """The protocol is two-stage: convert, wait, read -- never a bare read of
    the previous scratchpad value. The wait is the configured conversion
    window (750 ms at the 12-bit default), and the data pin is released for
    the whole window (multi-drop etiquette) and re-acquired before the
    read."""
    ds = FakeDS18X20()
    device = _initialized_device(ds=ds)
    ds.events.clear()
    device.read()
    assert ds.events == ["convert", "release", "acquire", "read"]
    assert fake_time.now_ms == 750  # the default conversion window


def test_wait_sleeps_in_bounded_slices_with_the_pin_released(fake_time, monkeypatch):
    """Every sleep of the conversion wait is no longer than the slice bound
    and observes the pin released: the sensor needs no bus traffic while it
    converts, and a multi-drop pin must be free for the whole window (the
    bus is the only communication path to any other sensor sharing it)."""
    ds = FakeDS18X20()
    device = _initialized_device(ds=ds)
    observations = []  # (slice_ms, pin_released) per sleep
    original_sleep = time_module.sleep_ms  # the fixture's FakeTime sleep_ms

    def observing_sleep(ms):
        observations.append((ms, ds.released))
        original_sleep(ms)

    monkeypatch.setattr(time_module, "sleep_ms", observing_sleep)
    device.read()
    assert observations, "the conversion wait slept"
    assert all(released for _, released in observations)
    assert all(ms <= 10 for ms, _ in observations)
    assert sum(ms for ms, _ in observations) == 750


@pytest.mark.parametrize(
    "error_attr, message",
    [("release_error", "release failed"), ("acquire_error", "acquire failed")],
)
def test_wait_pin_transitions_wrap_bus_errors_as_operational_errors(
    fake_time, error_attr, message
):
    """release()/acquire() are calls on the same injected bus as convert and
    read: a non-MemoryError raise from them normalizes to OSError (the
    OSError-only operational domain), not an escape to Core 1's worker
    boundary."""
    device = _initialized_device(ds=FakeDS18X20(**{error_attr: Exception("CRC error")}))
    with pytest.raises(OSError, match=message):
        device.read()


def test_read_uses_the_configured_conversion_wait(fake_time):
    ds = FakeDS18X20()
    device = _initialized_device(
        {"pin": 16, "rom": ROM_A, "conversion_ms": 1000}, ds=ds
    )
    device.read()
    assert fake_time.now_ms == 1000


def test_read_addresses_the_matched_rom(fake_time):
    ds = FakeDS18X20(roms=[ROM_B, ROM_A])
    device = _initialized_device(ds=ds)
    device.read()
    assert ds.read_roms == [bytes.fromhex(ROM_A)]


def test_read_reports_only_temperature_celsius(fake_time):
    result = _initialized_device().read()
    assert set(result) == {"temperature_c"}
    assert isinstance(result["temperature_c"], float)
    assert is_json_safe(result)


def test_offsets_are_applied_after_the_read(fake_time):
    """Two devices over identical bus state: the zero-offset device and a
    known-offset device. The delta is the offset exactly -- the offset is
    application policy, added on top of the raw reading."""
    base = _initialized_device().read()
    offset_device = _initialized_device(
        {"pin": 16, "rom": ROM_A, "offsets": {"temperature_c": 2.5}},
    )
    result = offset_device.read()
    assert abs(result["temperature_c"] - base["temperature_c"] - 2.5) < 1e-9


@pytest.mark.parametrize("temperature", [85.0, -55.0, 125.0, 0.0, 25.0625])
def test_read_accepts_valid_temperatures_including_power_on_value(
    fake_time, temperature
):
    """85.0 is the power-on scratchpad value AND a valid real temperature: it
    must be accepted (the correct software is convert->wait->read, not an
    85.0 filter). The documented range endpoints are valid too."""
    device = _initialized_device(ds=FakeDS18X20(temperature=temperature))
    result = device.read()
    assert result["temperature_c"] == temperature


@pytest.mark.parametrize(
    "temperature", [125.0625, -55.0625, 200.0, -100.0]
)
def test_read_rejects_out_of_range_temperatures(fake_time, temperature):
    """Outside the documented -55..+125 °C range the reading is corrupt (or
    a stale/broken bus): reject it as an operational failure instead of
    publishing it."""
    device = _initialized_device(ds=FakeDS18X20(temperature=temperature))
    with pytest.raises(OSError, match="out-of-range"):
        device.read()


@pytest.mark.parametrize("temperature", [float("nan"), float("inf"), float("-inf")])
def test_read_rejects_non_finite_temperatures(fake_time, temperature):
    device = _initialized_device(ds=FakeDS18X20(temperature=temperature))
    with pytest.raises(OSError):
        device.read()


def test_read_rejects_a_missing_reading(fake_time):
    device = _initialized_device(ds=FakeDS18X20(temperature=None))
    with pytest.raises(OSError, match="no temperature"):
        device.read()


def test_read_wraps_a_bus_failure_as_an_operational_error(fake_time):
    """A bus failure mid-read stays in the OSError-only operational domain
    (retried and reinit-able), annotated with the ROM for the log."""
    device = _initialized_device(
        ds=FakeDS18X20(read_error=OSError("reset failed"))
    )
    with pytest.raises(OSError) as excinfo:
        device.read()
    assert ROM_A in str(excinfo.value)
    assert "reset failed" in str(excinfo.value)


def test_read_wraps_the_bus_modules_bare_exception_as_an_operational_error(
    fake_time,
):
    """MicroPython's ds18x20 module raises a bare Exception on a scratchpad
    CRC failure (bit corruption in transit -- a stale scratchpad is still
    CRC-valid). It must normalize to OSError like every other bus failure,
    or it escapes the driver's boundary to Core 1's worker boundary and
    resets the device over one flaky read."""
    device = _initialized_device(ds=FakeDS18X20(read_error=Exception("CRC error")))
    with pytest.raises(OSError) as excinfo:
        device.read()
    assert ROM_A in str(excinfo.value)
    assert "CRC error" in str(excinfo.value)


def test_convert_wraps_the_bus_bare_exception_as_an_operational_error(fake_time):
    """The convert call is the same injected-bus boundary as the read call:
    a non-MemoryError raise from it normalizes to OSError too."""
    device = _initialized_device(ds=FakeDS18X20(convert_error=Exception("CRC error")))
    with pytest.raises(OSError, match="convert failed"):
        device.read()
