# test_bme280_device.py - BME280 factory wiring and driver lifecycle
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Host-side tests for the BME280 factory wiring and the driver's lifecycle
against a fake I2C. The factory tests check the registry and the per-device bus
injection (Core 1 owns the bus; the driver never creates it). The driver tests
run the real init/read/reinit protocol against a canned BME280 (chip ID,
calibration, and one raw sample), with MicroPython's time APIs shimmed so the
reset/NVM/measurement waits are deterministic. The compensation arithmetic itself
is covered in test_bme280_compensation; here the values are checked for
plausibility, offset application, altitude derivation, and the skip sentinels."""

import pathlib
import sys
import time as time_module

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from device_factory import (  # noqa: E402
    allowed_config_keys,
    create_device,
)
from devices.bme280.validation import (  # noqa: E402
    ALLOWED_CONFIG_KEYS,
    DEFAULT_I2C_FREQ_HZ,
)
from devices.bme280.bme280_device import (  # noqa: E402
    BME280Device,
    _SENTINEL_20,
)


# --- Calibration / sample fixtures -----------------------------------------

# Same plausible factory calibration and raw ADC set as the compensation test.
CAL = {
    "t1": 17570, "t2": -19041, "t3": -1302,
    "p1": 36781, "p2": -10685, "p3": 3024, "p4": 2857, "p5": -14193,
    "p6": 13505, "p7": -13997, "p8": 482, "p9": 3152,
    "h1": 230, "h2": -5071, "h3": 11, "h4": 1183, "h5": -1200, "h6": -9,
}
ADC_T = 0x0166A  # 5738
ADC_P = 0x6586C  # 415148
ADC_H = 0x2E8C   # 11916


def _put_u16(buf, offset, value):
    value &= 0xFFFF
    buf[offset] = value & 0xFF
    buf[offset + 1] = (value >> 8) & 0xFF


def _pack_calibration(cal):
    """Encode a calibration dict into the two NVM blocks the driver decodes
    (0x88-0xA1 and 0xE1-0xE7); the H4/H5 shared-register split is here too."""
    b1 = bytearray(26)
    _put_u16(b1, 0, cal["t1"])
    _put_u16(b1, 2, cal["t2"])
    _put_u16(b1, 4, cal["t3"])
    _put_u16(b1, 6, cal["p1"])
    _put_u16(b1, 8, cal["p2"])
    _put_u16(b1, 10, cal["p3"])
    _put_u16(b1, 12, cal["p4"])
    _put_u16(b1, 14, cal["p5"])
    _put_u16(b1, 16, cal["p6"])
    _put_u16(b1, 18, cal["p7"])
    _put_u16(b1, 20, cal["p8"])
    _put_u16(b1, 22, cal["p9"])
    b1[25] = cal["h1"] & 0xFF

    b2 = bytearray(7)
    _put_u16(b2, 0, cal["h2"])
    b2[2] = cal["h3"] & 0xFF
    h4_12 = cal["h4"] & 0xFFF
    h5_12 = cal["h5"] & 0xFFF
    b2[3] = (h4_12 >> 4) & 0xFF
    b2[4] = ((h5_12 & 0x0F) << 4) | (h4_12 & 0x0F)
    b2[5] = (h5_12 >> 4) & 0xFF
    b2[6] = cal["h6"] & 0xFF
    return bytes(b1), bytes(b2)


def _pack_data(adc_t, adc_p, adc_h):
    """Encode the three raw ADCs into the 8-byte 0xF7 burst (press[3], temp[3],
    hum[2]) the driver reconstructs."""
    d = bytearray(8)
    d[0] = (adc_p >> 12) & 0xFF
    d[1] = (adc_p >> 4) & 0xFF
    d[2] = ((adc_p & 0x0F) << 4) | ((adc_t >> 16) & 0x0F)
    d[3] = (adc_t >> 12) & 0xFF
    d[4] = (adc_t >> 4) & 0xFF
    d[5] = (adc_t & 0x0F) << 4
    d[6] = (adc_h >> 8) & 0xFF
    d[7] = adc_h & 0xFF
    return bytes(d)


# --- Fakes -----------------------------------------------------------------


class FakeBME280I2C:
    """A canned BME280: chip ID, NVM-clear status, the two calibration blocks,
    and one raw sample. Records every read and write so the init/read protocol
    and the ctrl_hum -> config -> ctrl_meas latch order can be asserted."""

    def __init__(self, chip_id=0x60, calib1=None, calib2=None, data=None, status=0):
        self.chip_id = chip_id
        default_c1, default_c2 = _pack_calibration(CAL)
        self.calib1 = bytes(calib1 if calib1 is not None else default_c1)
        self.calib2 = bytes(calib2 if calib2 is not None else default_c2)
        self.data = bytes(data if data is not None else _pack_data(ADC_T, ADC_P, ADC_H))
        self.status = status
        self.reads = []
        self.writes = []

    def readfrom_mem_into(self, address, register, buffer):
        self.reads.append(register)
        if register == 0xD0:
            buffer[0] = self.chip_id
        elif register == 0xF3:
            buffer[0] = self.status
        elif register == 0x88:
            buffer[:] = self.calib1[: len(buffer)]
        elif register == 0xE1:
            buffer[:] = self.calib2[: len(buffer)]
        elif register == 0xF7:
            buffer[:] = self.data[: len(buffer)]

    def writeto_mem(self, address, register, data):
        self.writes.append((register, data[0]))


class FakeTime:
    """Controllable monotonic clock (MicroPython ticks semantics); sleep_ms
    advances it so the reset/NVM/measurement waits are deterministic."""

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
        "id": "bme280-1",
        "device_type": "bme280",
        "name": "BME280 Environmental Sensor",
        "config": config
        if config is not None
        else {"i2c_bus": 0, "sea_level_pressure_pa": 101325},
    }


def _initialized_device(config=None, i2c=None):
    device = BME280Device(i2c if i2c is not None else FakeBME280I2C())
    device.initialize(config or {"i2c_bus": 0, "sea_level_pressure_pa": 101325})
    return device


# --- Factory ----------------------------------------------------------------


def test_registry_supports_bme280():
    assert allowed_config_keys("bme280") == ALLOWED_CONFIG_KEYS


def test_create_device_returns_a_bme280_device_and_calls_the_factory():
    calls = []

    def factory(bus, sda, scl, freq):
        calls.append((bus, sda, scl, freq))
        return FakeBME280I2C()

    definition = _valid_definition(
        {
            "i2c_bus": 1,
            "i2c_sda_pin": 4,
            "i2c_scl_pin": 5,
            "i2c_freq_hz": 100000,
            "sea_level_pressure_pa": 101325,
        }
    )
    device = create_device(definition, i2c_bus_factory=factory)
    assert isinstance(device, BME280Device)
    assert calls == [(1, 4, 5, 100000)]


def test_create_device_uses_bus_default_pins_and_freq_when_absent():
    calls = []

    def factory(bus, sda, scl, freq):
        calls.append((bus, sda, scl, freq))
        return FakeBME280I2C()

    device = create_device(_valid_definition(), i2c_bus_factory=factory)
    assert isinstance(device, BME280Device)
    assert calls == [(0, None, None, DEFAULT_I2C_FREQ_HZ)]


def test_create_device_without_a_factory_raises():
    with pytest.raises(ValueError):
        create_device(_valid_definition())


# --- Initialization ---------------------------------------------------------


def test_initialize_succeeds_with_a_valid_sensor(fake_time):
    device = _initialized_device()
    assert device._initialized is True


def test_initialize_fails_fast_on_a_wrong_chip_id(fake_time):
    device = BME280Device(FakeBME280I2C(chip_id=0x58))
    with pytest.raises(OSError):
        device.initialize({"i2c_bus": 0, "sea_level_pressure_pa": 101325})


def test_initialize_probes_every_candidate_before_failing(fake_time):
    # Every candidate answers a non-BME280 chip ID: the probe must walk the
    # whole candidate list (not just the first ACK) before giving up.
    i2c = FakeBME280I2C(chip_id=0x00)
    device = BME280Device(i2c)
    with pytest.raises(OSError):
        device.initialize(
            {
                "i2c_bus": 0,
                "i2c_address_candidates": [118, 119],
                "sea_level_pressure_pa": 101325,
            }
        )
    assert i2c.reads.count(0xD0) == 2


def test_initialize_fails_fast_on_a_zero_dig_p1(fake_time):
    # A blank-NVM pressure coefficient (dig_P1 == 0) would divide by zero on
    # every read, so init must reject it.
    cal = dict(CAL)
    cal["p1"] = 0
    c1, c2 = _pack_calibration(cal)
    device = BME280Device(FakeBME280I2C(calib1=c1, calib2=c2))
    with pytest.raises(OSError):
        device.initialize({"i2c_bus": 0, "sea_level_pressure_pa": 101325})


def test_configure_writes_ctrl_hum_before_config_before_ctrl_meas(fake_time):
    """The latch order: ctrl_hum latches only on a subsequent ctrl_meas write,
    so the init configuration must write 0xF2 -> 0xF5 -> 0xF4 in that order."""
    i2c = FakeBME280I2C()
    _initialized_device(i2c=i2c)
    regs = [reg for reg, _ in i2c.writes]
    assert regs.index(0xF2) < regs.index(0xF5) < regs.index(0xF4)


# --- Read -------------------------------------------------------------------


def test_read_before_initialize_raises():
    device = BME280Device(FakeBME280I2C())
    with pytest.raises(RuntimeError):
        device.read()


def test_read_returns_all_channels(fake_time):
    result = _initialized_device().read()
    assert set(result) == {
        "temperature_c",
        "pressure_pa",
        "humidity_percent",
        "altitude_m",
    }
    for key in result:
        assert isinstance(result[key], float), key


def test_read_returns_plausible_values(fake_time):
    result = _initialized_device().read()
    assert -50.0 <= result["temperature_c"] <= 85.0
    assert 30000.0 <= result["pressure_pa"] <= 115000.0
    assert 0.0 <= result["humidity_percent"] <= 100.0


def test_altitude_matches_the_standard_formula(fake_time):
    sea = 101325.0
    device = _initialized_device({"i2c_bus": 0, "sea_level_pressure_pa": sea})
    result = device.read()
    expected = 44330.0 * (1.0 - (result["pressure_pa"] / sea) ** 0.1903)
    assert abs(result["altitude_m"] - expected) < 1e-6


def test_offsets_are_applied_after_compensation(fake_time):
    """Two devices over identical sensor state (same calibration + sample): the
    zero-offset device and a known-offset device. Each channel's delta is the
    offset exactly -- proving offsets are added on top of the Bosch
    compensation, not folded into the calibration."""
    base = _initialized_device().read()
    offset_device = _initialized_device(
        {
            "i2c_bus": 0,
            "sea_level_pressure_pa": 101325,
            "offsets": {
                "temperature_c": 2.5,
                "pressure_pascal": 1000,
                "humidity_percent": -3.0,
            },
        }
    )
    result = offset_device.read()
    assert abs(result["temperature_c"] - base["temperature_c"] - 2.5) < 1e-9
    assert abs(result["pressure_pa"] - base["pressure_pa"] - 1000.0) < 1e-6
    assert abs(result["humidity_percent"] - base["humidity_percent"] + 3.0) < 1e-9


def test_altitude_is_none_when_pressure_is_skipped(fake_time):
    """A skipped channel is left at its raw sentinel (0x80000 for the 20-bit
    channels); the driver must report pressure and altitude as None while still
    reporting temperature and humidity."""
    i2c = FakeBME280I2C(data=_pack_data(ADC_T, _SENTINEL_20, ADC_H))
    device = _initialized_device(
        {
            "i2c_bus": 0,
            "sea_level_pressure_pa": 101325,
            "pressure_oversampling": 0,
        },
        i2c=i2c,
    )
    result = device.read()
    assert result["pressure_pa"] is None
    assert result["altitude_m"] is None
    assert isinstance(result["temperature_c"], float)
    assert isinstance(result["humidity_percent"], float)


# --- Reinitialization -------------------------------------------------------


def test_reinitialize_reruns_the_init_sequence(fake_time):
    """The read-failure reinit path calls initialize() again over the held bus;
    a soft reset does not preserve configuration, so the full sequence (reset,
    calibration re-read, reconfigure) must repeat."""
    i2c = FakeBME280I2C()
    device = BME280Device(i2c)
    config = {"i2c_bus": 0, "sea_level_pressure_pa": 101325}
    device.initialize(config)
    device.initialize(config)
    assert i2c.writes.count((0xE0, 0xB6)) == 2  # reset written on both passes
    assert i2c.reads.count(0x88) == 2  # first calibration block
    assert i2c.reads.count(0xE1) == 2  # second calibration block
    assert i2c.reads.count(0xF3) >= 2  # NVM-clear wait on both passes
