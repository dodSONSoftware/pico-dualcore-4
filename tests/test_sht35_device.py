# test_sht35_device.py - SHT35 factory wiring and driver lifecycle
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Host-side tests for the SHT35 factory wiring and the driver's lifecycle
against a fake I2C. The factory tests check the registry and the per-device
bus injection (Core 1 owns the bus; the driver never creates it). The driver
tests run the real init/read/reinit protocol against a canned SHT35 (CRC
valid, so the responses are genuine protocol frames), with MicroPython's
time APIs shimmed so the serial/break/conversion waits are deterministic.
The CRC implementation and the raw-value transfer functions are pinned by
the Sensirion validation vector and the equation endpoints."""

import pathlib
import sys
import time as time_module

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from device_factory import (  # noqa: E402
    allowed_config_keys,
    create_device,
)
from devices.sht35.validation import (  # noqa: E402
    ALLOWED_CONFIG_KEYS,
    DEFAULT_I2C_FREQ_HZ,
)
from devices.sht35.sht35_device import (  # noqa: E402
    SHT35,
    SHT35Device,
    _crc8,
    convert_humidity_percent,
    convert_temperature_c,
)
from message_protocol import is_json_safe  # noqa: E402


# --- Canned sensor state ----------------------------------------------------

SERIAL = 0x12345678
# raw 26214 is exactly 25.0 deg C ((25 + 45) * 65535 / 175); raw 29491 is
# ~45.0 %RH.
RAW_T = 26214
RAW_RH = 29491

_CMD_SERIAL = 0x3682
_CMD_BREAK = 0x3093
_CMD_HEATER_OFF = 0x3066
_CMD_MEASURE = {0x2400: 16, 0x240B: 7, 0x2416: 5}


def _word_frame(word):
    """A CRC-protected 16-bit data word (MSB, LSB, CRC over the two bytes)."""
    msb = (word >> 8) & 0xFF
    lsb = word & 0xFF
    return bytes((msb, lsb, _crc8(bytes((msb, lsb)), 0, 2)))


def _serial_frame(serial=SERIAL):
    return _word_frame((serial >> 16) & 0xFFFF) + _word_frame(serial & 0xFFFF)


def _measurement_frame(raw_t=RAW_T, raw_rh=RAW_RH,
                       bad_temperature_crc=False, bad_humidity_crc=False):
    frame = bytearray(_word_frame(raw_t) + _word_frame(raw_rh))
    if bad_temperature_crc:
        frame[2] ^= 0xFF
    if bad_humidity_crc:
        frame[5] ^= 0xFF
    return bytes(frame)


# --- Fakes -----------------------------------------------------------------


class FakeSHT35I2C:
    """A canned SHT35 at one address: a CRC-valid serial-number response to
    0x3682 and a CRC-valid measurement frame to any single-shot command.
    Records every 16-bit command word so the init/read protocol (probe order,
    break, heater off, the per-repeatability command) can be asserted."""

    def __init__(self, address=68, serial=SERIAL, raw_t=RAW_T, raw_rh=RAW_RH,
                 bad_serial_crc=False, bad_temperature_crc=False,
                 bad_humidity_crc=False, nack_commands=False, nack_reads=False):
        self.address = address
        self._serial = serial
        self._raw_t = raw_t
        self._raw_rh = raw_rh
        self._bad_serial_crc = bad_serial_crc
        self._bad_temperature_crc = bad_temperature_crc
        self._bad_humidity_crc = bad_humidity_crc
        self.nack_commands = nack_commands
        self.nack_reads = nack_reads
        self.commands = []

    def _frame_for(self, command):
        if command == _CMD_SERIAL:
            frame = bytearray(_serial_frame(self._serial))
            if self._bad_serial_crc:
                frame[2] ^= 0xFF
            return bytes(frame)
        if command in _CMD_MEASURE:
            return _measurement_frame(
                self._raw_t, self._raw_rh,
                bad_temperature_crc=self._bad_temperature_crc,
                bad_humidity_crc=self._bad_humidity_crc,
            )
        raise AssertionError("unexpected read after command 0x{:04X}".format(command))

    def writeto(self, address, data):
        if address != self.address or self.nack_commands:
            raise OSError(28, "I2C ACK timeout")
        self.commands.append((data[0] << 8) | data[1])

    def readfrom_into(self, address, buffer):
        if address != self.address or self.nack_reads:
            raise OSError(28, "I2C ACK timeout")
        buffer[:] = self._frame_for(self.commands[-1])


class FakeTime:
    """Controllable monotonic clock (MicroPython ticks semantics); sleep_ms
    advances it so the serial/break/conversion waits are deterministic."""

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
        "id": "sht35-1",
        "device_type": "sht35",
        "name": "SHT35 Environmental Sensor",
        "config": config if config is not None else {"i2c_bus": 0},
    }


def _initialized_device(config=None, i2c=None):
    device = SHT35Device(i2c if i2c is not None else FakeSHT35I2C())
    device.initialize(config or {"i2c_bus": 0})
    return device


# --- CRC and conversion (pure) ----------------------------------------------


def test_crc8_matches_the_sensirion_validation_vector():
    assert _crc8(bytes((0xBE, 0xEF)), 0, 2) == 0x92


def test_temperature_conversion_endpoints():
    assert convert_temperature_c(0) == -45.0
    assert convert_temperature_c(65535) == 130.0
    # raw 26214 = (25 + 45) * 65535 / 175 exactly.
    assert convert_temperature_c(RAW_T) == 25.0


def test_humidity_conversion_endpoints():
    assert convert_humidity_percent(0) == 0.0
    assert convert_humidity_percent(65535) == 100.0
    # The equation spans exactly 0-100, so a raw value can never escape.
    assert convert_humidity_percent(RAW_RH) == pytest.approx(45.0, abs=0.001)


# --- Factory ----------------------------------------------------------------


def test_registry_supports_sht35():
    assert allowed_config_keys("sht35") == ALLOWED_CONFIG_KEYS


def test_create_device_returns_an_sht35_device_and_calls_the_factory():
    calls = []

    def factory(bus, sda, scl, freq):
        calls.append((bus, sda, scl, freq))
        return FakeSHT35I2C()

    definition = _valid_definition(
        {
            "i2c_bus": 1,
            "i2c_sda_pin": 6,
            "i2c_scl_pin": 7,
            "i2c_freq_hz": 100000,
        }
    )
    device = create_device(definition, i2c_bus_factory=factory)
    assert isinstance(device, SHT35Device)
    assert calls == [(1, 6, 7, 100000)]


def test_create_device_uses_bus_default_pins_and_freq_when_absent():
    calls = []

    def factory(bus, sda, scl, freq):
        calls.append((bus, sda, scl, freq))
        return FakeSHT35I2C()

    device = create_device(_valid_definition(), i2c_bus_factory=factory)
    assert isinstance(device, SHT35Device)
    assert calls == [(0, None, None, DEFAULT_I2C_FREQ_HZ)]


def test_create_device_without_a_factory_raises():
    with pytest.raises(ValueError):
        create_device(_valid_definition())


# --- Initialization ---------------------------------------------------------


def test_initialize_succeeds_with_a_valid_sensor(fake_time):
    device = _initialized_device()
    assert device._initialized is True


def test_initialize_binds_the_first_crc_valid_candidate(fake_time):
    # 68 answers, so the probe must bind it and never reach 69.
    i2c = FakeSHT35I2C(address=68)
    device = SHT35Device(i2c)
    device.initialize({"i2c_bus": 0, "i2c_address_candidates": [68, 69]})
    assert i2c.commands.count(_CMD_SERIAL) == 1


def test_initialize_walks_past_a_crc_invalid_candidate(fake_time):
    # 68 answers the serial command but with a corrupt CRC (a different device
    # at 0x44 answering with garbage): the probe must skip it and fail at 69
    # (no sensor there), having walked the whole candidate list.
    i2c = FakeSHT35I2C(address=68, bad_serial_crc=True)
    device = SHT35Device(i2c)
    with pytest.raises(OSError):
        device.initialize({"i2c_bus": 0, "i2c_address_candidates": [68, 69]})
    # One serial probe at 68 (transmitted, CRC-rejected); the probe at 69
    # NACKs at the address phase, so the fake records nothing for it.
    assert i2c.commands == [_CMD_SERIAL]


def test_initialize_binds_the_second_candidate_when_the_first_nacks(fake_time):
    i2c = FakeSHT35I2C(address=69)
    device = SHT35Device(i2c)
    device.initialize({"i2c_bus": 0, "i2c_address_candidates": [68, 69]})
    result = device.read()
    assert result["temperature_c"] == 25.0


def test_initialize_fails_when_no_candidate_answers(fake_time):
    i2c = FakeSHT35I2C(address=70)  # answers neither 68 nor 69
    device = SHT35Device(i2c)
    with pytest.raises(OSError) as excinfo:
        device.initialize({"i2c_bus": 0, "i2c_address_candidates": [68, 69]})
    assert "SHT35 not found at candidates" in str(excinfo.value)


def test_initialize_writes_break_then_heater_off(fake_time):
    """init establishes the known state the reads depend on: out of any
    periodic/ART acquisition (break 0x3093) and heater off (0x3066) -- in
    that order, after the detection probe."""
    i2c = FakeSHT35I2C()
    _initialized_device(i2c=i2c)
    assert i2c.commands == [_CMD_SERIAL, _CMD_BREAK, _CMD_HEATER_OFF]


def test_initialize_waits_are_bounded(fake_time):
    # Serial probe 1 ms + break 1 ms; the heater command is a command-only
    # transaction with no wait.
    _initialized_device()
    assert fake_time.now_ms == 2


# --- Read -------------------------------------------------------------------


def test_read_before_initialize_raises():
    device = SHT35Device(FakeSHT35I2C())
    with pytest.raises(RuntimeError):
        device.read()


def test_read_returns_both_channels(fake_time):
    result = _initialized_device().read()
    assert set(result) == {"temperature_c", "humidity_percent"}
    assert result["temperature_c"] == 25.0
    assert result["humidity_percent"] == pytest.approx(45.0, abs=0.001)
    assert is_json_safe(result)


@pytest.mark.parametrize(
    "repeatability,command,wait_ms",
    [
        ("high", 0x2400, 16),
        ("medium", 0x240B, 7),
        ("low", 0x2416, 5),
    ],
)
def test_read_uses_the_configured_repeatability_command_and_wait(
    fake_time, repeatability, command, wait_ms
):
    i2c = FakeSHT35I2C()
    device = _initialized_device({"i2c_bus": 0, "repeatability": repeatability}, i2c)
    before = fake_time.now_ms
    device.read()
    assert i2c.commands[-1] == command
    assert fake_time.now_ms - before == wait_ms


def test_read_rejects_a_corrupt_temperature_crc(fake_time):
    # A failed CRC is a bus-integrity failure (OSError), never a reading:
    # the bytes transferred, but their integrity failed.
    device = _initialized_device(
        None, FakeSHT35I2C(bad_temperature_crc=True)
    )
    with pytest.raises(OSError, match="CRC mismatch"):
        device.read()


def test_read_rejects_a_corrupt_humidity_crc(fake_time):
    device = _initialized_device(None, FakeSHT35I2C(bad_humidity_crc=True))
    with pytest.raises(OSError, match="CRC mismatch"):
        device.read()


def test_read_normalizes_a_nacked_command_to_oserror(fake_time):
    device = _initialized_device()
    device._sensor._i2c.nack_commands = True
    with pytest.raises(OSError):
        device.read()


def test_read_normalizes_a_nack_read_to_oserror(fake_time):
    device = _initialized_device()
    device._sensor._i2c.nack_reads = True
    with pytest.raises(OSError):
        device.read()


def test_offsets_are_applied_after_conversion(fake_time):
    """Two devices over identical sensor state: the zero-offset device and a
    known-offset device. Each channel's delta is the offset exactly -- proving
    offsets are added on top of the factory-calibrated conversion."""
    base = _initialized_device().read()
    offset_device = _initialized_device(
        {
            "i2c_bus": 0,
            "offsets": {
                "temperature_c": 2.5,
                "humidity_percent": -3.0,
            },
        }
    )
    result = offset_device.read()
    assert abs(result["temperature_c"] - base["temperature_c"] - 2.5) < 1e-9
    assert abs(result["humidity_percent"] - base["humidity_percent"] + 3.0) < 1e-9


# --- Reinitialization -------------------------------------------------------


def test_reinitialize_reruns_the_init_sequence(fake_time):
    """The read-failure reinit path calls initialize() again over the held
    bus; the full sequence (re-detect, break, heater off) must repeat, because
    a previously failed read can leave the sensor in any state."""
    i2c = FakeSHT35I2C()
    device = SHT35Device(i2c)
    config = {"i2c_bus": 0}
    device.initialize(config)
    device.initialize(config)
    assert i2c.commands.count(_CMD_SERIAL) == 2  # detection probe, both passes
    assert i2c.commands.count(_CMD_BREAK) == 2
    assert i2c.commands.count(_CMD_HEATER_OFF) == 2
