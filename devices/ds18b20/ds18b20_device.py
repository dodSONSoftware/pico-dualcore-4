# devices/ds18b20/ds18b20_device.py - DS18B20 temperature device
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Analog Devices DS18B20 (digital thermometer) over 1-Wire.

The sensor protocol (bus scan, ROM identification, the two-stage
convert/wait/read cycle, read validation) lives in the low-level ``DS18B20``
class; ``DS18B20Device`` is the ``Device`` adapter that applies the
application-layer policy -- the user offset. The ``ds18x20.DS18X20`` bus
object is injected (Core 1 owns it); this module imports no
``machine``/``onewire``/``ds18x20`` API, so it stays host-importable.

A DS18B20 reading is a **two-stage operation**: ``convert_temp()`` starts the
conversion (up to 750 ms at the power-on 12-bit resolution), and only then
does ``read_temp()`` return a fresh value. The scratchpad holds the previous
value (85 °C after a power cycle) until the conversion completes, so the wait
is part of the protocol -- and 85.0 °C is a valid real temperature, never an
error code. The reference is the DS18B20 data sheet (Rev. 6); where guidance
disagrees with the data sheet, the data sheet wins.

The config's ``rom`` field is the sensor's 64-bit factory-programmed ID: it
is unique per sensor, not derivable from the wiring, and must be read from
the bus. In the MicroPython REPL (before the firmware starts, on the pin the
sensor's data line is connected to, e.g. 2):

    from machine import Pin
    import onewire, ds18x20
    for rom in ds18x20.DS18X20(onewire.OneWire(Pin(2))).scan():
        print(rom.hex())

One line is printed per sensor on the bus; copy the 16-character hex (it
starts with the family code ``28``) into ``rom``. The scan order is not a
stable identity -- with more than one sensor on the pin, identify the target
physically (e.g. by disconnecting the others one at a time).
"""

import time

from devices.device import Device
from devices.ds18b20.validation import (
    DEFAULT_CONVERSION_MS,
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


# The sensor's documented temperature range (Rev. 6). A value outside it is
# a corrupted or absent reading -- the scratchpad's two's-complement field
# can hold more, but the sensor is only specified here.
_MIN_TEMPERATURE_C = const(-55.0)
_MAX_TEMPERATURE_C = const(125.0)

class DS18B20:
    """Low-level DS18B20 protocol over the injected 1-Wire bus.

    Owns the ROM identification and the two-stage convert/wait/read cycle
    only -- not offsets. The ``ds18x20.DS18X20`` object is injected.
    """

    def __init__(self, ds, rom_hex, conversion_ms):
        self._ds = ds
        # rom_hex is 16 lowercase hex characters (the validator's canonical
        # form); rom is the 8-byte ROM used for the bus match.
        self._rom = bytes.fromhex(rom_hex)
        self._rom_label = rom_hex
        self._conversion_ms = conversion_ms
        # The scan result for this ROM (the exact object the bus driver
        # returned -- read_temp wants the form scan() yields).
        self._matched_rom = None

    # --- Initialization sequence -------------------------------------------

    def init(self):
        """Scan the bus and identify this sensor by its ROM. Re-runnable: the
        read-failure reinit path repeats this over the held bus, so a
        previously absent or disconnected sensor is found again by its ROM --
        never by its position in the scan list (the scan order is not a
        permanent identity)."""
        roms = self._scan()
        for rom in roms:
            if bytes(rom) == self._rom:
                self._matched_rom = rom
                return
        raise OSError(
            "DS18B20 ROM {} not found on bus ({} device(s) present)"
            .format(self._rom_label, len(roms))
        )

    def _scan(self):
        try:
            return list(self._ds.scan())
        except MemoryError:
            raise
        except OSError as err:
            raise OSError("DS18B20 bus scan failed: {}".format(err))

    # --- Measurement --------------------------------------------------------

    def _wait_conversion(self):
        """The conversion-completion wait: one uninterrupted sleep of the
        configured window. The pin stays configured but undriven for the
        whole wait -- the DQ line is open-drain, so an idle pin is high-Z
        and a shared (multi-drop) line remains free for any other master
        while the conversion runs. There is no pin to release: the
        documented MicroPython ds18x20 API exposes no release, and the
        sensor converts on its VDD supply (the documented wiring), not from
        the data line. The validator's 1000 ms ceiling on the window is
        what keeps this one sleep inside the Core 1 liveness budget."""
        time.sleep_ms(self._conversion_ms)

    def read(self):
        """One sample in Celsius: start the conversion, wait the configured
        conversion window (750 ms at the power-on 12-bit default), then
        read the scratchpad.
        The wait must cover the 12-bit maximum conversion time -- the driver
        never configures the sensor's resolution, so a shorter wait would
        read the previous scratchpad value (85 °C after a power cycle),
        which is indistinguishable from a real 85 °C."""
        try:
            self._ds.convert_temp()
        except MemoryError:
            raise
        except Exception as err:
            # Same operational boundary as the read_temp call below (whose
            # comment carries the rationale).
            raise OSError("DS18B20 convert failed: {}".format(err))
        self._wait_conversion()

        try:
            value = self._ds.read_temp(self._matched_rom)
        except MemoryError:
            raise
        except Exception as err:
            # The injected bus's failure domain is operational: MicroPython's
            # ds18x20 module raises a bare Exception on a scratchpad CRC
            # failure (bit corruption in transit -- a stale scratchpad is
            # still CRC-valid, so a CRC failure is not a stale read), and a
            # hardware failure must normalize to OSError like every other
            # driver's, or it escapes to Core 1's worker boundary and resets
            # the device over one flaky read.
            raise OSError(
                "DS18B20 ROM {} read failed: {}".format(self._rom_label, err)
            )

        if value is None:
            raise OSError(
                "DS18B20 ROM {} returned no temperature".format(self._rom_label)
            )
        # Range gate: outside the documented range (or non-finite, which the
        # comparison rejects) the reading is corrupt -- reject it as an
        # operational failure instead of publishing it. The power-on value
        # 85.0 °C sits inside the range and must NOT be singled out: it is a
        # valid real temperature, and the correct software is the
        # convert->wait->read sequence, not an 85.0 filter.
        if not _MIN_TEMPERATURE_C <= value <= _MAX_TEMPERATURE_C:
            raise OSError(
                "DS18B20 ROM {} returned out-of-range temperature {}".format(
                    self._rom_label, value
                )
            )
        return float(value)


class DS18B20Device(Device):
    """``Device`` adapter for the DS18B20: applies the application-layer
    policy (the user offset on the converted channel) on top of the
    low-level sensor's reading."""

    def __init__(self, ds):
        self._ds = ds
        self._sensor = None
        self._offset_temperature_c = 0.0
        self._initialized = False

    def initialize(self, config):
        """Validate (shared pure rules) then identify the sensor on the bus.
        Re-runnable: the read-failure reinit path calls this again to rescan
        and re-identify by ROM over the held bus."""
        validate_config(config)

        offsets = config.get("offsets", {})
        self._offset_temperature_c = offsets.get("temperature_c", 0)

        self._sensor = DS18B20(
            self._ds,
            config["rom"].lower(),
            config.get("conversion_ms", DEFAULT_CONVERSION_MS),
        )
        self._sensor.init()
        self._initialized = True

    def read(self):
        """One telemetry sample. The offset is applied after the read (the
        data sheet keeps calibration policy out of the raw measurement); the
        driver stays in Celsius -- the presentation layer converts units."""
        if not self._initialized:
            raise RuntimeError("DS18B20 device is not initialized")

        temperature_c = self._sensor.read()
        return {"temperature_c": temperature_c + self._offset_temperature_c}
