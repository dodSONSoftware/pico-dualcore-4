# system_information.py - Lean Core 1 software-sensor data source
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import gc
import machine
import os
import sys
import time

from hardware import classify_machine, RESET_CAUSE_UNKNOWN
from message_protocol import format_utc_epoch_ms
from observability import BOOT_REASON_UNKNOWN
from version import FIRMWARE_BUILD_COMMIT, FIRMWARE_VERSION, MESSAGE_SCHEMA_VERSION

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
    "configuration",
    "capabilities",
)

# The small static feature tuple: only capabilities this firmware implements
# are listed. runtime_configuration is implemented (read_config / write_config
# with staged, transactional persistence); TLS and broker failover are
# deliberately absent (not implemented); the watchdog is Core 0 recovery
# behavior, not a user-facing capability.
FIRMWARE_FEATURE_CAPABILITIES = (
    "health",
    "commands",
    "mqtt_qos1",
    "outage_buffering",
    "network_diagnostics",
    "heap_pressure_queue",
    "runtime_configuration",
)



class SystemInformation:
    """Core 1 view of local runtime state plus immutable Core 0 snapshots."""

    def __init__(self, intercore, config, runtime_id=None):
        self._intercore = intercore
        self._config = config
        self._device_manager = None
        # Runtime identity (the envelope's runtime_id); reported in the
        # runtime section for this runtime only -- the envelope stays the
        # authority on the wire.
        self._runtime_id = runtime_id

    def set_device_manager(self, device_manager):
        self._device_manager = device_manager

    def _network_snapshot(self):
        snapshot = self._intercore.state_mailboxes.get_network_snapshot()
        if snapshot is None:
            raise RuntimeError("Core 0 network snapshot is unavailable")
        return snapshot

    def get_network(self):
        snapshot = self._network_snapshot()
        # The section's rssi key is reported under the canonical name
        # wifi_rssi_dbm (the snapshot's rssi key), and dns as dns_server.
        # Diagnostic fields are null-tolerant: null means not-yet-tested or
        # unsupported (never converted to false).
        return {
            "ssid": snapshot.get("ssid"),
            "ip_address": snapshot.get("ip_address"),
            "wifi_rssi_dbm": snapshot.get("rssi"),
            "wifi_rssi_min_dbm": snapshot.get("wifi_rssi_min_dbm"),
            "wifi_rssi_max_dbm": snapshot.get("wifi_rssi_max_dbm"),
            "wifi_rssi_moving_average_dbm": snapshot.get("wifi_rssi_moving_average_dbm"),
            "wifi_rssi_sample_count": snapshot.get("wifi_rssi_sample_count"),
            "netmask": snapshot.get("netmask"),
            "gateway": snapshot.get("gateway"),
            "dns_server": snapshot.get("dns"),
            "wifi_bssid": snapshot.get("wifi_bssid"),
            "wifi_channel": snapshot.get("wifi_channel"),
            "wifi_association_details_supported": snapshot.get(
                "wifi_association_details_supported"),
            "gateway_reachability_supported": snapshot.get(
                "gateway_reachability_supported"),
            "gateway_reachable": snapshot.get("gateway_reachable"),
            "gateway_last_latency_ms": snapshot.get("gateway_last_latency_ms"),
            "dns_reachable": snapshot.get("dns_reachable"),
            "dns_last_latency_ms": snapshot.get("dns_last_latency_ms"),
            "mqtt_broker_last_round_trip_ms": snapshot.get(
                "mqtt_broker_last_round_trip_ms"),
            "network_diagnostics_last_run_age_ms": snapshot.get(
                "network_diagnostics_last_run_age_ms"),
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

        # Submit this reading as a checkpoint to the shared MemoryStats. This
        # intentionally does NOT call gc.collect(): get_memory must not collect
        # just to make the numbers look cleaner. The low-watermark minimum and
        # the controlled-GC statistics are reported from that one shared object
        # (the single source of truth), not a Core 1-private copy.
        memory_stats = self._intercore.memory_stats
        if isinstance(free, int) and not isinstance(free, bool) and free >= 0:
            memory_stats.observe_free_heap(free)
        snapshot = memory_stats.snapshot()

        return {
            "heap_alloc_bytes": allocated,
            "heap_free_bytes": free,
            "heap_total_bytes": None if allocated is None else allocated + free,
            "minimum_free_heap_observed_bytes": snapshot["minimum_free_heap_observed_bytes"],
            "gc_collect_count": snapshot["gc_collect_count"],
            "gc_bytes_reclaimed": snapshot["gc_bytes_reclaimed"],
            "gc_total_reclaimed_bytes": snapshot["gc_total_reclaimed_bytes"],
            "gc_last_duration_ms": snapshot["gc_last_duration_ms"],
            "gc_max_duration_ms": snapshot["gc_max_duration_ms"],
        }

    def get_runtime(self):
        utc = self._intercore.state_mailboxes.get_utc_snapshot()
        start_time = None
        if utc is not None:
            start_time = format_utc_epoch_ms(utc.get("runtime_start_epoch_ms"))
        return {
            "read_loop_sec": self._config["read_loop_sec"] if self._config is not None else None,
            "start_time": start_time,
            # Runtime identity for this boot (version identity is the
            # envelope's on the wire; these are the detailed-diagnostics
            # copies for the runtime section). runtime_id is null until
            # provided by the runtime (host-side builds may not carry one).
            "firmware_version": FIRMWARE_VERSION,
            "firmware_build_commit": FIRMWARE_BUILD_COMMIT,
            "message_schema_version": MESSAGE_SCHEMA_VERSION,
            "runtime_id": self._runtime_id,
        }

    def _device_snapshot(self):
        if self._device_manager is None:
            return {"devices": {"configured": 0, "active": 0}, "device_status": []}
        return self._device_manager.get_status_snapshot(now_ms=time.ticks_ms())

    def get_devices(self):
        return self._device_snapshot()["devices"]

    def get_device_status(self):
        return self._device_snapshot()["device_status"]

    def get_capabilities(self):
        """Compact capabilities section.

        Devices are the supported type NAMES from the factory registry (not
        configured instance ids); features is the small static tuple of what
        this firmware implements.
        """
        # Lazy import: device_factory imports the system-information device,
        # which imports this module -- a top-level import would be circular.
        from device_factory import supported_device_types
        return {
            "devices": list(supported_device_types()),
            "features": list(FIRMWARE_FEATURE_CAPABILITIES),
        }

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
        # machine-string -> board type and board-specific heap reserve).
        classification = classify_machine(machine_name)

        # The reset cause describes the boot event: it is captured once at
        # startup (main publishes it in the hardware snapshot) and is never
        # re-read from machine here. An unavailable snapshot or field
        # degrades to "unknown" instead of failing the read.
        last_reset_cause = RESET_CAUSE_UNKNOWN
        boot_reason = BOOT_REASON_UNKNOWN
        try:
            snapshot = self._intercore.state_mailboxes.get_hardware()
        except MemoryError:
            raise
        except Exception:
            snapshot = None
        if isinstance(snapshot, dict):
            last_reset_cause = snapshot.get("last_reset_cause") or RESET_CAUSE_UNKNOWN
            boot_reason = snapshot.get("boot_reason") or BOOT_REASON_UNKNOWN

        return {
            "hardware_type": classification["hardware_type"],
            "machine": machine_name,
            "version": version,
            "implementation": sys.implementation.name,
            "minimum_free_heap_bytes": classification["minimum_free_heap_bytes"],
            "last_reset_cause": last_reset_cause,
            "boot_reason": boot_reason,
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
            # MQTT reliability metrics (sourced from the Core 0 network
            # snapshot; an older/incomplete snapshot falls back to integer
            # zero, never null).
            "mqtt_publish_attempt_count": snapshot.get("mqtt_publish_attempt_count", 0),
            "mqtt_publish_retry_count": snapshot.get("mqtt_publish_retry_count", 0),
            "mqtt_puback_timeout_count": snapshot.get("mqtt_puback_timeout_count", 0),
            "mqtt_connection_failure_count": snapshot.get("mqtt_connection_failure_count", 0),
            "mqtt_reconnect_success_count": snapshot.get("mqtt_reconnect_success_count", 0),
            "mqtt_last_reconnect_duration_ms": snapshot.get("mqtt_last_reconnect_duration_ms", 0),
            "mqtt_last_outage_duration_ms": snapshot.get("mqtt_last_outage_duration_ms", 0),
            # Wi-Fi quality / diagnostics history (Core 0 owns; informational).
            "wifi_last_reconnect_duration_ms": snapshot.get("wifi_last_reconnect_duration_ms", 0),
            "wifi_last_dhcp_acquisition_duration_ms": snapshot.get(
                "wifi_last_dhcp_acquisition_duration_ms", 0),
            "wifi_last_status_reason": snapshot.get("wifi_last_status_reason", "unknown"),
            "wifi_last_reconnect_trigger": snapshot.get("wifi_last_reconnect_trigger", "unknown"),
            "network_diagnostics_run_count": snapshot.get("network_diagnostics_run_count", 0),
            "mqtt_broker_latency_enabled": snapshot.get("mqtt_broker_latency_enabled", False),
        }

    def get_queues(self):
        outbound = self._intercore.outbound_queue.status()
        events = self._intercore.event_queue.status()
        # Core 0 post-outage drain metrics (historical/observational only).
        # Read None-tolerantly: before Core 0's first publication no snapshot
        # exists yet and the safe defaults below stand in.
        snapshot = self._intercore.state_mailboxes.get_network_snapshot()
        if snapshot is None:
            snapshot = {}
        return {
            "outbound_pending": outbound["pending"],
            # The fixed entry ceiling (sanity guard), not a user-tunable size.
            "outbound_safety_max_entries": outbound["max_entries"],
            "outbound_high_watermark": outbound["high_watermark"],
            "outbound_evicted": outbound["messages_evicted"],
            "telemetry_evicted": outbound["telemetry_evicted"],
            "outbound_rejected": outbound["messages_rejected"],
            "outbound_queued_bytes": outbound["queued_bytes"],
            "intercore_events_pending": events["pending"],
            "intercore_events_max": events["max"],
            "outbound_queue_drain_active": snapshot.get("outbound_queue_drain_active", False),
            "outbound_queue_last_drain_start_depth": snapshot.get(
                "outbound_queue_last_drain_start_depth", 0),
            "outbound_queue_last_drain_message_count": snapshot.get(
                "outbound_queue_last_drain_message_count", 0),
            "outbound_queue_last_drain_duration_ms": snapshot.get(
                "outbound_queue_last_drain_duration_ms", 0),
            "outbound_queue_last_drain_rate_per_sec": snapshot.get(
                "outbound_queue_last_drain_rate_per_sec", 0),
        }

    def get_configuration(self):
        """Compact committed-configuration section (never the full config).

        Reads the shared ConfigState: schema version, firmware-managed
        generation, the SHA-256 checksum of the committed config.json bytes,
        and the RESTART_REQUIRED bookkeeping. This section is observational
        (invariant 9): it adds no degraded reason. When the shared state is
        absent (older test doubles) the fields are null -- null, never false.
        """
        state = getattr(self._intercore, "config_state", None)
        if state is None:
            return {
                "config_schema_version": None,
                "config_generation": None,
                "config_checksum_sha256": None,
                "reboot_required": None,
                "pending_restart_keys": None,
            }
        return state.snapshot()
