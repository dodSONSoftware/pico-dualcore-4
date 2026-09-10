# test_ltr390_device.py - LTR390 factory wiring and driver lifecycle
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Host-side tests for the LTR390 factory wiring and the driver's lifecycle
against a fake I2C. The factory tests check the registry and the per-device bus
injection (Core 1 owns the bus; the driver never creates it). The driver tests
run the real init/read/reinit protocol against a canned LTR390 (part ID, one
ALS and one UV sample, a data-ready bit that arrives after a conversion delay
following each active-mode write), with MicroPython's time APIs shimmed so the
data-ready waits are deterministic. The conversion arithmetic itself is covered
in test_ltr390_compensation; here the values are checked for exactness against
the documented reference points, the sequential ALS -> UV protocol (freshness
per channel, bounded wait, standby at the end), and offset application."""

import pathlib
import sys
import time as time_module

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from device_factory import (  # noqa: E402
    allowed_config_keys,
    create_device,
    is_supported_device_type,
)
from devices.ltr390.validation import (  # noqa: E402
    ALLOWED_CONFIG_KEYS,
    DEFAULT_I2C_FREQ_HZ,
)
from devices.ltr390.ltr390_device import LTR390Device  # noqa: E402


# --- Canned samples ---------------------------------------------------------

ALS_RAW = 10000    # default x3 / 18-bit -> 2000.0 lux exactly
UV_RAW = 2300      # default x3 / 18-bit -> 2300 / (2300/24) = 24.0 UVI


def _pack_raw20(value):
    """Encode a 20-bit raw count into the three-byte burst (low, mid, high
    nibble) the driver decodes."""
    b = bytearray(3)
    b[0] = value & 0xFF
    b[1] = (value >> 8) & 0xFF
    b[2] = (value >> 16) & 0x0F
    return bytes(b)


# --- Fakes -----------------------------------------------------------------


class FakeLTR390I2C:
    """A canned LTR390: part ID, one ALS and one UV sample, and a data-ready
    bit that clears on every data-register read and re-arms after each
    active-mode write, arriving ``conversion_delay_ms`` later (the fake reads
    the shimmed ticks clock, so the delay is deterministic under FakeTime).
    Records every event so the sequential protocol can be asserted."""

    def __init__(
        self,
        part_id=0xB2,
        als_raw=ALS_RAW,
        uv_raw=UV_RAW,
        conversion_delay_ms=100,
        latched_data_ready=False,
    ):
        self.part_id = part_id
        self.als_data = _pack_raw20(als_raw)
        self.uvs_data = _pack_raw20(uv_raw)
        self.conversion_delay_ms = conversion_delay_ms
        # Latched state simulates a stale data-ready (power-on bit, a previous
        # user, a half-finished read) that init's clear sequence must remove.
        self._armed = latched_data_ready
        self._pending = False
        self._ready_at = 0
        self._status_reads = 0
        self.first_polls = []
        self.events = []

    def readfrom_mem_into(self, address, register, buffer):
        self.events.append(("read", register))
        if register == 0x06:
            buffer[0] = self.part_id
        elif register == 0x07:
            if self._pending and time_module.ticks_ms() >= self._ready_at:
                self._pending = False
                self._armed = True
            if self._status_reads == 1:
                # The first poll after a mode switch: must see data-ready
                # clear (the previous data read cleared it) -- the freshness
                # boundary. A True here means a stale sample was accepted.
                self.first_polls.append(self._armed)
            self._status_reads += 1
            buffer[0] = 0x20 | (0x08 if self._armed else 0x00)
        elif register == 0x0D:
            buffer[:] = self.als_data
            self._armed = False
            self._pending = False
        elif register == 0x10:
            buffer[:] = self.uvs_data
            self._armed = False
            self._pending = False

    def writeto_mem(self, address, register, data):
        self.events.append(("write", register, data[0]))
        if register == 0x00 and data[0] in (0x02, 0x0A):
            # An active-mode write (re)starts the conversion: the previous
            # sample is stale and a fresh one completes after the delay.
            self._armed = False
            self._pending = True
            self._ready_at = time_module.ticks_ms() + self.conversion_delay_ms
            self._status_reads = 0


class FakeTime:
    """Controllable monotonic clock (MicroPython ticks semantics); sleep_ms
    advances it so the data-ready waits are deterministic."""

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
        "id": "ltr390-1",
        "device_type": "ltr390",
        "name": "LTR390 Light/UV Sensor",
        "config": config if config is not None else {"i2c_bus": 0},
    }


def _initialized_device(config=None, i2c=None):
    device = LTR390Device(i2c if i2c is not None else FakeLTR390I2C())
    device.initialize(config or {"i2c_bus": 0})
    return device


# --- Factory ----------------------------------------------------------------


def test_registry_supports_ltr390():
    assert is_supported_device_type("ltr390") is True
    assert allowed_config_keys("ltr390") == ALLOWED_CONFIG_KEYS


def test_create_device_returns_an_ltr390_device_and_calls_the_factory():
    calls = []

    def factory(bus, sda, scl, freq):
        calls.append((bus, sda, scl, freq))
        return FakeLTR390I2C()

    definition = _valid_definition(
        {
            "i2c_bus": 1,
            "i2c_sda_pin": 4,
            "i2c_scl_pin": 5,
            "i2c_freq_hz": 100000,
            "gain": 18,
            "resolution_bits": 20,
            "measurement_rate_ms": 1000,
        }
    )
    device = create_device(definition, i2c_bus_factory=factory)
    assert isinstance(device, LTR390Device)
    assert calls == [(1, 4, 5, 100000)]


def test_create_device_uses_bus_default_pins_and_freq_when_absent():
    calls = []

    def factory(bus, sda, scl, freq):
        calls.append((bus, sda, scl, freq))
        return FakeLTR390I2C()

    device = create_device(_valid_definition(), i2c_bus_factory=factory)
    assert isinstance(device, LTR390Device)
    assert calls == [(0, None, None, DEFAULT_I2C_FREQ_HZ)]


def test_create_device_without_a_factory_raises():
    with pytest.raises(ValueError):
        create_device(_valid_definition())


# --- Initialization ---------------------------------------------------------


def test_initialize_succeeds_with_a_valid_sensor(fake_time):
    device = _initialized_device()
    assert device._initialized is True


@pytest.mark.parametrize("part_id", [0xA2, 0xC2, 0x52, 0x00])
def test_initialize_fails_fast_on_a_wrong_part_id(fake_time, part_id):
    device = LTR390Device(FakeLTR390I2C(part_id=part_id))
    with pytest.raises(OSError):
        device.initialize({"i2c_bus": 0})
    assert device._initialized is False


def test_initialize_records_the_silicon_revision(fake_time):
    device = _initialized_device(i2c=FakeLTR390I2C(part_id=0xB7))
    assert device._sensor._part_id == 0xB7
    assert device._sensor._revision_id == 7


def test_initialize_clears_a_latched_data_ready(fake_time):
    """A stale data-ready (power-on state, a previous user) must not satisfy
    the first runtime freshness wait: init's clear sequence discards one read
    of each channel's data register, which clears the bit."""
    i2c = FakeLTR390I2C(latched_data_ready=True)
    _initialized_device(i2c=i2c)
    assert i2c._armed is False
    # Both channels walked during init, in standby, in ALS-then-UV order.
    events = [e for e in i2c.events if e[0] == "read"]
    assert ("read", 0x0D) in events and ("read", 0x10) in events
    assert events.index(("read", 0x0D)) < events.index(("read", 0x10))


# --- Read -------------------------------------------------------------------


def test_read_before_initialize_raises(fake_time):
    device = LTR390Device(FakeLTR390I2C())
    with pytest.raises(RuntimeError):
        device.read()


def test_read_returns_all_channels(fake_time):
    result = _initialized_device().read()
    assert set(result) == {"lux", "uv_index", "als_raw", "uv_raw"}
    assert result["als_raw"] == ALS_RAW
    assert result["uv_raw"] == UV_RAW
    # Documented reference points at the defaults (x3 / 18-bit, window 1.0).
    assert result["lux"] == 2000.0
    assert result["uv_index"] == pytest.approx(24.0)


def test_read_runs_the_sequential_als_then_uv_protocol(fake_time):
    """The full per-read sequence: ALS active -> (poll ->) ALS burst -> UVS
    active -> (poll ->) UVS burst -> standby, with data-ready clear observed
    on the first poll of each channel (the stale-data guard)."""
    i2c = FakeLTR390I2C()
    device = _initialized_device(i2c=i2c)
    start = len(i2c.events)  # init's clear sequence also reads data registers
    device.read()
    i2c.events = i2c.events[start:]

    def index_of(kind, register, value=None):
        for i, event in enumerate(i2c.events):
            if kind == "write":
                if event[0] == "write" and event[1] == register and (
                    value is None or event[2] == value
                ):
                    return i
            elif event[0] == kind and event[1] == register:
                return i
        raise AssertionError("event not found: {} {}".format(kind, register))

    i_als_on = index_of("write", 0x00, 0x02)
    i_als_read = index_of("read", 0x0D)
    i_uvs_on = index_of("write", 0x00, 0x0A)
    i_uvs_read = index_of("read", 0x10)

    # ALS enabled before it is read; UVS only after the ALS sample is taken;
    # the final MAIN_CTRL write of the read is the ALS standby value.
    assert i_als_on < i_als_read < i_uvs_on < i_uvs_read
    assert i2c.events[-1] == ("write", 0x00, 0x00)

    # Each channel had a status poll between its mode switch and its data read.
    assert index_of("read", 0x07) > i_als_on
    polls_between_uvs = [
        e
        for e in i2c.events[i_uvs_on:i_uvs_read]
        if e == ("read", 0x07)
    ]
    assert polls_between_uvs

    # The freshness boundary: the first poll of each channel saw data-ready
    # clear (set by the previous data read), so a stale sample could not be
    # accepted.
    assert i2c.first_polls == [False, False]


def test_read_times_out_bounded_when_data_ready_never_sets(fake_time):
    """A conversion that never completes must surface as an OSError after the
    configured-resolution timeout -- not a hang and not a wait for the full
    (deliberately huge) conversion delay."""
    i2c = FakeLTR390I2C(conversion_delay_ms=100000)
    device = _initialized_device(i2c=i2c)
    with pytest.raises(OSError, match="data-ready timeout"):
        device.read()
    # 18-bit timeout is 160 ms: far under the 100 s conversion the fake was
    # told to take, proving the wait was the bound, not the conversion.
    assert fake_time.now_ms < 1000


def test_read_applies_offsets_after_conversion(fake_time):
    """Two devices over identical sensor state (same part, samples): the
    zero-offset device and a known-offset device. Each converted channel's
    delta is the offset exactly, and the raw counts are untouched -- offsets
    never fold into the raw read or the conversion scaling."""
    base = _initialized_device().read()
    offset_device = _initialized_device(
        {"i2c_bus": 0, "offsets": {"lux": 15.5, "uv_index": -2.25}}
    )
    result = offset_device.read()
    assert result["lux"] == pytest.approx(base["lux"] + 15.5)
    assert result["uv_index"] == pytest.approx(base["uv_index"] - 2.25)
    assert result["als_raw"] == base["als_raw"]
    assert result["uv_raw"] == base["uv_raw"]


def test_read_applies_the_window_factor_to_converted_channels_only(fake_time):
    base = _initialized_device().read()
    windowed = _initialized_device(
        {"i2c_bus": 0, "window_factor": 2.0}
    ).read()
    assert windowed["lux"] == pytest.approx(2.0 * base["lux"])
    assert windowed["uv_index"] == pytest.approx(2.0 * base["uv_index"])
    assert windowed["als_raw"] == base["als_raw"]
    assert windowed["uv_raw"] == base["uv_raw"]


def test_read_with_a_uv_sensitive_profile(fake_time):
    """End-to-end at x18 / 20-bit (the UVI reference operating point):
    0.6 * raw / (18 * 4) lux and 2300 raw = 1.0 UVI exactly."""
    i2c = FakeLTR390I2C(als_raw=12000, uv_raw=2300, conversion_delay_ms=400)
    device = _initialized_device(
        {
            "i2c_bus": 0,
            "gain": 18,
            "resolution_bits": 20,
            "measurement_rate_ms": 1000,
        },
        i2c=i2c,
    )
    result = device.read()
    assert result["lux"] == pytest.approx(100.0)
    assert result["uv_index"] == pytest.approx(1.0)
    assert result["als_raw"] == 12000
    assert result["uv_raw"] == 2300


# --- Reinitialization -------------------------------------------------------


def test_reinitialize_reruns_the_init_sequence(fake_time):
    """The read-failure reinit path calls initialize() again over the held
    bus; a previously failed read can leave the sensor in any mode, so the
    full sequence (re-identify, clear, rewrite every register) must repeat."""
    i2c = FakeLTR390I2C()
    device = LTR390Device(i2c)
    config = {"i2c_bus": 0}
    device.initialize(config)
    device.initialize(config)
    assert i2c.events.count(("read", 0x06)) == 2        # part ID on both passes
    assert i2c.events.count(("write", 0x19, 0x00)) == 2  # INT_CFG on both passes
