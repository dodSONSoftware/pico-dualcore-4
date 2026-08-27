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
)
from intercore import (
    KIND_TELEMETRY,
    KIND_COMMAND_RESPONSE,
    RETENTION_PRIORITY_CRITICAL,
    RETENTION_PRIORITY_TELEMETRY,
)
from message_protocol import format_utc_epoch_ms
from system_information import SystemInformation


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


def core1_main(intercore, config, boot_ticks_ms):
    """Core 1 entry point. This core never imports or touches network/MQTT."""
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

        read_loop_ms = config["read_loop_sec"] * 1000
        next_read_ms = time.ticks_ms()
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
