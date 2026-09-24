# devices/yl69_fc28/yl69_fc28_device.py - YL-69 / FC-28 soil sensor device
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""YL-69 / FC-28 soil-conductivity probe (LM393 comparator module).

The measurement cycle (power sequencing, settle, discard + median
sampling, the optional DO read, and the raw-to-percentage calibration)
lives in the low-level ``Yl69Fc28`` class; ``Yl69Fc28Device`` is the
``Device`` adapter that shapes the telemetry sample. The ``machine.ADC``
object is injected (Core 1 owns it); the digital/power Pins are built
lazily inside ``initialize()`` (a guarded ``machine`` import), so the
module stays host-importable. The reference is the project's YL-69 /
FC-28 MicroPython implementation guide; where guidance disagrees with the
guide's measured-behavior rules, the guide's rules win (clone boards vary
-- the config, not the driver, carries the installation-specific facts).

The published value is ``relative_moisture_percent``: the percentage of
THIS INSTALLATION'S measured dry-to-wet span, per the two calibration
points in the config. It is a conductivity measurement -- dissolved
salts, fertilizer, temperature, and electrode corrosion all move it --
and is deliberately never presented as volumetric water content.
"""

import time

from devices.device import Device
from devices.yl69_fc28.validation import (
    DEFAULT_SAMPLE_COUNT,
    DEFAULT_SAMPLE_DELAY_MS,
    DEFAULT_SETTLE_MS,
    validate_config,
)

try:
    from micropython import const
except ImportError:
    # Host (CPython) has no micropython.const; the identity keeps the module
    # importable for the pure unit tests (const() only affects MicroPython's
    # bytecode, not the values).
    def const(value):
        return value


def relative_moisture_percent(raw, dry_raw, wet_raw):
    """Calibrated relative moisture (0-100) from one filtered raw count.
    The interpolation is sign-agnostic: on the common board polarity dry
    reads ABOVE wet (raw falls as soil conducts), while clone boards can
    invert it -- the same formula and the same 0/100 endpoints hold
    either way, because dry_raw and wet_raw were captured from the
    actual installation, not assumed. Out-of-span readings clamp to the
    endpoints instead of publishing percentages past the calibrated
    range. ``dry_raw == wet_raw`` is unreachable: the validator rejects
    a zero span at the config boundary."""
    percent = (raw - dry_raw) * 100.0 / (wet_raw - dry_raw)
    if percent < 0.0:
        return 0.0
    if percent > 100.0:
        return 100.0
    return percent


class Yl69Fc28:
    """Low-level YL-69 / FC-28 measurement cycle.

    Owns the power sequencing, the settle/discard/median sampling, the
    optional DO read, and the calibration conversion -- nothing else. The
    ``machine.ADC`` object and the digital/power ``machine.Pin`` objects
    (each None when the config omits that signal) are injected. Every
    operational failure (an ADC or pin call raising) is normalized to
    ``OSError``; ``MemoryError`` escapes to the heap boundary.
    """

    def __init__(
        self,
        adc,
        dry_raw,
        wet_raw,
        settle_ms,
        sample_count,
        sample_delay_ms,
        digital_pin=None,
        power=None,
        power_active_low=True,
    ):
        self._adc = adc
        self._digital = digital_pin
        self._power = power
        self._power_active_low = power_active_low
        self._dry_raw = dry_raw
        self._wet_raw = wet_raw
        self._settle_ms = settle_ms
        self._sample_count = sample_count
        self._sample_delay_ms = sample_delay_ms
        # Reusable sample buffer: allocated once (an odd-length list),
        # not per read.
        self._samples = [0] * sample_count

    def init(self):
        """Re-runnable bring-up. The ADC channel needs no setup beyond its
        construction (Core 1's factory), the DO line is already an input,
        and the power line was constructed in the off state -- so there is
        nothing to sequence. The method exists for the ``Device``
        initialize/reinit contract (a previously failed read can leave the
        power line on; ``read()``'s finally re-arms it)."""
        self._set_power(False)

    def _set_power(self, enabled):
        if self._power is None:
            return
        try:
            if self._power_active_low:
                self._power.value(0 if enabled else 1)
            else:
                self._power.value(1 if enabled else 0)
        except MemoryError:
            raise
        except Exception as err:
            raise OSError("YL-69/FC-28 power switch failed: {}".format(err))

    def _read_sample(self):
        try:
            return self._adc.read_u16()
        except MemoryError:
            raise
        except Exception as err:
            # The injected ADC's failure domain is operational: the driver
            # normalizes every hardware failure to OSError like every other
            # device, or it escapes to Core 1's worker boundary and resets
            # the device over one flaky read.
            raise OSError("YL-69/FC-28 ADC read failed: {}".format(err))

    def _read_filtered_raw(self):
        """One filtered raw count: discard the first conversion (the
        guide's post-power-up/idle first sample is not trusted), then take the
        median of ``sample_count`` samples spaced ``sample_delay_ms``
        apart. The median (not the mean) rejects the occasional spike a
        resistive probe is prone to; the small list is the guide's
        Pico-acceptable allocation."""
        self._read_sample()
        samples = self._samples
        for index in range(self._sample_count):
            samples[index] = self._read_sample()
            if self._sample_delay_ms:
                time.sleep_ms(self._sample_delay_ms)
        ordered = sorted(samples)
        return ordered[len(ordered) // 2]

    def read(self):
        """One measurement: power on (if switched), settle, sample, read
        DO, power off. Returns ``(raw, percent, digital)`` where ``raw`` is
        the filtered count the calibration was applied to and ``digital``
        is the comparator's raw 0/1 (None when unconfigured -- clone
        boards differ in DO polarity, so the driver reports the line as-is
        and leaves interpretation to the application). The power-off is in
        a ``finally``: a failed read must never leave the electrodes
        energized (continuous power is the corrosion mechanism this
        switching exists to avoid)."""
        self._set_power(True)
        try:
            if self._power is not None and self._settle_ms:
                time.sleep_ms(self._settle_ms)
            raw = self._read_filtered_raw()
            if self._digital is not None:
                try:
                    digital = self._digital.value()
                except MemoryError:
                    raise
                except Exception as err:
                    raise OSError("YL-69/FC-28 DO read failed: {}".format(err))
            else:
                digital = None
        finally:
            self._set_power(False)
        return raw, relative_moisture_percent(raw, self._dry_raw, self._wet_raw), digital


class Yl69Fc28Device(Device):
    """``Device`` adapter for the YL-69 / FC-28: builds the optional DO /
    power Pins from the config and shapes the telemetry sample. The
    calibration is the installation's, carried in the config -- there is
    no driver-side calibration state to apply (unlike the other devices'
    offsets)."""

    def __init__(self, adc):
        self._adc = adc
        self._sensor = None
        self._initialized = False

    def initialize(self, config):
        """Validate (shared pure rules) then build the Pins. Re-runnable:
        the read-failure reinit path calls this again; rebuilding the Pins
        over the same GPIOs reconfigures them to the same state, and the
        sensor's init re-arms the power line off."""
        validate_config(config)

        power_active_low = config.get("power_active_low", True)
        digital_pin = None
        if "digital_pin" in config:
            digital_pin = _make_pin(config["digital_pin"], "in")
        power = None
        if "power_pin" in config:
            # The power line is constructed in the off state so the probe
            # is never energized between construction and init(); init()
            # then re-asserts it so a previously failed read cannot leave
            # the probe energized.
            power = _make_pin(
                config["power_pin"], "out",
                off_value=1 if power_active_low else 0,
            )
        self._sensor = Yl69Fc28(
            self._adc,
            config["dry_raw"],
            config["wet_raw"],
            config.get("settle_ms", DEFAULT_SETTLE_MS),
            config.get("sample_count", DEFAULT_SAMPLE_COUNT),
            config.get("sample_delay_ms", DEFAULT_SAMPLE_DELAY_MS),
            digital_pin=digital_pin,
            power=power,
            power_active_low=power_active_low,
        )
        self._sensor.init()
        self._initialized = True

    def read(self):
        """One telemetry sample. ``raw`` stays reported unmodified so a
        stuck ADC endpoint (a disconnected probe reads near 65535, a short
        near 0) stays visible next to the percentage it drives."""
        if not self._initialized:
            raise RuntimeError("YL-69/FC-28 device is not initialized")

        raw, percent, digital = self._sensor.read()
        return {
            "raw": raw,
            "relative_moisture_percent": percent,
            "digital_state": digital,
        }


def _make_pin(pin, kind, off_value=0):
    """Build one machine.Pin for a validated config pin, imported lazily
    (host tests fake the ``machine`` module). ``kind`` is ``"in"`` for the
    DO line (a plain input -- the FC-28 board already carries the LM393's
    pull-up, and an MCU pull-up could back-feed an unpowered module
    through DO) or ``"out"`` for the power-switch control, constructed in
    the ``off_value`` state."""
    from machine import Pin

    if kind == "in":
        return Pin(pin, Pin.IN)
    return Pin(pin, Pin.OUT, value=off_value)
