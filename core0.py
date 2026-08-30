# core0.py - Core 0 exclusive network owner
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import json
import machine
import os
import time

from config import (
    CHANGE_DYNAMIC,
    CHANGE_RECONFIGURE,
    CHANGE_RESTART_REQUIRED,
    CONFIG_CHANGE_POLICIES,
    CONFIG_FILE,
    CONFIG_STAGED_SUFFIX,
    CORE0_DYNAMIC_KEYS,
    CORE0_MQTT_RECONFIGURE_KEYS,
    CORE1_DYNAMIC_KEYS,
    CORE1_RECONFIGURE_KEYS,
    ConfigPatchError,
    commit_config_file,
    stage_config_candidate,
    validate_config,
    validate_config_patch,
)
from debug import DEBUG
from intercore import (
    KIND_COMMAND_RESPONSE,
    KIND_HEALTH,
    KIND_LOG,
    KIND_TELEMETRY,
    CONFIG_TX_ACTION_APPLY,
    CONFIG_TX_ACTION_COMMIT,
    CONFIG_TX_ACTION_ROLLBACK,
)
from message_protocol import format_utc_epoch_ms
from message_serializer import PUBLISH_GC_HEADROOM_BYTES
from mqtt import Mqtt
from network_diagnostics import icmp_echo_supported, probe_dns_server, probe_gateway
from observability import (
    EVENT_COMMAND_REJECTED,
    EVENT_CONFIGURATION_RECOVERED,
    EVENT_CONFIGURATION_ROLLBACK_COMPLETED,
    EVENT_CONFIGURATION_ROLLBACK_FAILED,
    EVENT_CONFIGURATION_UPDATE_COMPLETED,
    EVENT_CONFIGURATION_UPDATE_FAILED,
    EVENT_CONFIGURATION_UPDATE_STARTED,
    EVENT_MQTT_CONNECTION_ESTABLISHED,
    EVENT_MQTT_RECONNECT_COMPLETED,
    EVENT_RUNTIME_CORE1_STALLED,
    EVENT_RUNTIME_REBOOT_REQUESTED,
    EVENT_UTC_SYNC_COMPLETED,
    EVENT_UTC_SYNC_FAILED,
    EVENT_WIFI_CONNECTION_ESTABLISHED,
    EVENT_WIFI_RECONNECT_COMPLETED,
    LEVEL_ERROR,
    LEVEL_INFO,
    LEVEL_WARNING,
    REASON_COMMAND_DUPLICATE,
    REASON_COMMAND_EXECUTION_FAILED,
    REASON_COMMAND_INVALID_ENVELOPE,
    REASON_COMMAND_INVALID_PAYLOAD,
    REASON_CONFIG_PERSISTENCE_FAILED,
    REASON_CONFIG_PRIMARY_INVALID,
    REASON_CONFIG_RECONFIGURE_FAILED,
    REASON_CONFIG_ROLLBACK_FAILED,
    REASON_CONFIG_STAGE_FAILED,
    REASON_CONFIG_INVALID_COMBINATION,
    REASON_CONFIG_UPDATE_IN_PROGRESS,
    REASON_CORE1_HEARTBEAT_TIMEOUT,
    REASON_MQTT_RECONNECT_SUCCEEDED,
    REASON_NONE,
    REASON_TIMEOUT,
    REASON_WIFI_RECONNECT_SUCCEEDED,
    build_event_payload,
)
from uptime import create_uptime_state, current_uptime_ms
from version import FIRMWARE_BUILD_COMMIT, FIRMWARE_VERSION, MESSAGE_SCHEMA_VERSION
from wifi import Wifi

from message_serializer import serialize_and_validate_message


_MAX_PENDING_CORE0_RESPONSES = 4
_MAX_PENDING_CONNECTION_LOGS = 4
_UTC_STARTUP_MAX_ATTEMPTS = 3
_UTC_RETRY_INTERVAL_MS = 30000
_UTC_PROMPT_RETRY_DELAY_MS = 500

# Core 0 watchdog timeout for the Core 1 liveness heartbeat. Core 1 refreshes
# core_1_activity_ms on a 5-second deadline (core1.py), so a stamp already
# this old means the Core 1 thread is dead or wedged: no legitimate loop pass
# can miss six consecutive refreshes. Deliberately conservative -- far above
# any live-loop processing gap, yet below the 60s core_1_inactive diagnostic
# threshold, so a dead Core 1 resets the board instead of the health stream
# quietly stopping. Static constant; not a config key.
_CORE_1_HEARTBEAT_STALE_TIMEOUT_MS = 30000

# Bounded wait for a Core 1 answer to a configuration transaction mailbox
# request. A live Core 1 answers within a few loop passes (~40 ms each); the
# bound only matters if Core 1 is wedged or dead -- in which case the Core 1
# heartbeat watchdog resets the board on its own schedule.
_CONFIG_TX_TIMEOUT_MS = 10000

# Network diagnostics stages (a scalar state machine, not a list of tasks).
# A cycle advances gateway -> dns -> optional broker, at most ONE bounded
# probe stage per run-loop pass. Observational only: the results never add a
# degraded reason, never drive recovery, and never delay normal operations.
_NETDIAG_IDLE = 0
_NETDIAG_GATEWAY = 1
_NETDIAG_DNS = 2
_NETDIAG_BROKER = 3


class ConfigTransactionError(Exception):
    """A configuration transaction failed after activation began.

    Carries the stable response code; the coordinator performs the rollback
    of whatever was already activated before responding with this code.
    """

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class Core0:
    """Own the complete network stack and all MQTT operations."""

    def __init__(self, intercore, config, wifi_config, runtime_id, boot_ticks_ms, led_manager):
        self._intercore = intercore
        self._config = config
        self._runtime_id = runtime_id
        self._uptime_state = create_uptime_state(boot_ticks_ms)
        self._led_manager = led_manager

        self._wifi = Wifi(
            wifi_config["wifi_ssid"],
            wifi_config["wifi_password"],
            config["wifi_reconnect_delays_sec"],
        )
        self._mqtt = Mqtt(config, self._on_mqtt_message)

        self._pending_reboot = None
        self._pending_core0_responses = []
        self._pending_connection_logs = []
        self._utc_request_counter = 0
        self._pending_utc_request_id = None
        self._utc_request_deadline_ms = None
        self._utc_last_attempt_ms = None
        self._utc_snapshot = None
        self._last_network_snapshot_ms = None
        self._last_command_poll_ms = time.ticks_ms()
        self._next_sequence = 0
        self._network_stack_ready = False

        # Network diagnostics (Core 0 owns scheduling, probes, and the
        # results published into the shared network snapshot). All scalar
        # state: no lists, no history, no background work.
        self._netdiag_interval_ms = config["network_diagnostics_interval_sec"] * 1000
        self._netdiag_broker_enabled = config["network_diagnostics_broker_latency_enabled"]
        self._netdiag_stage = _NETDIAG_IDLE
        self._netdiag_next_due_ms = None
        self._netdiag_last_completed_ms = None
        self._netdiag_run_count = 0
        # False until a feature-detect proves the port can create a raw ICMP
        # socket (never converted from a null "not tested" result).
        self._gateway_reachability_supported = False
        self._gateway_reachable = None
        self._gateway_last_latency_ms = None
        self._dns_reachable = None
        self._dns_last_latency_ms = None
        self._mqtt_broker_last_round_trip_ms = None

        # Post-outage queue drain (Core 0 owns the episode and the optional
        # rate ceiling). Started only by run-loop recovery, after a runtime
        # reconnect completes -- never by start(). All scalar state: no
        # lists, no history, no background work.
        self._drain_rate_per_sec = config["mqtt_post_outage_drain_rate_per_sec"]
        self._queue_drain_active = False
        self._queue_drain_started_ms = None
        self._queue_drain_start_depth = 0
        self._queue_drain_message_count = 0
        self._queue_drain_next_publish_ms = None
        # Last COMPLETED episode (false/0/0/0/0 before the first one).
        self._last_queue_drain_start_depth = 0
        self._last_queue_drain_message_count = 0
        self._last_queue_drain_duration_ms = 0
        self._last_queue_drain_rate_per_sec = 0

        # Runtime configuration transactions (write_config coordinator, Core 0
        # exclusive). At most one in flight: None when idle. The staged file is
        # only created after patch validation and no-op detection; it is
        # committed, discarded, or restored (rolled back) -- never left
        # dangling while another transaction runs.
        self._config_tx_in_progress = False
        self._config_tx = None

    def _uptime_ms(self):
        return current_uptime_ms(self._uptime_state)

    def _current_utc_timestamp(self):
        snapshot = self._utc_snapshot
        if snapshot is None:
            return None
        elapsed_ms = time.ticks_diff(time.ticks_ms(), snapshot["ticks_ms"])
        return format_utc_epoch_ms(snapshot["utc_epoch_ms"] + elapsed_ms)

    def _target_matches(self, target):
        if target == "*" or target == self._config["source"]:
            return True
        return target == self._wifi.ip_address()

    def _queue_core0_response(self, response):
        if len(self._pending_core0_responses) >= _MAX_PENDING_CORE0_RESPONSES:
            print("[WARNING] Core 0 response queue full; response rejected")
            return False
        self._pending_core0_responses.append(response)
        return True

    def _queue_connection_log(self, level, event, reason_code, message, data=None):
        if len(self._pending_connection_logs) >= _MAX_PENDING_CONNECTION_LOGS:
            print("[WARNING] Core 0 connection log queue full; log rejected")
            return False
        # The message is final when queued: uptime and timestamp are the
        # sender's to carry (the envelope is spliced in at publish time), so
        # they are stamped here, not at service time.
        self._pending_connection_logs.append({
            "message_type": "log",
            "uptime_ms": self._uptime_ms(),
            "timestamp": self._current_utc_timestamp(),
            "payload": build_event_payload(level, event, reason_code, message, data),
        })
        return True

    def _service_pending_connection_log(self):
        if not self._pending_connection_logs:
            return

        # For connection logs, we use the pre-serialized message approach
        from message_serializer import serialize_and_validate_message
        message = self._pending_connection_logs[0]
        # The message is final (uptime/timestamp were stamped at queue time,
        # the envelope is spliced in at publish time) -- publish it as-is.
        try:
            payload_bytes = serialize_and_validate_message(message)
            # KIND_LOG: _publish_entry resolves the log topic via _topic_for_kind
            entry = {
                "payload_bytes": payload_bytes,
                "kind": KIND_LOG,
            }
            self._publish_entry(entry)
        except MemoryError:
            raise
        except Exception as err:
            # Log serialization failures but still remove the log from the queue
            # to prevent infinite retry loop
            print("[DEBUG] Connection log failed: {}".format(err))
        finally:
            # Always remove the log from the queue to prevent infinite retry
            self._pending_connection_logs.pop(0)

    def _on_mqtt_message(self, topic, payload):
        """Handle subscribed MQTT traffic on Core 0."""
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

        if topic == self._config["mqtt_topic_info_response"]:
            if doc.get("message_type") == "info_response":
                self._handle_info_response(doc)
            return

        if topic != self._config["mqtt_topic_command"]:
            return
        if doc.get("message_type") != "command":
            return

        target = doc.get("target", "")
        if not self._target_matches(target):
            return

        command = doc.get("command")
        command_id = doc.get("command_id")
        targeted = target != "*"

        if not isinstance(command, str) or not command:
            return
        if not isinstance(command_id, str) or not command_id:
            return

        if doc.get("message_schema_version") != MESSAGE_SCHEMA_VERSION:
            self._queue_connection_log(
                LEVEL_WARNING,
                EVENT_COMMAND_REJECTED,
                REASON_COMMAND_INVALID_ENVELOPE,
                "Command rejected: unsupported message_schema_version",
                {"command": command, "command_id": command_id},
            )
            self._queue_core0_response({
                "command_id": command_id,
                "command": command,
                "success": False,
                "targeted": targeted,
                "error": {
                    "code": REASON_COMMAND_INVALID_ENVELOPE,
                    "message": "Unsupported message_schema_version",
                },
            })
            return

        if "payload" not in doc:
            self._queue_connection_log(
                LEVEL_WARNING,
                EVENT_COMMAND_REJECTED,
                REASON_COMMAND_INVALID_PAYLOAD,
                "Command rejected: payload is required",
                {"command": command, "command_id": command_id},
            )
            self._queue_core0_response({
                "command_id": command_id,
                "command": command,
                "success": False,
                "targeted": targeted,
                "error": {
                    "code": REASON_COMMAND_INVALID_PAYLOAD,
                    "message": "command payload is required",
                },
            })
            return

        payload_obj = doc["payload"]
        if not isinstance(payload_obj, dict):
            self._queue_connection_log(
                LEVEL_WARNING,
                EVENT_COMMAND_REJECTED,
                REASON_COMMAND_INVALID_PAYLOAD,
                "Command rejected: payload must be an object",
                {"command": command, "command_id": command_id},
            )
            self._queue_core0_response({
                "command_id": command_id,
                "command": command,
                "success": False,
                "targeted": targeted,
                "error": {
                    "code": REASON_COMMAND_INVALID_PAYLOAD,
                    "message": "command payload must be an object",
                },
            })
            return

        if command == "reboot":
            if payload_obj:
                self._queue_connection_log(
                    LEVEL_WARNING,
                    EVENT_COMMAND_REJECTED,
                    REASON_COMMAND_INVALID_PAYLOAD,
                    "Command rejected: reboot payload must be {}",
                    {"command": command, "command_id": command_id},
                )
                self._queue_core0_response({
                    "command_id": command_id,
                    "command": command,
                    "success": False,
                    "targeted": targeted,
                    "error": {
                        "code": REASON_COMMAND_INVALID_PAYLOAD,
                        "message": "reboot payload must be {}",
                    },
                })
                return

            if self._pending_reboot is not None:
                self._queue_connection_log(
                    LEVEL_WARNING,
                    EVENT_COMMAND_REJECTED,
                    REASON_COMMAND_DUPLICATE,
                    "Command rejected: a reboot is already pending",
                    {"command": command, "command_id": command_id},
                )
                self._queue_core0_response({
                    "command_id": command_id,
                    "command": command,
                    "success": False,
                    "targeted": targeted,
                    "error": {
                        "code": REASON_COMMAND_DUPLICATE,
                        "message": "A reboot is already pending",
                    },
                })
                return

            self._pending_reboot = {
                "command_id": command_id,
                "command": command,
                "targeted": targeted,
            }
            self._queue_connection_log(
                LEVEL_INFO,
                EVENT_RUNTIME_REBOOT_REQUESTED,
                REASON_NONE,
                "Reboot command accepted",
                {"command": command, "command_id": command_id},
            )
            return

        if command == "read_config":
            if payload_obj:
                self._queue_connection_log(
                    LEVEL_WARNING,
                    EVENT_COMMAND_REJECTED,
                    REASON_COMMAND_INVALID_PAYLOAD,
                    "Command rejected: read_config payload must be {}",
                    {"command": command, "command_id": command_id},
                )
                self._queue_core0_response({
                    "command_id": command_id,
                    "command": command,
                    "success": False,
                    "targeted": targeted,
                    "error": {
                        "code": REASON_COMMAND_INVALID_PAYLOAD,
                        "message": "read_config payload must be {}",
                    },
                })
                return

            state = self._intercore.config_state
            if state is None:
                self._queue_core0_response({
                    "command_id": command_id,
                    "command": command,
                    "success": False,
                    "targeted": targeted,
                    "error": {
                        "code": REASON_COMMAND_EXECUTION_FAILED,
                        "message": "Configuration state is unavailable",
                    },
                })
                return

            view = state.snapshot()
            # The committed configuration comes from config.json, which never
            # carries Wi-Fi credentials (those live in config-secrets.json and
            # only ever reach Core 0 as wifi_config) -- nothing to redact.
            self._queue_core0_response({
                "command_id": command_id,
                "command": command,
                "success": True,
                "targeted": targeted,
                "data": {
                    "config": state.committed_config(),
                    "config_checksum_sha256": view["config_checksum_sha256"],
                    "reboot_required": view["reboot_required"],
                    "pending_restart_keys": view["pending_restart_keys"],
                },
            })
            return

        if command == "write_config":
            if not payload_obj:
                self._queue_connection_log(
                    LEVEL_WARNING,
                    EVENT_COMMAND_REJECTED,
                    REASON_COMMAND_INVALID_PAYLOAD,
                    "Command rejected: write_config payload must be a non-empty object",
                    {"command": command, "command_id": command_id},
                )
                self._queue_core0_response({
                    "command_id": command_id,
                    "command": command,
                    "success": False,
                    "targeted": targeted,
                    "error": {
                        "code": REASON_COMMAND_INVALID_PAYLOAD,
                        "message": "write_config payload must be a non-empty object",
                    },
                })
                return

            # The transaction is synchronous up to the Core 1 mailbox phase;
            # a MemoryError from any allocation below propagates (fail-fast).
            self._begin_config_write_transaction(command_id, targeted, payload_obj)
            return

        event = {
            "command_id": command_id,
            "command": command,
            "payload": payload_obj,
            "targeted": targeted,
        }
        if not self._intercore.event_queue.put(event):
            self._queue_core0_response({
                "command_id": command_id,
                "command": command,
                "success": False,
                "targeted": targeted,
                "error": {
                    "code": REASON_COMMAND_EXECUTION_FAILED,
                    "message": "Core 1 event queue is full",
                },
            })

    def _service_pending_core0_response(self):
        if not self._pending_core0_responses:
            return

        response = self._pending_core0_responses[0]
        # Claim (or, on a retry, reuse) the wire sequence on the persistent
        # response so a re-publish after an ambiguous QoS 1 failure keeps the
        # same (runtime_id, sequence) identity instead of shifting it.
        wire_sequence = self._claim_wire_sequence(response)
        self._publish_core0_command_response(
            response["command_id"],
            response["command"],
            response["success"],
            targeted=response.get("targeted", False),
            data=response.get("data"),
            error=response.get("error"),
            wire_sequence=wire_sequence,
            container=response,
        )
        self._pending_core0_responses.pop(0)

    # ------------------------------------------------------------------
    # Runtime configuration transactions (write_config coordinator)
    #
    # Core 0 owns the complete transaction: validation, staging, activation
    # (Core 1 via the mailbox first, then Core 0), commit, and state update.
    # All-or-nothing: on any activation or commit failure, every change
    # already activated is rolled back and the staged file is discarded, so
    # runtime and config.json both stay at the previous known-good state.
    # A response-delivery failure never rolls a committed configuration
    # back (responses ride the existing retry mechanism).
    # ------------------------------------------------------------------

    def _reject_config_write(self, command_id, targeted, code, message):
        self._queue_connection_log(
            LEVEL_WARNING,
            EVENT_COMMAND_REJECTED,
            code,
            "Command rejected: {}".format(message),
            {"command": "write_config", "command_id": command_id},
        )
        self._queue_core0_response({
            "command_id": command_id,
            "command": "write_config",
            "success": False,
            "targeted": targeted,
            "error": {
                "code": code,
                "message": message,
            },
        })

    def _discard_staged(self, tx):
        """Remove the staged candidate (best effort; a stale .tmp is cleaned
        at boot and is never auto-promoted)."""
        staged = tx.get("staged_path")
        if staged is None:
            return
        tx["staged_path"] = None
        try:
            os.remove(staged)
        except MemoryError:
            raise
        except Exception:
            pass

    def _begin_config_write_transaction(self, command_id, targeted, patch):
        if self._config_tx_in_progress:
            self._reject_config_write(
                command_id, targeted,
                REASON_CONFIG_UPDATE_IN_PROGRESS,
                "A configuration transaction is already in progress",
            )
            return

        state = self._intercore.config_state
        if state is None:
            self._reject_config_write(
                command_id, targeted,
                REASON_COMMAND_EXECUTION_FAILED,
                "Configuration state is unavailable",
            )
            return

        try:
            changed_keys = validate_config_patch(patch)
        except MemoryError:
            raise
        except ConfigPatchError as err:
            self._reject_config_write(command_id, targeted, err.code, err.message)
            return

        committed = state.committed_config()
        # No-op detection: a patch equal to the committed configuration
        # (including a redelivered already-committed command) succeeds
        # without incrementing the generation.
        if all(committed[key] == patch[key] for key in changed_keys):
            view = state.snapshot()
            self._queue_core0_response({
                "command_id": command_id,
                "command": "write_config",
                "success": True,
                "targeted": targeted,
                "data": {
                    "config_generation": view["config_generation"],
                    "config_checksum_sha256": view["config_checksum_sha256"],
                    "changed_keys": [],
                    "dynamic_keys": [],
                    "reconfigured_keys": [],
                    "restart_required_keys": [],
                    "reboot_required": view["reboot_required"],
                },
            })
            return

        candidate = dict(committed)
        candidate.update(patch)
        candidate["config_generation"] = state.generation() + 1
        try:
            validate_config(candidate)
        except MemoryError:
            raise
        except Exception:
            self._reject_config_write(
                command_id, targeted,
                REASON_CONFIG_INVALID_COMBINATION,
                "Combined configuration is invalid",
            )
            return

        started_ms = time.ticks_ms()
        staged_path = CONFIG_FILE + CONFIG_STAGED_SUFFIX
        try:
            staged_checksum = stage_config_candidate(
                candidate, CONFIG_FILE, staged_path)
        except MemoryError:
            raise
        except Exception:
            self._queue_connection_log(
                LEVEL_ERROR,
                EVENT_CONFIGURATION_UPDATE_FAILED,
                REASON_CONFIG_STAGE_FAILED,
                "Configuration transaction failed: candidate staging failed",
                {
                    "config_generation": candidate["config_generation"],
                    "changed_key_count": len(changed_keys),
                },
            )
            self._reject_config_write(
                command_id, targeted,
                REASON_CONFIG_STAGE_FAILED,
                "Unable to stage candidate configuration",
            )
            return

        dynamic_keys = [
            key for key in changed_keys
            if CONFIG_CHANGE_POLICIES[key] == CHANGE_DYNAMIC
        ]
        reconfigured_keys = [
            key for key in changed_keys
            if CONFIG_CHANGE_POLICIES[key] == CHANGE_RECONFIGURE
        ]
        restart_keys = [
            key for key in changed_keys
            if CONFIG_CHANGE_POLICIES[key] == CHANGE_RESTART_REQUIRED
        ]
        core1_changes = {
            key: candidate[key] for key in changed_keys
            if key in CORE1_DYNAMIC_KEYS or key in CORE1_RECONFIGURE_KEYS
        }
        core0_dynamic_changes = {
            key: candidate[key] for key in changed_keys
            if key in CORE0_DYNAMIC_KEYS
        }
        mqtt_reconfigure_keys = [
            key for key in reconfigured_keys
            if key in CORE0_MQTT_RECONFIGURE_KEYS
        ]

        self._config_tx = {
            "command_id": command_id,
            "targeted": targeted,
            "candidate": candidate,
            "staged_path": staged_path,
            "staged_checksum": staged_checksum,
            "started_ms": started_ms,
            "changed_keys": changed_keys,
            "dynamic_keys": dynamic_keys,
            "reconfigured_keys": reconfigured_keys,
            "restart_keys": restart_keys,
            "core1_changes": core1_changes,
            "core0_dynamic_changes": core0_dynamic_changes,
            "mqtt_reconfigure_keys": mqtt_reconfigure_keys,
            "phase": "staged",
            "deadline_ms": None,
            "core0_snapshot": None,
            "mqtt_previous": None,
        }
        self._config_tx_in_progress = True
        tx = self._config_tx
        # Log the changed keys and policy classes -- never the values.
        self._queue_connection_log(
            LEVEL_INFO,
            EVENT_CONFIGURATION_UPDATE_STARTED,
            REASON_NONE,
            "Configuration update started",
            {
                "config_generation": candidate["config_generation"],
                "changed_keys": changed_keys,
                "dynamic_keys": dynamic_keys,
                "reconfigured_keys": reconfigured_keys,
                "restart_required_keys": restart_keys,
            },
        )

        if core1_changes:
            # Core 1 activates first (its phase is the long one: device
            # reconfiguration). The run-loop hook polls the mailbox; no
            # busy-spin, one transaction at a time.
            if not self._intercore.config_transaction_mailbox.put_request({
                "transaction_id": candidate["config_generation"],
                "action": CONFIG_TX_ACTION_APPLY,
                "changes": core1_changes,
            }):
                self._fail_config_transaction(
                    tx,
                    REASON_CONFIG_UPDATE_IN_PROGRESS,
                    "Core 1 is not accepting configuration transactions",
                    rollback_done=False,
                )
                return
            tx["phase"] = "awaiting_core1_apply"
            tx["deadline_ms"] = time.ticks_add(started_ms, _CONFIG_TX_TIMEOUT_MS)
            return

        try:
            self._apply_core0_phase(tx)
            self._complete_config_transaction(tx)
        except ConfigTransactionError as err:
            self._rollback_after_core0_failure(tx, err.code, err.message)

    def _apply_core0_phase(self, tx):
        """Activate the Core 0-owned changes (Core 1 already succeeded or is
        not involved). Order: Core 0 DYNAMIC (non-disruptive) first, then the
        single MQTT RECONFIGURE operation (most disruptive, last). Previous
        values are snapshotted for rollback. Raises ConfigTransactionError
        with the stable code on failure (after leaving the runtime in the
        previous configuration where possible)."""
        candidate = tx["candidate"]
        changes = tx["core0_dynamic_changes"]
        snapshot = {}
        netdiag_changed = False
        mqtt_reconnect_changed = False
        for key, value in changes.items():
            snapshot[key] = self._config.get(key)
            self._config[key] = value
            if key == "wifi_reconnect_delays_sec":
                self._wifi.set_reconnect_delays(value)
            elif key == "mqtt_reconnect_delays_sec":
                mqtt_reconnect_changed = True
            elif key == "network_diagnostics_interval_sec":
                self._netdiag_interval_ms = value * 1000
                netdiag_changed = True
            elif key == "network_diagnostics_broker_latency_enabled":
                self._netdiag_broker_enabled = value
                netdiag_changed = True
            elif key == "mqtt_post_outage_drain_rate_per_sec":
                self._drain_rate_per_sec = value
        if netdiag_changed:
            self._reset_netdiag_cycle(time.ticks_ms())
        if mqtt_reconnect_changed:
            try:
                self._mqtt.update_connection_config({
                    "mqtt_reconnect_delays_sec":
                        changes["mqtt_reconnect_delays_sec"],
                })
            except MemoryError:
                raise
            except Exception:
                pass  # snapshot restore covers it on rollback
        tx["core0_snapshot"] = snapshot

        if not tx["mqtt_reconfigure_keys"]:
            return

        previous = self._mqtt.mqtt_config_snapshot()
        mqtt_changes = {
            key: candidate[key] for key in tx["mqtt_reconfigure_keys"]
        }
        # The shared config dict is the live source for topic lookups; keep
        # it and the connection object consistent for the rest of the pass.
        for key, value in mqtt_changes.items():
            self._config[key] = value
        tx["mqtt_previous"] = previous
        # Reconfigure = mark disconnected (existing bounded cleanup closes the
        # socket directly, no DISCONNECT write into the dead link), apply the
        # candidate, reconnect with the bounded handshake and subscriptions.
        self._mqtt.mark_disconnected()
        self._mqtt.update_connection_config(mqtt_changes)
        try:
            connected = self._mqtt.connect()
        except MemoryError:
            raise
        except Exception:
            connected = False
        if not connected:
            # Leave the previous connection configuration in place; the
            # rollback path performs the reconnect and reports the outcome.
            self._mqtt.mark_disconnected()
            self._mqtt.update_connection_config(previous)
            for key in tx["mqtt_reconfigure_keys"]:
                self._config[key] = previous[key]
            raise ConfigTransactionError(
                REASON_CONFIG_RECONFIGURE_FAILED,
                "MQTT reconfiguration failed; previous connection being restored",
            )

    def _rollback_after_core0_failure(self, tx, code, message):
        """Roll back every activated change after a post-activation failure.

        Restores the MQTT connection (if it was reconfigured), the Core 0
        DYNAMIC values, and requests a Core 1 rollback (if Core 1 applied).
        Reports the original failure code, or configuration_rollback_failed
        when the rollback itself could not restore the MQTT connection.
        """
        rollback_ok = True

        if tx["mqtt_previous"] is not None:
            previous = tx["mqtt_previous"]
            self._mqtt.mark_disconnected()
            try:
                self._mqtt.update_connection_config(previous)
            except MemoryError:
                raise
            except Exception:
                rollback_ok = False
            for key in tx["mqtt_reconfigure_keys"]:
                self._config[key] = previous[key]
            if rollback_ok:
                try:
                    if not self._mqtt.connect():
                        rollback_ok = False
                except MemoryError:
                    raise
                except Exception:
                    rollback_ok = False

        # Restored after the MQTT restore so an overlapping
        # mqtt_reconnect_delays_sec DYNAMIC change wins with its previous
        # value.
        snapshot = tx["core0_snapshot"]
        if snapshot:
            for key, value in snapshot.items():
                self._config[key] = value
                if key == "wifi_reconnect_delays_sec":
                    self._wifi.set_reconnect_delays(value)
                elif key == "mqtt_reconnect_delays_sec":
                    try:
                        self._mqtt.update_connection_config({
                            "mqtt_reconnect_delays_sec": value,
                        })
                    except MemoryError:
                        raise
                    except Exception:
                        pass
                elif key == "network_diagnostics_interval_sec":
                    self._netdiag_interval_ms = value * 1000
                elif key == "network_diagnostics_broker_latency_enabled":
                    self._netdiag_broker_enabled = value
                elif key == "mqtt_post_outage_drain_rate_per_sec":
                    self._drain_rate_per_sec = value
            if "network_diagnostics_interval_sec" in snapshot \
                    or "network_diagnostics_broker_latency_enabled" in snapshot:
                self._reset_netdiag_cycle(time.ticks_ms())

        if tx["core1_changes"]:
            # Core 1 applied atomically (or not at all); ask it to undo.
            # The result arrives asynchronously and is drained by the run-loop
            # hook; a live Core 1 answers well before the transaction guard
            # matters again.
            self._intercore.config_transaction_mailbox.put_request({
                "transaction_id": tx["candidate"]["config_generation"],
                "action": CONFIG_TX_ACTION_ROLLBACK,
            })

        final_code = code if rollback_ok else REASON_CONFIG_ROLLBACK_FAILED
        final_message = message if rollback_ok else \
            "Configuration rollback failed: previous MQTT connection could not be restored"
        self._fail_config_transaction(
            tx, final_code, final_message, rollback_done=True)

    def _complete_config_transaction(self, tx):
        """Commit the staged file, update the shared state, finalize.

        The file commit is last (after both cores are active) so a commit
        failure can roll the runtime back without a torn file. The response
        is queued here, AFTER the commit -- a response-delivery failure from
        this point on must not (and cannot) roll the configuration back.
        """
        try:
            committed_checksum = commit_config_file(
                CONFIG_FILE, tx["staged_path"])
        except MemoryError:
            raise
        except Exception:
            raise ConfigTransactionError(
                REASON_CONFIG_PERSISTENCE_FAILED,
                "Configuration persistence failed",
            )
        tx["staged_path"] = None  # promoted to the primary; never re-removed

        state = self._intercore.config_state
        try:
            state.commit(tx["candidate"], committed_checksum)
        except MemoryError:
            raise
        except Exception:
            # The file (source of truth) is committed and the runtime is
            # active; only the shared view failed to swap. Flag the anomaly;
            # the next boot restores the view from the committed file.
            self._queue_connection_log(
                LEVEL_ERROR,
                EVENT_CONFIGURATION_ROLLBACK_FAILED,
                REASON_CONFIG_ROLLBACK_FAILED,
                "Configuration state view update failed after commit",
                {
                    "config_generation": tx["candidate"]["config_generation"],
                },
            )

        view = state.snapshot()
        data = {
            "config_generation": tx["candidate"]["config_generation"],
            "config_checksum_sha256": committed_checksum,
            "changed_keys": tx["changed_keys"],
            "dynamic_keys": tx["dynamic_keys"],
            "reconfigured_keys": tx["reconfigured_keys"],
            "restart_required_keys": tx["restart_keys"],
            "reboot_required": view["reboot_required"],
        }

        duration_ms = time.ticks_diff(time.ticks_ms(), tx["started_ms"])
        self._queue_connection_log(
            LEVEL_INFO,
            EVENT_CONFIGURATION_UPDATE_COMPLETED,
            REASON_NONE,
            "Configuration update completed",
            {
                "config_generation": tx["candidate"]["config_generation"],
                "changed_key_count": len(tx["changed_keys"]),
                "duration_ms": duration_ms,
            },
        )
        self._queue_core0_response({
            "command_id": tx["command_id"],
            "command": "write_config",
            "success": True,
            "targeted": tx["targeted"],
            "data": data,
        })

        if tx["core1_changes"] and \
                self._intercore.config_transaction_mailbox.put_request({
                    "transaction_id": tx["candidate"]["config_generation"],
                    "action": CONFIG_TX_ACTION_COMMIT,
                }):
            tx["phase"] = "awaiting_core1_commit"
            tx["deadline_ms"] = time.ticks_add(
                time.ticks_ms(), _CONFIG_TX_TIMEOUT_MS)
        else:
            self._config_tx = None
            self._config_tx_in_progress = False

    def _fail_config_transaction(self, tx, code, message, rollback_done):
        """Fail the transaction: events (never values), response, cleanup."""
        duration_ms = time.ticks_diff(time.ticks_ms(), tx["started_ms"])
        if rollback_done:
            self._queue_connection_log(
                LEVEL_INFO,
                EVENT_CONFIGURATION_ROLLBACK_COMPLETED,
                REASON_NONE,
                "Configuration rollback completed",
                {
                    "config_generation": tx["candidate"]["config_generation"],
                    "duration_ms": duration_ms,
                },
            )
        self._queue_connection_log(
            LEVEL_ERROR,
            EVENT_CONFIGURATION_UPDATE_FAILED,
            code,
            "Configuration transaction failed: {}".format(message),
            {
                "config_generation": tx["candidate"]["config_generation"],
                "changed_key_count": len(tx["changed_keys"]),
                "duration_ms": duration_ms,
            },
        )
        self._queue_core0_response({
            "command_id": tx["command_id"],
            "command": "write_config",
            "success": False,
            "targeted": tx["targeted"],
            "error": {
                "code": code,
                "message": message,
            },
        })
        self._discard_staged(tx)
        self._config_tx = None
        self._config_tx_in_progress = False

    def _timeout_config_transaction(self, tx):
        if tx["phase"] == "awaiting_core1_apply":
            # Core 0 has activated nothing yet, so the only outstanding work
            # is whatever Core 1 may have applied. Request a rollback
            # (best effort; a wedged Core 1 is bounded by its watchdog) and
            # fail the transaction.
            self._intercore.config_transaction_mailbox.put_request({
                "transaction_id": tx["candidate"]["config_generation"],
                "action": CONFIG_TX_ACTION_ROLLBACK,
            })
            self._fail_config_transaction(
                tx,
                REASON_CONFIG_RECONFIGURE_FAILED,
                "Core 1 did not answer the configuration apply request in time",
                rollback_done=True,
            )
            return
        # awaiting_core1_commit: the transaction already finalized (file
        # committed, state updated, response queued); only the token discard
        # is outstanding. Drop the guard; a late answer is drained by the
        # hook.
        self._config_tx = None
        self._config_tx_in_progress = False

    def _advance_config_transaction(self, tx, result):
        if tx["phase"] == "awaiting_core1_apply":
            if not result.get("success"):
                # Core 1 applied nothing (its apply is atomic); discard the
                # staged candidate and fail the transaction.
                self._discard_staged(tx)
                self._fail_config_transaction(
                    tx,
                    REASON_CONFIG_RECONFIGURE_FAILED,
                    result.get("error") or
                    "Core 1 rejected the configuration changes",
                    rollback_done=False,
                )
                return
            # Core 1 is active: activate the Core 0 side, commit, finalize.
            tx["phase"] = "core0_apply"
            try:
                self._apply_core0_phase(tx)
                self._complete_config_transaction(tx)
            except ConfigTransactionError as err:
                self._rollback_after_core0_failure(tx, err.code, err.message)
            return

        # awaiting_core1_commit: the late token-discard acknowledgement.
        self._config_tx = None
        self._config_tx_in_progress = False

    def _service_config_transaction(self):
        """Run-loop hook: advance or drain the configuration transaction.

        Non-blocking: at most one mailbox poll per pass. A live Core 1
        answers within a few passes; the bound is a safety net for a wedged
        one (the heartbeat watchdog resets the board on its own schedule).
        """
        mailbox = self._intercore.config_transaction_mailbox
        result = mailbox.take_result()
        tx = self._config_tx
        if tx is None:
            # A result with no active transaction is a late Core 1
            # acknowledgement (commit/rollback) for a transaction that already
            # finalized; consuming it frees the mailbox.
            return
        if result is not None and tx["phase"] in (
                "awaiting_core1_apply", "awaiting_core1_commit"):
            self._advance_config_transaction(tx, result)
            return
        deadline = tx["deadline_ms"]
        if deadline is not None and \
                time.ticks_diff(time.ticks_ms(), deadline) >= 0:
            self._timeout_config_transaction(tx)

    def _handle_info_response(self, doc):
        if doc.get("message_schema_version") != MESSAGE_SCHEMA_VERSION:
            return
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
        """Serialize the six envelope members Core 0 owns on the wire.

        Returns them as JSON members without the outer braces, ready to be
        spliced into a queued message before its closing brace. ``json.dumps``
        escapes the string values (runtime_id, source). Senders must not carry
        any of these keys at the top level of their message, or the wire
        document would repeat a name.
        """
        fragment = json.dumps({
            "sequence": sequence,
            "runtime_id": self._runtime_id,
            "source": self._config["source"],
            "firmware_version": FIRMWARE_VERSION,
            "message_schema_version": MESSAGE_SCHEMA_VERSION,
            "firmware_build_commit": FIRMWARE_BUILD_COMMIT,
        })
        return fragment[1:-1].encode("utf-8")

    def _claim_wire_sequence(self, container):
        """Claim the next wire sequence number and stamp it on ``container``.

        QoS 1 has an ambiguous failure mode: the PUBLISH frame can reach the
        broker while the PUBACK is lost, so a failed publish attempt may still
        have been delivered. A number handed to such an attempt can therefore
        never be given to a different logical message -- that is the
        (runtime_id, sequence) collision this avoids. The claim is made before
        the first transmission attempt, stamped on the logical object (the
        queue entry, or the persistent response/reboot dict for Core 0's own
        retryable messages), and never rolled back: a retry of the SAME logical
        message finds the stamp and reuses the number (both copies identify one
        message -- legitimate QoS 1 duplicate delivery), while a DIFFERENT
        message always gets a fresh number.
        """
        sequence = container.get("_wire_sequence")
        if sequence is None:
            sequence = self._next_sequence
            self._next_sequence += 1
            container["_wire_sequence"] = sequence
        return sequence

    def _publish_entry(self, entry):
        """Publish one MQTT entry from its pre-serialized message bytes.

        The queue stores the message as final, pre-serialized bytes: the wire
        frame is that same JSON object with the six Core-0-owned envelope
        members (sequence, runtime_id, source, firmware_version,
        message_schema_version, firmware_build_commit) spliced in before its
        closing brace. The
        payload is never decoded, parsed, or re-serialized here, so publishing
        allocates only the small envelope fragment and the assembled frame
        instead of a full decode + dict graph + re-encode of the message.

        Sequence: claimed once, before the first transmission attempt (see
        _claim_wire_sequence). An attempt that fails the object check above has
        transmitted nothing, so it consumes no number; a frame that is
        transmitted is always bound to a number that no other logical message
        can ever receive.
        """
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
        # Boundary 5 (publish): record a free-heap checkpoint and leave headroom
        # for the assembled wire frame (one message + small envelope) BEFORE it
        # is allocated. Optional collect (cooldown-gated); the admission path is
        # the authority for the reserve itself.
        self._intercore.memory_stats.observe_current_free_heap()
        self._intercore.memory_stats.collect_if_below(
            self._intercore.minimum_free_heap_bytes + PUBLISH_GC_HEADROOM_BYTES
        )
        encoded = b"".join((body[:-1], b",", self._envelope_fragment(sequence), b"}"))
        topic = entry.get("topic")
        if topic is None:
            topic = self._topic_for_kind(entry["kind"])
        # Logical retry classification (owned by the caller that knows whether
        # this same logical message was attempted before): read the marker on
        # the logical object, then set it. For a queued entry the entry IS the
        # persistent in-flight object; for a command response the marker was
        # synced onto the entry from the persistent response container by
        # _publish_core0_command_response. A first attempt reports False; every
        # later attempt of the same logical message reports True.
        is_retry = entry.get("_publish_attempted", False)
        entry["_publish_attempted"] = True
        self._mqtt.publish_qos1(topic, encoded, is_retry=is_retry)
        if entry.get("kind") == KIND_TELEMETRY:
            self._led_manager.telemetry_sent()
        if DEBUG:
            print("[DEBUG] QoS 1 published: seq={}".format(sequence))

    def _publish_core0_command_response(
        self, command_id, command, success, targeted=True, data=None, error=None,
        wire_sequence=None, container=None
    ):
        """Build and publish a Core 0 command response.

        The command response is serialized before publishing to honor the
        pre-serialized outbound-message contract.

        ``wire_sequence`` carries a sequence already claimed for this logical
        response (by a retrying caller), so the re-publish keeps that identity.
        Omit it on a first attempt: _publish_entry then claims a fresh number.

        ``container`` is the persistent logical object for this response (the
        pending response or reboot dict). When given, the serialized bytes are
        built once, on the first attempt, and frozen on it; a retry after an
        ambiguous QoS 1 failure re-publishes those SAME bytes. Combined with the
        sequence being claimed on the same container, a retry is now the same
        logical message with the same identity AND the same wire content, the
        way the outbound queue's in-flight entry reuses its pre-serialized
        bytes. Omit it to build-and-publish with no persistent owner.
        """
        payload_bytes = container.get("_payload_bytes") if container is not None else None
        if payload_bytes is None:
            payload = {
                "command_id": command_id,
                "command": command,
                "targeted": targeted,
                "success": success,
            }
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
            except Exception as err:
                if DEBUG:
                    print("[DEBUG] Command response serialization failed: {}".format(err))
                return
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
        # The retry marker is owned by the persistent logical object (the
        # pending response/reboot container), not this attempt's entry, which is
        # rebuilt on every attempt. Copy it onto the entry so _publish_entry
        # classifies the attempt, and advance it on the container so a later
        # re-publish of this same logical response is recognized as a retry. A
        # freshly created response (no container marker) is a first attempt.
        if container is not None:
            entry["_publish_attempted"] = container.get("_publish_attempted", False)
            container["_publish_attempted"] = True
        self._publish_entry(entry)

    def _perform_reboot(self):
        request = self._pending_reboot
        if request is None:
            return True
        if self._intercore.outbound_queue.has_in_flight():
            return False

        try:
            # Claim (or, on a retry, reuse) the wire sequence on the pending
            # reboot request so a re-publish keeps the same sequence identity.
            wire_sequence = self._claim_wire_sequence(request)
            self._publish_core0_command_response(
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
        # MQTT reliability metrics (Mqtt is the single source of truth): copied
        # into this immutable network snapshot, the one Core 0 -> Core 1
        # transport for them (no new mailbox, event entry, or lock).
        snapshot["mqtt_publish_attempt_count"] = mqtt_status["publish_attempt_count"]
        snapshot["mqtt_publish_retry_count"] = mqtt_status["publish_retry_count"]
        snapshot["mqtt_puback_timeout_count"] = mqtt_status["puback_timeout_count"]
        snapshot["mqtt_connection_failure_count"] = mqtt_status["connection_failure_count"]
        snapshot["mqtt_reconnect_success_count"] = mqtt_status["reconnect_success_count"]
        snapshot["mqtt_last_reconnect_duration_ms"] = mqtt_status["last_reconnect_duration_ms"]
        snapshot["mqtt_last_outage_duration_ms"] = mqtt_status["last_outage_duration_ms"]
        # Network diagnostics (Core 0-owned; observational — they never add a
        # degraded reason or drive recovery). Canonical external names; null
        # means not-yet-tested or unsupported (never converted to false).
        snapshot["wifi_rssi_min_dbm"] = snapshot.get("rssi_min_dbm")
        snapshot["wifi_rssi_max_dbm"] = snapshot.get("rssi_max_dbm")
        snapshot["wifi_rssi_moving_average_dbm"] = snapshot.get("rssi_moving_average_dbm")
        snapshot["wifi_rssi_sample_count"] = snapshot.get("rssi_sample_count")
        snapshot["wifi_last_reconnect_duration_ms"] = snapshot.get("last_reconnect_duration_ms")
        snapshot["wifi_last_dhcp_acquisition_duration_ms"] = snapshot.get(
            "last_dhcp_acquisition_duration_ms")
        snapshot["wifi_last_status_reason"] = snapshot.get("last_status_reason", "unknown")
        snapshot["wifi_last_reconnect_trigger"] = snapshot.get(
            "last_reconnect_trigger", "unknown")
        snapshot["wifi_association_details_supported"] = snapshot.get(
            "association_details_supported")
        snapshot["wifi_bssid"] = snapshot.get("bssid")
        snapshot["wifi_channel"] = snapshot.get("channel")
        snapshot["gateway_reachability_supported"] = self._gateway_reachability_supported
        snapshot["gateway_reachable"] = self._gateway_reachable
        snapshot["gateway_last_latency_ms"] = self._gateway_last_latency_ms
        snapshot["dns_reachable"] = self._dns_reachable
        snapshot["dns_last_latency_ms"] = self._dns_last_latency_ms
        snapshot["mqtt_broker_latency_enabled"] = self._netdiag_broker_enabled
        snapshot["mqtt_broker_last_round_trip_ms"] = self._mqtt_broker_last_round_trip_ms
        snapshot["network_diagnostics_run_count"] = self._netdiag_run_count
        snapshot["network_diagnostics_last_run_age_ms"] = (
            time.ticks_diff(now_ms, self._netdiag_last_completed_ms)
            if self._netdiag_last_completed_ms is not None
            else None
        )
        # Post-outage queue drain (Core 0-owned; historical/observational --
        # a past slow drain never adds a degraded reason or changes status).
        # Core 0 is the single source; Core 1 only reads them from here.
        snapshot["outbound_queue_drain_active"] = self._queue_drain_active
        snapshot["outbound_queue_last_drain_start_depth"] = self._last_queue_drain_start_depth
        snapshot["outbound_queue_last_drain_message_count"] = self._last_queue_drain_message_count
        snapshot["outbound_queue_last_drain_duration_ms"] = self._last_queue_drain_duration_ms
        snapshot["outbound_queue_last_drain_rate_per_sec"] = self._last_queue_drain_rate_per_sec
        snapshot["network_stack_ready"] = self._network_stack_ready
        self._intercore.state_mailboxes.set_network_snapshot(snapshot)
        self._last_network_snapshot_ms = now_ms

    def _utc_clear_pending(self):
        self._pending_utc_request_id = None
        self._utc_request_deadline_ms = None

    def _utc_note_reachable_failure(self):
        """Clear a pending request answered with a malformed payload.

        The server reached us, so the full retry interval does not apply:
        re-key the throttle so the resend is allowed after a short delay
        instead of up to _UTC_RETRY_INTERVAL_MS after the original send.
        """
        self._utc_clear_pending()
        self._utc_last_attempt_ms = time.ticks_add(
            time.ticks_ms(),
            _UTC_PROMPT_RETRY_DELAY_MS - _UTC_RETRY_INTERVAL_MS,
        )

    def _utc_send_request(self):
        """Send a UTC time request without blocking.

        The response arrives through the normal MQTT pump; the deadline is
        tracked so the run loop can give up on a request that never answers.

        The pending request ID is armed before publishing: the client
        delivers broker messages while awaiting the PUBACK, so a fast
        response can arrive inside the publish call itself and must find
        the request ID already set.
        """
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

        self._utc_last_attempt_ms = time.ticks_ms()
        timeout_ms = self._config["mqtt_broker_response_timeout_sec"] * 1000
        self._utc_request_deadline_ms = time.ticks_add(time.ticks_ms(), timeout_ms)

    def _utc_wait_response(self):
        """Pump MQTT until the pending UTC response arrives or its deadline.

        Used only during startup, when no other Core 0 work is in flight.
        """
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
        """Clear a pending UTC request whose deadline has passed."""
        if self._pending_utc_request_id is None:
            return
        if time.ticks_diff(
            time.ticks_ms(), self._utc_request_deadline_ms
        ) >= 0:
            self._utc_clear_pending()
            if DEBUG:
                print("[DEBUG] UTC request timed out")

    def _utc_should_send_request(self):
        """True when a new UTC request is due and retry throttling allows it."""
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
        """Perform a QoS 1 network probe and verify matching PUBACK.

        Returns True if the probe succeeds with matching PUBACK,
        False otherwise.
        """
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
            return result
        except MemoryError:
            raise
        except Exception as err:
            if DEBUG:
                print("[DEBUG] Network probe failed: {}".format(err))
            return False

    def _netdiag_network_stable(self):
        """Stable-network preconditions every diagnostics stage requires.

        Diagnostics run only when the full stack is up: startup complete,
        Wi-Fi connected with a valid IP, MQTT connected. Busy/recovery
        periods simply defer (the stage stays armed and retries next pass).
        """
        if not self._network_stack_ready:
            return False
        if not self._wifi.is_connected():
            return False
        if self._wifi.ip_address() is None:
            return False
        if not self._mqtt.is_connected():
            return False
        return True

    def _netdiag_broker_idle(self):
        """The QoS 1 path is completely idle (the broker stage never delays
        real traffic)."""
        if not self._mqtt.is_connected():
            return False
        if self._intercore.outbound_queue.has_in_flight():
            return False
        if self._intercore.outbound_queue.status()["pending"] > 0:
            return False
        if self._pending_core0_responses:
            return False
        if self._pending_connection_logs:
            return False
        if self._pending_reboot is not None:
            return False
        return True

    def _reset_netdiag_cycle(self, now_ms):
        """Discard a partial cycle without counting it; retry next interval."""
        self._netdiag_stage = _NETDIAG_IDLE
        self._netdiag_next_due_ms = time.ticks_add(now_ms, self._netdiag_interval_ms)

    def _complete_netdiag(self, now_ms):
        """One completed cycle: count it and schedule the next one."""
        self._netdiag_last_completed_ms = now_ms
        self._netdiag_run_count += 1
        self._netdiag_next_due_ms = time.ticks_add(now_ms, self._netdiag_interval_ms)
        self._netdiag_stage = _NETDIAG_IDLE

    def _run_gateway_stage(self):
        """Gateway reachability via the feature-detected bounded ICMP echo.

        When the port cannot create a raw ICMP socket (expected on RP2/cyw43)
        the fields report supported=False with null reachable/latency — the
        probe is never faked with a UDP send.
        """
        if not icmp_echo_supported():
            self._gateway_reachability_supported = False
            self._gateway_reachable = None
            self._gateway_last_latency_ms = None
            return
        self._gateway_reachability_supported = True
        gateway = self._wifi.gateway_address()
        if not gateway:
            # No gateway on the interface: not tested (null), not "unreachable".
            self._gateway_reachable = None
            self._gateway_last_latency_ms = None
            return
        try:
            reachable, latency_ms = probe_gateway(gateway)
        except MemoryError:
            raise
        except Exception as err:
            if DEBUG:
                print("[DEBUG] Gateway probe failed: {}".format(err))
            self._gateway_reachable = False
            self._gateway_last_latency_ms = None
            return
        self._gateway_reachable = reachable
        self._gateway_last_latency_ms = latency_ms

    def _run_dns_stage(self):
        """DNS server reachability: one bounded UDP query to the configured
        server (never a public resolver)."""
        dns_server = self._wifi.dns_address()
        if not dns_server:
            # No DNS server on the interface: not tested (null).
            self._dns_reachable = None
            self._dns_last_latency_ms = None
            return
        try:
            reachable, latency_ms = probe_dns_server(dns_server)
        except MemoryError:
            raise
        except Exception as err:
            if DEBUG:
                print("[DEBUG] DNS probe failed: {}".format(err))
            self._dns_reachable = False
            self._dns_last_latency_ms = None
            return
        self._dns_reachable = reachable
        self._dns_last_latency_ms = latency_ms

    def _run_broker_stage(self):
        """Optional broker round-trip latency, reusing the existing QoS 1
        network-probe machinery (same client, topic, and bounded PUBACK
        exchange). A failed probe marks the connection disconnected and the
        existing recovery policy handles it — there is no second recovery
        policy. The latency is recorded only on success."""
        started_ms = time.ticks_ms()
        if self._perform_network_probe():
            self._mqtt_broker_last_round_trip_ms = time.ticks_diff(
                time.ticks_ms(), started_ms)
        else:
            self._mqtt_broker_last_round_trip_ms = None

    def _service_network_diagnostics(self):
        """Advance the staged diagnostics by at most one probe stage.

        interval 0 disables active probes entirely (passive RSSI sampling in
        wifi.py continues; the run count stays 0). When active, a cycle runs
        gateway -> dns -> optional broker, one stage per run-loop pass, only
        while the network is stable; a mid-cycle loss of stability discards
        the partial cycle without counting it. A cycle is counted exactly
        once, on completion.
        """
        if self._netdiag_interval_ms == 0:
            return
        now_ms = time.ticks_ms()

        if self._netdiag_stage == _NETDIAG_IDLE:
            if self._netdiag_next_due_ms is None:
                # First pass arms the deadline: the first cycle starts one
                # full interval after Core 0 is running, never at startup.
                self._netdiag_next_due_ms = time.ticks_add(
                    now_ms, self._netdiag_interval_ms)
                return
            if time.ticks_diff(now_ms, self._netdiag_next_due_ms) < 0:
                return
            if not self._netdiag_network_stable():
                return
            self._run_gateway_stage()
            # One stage this pass: the DNS stage runs on the NEXT pass.
            self._netdiag_stage = _NETDIAG_DNS
            return

        if self._netdiag_stage == _NETDIAG_DNS:
            if not self._netdiag_network_stable():
                self._reset_netdiag_cycle(now_ms)
                return
            self._run_dns_stage()
            if self._netdiag_broker_enabled:
                self._netdiag_stage = _NETDIAG_BROKER
            else:
                self._complete_netdiag(now_ms)
            return

        if self._netdiag_stage == _NETDIAG_BROKER:
            if not self._netdiag_network_stable():
                self._reset_netdiag_cycle(now_ms)
                return
            if not self._netdiag_broker_idle():
                # Busy: never delay real traffic. Skip the broker stage and
                # complete the cycle (it is recorded without a latency).
                self._complete_netdiag(now_ms)
                return
            self._run_broker_stage()
            self._complete_netdiag(now_ms)
            return

    def _drain_startup_mqtt_work(self):
        """Drain any pending Core 0 MQTT work (connection logs, etc.).

        Returns True when no startup work remains, False if timeout reached.
        """
        timeout_ms = 2000  # 2 second max drain time
        start_ms = time.ticks_ms()

        while self._pending_connection_logs:
            if time.ticks_diff(time.ticks_ms(), start_ms) >= timeout_ms:
                print("[WARNING] Startup MQTT work drain timeout")
                return False
            try:
                self._service_pending_connection_log()
                # Brief wait for publish to complete
                time.sleep_ms(50)
            except MemoryError:
                raise
            except Exception as err:
                if DEBUG:
                    print("[DEBUG] Startup work drain failed: {}".format(err))
                # Continue draining, don't fail the entire startup
                time.sleep_ms(50)

        return True

    def _synchronize_utc_required(self):
        """Run one bounded pass of startup UTC synchronization.

        Returns True once a valid snapshot is acquired. Returns False after
        _UTC_STARTUP_MAX_ATTEMPTS attempts without one, so the caller
        (_verify_startup_contract) can re-establish the network and retry the
        pass. A MemoryError propagates unchanged (fail-fast).
        """
        for attempt in range(_UTC_STARTUP_MAX_ATTEMPTS):
            self._utc_send_request()
            self._utc_wait_response()
            if self._utc_snapshot is not None:
                return True
            # Wait before retry
            time.sleep_ms(500)

        print("[WARNING] UTC synchronization pass failed after {} attempts; will re-establish and retry".format(_UTC_STARTUP_MAX_ATTEMPTS))
        return False

    def establish_network(self, reconnect=False):
        """Establish (or re-establish) Wi-Fi, then MQTT + subscriptions.

        ``reconnect`` distinguishes a runtime recovery re-establishment from
        the initial startup pass: it changes only the event names and reason
        codes of the connection log (the initial boot connection is never a
        reconnect).
        """
        self._led_manager.set_connecting(True)

        while not self._wifi.is_connected():
            if self._wifi.connect():
                snapshot = self._wifi.snapshot(False)
                self._queue_connection_log(
                    LEVEL_INFO,
                    EVENT_WIFI_RECONNECT_COMPLETED if reconnect else EVENT_WIFI_CONNECTION_ESTABLISHED,
                    REASON_WIFI_RECONNECT_SUCCEEDED if reconnect else REASON_NONE,
                    "Connected to Wi-Fi",
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
            time.sleep(delay_sec)

        while not self._mqtt.is_connected():
            if self._mqtt.connect():
                mqtt_status = self._mqtt.status()
                self._queue_connection_log(
                    LEVEL_INFO,
                    EVENT_MQTT_RECONNECT_COMPLETED if reconnect else EVENT_MQTT_CONNECTION_ESTABLISHED,
                    REASON_MQTT_RECONNECT_SUCCEEDED if reconnect else REASON_NONE,
                    "Connected to MQTT broker",
                    {
                        "broker_address": self._config["mqtt_broker_ip_address"],
                        "connect_count": mqtt_status["connect_count"],
                        # On a reconnect this records the just-completed outage
                        # and reconnect spans (a discrete event for the MQTT
                        # capture); on the initial connection these are zero
                        # (no outage/reconnect has completed yet).
                        "reconnect_success_count": mqtt_status["reconnect_success_count"],
                        "reconnect_duration_ms": mqtt_status["last_reconnect_duration_ms"],
                        "outage_duration_ms": mqtt_status["last_outage_duration_ms"],
                    },
                )
                # LED remains flashing during network probe and UTC sync
                break
            delay_sec = self._config["mqtt_reconnect_delays_sec"][-1]
            print("[WARNING] MQTT connection sequence exhausted; retrying in {} sec".format(delay_sec))
            time.sleep(delay_sec)

    # --- Post-outage queue drain -------------------------------------------
    # A runtime MQTT reconnect can leave Core 1's outage-buffered telemetry in
    # the outbound queue. Core 0 owns that drain episode and an optional,
    # non-blocking ceiling on its publish rate. Scalar state only; the
    # metrics are historical/observational (never a degraded reason) and the
    # gate touches only the run-loop take()/publish step -- never connect,
    # logs, responses, polling, PINGREQ, UTC, probes, or the queue's own
    # eviction/capacity/byte policies.

    def _start_post_outage_queue_drain(self):
        """Start a drain episode after a runtime reconnect completed.

        Only the run-loop recovery path calls this -- never start(), so the
        initial connection and its startup work drain are not treated as a
        post-outage drain. An empty queue starts no episode and leaves the
        last completed metrics unchanged.
        """
        now_ms = time.ticks_ms()
        depth = self._intercore.outbound_queue.get_depth()
        if depth <= 0:
            self._queue_drain_active = False
            self._queue_drain_started_ms = None
            self._queue_drain_start_depth = 0
            self._queue_drain_message_count = 0
            self._queue_drain_next_publish_ms = None
            return

        self._queue_drain_active = True
        self._queue_drain_started_ms = now_ms
        self._queue_drain_start_depth = depth
        self._queue_drain_message_count = 0
        # The first entry is eligible immediately after the reconnect.
        self._queue_drain_next_publish_ms = now_ms

    def _cancel_post_outage_queue_drain(self):
        """Interrupt an in-progress drain episode (the connection failed
        mid-drain). The unfinished episode is discarded; the last completed
        metrics are preserved and the next successful reconnect starts a
        fresh episode at the then-current depth.
        """
        self._queue_drain_active = False
        self._queue_drain_started_ms = None
        self._queue_drain_start_depth = 0
        self._queue_drain_message_count = 0
        self._queue_drain_next_publish_ms = None

    def _post_outage_queue_publish_allowed(self, now_ms):
        """Whether the run-loop take/publish step is eligible this pass.

        Always true when no episode is active or the limit is disabled (0) --
        that preserves the current unlimited drain. Only an active episode
        with a positive rate defers the step until the next drain slot is due
        (tick-wrap-safe; non-blocking -- it never sleeps).
        """
        if not self._queue_drain_active or self._drain_rate_per_sec == 0:
            return True
        if self._queue_drain_next_publish_ms is None:
            return True
        return time.ticks_diff(now_ms, self._queue_drain_next_publish_ms) >= 0

    def _advance_post_outage_queue_publish_deadline(self, now_ms):
        """Consume a drain slot: the next attempt may begin no earlier than
        one interval from now (integer math; a failed attempt consumes it)."""
        if not self._queue_drain_active or self._drain_rate_per_sec == 0:
            return
        interval_ms = max(1, (1000 + self._drain_rate_per_sec - 1) // self._drain_rate_per_sec)
        self._queue_drain_next_publish_ms = time.ticks_add(now_ms, interval_ms)

    def _record_queue_drain_success(self):
        """Count a completed entry; end the episode when the queue is empty
        (get_depth() includes the in-flight entry, so 0 means truly done)."""
        if not self._queue_drain_active:
            return
        self._queue_drain_message_count += 1
        if self._intercore.outbound_queue.get_depth() == 0:
            self._complete_post_outage_queue_drain()

    def _complete_post_outage_queue_drain(self):
        """Freeze the episode metrics. Integer-only, ticks-safe arithmetic;
        max(1, ...) guards a zero-duration sample in the rate division."""
        now_ms = time.ticks_ms()
        duration_ms = max(1, time.ticks_diff(now_ms, self._queue_drain_started_ms))
        message_count = self._queue_drain_message_count
        self._last_queue_drain_start_depth = self._queue_drain_start_depth
        self._last_queue_drain_message_count = message_count
        self._last_queue_drain_duration_ms = duration_ms
        self._last_queue_drain_rate_per_sec = (message_count * 1000) // duration_ms
        self._queue_drain_active = False
        self._queue_drain_started_ms = None
        self._queue_drain_start_depth = 0
        self._queue_drain_message_count = 0
        self._queue_drain_next_publish_ms = None

    def _recover_network_if_needed(self):
        if self._wifi.is_connected() and self._mqtt.is_connected():
            return

        # An active drain episode ends here: the connection failed mid-drain,
        # so the unfinished episode is cancelled (last completed metrics
        # preserved) and a fresh episode starts at the then-current depth
        # once this reconnect completes.
        self._cancel_post_outage_queue_drain()

        # Report the outage immediately so the snapshot (and Core 1 health
        # gating) reflects the loss; the flag is restored only after the
        # link is re-established.
        self._network_stack_ready = False
        if not self._wifi.is_connected():
            # Wi-Fi loss implies MQTT loss; drop the stale session state.
            self._mqtt.mark_disconnected()
            # Record the firmware-level reason this reconnection sequence is
            # being driven (the only value Core 0 knows on this path).
            self._wifi.note_reconnect_trigger("wifi_disconnected")
        self._publish_network_snapshot(force=True)
        # Runtime recovery path: the events on this pass are reconnect
        # completions (recovery events), not initial connections.
        self.establish_network(reconnect=True)
        self._network_stack_ready = True
        # Runtime reconnect completed: if Core 1 buffered work during the
        # outage, start the post-outage drain episode (empty queue -> none).
        self._start_post_outage_queue_drain()
        # establish_network() armed the connection LED; recovery is complete.
        self._led_manager.set_connecting(False)
        self._publish_network_snapshot(force=True)

    def _watch_core_1_heartbeat(self):
        """Watch Core 1's liveness heartbeat and reset the MCU when stale.

        Core 1 is the heartbeat producer; Core 0 is its independent consumer.
        A dead or wedged Core 1 cannot report its own death -- it no longer
        builds the health message that would carry core_1_inactive -- so Core
        0 must detect the silence and recover the whole board.

        Before the first stamp exists (Core 1 has not started yet) the check
        is a no-op, so the unbounded startup connect loops are unaffected.
        Once a stamp exists, age beyond _CORE_1_HEARTBEAT_STALE_TIMEOUT_MS
        means the thread stopped refreshing and the only recovery is a reset.
        """
        last_activity_ms = self._intercore.state_mailboxes.get_core_1_activity_ms()
        if last_activity_ms is None:
            return
        age_ms = time.ticks_diff(time.ticks_ms(), last_activity_ms)
        if age_ms >= _CORE_1_HEARTBEAT_STALE_TIMEOUT_MS:
            print("[FATAL] Core 1 heartbeat stale ({} ms) - resetting".format(age_ms))
            # Last diagnostic chance before the board is reset: one bounded
            # QoS 1 log event (the existing deadline-limited PUBACK machinery),
            # only while MQTT is connected. A MemoryError still propagates
            # (fail-fast); any other failure simply proceeds to the reset --
            # the watchdog must never be blocked by the publish.
            if self._mqtt.is_connected():
                try:
                    log_message = {
                        "message_type": "log",
                        "uptime_ms": self._uptime_ms(),
                        "timestamp": self._current_utc_timestamp(),
                        "payload": build_event_payload(
                            LEVEL_ERROR,
                            EVENT_RUNTIME_CORE1_STALLED,
                            REASON_CORE1_HEARTBEAT_TIMEOUT,
                            "Core 1 heartbeat stale; resetting",
                            {"age_ms": age_ms},
                        ),
                    }
                    entry = {
                        "payload_bytes": serialize_and_validate_message(log_message),
                        "kind": KIND_LOG,
                    }
                    self._publish_entry(entry)
                except MemoryError:
                    raise
                except Exception as err:
                    if DEBUG:
                        print("[DEBUG] Core 1 stall log publish failed: {}".format(err))
            machine.reset()

    def start(self, config_recovery_event=None):
        """Establish Core 0 network services before Core 1 is started.

        config_recovery_event is the boot-recovery info from main.py when a
        valid backup was promoted over an invalid primary (None in the normal
        case). When set, a configuration_recovered connection log is queued
        after the network is up so it publishes for real.

        This method performs the complete deterministic startup contract:
        1. Establish Wi-Fi
        2. Establish MQTT + subscriptions
        3. Run QoS 1 network probe #1
        4. Drain startup MQTT work
        5. Wait 5 seconds
        6. Run QoS 1 network probe #2
        7. Acquire UTC
        8. Publish initial UTC snapshot
        9. Mark the network stack ready
        10. Publish initial network snapshot
        11. Stop connection LED

        The connection steps (1-2) are unbounded by design. The verification
        steps (3-7) are self-healing: a transient failure (a dropped PUBACK, a
        brief UTC-server outage) drops the session, re-establishes the network,
        and retries the whole verification pass instead of halting the device.
        A MemoryError still propagates (fail-fast), so an out-of-memory device
        is not looped.

        Returns only when a complete, clean pass of the contract has succeeded;
        Core 1 stays gated until then.
        """
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
        # _verify_startup_contract and out of this loop (fail-fast on OOM).
        while True:
            if self._verify_startup_contract():
                break
            delay_sec = self._config["mqtt_reconnect_delays_sec"][-1]
            print("[WARNING] Startup verification failed; re-establishing network and retrying in {} sec".format(delay_sec))
            time.sleep(delay_sec)
            self._mqtt.mark_disconnected()
            # Startup self-heal re-establishment: record the firmware-level
            # trigger for this reconnection sequence.
            self._wifi.note_reconnect_trigger("network_probe_failure")
            self.establish_network()

        # The verification pass completed, including a clean UTC sync.
        self._queue_connection_log(
            LEVEL_INFO,
            EVENT_UTC_SYNC_COMPLETED,
            REASON_NONE,
            "UTC synchronized during startup",
        )

        # Step 8: Publish initial UTC snapshot
        self._publish_utc_snapshot()

        # Step 9: Network startup proven complete - set ready flag
        self._network_stack_ready = True

        # Step 10: Publish initial network snapshot with ready flag
        self._publish_network_snapshot(force=True)

        # Step 11: Stop connection LED
        self._led_manager.set_connecting(False)

        print("[INFO] Core 0 startup complete - network stack verified and ready")

        # Boot recovery notice: if main.py promoted a valid backup over an
        # invalid primary, surface it as a connection log now that the
        # network is up (so it publishes for real). Observational only.
        if config_recovery_event is not None:
            self._queue_connection_log(
                LEVEL_INFO,
                EVENT_CONFIGURATION_RECOVERED,
                REASON_CONFIG_PRIMARY_INVALID,
                "Configuration recovered from backup at startup",
                {
                    "reason": config_recovery_event.get("reason",
                        REASON_CONFIG_PRIMARY_INVALID),
                },
            )

    def _verify_startup_contract(self):
        """Run one full pass of the startup verification contract.

        Contract, in order: QoS 1 network probe #1, drain startup MQTT work,
        a 5 s stabilization wait, QoS 1 network probe #2, then UTC
        synchronization. Returns True only when every step succeeds; returns
        False on any probe or UTC failure so the caller can re-establish the
        network and retry. A MemoryError propagates unchanged (fail-fast).
        """
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
            self._queue_connection_log(
                LEVEL_WARNING,
                EVENT_UTC_SYNC_FAILED,
                REASON_TIMEOUT,
                "UTC synchronization pass failed",
            )
            print("[WARNING] Startup verification: UTC synchronization failed")
            return False

        return True

    def _publish_utc_snapshot(self):
        """Publish the current UTC snapshot to the state mailbox."""
        if self._utc_snapshot is None:
            return
        self._intercore.state_mailboxes.set_utc_snapshot(self._utc_snapshot)

    def run(self):
        """Run the Core 0 network/MQTT service loop."""

        while True:
            # First each pass: a dead Core 1 wedges the whole sensor (no
            # telemetry, no health) and cannot report itself, so Core 0
            # resets the board before doing any other work.
            self._watch_core_1_heartbeat()

            # Advance (or drain) a configuration transaction, if one is in
            # flight. Non-blocking: at most one mailbox poll per pass, so a
            # wedged Core 1 can never stall the network loop or its own
            # watchdog.
            self._service_config_transaction()

            if self._pending_reboot is not None:
                self._perform_reboot()

            self._recover_network_if_needed()

            if self._mqtt.is_connected() and self._pending_connection_logs:
                try:
                    self._service_pending_connection_log()
                except MemoryError:
                    raise
                except Exception as err:
                    if DEBUG:
                        print("[DEBUG] Connection log publish failed: {}".format(err))

            # Read per pass so a DYNAMIC mqtt_command_poll_sec change is live.
            poll_ms = self._config["mqtt_command_poll_ms"]
            now_ms = time.ticks_ms()
            if time.ticks_diff(now_ms, self._last_command_poll_ms) >= poll_ms:
                try:
                    self._mqtt.check_msg()
                except MemoryError:
                    raise
                except Exception as err:
                    if DEBUG:
                        print("[DEBUG] MQTT check failed: {}".format(err))
                self._last_command_poll_ms = now_ms

            if self._pending_reboot is not None:
                self._perform_reboot()

            if (
                self._mqtt.is_connected()
                and self._pending_core0_responses
                and not self._intercore.outbound_queue.has_in_flight()
            ):
                try:
                    self._service_pending_core0_response()
                except MemoryError:
                    raise
                except Exception as err:
                    if DEBUG:
                        print("[DEBUG] Core 0 response publish failed: {}".format(err))

            if self._mqtt.is_connected():
                # One outbound queue entry per pass, published with QoS 1.
                # During an active post-outage drain with a positive rate,
                # the drain gate may defer ONLY the take() step (non-blocking);
                # when the slot is not yet due the entry stays queued and the
                # PINGREQ branch below stays eligible, so a pending drain slot
                # never delays a keepalive. A slot is consumed when the
                # attempt begins, so a failed attempt consumes it too.
                entry = None
                if self._post_outage_queue_publish_allowed(time.ticks_ms()):
                    entry = self._intercore.outbound_queue.take()
                if entry is not None:
                    self._advance_post_outage_queue_publish_deadline(time.ticks_ms())
                    try:
                        self._publish_entry(entry)
                    except MemoryError:
                        raise
                    except Exception as err:
                        # Keep the entry in flight so the next take() retries it:
                        # QoS 1 must not drop a message the broker has not PUBACKed.
                        if DEBUG:
                            print("[DEBUG] MQTT publish failed; entry remains in flight: {}".format(err))
                    else:
                        self._intercore.outbound_queue.complete_in_flight(entry)
                        self._record_queue_drain_success()
                elif self._mqtt.ping_due():
                    # No publish to send (or the drain slot is not yet due):
                    # PINGREQ keeps the broker from disconnecting us at
                    # 1.5 x keepalive.
                    try:
                        self._mqtt.ping()
                    except MemoryError:
                        raise
                    except Exception as err:
                        if DEBUG:
                            print("[DEBUG] MQTT PINGREQ failed: {}".format(err))

            # Network diagnostics: advance at most one bounded probe stage
            # (lower priority than normal operations; results land in the
            # snapshot this same pass publishes). Diagnostics must observe
            # without destabilizing: an unexpected ordinary failure discards
            # the partial cycle and restarts it next interval.
            try:
                self._service_network_diagnostics()
            except MemoryError:
                raise
            except Exception as err:
                if DEBUG:
                    print("[DEBUG] Network diagnostics failed: {}".format(err))
                self._reset_netdiag_cycle(time.ticks_ms())

            self._publish_network_snapshot()

            self._utc_request_expired()
            if self._mqtt.is_connected() and self._utc_should_send_request():
                self._utc_send_request()

            time.sleep_ms(10)
