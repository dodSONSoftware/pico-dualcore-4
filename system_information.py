# system_information.py - Lean Core 1 software-sensor data source
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import gc
import machine
import os
import sys
import time

from hardware import classify_machine
from message_protocol import format_utc_epoch_ms
from version import FIRMWARE_NAME

# The reportable system-information sections (the get-details data source's
# full section set). Pure data, owned here with the data source.
SYSTEM_INFORMATION_SECTIONS = (
    "network",
    "memory",
    "runtime",
    "devices",
    "cpu",
    "machine",
    "communications",
    "queues",
    "device_status",
)

# machine.reset_cause() constant name -> stable short label. The mapping
# reads the constants off the machine module (no hardcoded values), so a
# build whose MicroPython names them differently degrades to "unknown"
# instead of mislabeling a boot. The v1.28 rp2 port exposes exactly these
# two causes. Field-verified on the flashed build (2026-09-18 capture,
# 6/6 commanded reboots): a soft machine.reset() reports the WDT_RESET
# value, not PWRON_RESET as 0.4.105 assumed — so "wdt" means "a reset via
# machine.reset() or a hardware-watchdog expiry", and the two are
# indistinguishable from this field alone. A commanded reboot is
# positively identified by the {"rebooting": true} acknowledgement Core 0
# publishes immediately before the reset; a "wdt" boot with no such
# preceding acknowledgement is unclassified within that WDT/software-reset
# family — a hardware-watchdog expiry or an unacknowledged commanded reset
# (the main.py recovery boundary publishes none).
_RESET_CAUSE_LABELS = (
    ("WDT_RESET", "wdt"),
    ("PWRON_RESET", "poweron"),
)



class SystemInformation:
    """Core 1 view of local runtime state plus immutable Core 0 snapshots."""

    def __init__(self, intercore, config):
        self._intercore = intercore
        self._config = config
        self._device_manager = None
        # How this boot began (machine.reset_cause()); captured once, on
        # first report — the value cannot change during a boot, so the
        # later reports are a cache hit, not re-reads.
        self._reset_cause = None
        # Lazily built ADC channel for the die temperature (the rp2 ADC is a
        # shared peripheral; the channel is a pin selection), kept after
        # construction instead of rebuilt per read.
        self._adc = None

    def get_reset_cause(self):
        """Stable short label for how this boot began: "wdt" for the
        WDT_RESET value, "poweron" for the PWRON_RESET value, else
        "unknown" — the v1.28 rp2 port reports no finer cause. Field-
        verified on the flashed build: a machine.reset() reboot reports
        "wdt", not "poweron" — so "wdt" covers both a deliberate
        machine.reset() and a hardware-watchdog expiry. A commanded
        reboot is positively identified by the {"rebooting": true}
        acknowledgement Core 0 publishes immediately before it; a "wdt"
        boot with no such preceding acknowledgement is unclassified
        within that WDT/software-reset family — a hardware-watchdog
        expiry or an unacknowledged commanded reset (the main.py
        recovery boundary publishes none), not a confirmed watchdog fire."""
        if self._reset_cause is None:
            try:
                cause = machine.reset_cause()
            except MemoryError:
                raise
            except Exception:
                cause = None
            label = "unknown"
            if cause is not None:
                for name, value in _RESET_CAUSE_LABELS:
                    if getattr(machine, name, None) == cause:
                        label = value
                        break
            self._reset_cause = label
        return self._reset_cause

    def set_device_manager(self, device_manager):
        self._device_manager = device_manager

    def _network_snapshot(self):
        snapshot = self._intercore.state_mailboxes.get_network_snapshot()
        if snapshot is None:
            raise RuntimeError("Core 0 network snapshot is unavailable")
        return snapshot

    def get_network(self):
        snapshot = self._network_snapshot()
        return {
            "ssid": snapshot.get("ssid"),
            "ip_address": snapshot.get("ip_address"),
            "rssi": snapshot.get("rssi"),
            "netmask": snapshot.get("netmask"),
            "gateway": snapshot.get("gateway"),
            "dns": snapshot.get("dns"),
        }

    def get_memory(self):
        try:
            allocated = gc.mem_alloc()
            free = gc.mem_free()
        except MemoryError:
            raise
        except Exception:
            allocated = None
            free = None

        return {
            "heap_alloc_bytes": allocated,
            "heap_free_bytes": free,
            "heap_total_bytes": None if allocated is None else allocated + free,
        }

    def get_runtime(self):
        utc = self._intercore.state_mailboxes.get_utc_snapshot()
        start_time = None
        if utc is not None:
            start_time = format_utc_epoch_ms(utc.get("runtime_start_epoch_ms"))
        return {
            "read_loop_sec": self._config["read_loop_sec"] if self._config is not None else None,
            "start_time": start_time,
        }

    def get_device_sections(self):
        """Both device report sections from one snapshot walk (they share a
        single status-snapshot source)."""
        if self._device_manager is None:
            return {"devices": {"configured": 0, "active": 0}, "device_status": []}
        return self._device_manager.get_status_snapshot(now_ms=time.ticks_ms())

    def get_device_counts(self):
        """The device counts without the per-device snapshot walk: the health
        message needs only the two counts, while get-device-sections' full
        walk (per-device dicts, ages, failure reasons) exists for get-details."""
        if self._device_manager is None:
            return {"configured": 0, "active": 0, "initialization_failed": 0}
        return self._device_manager.get_device_counts()

    def get_device_status(self):
        return self.get_device_sections()["device_status"]

    def get_cpu_temperature(self):
        # The rp2 port exposes the die sensor only as an ADC channel (no
        # machine.temperature() binding); read_u16() returns the 12-bit
        # reading scaled to 16 bits, so scale it back. The RP2040 and RP2350
        # datasheets state the same calibration (Vbe = 0.706 V at 27 C, slope
        # -1.721 mV/C), so one formula serves both boards. The conversion is
        # VREF-sensitive (~4 C per 1% VREF): a trend indicator at roughly
        # +/-5 C, not a calibrated absolute.
        # The channel object is built once and kept: constructing an ADC per
        # read puts a GC-managed allocation on every health message, on
        # Core 1's most memory-sensitive path. A failed construction leaves
        # the cache empty, so a broken channel is retried on each call as
        # before (a failed RHS never assigns self._adc).
        adc = self._adc
        if adc is None:
            try:
                adc = self._adc = machine.ADC(machine.ADC.CORE_TEMP)
            except MemoryError:
                raise
            except Exception:
                return None
        try:
            raw = adc.read_u16() >> 4
        except MemoryError:
            raise
        except Exception:
            return None
        voltage = raw * 3.3 / 4096
        return round(27 - (voltage - 0.706) / 0.001721, 1)

    def get_cpu(self):
        try:
            frequency_hz = machine.freq()
        except MemoryError:
            raise
        except Exception:
            frequency_hz = None
        return {"frequency_hz": frequency_hz, "temperature_c": self.get_cpu_temperature()}

    def get_machine(self):
        try:
            uname = os.uname()
            machine_name = uname.machine
            version = uname.version
        except MemoryError:
            raise
        except Exception:
            machine_name = "unknown"
            version = "unknown"

        # Classify via the shared hardware policy (single source of truth for
        # machine-string -> board type and heap thresholds).
        classification = classify_machine(machine_name)
        return {
            "hardware_type": classification["hardware_type"],
            "machine": machine_name,
            # The product codename — the firmware's own identity alongside
            # the board's.
            "firmware_name": FIRMWARE_NAME,
            "version": version,
            "implementation": sys.implementation.name,
            "preferred_free_heap_bytes": classification["preferred_free_heap_bytes"],
            "minimum_free_heap_bytes": classification["minimum_free_heap_bytes"],
            "reset_cause": self.get_reset_cause(),
        }

    def get_communications(self):
        snapshot = self._network_snapshot()
        return {
            "wifi_connected": bool(snapshot.get("wifi_connected")),
            "mqtt_connected": bool(snapshot.get("mqtt_connected")),
            "wifi_connect_count": snapshot.get("wifi_connect_count", 0),
            "wifi_disconnect_count": snapshot.get("wifi_disconnect_count", 0),
            "mqtt_connect_count": snapshot.get("mqtt_connect_count", 0),
            "mqtt_disconnect_count": snapshot.get("mqtt_disconnect_count", 0),
            "mqtt_last_disconnect_reason": snapshot.get("mqtt_last_disconnect_reason"),
        }

    def get_queues(self):
        # Observability metrics only: both queues are heap-governed, so there
        # is no fixed capacity to report.
        outbound = self._intercore.outbound_queue.status()
        events = self._intercore.event_queue.status()
        return {
            "outbound_pending": outbound["pending"],
            "outbound_high_watermark": outbound["high_watermark"],
            "outbound_high_watermark_bytes": outbound["high_watermark_bytes"],
            "outbound_evicted": outbound["messages_evicted"],
            "telemetry_evicted": outbound["telemetry_evicted"],
            "outbound_rejected": outbound["messages_rejected"],
            "outbound_queued_bytes": outbound["queued_bytes"],
            "intercore_events_pending": events["pending"],
            "intercore_events_high_watermark": events["high_watermark"],
            "intercore_events_rejected": events["rejected"],
        }
