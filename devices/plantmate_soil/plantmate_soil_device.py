# devices/plantmate_soil/plantmate_soil_device.py - PlantMate soil sensor device
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""PlantMate 3.3 V capacitive soil-moisture sensor.

The measurement cycle (median sampling and the raw-to-percentage
calibration) lives in the low-level ``PlantMateSoil`` class;
``PlantMateSoilDevice`` is the ``Device`` adapter that shapes the telemetry
sample. The ``machine.ADC`` object is injected (Core 1 owns it), so the
module stays host-importable. The reference is the project's PlantMate
MicroPython implementation guide; where guidance disagrees with the
guide's measured-behavior rules, the guide's rules win (the config, not
the driver, carries the installation-specific facts).

The probe is **capacitive**: it responds to the dielectric properties of
the surrounding soil, so (unlike a resistive probe) it needs no power
cycling to prevent electrolysis and is wired continuously to the board's
3.3 V rail -- the driver switches nothing and settles nothing, because the
guide forbids assuming a settling delay the manufacturer never published.
It also has no digital comparator output, so there is no DO line to read.

The published value is ``relative_moisture_percent``: the percentage of
THIS INSTALLATION'S measured dry-to-wet span, per the two calibration
points in the config. It is a relative measure -- soil composition,
density, salinity, depth, orientation, temperature, and individual sensor
variation all move it -- and is deliberately never presented as
volumetric water content. Telemetry keeps one decimal place's worth of
precision at the application: the overall soil/sensor system does not
support more.
"""

import time

from devices.device import Device
from devices.plantmate_soil.validation import (
    DEFAULT_SAMPLE_COUNT,
    DEFAULT_SAMPLE_DELAY_MS,
    validate_config,
)


def relative_moisture_percent(raw, dry_raw, wet_raw):
    """Calibrated relative moisture (0-100) from one filtered raw count.
    The interpolation is sign-agnostic: on the measured bench polarity dry
    reads ABOVE wet (raw falls as the soil wets), and the guide's
    installation calibration can yield the reverse on another probe -- the
    same formula and the same 0/100 endpoints hold either way, because
    dry_raw and wet_raw were captured from the actual installation, not
    assumed. Out-of-span readings clamp to the endpoints instead of
    publishing percentages past the calibrated range. ``dry_raw ==
    wet_raw`` is unreachable: the validator rejects a zero span at the
    config boundary."""
    percent = (raw - dry_raw) * 100.0 / (wet_raw - dry_raw)
    if percent < 0.0:
        return 0.0
    if percent > 100.0:
        return 100.0
    return percent


class PlantMateSoil:
    """Low-level PlantMate measurement cycle.

    Owns the median sampling and the calibration conversion -- nothing
    else. The ``machine.ADC`` object is injected (Core 1's per-pin
    factory). Every operational failure (an ADC call raising) is
    normalized to ``OSError``; ``MemoryError`` escapes to the heap
    boundary.
    """

    def __init__(self, adc, dry_raw, wet_raw, sample_count, sample_delay_ms):
        self._adc = adc
        self._dry_raw = dry_raw
        self._wet_raw = wet_raw
        self._sample_count = sample_count
        self._sample_delay_ms = sample_delay_ms
        # Reusable sample buffer: allocated once (an odd-length list),
        # not per read.
        self._samples = [0] * sample_count

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
            raise OSError("PlantMate ADC read failed: {}".format(err))

    def _read_filtered_raw(self):
        """One filtered raw count: the median of ``sample_count`` samples
        spaced ``sample_delay_ms`` apart. The median (not the mean) rejects
        the occasional spike an analog cable run is exposed to; the small
        list is the guide's Pico-acceptable allocation. There is no
        discard-first sample (the yl69_fc28 discard follows its power-up,
        which this continuously powered probe has no equivalent of) and no
        settle (the guide forbids assuming one the manufacturer never
        published)."""
        samples = self._samples
        for index in range(self._sample_count):
            samples[index] = self._read_sample()
            if self._sample_delay_ms:
                time.sleep_ms(self._sample_delay_ms)
        ordered = sorted(samples)
        return ordered[len(ordered) // 2]

    def read(self):
        """One measurement: sample and convert. Returns ``(raw, percent)``
        where ``raw`` is the filtered count the calibration was applied to."""
        raw = self._read_filtered_raw()
        return raw, relative_moisture_percent(raw, self._dry_raw, self._wet_raw)


class PlantMateSoilDevice(Device):
    """``Device`` adapter for the PlantMate: shapes the telemetry sample.
    The calibration is the installation's, carried in the config -- there
    is no driver-side calibration state to apply (unlike the other
    devices' offsets)."""

    def __init__(self, adc):
        self._adc = adc
        self._sensor = None
        self._initialized = False

    def initialize(self, config):
        """Validate (shared pure rules) then build the sensor. Re-runnable:
        the read-failure reinit path calls this again; rebuilding over the
        same ADC channel is idempotent (the channel needs no setup beyond
        its construction)."""
        validate_config(config)

        self._sensor = PlantMateSoil(
            self._adc,
            config["dry_raw"],
            config["wet_raw"],
            config.get("sample_count", DEFAULT_SAMPLE_COUNT),
            config.get("sample_delay_ms", DEFAULT_SAMPLE_DELAY_MS),
        )
        self._initialized = True

    def read(self):
        """One telemetry sample. ``raw`` stays reported unmodified so a
        stuck ADC endpoint (a disconnected probe, a short, or a 5 V-
        damaged sensor reads near an endpoint) stays visible next to the
        percentage it drives."""
        if not self._initialized:
            raise RuntimeError("PlantMate device is not initialized")

        raw, percent = self._sensor.read()
        return {
            "raw": raw,
            "relative_moisture_percent": percent,
        }
