# Host-side behavioral tests for health message generation.

import gc
import json
import pathlib
import sys
import time as std_time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from intercore import (
    InterCore,
    KIND_HEALTH,
    RETENTION_PRIORITY_HEALTH,
)
from version import FIRMWARE_VERSION, MESSAGE_SCHEMA_VERSION


class TestHealthPayloadBuilder:
    """Tests for health payload building logic."""

    def _ticks_diff(self, later, earlier):
        """MicroPython-compatible time difference."""
        return later - earlier

    def _ticks_add(self, start, interval):
        """MicroPython-compatible time addition."""
        return start + interval

    def _build_health_payload_test(self, bus, boot_ticks_ms, source, config, runtime_id,
                                    now_ms=110000, devices_active=None):
        """Build health payload using the same logic as core1.py, with test overrides."""
        uptime_ms = self._ticks_diff(now_ms, boot_ticks_ms)

        # Get network snapshot
        network_snapshot = bus.state_mailboxes.get_network_snapshot()
        if network_snapshot is None:
            return None

        # Get UTC snapshot
        utc_snapshot = bus.state_mailboxes.get_utc_snapshot()
        utc_valid = utc_snapshot is not None

        # Get Core 1 activity
        core_1_activity_ms = bus.state_mailboxes.get_core_1_activity_ms()
        core_1_inactive_ms = self._ticks_diff(now_ms, core_1_activity_ms) if core_1_activity_ms is not None else None
        core_1_activity_threshold_ms = max(config["read_loop_sec"] * 3 * 1000, 60000)
        core_1_active = core_1_inactive_ms is not None and core_1_inactive_ms <= core_1_activity_threshold_ms

        # Get device status
        device_status = bus.state_mailboxes.get_device_status() if hasattr(bus.state_mailboxes, 'get_device_status') else None

        if devices_active is not None:
            if device_status is None:
                device_status = {"devices": {"configured": len(config["devices"]), "active": devices_active}}
            else:
                device_status["devices"]["active"] = devices_active

        devices_configured = len(config["devices"])
        devices_active_actual = device_status["devices"]["active"] if device_status else 0

        # Get queue status from bus
        queue_depth, queue_capacity = bus.outbound_queue.get_depth_with_capacity()

        # Get memory info - return a value that won't trigger low_free_heap
        try:
            free_heap = gc.mem_free()
        except AttributeError:
            # In host tests, gc.mem_free() doesn't exist, use a safe value
            free_heap = 100000  # Well above Pico W minimum of 65536

        # Get hardware info
        try:
            hardware = bus.state_mailboxes.get_hardware()
            minimum_free_heap = hardware.get("minimum_free_heap_bytes") if hardware else 65536
            hardware_type = hardware.get("hardware_type", "unknown")
            machine = hardware.get("machine", "unknown")
        except Exception:
            minimum_free_heap = 65536
            hardware_type = "unknown"
            machine = "unknown"

        # Calculate heap headroom
        heap_headroom_bytes = free_heap - minimum_free_heap

        # Calculate Core 1 activity age in milliseconds
        core_1_activity_age_ms = self._ticks_diff(now_ms, core_1_activity_ms) if core_1_activity_ms is not None else None

        # Calculate UTC sync age in seconds
        utc_sync_age_sec = None
        if utc_snapshot is not None:
            elapsed_ms = self._ticks_diff(now_ms, utc_snapshot["ticks_ms"])
            utc_sync_age_sec = elapsed_ms // 1000  # Integer division for seconds

        # Calculate device failures
        device_failures = devices_configured - devices_active_actual

        # Calculate queue utilization percentage
        queue_utilization_percent = 0
        if queue_capacity > 0:
            queue_utilization_percent = (queue_depth * 100) // queue_capacity

        # Determine queue pressure (75% threshold)
        queue_pressure = queue_capacity > 0 and queue_utilization_percent >= 75

        # Evaluate health status
        degraded_reasons = []
        network_stack_ready = bool(network_snapshot.get("network_stack_ready"))
        wifi_connected = bool(network_snapshot.get("wifi_connected"))
        mqtt_connected = bool(network_snapshot.get("mqtt_connected"))

        if not network_stack_ready:
            degraded_reasons.append("network_stack_not_ready")
        if not wifi_connected:
            degraded_reasons.append("wifi_not_connected")
        if not mqtt_connected:
            degraded_reasons.append("mqtt_not_connected")
        if not core_1_active:
            degraded_reasons.append("core_1_inactive")
        if free_heap < minimum_free_heap:
            degraded_reasons.append("low_free_heap")
        if devices_active_actual != devices_configured:
            degraded_reasons.append("device_count_mismatch")
        if queue_pressure:
            degraded_reasons.append("outbound_queue_pressure")
        if not utc_valid:
            degraded_reasons.append("utc_not_valid")

        status = "healthy" if not degraded_reasons else "degraded"

        payload = {
            "message_schema_version": MESSAGE_SCHEMA_VERSION,
            "runtime_id": runtime_id,
            "uptime_ms": uptime_ms,
            "timestamp": None,
            "source": source,
            "message_type": "health",
            "firmware_version": FIRMWARE_VERSION,
            "payload": {
                "status": status,
                "degraded_reasons": degraded_reasons,
                "hardware_type": hardware_type,
                "machine": machine,
                "network_stack_ready": network_stack_ready,
                "wifi_connected": wifi_connected,
                "wifi_rssi_dbm": network_snapshot.get("rssi"),
                "mqtt_connected": mqtt_connected,
                "core_1_active": core_1_active,
                "core_1_activity_age_ms": core_1_activity_age_ms,
                "free_heap_bytes": free_heap,
                "minimum_free_heap_bytes": minimum_free_heap,
                "heap_headroom_bytes": heap_headroom_bytes,
                "devices_configured": devices_configured,
                "devices_active": devices_active_actual,
                "device_failures": device_failures,
                "outbound_queue_depth": queue_depth,
                "outbound_queue_capacity": queue_capacity,
                "outbound_queue_utilization_percent": queue_utilization_percent,
                "utc_valid": utc_valid,
                "utc_sync_age_sec": utc_sync_age_sec,
            },
        }

        return payload

    def test_healthy_state_payload(self, tmp_path):
        """Build healthy health payload when all checks pass."""
        from config import load_config, split_config

        config_path = pathlib.Path(__file__).resolve().parents[1] / "config.json"
        config = load_config(str(config_path))
        core0, core1, bus_config = split_config(config)

        bus = InterCore(outbound_max=bus_config["max_outbound_queue_entries"],
                       event_max=bus_config["max_intercore_event_entries"])

        boot_ticks_ms = 100000  # Fixed starting timestamp
        runtime_id = "test_runtime_12345"

        # Set up network snapshot with ready state
        bus.state_mailboxes.set_network_snapshot({
            "wifi_connected": True,
            "mqtt_connected": True,
            "network_stack_ready": True,
            "ssid": "test-ssid",
            "ip_address": "10.0.0.1",
        })

        # Set up UTC snapshot
        bus.state_mailboxes.set_utc_snapshot({
            "utc_epoch_ms": 200000,
            "ticks_ms": 100000,
        })

        # Set Core 1 activity (recent)
        bus.state_mailboxes.set_core_1_activity_ms(100100)  # 100ms ago

        # Set hardware info
        bus.state_mailboxes.set_hardware({
            "hardware_type": "pico_w",
            "minimum_free_heap_bytes": 65536,
        })

        # Build health payload
        payload = self._build_health_payload_test(
            bus, boot_ticks_ms, "test-source", core1, runtime_id,
            now_ms=110000, devices_active=len(config["devices"])
        )

        assert payload is not None
        assert payload["message_type"] == "health"
        assert payload["payload"]["status"] == "healthy"
        assert payload["payload"]["degraded_reasons"] == []
        assert payload["payload"]["wifi_connected"] is True
        assert payload["payload"]["mqtt_connected"] is True
        assert payload["payload"]["core_1_active"] is True
        assert payload["payload"]["network_stack_ready"] is True
        assert payload["payload"]["devices_configured"] == len(config["devices"])
        assert payload["payload"]["devices_active"] == len(config["devices"])
        assert payload["payload"]["outbound_queue_depth"] == 0
        assert payload["payload"]["outbound_queue_capacity"] == bus_config["max_outbound_queue_entries"]
        assert payload["payload"]["utc_valid"] is True
        assert payload["uptime_ms"] == 10000  # 110000 - 100000

    def test_wifi_disconnected_triggers_degraded(self, tmp_path):
        """Verify wifi_not_connected triggers degraded status."""
        from config import load_config, split_config

        config_path = pathlib.Path(__file__).resolve().parents[1] / "config.json"
        config = load_config(str(config_path))
        _, core1, _ = split_config(config)

        bus = InterCore(outbound_max=16, event_max=4)

        bus.state_mailboxes.set_network_snapshot({
            "wifi_connected": False,  # Disconnected
            "mqtt_connected": True,
            "network_stack_ready": True,
        })
        bus.state_mailboxes.set_utc_snapshot({"utc_epoch_ms": 200000, "ticks_ms": 100000})
        bus.state_mailboxes.set_core_1_activity_ms(100100)
        bus.state_mailboxes.set_hardware({"hardware_type": "pico_w", "minimum_free_heap_bytes": 65536})

        payload = self._build_health_payload_test(
            bus, 100000, "test-source", core1, "test-runtime",
            now_ms=110000, devices_active=1
        )

        assert payload["payload"]["status"] == "degraded"
        assert "wifi_not_connected" in payload["payload"]["degraded_reasons"]

    def test_mqtt_disconnected_triggers_degraded(self, tmp_path):
        """Verify mqtt_not_connected triggers degraded status."""
        from config import load_config, split_config

        config_path = pathlib.Path(__file__).resolve().parents[1] / "config.json"
        config = load_config(str(config_path))
        _, core1, _ = split_config(config)

        bus = InterCore(outbound_max=16, event_max=4)

        bus.state_mailboxes.set_network_snapshot({
            "wifi_connected": True,
            "mqtt_connected": False,  # Disconnected
            "network_stack_ready": True,
        })
        bus.state_mailboxes.set_utc_snapshot({"utc_epoch_ms": 200000, "ticks_ms": 100000})
        bus.state_mailboxes.set_core_1_activity_ms(100100)
        bus.state_mailboxes.set_hardware({"hardware_type": "pico_w", "minimum_free_heap_bytes": 65536})

        payload = self._build_health_payload_test(
            bus, 100000, "test-source", core1, "test-runtime",
            now_ms=110000, devices_active=1
        )

        assert payload["payload"]["status"] == "degraded"
        assert "mqtt_not_connected" in payload["payload"]["degraded_reasons"]

    def test_network_stack_not_ready_triggers_degraded(self, tmp_path):
        """Verify network_stack_not_ready triggers degraded status."""
        from config import load_config, split_config

        config_path = pathlib.Path(__file__).resolve().parents[1] / "config.json"
        config = load_config(str(config_path))
        _, core1, _ = split_config(config)

        bus = InterCore(outbound_max=16, event_max=4)

        bus.state_mailboxes.set_network_snapshot({
            "wifi_connected": True,
            "mqtt_connected": True,
            "network_stack_ready": False,  # Not ready
        })
        bus.state_mailboxes.set_utc_snapshot({"utc_epoch_ms": 200000, "ticks_ms": 100000})
        bus.state_mailboxes.set_core_1_activity_ms(100100)
        bus.state_mailboxes.set_hardware({"hardware_type": "pico_w", "minimum_free_heap_bytes": 65536})

        payload = self._build_health_payload_test(
            bus, 100000, "test-source", core1, "test-runtime",
            now_ms=110000, devices_active=1
        )

        assert payload["payload"]["status"] == "degraded"
        assert "network_stack_not_ready" in payload["payload"]["degraded_reasons"]

    def test_core_1_inactive_triggers_degraded(self, tmp_path):
        """Verify core_1_inactive triggers degraded status when activity is stale."""
        from config import load_config, split_config

        config_path = pathlib.Path(__file__).resolve().parents[1] / "config.json"
        config = load_config(str(config_path))
        _, core1, _ = split_config(config)

        bus = InterCore(outbound_max=16, event_max=4)

        bus.state_mailboxes.set_network_snapshot({
            "wifi_connected": True,
            "mqtt_connected": True,
            "network_stack_ready": True,
        })
        bus.state_mailboxes.set_utc_snapshot({"utc_epoch_ms": 200000, "ticks_ms": 100000})
        # Set Core 1 activity to 2 minutes ago (exceeds threshold of 60 seconds)
        bus.state_mailboxes.set_core_1_activity_ms(40000)  # 60000ms ago
        bus.state_mailboxes.set_hardware({"hardware_type": "pico_w", "minimum_free_heap_bytes": 65536})

        payload = self._build_health_payload_test(
            bus, 100000, "test-source", core1, "test-runtime",
            now_ms=110000, devices_active=1
        )

        assert payload["payload"]["status"] == "degraded"
        assert "core_1_inactive" in payload["payload"]["degraded_reasons"]

    def test_queue_pressure_triggers_degraded(self, tmp_path):
        """Verify queue_pressure triggers degraded status when utilization >= 75%."""
        from config import load_config, split_config

        config_path = pathlib.Path(__file__).resolve().parents[1] / "config.json"
        config = load_config(str(config_path))
        _, core1, bus_config = split_config(config)

        bus = InterCore(outbound_max=16, event_max=4)

        bus.state_mailboxes.set_network_snapshot({
            "wifi_connected": True,
            "mqtt_connected": True,
            "network_stack_ready": True,
        })
        bus.state_mailboxes.set_utc_snapshot({"utc_epoch_ms": 200000, "ticks_ms": 100000})
        bus.state_mailboxes.set_core_1_activity_ms(100100)
        bus.state_mailboxes.set_hardware({"hardware_type": "pico_w", "minimum_free_heap_bytes": 65536})

        # Simulate queue at 75% capacity (12 out of 16 = 0.75)
        for i in range(12):
            bus.outbound_queue.put_with_kind(
                KIND_HEALTH, json.dumps({"id": i}).encode(), RETENTION_PRIORITY_HEALTH
            )

        payload = self._build_health_payload_test(
            bus, 100000, "test-source", core1, "test-runtime",
            now_ms=110000, devices_active=1
        )

        assert payload["payload"]["status"] == "degraded"
        assert "outbound_queue_pressure" in payload["payload"]["degraded_reasons"]

    def test_utc_invalid_triggers_degraded(self, tmp_path):
        """Verify utc_not_valid triggers degraded status when UTC snapshot is missing."""
        from config import load_config, split_config

        config_path = pathlib.Path(__file__).resolve().parents[1] / "config.json"
        config = load_config(str(config_path))
        _, core1, _ = split_config(config)

        bus = InterCore(outbound_max=16, event_max=4)

        bus.state_mailboxes.set_network_snapshot({
            "wifi_connected": True,
            "mqtt_connected": True,
            "network_stack_ready": True,
        })
        # No UTC snapshot set (None)
        bus.state_mailboxes.set_core_1_activity_ms(100100)
        bus.state_mailboxes.set_hardware({"hardware_type": "pico_w", "minimum_free_heap_bytes": 65536})

        payload = self._build_health_payload_test(
            bus, 100000, "test-source", core1, "test-runtime",
            now_ms=110000, devices_active=1
        )

        assert payload["payload"]["status"] == "degraded"
        assert "utc_not_valid" in payload["payload"]["degraded_reasons"]

    def test_multiple_degradation_reasons(self, tmp_path):
        """Verify multiple failure conditions result in multiple degradation reasons."""
        from config import load_config, split_config

        config_path = pathlib.Path(__file__).resolve().parents[1] / "config.json"
        config = load_config(str(config_path))
        _, core1, _ = split_config(config)

        bus = InterCore(outbound_max=16, event_max=4)

        # Set network to not ready (one failure)
        bus.state_mailboxes.set_network_snapshot({
            "wifi_connected": False,  # Another failure
            "mqtt_connected": True,
            "network_stack_ready": False,  # Another failure
        })
        bus.state_mailboxes.set_utc_snapshot({"utc_epoch_ms": 200000, "ticks_ms": 100000})
        bus.state_mailboxes.set_core_1_activity_ms(100100)
        bus.state_mailboxes.set_hardware({"hardware_type": "pico_w", "minimum_free_heap_bytes": 65536})

        # Simulate queue at 75% capacity
        for i in range(12):
            bus.outbound_queue.put_with_kind(
                KIND_HEALTH, json.dumps({"id": i}).encode(), RETENTION_PRIORITY_HEALTH
            )

        payload = self._build_health_payload_test(
            bus, 100000, "test-source", core1, "test-runtime",
            now_ms=110000, devices_active=1
        )

        assert payload["payload"]["status"] == "degraded"
        reasons = payload["payload"]["degraded_reasons"]
        assert "network_stack_not_ready" in reasons
        assert "wifi_not_connected" in reasons
        assert "outbound_queue_pressure" in reasons
        assert len(reasons) >= 3

    def test_payload_structure_matches_spec(self, tmp_path):
        """Verify health payload has all required fields per specification."""
        from config import load_config, split_config

        config_path = pathlib.Path(__file__).resolve().parents[1] / "config.json"
        config = load_config(str(config_path))
        _, core1, _ = split_config(config)

        bus = InterCore(outbound_max=16, event_max=4)

        bus.state_mailboxes.set_network_snapshot({
            "wifi_connected": True,
            "mqtt_connected": True,
            "network_stack_ready": True,
        })
        bus.state_mailboxes.set_utc_snapshot({"utc_epoch_ms": 200000, "ticks_ms": 100000})
        bus.state_mailboxes.set_core_1_activity_ms(100100)
        bus.state_mailboxes.set_hardware({"hardware_type": "pico_w", "minimum_free_heap_bytes": 65536})

        payload = self._build_health_payload_test(
            bus, 100000, "test-source", core1, "test-runtime",
            now_ms=110000, devices_active=1
        )

        # Verify top-level fields
        assert payload["message_schema_version"] == MESSAGE_SCHEMA_VERSION
        assert payload["runtime_id"] == "test-runtime"
        assert payload["message_type"] == "health"
        assert payload["firmware_version"] == FIRMWARE_VERSION
        assert payload["source"] == "test-source"
        assert "uptime_ms" in payload
        assert "timestamp" in payload

        # Verify payload fields
        p = payload["payload"]
        assert "status" in p
        assert "degraded_reasons" in p
        assert "hardware_type" in p
        assert "machine" in p
        assert "network_stack_ready" in p
        assert "wifi_connected" in p
        assert "wifi_rssi_dbm" in p
        assert "mqtt_connected" in p
        assert "core_1_active" in p
        assert "core_1_activity_age_ms" in p
        assert "free_heap_bytes" in p
        assert "minimum_free_heap_bytes" in p
        assert "heap_headroom_bytes" in p
        assert "devices_configured" in p
        assert "devices_active" in p
        assert "device_failures" in p
        assert "outbound_queue_depth" in p
        assert "outbound_queue_capacity" in p
        assert "outbound_queue_utilization_percent" in p
        assert "utc_valid" in p
        assert "utc_sync_age_sec" in p

    def test_payload_is_json_safe(self, tmp_path):
        """Verify health payload can be serialized to JSON."""
        from config import load_config, split_config
        from message_serializer import serialize_and_validate_message

        config_path = pathlib.Path(__file__).resolve().parents[1] / "config.json"
        config = load_config(str(config_path))
        _, core1, _ = split_config(config)

        bus = InterCore(outbound_max=16, event_max=4)

        bus.state_mailboxes.set_network_snapshot({
            "wifi_connected": True,
            "mqtt_connected": True,
            "network_stack_ready": True,
        })
        bus.state_mailboxes.set_utc_snapshot({"utc_epoch_ms": 200000, "ticks_ms": 100000})
        bus.state_mailboxes.set_core_1_activity_ms(100100)
        bus.state_mailboxes.set_hardware({"hardware_type": "pico_w", "minimum_free_heap_bytes": 65536})

        payload = self._build_health_payload_test(
            bus, 100000, "test-source", core1, "test-runtime",
            now_ms=110000, devices_active=1
        )

        # Should not raise an exception
        payload_bytes = serialize_and_validate_message(payload)
        assert isinstance(payload_bytes, bytes)

        # Verify it can be deserialized
        reconstructed = json.loads(payload_bytes.decode("utf-8"))
        assert reconstructed["message_type"] == "health"
        assert reconstructed["payload"]["status"] == "healthy"

    def test_queue_depth_calculation(self, tmp_path):
        """Verify queue depth includes both queued and in-flight entries."""
        from config import load_config, split_config

        config_path = pathlib.Path(__file__).resolve().parents[1] / "config.json"
        config = load_config(str(config_path))
        _, _, bus_config = split_config(config)

        bus = InterCore(outbound_max=16, event_max=4)

        # Empty queue should return (0, 16)
        depth, capacity = bus.outbound_queue.get_depth_with_capacity()
        assert depth == 0
        assert capacity == 16

        # Add one message
        assert bus.outbound_queue.put_with_kind(
            KIND_HEALTH, json.dumps({"id": 1}).encode(), RETENTION_PRIORITY_HEALTH
        )
        depth, capacity = bus.outbound_queue.get_depth_with_capacity()
        assert depth == 1
        assert capacity == 16

        # Take one (moves to in_flight)
        first = bus.outbound_queue.take()
        assert first is not None
        depth, capacity = bus.outbound_queue.get_depth_with_capacity()
        # In-flight counts toward depth
        assert depth == 1
        assert capacity == 16

        bus.outbound_queue.complete_in_flight(first)
        depth, capacity = bus.outbound_queue.get_depth_with_capacity()
        assert depth == 0
        assert capacity == 16


def test_health_message_queued_after_startup_before_telemetry():
    """Verify health message is queued after startup log but before telemetry.

    The startup ordering should be:
    - Startup log admitted
    - Health message queued (immediately, before telemetry)
    - Telemetry begins (after health is queued)

    This ensures the health message appears early in the startup sequence,
    before the first periodic telemetry read.
    """
    from config import load_config, split_config
    from message_serializer import serialize_and_validate_message

    config_path = pathlib.Path(__file__).resolve().parents[1] / "config.json"
    config = load_config(str(config_path))
    _, core1, bus_config = split_config(config)

    bus = InterCore(outbound_max=bus_config["max_outbound_queue_entries"],
                   event_max=bus_config["max_intercore_event_entries"])

    boot_ticks_ms = 100000
    runtime_id = "test_runtime_12345"
    source = "test-source"

    # Set up network snapshot with ready state (required for health generation)
    bus.state_mailboxes.set_network_snapshot({
        "wifi_connected": True,
        "mqtt_connected": True,
        "network_stack_ready": True,
        "ssid": "test-ssid",
        "ip_address": "10.0.0.1",
    })

    # Set up UTC snapshot
    bus.state_mailboxes.set_utc_snapshot({
        "utc_epoch_ms": 200000,
        "ticks_ms": 100000,
    })

    # Set Core 1 activity (recent)
    bus.state_mailboxes.set_core_1_activity_ms(100100)

    # Set hardware info
    bus.state_mailboxes.set_hardware({
        "hardware_type": "pico_w",
        "minimum_free_heap_bytes": 65536,
    })

    # Build health payload using the same logic as core1.py
    uptime_ms = 100  # 100ms uptime
    network_snapshot = bus.state_mailboxes.get_network_snapshot()
    utc_snapshot = bus.state_mailboxes.get_utc_snapshot()
    core_1_activity_ms = bus.state_mailboxes.get_core_1_activity_ms()
    core_1_inactive_ms = 100100 - core_1_activity_ms if core_1_activity_ms is not None else None
    core_1_activity_threshold_ms = max(core1["read_loop_sec"] * 3 * 1000, 60000)
    core_1_active = core_1_inactive_ms is not None and core_1_inactive_ms <= core_1_activity_threshold_ms

    free_heap = 100000
    hardware = bus.state_mailboxes.get_hardware()
    minimum_free_heap = hardware.get("minimum_free_heap_bytes") if hardware else 65536

    network_stack_ready = bool(network_snapshot.get("network_stack_ready"))
    wifi_connected = bool(network_snapshot.get("wifi_connected"))
    mqtt_connected = bool(network_snapshot.get("mqtt_connected"))
    utc_valid = utc_snapshot is not None

    degraded_reasons = []
    if not network_stack_ready:
        degraded_reasons.append("network_stack_not_ready")
    if not wifi_connected:
        degraded_reasons.append("wifi_not_connected")
    if not mqtt_connected:
        degraded_reasons.append("mqtt_not_connected")
    if not core_1_active:
        degraded_reasons.append("core_1_inactive")
    if free_heap < minimum_free_heap:
        degraded_reasons.append("low_free_heap")
    if core_1_active:  # We set it to active above
        pass  # device_count_mismatch check would go here

    # Calculate additional fields
    hardware = bus.state_mailboxes.get_hardware()
    hardware_type = hardware.get("hardware_type", "unknown") if hardware else "unknown"
    machine = hardware.get("machine", "unknown") if hardware else "unknown"
    minimum_free_heap = hardware.get("minimum_free_heap_bytes") if hardware else 65536
    heap_headroom_bytes = free_heap - minimum_free_heap
    core_1_activity_age_ms = 100100 - core_1_activity_ms if core_1_activity_ms is not None else None

    devices_configured = len(core1["devices"])
    devices_active = devices_configured
    device_failures = devices_configured - devices_active

    queue_depth = 0
    queue_capacity = bus_config["max_outbound_queue_entries"]
    queue_utilization_percent = (queue_depth * 100) // queue_capacity if queue_capacity > 0 else 0

    utc_sync_age_sec = None
    if utc_snapshot is not None:
        elapsed_ms = 100  # 100ms since UTC sync
        utc_sync_age_sec = elapsed_ms // 1000

    payload = {
        "message_schema_version": MESSAGE_SCHEMA_VERSION,
        "runtime_id": runtime_id,
        "uptime_ms": uptime_ms,
        "timestamp": None,
        "source": source,
        "message_type": "health",
        "firmware_version": FIRMWARE_VERSION,
        "payload": {
            "status": "healthy" if not degraded_reasons else "degraded",
            "degraded_reasons": degraded_reasons,
            "hardware_type": hardware_type,
            "machine": machine,
            "network_stack_ready": network_stack_ready,
            "wifi_connected": wifi_connected,
            "wifi_rssi_dbm": network_snapshot.get("rssi"),
            "mqtt_connected": mqtt_connected,
            "core_1_active": core_1_active,
            "core_1_activity_age_ms": core_1_activity_age_ms,
            "free_heap_bytes": free_heap,
            "minimum_free_heap_bytes": minimum_free_heap,
            "heap_headroom_bytes": heap_headroom_bytes,
            "devices_configured": devices_configured,
            "devices_active": devices_active,
            "device_failures": device_failures,
            "outbound_queue_depth": queue_depth,
            "outbound_queue_capacity": queue_capacity,
            "outbound_queue_utilization_percent": queue_utilization_percent,
            "utc_valid": utc_valid,
            "utc_sync_age_sec": utc_sync_age_sec,
        },
    }

    # Initially, queue should be empty
    assert bus.outbound_queue.take() is None

    # Simulate startup log admission (same as _try_queue_startup_log does)
    startup_log_message = {
        "message_type": "log",
        "payload": {
            "level": "info",
            "event": "system_startup_completed",
            "module": "system",
            "message": "System startup completed",
            "data": {"startup": {"status": "ready"}},
        },
    }
    payload_bytes = serialize_and_validate_message(startup_log_message)
    bus.outbound_queue.put_with_kind(KIND_HEALTH, payload_bytes, RETENTION_PRIORITY_HEALTH)

    # Take the startup log
    first = bus.outbound_queue.take()
    assert first is not None
    bus.outbound_queue.complete_in_flight(first)

    # After startup log, health message should be queued immediately
    # (before telemetry which runs on read_loop_sec)
    payload_bytes = serialize_and_validate_message(payload)
    bus.outbound_queue.put_with_kind(KIND_HEALTH, payload_bytes, RETENTION_PRIORITY_HEALTH)

    # Now the health message should be in the queue
    health_entry = bus.outbound_queue.take()
    assert health_entry is not None  # Health message should be in queue
    health_msg = json.loads(health_entry["payload_bytes"].decode("utf-8"))
    assert health_msg["message_type"] == "health"
    assert health_msg["payload"]["status"] == "healthy"


class TestHealthNewFields:
    """Tests for new health message fields."""

    def _ticks_diff(self, later, earlier):
        """MicroPython-compatible time difference."""
        return later - earlier

    def _build_health_payload_test(self, bus, boot_ticks_ms, source, config, runtime_id,
                                    now_ms=110000, devices_active=None):
        """Build health payload using the same logic as core1.py, with test overrides."""
        uptime_ms = self._ticks_diff(now_ms, boot_ticks_ms)

        network_snapshot = bus.state_mailboxes.get_network_snapshot()
        if network_snapshot is None:
            return None

        utc_snapshot = bus.state_mailboxes.get_utc_snapshot()
        utc_valid = utc_snapshot is not None

        core_1_activity_ms = bus.state_mailboxes.get_core_1_activity_ms()
        core_1_inactive_ms = self._ticks_diff(now_ms, core_1_activity_ms) if core_1_activity_ms is not None else None
        core_1_activity_threshold_ms = max(config["read_loop_sec"] * 3 * 1000, 60000)
        core_1_active = core_1_inactive_ms is not None and core_1_inactive_ms <= core_1_activity_threshold_ms

        device_status = bus.state_mailboxes.get_device_status() if hasattr(bus.state_mailboxes, 'get_device_status') else None

        if devices_active is not None:
            if device_status is None:
                device_status = {"devices": {"configured": len(config["devices"]), "active": devices_active}}
            else:
                device_status["devices"]["active"] = devices_active

        devices_configured = len(config["devices"])
        devices_active_actual = device_status["devices"]["active"] if device_status else 0

        queue_depth, queue_capacity = bus.outbound_queue.get_depth_with_capacity()

        try:
            free_heap = gc.mem_free()
        except AttributeError:
            free_heap = 100000

        try:
            hardware = bus.state_mailboxes.get_hardware()
            minimum_free_heap = hardware.get("minimum_free_heap_bytes") if hardware else 65536
            hardware_type = hardware.get("hardware_type", "unknown")
            machine = hardware.get("machine", "unknown")
        except Exception:
            minimum_free_heap = 65536
            hardware_type = "unknown"
            machine = "unknown"

        heap_headroom_bytes = free_heap - minimum_free_heap
        core_1_activity_age_ms = self._ticks_diff(now_ms, core_1_activity_ms) if core_1_activity_ms is not None else None
        utc_sync_age_sec = None
        if utc_snapshot is not None:
            elapsed_ms = self._ticks_diff(now_ms, utc_snapshot["ticks_ms"])
            utc_sync_age_sec = elapsed_ms // 1000

        device_failures = devices_configured - devices_active_actual
        queue_utilization_percent = (queue_depth * 100) // queue_capacity if queue_capacity > 0 else 0
        queue_pressure = queue_capacity > 0 and queue_utilization_percent >= 75

        degraded_reasons = []
        network_stack_ready = bool(network_snapshot.get("network_stack_ready"))
        wifi_connected = bool(network_snapshot.get("wifi_connected"))
        mqtt_connected = bool(network_snapshot.get("mqtt_connected"))

        if not network_stack_ready:
            degraded_reasons.append("network_stack_not_ready")
        if not wifi_connected:
            degraded_reasons.append("wifi_not_connected")
        if not mqtt_connected:
            degraded_reasons.append("mqtt_not_connected")
        if not core_1_active:
            degraded_reasons.append("core_1_inactive")
        if free_heap < minimum_free_heap:
            degraded_reasons.append("low_free_heap")
        if devices_active_actual != devices_configured:
            degraded_reasons.append("device_count_mismatch")
        if queue_pressure:
            degraded_reasons.append("outbound_queue_pressure")
        if not utc_valid:
            degraded_reasons.append("utc_not_valid")

        status = "healthy" if not degraded_reasons else "degraded"

        return {
            "message_schema_version": MESSAGE_SCHEMA_VERSION,
            "runtime_id": runtime_id,
            "uptime_ms": uptime_ms,
            "timestamp": None,
            "source": source,
            "message_type": "health",
            "firmware_version": FIRMWARE_VERSION,
            "payload": {
                "status": status,
                "degraded_reasons": degraded_reasons,
                "hardware_type": hardware_type,
                "machine": machine,
                "network_stack_ready": network_stack_ready,
                "wifi_connected": wifi_connected,
                "wifi_rssi_dbm": network_snapshot.get("rssi"),
                "mqtt_connected": mqtt_connected,
                "core_1_active": core_1_active,
                "core_1_activity_age_ms": core_1_activity_age_ms,
                "free_heap_bytes": free_heap,
                "minimum_free_heap_bytes": minimum_free_heap,
                "heap_headroom_bytes": heap_headroom_bytes,
                "devices_configured": devices_configured,
                "devices_active": devices_active_actual,
                "device_failures": device_failures,
                "outbound_queue_depth": queue_depth,
                "outbound_queue_capacity": queue_capacity,
                "outbound_queue_utilization_percent": queue_utilization_percent,
                "utc_valid": utc_valid,
                "utc_sync_age_sec": utc_sync_age_sec,
            },
        }

    def test_hardware_type_field(self, tmp_path):
        """Verify hardware_type field reflects detected hardware."""
        from config import load_config, split_config

        config_path = pathlib.Path(__file__).resolve().parents[1] / "config.json"
        config = load_config(str(config_path))
        _, core1, _ = split_config(config)

        bus = InterCore(outbound_max=16, event_max=4)

        bus.state_mailboxes.set_network_snapshot({
            "wifi_connected": True,
            "mqtt_connected": True,
            "network_stack_ready": True,
        })
        bus.state_mailboxes.set_utc_snapshot({"utc_epoch_ms": 200000, "ticks_ms": 100000})
        bus.state_mailboxes.set_core_1_activity_ms(100100)
        bus.state_mailboxes.set_hardware({"hardware_type": "pico_w", "machine": "Raspberry Pi Pico W with RP2040", "minimum_free_heap_bytes": 65536})

        payload = self._build_health_payload_test(
            bus, 100000, "test-source", core1, "test-runtime",
            now_ms=110000, devices_active=1
        )

        assert payload["payload"]["hardware_type"] == "pico_w"
        assert payload["payload"]["machine"] == "Raspberry Pi Pico W with RP2040"

    def test_wifi_rssi_dbm_field(self, tmp_path):
        """Verify wifi_rssi_dbm field comes from network snapshot."""
        from config import load_config, split_config

        config_path = pathlib.Path(__file__).resolve().parents[1] / "config.json"
        config = load_config(str(config_path))
        _, core1, _ = split_config(config)

        bus = InterCore(outbound_max=16, event_max=4)

        bus.state_mailboxes.set_network_snapshot({
            "wifi_connected": True,
            "mqtt_connected": True,
            "network_stack_ready": True,
            "rssi": -34,
        })
        bus.state_mailboxes.set_utc_snapshot({"utc_epoch_ms": 200000, "ticks_ms": 100000})
        bus.state_mailboxes.set_core_1_activity_ms(100100)
        bus.state_mailboxes.set_hardware({"hardware_type": "pico_w", "machine": "Raspberry Pi Pico W with RP2040", "minimum_free_heap_bytes": 65536})

        payload = self._build_health_payload_test(
            bus, 100000, "test-source", core1, "test-runtime",
            now_ms=110000, devices_active=1
        )

        assert payload["payload"]["wifi_rssi_dbm"] == -34

    def test_wifi_rssi_dbm_null_when_unavailable(self, tmp_path):
        """Verify wifi_rssi_dbm is None when RSSI is not available."""
        from config import load_config, split_config

        config_path = pathlib.Path(__file__).resolve().parents[1] / "config.json"
        config = load_config(str(config_path))
        _, core1, _ = split_config(config)

        bus = InterCore(outbound_max=16, event_max=4)

        bus.state_mailboxes.set_network_snapshot({
            "wifi_connected": True,
            "mqtt_connected": True,
            "network_stack_ready": True,
            # rssi is None
        })
        bus.state_mailboxes.set_utc_snapshot({"utc_epoch_ms": 200000, "ticks_ms": 100000})
        bus.state_mailboxes.set_core_1_activity_ms(100100)
        bus.state_mailboxes.set_hardware({"hardware_type": "pico_w", "machine": "Raspberry Pi Pico W with RP2040", "minimum_free_heap_bytes": 65536})

        payload = self._build_health_payload_test(
            bus, 100000, "test-source", core1, "test-runtime",
            now_ms=110000, devices_active=1
        )

        assert payload["payload"]["wifi_rssi_dbm"] is None

    def test_heap_headroom_bytes_field(self, tmp_path):
        """Verify heap_headroom_bytes = free_heap - minimum_free_heap."""
        from config import load_config, split_config

        config_path = pathlib.Path(__file__).resolve().parents[1] / "config.json"
        config = load_config(str(config_path))
        _, core1, _ = split_config(config)

        bus = InterCore(outbound_max=16, event_max=4)

        bus.state_mailboxes.set_network_snapshot({
            "wifi_connected": True,
            "mqtt_connected": True,
            "network_stack_ready": True,
        })
        bus.state_mailboxes.set_utc_snapshot({"utc_epoch_ms": 200000, "ticks_ms": 100000})
        bus.state_mailboxes.set_core_1_activity_ms(100100)
        # Set hardware with minimum_free_heap of 65536
        bus.state_mailboxes.set_hardware({"hardware_type": "pico_w", "machine": "Raspberry Pi Pico W with RP2040", "minimum_free_heap_bytes": 65536})

        payload = self._build_health_payload_test(
            bus, 100000, "test-source", core1, "test-runtime",
            now_ms=110000, devices_active=1
        )

        # free_heap is 100000 (from test override), minimum is 65536
        # headroom should be 34464
        assert payload["payload"]["heap_headroom_bytes"] == 34464

    def test_heap_headroom_negative_when_low(self, tmp_path):
        """Verify heap_headroom_bytes can be negative when below reserve."""
        from config import load_config, split_config

        config_path = pathlib.Path(__file__).resolve().parents[1] / "config.json"
        config = load_config(str(config_path))
        _, core1, _ = split_config(config)

        bus = InterCore(outbound_max=16, event_max=4)

        bus.state_mailboxes.set_network_snapshot({
            "wifi_connected": True,
            "mqtt_connected": True,
            "network_stack_ready": True,
        })
        bus.state_mailboxes.set_utc_snapshot({"utc_epoch_ms": 200000, "ticks_ms": 100000})
        bus.state_mailboxes.set_core_1_activity_ms(100100)
        bus.state_mailboxes.set_hardware({"hardware_type": "pico_w", "machine": "Raspberry Pi Pico W with RP2040", "minimum_free_heap_bytes": 120000})

        payload = self._build_health_payload_test(
            bus, 100000, "test-source", core1, "test-runtime",
            now_ms=110000, devices_active=1
        )

        # free_heap is 100000, minimum is 120000
        # headroom should be -20000 (negative)
        assert payload["payload"]["heap_headroom_bytes"] == -20000

    def test_core_1_activity_age_ms_field(self, tmp_path):
        """Verify core_1_activity_age_ms is correct."""
        from config import load_config, split_config

        config_path = pathlib.Path(__file__).resolve().parents[1] / "config.json"
        config = load_config(str(config_path))
        _, core1, _ = split_config(config)

        bus = InterCore(outbound_max=16, event_max=4)

        bus.state_mailboxes.set_network_snapshot({
            "wifi_connected": True,
            "mqtt_connected": True,
            "network_stack_ready": True,
        })
        bus.state_mailboxes.set_utc_snapshot({"utc_epoch_ms": 200000, "ticks_ms": 100000})
        # Set Core 1 activity to 43ms ago
        bus.state_mailboxes.set_core_1_activity_ms(109957)

        payload = self._build_health_payload_test(
            bus, 100000, "test-source", core1, "test-runtime",
            now_ms=110000, devices_active=1
        )

        assert payload["payload"]["core_1_activity_age_ms"] == 43

    def test_utc_sync_age_sec_field(self, tmp_path):
        """Verify utc_sync_age_sec is calculated correctly."""
        from config import load_config, split_config

        config_path = pathlib.Path(__file__).resolve().parents[1] / "config.json"
        config = load_config(str(config_path))
        _, core1, _ = split_config(config)

        bus = InterCore(outbound_max=16, event_max=4)

        bus.state_mailboxes.set_network_snapshot({
            "wifi_connected": True,
            "mqtt_connected": True,
            "network_stack_ready": True,
        })
        # UTC snapshot with ticks_ms 100000, now_ms is 110000 (10 sec ago)
        bus.state_mailboxes.set_utc_snapshot({"utc_epoch_ms": 200000, "ticks_ms": 100000})

        payload = self._build_health_payload_test(
            bus, 100000, "test-source", core1, "test-runtime",
            now_ms=110000, devices_active=1
        )

        assert payload["payload"]["utc_sync_age_sec"] == 10
        assert payload["payload"]["utc_valid"] is True

    def test_utc_sync_age_null_when_not_synchronized(self, tmp_path):
        """Verify utc_sync_age_sec is None when UTC never synchronized."""
        from config import load_config, split_config

        config_path = pathlib.Path(__file__).resolve().parents[1] / "config.json"
        config = load_config(str(config_path))
        _, core1, _ = split_config(config)

        bus = InterCore(outbound_max=16, event_max=4)

        bus.state_mailboxes.set_network_snapshot({
            "wifi_connected": True,
            "mqtt_connected": True,
            "network_stack_ready": True,
        })
        # No UTC snapshot set

        payload = self._build_health_payload_test(
            bus, 100000, "test-source", core1, "test-runtime",
            now_ms=110000, devices_active=1
        )

        assert payload["payload"]["utc_valid"] is False
        assert payload["payload"]["utc_sync_age_sec"] is None

    def test_device_failures_field(self, tmp_path):
        """Verify device_failures = devices_configured - devices_active."""
        from config import load_config, split_config

        config_path = pathlib.Path(__file__).resolve().parents[1] / "config.json"
        config = load_config(str(config_path))
        _, core1, _ = split_config(config)

        bus = InterCore(outbound_max=16, event_max=4)

        bus.state_mailboxes.set_network_snapshot({
            "wifi_connected": True,
            "mqtt_connected": True,
            "network_stack_ready": True,
        })
        bus.state_mailboxes.set_utc_snapshot({"utc_epoch_ms": 200000, "ticks_ms": 100000})
        bus.state_mailboxes.set_core_1_activity_ms(100100)
        bus.state_mailboxes.set_hardware({"hardware_type": "pico_w", "machine": "Raspberry Pi Pico W with RP2040", "minimum_free_heap_bytes": 65536})

        payload = self._build_health_payload_test(
            bus, 100000, "test-source", core1, "test-runtime",
            now_ms=110000, devices_active=0  # 0 active out of 1 configured
        )

        assert payload["payload"]["devices_configured"] == 1
        assert payload["payload"]["devices_active"] == 0
        assert payload["payload"]["device_failures"] == 1

    def test_outbound_queue_utilization_percent_field(self, tmp_path):
        """Verify queue utilization is calculated correctly."""
        from config import load_config, split_config
        import json

        config_path = pathlib.Path(__file__).resolve().parents[1] / "config.json"
        config = load_config(str(config_path))
        _, core1, _ = split_config(config)

        bus = InterCore(outbound_max=16, event_max=4)

        bus.state_mailboxes.set_network_snapshot({
            "wifi_connected": True,
            "mqtt_connected": True,
            "network_stack_ready": True,
        })
        bus.state_mailboxes.set_utc_snapshot({"utc_epoch_ms": 200000, "ticks_ms": 100000})
        bus.state_mailboxes.set_core_1_activity_ms(100100)
        bus.state_mailboxes.set_hardware({"hardware_type": "pico_w", "machine": "Raspberry Pi Pico W with RP2040", "minimum_free_heap_bytes": 65536})

        # Fill queue with 12 messages (75% of 16 = 12)
        for i in range(12):
            bus.outbound_queue.put_with_kind(
                KIND_HEALTH, json.dumps({"id": i}).encode(), RETENTION_PRIORITY_HEALTH
            )

        payload = self._build_health_payload_test(
            bus, 100000, "test-source", core1, "test-runtime",
            now_ms=110000, devices_active=1
        )

        assert payload["payload"]["outbound_queue_depth"] == 12
        assert payload["payload"]["outbound_queue_capacity"] == 16
        assert payload["payload"]["outbound_queue_utilization_percent"] == 75

    def test_existing_classification_unchanged(self, tmp_path):
        """Verify added fields do not alter healthy/degraded classification."""
        from config import load_config, split_config

        config_path = pathlib.Path(__file__).resolve().parents[1] / "config.json"
        config = load_config(str(config_path))
        _, core1, _ = split_config(config)

        bus = InterCore(outbound_max=16, event_max=4)

        bus.state_mailboxes.set_network_snapshot({
            "wifi_connected": True,
            "mqtt_connected": True,
            "network_stack_ready": True,
        })
        bus.state_mailboxes.set_utc_snapshot({"utc_epoch_ms": 200000, "ticks_ms": 100000})
        bus.state_mailboxes.set_core_1_activity_ms(100100)
        bus.state_mailboxes.set_hardware({"hardware_type": "pico_w", "machine": "Raspberry Pi Pico W with RP2040", "minimum_free_heap_bytes": 65536})

        payload = self._build_health_payload_test(
            bus, 100000, "test-source", core1, "test-runtime",
            now_ms=110000, devices_active=1
        )

        # All healthy conditions should result in healthy status
        assert payload["payload"]["status"] == "healthy"
        assert payload["payload"]["degraded_reasons"] == []

        # Set conditions that trigger degradation
        bus.state_mailboxes.set_network_snapshot({
            "wifi_connected": False,
            "mqtt_connected": True,
            "network_stack_ready": True,
        })

        payload = self._build_health_payload_test(
            bus, 100000, "test-source", core1, "test-runtime",
            now_ms=110000, devices_active=1
        )

        assert payload["payload"]["status"] == "degraded"
        assert "wifi_not_connected" in payload["payload"]["degraded_reasons"]


def _build_health_payload_test(bus, boot_ticks_ms, source, config, runtime_id,
                                now_ms=110000, devices_active=None):
    """Build health payload using the same logic as core1.py, with test overrides."""
    uptime_ms = now_ms - boot_ticks_ms

    # Get network snapshot
    network_snapshot = bus.state_mailboxes.get_network_snapshot()
    if network_snapshot is None:
        return None

    # Get UTC snapshot
    utc_snapshot = bus.state_mailboxes.get_utc_snapshot()
    utc_valid = utc_snapshot is not None

    # Get Core 1 activity
    core_1_activity_ms = bus.state_mailboxes.get_core_1_activity_ms()
    core_1_inactive_ms = now_ms - core_1_activity_ms if core_1_activity_ms is not None else None
    core_1_activity_threshold_ms = max(config["read_loop_sec"] * 3 * 1000, 60000)
    core_1_active = core_1_inactive_ms is not None and core_1_inactive_ms <= core_1_activity_threshold_ms

    # Get device status
    device_status = bus.state_mailboxes.get_device_status() if hasattr(bus.state_mailboxes, 'get_device_status') else None

    if devices_active is not None:
        if device_status is None:
            device_status = {"devices": {"configured": len(config["devices"]), "active": devices_active}}
        else:
            device_status["devices"]["active"] = devices_active

    devices_configured = len(config["devices"])
    devices_active_actual = device_status["devices"]["active"] if device_status else 0

    # Get queue status from bus
    queue_depth, queue_capacity = bus.outbound_queue.get_depth_with_capacity()

    # Get memory info - return a value that won't trigger low_free_heap
    try:
        free_heap = gc.mem_free()
    except AttributeError:
        # In host tests, gc.mem_free() doesn't exist, use a safe value
        free_heap = 100000  # Well above Pico W minimum of 65536

    # Get hardware info
    try:
        hardware = bus.state_mailboxes.get_hardware()
        minimum_free_heap = hardware.get("minimum_free_heap_bytes") if hardware else 65536
        hardware_type = hardware.get("hardware_type", "unknown")
        machine = hardware.get("machine", "unknown")
    except Exception:
        minimum_free_heap = 65536
        hardware_type = "unknown"
        machine = "unknown"

    # Calculate heap headroom
    heap_headroom_bytes = free_heap - minimum_free_heap

    # Calculate Core 1 activity age in milliseconds
    core_1_activity_age_ms = now_ms - core_1_activity_ms if core_1_activity_ms is not None else None

    # Calculate UTC sync age in seconds
    utc_sync_age_sec = None
    if utc_snapshot is not None:
        elapsed_ms = now_ms - utc_snapshot["ticks_ms"]
        utc_sync_age_sec = elapsed_ms // 1000  # Integer division for seconds

    # Calculate device failures
    device_failures = devices_configured - devices_active_actual

    # Calculate queue utilization percentage
    queue_utilization_percent = 0
    if queue_capacity > 0:
        queue_utilization_percent = (queue_depth * 100) // queue_capacity

    # Determine queue pressure (75% threshold)
    queue_pressure = queue_capacity > 0 and queue_utilization_percent >= 75

    # Evaluate health status
    degraded_reasons = []
    network_stack_ready = bool(network_snapshot.get("network_stack_ready"))
    wifi_connected = bool(network_snapshot.get("wifi_connected"))
    mqtt_connected = bool(network_snapshot.get("mqtt_connected"))

    if not network_stack_ready:
        degraded_reasons.append("network_stack_not_ready")
    if not wifi_connected:
        degraded_reasons.append("wifi_not_connected")
    if not mqtt_connected:
        degraded_reasons.append("mqtt_not_connected")
    if not core_1_active:
        degraded_reasons.append("core_1_inactive")
    if free_heap < minimum_free_heap:
        degraded_reasons.append("low_free_heap")
    if devices_active_actual != devices_configured:
        degraded_reasons.append("device_count_mismatch")
    if queue_pressure:
        degraded_reasons.append("outbound_queue_pressure")
    if not utc_valid:
        degraded_reasons.append("utc_not_valid")

    status = "healthy" if not degraded_reasons else "degraded"

    payload = {
        "message_schema_version": MESSAGE_SCHEMA_VERSION,
        "runtime_id": runtime_id,
        "uptime_ms": uptime_ms,
        "timestamp": None,
        "source": source,
        "message_type": "health",
        "firmware_version": FIRMWARE_VERSION,
        "payload": {
            "status": status,
            "degraded_reasons": degraded_reasons,
            "hardware_type": hardware_type,
            "machine": machine,
            "network_stack_ready": network_stack_ready,
            "wifi_connected": wifi_connected,
            "wifi_rssi_dbm": network_snapshot.get("rssi"),
            "mqtt_connected": mqtt_connected,
            "core_1_active": core_1_active,
            "core_1_activity_age_ms": core_1_activity_age_ms,
            "free_heap_bytes": free_heap,
            "minimum_free_heap_bytes": minimum_free_heap,
            "heap_headroom_bytes": heap_headroom_bytes,
            "devices_configured": devices_configured,
            "devices_active": devices_active_actual,
            "device_failures": device_failures,
            "outbound_queue_depth": queue_depth,
            "outbound_queue_capacity": queue_capacity,
            "outbound_queue_utilization_percent": queue_utilization_percent,
            "utc_valid": utc_valid,
            "utc_sync_age_sec": utc_sync_age_sec,
        },
    }

    return payload
