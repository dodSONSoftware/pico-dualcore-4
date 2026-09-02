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
# as-is (the pending queue is FIFO, so holding it would stall every
# response behind it -- the same policy Core 1 applies at admission).
_SUBSTITUTE_ERROR_MESSAGES = {
    "response_too_large": "Command response exceeded the per-message size limit",
    "response_invalid": "Command response could not be serialized for transmission",
}
_UTC_STARTUP_MAX_ATTEMPTS = 3
_UTC_RETRY_INTERVAL_MS = 30000
_UTC_PROMPT_RETRY_DELAY_MS = 500

# Command-ID debounce cache: the number of recent command IDs Core 0
# retains. The cache is a short-lived debounce mechanism, not durable
# idempotency or exactly-once execution: it stops repeated copies of the same
# message (broker redelivery, sender retry) from generating repeated
# validation responses and repeated executions. A repeated command_id is
# silently ignored (no execution, no event, no response) until it evicts.
# Fixed count, FIFO eviction, RAM-only (cleared on reboot): a tiny amount of
# bounded control metadata, deliberately not heap-governed. The first bounded
# use of an ID claims it; a duplicate receipt does not refresh its position
# (the cache holds the last claimed distinct IDs, not an LRU access order).
# Static constant; not a config key.
_RECENT_COMMAND_ID_CAPACITY = 16

# Core 0 watchdog timeout for the Core 1 liveness heartbeat. Core 1 refreshes
# core_1_activity_ms on a 5-second deadline (core1.py), so a stamp already
# this old means the Core 1 thread is dead or wedged: no legitimate loop pass
# can miss six consecutive refreshes. Deliberately conservative -- far above
# any live-loop processing gap, yet below the 60s core_1_inactive diagnostic
# threshold, so a dead Core 1 resets the board instead of the health stream
# quietly stopping. Static constant; not a config key.
_CORE_1_HEARTBEAT_STALE_TIMEOUT_MS = 30000

# HOT_RELOADED settings Core 0 applies to its own live configuration when a
# write-config transaction is about to commit. The two Core 1-owned HOT
# settings (read_loop_sec / health_interval_sec) are not in Core 0's split
# config; they cross to Core 1 as an internal config-update event.
_HOT_APPLY_CORE0_KEYS = (
    "datetime_sync_interval_min",
    "mqtt_command_poll_ms",
    "mqtt_outbound_publish_delay_ms",
    "network_snapshot_interval_sec",
)
_HOT_APPLY_CORE1_KEYS = ("read_loop_sec", "health_interval_sec")

# Reserved key in the hot-apply rollback record: when mqtt_command_poll_ms
# changes, the last-poll stamp is re-anchored at apply time so the new cadence
# starts cleanly; the old stamp is carried under this key for rollback.
_POLL_STAMP_KEY = "_last_command_poll_ms"


class Core0:

    def __init__(self, intercore, config, wifi_config, runtime_id, boot_ticks_ms, led_manager, config_manager):
        self._intercore = intercore
        self._config = config
        # Core 0 owns the configuration manager (persistence, ACTIVE-vs-
        # PERSISTED state, change classification); it is never shared with
        # Core 1 -- Core 1 only ever sees the internal config-update event.
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
        # Recent command IDs (debounce cache; see _RECENT_COMMAND_ID_CAPACITY).
        # Oldest first, evicted FIFO.
        self._recent_command_ids = []
        self._pending_core0_responses = []
        self._pending_connection_logs = []
        # A HOT_RELOADED write-config is one logical transaction across both
        # cores: the Core 0 subset is applied, the Core 1 subset is requested
        # on the config-update lane, and the response + file commit are held
        # until Core 1's acknowledgement arrives (resolved in the run loop).
        # None when no such transaction is pending. _transaction_active (the
        # config manager) is True for the same window, enforcing single-flight.
        self._pending_config_update = None
        # Monotonic generation for config-update requests/results; each
        # transaction uses the next value so a stale result can never be read
        # as the current one.
        self._config_update_generation = 0
        self._utc_request_counter = 0
        self._pending_utc_request_id = None
        self._utc_request_deadline_ms = None
        self._utc_last_attempt_ms = None
        self._utc_snapshot = None
        self._last_network_snapshot_ms = None
        # Completion time of the most recent successful outbound application
        # PUBLISH (the QoS 1 exchange finished, PUBACK received); None until
        # the first publish. Drives the mqtt_outbound_publish_delay_ms pacing
        # gate: one timestamp, one source of truth for every publish path.
        self._last_mqtt_publish_completed_ms = None
        self._last_command_poll_ms = time.ticks_ms()
        self._next_sequence = 0
        self._network_stack_ready = False

    def _uptime_ms(self):
        return current_uptime_ms(self._uptime_state)

    def _current_utc_timestamp(self):
        snapshot = self._utc_snapshot
        if snapshot is None:
            return None
        elapsed_ms = time.ticks_diff(time.ticks_ms(), snapshot["ticks_ms"])
        return format_utc_epoch_ms(snapshot["utc_epoch_ms"] + elapsed_ms)

    def _target_matches(self, target):
        # A target is a non-empty string within the protocol bound before any
        # matching; everything below only decides whether it addresses this
        # device.
        if not is_bounded_target(target):
            return False
        if target == BROADCAST_TARGET:
            return True
        # Source and IP matching are case-insensitive: only the comparison is
        # normalized; the stored source keeps its configured casing, and
        # emitted messages do too. (IPv4 strings have no case distinction;
        # the comparison stays uniform anyway.)
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

        # For connection logs, we use the pre-serialized message approach
        from message_serializer import serialize_and_validate_message
        message = self._pending_connection_logs[0]
        # The message is final at this point: uptime and timestamp are the
        # sender's to carry, and the envelope is spliced in at publish time.
        message["uptime_ms"] = self._uptime_ms()
        message["timestamp"] = self._current_utc_timestamp()
        try:
            payload_bytes = serialize_and_validate_message(message)
            # KIND_LOG: _publish_entry resolves the log topic via _topic_for_kind
            entry = {
                "payload_bytes": payload_bytes,
                "kind": KIND_LOG,
            }
            self._publish_entry(entry)
            self._pending_connection_logs.pop(0)
        except MemoryError:
            raise
        except OutboundMessageTooLargeError as err:
            # The log body passed the admission ceiling but the spliced
            # envelope pushed it over the wire limit: permanent, and a log has
            # no command to answer, so it is dropped (the entries behind it
            # keep moving) instead of retried.
            print("[WARNING] Connection log dropped, envelope splice exceeded the per-message ceiling: {}".format(err))
            self._pending_connection_logs.pop(0)
        except (OSError, MQTTException) as err:
            # A transport/protocol failure is a link condition, not a verdict
            # on the log: it stays pending (the queue is bounded, so this
            # cannot retry forever) and the next service pass after the link
            # recovers delivers it -- a connection event is most diagnostic
            # exactly during the instability that dropped it. A programming
            # failure is NOT caught here: it escapes to the top-level recovery
            # boundary instead of being hidden as a "log publish failure".
            print("[DEBUG] Connection log publish failed, retrying: {}".format(err))

    def _on_mqtt_message(self, topic, payload):
        try:
            if isinstance(topic, bytes):
                topic = topic.decode()
            if isinstance(payload, bytes):
                payload = payload.decode()
            doc = json.loads(payload)
        except MemoryError:
            raise
        except Exception as err:
            if DEBUG:
                print("[DEBUG] Ignoring invalid MQTT payload: {}".format(err))
            return

        if not isinstance(doc, dict):
            if DEBUG:
                print("[DEBUG] Ignoring MQTT payload that is not an object")
            return

        # Global inbound message-schema gate: the firmware never interprets a
        # wire protocol version it does not support. A missing, wrong-typed,
        # older, or newer version is ignored for the whole message -- no
        # response, no debounce-cache entry, no UTC state change, no
        # message-type or topic-specific processing -- including inbound
        # info_response.
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

        # Staged command validation: target and command_id drops are silent
        # (no response is possible without a bounded command_id, and another
        # device's traffic must not consume a cache entry); once the debounce
        # cache has claimed the ID, every failure is answered.
        target = doc.get("target")
        if not self._target_matches(target):
            return

        command_id = doc.get("command_id")
        if not is_bounded_command_id(command_id):
            # A standard response requires a bounded command_id, so a
            # missing / non-string / empty / over-long one is dropped without
            # a response and is never cached.
            return

        if self._is_recent_command_id(command_id):
            # Debounce: this command_id was already claimed in this runtime.
            # Ignore the copy -- no execution, no Core 1 event, no response,
            # no change to a pending reboot. Expected transport behavior
            # (broker redelivery, sender retry), so DEBUG-only: not a
            # production warning.
            if DEBUG:
                print("[DEBUG] Duplicate command_id ignored: {}".format(command_id))
            return

        # Admission is conditioned on being able to acknowledge: a command
        # must be able to reserve a response slot before it is claimed and
        # executed. Otherwise it could execute -- write-config promotes
        # config.json, reboot arms _pending_reboot -- and then lose its
        # acknowledgement to a full response queue, after which the sender's
        # retry hits the debounce cache above and is silently dropped: the
        # command ran, the answer never arrived, and no retry can recover it.
        # Refusing here (before the ID is claimed) leaves the side effects
        # unapplied and the ID unclaimed, so an application-level retry
        # redelivers the command once capacity frees up.
        #
        # The single-flight pending HOT_RELOADED update holds one slot too:
        # its response is queued later (at Core 1's acknowledgement, in
        # _resolve_pending_config_update), so it must not be overrun by a
        # new admission -- or that deferred acknowledgement is the one lost.
        reserved = 1 if self._pending_config_update is not None else 0
        if (
            len(self._pending_core0_responses) + reserved
            >= _MAX_PENDING_CORE0_RESPONSES
        ):
            return

        # The first bounded use of the ID claims it -- before deeper
        # validation, so a malformed duplicate cannot generate a second
        # validation response.
        self._remember_command_id(command_id)
        targeted = target != BROADCAST_TARGET

        # Command envelope: every top-level key must be a known v3 envelope
        # field. All unknown fields at this scope are named, sorted, in one
        # error.
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

        # Required envelope fields: command is a non-empty string...
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

        # ...and within the length bound. An over-long name is answered with
        # a bounded error and is never echoed into the response (not even
        # partially): echoing it back would itself build the oversized
        # response the bound exists to prevent.
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

        # Ownership dispatch: only a supported command name proceeds. A
        # bounded name outside the registry is answered here -- never routed
        # blindly to Core 1, which is not the generic fallback -- and the
        # actual name is preserved in the standard command field (not
        # duplicated inside error).
        if not is_supported_command(command):
            self._queue_core0_response(self._command_error(
                command_id, targeted, command, {
                    "code": "unsupported_command",
                    "message": "Unsupported command",
                },
            ))
            return

        # Broadcast policy: write-config must never apply fleet-wide -- no
        # configuration validation, no filesystem operation, no response.
        # The ID remains claimed by the debounce cache above: the cache is
        # intentionally a message-debounce mechanism.
        if command == COMMAND_WRITE_CONFIG and target == BROADCAST_TARGET:
            return

        if command == COMMAND_REBOOT:
            # Core 0-owned: the shared protocol validation above is done;
            # this is the command's own contract.
            self._handle_reboot_command(command_id, targeted, payload_obj)
            return

        if command == COMMAND_GET_DETAILS:
            # Core 1-owned: dispatch the validated bounded event.
            self._handle_get_details_command(command_id, targeted, payload_obj)
            return

        if command == COMMAND_READ_CONFIG:
            self._handle_read_config_command(command_id, targeted, payload_obj)
            return

        if command == COMMAND_WRITE_CONFIG:
            self._handle_write_config_command(command_id, targeted, payload_obj)
            return

    def _command_error(self, command_id, targeted, command, error):
        """A bounded Core 0 command error response.

        The identifying command field is carried only when it is a bounded,
        valid string: an over-long or non-string name is never echoed into
        the response, so the response itself can never be oversized."""
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
        """True if the response is already one of the bounded substitutes (a failed response carrying a substitute error code)."""
        error = response.get("error")
        return (
            response.get("success") is False
            and isinstance(error, dict)
            and error.get("code") in _SUBSTITUTE_ERROR_MESSAGES
        )

    def _handle_reboot_command(self, command_id, targeted, payload):
        """Dedicated reboot validator/handler, called after the common protocol validation.

        The payload contract is exactly {}: any key is unknown for this command, so a non-empty object is answered with an ``unknown_fields`` error carrying the keys sorted (the missing / non-object cases are the common contract and are rejected before this handler). Debounce already ran, so a duplicate command_id never reaches the pending check."""
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
        """A non-empty payload is an unknown-fields error for a command that requires {}.

        Both Core 0-owned (reboot) and Core 1-owned (get-details) commands that
        require an empty payload share this contract: any key is unknown, and
        every offending key is named in a sorted ``unknown_fields`` array so
        the sender knows exactly which fields to remove."""
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
        """Dedicated get-details validator/dispatcher, called after the common protocol validation.

        Core 1 owns the execution because the authoritative SystemInformation
        instance and device state live there, so Core 0 only dispatches a
        validated bounded event. The payload contract is exactly {} (shared with
        reboot): any key is unknown and is answered with an ``unknown_fields``
        error naming the keys sorted. A {} dispatches the event with the
        validated empty payload; if event admission fails the only cause is a
        free-heap reserve that could not be restored, so the error names that
        cause rather than a "full" queue."""
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
        """read-config: answer with the committed (PERSISTED) configuration and the derived reboot state.

        The payload contract is exactly {} (shared with reboot / get-details). Wi-Fi secrets live in a separate file and never cross this path. A missing or invalid committed file is answered with the actual cause (normally unreachable: boot recovery guarantees a valid config.json)."""
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
        """write-config: the payload is exactly {"config": <complete candidate configuration>}, validated by the same validate_config() path startup uses.

        The payload contract is one key, "config", whose value must be the
        complete candidate configuration object -- there is no patch, merge,
        or partial-update shape in this command. A changed candidate is
        atomically promoted to config.json. UNCHANGED writes nothing;
        REBOOT_REQUIRED commits (the active snapshot holds the running values
        until the next reboot); HOT_RELOADED is additionally applied on both
        cores, and the transaction is committed once that application is
        acknowledged -- rolled back, with the file state restored, when it
        cannot be. MemoryError propagates to the fail-fast boundary."""
        # Unknown payload keys are all named together (sorted), whatever the
        # rest of the payload is.
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
            # A hot transaction is pending (Core 1's acknowledgement not yet
            # resolved): single-flight, so a second write is refused with a
            # bounded, non-executing answer (the command ID stays governed by
            # the debounce cache).
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
            # Structured fields of the error (e.g. expected/received schema
            # version) cross into the response as-is.
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

        # HOT_RELOADED: Core 0 and Core 1 form one logical transaction. Apply
        # the Core 0 subset now, then ask Core 1 for its subset on the
        # config-update lane. No success response is sent until Core 1's
        # acknowledgement arrives (resolved in the run loop), and a failed
        # apply rolls Core 0 and the file back so the committed configuration
        # and the running firmware agree again.
        old_values, core1_update = self._apply_hot_changes(result["changes"])
        if not core1_update:
            # Only Core 0-owned hot settings changed: Core 0 already applied
            # them and nothing is owed to Core 1, so commit now.
            self._config_manager.commit_hot_reload()
            self._queue_core0_response(
                self._write_config_success(command_id, targeted, result)
            )
            return

        generation = self._next_config_update_generation()
        request = {"generation": generation}
        request.update(core1_update)
        self._intercore.config_update_lane.post_request(request)
        # Hold the response and the rollback state until Core 1's ack is on
        # the lane (resolved by _resolve_pending_config_update in the run
        # loop). While this is pending _transaction_active stays True, so a
        # second write-config is refused with config_update_in_progress.
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
                # This command's effect on the persisted desired config,
                # independent of the reboot state it left behind.
                "configuration_changed": classification != CLASSIFICATION_UNCHANGED,
                "classification": classification,
                # Derived at response time: True for REBOOT_REQUIRED, False
                # for UNCHANGED without a pending reboot, and False once a
                # hot application has committed (cancelling any pending one).
                "reboot_required": self._config_manager.reboot_required,
                "changes": result["changes"],
            },
        }

    def _apply_hot_changes(self, changes):
        """Apply the changed Core 0-owned HOT settings to the live config.

        Returns the old values (for rollback -- including the last-poll stamp
        under _POLL_STAMP_KEY when the poll interval changed) and the Core 1
        update (the Core 1-owned HOT settings that changed; {} when none)."""
        old_values = {}
        core1_update = {}
        for change in changes:
            setting = change["setting"]
            if setting in _HOT_APPLY_CORE0_KEYS:
                old_values[setting] = self._config[setting]
                self._config[setting] = change["new_value"]
                if setting == "mqtt_command_poll_ms":
                    # The new poll cadence starts cleanly from the reload
                    # instant: re-anchor the last-poll stamp so it neither
                    # bursts a stale interval nor waits one out.
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
        """Resolve a pending HOT_RELOADED apply once Core 1's acknowledgement is on the lane.

        Success commits the manager (releasing the retained config.json.old)
        and releases the held success response; a failed apply rolls Core 0 and
        the file back and answers a bounded error. A dead Core 1 never acks,
        but that is bounded by the existing liveness watchdog (which resets the
        board and boot recovery restores the retained config.json.old) -- so no
        unbounded wait is owed and no success response is sent before the
        runtime update is complete."""
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
        # Claim (or, on a retry, reuse) the wire sequence on the persistent
        # response so a re-publish after an ambiguous QoS 1 failure keeps the
        # same (runtime_id, sequence) identity instead of shifting it.
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
                # No recorded cause, or the response is already the bounded
                # substitute: hold it for a later pass, the same way a failed
                # publish (which raises) leaves it pending.
                return
            # A permanent serialization failure can never succeed by
            # retrying the same bytes, and the queue is FIFO -- so instead of
            # blocking every response behind it (the same policy Core 1
            # applies at admission), the command is answered with a small
            # bounded error substitute whose code states the cause. The
            # substitute publishes on a later pass.
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
        # A substitute that answered a permanently unsendable reboot
        # acknowledgement has now been published: the command was reported
        # failed, so release the held reboot. The device keeps running, and a
        # later reboot command is admissible instead of hitting
        # reboot_already_pending forever.
        if response.get("_clears_pending_reboot"):
            self._pending_reboot = None

    def _handle_info_response(self, doc):
        # The global inbound message-schema gate in _on_mqtt_message has
        # already run: only a supported message_schema_version reaches here.
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

        # A malformed answer to OUR request is still an answer: clear the
        # pending request so we retry after a short backoff instead of
        # waiting out the full deadline or the 30s retry interval.
        # (Responses that are not ours are rejected earlier above, without
        # touching pending state.)
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

        now_ticks = time.ticks_ms()
        snapshot = {
            "timestamp": normalized_timestamp,
            "utc_epoch_ms": utc_epoch_ms,
            "ticks_ms": now_ticks,
            "runtime_start_epoch_ms": utc_epoch_ms - self._uptime_ms(),
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
        """Serialize the five Core-0-owned envelope members as a braceless fragment.

        Senders must not carry any of these keys at the top level of their message."""
        fragment = json.dumps({
            "sequence": sequence,
            "runtime_id": self._runtime_id,
            "source": self._config["source"],
            "firmware_version": FIRMWARE_VERSION,
            "message_schema_version": MESSAGE_SCHEMA_VERSION,
        })
        return fragment[1:-1].encode("utf-8")

    def _claim_wire_sequence(self, container):
        """Claim the next wire sequence and stamp it on ``container``; never rolled back.

        A retry of the same logical message reuses the stamped number; a different message always gets a fresh one, so a (runtime_id, sequence) pair never collides."""
        sequence = container.get("_wire_sequence")
        if sequence is None:
            sequence = self._next_sequence
            self._next_sequence += 1
            container["_wire_sequence"] = sequence
        return sequence

    # --- Outbound publish pacing (mqtt_outbound_publish_delay_ms) --------
    #
    # A minimum quiet period between consecutive outbound application
    # PUBLISHes, measured from the moment the previous QoS 1 publish COMPLETED
    # (PUBACK received), not from when it started: broker latency is outside
    # the configured interval. It exists to drain a backlogged outbound queue
    # progressively after a reconnect instead of as a broker-speed burst.
    #
    # The interval is state, not a sleep: while the gate is closed Core 0
    # keeps running its normal loop (Core 1 watchdog, command polling,
    # keepalive, recovery) and simply does not begin another application
    # PUBLISH. Protocol-control traffic (PINGREQ and friends) is never paced.

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
        """Publish one MQTT entry from its pre-serialized bytes, envelope spliced in before the closing brace.

        The payload is never decoded or re-serialized here; the wire sequence is claimed once, before the first transmission attempt. The spliced wire length must stay within MAX_OUTBOUND_MESSAGE_BYTES: a body the envelope pushes over the ceiling raises OutboundMessageTooLargeError instead of publishing (permanent for the entry, never retried)."""
        payload = entry["payload_bytes"]
        if not isinstance(payload, (bytes, bytearray)) or bytes(payload[-1:]) != b"}":
            raise ValueError("queued payload must be a serialized JSON object")
        # The queue contract allows bytes or bytearray; normalize once so the
        # assembled frame is plain bytes (MicroPython's bytes.join is strict
        # about item types).
        body = payload if isinstance(payload, bytes) else bytes(payload)
        # Claim the wire sequence now that the frame is about to be transmitted,
        # and stamp it on the entry. A later retry of this same entry (an
        # ambiguous QoS 1 failure left it in flight) reuses the stamped number,
        # while a different message is never handed it.
        sequence = self._claim_wire_sequence(entry)
        fragment = self._envelope_fragment(sequence)
        # The body was admitted at or under MAX_OUTBOUND_MESSAGE_BYTES, but the
        # spliced envelope is added on top of it (the comma and fragment
        # replace the body's closing brace): enforce the ceiling against the
        # FINAL wire length, before the joined frame is allocated. Permanent
        # for this entry (its bytes are fixed), so raise the size error the
        # admission paths raise; the callers answer command responses with the
        # bounded substitute and discard the other kinds instead of retrying.
        wire_length = len(body) + len(fragment) + 1
        if wire_length > MAX_OUTBOUND_MESSAGE_BYTES:
            raise OutboundMessageTooLargeError(
                "Outbound message too large after envelope splice: {} > {}".format(
                    wire_length, MAX_OUTBOUND_MESSAGE_BYTES
                )
            )
        encoded = b"".join((body[:-1], b",", fragment, b"}"))
        topic = entry.get("topic")
        if topic is None:
            topic = self._topic_for_kind(entry["kind"])
        self._mqtt.publish_qos1(topic, encoded)
        # The PUBACK has been received: the publish is complete, so the
        # pacing interval (if any) now begins. A failed publish raises before
        # this line and records nothing.
        self._note_mqtt_publish_completed()
        if entry.get("kind") == KIND_TELEMETRY:
            self._led_manager.telemetry_sent()
        if DEBUG:
            print("[DEBUG] QoS 1 published: seq={}".format(sequence))

    def _answer_discarded_command_response(self, entry):
        """Queue the bounded substitute for a discarded oversized command response.

        The command was accepted and its acknowledgement is owed, so the channel moves on with a small error response for the same command (code "response_too_large"), serviced by the normal Core 0 response path. The body carries the command's identifying fields; only a bounded subset is read back (an over-long name or ID is never echoed). A body that cannot be read back has no identity to answer with: the discard stands."""
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
        """Build and publish a Core 0 command response (pre-serialized).

        ``wire_sequence``/``container`` let a retry keep the claimed identity and the same bytes. Returns True on publish, False on a permanent serialization failure or a splice-time size failure (its cause is recorded on the container, when there is one, so the servicing path can answer with the matching bounded substitute); MemoryError propagates."""
        payload_bytes = container.get("_payload_bytes") if container is not None else None
        if payload_bytes is None:
            payload = {
                "command_id": command_id,
            }
            # The command field is the standard location for the command
            # name and is carried whenever it is available (bounded and
            # valid); an error whose name failed the bound omits it rather
            # than echoing it.
            if command is not None:
                payload["command"] = command
            payload["targeted"] = targeted
            payload["success"] = success
            if success:
                payload["data"] = data
            else:
                payload["error"] = error

            # Build the logical message first. Uptime and timestamp are the
            # sender's to carry (the envelope is spliced in at publish time),
            # so they are captured at construction time, not at publish time.
            message = {
                "message_type": "command_response",
                "uptime_ms": self._uptime_ms(),
                "timestamp": self._current_utc_timestamp(),
                "payload": payload,
            }

            # Serialize and encode the message for the pre-serialized queue
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
                # Freeze the bytes on the persistent container so a retry after
                # an ambiguous QoS 1 failure re-publishes the same document
                # (same sequence, same content) instead of rebuilding it with a
                # newer uptime/timestamp.
                container["_payload_bytes"] = payload_bytes

        entry = {
            "topic": self._config["mqtt_topic_command_response"],
            "kind": KIND_COMMAND_RESPONSE,
            "payload_bytes": payload_bytes,
        }
        # Carry the caller's claimed sequence into the entry so the retry keeps
        # it; a first attempt (None) leaves the entry unstamped and
        # _publish_entry claims a fresh number.
        if wire_sequence is not None:
            entry["_wire_sequence"] = wire_sequence
        try:
            self._publish_entry(entry)
        except OutboundMessageTooLargeError as err:
            # The body passed the admission ceiling but the spliced envelope
            # pushed the final wire length over it: permanent for these bytes,
            # the same cause class as an oversized serialization. Record the
            # cause so the servicing path answers with the matching bounded
            # substitute instead of retrying the same entry forever.
            if DEBUG:
                print("[DEBUG] Command response too large after envelope splice: {}".format(err))
            if container is not None:
                container["_permanent_failure"] = "response_too_large"
            return False
        return True

    def _perform_reboot(self):
        request = self._pending_reboot
        if request is None:
            return True
        if self._intercore.outbound_queue.has_in_flight():
            return False
        # The success acknowledgement was permanently unsendable and the
        # command was answered with the bounded substitute: the request is
        # terminal. Never re-attempt the unsendable bytes and never queue the
        # same substitute a second time; the held reboot is released by the
        # servicing path once the substitute is published.
        if request.get("_permanent_failure_answer_queued"):
            return False

        try:
            # Claim (or, on a retry, reuse) the wire sequence on the pending
            # reboot request so a re-publish keeps the same sequence identity.
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
        except Exception as err:
            if DEBUG:
                print("[DEBUG] Reboot response publish failed; reboot remains pending: {}".format(err))
            return False
        if not published:
            failure = request.get("_permanent_failure")
            if failure is not None:
                # A permanent failure (the recorded cause names it) can never
                # publish by retrying the same bytes: answer the command with
                # the bounded substitute instead of spinning. Hold the reboot
                # (a reset with no acknowledgement at all is worse) until the
                # substitute is published, then release it -- the command has
                # been reported failed and the device keeps running.
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
                # When the servicing path publishes it, release the held
                # reboot (see _service_pending_core0_response).
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
        time.sleep_ms(5000)
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
        except Exception as err:
            # Roll back the armed ID: no response can ever arrive for a
            # request that was not delivered.
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
            except Exception as err:
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
        return (
            time.ticks_diff(time.ticks_ms(), self._utc_snapshot["ticks_ms"])
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
                # A matched PUBACK is a completed outbound publish: it opens
                # the pacing gate for the following startup publishes (drain,
                # probe #2, UTC request) exactly like any other one.
                self._note_mqtt_publish_completed()
            return result
        except MemoryError:
            raise
        except Exception as err:
            if DEBUG:
                print("[DEBUG] Network probe failed: {}".format(err))
            return False

    def _drain_startup_mqtt_work(self):
        """Drain pending Core 0 MQTT work (connection logs, etc.); True when none remains, False on timeout.

        Each pending log is measured against its own grace window, starting when the previous log's cycle completed: one slow-but-legal QoS 1 cycle (its PUBACK wait can lawfully run to mqtt_broker_response_timeout_sec) must not consume the budget of the logs behind it. Startup publishes are paced like any other outbound traffic."""
        grace_ms = 2000  # per-log grace before that log may begin its publish

        while self._pending_connection_logs:
            # A fresh window per log, measured from here -- i.e. from the
            # previous log's completion, never from the drain's start.
            deadline_ms = time.ticks_add(time.ticks_ms(), grace_ms)
            self._wait_for_mqtt_publish_slot()
            if time.ticks_diff(time.ticks_ms(), deadline_ms) >= 0:
                print("[WARNING] Startup MQTT work drain timeout")
                return False
            try:
                self._service_pending_connection_log()
            except MemoryError:
                raise
            except Exception as err:
                if DEBUG:
                    print("[DEBUG] Startup work drain failed: {}".format(err))
                # Continue draining, don't fail the entire startup

        return True

    def _synchronize_utc_required(self):
        """Run one bounded pass of startup UTC synchronization.

        Returns True once a valid snapshot is acquired, False after _UTC_STARTUP_MAX_ATTEMPTS so the caller re-establishes and retries. MemoryError propagates."""
        for attempt in range(_UTC_STARTUP_MAX_ATTEMPTS):
            # The preceding startup publish (probe #2, or the drain) recorded
            # its completion: respect the same pacing interval before the
            # request's PUBLISH begins.
            self._wait_for_mqtt_publish_slot()
            self._utc_send_request()
            self._utc_wait_response()
            if self._utc_snapshot is not None:
                return True
            # Wait before retry
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
            print("[WARNING] MQTT connection sequence exhausted; retrying in {} sec".format(delay_sec))
            self._sleep_and_service(delay_sec)

    def _recover_network_if_needed(self):
        if self._wifi.is_connected() and self._mqtt.is_connected():
            return

        # Report the outage immediately so the snapshot (and Core 1 health
        # gating) reflects the loss; the flag is restored only after the
        # link is re-established.
        self._network_stack_ready = False
        if not self._wifi.is_connected():
            # Wi-Fi loss implies MQTT loss; drop the stale session state.
            self._mqtt.mark_disconnected()
        self._publish_network_snapshot(force=True)
        self.establish_network()
        self._network_stack_ready = True
        # establish_network() armed the connection LED; recovery is complete.
        self._led_manager.set_connecting(False)
        self._publish_network_snapshot(force=True)

    def _watch_core_1_heartbeat(self):
        """Watch Core 1's liveness heartbeat and reset the MCU when stale.

        No-op before Core 1's first stamp, so the unbounded startup connect loops are unaffected."""
        last_activity_ms = self._intercore.state_mailboxes.get_core_1_activity_ms()
        if last_activity_ms is None:
            return
        age_ms = time.ticks_diff(time.ticks_ms(), last_activity_ms)
        if age_ms >= _CORE_1_HEARTBEAT_STALE_TIMEOUT_MS:
            print("[FATAL] Core 1 heartbeat stale ({} ms) - resetting".format(age_ms))
            machine.reset()

    def _service_wait(self):
        """Core 0 servicing hook invoked at each 100 ms slice of long network waits.

        Keeps the Core 1 heartbeat check firing through connect/reconnect backoffs."""
        self._watch_core_1_heartbeat()

    def _sleep_and_service(self, delay_sec):
        if delay_sec <= 0:
            return

        for _ in range(int(delay_sec * 10)):
            self._service_wait()
            time.sleep_ms(100)

    def start(self):
        """Establish Core 0 network services before Core 1 starts (the deterministic startup contract).

        Connect steps are unbounded; the verification steps (probes, drain, UTC) are self-healing -- a failed pass re-establishes the network and retries. Returns only on a clean pass; Core 1 stays gated until then. A MemoryError propagates to the recovery boundary in main()."""
        self._led_manager.set_connecting(True)

        # Steps 1-2: Establish Wi-Fi, then MQTT + subscriptions.
        # Shared with the run-loop recovery path so connect loops, backoff,
        # logging, and LED behavior stay in one place.
        self.establish_network()

        # Steps 3-7: Verify the QoS 1 path (two probes) and acquire UTC.
        # Self-healing, like the connect loops above: on any verification
        # failure, drop the (possibly wedged) MQTT session, re-establish the
        # network, and retry the whole pass. Core 1 stays gated because
        # start() has not returned. A MemoryError propagates out of
        # _verify_startup_contract and out of this loop (fail-fast on OOM;
        # the recovery boundary in main() turns it into a board reset).
        while True:
            if self._verify_startup_contract():
                break
            delay_sec = self._config["mqtt_reconnect_delays_sec"][-1]
            print("[WARNING] Startup verification failed; re-establishing network and retrying in {} sec".format(delay_sec))
            self._sleep_and_service(delay_sec)
            self._mqtt.mark_disconnected()
            self.establish_network()

        # Step 8: Publish initial UTC snapshot
        self._publish_utc_snapshot()

        # Step 9: Network startup proven complete - set ready flag
        self._network_stack_ready = True

        # Step 10: Publish initial network snapshot with ready flag
        self._publish_network_snapshot(force=True)

        # Step 11: Stop connection LED
        self._led_manager.set_connecting(False)

        print("[INFO] Core 0 startup complete - network stack verified and ready")

    def _verify_startup_contract(self):
        """Run one full pass of the startup verification contract (probe #1, drain, stabilization, probe #2, UTC).

        Returns True only when every step succeeds, False on any failure so the caller re-establishes and retries. MemoryError propagates."""
        # Step 3: QoS 1 network probe #1
        if not self._perform_network_probe():
            print("[WARNING] Startup verification: network probe #1 failed")
            return False

        # Step 4: Drain startup MQTT work (non-fatal, matching prior behavior)
        if not self._drain_startup_mqtt_work():
            print("[WARNING] Startup MQTT work drain did not complete")

        # Step 5: Wait 5 seconds for stabilization
        time.sleep_ms(5000)

        # Step 6: QoS 1 network probe #2
        if not self._perform_network_probe():
            print("[WARNING] Startup verification: network probe #2 failed")
            return False

        # Step 7: Acquire UTC (mandatory before Core 1 starts)
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
            # First each pass: a dead Core 1 wedges the whole sensor (no
            # telemetry, no health) and cannot report itself, so Core 0
            # resets the board before doing any other work.
            self._watch_core_1_heartbeat()

            # Resolve a pending HOT_RELOADED apply now that the watchdog has
            # run: if Core 1 has acknowledged, commit or roll back; if Core 1
            # is dead the watchdog above has already reset, so no unbounded
            # wait can accumulate here.
            self._resolve_pending_config_update()

            # The reboot response is an outbound PUBLISH: hold (without
            # resetting, without blocking) until the pacing gate is open.
            if (
                self._pending_reboot is not None
                and self._mqtt_publish_ready()
            ):
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
                    # Transport failure only: a link condition, recovered on
                    # the next pass. A programming failure escapes run() to
                    # the top-level recovery boundary instead of being
                    # reclassified as an MQTT outage.
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
                    # Transport/protocol failure only: a stalled or corrupt
                    # stream is a link condition (the session is already
                    # marked down; recovery re-establishes it). A bug in the
                    # message callback is a programming failure: it escapes
                    # run() to the top-level recovery boundary instead of
                    # being hidden as an outage and the message redelivered
                    # into the same fault.
                    if DEBUG:
                        print("[DEBUG] MQTT check failed: {}".format(err))
                self._last_command_poll_ms = now_ms

            # The reboot response is an outbound PUBLISH: hold (without
            # resetting, without blocking) until the pacing gate is open.
            if (
                self._pending_reboot is not None
                and self._mqtt_publish_ready()
            ):
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
                    # Transport failure only: the response stays pending for
                    # the retry on the recovered link. A programming failure
                    # escapes run() to the top-level recovery boundary.
                    if DEBUG:
                        print("[DEBUG] Core 0 response publish failed: {}".format(err))

            if self._mqtt.is_connected():
                # Dequeue only when the pacing gate is open: take() promotes
                # the entry to in-flight, and there is no benefit in doing that
                # for a message Core 0 already knows it cannot transmit yet.
                # The PINGREQ below is NOT gated on pacing: keepalive is
                # protocol-control traffic and never waits on application
                # publishes (and vice versa).
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
                        # The spliced wire length exceeds the per-message
                        # ceiling: permanent for this entry (its bytes are
                        # fixed), so retrying it would only re-fail and stall
                        # the in-flight slot. Discard it -- and answer a
                        # command response with the bounded substitute, so the
                        # command channel never stalls behind it either.
                        self._intercore.outbound_queue.complete_in_flight(
                            entry, discarded=True
                        )
                        print("[WARNING] Outbound entry dropped, envelope splice exceeded the per-message ceiling: {}".format(err))
                        if entry["kind"] == KIND_COMMAND_RESPONSE:
                            self._answer_discarded_command_response(entry)
                    except (OSError, MQTTException) as err:
                        # Transport failure only: an ambiguous QoS 1 failure
                        # keeps the entry in flight so the next take() retries
                        # it -- QoS 1 must not drop a message the broker has
                        # not PUBACKed. A programming failure escapes run()
                        # to the top-level recovery boundary instead of
                        # looping on the same fault.
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
                        # Transport failure only (the session is already
                        # marked down; recovery re-establishes it). A
                        # programming failure escapes run() to the top-level
                        # recovery boundary.
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
