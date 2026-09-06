# core0.py - Core 0 exclusive network owner
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import json
import machine
import time

from command_protocol import (
    BROADCAST_TARGET,
    COMMAND_ENVELOPE_KEYS,
    COMMAND_GET_DETAILS,
    COMMAND_READ_CONFIG,
    COMMAND_REBOOT,
    COMMAND_WRITE_CONFIG,
    MAX_COMMAND_LENGTH,
    is_bounded_command,
    is_bounded_command_id,
    is_bounded_target,
    is_supported_command,
    unknown_field_names,
)
from config import ConfigError
from config_manager import CLASSIFICATION_UNCHANGED
from debug import DEBUG
from intercore import (
    KIND_COMMAND_RESPONSE,
    KIND_HEALTH,
    KIND_LOG,
    KIND_TELEMETRY,
    OutboundMessageTooLargeError,
)
from message_protocol import format_utc_epoch_ms
from mqtt import Mqtt
from mqtt_client import MQTTException
from uptime import create_uptime_state, current_uptime_ms
from version import FIRMWARE_VERSION, MESSAGE_SCHEMA_VERSION
from wifi import Wifi

from message_serializer import (
    MAX_OUTBOUND_MESSAGE_BYTES,
    MessageTooLargeError,
    serialize_and_validate_message,
)


_MAX_PENDING_CORE0_RESPONSES = 4
_MAX_PENDING_CONNECTION_LOGS = 4

# Bounded substitutes for a command response that can never be published
# as-is (holding it would stall the FIFO responses behind it).
_SUBSTITUTE_ERROR_MESSAGES = {
    "response_too_large": "Command response exceeded the per-message size limit",
    "response_invalid": "Command response could not be serialized for transmission",
}
_UTC_STARTUP_MAX_ATTEMPTS = 3
_UTC_RETRY_INTERVAL_MS = 30000
_UTC_PROMPT_RETRY_DELAY_MS = 500

# Command-ID debounce cache: stops repeated copies of one message (broker
# redelivery, sender retry) from producing repeated responses and executions.
# FIFO eviction, RAM-only, deliberately not heap-governed; a repeated ID is
# ignored until it evicts and a duplicate never refreshes its position (not
# LRU). Not durable idempotency or exactly-once. Static constant; not a config key.
_RECENT_COMMAND_ID_CAPACITY = 16

# Core 1 heartbeat watchdog timeout: Core 1 refreshes the stamp on a 5 s
# deadline, so a stamp this old means the Core 1 thread is dead or wedged.
# Far above any live-loop gap, below the 60 s core_1_inactive diagnostic
# threshold, so a dead Core 1 resets the board. Static constant; not a config key.
_CORE_1_HEARTBEAT_STALE_TIMEOUT_MS = 30000

# Hardware watchdog (machine.WDT) timeout: the one supervision layer for
# Core 0 itself — nothing else can detect a Core 0 that is alive but no
# longer making progress (a wedged driver, a deadlock, a native hang).
#
# The budget is derived, not arbitrary: every single blocking operation
# Core 0 performs must fail on its OWN timeout before this one can fire, so
# a watchdog reset means "Core 0 is wedged", never "the link was slow". The
# longest such operation is one bounded MQTT exchange wait, capped at
# max(MQTT's 5 s response-timeout bound in config.py, mqtt.py's 5 s PINGRESP
# bound) — both checked against this constant by a test. Every longer wait
# in Core 0's paths is sliced at 100 ms with _service_wait() between slices,
# which feeds the watchdog. 8 s stays under the RP2 hardware maximum
# (8388 ms) while keeping 60% margin over the 5 s wait ceiling.
# Static constant; not a config key.
WDT_TIMEOUT_MS = 8000

# HOT_RELOADED settings Core 0 applies to its live config at commit time;
# Core 1's two HOT settings cross as an internal config-update event.
_HOT_APPLY_CORE0_KEYS = (
    "datetime_sync_interval_min",
    "mqtt_command_poll_ms",
    "mqtt_outbound_publish_delay_ms",
    "network_snapshot_interval_sec",
)
_HOT_APPLY_CORE1_KEYS = ("read_loop_sec", "health_interval_sec")

# Rollback key for the last-poll stamp: when mqtt_command_poll_ms changes the
# stamp is re-anchored at apply time; the old value is kept for rollback.
_POLL_STAMP_KEY = "_last_command_poll_ms"


class Core0:

    def __init__(self, intercore, config, wifi_config, runtime_id, boot_ticks_ms, led_manager, config_manager):
        self._intercore = intercore
        self._config = config
        # Core 0 owns the configuration manager; it is never shared with
        # Core 1, which only ever sees the internal config-update event.
        self._config_manager = config_manager
        self._runtime_id = runtime_id
        self._uptime_state = create_uptime_state(boot_ticks_ms)
        self._led_manager = led_manager

        self._wifi = Wifi(
            wifi_config["wifi_ssid"],
            wifi_config["wifi_password"],
            config["wifi_reconnect_delays_sec"],
            self._service_wait,
        )
        self._mqtt = Mqtt(config, self._on_mqtt_message, self._service_wait)

        self._pending_reboot = None
        # Recent command IDs (debounce cache, FIFO; see _RECENT_COMMAND_ID_CAPACITY).
        self._recent_command_ids = []
        self._pending_core0_responses = []
        self._pending_connection_logs = []
        # A HOT_RELOADED write-config is one transaction across both cores:
        # Core 0 applies its subset, Core 1's is requested on the
        # config-update lane, and the response + commit are held until
        # Core 1's ack (the run loop resolves it). _transaction_active stays
        # True for the same window (single-flight).
        self._pending_config_update = None
        # Monotonic generation for config-update requests/results; a stale
        # result can never be read as the current one.
        self._config_update_generation = 0
        self._utc_request_counter = 0
        self._pending_utc_request_id = None
        self._utc_request_deadline_ms = None
        self._utc_last_attempt_ms = None
        self._utc_snapshot = None
        self._last_network_snapshot_ms = None
        # Completion time of the last successful application PUBLISH (PUBACK
        # received); drives the publish pacing gate for every publish path.
        self._last_mqtt_publish_completed_ms = None
        self._last_command_poll_ms = time.ticks_ms()
        self._next_sequence = 0
        self._network_stack_ready = False
        # Hardware watchdog, armed by start() once the startup contract has
        # passed (startup itself is unbounded and must not be supervised);
        # None when the build lacks machine.WDT (degraded, see
        # _enable_watchdog).
        self._wdt = None

    def _uptime_ms(self):
        return current_uptime_ms(self._uptime_state)

    def _current_utc_timestamp(self):
        snapshot = self._utc_snapshot
        if snapshot is None:
            return None
        # current = runtime_start + current_uptime: both accumulate from the
        # shared boot base, so this stays correct across the tick wrap, where
        # a one-shot ticks_diff against the sync tick would not.
        return format_utc_epoch_ms(
            snapshot["runtime_start_epoch_ms"] + self._uptime_ms()
        )

    def _target_matches(self, target):
        if not is_bounded_target(target):
            return False
        if target == BROADCAST_TARGET:
            return True
        # Case-insensitive matching: only the comparison is normalized; the
        # stored source keeps its configured casing in emitted messages.
        if target.lower() == self._config["source"].lower():
            return True
        ip_address = self._wifi.ip_address()
        return isinstance(ip_address, str) and target.lower() == ip_address.lower()

    def _queue_core0_response(self, response):
        if len(self._pending_core0_responses) >= _MAX_PENDING_CORE0_RESPONSES:
            print("[WARNING] Core 0 response queue full; response rejected")
            return False
        self._pending_core0_responses.append(response)
        return True

    def _queue_connection_log(self, event, message, module, data):
        if len(self._pending_connection_logs) >= _MAX_PENDING_CONNECTION_LOGS:
            print("[WARNING] Core 0 connection log queue full; log rejected")
            return False
        self._pending_connection_logs.append({
            "message_type": "log",
            "payload": {
                "level": "info",
                "message": message,
                "event": event,
                "module": module,
                "data": data,
            },
        })
        return True

    def _service_pending_connection_log(self):
        if not self._pending_connection_logs:
            return

        message = self._pending_connection_logs[0]
        # The container is the persistent identity: uptime, timestamp, and
        # body are stamped/serialized once, and the wire sequence is claimed
        # once (by _publish_entry) -- a transport retry redelivers ONE
        # (runtime_id, sequence) pair with the same document.
        if "payload_bytes" not in message:
            message["uptime_ms"] = self._uptime_ms()
            message["timestamp"] = self._current_utc_timestamp()
            message["payload_bytes"] = serialize_and_validate_message(message)
            message["kind"] = KIND_LOG
        try:
            self._publish_entry(message)
            self._pending_connection_logs.pop(0)
        except MemoryError:
            raise
        except OutboundMessageTooLargeError as err:
            # The spliced envelope pushed the log over the wire limit:
            # permanent, and a log has no command to answer, so it is dropped
            # (entries behind it keep moving) instead of retried.
            print("[WARNING] Connection log dropped, envelope splice exceeded the per-message ceiling: {}".format(err))
            self._pending_connection_logs.pop(0)
        except (OSError, MQTTException) as err:
            # Transport failure is a link condition: the log stays pending
            # (bounded queue) for the pass after the link recovers. A
            # programming failure is NOT caught here: it escapes to the
            # top-level recovery boundary instead of being hidden.
            print("[DEBUG] Connection log publish failed, retrying: {}".format(err))

    def _on_mqtt_message(self, topic, payload):
        try:
            if isinstance(topic, bytes):
                topic = topic.decode()
            # The parser reads the frame bytes directly (MicroPython's
            # json.loads takes any buffer): no decoded-string copy sits
            # alongside the parsed object graph at the 20 KiB inbound
            # ceiling.
            doc = json.loads(payload)
        except MemoryError:
            raise
        except Exception as err:
            if DEBUG:
                print("[DEBUG] Ignoring invalid MQTT payload: {}".format(err))
            return

        # Release the raw frame now that the parse succeeded: otherwise the
        # frame bytes (up to the inbound ceiling) stay alive across the whole
        # command handler alongside the parsed graph, adding a full frame of
        # heap to every response allocation the handler makes.
        del payload

        if not isinstance(doc, dict):
            if DEBUG:
                print("[DEBUG] Ignoring MQTT payload that is not an object")
            return

        # Global inbound schema gate: an unsupported message_schema_version
        # is ignored for the whole message -- no response, no debounce
        # entry, no state change -- including inbound info_response.
        version = doc.get("message_schema_version")
        if type(version) is not int or version != MESSAGE_SCHEMA_VERSION:
            if DEBUG:
                print("[DEBUG] Ignoring inbound message with unsupported message_schema_version")
            return

        if topic == self._config["mqtt_topic_info_response"]:
            if doc.get("message_type") == "info_response":
                self._handle_info_response(doc)
            return

        if topic != self._config["mqtt_topic_command"]:
            return
        if doc.get("message_type") != "command":
            return

        # Staged validation: target and command_id drops are silent (no
        # response is possible without a bounded command_id, and foreign
        # traffic must not consume a cache entry); once the ID is claimed,
        # every failure is answered.
        target = doc.get("target")
        if not self._target_matches(target):
            return

        command_id = doc.get("command_id")
        if not is_bounded_command_id(command_id):
            # No standard response is possible without a bounded command_id:
            # drop without a response and without caching.
            return

        if self._is_recent_command_id(command_id):
            # Debounce: an already-claimed ID is ignored (no execution, event,
            # or response) -- expected transport behavior, so DEBUG-only.
            if DEBUG:
                print("[DEBUG] Duplicate command_id ignored: {}".format(command_id))
            return

        # Admit only if a response slot can be reserved first: otherwise the
        # command could execute and then lose its answer to a full queue,
        # with the sender's retry hitting the debounce cache above -- the
        # command ran, the answer never arrived. The pending HOT transaction
        # holds one slot too (reserved above) so its deferred acknowledgement
        # cannot be overrun by a new admission.
        reserved = 1 if self._pending_config_update is not None else 0
        if (
            len(self._pending_core0_responses) + reserved
            >= _MAX_PENDING_CORE0_RESPONSES
        ):
            return

        # Claim the ID before deeper validation so a malformed duplicate
        # cannot generate a second validation response.
        self._remember_command_id(command_id)
        targeted = target != BROADCAST_TARGET

        # Envelope: every top-level key must be a known v3 field; all
        # unknowns are named, sorted, in one error.
        unknown = unknown_field_names(doc, COMMAND_ENVELOPE_KEYS)
        if unknown:
            self._queue_core0_response(self._command_error(
                command_id, targeted, doc.get("command"), {
                    "code": "unknown_fields",
                    "message": "Message contains unknown fields",
                    "unknown_fields": unknown,
                },
            ))
            return

        command = doc.get("command")
        if not isinstance(command, str) or not command:
            self._queue_core0_response(self._command_error(
                command_id, targeted, command, {
                    "code": "invalid_command",
                    "message": "Command must be a non-empty string",
                },
            ))
            return
        if "payload" not in doc:
            self._queue_core0_response(self._command_error(
                command_id, targeted, command, {
                    "code": "invalid_payload",
                    "message": "command payload is required",
                },
            ))
            return

        # An over-long name is answered with a bounded error and never
        # echoed: echoing it back would build the oversized response the
        # bound exists to prevent.
        if len(command) > MAX_COMMAND_LENGTH:
            self._queue_core0_response(self._command_error(
                command_id, targeted, command, {
                    "code": "invalid_command",
                    "message": "Command name exceeds the {} character bound".format(MAX_COMMAND_LENGTH),
                },
            ))
            return

        payload_obj = doc["payload"]
        if not isinstance(payload_obj, dict):
            self._queue_core0_response(self._command_error(
                command_id, targeted, command, {
                    "code": "invalid_payload",
                    "message": "command payload must be an object",
                },
            ))
            return

        # Ownership dispatch: only a supported name proceeds. An unregistered
        # bounded name is answered here (Core 1 is not the generic
        # fallback); the actual name rides in the standard command field,
        # not duplicated inside error.
        if not is_supported_command(command):
            self._queue_core0_response(self._command_error(
                command_id, targeted, command, {
                    "code": "unsupported_command",
                    "message": "Unsupported command",
                },
            ))
            return

        # write-config never applies fleet-wide: no validation, no file
        # operation, no response (the ID stays claimed -- this is a
        # message-debounce mechanism).
        if command == COMMAND_WRITE_CONFIG and target == BROADCAST_TARGET:
            return

        if command == COMMAND_REBOOT:
            self._handle_reboot_command(command_id, targeted, payload_obj)
            return

        if command == COMMAND_GET_DETAILS:
            # Core 1 owns execution; dispatch the validated bounded event.
            self._handle_get_details_command(command_id, targeted, payload_obj)
            return

        if command == COMMAND_READ_CONFIG:
            self._handle_read_config_command(command_id, targeted, payload_obj)
            return

        if command == COMMAND_WRITE_CONFIG:
            self._handle_write_config_command(command_id, targeted, payload_obj)
            return

    def _command_error(self, command_id, targeted, command, error):
        """A bounded command error response; an unbounded command name is never echoed."""
        response = {
            "command_id": command_id,
            "success": False,
            "targeted": targeted,
            "error": error,
        }
        if is_bounded_command(command):
            response["command"] = command
        return response

    def _is_response_substitute(self, response):
        """True if the response is already one of the bounded substitutes."""
        error = response.get("error")
        return (
            response.get("success") is False
            and isinstance(error, dict)
            and error.get("code") in _SUBSTITUTE_ERROR_MESSAGES
        )

    def _handle_reboot_command(self, command_id, targeted, payload):
        """Reboot handler, after the common protocol validation. Payload is
        exactly {}: a non-empty object is answered with unknown_fields (sorted)."""
        if payload:
            self._queue_core0_response(
                self._unknown_fields_error(COMMAND_REBOOT, command_id, targeted, payload)
            )
            return

        if self._pending_reboot is not None:
            self._queue_core0_response({
                "command_id": command_id,
                "command": COMMAND_REBOOT,
                "success": False,
                "targeted": targeted,
                "error": {
                    "code": "reboot_already_pending",
                    "message": "A reboot is already pending",
                },
            })
            return

        self._pending_reboot = {
            "command_id": command_id,
            "command": COMMAND_REBOOT,
            "targeted": targeted,
        }

    def _unknown_fields_error(self, command, command_id, targeted, payload):
        """Shared contract for {}-payload commands: any key is unknown, named
        in a sorted unknown_fields array so the sender knows what to remove."""
        return {
            "command_id": command_id,
            "command": command,
            "success": False,
            "targeted": targeted,
            "error": {
                "code": "unknown_fields",
                "message": "{} payload contains unknown fields".format(command),
                "unknown_fields": sorted(payload.keys()),
            },
        }

    def _handle_get_details_command(self, command_id, targeted, payload):
        """get-details dispatch: Core 1 owns execution (it holds the
        SystemInformation instance and device state), so Core 0 only forwards
        the validated event. Payload is exactly {} (shared with reboot); an
        admission failure means the free-heap reserve could not be restored."""
        if payload:
            self._queue_core0_response(
                self._unknown_fields_error(COMMAND_GET_DETAILS, command_id, targeted, payload)
            )
            return

        event = {
            "command_id": command_id,
            "command": COMMAND_GET_DETAILS,
            "payload": {},
            "targeted": targeted,
        }
        if not self._intercore.event_queue.put(event):
            self._queue_core0_response({
                "command_id": command_id,
                "command": COMMAND_GET_DETAILS,
                "success": False,
                "targeted": targeted,
                "error": {
                    "code": "intercore_event_queue_memory_pressure",
                    "message": "Insufficient free heap to queue the Core 1 event",
                },
            })

    def _handle_read_config_command(self, command_id, targeted, payload):
        """read-config: answer with the committed (PERSISTED) configuration
        and derived reboot state. Payload is exactly {}; Wi-Fi secrets never
        cross this path; a missing/invalid committed file is answered with
        the actual cause (boot recovery normally guarantees a valid one)."""
        if payload:
            self._queue_core0_response(
                self._unknown_fields_error(COMMAND_READ_CONFIG, command_id, targeted, payload)
            )
            return

        try:
            config = self._config_manager.read_persisted()
        except MemoryError:
            raise
        except ConfigError as err:
            self._queue_core0_response(self._command_error(
                command_id, targeted, COMMAND_READ_CONFIG, {
                    "code": "config_unavailable",
                    "message": str(err),
                },
            ))
            return

        self._queue_core0_response({
            "command_id": command_id,
            "command": COMMAND_READ_CONFIG,
            "success": True,
            "targeted": targeted,
            "data": {
                "config": config,
                "reboot_required": self._config_manager.reboot_required,
            },
        })

    def _handle_write_config_command(self, command_id, targeted, payload):
        """write-config: the payload is exactly {"config": <complete
        candidate>} -- no patch/merge/partial shape -- validated by the same
        validate_config() as startup. UNCHANGED writes nothing; REBOOT_REQUIRED
        commits; HOT_RELOADED is applied on both cores and committed once Core
        1 acknowledges, rolled back with the file restored if it cannot be.
        MemoryError propagates to the fail-fast boundary."""
        # Unknown payload keys are named together (sorted) regardless of the
        # rest of the payload.
        unknown = sorted(key for key in payload if key != "config")
        if unknown:
            self._queue_core0_response(self._command_error(
                command_id, targeted, COMMAND_WRITE_CONFIG, {
                    "code": "unknown_fields",
                    "message": "write-config payload contains unknown fields",
                    "unknown_fields": unknown,
                },
            ))
            return

        if "config" not in payload:
            self._queue_core0_response(self._command_error(
                command_id, targeted, COMMAND_WRITE_CONFIG, {
                    "code": "missing_key",
                    "message": "write-config payload is missing the required config key",
                },
            ))
            return

        candidate = payload["config"]
        if not isinstance(candidate, dict):
            self._queue_core0_response(self._command_error(
                command_id, targeted, COMMAND_WRITE_CONFIG, {
                    "code": "invalid_value",
                    "message": "write-config payload config must be an object",
                },
            ))
            return

        if self._config_manager.transaction_active:
            # A hot transaction is pending Core 1's acknowledgement:
            # single-flight, so a second write is refused with a bounded,
            # non-executing answer.
            self._queue_core0_response(self._command_error(
                command_id, targeted, COMMAND_WRITE_CONFIG, {
                    "code": "config_update_in_progress",
                    "message": "A configuration update is already in progress",
                },
            ))
            return

        try:
            result = self._config_manager.begin_write(candidate)
        except MemoryError:
            raise
        except ConfigError as err:
            error = {
                "code": err.code if err.code is not None else "invalid_config",
                "message": str(err),
            }
            if err.unknown_fields:
                error["unknown_fields"] = err.unknown_fields
            # Structured error fields (e.g. expected/received schema version)
            # cross into the response as-is.
            if err.details:
                error.update(err.details)
            self._queue_core0_response(self._command_error(
                command_id, targeted, COMMAND_WRITE_CONFIG, error
            ))
            return

        if not result["pending"]:
            if result["classification"] == CLASSIFICATION_UNCHANGED:
                message = ("[WARNING] write-config ignored: submitted "
                           "configuration is identical to persisted "
                           "configuration")
                if self._config_manager.reboot_required:
                    message += "; a reboot-required configuration remains pending"
                print(message)
            # UNCHANGED: no write, reboot state preserved. REBOOT_REQUIRED:
            # committed, no runtime application owed.
            self._queue_core0_response(
                self._write_config_success(command_id, targeted, result)
            )
            return

        # HOT_RELOADED: one transaction across both cores. No success
        # response until Core 1's ack (run loop); a failed apply rolls Core 0
        # and the file back so committed and running configuration agree.
        old_values, core1_update = self._apply_hot_changes(result["changes"])
        if not core1_update:
            # Only Core 0-owned settings changed: nothing owed to Core 1,
            # commit now.
            self._config_manager.commit_hot_reload()
            self._queue_core0_response(
                self._write_config_success(command_id, targeted, result)
            )
            return

        generation = self._next_config_update_generation()
        request = {"generation": generation}
        request.update(core1_update)
        self._intercore.config_update_lane.post_request(request)
        # Hold the response and rollback state until Core 1's ack (the run
        # loop resolves it); _transaction_active stays True meanwhile
        # (single-flight).
        self._pending_config_update = {
            "generation": generation,
            "old_values": old_values,
            "command_id": command_id,
            "targeted": targeted,
            "result": result,
        }

    def _write_config_success(self, command_id, targeted, result):
        classification = result["classification"]
        return {
            "command_id": command_id,
            "command": COMMAND_WRITE_CONFIG,
            "success": True,
            "targeted": targeted,
            "data": {
                # This command's effect on the persisted config, independent
                # of the reboot state it left behind.
                "configuration_changed": classification != CLASSIFICATION_UNCHANGED,
                "classification": classification,
                # Derived at response time: True for REBOOT_REQUIRED, False
                # for UNCHANGED without a pending reboot or after a hot
                # commit cancels one.
                "reboot_required": self._config_manager.reboot_required,
                "changes": result["changes"],
            },
        }

    def _apply_hot_changes(self, changes):
        """Apply changed Core 0 HOT settings to the live config; return the
        old values (for rollback, incl. the last-poll stamp) and the Core 1
        update ({} when none)."""
        old_values = {}
        core1_update = {}
        for change in changes:
            setting = change["setting"]
            if setting in _HOT_APPLY_CORE0_KEYS:
                old_values[setting] = self._config[setting]
                self._config[setting] = change["new_value"]
                if setting == "mqtt_command_poll_ms":
                    # Re-anchor the last-poll stamp to the reload instant so
                    # the new cadence starts cleanly.
                    old_values[_POLL_STAMP_KEY] = self._last_command_poll_ms
                    self._last_command_poll_ms = time.ticks_ms()
            elif setting in _HOT_APPLY_CORE1_KEYS:
                core1_update[setting] = change["new_value"]
        return old_values, core1_update

    def _restore_hot_values(self, old_values):
        for setting, value in old_values.items():
            if setting == _POLL_STAMP_KEY:
                self._last_command_poll_ms = value
            else:
                self._config[setting] = value

    def _next_config_update_generation(self):
        self._config_update_generation += 1
        return self._config_update_generation

    def _resolve_pending_config_update(self):
        """Resolve a pending HOT apply once Core 1's ack is on the lane:
        success commits (releasing the retained config.json.old) and releases
        the held response; a failed apply rolls back and answers a bounded
        error. A dead Core 1 never acks, but the liveness watchdog bounds
        that (board reset; boot recovery restores config.json.old)."""
        pending = self._pending_config_update
        if pending is None:
            return
        result = self._intercore.config_update_lane.take_result_for(
            pending["generation"]
        )
        if result is None:
            return
        self._pending_config_update = None
        if result.get("success"):
            self._config_manager.commit_hot_reload()
            self._queue_core0_response(self._write_config_success(
                pending["command_id"], pending["targeted"], pending["result"]
            ))
        else:
            self._restore_hot_values(pending["old_values"])
            self._config_manager.rollback_hot_reload()
            self._queue_core0_response(self._command_error(
                pending["command_id"], pending["targeted"], COMMAND_WRITE_CONFIG, {
                    "code": result.get("code") or "core1_apply_failed",
                    "message": "Core 1 could not apply the configuration update",
                },
            ))

    def _is_recent_command_id(self, command_id):
        """True if command_id is still claimed in the debounce cache (exact, case-sensitive)."""
        return command_id in self._recent_command_ids

    def _remember_command_id(self, command_id):
        """Claim an ID in the debounce cache; evict the oldest once over capacity (FIFO, not LRU)."""
        self._recent_command_ids.append(command_id)
        if len(self._recent_command_ids) > _RECENT_COMMAND_ID_CAPACITY:
            self._recent_command_ids.pop(0)

    def _service_pending_core0_response(self):
        if not self._pending_core0_responses:
            return

        response = self._pending_core0_responses[0]
        # Claim (or reuse, on retry) the wire sequence on the persistent
        # response so a re-publish keeps the same (runtime_id, sequence).
        wire_sequence = self._claim_wire_sequence(response)
        published = self._publish_core0_command_response(
            response["command_id"],
            response.get("command"),
            response["success"],
            targeted=response.get("targeted", False),
            data=response.get("data"),
            error=response.get("error"),
            wire_sequence=wire_sequence,
            container=response,
        )
        if not published:
            failure = response.get("_permanent_failure")
            if failure is None or self._is_response_substitute(response):
                # No recorded cause (or already the bounded substitute):
                # hold it for a later pass.
                return
            # A permanent failure can never succeed by retrying the same
            # bytes, and the queue is FIFO: answer the command with the
            # bounded substitute whose code states the cause (it publishes on
            # a later pass).
            print("[WARNING] Core 0 command response {}: answering with a "
                  "bounded error response".format(failure))
            self._pending_core0_responses[0] = self._command_error(
                response["command_id"],
                response.get("targeted", False),
                response.get("command"),
                {
                    "code": failure,
                    "message": _SUBSTITUTE_ERROR_MESSAGES[failure],
                },
            )
            return
        self._pending_core0_responses.pop(0)
        # The substitute for an unsendable reboot acknowledgement is
        # published: the command was reported failed, so release the held
        # reboot.
        if response.get("_clears_pending_reboot"):
            self._pending_reboot = None

    def _handle_info_response(self, doc):
        if doc.get("source") not in ("server", self._config["source"]):
            return
        if doc.get("target") != self._config["source"]:
            return
        if doc.get("request_type") != "utc_time":
            return
        if self._pending_utc_request_id is None:
            return
        if doc.get("request_id") != self._pending_utc_request_id:
            return

        # A malformed answer to our request is still an answer: re-key the
        # throttle for a prompt retry instead of waiting out the deadline or
        # 30 s interval.
        payload = doc.get("payload")
        if not isinstance(payload, dict):
            self._utc_note_reachable_failure()
            return

        timestamp = payload.get("timestamp")
        utc_epoch_ms = payload.get("utc_epoch_ms")
        if not isinstance(timestamp, str) or not timestamp:
            self._utc_note_reachable_failure()
            return
        if (
            isinstance(utc_epoch_ms, bool)
            or not isinstance(utc_epoch_ms, int)
            or utc_epoch_ms <= 0
        ):
            self._utc_note_reachable_failure()
            return

        try:
            normalized_timestamp = format_utc_epoch_ms(utc_epoch_ms)
        except MemoryError:
            raise
        except Exception as err:
            self._utc_note_reachable_failure()
            if DEBUG:
                print("[DEBUG] UTC response rejected - invalid epoch: {}".format(err))
            return

        # Anchor the snapshot to the accumulated uptime, not a raw ticks_ms
        # sample: a one-shot ticks_diff is only guaranteed within half a tick
        # period, and a stale one-shot diff could block the very refresh that
        # repairs the clock.
        sync_uptime_ms = self._uptime_ms()
        snapshot = {
            "timestamp": normalized_timestamp,
            "utc_epoch_ms": utc_epoch_ms,
            "sync_uptime_ms": sync_uptime_ms,
            "runtime_start_epoch_ms": utc_epoch_ms - sync_uptime_ms,
        }
        self._utc_snapshot = snapshot
        self._intercore.state_mailboxes.set_utc_snapshot(snapshot)
        self._utc_clear_pending()
        print("[INFO] UTC synchronized: {}".format(normalized_timestamp))

    def _topic_for_kind(self, kind):
        if kind == KIND_TELEMETRY:
            return self._config["mqtt_topic_telemetry"]
        if kind == KIND_COMMAND_RESPONSE:
            return self._config["mqtt_topic_command_response"]
        if kind == KIND_HEALTH:
            return self._config["mqtt_topic_health"]
        if kind == KIND_LOG:
            return self._config["mqtt_topic_log"]
        raise ValueError("Unsupported outbound message kind: {}".format(kind))

    def _envelope_fragment(self, sequence):
        """Serialize the five Core-0-owned envelope members as a braceless
        fragment; senders must not carry any of these keys at the top level."""
        fragment = json.dumps({
            "sequence": sequence,
            "runtime_id": self._runtime_id,
            "source": self._config["source"],
            "firmware_version": FIRMWARE_VERSION,
            "message_schema_version": MESSAGE_SCHEMA_VERSION,
        })
        return fragment[1:-1].encode("utf-8")

    def _claim_wire_sequence(self, container):
        """Claim the next wire sequence and stamp it on ``container`` (never
        rolled back); a retry reuses it and a different message always gets a fresh one."""
        sequence = container.get("_wire_sequence")
        if sequence is None:
            sequence = self._next_sequence
            self._next_sequence += 1
            container["_wire_sequence"] = sequence
        return sequence

    # --- Outbound publish pacing (mqtt_outbound_publish_delay_ms) --------
    #
    # A minimum quiet period between consecutive outbound application
    # PUBLISHes, measured from the previous publish's COMPLETION (PUBACK),
    # not its start. It is state, not a sleep: while the gate is closed Core
    # 0 keeps running its normal loop and simply does not begin another
    # application PUBLISH. Protocol-control traffic (PINGREQ) is never paced.

    def _mqtt_publish_ready(self):
        delay_ms = self._config["mqtt_outbound_publish_delay_ms"]
        if delay_ms == 0:
            return True
        if self._last_mqtt_publish_completed_ms is None:
            return True
        return time.ticks_diff(
            time.ticks_ms(),
            self._last_mqtt_publish_completed_ms,
        ) >= delay_ms

    def _note_mqtt_publish_completed(self):
        self._last_mqtt_publish_completed_ms = time.ticks_ms()

    def _wait_for_mqtt_publish_slot(self):
        """Block in 10 ms slices until a publish may begin. Startup-only: never call from the run loop."""
        while not self._mqtt_publish_ready():
            time.sleep_ms(10)

    def _publish_entry(self, entry):
        """Publish one entry from its pre-serialized bytes, envelope spliced
        in before the closing brace (never decoded/re-serialized here). The
        final spliced length must stay within MAX_OUTBOUND_MESSAGE_BYTES or
        OutboundMessageTooLargeError is raised -- permanent for the entry."""
        payload = entry["payload_bytes"]
        if not isinstance(payload, (bytes, bytearray)) or bytes(payload[-1:]) != b"}":
            raise ValueError("queued payload must be a serialized JSON object")
        # Normalize bytearray to bytes: the wire view must be a stable
        # buffer held for the whole QoS 1 exchange.
        body = payload if isinstance(payload, bytes) else bytes(payload)
        sequence = self._claim_wire_sequence(entry)
        fragment = self._envelope_fragment(sequence)
        # The spliced envelope is added on top of the admitted body: enforce
        # the ceiling against the FINAL wire length, before the frame goes
        # out. Permanent for this entry (its bytes are fixed) -- callers
        # answer a command response with the bounded substitute and discard
        # the other kinds.
        wire_length = len(body) + len(fragment) + 1
        if wire_length > MAX_OUTBOUND_MESSAGE_BYTES:
            raise OutboundMessageTooLargeError(
                "Outbound message too large after envelope splice: {} > {}".format(
                    wire_length, MAX_OUTBOUND_MESSAGE_BYTES
                )
            )
        topic = entry.get("topic")
        if topic is None:
            topic = self._topic_for_kind(entry["kind"])
        # Segment the spliced write at the wire: no frame-sized allocation
        # on the fragmented post-startup heap (rationale: MQTTClient.publish).
        self._mqtt.publish_qos1(topic, body, splice_fragment=fragment)
        # PUBACK received: the publish is complete, so the pacing interval
        # begins (a failed publish raises before this line and records nothing).
        self._note_mqtt_publish_completed()
        if entry.get("kind") == KIND_TELEMETRY:
            self._led_manager.telemetry_sent()
        if DEBUG:
            print("[DEBUG] QoS 1 published: seq={}".format(sequence))

    def _answer_discarded_command_response(self, entry):
        """Queue the bounded substitute for a discarded oversized command
        response: the acknowledgement is owed, so answer with a small
        response_too_large error, reading only bounded identifying fields back
        (an unreadable body has no identity to answer with -- the discard stands)."""
        body = entry["payload_bytes"]
        if isinstance(body, bytearray):
            body = bytes(body)
        try:
            doc = json.loads(body.decode("utf-8"))
        except MemoryError:
            raise
        except Exception as err:
            print("[WARNING] Discarded command response could not be read back; no substitute: {}".format(err))
            return
        payload = doc.get("payload") if isinstance(doc, dict) else None
        if not isinstance(payload, dict):
            return
        command_id = payload.get("command_id")
        if not is_bounded_command_id(command_id):
            return
        command = payload.get("command")
        targeted = payload.get("targeted") is True
        self._queue_core0_response(
            self._command_error(
                command_id,
                targeted,
                command,
                {
                    "code": "response_too_large",
                    "message": _SUBSTITUTE_ERROR_MESSAGES["response_too_large"],
                },
            )
        )

    def _publish_core0_command_response(
        self, command_id, command, success, targeted=True, data=None, error=None,
        wire_sequence=None, container=None
    ):
        """Build and publish a Core 0 command response (pre-serialized); the
        container lets a retry keep the claimed identity and bytes. True on
        publish, False on a permanent failure (cause recorded on the container
        for the matching bounded substitute); MemoryError propagates."""
        payload_bytes = container.get("_payload_bytes") if container is not None else None
        if payload_bytes is None:
            payload = {
                "command_id": command_id,
            }
            # The command field is carried only when bounded and valid; an
            # over-long name is omitted rather than echoed.
            if command is not None:
                payload["command"] = command
            payload["targeted"] = targeted
            payload["success"] = success
            if success:
                payload["data"] = data
            else:
                payload["error"] = error

            # Uptime and timestamp are the sender's to carry (envelope spliced
            # at publish time): capture them at construction, not publish, time.
            message = {
                "message_type": "command_response",
                "uptime_ms": self._uptime_ms(),
                "timestamp": self._current_utc_timestamp(),
                "payload": payload,
            }

            try:
                payload_bytes = serialize_and_validate_message(message)
            except MemoryError:
                raise
            except MessageTooLargeError as err:
                if DEBUG:
                    print("[DEBUG] Command response exceeded the size limit: {}".format(err))
                if container is not None:
                    container["_permanent_failure"] = "response_too_large"
                return False
            except Exception as err:
                if DEBUG:
                    print("[DEBUG] Command response serialization failed: {}".format(err))
                if container is not None:
                    container["_permanent_failure"] = "response_invalid"
                return False
            if container is not None:
                # Freeze the bytes on the container so a retry re-publishes
                # the same document instead of a newer uptime/timestamp.
                container["_payload_bytes"] = payload_bytes

        entry = {
            "topic": self._config["mqtt_topic_command_response"],
            "kind": KIND_COMMAND_RESPONSE,
            "payload_bytes": payload_bytes,
        }
        # Carry the claimed sequence into the entry (a retry keeps it; a first
        # attempt claims a fresh one in _publish_entry).
        if wire_sequence is not None:
            entry["_wire_sequence"] = wire_sequence
        try:
            self._publish_entry(entry)
        except OutboundMessageTooLargeError as err:
            # The spliced envelope pushed the final wire length over the
            # ceiling: permanent for these bytes -- record the cause so the
            # servicing path answers with the matching bounded substitute.
            if DEBUG:
                print("[DEBUG] Command response too large after envelope splice: {}".format(err))
            if container is not None:
                container["_permanent_failure"] = "response_too_large"
            return False
        return True

    def _reboot_publish_due(self):
        # The reboot response is an outbound PUBLISH: hold (without
        # resetting, without blocking) until the link is up and the
        # pacing gate is open — like every other publish path, an attempt
        # on a down link would fail immediately, so it is not one.
        return (
            self._pending_reboot is not None
            and self._mqtt.is_connected()
            and self._mqtt_publish_ready()
        )

    def _perform_reboot(self):
        request = self._pending_reboot
        if request is None:
            return True
        if self._intercore.outbound_queue.has_in_flight():
            return False
        # The acknowledgement was permanently unsendable and already answered
        # with the bounded substitute: terminal -- never re-attempt the bytes
        # or queue the substitute twice; the servicing path releases the
        # held reboot.
        if request.get("_permanent_failure_answer_queued"):
            return False

        try:
            # Claim (or reuse) the wire sequence so a re-publish keeps the
            # same identity.
            wire_sequence = self._claim_wire_sequence(request)
            published = self._publish_core0_command_response(
                request["command_id"],
                request["command"],
                True,
                targeted=request.get("targeted", False),
                data={"rebooting": True},
                wire_sequence=wire_sequence,
                container=request,
            )
        except MemoryError:
            raise
        except (OSError, MQTTException) as err:
            # Transport failure is a link condition: the reboot stays
            # pending. A programming failure escapes to the top-level
            # recovery boundary.
            if DEBUG:
                print("[DEBUG] Reboot response publish failed; reboot remains pending: {}".format(err))
            return False
        if not published:
            failure = request.get("_permanent_failure")
            if failure is not None:
                # A permanent failure can never publish by retrying the same
                # bytes: answer with the bounded substitute and hold the
                # reboot (a reset with no acknowledgement is worse) until the
                # substitute is published.
                print("[WARNING] Reboot response {}: answering with a "
                      "bounded error response".format(failure))
                substitute = self._command_error(
                    request["command_id"],
                    request.get("targeted", False),
                    request.get("command"),
                    {
                        "code": failure,
                        "message": _SUBSTITUTE_ERROR_MESSAGES[failure],
                    },
                )
                # The servicing path releases the held reboot when it
                # publishes the substitute.
                substitute["_clears_pending_reboot"] = True
                if self._queue_core0_response(substitute):
                    # Only now is the request terminal: a full queue left the
                    # answer unsent, so a later pass must retry the answer
                    # rather than treating it as already given.
                    request["_permanent_failure_answer_queued"] = True
                return False
            # The success acknowledgement was never published; resetting now
            # would reboot without an answer. The reboot stays pending and is
            # retried on a later pass instead of discarding the response.
            if DEBUG:
                print("[DEBUG] Reboot response serialization failed; reboot remains pending")
            return False

        self._pending_reboot = None
        print("[INFO] Rebooting in 5000 milliseconds")
        # Sliced, like every other long run-loop wait: a monolithic sleep plus
        # the publish wait that preceded it could stretch past WDT_TIMEOUT_MS,
        # and the board would reset via the hardware watchdog instead of this
        # explicit path (a watchdog trip that was really a planned reboot).
        self._sleep_and_service(5)
        print("[INFO] machine.reset()")
        machine.reset()
        return True

    def _publish_network_snapshot(self, force=False):
        now_ms = time.ticks_ms()
        interval_ms = self._config["network_snapshot_interval_sec"] * 1000
        if not force and self._last_network_snapshot_ms is not None:
            if time.ticks_diff(now_ms, self._last_network_snapshot_ms) < interval_ms:
                return

        mqtt_status = self._mqtt.status()
        snapshot = self._wifi.snapshot(mqtt_status["connected"])
        snapshot["mqtt_connect_count"] = mqtt_status["connect_count"]
        snapshot["mqtt_disconnect_count"] = mqtt_status["disconnect_count"]
        snapshot["network_stack_ready"] = self._network_stack_ready
        self._intercore.state_mailboxes.set_network_snapshot(snapshot)
        self._last_network_snapshot_ms = now_ms

    def _utc_clear_pending(self):
        self._pending_utc_request_id = None
        self._utc_request_deadline_ms = None

    def _utc_note_reachable_failure(self):
        """Clear a malformed-payload pending request; re-key the throttle for a prompt retry."""
        self._utc_clear_pending()
        self._utc_last_attempt_ms = time.ticks_add(
            time.ticks_ms(),
            _UTC_PROMPT_RETRY_DELAY_MS - _UTC_RETRY_INTERVAL_MS,
        )

    def _utc_send_request(self):
        """Send a UTC time request without blocking; deadline tracked for the run loop.

        The pending request ID is armed before publishing: a fast response can arrive inside the PUBACK wait."""
        self._utc_request_counter += 1
        request_id = "{}_{}".format(self._runtime_id, self._utc_request_counter)
        request = {
            "message_type": "info_request",
            "message_schema_version": MESSAGE_SCHEMA_VERSION,
            "source": self._config["source"],
            "request_id": request_id,
            "request_type": "utc_time",
            "payload": {},
        }
        self._pending_utc_request_id = request_id

        try:
            self._mqtt.publish_qos1(
                self._config["mqtt_topic_info_request"],
                json.dumps(request),
            )
        except MemoryError:
            self._pending_utc_request_id = None
            raise
        except (OSError, MQTTException) as err:
            # Roll back the armed ID: no response can ever arrive for a
            # request that was not delivered. A programming failure escapes
            # to the top-level recovery boundary.
            self._pending_utc_request_id = None
            if DEBUG:
                print("[DEBUG] UTC request publish failed: {}".format(err))
            return

        self._note_mqtt_publish_completed()
        self._utc_last_attempt_ms = time.ticks_ms()
        timeout_ms = self._config["mqtt_broker_response_timeout_sec"] * 1000
        self._utc_request_deadline_ms = time.ticks_add(time.ticks_ms(), timeout_ms)

    def _utc_wait_response(self):
        """Pump MQTT until the pending UTC response arrives or its deadline (startup-only)."""
        while self._pending_utc_request_id is not None:
            if time.ticks_diff(
                time.ticks_ms(), self._utc_request_deadline_ms
            ) >= 0:
                break
            try:
                self._mqtt.check_msg()
            except MemoryError:
                raise
            except (OSError, MQTTException) as err:
                # A transport failure fails the attempt cleanly; a
                # programming failure (the realistic source: the inbound
                # callback) escapes to the top-level recovery boundary.
                if DEBUG:
                    print("[DEBUG] UTC response wait failed: {}".format(err))
                break
            time.sleep_ms(20)
        self._utc_clear_pending()

    def _utc_request_expired(self):
        if self._pending_utc_request_id is None:
            return
        if time.ticks_diff(
            time.ticks_ms(), self._utc_request_deadline_ms
        ) >= 0:
            self._utc_clear_pending()
            if DEBUG:
                print("[DEBUG] UTC request timed out")

    def _utc_should_send_request(self):
        if self._pending_utc_request_id is not None:
            return False
        if not self._utc_sync_due():
            return False
        if self._utc_last_attempt_ms is not None and time.ticks_diff(
            time.ticks_ms(), self._utc_last_attempt_ms
        ) < _UTC_RETRY_INTERVAL_MS:
            return False
        return True

    def _utc_sync_due(self):
        if self._utc_snapshot is None:
            return True
        interval_ms = self._config["datetime_sync_interval_min"] * 60 * 1000
        # current_uptime - sync_uptime: correct for any duration, where
        # ticks_diff(now, sync_ticks) is only valid within half a tick period
        # and would block the refresh that repairs the clock on a long outage.
        return (
            self._uptime_ms() - self._utc_snapshot["sync_uptime_ms"]
            >= interval_ms
        )

    def _perform_network_probe(self):
        """Perform a QoS 1 network probe and verify matching PUBACK (startup-only, paced)."""
        self._wait_for_mqtt_publish_slot()
        probe_packet_id = self._mqtt.get_next_packet_id()

        probe_message = json.dumps({
            "message_type": "network_probe",
            "runtime_id": self._runtime_id,
            "uptime_ms": self._uptime_ms(),
            "packet_id": probe_packet_id,
        })

        timeout_ms = self._config["network_probe_timeout_sec"] * 1000
        try:
            # publish_qos1_with_packet_id will block until matching PUBACK arrives
            # or timeout occurs (via socket timeout)
            result = self._mqtt.publish_qos1_with_packet_id(
                self._config["mqtt_topic_network_probe"],
                probe_message,
                probe_packet_id,
                timeout_ms=timeout_ms,
            )
            if result:
                # A matched PUBACK is a completed publish: it opens the pacing
                # gate for the following startup publishes like any other.
                self._note_mqtt_publish_completed()
            return result
        except MemoryError:
            raise
        except (OSError, MQTTException) as err:
            # A transport failure is a link condition (the pass
            # re-establishes and retries); a programming failure escapes to
            # the top-level recovery boundary.
            if DEBUG:
                print("[DEBUG] Network probe failed: {}".format(err))
            return False

    def _drain_startup_mqtt_work(self):
        """Drain pending Core 0 MQTT work; True when none remains, False on timeout.

        Each head log gets its own grace window: it starts when the head
        changes and is NOT re-armed by failed attempts, so a dead or
        blackholed link (whose publish fails and leaves the same head
        pending) fails the pass -- which re-establishes the network and
        retries the contract -- instead of retrying the same head forever
        with a fresh window every ~100 ms. A slow-but-legal QoS 1 cycle on
        one log (up to mqtt_broker_response_timeout_sec) completes it and
        the next log starts its own fresh window, so it cannot consume the
        logs behind it."""
        grace_ms = 2000  # per-head-log grace before the pass times out

        while self._pending_connection_logs:
            deadline_ms = time.ticks_add(time.ticks_ms(), grace_ms)
            while self._pending_connection_logs:
                head = self._pending_connection_logs[0]
                self._wait_for_mqtt_publish_slot()
                if time.ticks_diff(time.ticks_ms(), deadline_ms) >= 0:
                    print("[WARNING] Startup MQTT work drain timeout")
                    return False
                # No wrapper of its own: _service_pending_connection_log()
                # already owns MemoryError, the size rejection, and transport
                # failures; a programming failure must escape to the top-level
                # recovery boundary.
                self._service_pending_connection_log()
                if (
                    self._pending_connection_logs
                    and self._pending_connection_logs[0] is not head
                ):
                    # The head log completed (published or discarded): the
                    # next head starts its own fresh window.
                    break

        return True

    def _synchronize_utc_required(self):
        """Run one bounded pass of startup UTC sync; True once a snapshot is
        acquired, False after _UTC_STARTUP_MAX_ATTEMPTS. A MemoryError or
        programming failure propagates to the recovery boundary in main()."""
        for attempt in range(_UTC_STARTUP_MAX_ATTEMPTS):
            # Respect the pacing interval left by the preceding startup
            # publish.
            self._wait_for_mqtt_publish_slot()
            self._utc_send_request()
            self._utc_wait_response()
            if self._utc_snapshot is not None:
                return True
            time.sleep_ms(500)

        print("[WARNING] UTC synchronization pass failed after {} attempts; will re-establish and retry".format(_UTC_STARTUP_MAX_ATTEMPTS))
        return False

    def establish_network(self):
        self._led_manager.set_connecting(True)

        while not self._wifi.is_connected():
            if self._wifi.connect():
                snapshot = self._wifi.snapshot(False)
                self._queue_connection_log(
                    "wifi_connection_established",
                    "Connected to Wi-Fi",
                    "wifi",
                    {
                        "ssid": snapshot["ssid"],
                        "ip_address": snapshot["ip_address"],
                        "rssi": snapshot["rssi"],
                        "connect_count": snapshot["wifi_connect_count"],
                    },
                )
                break
            delay_sec = self._config["wifi_reconnect_delays_sec"][-1]
            print("[WARNING] Wi-Fi connection sequence exhausted; retrying in {} sec".format(delay_sec))
            self._sleep_and_service(delay_sec)

        while not self._mqtt.is_connected():
            if self._mqtt.connect():
                mqtt_status = self._mqtt.status()
                self._queue_connection_log(
                    "mqtt_connection_established",
                    "Connected to MQTT broker",
                    "mqtt",
                    {
                        "broker_address": self._config["mqtt_broker_ip_address"],
                        "connect_count": mqtt_status["connect_count"],
                    },
                )
                # LED remains flashing during network probe and UTC sync
                break
            delay_sec = self._config["mqtt_reconnect_delays_sec"][-1]
            # Name the final cause once per exhausted sequence; no
            # per-attempt or stack logging.
            last_error = self._mqtt.last_connect_error
            if last_error is not None:
                print("[WARNING] MQTT connection sequence exhausted: {}; retrying in {} sec".format(last_error, delay_sec))
            else:
                print("[WARNING] MQTT connection sequence exhausted; retrying in {} sec".format(delay_sec))
            self._sleep_and_service(delay_sec)

    def _recover_network_if_needed(self):
        if self._wifi.is_connected() and self._mqtt.is_connected():
            return

        # Report the outage immediately so the snapshot (and Core 1 health
        # gating) reflects the loss; the flag is restored after
        # re-establishment.
        self._network_stack_ready = False
        if not self._wifi.is_connected():
            # Wi-Fi loss implies MQTT loss; drop the stale session state.
            self._mqtt.mark_disconnected()
        self._publish_network_snapshot(force=True)
        self.establish_network()
        self._network_stack_ready = True
        self._led_manager.set_connecting(False)
        self._publish_network_snapshot(force=True)

    def _watch_core_1_heartbeat(self):
        """Watch Core 1's liveness heartbeat and reset the MCU when stale;
        no-op before Core 1's first stamp so the unbounded startup connect
        loops are unaffected."""
        last_activity_ms = self._intercore.state_mailboxes.get_core_1_activity_ms()
        if last_activity_ms is None:
            return
        age_ms = time.ticks_diff(time.ticks_ms(), last_activity_ms)
        if age_ms >= _CORE_1_HEARTBEAT_STALE_TIMEOUT_MS:
            print("[FATAL] Core 1 heartbeat stale ({} ms) - resetting".format(age_ms))
            machine.reset()

    def _feed_watchdog(self):
        """Feed the hardware watchdog; a no-op before arming (or when the
        build lacks machine.WDT). Fed only from Core 0's own execution —
        the run loop and the sliced waits it drives — never by an
        independent timer or Core 1, so a subsystem that keeps running can
        never mask a dead Core 0. A feed() failure is a real failure and
        escapes to the top-level recovery boundary (no catch here)."""
        if self._wdt is not None:
            self._wdt.feed()

    def _enable_watchdog(self):
        """Arm the hardware watchdog once the startup contract has passed:
        from here on, a Core 0 that stops making progress resets the board
        within WDT_TIMEOUT_MS instead of idling until a power cycle.

        Arming is the one intentional capability probe (the Wi-Fi PM_NONE
        precedent): a build without machine.WDT degrades to no hardware
        supervision plus a warning instead of a deterministic reset loop —
        making absence fatal would reboot into the same missing attribute
        forever."""
        try:
            self._wdt = machine.WDT(timeout=WDT_TIMEOUT_MS)
        except MemoryError:
            raise
        except Exception as err:
            self._wdt = None
            print("[WARNING] Hardware watchdog unavailable; Core 0 runs without hardware supervision: {}".format(err))

    def _service_wait(self):
        """Core 0 servicing hook for each 100 ms slice of long network waits:
        keeps the Core 1 heartbeat check firing and the hardware watchdog
        fed through backoffs and observation windows."""
        self._watch_core_1_heartbeat()
        self._feed_watchdog()

    def _sleep_and_service(self, delay_sec):
        if delay_sec <= 0:
            return

        for _ in range(int(delay_sec * 10)):
            self._service_wait()
            time.sleep_ms(100)

    def start(self):
        """Establish Core 0 network services before Core 1 starts. Connect
        steps are unbounded; verification (probes, drain, UTC) is
        self-healing -- a failed pass re-establishes and retries. Returns only
        on a clean pass; a MemoryError or programming failure propagates to
        the recovery boundary in main()."""
        self._led_manager.set_connecting(True)

        # Connect (Wi-Fi, then MQTT + subscriptions), shared with the
        # run-loop recovery path so backoff, logging, and LED behavior stay
        # in one place.
        self.establish_network()

        # Verify the QoS 1 path (two probes) and acquire UTC; self-healing
        # like the connect loops (the failed session is dropped and
        # re-established). Core 1 stays gated until start() returns; only
        # transport failures are retried.
        while True:
            if self._verify_startup_contract():
                break
            delay_sec = self._config["mqtt_reconnect_delays_sec"][-1]
            print("[WARNING] Startup verification failed; re-establishing network and retrying in {} sec".format(delay_sec))
            self._sleep_and_service(delay_sec)
            self._mqtt.mark_disconnected()
            self.establish_network()

        self._publish_utc_snapshot()

        self._network_stack_ready = True

        self._publish_network_snapshot(force=True)

        self._led_manager.set_connecting(False)

        # Arm hardware supervision only now: the connect/verification loops
        # above are deliberately unbounded (self-healing retries), and a
        # watchdog would reset them into the same waits. From here on every
        # long wait is sliced and fed, and the bounded waits fail under the
        # watchdog budget.
        self._enable_watchdog()

        print("[INFO] Core 0 startup complete - network stack verified and ready")

    def _verify_startup_contract(self):
        """Run one full pass of the startup verification (probe, drain,
        stabilization, probe, UTC); True only when every step succeeds. A
        MemoryError or programming failure propagates to the recovery
        boundary in main()."""
        if not self._perform_network_probe():
            print("[WARNING] Startup verification: network probe #1 failed")
            return False

        # Non-fatal: a drain timeout does not fail the pass.
        if not self._drain_startup_mqtt_work():
            print("[WARNING] Startup MQTT work drain did not complete")

        time.sleep_ms(5000)

        if not self._perform_network_probe():
            print("[WARNING] Startup verification: network probe #2 failed")
            return False

        # UTC is mandatory before Core 1 starts.
        if not self._synchronize_utc_required():
            print("[WARNING] Startup verification: UTC synchronization failed")
            return False

        return True

    def _publish_utc_snapshot(self):
        if self._utc_snapshot is None:
            return
        self._intercore.state_mailboxes.set_utc_snapshot(self._utc_snapshot)

    def run(self):
        while True:
            # First each pass: a dead Core 1 wedges the whole sensor and
            # cannot report itself, so Core 0 resets the board before doing
            # any other work. Feeding the hardware watchdog here proves this
            # pass of the loop executed; the longest un-fed stretch after
            # this point is one bounded MQTT wait (under WDT_TIMEOUT_MS).
            self._watch_core_1_heartbeat()
            self._feed_watchdog()

            # Resolve a pending HOT apply now that the watchdog has run: if
            # Core 1 is dead it has already reset, so no unbounded wait can
            # accumulate here.
            self._resolve_pending_config_update()

            if self._reboot_publish_due():
                self._perform_reboot()

            self._recover_network_if_needed()

            if (
                self._mqtt.is_connected()
                and self._pending_connection_logs
                and self._mqtt_publish_ready()
            ):
                try:
                    self._service_pending_connection_log()
                except MemoryError:
                    raise
                except (OSError, MQTTException) as err:
                    # Transport failure only (recovered on the next pass); a
                    # programming failure escapes run() to the top-level
                    # recovery boundary.
                    if DEBUG:
                        print("[DEBUG] Connection log publish failed: {}".format(err))

            # Read per pass (not captured once): a HOT_RELOADED
            # mqtt_command_poll_ms applies from the next pass.
            now_ms = time.ticks_ms()
            if time.ticks_diff(now_ms, self._last_command_poll_ms) >= self._config["mqtt_command_poll_ms"]:
                try:
                    self._mqtt.check_msg()
                except MemoryError:
                    raise
                except (OSError, MQTTException) as err:
                    # Transport failure only (recovery re-establishes the
                    # down session); a bug in the message callback escapes
                    # run() to the top-level recovery boundary.
                    if DEBUG:
                        print("[DEBUG] MQTT check failed: {}".format(err))
                self._last_command_poll_ms = now_ms

            if self._reboot_publish_due():
                self._perform_reboot()

            if (
                self._mqtt.is_connected()
                and self._pending_core0_responses
                and not self._intercore.outbound_queue.has_in_flight()
                and self._mqtt_publish_ready()
            ):
                try:
                    self._service_pending_core0_response()
                except MemoryError:
                    raise
                except (OSError, MQTTException) as err:
                    # Transport failure only (the response stays pending for
                    # the recovered link); a programming failure escapes run()
                    # to the top-level recovery boundary.
                    if DEBUG:
                        print("[DEBUG] Core 0 response publish failed: {}".format(err))

            if self._mqtt.is_connected():
                # Dequeue only when the pacing gate is open: take() promotes
                # the entry to in-flight, and there is no benefit for a
                # message Core 0 cannot yet send. The PINGREQ below is
                # protocol-control traffic and never pacing-gated.
                entry = (
                    self._intercore.outbound_queue.take()
                    if self._mqtt_publish_ready()
                    else None
                )
                if entry is not None:
                    try:
                        self._publish_entry(entry)
                    except MemoryError:
                        raise
                    except OutboundMessageTooLargeError as err:
                        # The spliced wire length exceeds the ceiling:
                        # permanent for this entry (its bytes are fixed), so
                        # discard it -- and answer a command response with
                        # the bounded substitute, so the channel never stalls
                        # behind it.
                        self._intercore.outbound_queue.complete_in_flight(
                            entry, discarded=True
                        )
                        print("[WARNING] Outbound entry dropped, envelope splice exceeded the per-message ceiling: {}".format(err))
                        if entry["kind"] == KIND_COMMAND_RESPONSE:
                            self._answer_discarded_command_response(entry)
                    except (OSError, MQTTException) as err:
                        # Transport failure only: an ambiguous QoS 1 failure
                        # keeps the entry in flight for the next take() --
                        # QoS 1 must not drop a message the broker has not
                        # PUBACKed. A programming failure escapes run().
                        if DEBUG:
                            print("[DEBUG] MQTT publish failed; entry remains in flight: {}".format(err))
                    else:
                        self._intercore.outbound_queue.complete_in_flight(entry)
                elif self._mqtt.ping_due():
                    # No publish to send: PINGREQ keeps the broker from
                    # disconnecting us at 1.5 x keepalive.
                    try:
                        self._mqtt.ping()
                    except MemoryError:
                        raise
                    except (OSError, MQTTException) as err:
                        # Transport failure only (recovery re-establishes the
                        # down session); a programming failure escapes run()
                        # to the top-level recovery boundary.
                        if DEBUG:
                            print("[DEBUG] MQTT PINGREQ failed: {}".format(err))

            self._publish_network_snapshot()

            self._utc_request_expired()
            if (
                self._mqtt.is_connected()
                and self._utc_should_send_request()
                and self._mqtt_publish_ready()
            ):
                self._utc_send_request()

            time.sleep_ms(10)
