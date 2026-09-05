# system_information.py - Lean Core 1 software-sensor data source
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import gc
import machine
import os
import sys
import time

from devices.system_information.validation import SYSTEM_INFORMATION_SECTIONS
from hardware import classify_machine
from message_protocol import format_utc_epoch_ms



class SystemInformation:
    """Core 1 view of local runtime state plus immutable Core 0 snapshots."""

    def __init__(self, intercore, config):
        self._intercore = intercore
        self._config = config
        self._device_manager = None

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

    def get_devices(self):
        return self.get_device_sections()["devices"]

    def get_device_status(self):
        return self.get_device_sections()["device_status"]

    def get_cpu(self):
        try:
            frequency_hz = machine.freq()
        except MemoryError:
            raise
        except Exception:
            frequency_hz = None
        return {"frequency_hz": frequency_hz}

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
            "version": version,
            "implementation": sys.implementation.name,
            "preferred_free_heap_bytes": classification["preferred_free_heap_bytes"],
            "minimum_free_heap_bytes": classification["minimum_free_heap_bytes"],
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
