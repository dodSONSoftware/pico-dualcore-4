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
)
from intercore import (
    KIND_TELEMETRY,
    KIND_COMMAND_RESPONSE,
    KIND_HEALTH,
    KIND_LOG,
    RETENTION_PRIORITY_CRITICAL,
    RETENTION_PRIORITY_TELEMETRY,
    RETENTION_PRIORITY_INFO,
    RETENTION_PRIORITY_HEALTH,
)
from message_protocol import format_utc_epoch_ms, is_json_safe
from system_information import SystemInformation, SYSTEM_INFORMATION_SECTIONS
from uptime import create_uptime_state, current_uptime_ms

from message_serializer import (
    serialize_and_validate_message,
    MessageTooLargeError,
    UnsupportedValueError,
    NonStringKeyError,
    NonFiniteFloatError,
)


COMMAND_GET_DETAILS = "get-details"


def _collect_system_information_full(system_information):
    """Collect full system information snapshot including all sections."""
    system_info = {}
    for section in SYSTEM_INFORMATION_SECTIONS:
        try:
            getter_name = "get_{}".format(section)
            if hasattr(system_information, getter_name):
                section_data = getattr(system_information, getter_name)()
                if is_json_safe(section_data):
                    system_info[section] = section_data
        except MemoryError:
            raise
        except Exception as err:
            # If a section fails to collect, include error info but continue
            system_info[section] = {"error": str(err)}
    return system_info


def _build_startup_log(intercore, boot_ticks_ms, device_manager, config, startup_duration_ms, system_information=None):
    """Build the system_startup_completed log message.

    The message carries only Core 1's own fields (message_type, uptime_ms,
    timestamp, payload). It must NOT carry the envelope keys (sequence,
    runtime_id, source, firmware_version, message_schema_version): those are
    owned by Core 0, which injects them at publish time, and repeating a name
    in the wire document would violate the JSON object contract.

    Args:
        intercore: InterCore bus instance
        boot_ticks_ms: Monotonic timestamp at firmware boot
        device_manager: DeviceManager instance
        config: Core 1 configuration
        startup_duration_ms: Duration of startup in milliseconds
        system_information: Optional SystemInformation instance with device_manager set
    """
    # Build startup summary. Subscription readiness is reported without topic
    # names: topic ownership belongs to Core 0 (message kind only crosses cores).
    # The startup duration is named explicitly (duration_ms) so it carries its
    # units and is not confused with the envelope's device-uptime (uptime_ms).
    startup_summary = {
        "duration_ms": startup_duration_ms,
        "hardware": {"status": "ready"},
        "wifi": {"status": "ready"},
        "mqtt": {"status": "ready"},
        "subscriptions": {
            "status": "ready",
        },
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
    # Use provided system_information if available (with device_manager set),
    # otherwise create a new one for host-side testing
    if system_information is None:
        system_information = SystemInformation(intercore, config)
    system_info = _collect_system_information_full(system_information)

    # Build the message. Core 0 injects the envelope (sequence, runtime_id,
    # source, firmware_version, message_schema_version) at publish time.
    payload = {
        "message_type": "log",
        "uptime_ms": startup_duration_ms,
        "timestamp": _current_utc_timestamp(intercore),
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


def _try_queue_startup_log(intercore, message, retention_priority):
    """Attempt to queue the startup log message."""
    try:
        payload_bytes = serialize_and_validate_message(message)
    except (UnsupportedValueError, NonStringKeyError, NonFiniteFloatError) as err:
        print("[ERROR] Startup log validation failed: {}".format(err))
        return False
    except MessageTooLargeError as err:
        print("[ERROR] Startup log too large: {}".format(err))
        return False
    except MemoryError:
        raise
    except Exception as err:
        print("[ERROR] Startup log serialization failed: {}".format(err))
        return False

    # Queue under KIND_LOG: Core 0 maps the kind to the log topic at publish
    # time. Core 1 never names MQTT topics.
    return intercore.outbound_queue.put_with_kind(
        KIND_LOG,
        payload_bytes,
        retention_priority,
    )


def _current_utc_timestamp(intercore):
    """Current UTC timestamp from the shared snapshot, or None if unsynchronized.

    Core 1 reads the snapshot Core 0 publishes to the state mailboxes and
    advances it by the elapsed local ticks so it tracks the clock between
    refreshes. When no snapshot exists the value is None (null on the wire),
    which is the same value Core 0 would have published from the same
    snapshot state.
    """
    snapshot = intercore.state_mailboxes.get_utc_snapshot()
    if snapshot is None:
        return None
    elapsed_ms = time.ticks_diff(time.ticks_ms(), snapshot["ticks_ms"])
    return format_utc_epoch_ms(snapshot["utc_epoch_ms"] + elapsed_ms)


def _message_time(intercore, uptime_state):
    return current_uptime_ms(uptime_state), _current_utc_timestamp(intercore)


def _build_command_response(intercore, uptime_state, event, success, data=None, error=None):
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

    uptime_ms, timestamp = _message_time(intercore, uptime_state)
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


def _process_intercore_event(intercore, uptime_state, system_information=None):
    event = intercore.event_queue.take()
    if event is None:
        return None

    if event.get("command") == COMMAND_GET_DETAILS:
        if event.get("payload") != {}:
            return _build_command_response(
                intercore,
                uptime_state,
                event,
                False,
                error={
                    "code": "invalid_payload",
                    "message": "get-details payload must be {}",
                },
            )

        if system_information is None:
            return _build_command_response(
                intercore,
                uptime_state,
                event,
                False,
                error={
                    "code": "system_information_unavailable",
                    "message": "System information is unavailable",
                },
            )

        return _build_command_response(
            intercore,
            uptime_state,
            event,
            True,
            data=_collect_system_information_full(system_information),
        )

    return _build_command_response(
        intercore,
        uptime_state,
        event,
        False,
        error={
            "code": "unsupported_command",
            "message": "Command is not implemented in baseline firmware",
        },
    )


def _handle_device_result(intercore, config, uptime_state, result):
    status = result["status"]

    if status == DEVICE_RESULT_TELEMETRY:
        uptime_ms, timestamp = _message_time(intercore, uptime_state)
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
        # device_manager flags which reinitialization failures should warn: the
        # first for a device, not the repeats. Default to logging so a missing
        # field never silences a genuine failure.
        if result.get("log_failure_warning", True):
            print("[WARNING] Core 1 device reinitialization failed: {}: {}".format(
                result["device_id"], result.get("error")
            ))


def _build_health_payload(intercore, uptime_state, config, system_information):
    """Build the health payload from shared state snapshots.

    The message carries only Core 1's own fields (message_type, uptime_ms,
    timestamp, payload). The envelope keys (sequence, runtime_id, source,
    firmware_version, message_schema_version) are owned by Core 0, which
    injects them at publish time; repeating them here would duplicate a name
    in the wire document.

    Args:
        intercore: InterCore bus instance
        uptime_state: Accumulated uptime state (see uptime.py)
        config: Core 1 configuration
        system_information: SystemInformation instance for device status

    Returns:
        dict: Health message payload with status and diagnostic fields
    """
    now_ms = time.ticks_ms()
    uptime_ms = current_uptime_ms(uptime_state)

    # Get network snapshot (from Core 0)
    network_snapshot = intercore.state_mailboxes.get_network_snapshot()
    if network_snapshot is None:
        # No network snapshot available yet
        return None

    # Get UTC snapshot (from Core 0)
    utc_snapshot = intercore.state_mailboxes.get_utc_snapshot()
    utc_valid = utc_snapshot is not None

    # Get Core 1 activity timestamp
    core_1_activity_ms = intercore.state_mailboxes.get_core_1_activity_ms()
    # Age of the last Core 1 activity report (None if never reported)
    core_1_activity_age_ms = time.ticks_diff(now_ms, core_1_activity_ms) if core_1_activity_ms is not None else None
    # Calculate threshold: 3x the read loop interval (with reasonable minimum)
    core_1_activity_threshold_ms = max(config["read_loop_sec"] * 3 * 1000, 60000)  # 60 seconds min
    core_1_active = core_1_activity_age_ms is not None and core_1_activity_age_ms <= core_1_activity_threshold_ms

    # Get device status from SystemInformation (which uses DeviceManager)
    devices = system_information.get_devices() if system_information else {"configured": 0, "active": 0}
    devices_configured = devices["configured"]
    devices_active = devices["active"]

    # Get queue status
    outbound_queue = intercore.outbound_queue
    (
        queue_depth,
        queue_capacity,
        queue_queued_bytes,
        queue_max_queued_bytes,
    ) = outbound_queue.get_health_metrics()

    # Get memory info
    try:
        free_heap = gc.mem_free()
    except MemoryError:
        raise
    except Exception:
        free_heap = 0

    # Get hardware info from state mailboxes
    try:
        hardware = intercore.state_mailboxes.get_hardware()
        minimum_free_heap = hardware.get("minimum_free_heap_bytes") if hardware else 65536
        hardware_type = hardware.get("hardware_type", "unknown")
        machine = hardware.get("machine", "unknown")
    except MemoryError:
        raise
    except Exception:
        minimum_free_heap = 65536
        hardware_type = "unknown"
        machine = "unknown"

    # Get RSSI from network snapshot
    wifi_rssi_dbm = network_snapshot.get("rssi")

    # Calculate heap headroom
    heap_headroom_bytes = free_heap - minimum_free_heap

    # Calculate UTC sync age in seconds
    utc_sync_age_sec = None
    if utc_snapshot is not None:
        elapsed_ms = time.ticks_diff(now_ms, utc_snapshot["ticks_ms"])
        utc_sync_age_sec = elapsed_ms // 1000  # Integer division for seconds

    # Calculate device failures from DeviceManager state
    device_failures = devices_configured - devices_active

    # Calculate queue utilization percentages. The queue has two independent
    # budgets (entry count and queued payload bytes); a handful of large
    # entries can reach the byte ceiling while entry utilization is modest,
    # so both are reported.
    queue_utilization_percent = 0
    if queue_capacity > 0:
        queue_utilization_percent = (queue_depth * 100) // queue_capacity
    queue_byte_utilization_percent = 0
    if queue_max_queued_bytes > 0:
        queue_byte_utilization_percent = (queue_queued_bytes * 100) // queue_max_queued_bytes

    # Determine queue pressure: either budget at or beyond the 75% threshold
    # is a full-condition risk, so pressure is the worse of the two.
    queue_pressure = max(
        queue_utilization_percent, queue_byte_utilization_percent
    ) >= 75

    # Evaluate health status and build degraded reasons
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
    if devices_active != devices_configured:
        degraded_reasons.append("device_count_mismatch")
    if queue_pressure:
        degraded_reasons.append("outbound_queue_pressure")
    if not utc_valid:
        degraded_reasons.append("utc_not_valid")

    # Determine status
    status = "healthy" if not degraded_reasons else "degraded"

    # Build health payload. The timestamp comes from the shared UTC snapshot
    # (already fetched above for the utc_* fields); Core 0 injects the
    # envelope at publish time.
    if utc_snapshot is None:
        timestamp = None
    else:
        timestamp = format_utc_epoch_ms(
            utc_snapshot["utc_epoch_ms"] + time.ticks_diff(now_ms, utc_snapshot["ticks_ms"])
        )
    payload = {
        "uptime_ms": uptime_ms,
        "timestamp": timestamp,
        "message_type": "health",
        "payload": {
            "status": status,
            "degraded_reasons": degraded_reasons,
            "hardware_type": hardware_type,
            "machine": machine,
            "network_stack_ready": network_stack_ready,
            "wifi_connected": wifi_connected,
            "wifi_rssi_dbm": wifi_rssi_dbm,
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
            "outbound_queued_bytes": queue_queued_bytes,
            "outbound_max_queued_bytes": queue_max_queued_bytes,
            "outbound_queue_byte_utilization_percent": queue_byte_utilization_percent,
            "utc_valid": utc_valid,
            "utc_sync_age_sec": utc_sync_age_sec,
        },
    }

    return payload


def _try_queue_health_message(intercore, message):
    """Attempt to queue a health message.

    Args:
        intercore: InterCore bus instance
        message: Health message payload dict

    Returns:
        bool: True if message was admitted, False otherwise
    """
    try:
        payload_bytes = serialize_and_validate_message(message)
    except (UnsupportedValueError, NonStringKeyError, NonFiniteFloatError) as err:
        if DEBUG:
            print("[DEBUG] Health message validation failed: {}".format(err))
        return False
    except MessageTooLargeError as err:
        print("[WARNING] Health message too large: {}".format(err))
        return False
    except MemoryError:
        raise
    except Exception as err:
        print("[WARNING] Health message serialization failed: {}".format(err))
        return False

    # Queue the pre-serialized message with health kind
    return intercore.outbound_queue.put_with_kind(
        KIND_HEALTH,
        payload_bytes,
        RETENTION_PRIORITY_HEALTH,
    )


def _try_queue_health_message_intercore(intercore, uptime_state, config, system_information):
    """Build health payload and attempt to queue it.

    Only generates health message if network stack is ready.
    This prevents health messages from accumulating during MQTT outages.

    Args:
        intercore: InterCore bus instance
        uptime_state: Accumulated uptime state (see uptime.py)
        config: Core 1 configuration
        system_information: SystemInformation instance for device status
    """
    # Check if network stack is ready before generating health
    network_snapshot = intercore.state_mailboxes.get_network_snapshot()
    if network_snapshot is None:
        return

    # Only generate health if network is ready and MQTT is connected
    # This prevents stale health messages from accumulating during outages
    network_stack_ready = network_snapshot.get("network_stack_ready", False)
    mqtt_connected = network_snapshot.get("mqtt_connected", False)

    if not network_stack_ready or not mqtt_connected:
        return

    # Build health payload
    health_payload = _build_health_payload(intercore, uptime_state, config, system_information)
    if health_payload is None:
        return

    # Queue the health message
    _try_queue_health_message(intercore, health_payload)


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

        # Accumulated uptime: every ticks_diff compares recent samples, so
        # uptime stays correct across a tick-counter wrap on long runs.
        # boot_ticks_ms anchors this boot-lifetime uptime only; periodic
        # scheduling anchors to normal_runtime_start_ticks_ms (captured
        # after startup-log admission, below).
        uptime_state = create_uptime_state(boot_ticks_ms)

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
        startup_duration_ms = current_uptime_ms(uptime_state)

        # Build and queue the one-time startup log
        startup_log_message = _build_startup_log(
            intercore, boot_ticks_ms, device_manager, config, startup_duration_ms, system_information
        )

        # Attempt to queue the startup log with INFO priority
        startup_log_admitted = _try_queue_startup_log(
            intercore, startup_log_message, RETENTION_PRIORITY_INFO
        )

        if not startup_log_admitted:
            # Startup log must be admitted before telemetry can begin
            print("[ERROR] Startup log queue admission failed - telemetry gated")
            # Wait for queue space and retry once
            time.sleep_ms(100)
            startup_log_admitted = _try_queue_startup_log(
                intercore, startup_log_message, RETENTION_PRIORITY_INFO
            )
            if not startup_log_admitted:
                print("[FATAL] Startup log queue admission failed after retry - halting")
                raise RuntimeError("Startup log queue admission failed")

        print("[INFO] Startup log admitted to outbound queue")

        # The single normal-runtime scheduling anchor, captured exactly once,
        # immediately after system_startup_completed is admitted. All
        # periodic Core 1 work (telemetry and health) derives its fixed
        # boundaries from this moment -- not from boot_ticks_ms (which
        # remains the boot-lifetime reference for uptime and startup-duration
        # measurement) and not from when startup merely completed. A Wi-Fi or
        # MQTT reconnect, a UTC resynchronization, a device reinitialization,
        # or a queue drain must never re-capture this anchor; only a true
        # reboot -- a new runtime with a new runtime_id -- creates a new one.
        normal_runtime_start_ticks_ms = time.ticks_ms()

        # Store hardware info in state mailboxes (from system_information)
        try:
            hardware = system_information.get_machine()
            intercore.state_mailboxes.set_hardware(hardware)
        except MemoryError:
            raise
        except Exception as err:
            if DEBUG:
                print("[DEBUG] Hardware storage failed: {}".format(err))

        # Register initial Core 1 activity
        intercore.state_mailboxes.set_core_1_activity_ms(time.ticks_ms())

        # Health scheduler anchored to the shared normal-runtime anchor:
        # fixed boundaries every health_interval_sec from normal-runtime
        # start (a 60s interval means +60s, +120s, +180s relative to the
        # anchor). Independent of the telemetry scheduler: the two share
        # the epoch, not an execution dependency. No immediate health
        # message is emitted at startup.
        health_interval_ms = config["health_interval_sec"] * 1000
        next_health_ms = time.ticks_add(normal_runtime_start_ticks_ms, health_interval_ms)

        now_ms = time.ticks_ms()

        # Boundaries already passed (only possible if scheduler initialization
        # was delayed by more than a full interval) are skipped, never
        # replayed: health is current-state data, not historical telemetry.
        # Advance to the next future boundary.
        while time.ticks_diff(now_ms, next_health_ms) >= 0:
            next_health_ms = time.ticks_add(next_health_ms, health_interval_ms)

        # Liveness heartbeat scheduler (deadline-based, independent of loop phase)
        activity_interval_ms = 5000
        next_activity_ms = time.ticks_add(now_ms, activity_interval_ms)

        # Now that the startup log is admitted, telemetry can begin. The
        # telemetry scheduler shares the same normal-runtime anchor as the
        # health scheduler but keeps its own independent deadline.
        read_loop_ms = config["read_loop_sec"] * 1000
        next_read_ms = time.ticks_add(normal_runtime_start_ticks_ms, read_loop_ms)

        pending_command_response = None

        while True:
            if pending_command_response is not None:
                if _try_queue_response(intercore, pending_command_response):
                    pending_command_response = None
            else:
                pending_command_response = _process_intercore_event(
                    intercore, uptime_state, system_information
                )
                if pending_command_response is not None:
                    if _try_queue_response(intercore, pending_command_response):
                        pending_command_response = None

            now_ms = time.ticks_ms()
            if time.ticks_diff(now_ms, next_read_ms) >= 0:
                for managed_device in device_manager.get_active_devices():
                    result = device_manager.process_device(managed_device)
                    _handle_device_result(intercore, config, uptime_state, result)

                # Skip any boundaries that elapsed while the read ran, rather
                # than replaying them as catch-up reads. Telemetry is a current
                # sample, not historical data, so a slow read cannot reconstruct
                # the missed samples -- emitting them as catch-up reads would
                # only produce a burst of near-identical samples, JSON work, and
                # queue admissions immediately after an overload. This is the
                # same missed-boundary policy the health scheduler already uses.
                # Advance from the previous deadline (never from execution time)
                # so processing delay cannot accumulate into drift: boundaries
                # stay fixed at anchor + n * read_loop_ms.
                #
                # Re-capture the clock here: now_ms above is stale by however
                # long the read took, and skipping against it would
                # under-advance and leave a catch-up read for the next pass.
                skip_now_ms = time.ticks_ms()
                while time.ticks_diff(skip_now_ms, next_read_ms) >= 0:
                    next_read_ms = time.ticks_add(next_read_ms, read_loop_ms)
                gc.collect()

            # Register Core 1 activity periodically (every 5 seconds)
            if time.ticks_diff(now_ms, next_activity_ms) >= 0:
                intercore.state_mailboxes.set_core_1_activity_ms(now_ms)
                next_activity_ms = time.ticks_add(now_ms, activity_interval_ms)

            # Health boundary reached: emit at most one current health
            # report (skipped entirely during a network outage), then advance
            # to the next normal-runtime-relative boundary. Advancing from
            # the old deadline -- not from now -- keeps the cadence aligned
            # to the anchor-based boundaries and avoids cumulative drift;
            # missed boundaries are skipped, never replayed as catch-up
            # reports.
            if time.ticks_diff(now_ms, next_health_ms) >= 0:
                _try_queue_health_message_intercore(intercore, uptime_state, config, system_information)
                while time.ticks_diff(now_ms, next_health_ms) >= 0:
                    next_health_ms = time.ticks_add(next_health_ms, health_interval_ms)

            time.sleep_ms(20)

    except MemoryError:
        print("[ERROR] Core 1 stopped: MemoryError")
        raise
    except Exception as err:
        print("[ERROR] Core 1 stopped: {}".format(err))
        raise
