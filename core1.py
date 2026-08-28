# core1.py - Core 1 exclusive sensor/device owner
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import gc
import time

from debug import DEBUG
from device_manager import (
    DeviceManager,
    DEVICE_RESULT_TELEMETRY,
    DEVICE_RESULT_READ_FAILED,
    DEVICE_RESULT_REINITIALIZED,
    DEVICE_RESULT_REINITIALIZATION_FAILED,
    DEVICE_STATE_READY,
    DEVICE_STATE_INITIALIZATION_FAILED,
    DEVICE_STATE_REMOVED,
)
from intercore import (
    KIND_TELEMETRY,
    KIND_COMMAND_RESPONSE,
    RETENTION_PRIORITY_CRITICAL,
    RETENTION_PRIORITY_TELEMETRY,
    RETENTION_PRIORITY_INFO,
)

# Core 1 uses the same MQTT log topic as Core 0
# This is a deliberate exception to the architecture rule - Core 1 needs to know
# the log topic to queue startup logs before any network configuration is available
_MQTT_TOPIC_LOG = "iot/v3/log"
from message_protocol import format_utc_epoch_ms, is_json_safe
from system_information import SystemInformation, SYSTEM_INFORMATION_SECTIONS

from version import FIRMWARE_VERSION, MESSAGE_SCHEMA_VERSION
from message_serializer import (
    serialize_and_validate_message,
    MessageTooLargeError,
    UnsupportedValueError,
    NonStringKeyError,
    NonFiniteFloatError,
)


def _build_common_envelope(intercore, boot_ticks_ms, message_type, runtime_id):
    """Build the common MQTT envelope fields.

    This function is not currently used - the startup log uses the envelope
    directly in the payload as per the canonical message format. Kept for
    potential future use.
    """
    # Placeholder - envelope is built inside the message payload for startup log
    pass


def _collect_system_information_full(system_information):
    """Collect full system information snapshot including all sections."""
    system_info = {}
    for section in SYSTEM_INFORMATION_SECTIONS:
        try:
            getter_name = "get_{}".format(section.replace("_", ""))
            if hasattr(system_information, getter_name):
                section_data = getattr(system_information, getter_name)()
                if is_json_safe(section_data):
                    system_info[section] = section_data
        except Exception as err:
            # If a section fails to collect, include error info but continue
            system_info[section] = {"error": str(err)}
    return system_info


def _build_startup_log(intercore, source, boot_ticks_ms, runtime_id, device_manager, startup_duration_ms):
    """Build the system_startup_completed log message.

    The payload must include all envelope fields since Core 0's _make_envelope
    deserializes the payload and adds/overwrites fields (sequence, runtime_id,
    firmware_version, message_schema_version, source).

    For startup log, we build the complete message with all required envelope fields.

    Args:
        intercore: InterCore bus instance
        source: Device source identifier (from config)
        boot_ticks_ms: Monotonic timestamp at firmware boot
        runtime_id: Unique runtime identifier
        device_manager: DeviceManager instance
        startup_duration_ms: Duration of startup in milliseconds
    """
    # Use provided source

    # Build startup summary
    startup_summary = {
        "duration_ms": startup_duration_ms,
        "hardware": {"status": "ready"},
        "wifi": {"status": "ready"},
        "mqtt": {"status": "ready"},
        "subscriptions": {"status": "ready"},
        "utc": {"status": "synchronized"},
        "core_0": {"status": "running"},
        "core_1": {"status": "running"},
    }

    # Add device status
    device_status = device_manager.get_status_snapshot(now_ms=time.ticks_ms())
    startup_summary["devices_configured"] = device_status["devices"]["configured"]
    startup_summary["devices_ready"] = device_status["devices"]["active"]
    startup_summary["devices_failed"] = device_status["devices"].get("initialization_failed", 0)

    # Add lists of detailed device info for ready and failed devices
    ready_devices = []
    failed_devices = []
    for status in device_status["device_status"]:
        device_info = {
            "device": status.get("device", "unknown"),
            "name": status.get("name") or status.get("id", "unknown"),
            "sensor_type": status.get("sensor_type", "unknown"),
        }
        if status["state"] == DEVICE_STATE_READY:
            ready_devices.append(device_info)
        elif status["state"] == DEVICE_STATE_INITIALIZATION_FAILED:
            failed_devices.append(device_info)

    startup_summary["ready_devices"] = ready_devices
    startup_summary["failed_devices"] = failed_devices

    # Collect full system information
    system_info = _collect_system_information_full(SystemInformation(intercore, None))

    # Build the complete message with envelope fields
    # Note: Core 0 will add sequence and may add timestamp/uptime_ms if missing
    payload = {
        "message_schema_version": MESSAGE_SCHEMA_VERSION,
        "runtime_id": runtime_id,
        "message_type": "log",
        "source": source,
        "firmware_version": FIRMWARE_VERSION,
        "uptime_ms": startup_duration_ms,
        "timestamp": None,  # Core 0 will fill from UTC snapshot
        "payload": {
            "level": "info",
            "event": "system_startup_completed",
            "module": "system",
            "message": "System startup completed",
            "data": {
                "startup": startup_summary,
                "system_information": system_info,
            },
        },
    }

    return payload


def _try_queue_startup_log(intercore, source, message, retention_priority):
    """Attempt to queue the startup log message."""
    try:
        payload_bytes = serialize_and_validate_message(message)
    except (UnsupportedValueError, NonStringKeyError, NonFiniteFloatError) as err:
        print("[ERROR] Startup log validation failed: {}".format(err))
        return False
    except MessageTooLargeError as err:
        print("[ERROR] Startup log too large: {}".format(err))
        return False
    except Exception as err:
        print("[ERROR] Startup log serialization failed: {}".format(err))
        return False

    # Create the queue entry with log topic
    # Core 1 uses a constant for the log topic since it's shared with Core 0
    entry = {
        "topic": _MQTT_TOPIC_LOG,  # Use log topic directly
        "retention_priority": retention_priority,
        "payload_bytes": payload_bytes,
    }

    # Manually queue the entry following the same pattern as OutboundQueue.put
    # but without re-serialization since we already have payload_bytes
    with intercore.outbound_queue._lock:
        occupied = len(intercore.outbound_queue._queue) + (1 if intercore.outbound_queue._in_flight is not None else 0)
        if occupied >= intercore.outbound_queue._max_entries:
            if not intercore.outbound_queue._queue:
                intercore.outbound_queue._messages_rejected += 1
                return False

            worst_priority = max(entry["retention_priority"] for entry in intercore.outbound_queue._queue)
            if retention_priority > worst_priority:
                intercore.outbound_queue._messages_rejected += 1
                return False

            if not intercore.outbound_queue._evict_oldest_by_priority_locked(worst_priority):
                intercore.outbound_queue._messages_rejected += 1
                return False

        intercore.outbound_queue._queue.append(entry)
        return True


def _message_time(intercore, boot_ticks_ms):
    now_ticks = time.ticks_ms()
    uptime_ms = time.ticks_diff(now_ticks, boot_ticks_ms)

    snapshot = intercore.state_mailboxes.get_utc_snapshot()
    if snapshot is None:
        return uptime_ms, None

    elapsed_ms = time.ticks_diff(now_ticks, snapshot["ticks_ms"])
    timestamp = format_utc_epoch_ms(snapshot["utc_epoch_ms"] + elapsed_ms)
    return uptime_ms, timestamp


def _build_command_response(intercore, boot_ticks_ms, event, success, data=None, error=None):
    payload = {
        "command_id": event.get("command_id"),
        "command": event.get("command"),
        "targeted": event.get("targeted", False),
        "success": success,
    }
    if success:
        payload["data"] = data
    else:
        payload["error"] = error

    uptime_ms, timestamp = _message_time(intercore, boot_ticks_ms)
    return {
        "kind": KIND_COMMAND_RESPONSE,
        "message": {
            "message_type": "command_response",
            "uptime_ms": uptime_ms,
            "timestamp": timestamp,
            "payload": payload,
        },
    }


def _try_queue_response(intercore, response):
    return intercore.outbound_queue.put(
        response["kind"],
        response["message"],
        RETENTION_PRIORITY_CRITICAL,
    )


def _process_intercore_event(intercore, boot_ticks_ms):
    event = intercore.event_queue.take()
    if event is None:
        return None

    # Baseline rebuild intentionally implements no Core 1 commands yet.
    return _build_command_response(
        intercore,
        boot_ticks_ms,
        event,
        False,
        error={
            "code": "unsupported_command",
            "message": "Command is not implemented in baseline firmware",
        },
    )


def _handle_device_result(intercore, config, boot_ticks_ms, result):
    status = result["status"]

    if status == DEVICE_RESULT_TELEMETRY:
        uptime_ms, timestamp = _message_time(intercore, boot_ticks_ms)
        message = {
            "message_type": "telemetry",
            "uptime_ms": uptime_ms,
            "timestamp": timestamp,
            "device_id": result["device_id"],
            "device": result["device"],
            "sensor_type": result["sensor_type"],
            "name": result.get("name"),
            "payload": result["telemetry"],
        }
        admitted = intercore.outbound_queue.put(
            KIND_TELEMETRY,
            message,
            RETENTION_PRIORITY_TELEMETRY,
        )
        if not admitted:
            print("[WARNING] Core 1 telemetry rejected: {}".format(result["device_id"]))
        elif DEBUG:
            print("[DEBUG] Core 1 telemetry queued: {}".format(result["device_id"]))
        return

    if status == DEVICE_RESULT_READ_FAILED:
        print("[WARNING] Core 1 device read failed: {}: {}".format(
            result["device_id"], result.get("error")
        ))
        return

    if status == DEVICE_RESULT_REINITIALIZED:
        print("[INFO] Core 1 device reinitialized: {}".format(result["device_id"]))
        return

    if status == DEVICE_RESULT_REINITIALIZATION_FAILED:
        print("[WARNING] Core 1 device reinitialization failed: {}: {}".format(
            result["device_id"], result.get("error")
        ))


def core1_main(intercore, config, boot_ticks_ms, runtime_id):
    """Core 1 entry point. This core never imports or touches network/MQTT.

    Args:
        intercore: InterCore bus instance
        config: Core 1 configuration
        boot_ticks_ms: Monotonic timestamp at firmware boot
        runtime_id: Unique runtime identifier
    """
    try:
        print("[INFO] Core 1 starting")

        system_information = SystemInformation(intercore, config)
        device_manager = DeviceManager(config, system_information=system_information)
        system_information.set_device_manager(device_manager)

        initialized, failed, attempt_logs = device_manager.initialize_devices()
        print("[INFO] Core 1 devices initialized: {}/{}".format(
            initialized, len(config["devices"])
        ))
        if failed and DEBUG:
            print("[DEBUG] Core 1 failed devices: {}".format(failed))
        if DEBUG:
            for item in attempt_logs:
                print("[DEBUG] Core 1 init attempt: {}".format(item))

        # Calculate startup duration
        startup_duration_ms = time.ticks_diff(time.ticks_ms(), boot_ticks_ms)

        # Get source from network snapshot
        network_snapshot = intercore.state_mailboxes.get_network_snapshot()
        source = network_snapshot.get("ip_address", "unknown") if network_snapshot else "unknown"

        # Build and queue the one-time startup log
        startup_log_message = _build_startup_log(
            intercore, source, boot_ticks_ms, runtime_id, device_manager, startup_duration_ms
        )

        # Attempt to queue the startup log with INFO priority
        startup_log_admitted = _try_queue_startup_log(
            intercore, source, startup_log_message, RETENTION_PRIORITY_INFO
        )

        if not startup_log_admitted:
            # Startup log must be admitted before telemetry can begin
            print("[ERROR] Startup log queue admission failed - telemetry gated")
            # Wait for queue space and retry once
            time.sleep_ms(100)
            startup_log_admitted = _try_queue_startup_log(
                intercore, source, startup_log_message, RETENTION_PRIORITY_INFO
            )
            if not startup_log_admitted:
                print("[FATAL] Startup log queue admission failed after retry - halting")
                raise RuntimeError("Startup log queue admission failed")

        print("[INFO] Startup log admitted to outbound queue")

        # Now that startup log is queued, telemetry can begin
        read_loop_ms = config["read_loop_sec"] * 1000
        next_read_ms = time.ticks_add(time.ticks_ms(), read_loop_ms)
        pending_command_response = None

        while True:
            if pending_command_response is not None:
                if _try_queue_response(intercore, pending_command_response):
                    pending_command_response = None
            else:
                pending_command_response = _process_intercore_event(intercore, boot_ticks_ms)
                if pending_command_response is not None:
                    if _try_queue_response(intercore, pending_command_response):
                        pending_command_response = None

            now_ms = time.ticks_ms()
            if time.ticks_diff(now_ms, next_read_ms) >= 0:
                for managed_device in device_manager.get_active_devices():
                    result = device_manager.process_device(managed_device)
                    _handle_device_result(intercore, config, boot_ticks_ms, result)

                next_read_ms = time.ticks_add(now_ms, read_loop_ms)
                gc.collect()

            time.sleep_ms(20)

    except MemoryError:
        print("[ERROR] Core 1 stopped: MemoryError")
        raise
    except Exception as err:
        print("[ERROR] Core 1 stopped: {}".format(err))
        raise
