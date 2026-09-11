# core1.py - Core 1 exclusive sensor/device owner
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import gc
import time

from command_protocol import COMMAND_GET_DETAILS
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
    OutboundMessageTooLargeError,
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


class StartupLogTooLargeError(ValueError):
    """The startup event log exceeds MAX_OUTBOUND_MESSAGE_BYTES; the one
    permanent rejection a different object (the bounded fallback summary)
    can answer."""


def _collect_system_information_full(system_information):
    system_info = {}
    # The devices and device_status sections share one snapshot source: take
    # it once and derive both, instead of walking every device twice.
    device_sections = None
    device_sections_getter = getattr(system_information, "get_device_sections", None)
    if device_sections_getter is not None:
        try:
            device_sections = device_sections_getter()
        except MemoryError:
            raise
        except Exception as err:
            device_sections = {
                "devices": {"error": str(err)},
                "device_status": {"error": str(err)},
            }
    for section in SYSTEM_INFORMATION_SECTIONS:
        try:
            if device_sections is not None and section in device_sections:
                section_data = device_sections[section]
                if is_json_safe(section_data):
                    system_info[section] = section_data
                continue
            getter_name = "get_{}".format(section)
            if hasattr(system_information, getter_name):
                section_data = getattr(system_information, getter_name)()
                if is_json_safe(section_data):
                    system_info[section] = section_data
        except MemoryError:
            raise
        except Exception as err:
            system_info[section] = {"error": str(err)}
    return system_info


# The get-details fallback's drop order when the full snapshot cannot be
# serialized: the device sections are the only two that grow with the device
# count, and device_status carries the unbounded failure_reason strings, so
# dropping them first keeps every bounded form a fixed small size.
_GET_DETAILS_FALLBACK_DROP_ORDER = ("device_status", "devices")


def _startup_summary(device_status, startup_duration_ms):
    """Startup statuses and device counts, shared by the startup event log
    and its bounded fallback. Subscription readiness is reported without topic
    names (topic ownership belongs to Core 0), and the startup duration is
    named duration_ms so it is not confused with the envelope's uptime_ms."""
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

    startup_summary["devices_configured"] = device_status["devices"]["configured"]
    startup_summary["devices_ready"] = device_status["devices"]["active"]
    startup_summary["devices_failed"] = device_status["devices"].get("initialization_failed", 0)
    return startup_summary


def _startup_log_message(intercore, startup_duration_ms, startup_summary):
    """Envelope for the startup event log and its bounded fallback."""
    data = {"startup": startup_summary}
    return {
        "message_type": "log",
        "uptime_ms": startup_duration_ms,
        "timestamp": _current_utc_timestamp(intercore, startup_duration_ms),
        "payload": {
            "level": "info",
            "event": "system_startup_completed",
            "module": "system",
            "message": "System startup completed",
            "data": data,
        },
    }


def _build_startup_log(intercore, device_manager, startup_duration_ms):
    """Build the system_startup_completed event log (Core 1's own fields
    only). Failed devices carry their failure_reason here: the one diagnosis
    that matters at boot."""
    device_status = device_manager.get_status_snapshot(now_ms=time.ticks_ms())
    startup_summary = _startup_summary(device_status, startup_duration_ms)

    ready_devices = []
    failed_devices = []
    for status in device_status["device_status"]:
        device_info = {
            "device": status.get("device", "unknown"),
            "name": status.get("name") or status.get("id", "unknown"),
        }
        if status["state"] == DEVICE_STATE_READY:
            ready_devices.append(device_info)
        elif status["state"] == DEVICE_STATE_INITIALIZATION_FAILED:
            device_info["failure_reason"] = status.get("failure_reason")
            failed_devices.append(device_info)

    startup_summary["ready_devices"] = ready_devices
    startup_summary["failed_devices"] = failed_devices

    return _startup_log_message(intercore, startup_duration_ms, startup_summary)


def _build_startup_log_bounded(intercore, device_manager, startup_duration_ms):
    """Build the bounded startup-log fallback: only the startup statuses and
    device counts -- no per-device lists, no failure reasons -- so it fits
    when the event log (which carries device lists and driver failure reasons
    no bound can pin) cannot. Losing the verbose diagnostics must not keep
    the device from entering normal operation."""
    device_status = device_manager.get_status_snapshot(now_ms=time.ticks_ms())
    return _startup_log_message(
        intercore,
        startup_duration_ms,
        _startup_summary(device_status, startup_duration_ms),
    )


def _try_queue_startup_log(intercore, message, retention_priority):
    """Attempt to queue the startup log message.

    True if admitted; False on transient heap pressure. ValueError on a
    permanent rejection (retrying cannot succeed), except the oversized case,
    which raises StartupLogTooLargeError so the caller can answer it with the
    bounded fallback instead of failing startup."""
    try:
        payload_bytes = serialize_and_validate_message(message)
    except (UnsupportedValueError, NonStringKeyError, NonFiniteFloatError) as err:
        print("[ERROR] Startup log validation failed: {}".format(err))
        raise ValueError("Startup log validation failed: {}".format(err))
    except MessageTooLargeError as err:
        print("[ERROR] Startup log too large: {}".format(err))
        raise StartupLogTooLargeError("Startup log too large: {}".format(err))
    except MemoryError:
        raise
    except Exception as err:
        print("[ERROR] Startup log serialization failed: {}".format(err))
        raise ValueError("Startup log serialization failed: {}".format(err))

    # Queue under KIND_LOG (Core 0 maps the kind to the log topic; Core 1
    # never names MQTT topics).
    return intercore.outbound_queue.put_with_kind(
        KIND_LOG,
        payload_bytes,
        retention_priority,
    )


def _admit_startup_log(intercore, message):
    """Admit the startup log, retrying only a transient rejection (a
    permanent one escapes to the caller). True if admitted, False if the
    single transient retry also failed."""
    if _try_queue_startup_log(intercore, message, RETENTION_PRIORITY_INFO):
        return True

    # Transient (heap pressure): the queue may admit it on the next pass.
    # Startup log must be admitted before telemetry can begin.
    print("[ERROR] Startup log queue admission failed - telemetry gated")
    time.sleep_ms(100)
    return _try_queue_startup_log(intercore, message, RETENTION_PRIORITY_INFO)


def _admit_startup_log_with_fallback(intercore, message, fallback_builder):
    """Admit the startup log; if the detailed message is rejected for
    exceeding the outbound ceiling or fails serialization with MemoryError
    (a memory-tight board's fragmented pool), admit the bounded fallback
    summary instead. Every other permanent rejection escapes with its actual
    reason so the caller fails fast (each admission keeps the single
    transient retry)."""
    try:
        return _admit_startup_log(intercore, message)
    except StartupLogTooLargeError:
        print("[WARN] Startup log exceeds the outbound ceiling; admitting the bounded summary instead")
        return _admit_startup_log(intercore, fallback_builder())
    except MemoryError:
        # The detailed message's serialization buffers do not fit on a
        # memory-tight board: a fragmented pool can hold tens of KiB in total
        # yet no run large enough for the serialized form, so the size check
        # never sees the bytes. The bounded summary is the smaller object that
        # answers it, so the verbose diagnostics cannot keep an otherwise
        # valid configuration from entering normal operation. If the summary
        # cannot be admitted either, the MemoryError propagates to the
        # recovery boundary.
        print("[WARN] Startup log serialization exhausted the heap; admitting the bounded summary instead")
        return _admit_startup_log(intercore, fallback_builder())


def _current_utc_timestamp(intercore, uptime_ms):
    """Current UTC timestamp from the shared snapshot, or None if
    unsynchronized. Adding this sample's accumulated uptime to the pinned
    runtime start stays correct across the tick wrap, where a one-shot
    ticks_diff against the sync tick would go stale past half a tick period."""
    snapshot = intercore.state_mailboxes.get_utc_snapshot()
    if snapshot is None:
        return None
    return format_utc_epoch_ms(snapshot["runtime_start_epoch_ms"] + uptime_ms)


def _message_time(intercore, uptime_state):
    uptime_ms = current_uptime_ms(uptime_state)
    return uptime_ms, _current_utc_timestamp(intercore, uptime_ms)


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
    """Queue a command response at CRITICAL retention priority.

    True if admitted; False if transiently rejected (retry on a later pass);
    ValueError on a permanent rejection (oversized: OutboundMessageTooLargeError);
    MemoryError if the serializer's own gc + eviction recovery exhausts."""
    return intercore.outbound_queue.put(
        response["kind"],
        response["message"],
        RETENTION_PRIORITY_CRITICAL,
    )


def _build_substitute_error_response(intercore, uptime_state, response, code, message):
    """A small error response standing in for a permanently rejected one; the
    rejected payload carries the command's identifying fields and doubles as
    the descriptor."""
    payload = response["message"]["payload"]
    return _build_command_response(
        intercore,
        uptime_state,
        payload,
        False,
        error={
            "code": code,
            "message": message,
        },
    )


def _admit_substitute(intercore, uptime_state, response, code, message, warning):
    """Log a permanent rejection and admit the small error substitute for it;
    None if admitted, the substitute (still pending) if transiently rejected."""
    print("[WARNING] {}".format(warning))
    substitute = _build_substitute_error_response(intercore, uptime_state, response, code, message)
    if _try_queue_response(intercore, substitute):
        return None
    return substitute


def _build_get_details_response_without(intercore, uptime_state, response, section):
    """The same get-details response with one section dropped; the
    omitted_sections marker names what the response no longer carries (a
    missing section is a detectable gap)."""
    payload = response["message"]["payload"]
    data = dict(payload["data"])
    del data[section]
    omitted = list(data.get("omitted_sections") or [])
    omitted.append(section)
    data["omitted_sections"] = omitted
    return _build_command_response(intercore, uptime_state, payload, True, data=data)


def _admit_after_serialization_memory_error(intercore, uptime_state, response):
    """A persistent serialization MemoryError: the queue's own recovery
    (gc.collect() first, then one eligible eviction per failure) has already
    exhausted, so the heap cannot form the contiguous run the serialized form
    needs, now. A MemoryError escaping the admission used to kill Core 1's
    worker thread -- the 0.4.91 Pico W died exactly here on a get-details
    full snapshot (2,360 bytes with ~57 KiB free), and Core 0's heartbeat
    watchdog reset the board 30 s later, the command unanswered. The full
    get-details snapshot is the one response large enough to hit that wall:
    answer it one section smaller at a time, dropping the device sections in
    order (each attempt follows the queue's own gc.collect(), so the pool is
    coalesced when the strictly smaller form is retried), until one is
    admitted; the response stays a success and names what it omitted. Any
    other response -- or a get-details whose device sections are already
    gone -- takes the small error substitute. A MemoryError on a bounded
    form's or the substitute's own admission propagates to the recovery
    boundary (the 0.4.87 bounded-summary precedent: nothing loops)."""
    if response["message"]["payload"].get("command") == COMMAND_GET_DETAILS:
        candidate = response
        for section in _GET_DETAILS_FALLBACK_DROP_ORDER:
            if section not in (candidate["message"]["payload"].get("data") or {}):
                continue
            candidate = _build_get_details_response_without(
                intercore, uptime_state, candidate, section
            )
            print(
                "[WARNING] get-details response could not be serialized "
                "(MemoryError); answering without the {} section".format(section)
            )
            try:
                if _try_queue_response(intercore, candidate):
                    return None
                return candidate
            except MemoryError:
                # The pool still has no run for this form: the next, strictly
                # smaller form is the next attempt.
                continue
            except OutboundMessageTooLargeError as err:
                return _admit_substitute(
                    intercore,
                    uptime_state,
                    candidate,
                    "response_too_large",
                    "Command response exceeded the per-message size limit",
                    "get-details bounded response too large: {}".format(err),
                )
            except ValueError as err:
                return _admit_substitute(
                    intercore,
                    uptime_state,
                    candidate,
                    "response_invalid",
                    "Command response could not be serialized for transmission",
                    "get-details bounded response invalid: {}".format(err),
                )
    return _admit_substitute(
        intercore,
        uptime_state,
        response,
        "response_invalid",
        "Command response could not be serialized for transmission",
        "Command response could not be serialized (MemoryError); answering "
        "with the error response",
    )


def _admit_or_substitute_command_response(intercore, uptime_state, response):
    """Admit a command response, or a bounded form of it / a small error
    substitute for it (code "response_too_large" for oversized,
    "response_invalid" for a validation/serialization failure); either way
    the channel moves on. Returns the response still pending, or None if one
    was admitted."""
    try:
        if _try_queue_response(intercore, response):
            return None
        return response
    except OutboundMessageTooLargeError as err:
        return _admit_substitute(
            intercore,
            uptime_state,
            response,
            "response_too_large",
            "Command response exceeded the per-message size limit",
            "Command response too large: {}".format(err),
        )
    except ValueError as err:
        return _admit_substitute(
            intercore,
            uptime_state,
            response,
            "response_invalid",
            "Command response could not be serialized for transmission",
            "Command response invalid: {}".format(err),
        )
    except MemoryError:
        return _admit_after_serialization_memory_error(intercore, uptime_state, response)


def _regrid_next_boundary(anchor, now, interval_ms):
    """The next interval-aligned boundary from the anchor strictly after now;
    boundaries that already passed are skipped, never replayed."""
    elapsed = time.ticks_diff(now, anchor)
    if elapsed < 0:
        return anchor
    return time.ticks_add(anchor, (elapsed // interval_ms + 1) * interval_ms)


def _apply_config_update(config_update, config, schedulers):
    """Apply a HOT_RELOADED Core 1 hot update (or its rollback): re-anchor
    the read/health schedulers and refresh Core 1's own config copy.

    A reload is a NEW scheduling boundary, re-anchored from the reload instant
    rather than re-gridded to the boot anchor (next = now + interval); only
    the keys present are touched, and no catch-up sample is emitted for a
    shortened interval."""
    now_ms = time.ticks_ms()
    if "read_loop_sec" in config_update:
        config["read_loop_sec"] = config_update["read_loop_sec"]
        schedulers["read_loop_ms"] = config_update["read_loop_sec"] * 1000
        schedulers["next_read_ms"] = time.ticks_add(
            now_ms, schedulers["read_loop_ms"]
        )
    if "health_interval_sec" in config_update:
        config["health_interval_sec"] = config_update["health_interval_sec"]
        schedulers["health_interval_ms"] = config_update["health_interval_sec"] * 1000
        schedulers["next_health_ms"] = time.ticks_add(
            now_ms, schedulers["health_interval_ms"]
        )


def _process_config_update(intercore, config, schedulers):
    """Apply one HOT_RELOADED config-update request from Core 0 and
    acknowledge it. Internal runtime control on the dedicated lane, not an
    external command: take the pending request, apply it, then post exactly
    one result for the request's generation (no command response is owed).
    On an apply failure the prior values are restored and a bounded failure
    posted, so Core 1 is left unchanged. MemoryError propagates."""
    request = intercore.config_update_lane.take_request()
    if request is None:
        return
    generation = request.get("generation")

    prior = (
        config.get("read_loop_sec"),
        config.get("health_interval_sec"),
        schedulers["read_loop_ms"],
        schedulers["health_interval_ms"],
        schedulers["next_read_ms"],
        schedulers["next_health_ms"],
    )
    try:
        _apply_config_update(request, config, schedulers)
    except MemoryError:
        raise
    except Exception:
        # An apply failure must leave Core 1 unchanged: restore the prior
        # values, then report a bounded failure so Core 0 rolls back.
        (
            config["read_loop_sec"],
            config["health_interval_sec"],
            schedulers["read_loop_ms"],
            schedulers["health_interval_ms"],
            schedulers["next_read_ms"],
            schedulers["next_health_ms"],
        ) = prior
        intercore.config_update_lane.post_result({
            "generation": generation,
            "success": False,
            "code": "core1_apply_failed",
        })
        return
    intercore.config_update_lane.post_result({
        "generation": generation,
        "success": True,
    })


def _process_intercore_event(intercore, uptime_state, system_information=None):
    """Handle a Core 1-owned command event dispatched by Core 0 (currently
    get-details, with a validated {} payload). Core 0 owns the unknown-command
    response, so Core 1 is not the generic fallback for arbitrary command
    names. (Config-update traffic uses the dedicated lane, not this queue.)"""
    event = intercore.event_queue.take()
    if event is None:
        return None

    if event.get("command") != COMMAND_GET_DETAILS:
        return None

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


def _handle_device_result(intercore, uptime_state, result):
    status = result["status"]

    if status == DEVICE_RESULT_TELEMETRY:
        uptime_ms, timestamp = _message_time(intercore, uptime_state)
        message = {
            "message_type": "telemetry",
            "uptime_ms": uptime_ms,
            "timestamp": timestamp,
            "device_id": result["device_id"],
            "device": result["device"],
            "name": result.get("name"),
            "payload": result["telemetry"],
        }
        try:
            admitted = intercore.outbound_queue.put(
                KIND_TELEMETRY,
                message,
                RETENTION_PRIORITY_TELEMETRY,
            )
        except ValueError as err:
            # Permanent rejection (oversized or invalid): discard this sample
            # rather than retry it -- telemetry is current, not replayable.
            print("[WARNING] Core 1 telemetry rejected: {}: {}".format(
                result["device_id"], err
            ))
            return
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
        # device_manager flags which failures should warn (the first, not the
        # repeats); default to logging so a missing field never silences one.
        if result.get("log_failure_warning", True):
            print("[WARNING] Core 1 device reinitialization failed: {}: {}".format(
                result["device_id"], result.get("error")
            ))


def _run_telemetry_read_pass(device_manager, intercore, uptime_state):
    """One telemetry read pass across all active devices; shared by the
    initial at-anchor sample and the periodic read boundary."""
    for managed_device in device_manager.get_active_devices():
        result = device_manager.process_device(managed_device)
        _handle_device_result(intercore, uptime_state, result)
    gc.collect()


def _build_health_payload(intercore, uptime_state, config, system_information):
    """Build the health payload from shared state snapshots (Core 1's own
    fields only); None if no network snapshot exists yet."""
    now_ms = time.ticks_ms()
    uptime_ms = current_uptime_ms(uptime_state)

    network_snapshot = intercore.state_mailboxes.get_network_snapshot()
    if network_snapshot is None:
        return None

    utc_snapshot = intercore.state_mailboxes.get_utc_snapshot()
    utc_valid = utc_snapshot is not None

    core_1_activity_ms = intercore.state_mailboxes.get_core_1_activity_ms()
    core_1_activity_age_ms = time.ticks_diff(now_ms, core_1_activity_ms) if core_1_activity_ms is not None else None
    core_1_activity_threshold_ms = max(config["read_loop_sec"] * 3 * 1000, 60000)  # 60 seconds min
    core_1_active = core_1_activity_age_ms is not None and core_1_activity_age_ms <= core_1_activity_threshold_ms

    devices = system_information.get_devices() if system_information else {"configured": 0, "active": 0}
    devices_configured = devices["configured"]
    devices_active = devices["active"]
    cpu_temperature_c = system_information.get_cpu_temperature() if system_information else None

    outbound_status = intercore.outbound_queue.status()

    try:
        free_heap = gc.mem_free()
    except MemoryError:
        raise
    except Exception:
        free_heap = 0

    try:
        hardware = intercore.state_mailboxes.get_hardware()
        minimum_free_heap = hardware.get("minimum_free_heap_bytes") if hardware else 65536
        # The preferred reserve defaults to the hard floor for
        # single-threshold hardware snapshots.
        preferred_free_heap = (
            hardware.get("preferred_free_heap_bytes", minimum_free_heap)
            if hardware else 65536
        )
        hardware_type = hardware.get("hardware_type", "unknown")
        machine = hardware.get("machine", "unknown")
    except MemoryError:
        raise
    except Exception:
        minimum_free_heap = 65536
        preferred_free_heap = 65536
        hardware_type = "unknown"
        machine = "unknown"

    wifi_rssi_dbm = network_snapshot.get("rssi")

    heap_headroom_bytes = free_heap - minimum_free_heap

    # UTC sync age: current_uptime - sync_uptime on the shared boot base
    # (correct for any duration, unlike a one-shot ticks_diff past half a
    # tick period, which could go negative).
    utc_sync_age_sec = None
    if utc_snapshot is not None:
        utc_sync_age_sec = (uptime_ms - utc_snapshot["sync_uptime_ms"]) // 1000

    device_failures = devices_configured - devices_active

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
    if not utc_valid:
        degraded_reasons.append("utc_not_valid")

    status = "healthy" if not degraded_reasons else "degraded"

    if utc_snapshot is None:
        timestamp = None
    else:
        timestamp = format_utc_epoch_ms(
            utc_snapshot["runtime_start_epoch_ms"] + uptime_ms
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
            "cpu_temperature_c": cpu_temperature_c,
            "network_stack_ready": network_stack_ready,
            "wifi_connected": wifi_connected,
            "wifi_rssi_dbm": wifi_rssi_dbm,
            "mqtt_connected": mqtt_connected,
            "core_1_active": core_1_active,
            "core_1_activity_age_ms": core_1_activity_age_ms,
            "free_heap_bytes": free_heap,
            "preferred_free_heap_bytes": preferred_free_heap,
            "minimum_free_heap_bytes": minimum_free_heap,
            "heap_headroom_bytes": heap_headroom_bytes,
            "devices_configured": devices_configured,
            "devices_active": devices_active,
            "device_failures": device_failures,
            "outbound_queue_depth": outbound_status["depth"],
            "outbound_queued_bytes": outbound_status["queued_bytes"],
            "outbound_queue_high_watermark": outbound_status["high_watermark"],
            "outbound_queue_high_watermark_bytes": outbound_status["high_watermark_bytes"],
            "outbound_evicted": outbound_status["messages_evicted"],
            "telemetry_evicted": outbound_status["telemetry_evicted"],
            "outbound_rejected": outbound_status["messages_rejected"],
            "utc_valid": utc_valid,
            "utc_sync_age_sec": utc_sync_age_sec,
        },
    }

    return payload


def _try_queue_health_message(intercore, message):
    """Attempt to queue a health message; True if admitted, False otherwise."""
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

    try:
        return intercore.outbound_queue.put_with_kind(
            KIND_HEALTH,
            payload_bytes,
            RETENTION_PRIORITY_HEALTH,
        )
    except ValueError as err:
        # Permanent rejection: health is a current-state report, so the
        # boundary is discarded (missed boundaries are never replayed).
        print("[WARNING] Health message permanently rejected: {}".format(err))
        return False


def _try_queue_health_message_intercore(intercore, uptime_state, config, system_information):
    """Build health payload and attempt to queue it.

    Only when the network stack is ready and MQTT is connected, so health messages don't accumulate during outages."""
    network_snapshot = intercore.state_mailboxes.get_network_snapshot()
    if network_snapshot is None:
        return

    network_stack_ready = network_snapshot.get("network_stack_ready", False)
    mqtt_connected = network_snapshot.get("mqtt_connected", False)

    if not network_stack_ready or not mqtt_connected:
        return

    health_payload = _build_health_payload(intercore, uptime_state, config, system_information)
    if health_payload is None:
        return

    _try_queue_health_message(intercore, health_payload)


def _build_i2c_bus_factory():
    """Core 1 owns its I2C buses (ARCHITECTURE ownership invariant). Returns a
    factory that builds one machine.I2C per distinct (bus, sda, scl, freq) and
    caches it, so two devices on the same bus share one object. sda/scl are
    None when a device config relies on the bus's default pins.

    The machine import is inside the closure (not here) so building the factory
    is side-effect-free: the bus -- and the machine import -- only happen when a
    configured I2C device first requests one. Host tests that run core1_main
    under a minimal fake machine never touch I2C/Pin, and pass a fake factory
    directly for I2C device tests.
    """
    cache = {}

    def create(bus, sda, scl, freq_hz):
        from machine import I2C, Pin

        key = (bus, sda, scl, freq_hz)
        if key not in cache:
            kwargs = {"freq": freq_hz}
            if sda is not None:
                kwargs["sda"] = Pin(sda)
            if scl is not None:
                kwargs["scl"] = Pin(scl)
            cache[key] = I2C(bus, **kwargs)
        return cache[key]

    return create


def core1_main(intercore, config, boot_ticks_ms, runtime_id):
    """Core 1 entry point; this core never imports or touches network/MQTT."""
    try:
        print("[INFO] Core 1 starting")

        # Register the liveness stamp before any initialization work: Core 0's
        # heartbeat watchdog is a no-op until the first stamp exists, so a
        # startup wedge must age a stamp to be caught.
        intercore.state_mailboxes.set_core_1_activity_ms(time.ticks_ms())

        # Accumulated uptime: every ticks_diff compares recent samples, so
        # uptime stays correct across a tick-counter wrap. boot_ticks_ms
        # anchors boot-lifetime uptime only; periodic scheduling anchors to
        # normal_runtime_start_ticks_ms (captured below).
        uptime_state = create_uptime_state(boot_ticks_ms)

        system_information = SystemInformation(intercore, config)
        # activity_refresh keeps the liveness stamp current at initialization
        # progress boundaries, so a legitimately long initialization does not
        # age it past Core 0's 30 s watchdog bound while a wedged driver call
        # (which stops the refresh) is still caught.
        device_manager = DeviceManager(
            config,
            system_information=system_information,
            activity_refresh=lambda: intercore.state_mailboxes.set_core_1_activity_ms(time.ticks_ms()),
            # Shared boot-relative uptime base: the device read-age fields
            # stay correct across a tick wrap (past half a tick period, raw
            # ticks would report a wrong age).
            uptime_state=uptime_state,
            # Core 1 owns its I2C buses; create_device pulls a bus from this
            # factory only for I2C devices (never for system-information).
            i2c_bus_factory=_build_i2c_bus_factory(),
        )
        system_information.set_device_manager(device_manager)

        initialized = device_manager.initialize_devices()
        print("[INFO] Core 1 devices initialized: {}/{}".format(
            initialized, len(config["devices"])
        ))

        startup_duration_ms = current_uptime_ms(uptime_state)

        # Reclaim the import/initialization residue before the startup log's
        # serialization buffers are needed: on a memory-tight board (Pico W)
        # the pool is fragmented after the device init pass, and this
        # allocation-light gc is the difference between the event log (or its
        # bounded fallback) and the per-section diagnostics fitting and a
        # MemoryError.
        gc.collect()
        startup_log_message = _build_startup_log(
            intercore, device_manager, startup_duration_ms
        )

        # The startup event log must be admitted before telemetry can begin.
        # A permanent rejection fails fast with its actual reason; only heap
        # pressure is retried. The oversized case is the one a different
        # object can answer: the bounded fallback summary.
        try:
            startup_log_admitted = _admit_startup_log_with_fallback(
                intercore,
                startup_log_message,
                lambda: _build_startup_log_bounded(
                    intercore, device_manager, startup_duration_ms
                ),
            )
        except ValueError as err:
            print("[FATAL] Startup log permanently rejected: {}".format(err))
            raise RuntimeError("Startup log queue admission failed")

        if not startup_log_admitted:
            print("[FATAL] Startup log queue admission failed after retry - halting")
            raise RuntimeError("Startup log queue admission failed")

        print("[INFO] Startup log admitted to outbound queue")

        # The single normal-runtime scheduling anchor, captured exactly once,
        # after the startup event log is admitted. All periodic Core 1 work
        # derives its fixed
        # boundaries from this moment (not boot_ticks_ms). A reconnect, UTC
        # resync, device reinit, or queue drain must never re-capture it; only
        # a true reboot creates a new one.
        normal_runtime_start_ticks_ms = time.ticks_ms()

        try:
            hardware = system_information.get_machine()
            intercore.state_mailboxes.set_hardware(hardware)
        except MemoryError:
            raise
        except Exception as err:
            if DEBUG:
                print("[DEBUG] Hardware storage failed: {}".format(err))

        # Refresh Core 1 activity: marks the transition into the normal
        # runtime loop.
        intercore.state_mailboxes.set_core_1_activity_ms(time.ticks_ms())

        # The read/health schedulers share the normal-runtime anchor: fixed
        # boundaries every read_loop_sec / health_interval_sec from it,
        # independent of each other (they share the epoch, not an execution
        # dependency). A HOT_RELOADED write re-anchors a changed interval
        # from the reload instant via the config-update lane, without
        # touching the anchor; the one immediate anchor pass runs below.
        schedulers = {
            "anchor_ms": normal_runtime_start_ticks_ms,
            "read_loop_ms": config["read_loop_sec"] * 1000,
            "health_interval_ms": config["health_interval_sec"] * 1000,
        }
        now_ms = time.ticks_ms()

        # Boundaries already passed (scheduler initialization delayed by more
        # than a full interval) are skipped, never replayed.
        schedulers["next_read_ms"] = _regrid_next_boundary(
            normal_runtime_start_ticks_ms, now_ms, schedulers["read_loop_ms"]
        )
        schedulers["next_health_ms"] = _regrid_next_boundary(
            normal_runtime_start_ticks_ms, now_ms, schedulers["health_interval_ms"]
        )

        # Liveness heartbeat scheduler (deadline-based, independent of loop phase)
        activity_interval_ms = 5000
        next_activity_ms = time.ticks_add(now_ms, activity_interval_ms)

        # Initial sample at the anchor: one telemetry pass and one health
        # report right after startup-log admission, so a subscriber connecting
        # at boot sees current data. Telemetry first, then health, so the
        # health report reflects queue state that already includes the fresh
        # samples. The periodic schedulers are untouched (not a second grid).
        _run_telemetry_read_pass(device_manager, intercore, uptime_state)
        _try_queue_health_message_intercore(intercore, uptime_state, config, system_information)

        pending_command_response = None

        while True:
            # Internal control first: a pending HOT_RELOADED config-update
            # request from Core 0 re-anchors the schedulers and is acked on
            # the config-update lane; independent of the user-command queue.
            _process_config_update(intercore, config, schedulers)

            if pending_command_response is not None:
                pending_command_response = _admit_or_substitute_command_response(
                    intercore, uptime_state, pending_command_response
                )
            else:
                response = _process_intercore_event(
                    intercore, uptime_state, system_information
                )
                if response is not None:
                    pending_command_response = _admit_or_substitute_command_response(
                        intercore, uptime_state, response
                    )

            now_ms = time.ticks_ms()
            if time.ticks_diff(now_ms, schedulers["next_read_ms"]) >= 0:
                _run_telemetry_read_pass(device_manager, intercore, uptime_state)

                # Skip boundaries that elapsed while the read ran, never
                # replay them: telemetry is a current sample, not historical
                # data (same policy as the health scheduler). Advance from the
                # previous deadline (not execution time) so delay cannot
                # accumulate into drift: boundaries stay at anchor + n *
                # read_loop_ms. now_ms is stale by the read duration, so
                # re-capture the clock for the skip comparison.
                skip_now_ms = time.ticks_ms()
                while time.ticks_diff(skip_now_ms, schedulers["next_read_ms"]) >= 0:
                    schedulers["next_read_ms"] = time.ticks_add(
                        schedulers["next_read_ms"], schedulers["read_loop_ms"]
                    )

            # Activity stamp every 5 s. now_ms is stale by the pass duration,
            # and stamping with it could age a healthy slow read past Core 0's
            # watchdog bound, so re-capture the clock for both compare and stamp.
            activity_now_ms = time.ticks_ms()
            if time.ticks_diff(activity_now_ms, next_activity_ms) >= 0:
                intercore.state_mailboxes.set_core_1_activity_ms(activity_now_ms)
                next_activity_ms = time.ticks_add(activity_now_ms, activity_interval_ms)

            # Health boundary reached: emit at most one current report (skipped
            # during a network outage), then advance from the old deadline so
            # the cadence stays anchored and missed boundaries are skipped,
            # never replayed.
            if time.ticks_diff(now_ms, schedulers["next_health_ms"]) >= 0:
                _try_queue_health_message_intercore(intercore, uptime_state, config, system_information)
                while time.ticks_diff(now_ms, schedulers["next_health_ms"]) >= 0:
                    schedulers["next_health_ms"] = time.ticks_add(
                        schedulers["next_health_ms"], schedulers["health_interval_ms"]
                    )

            time.sleep_ms(20)

    except MemoryError:
        print("[ERROR] Core 1 stopped: MemoryError")
        raise
    except Exception as err:
        print("[ERROR] Core 1 stopped: {}".format(err))
        raise
