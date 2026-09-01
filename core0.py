# core0.py - Core 0 exclusive network owner
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import json
import machine
import time

from debug import DEBUG
from intercore import KIND_COMMAND_RESPONSE, KIND_HEALTH, KIND_LOG, KIND_TELEMETRY
from message_protocol import format_utc_epoch_ms
from mqtt import Mqtt
from uptime import create_uptime_state, current_uptime_ms
from version import FIRMWARE_VERSION, MESSAGE_SCHEMA_VERSION
from wifi import Wifi

from message_serializer import serialize_and_validate_message


_MAX_PENDING_CORE0_RESPONSES = 4
_MAX_PENDING_CONNECTION_LOGS = 4
_UTC_STARTUP_MAX_ATTEMPTS = 3
_UTC_RETRY_INTERVAL_MS = 30000
_UTC_PROMPT_RETRY_DELAY_MS = 500

# Duplicate-command suppression: the number of recent accepted command IDs
# Core 0 retains. command_id is the idempotency key -- a command is a one-shot
# request for the lifetime of its cache entry, and a repeated command_id is
# ignored (no execution, no event, no response) until it evicts. Fixed count,
# FIFO eviction, RAM-only (cleared on reboot): a tiny amount of bounded
# control metadata, deliberately not heap-governed. The first accepted use of
# an ID owns it; a duplicate receipt does not refresh its position (the cache
# holds the last accepted distinct IDs, not an LRU access order). Static
# constant; not a config key.
_RECENT_COMMAND_ID_CAPACITY = 16

# Core 0 watchdog timeout for the Core 1 liveness heartbeat. Core 1 refreshes
# core_1_activity_ms on a 5-second deadline (core1.py), so a stamp already
# this old means the Core 1 thread is dead or wedged: no legitimate loop pass
# can miss six consecutive refreshes. Deliberately conservative -- far above
# any live-loop processing gap, yet below the 60s core_1_inactive diagnostic
# threshold, so a dead Core 1 resets the board instead of the health stream
# quietly stopping. Static constant; not a config key.
_CORE_1_HEARTBEAT_STALE_TIMEOUT_MS = 30000


class Core0:

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
            self._service_wait,
        )
        self._mqtt = Mqtt(config, self._on_mqtt_message, self._service_wait)

        self._pending_reboot = None
        # Recent accepted command IDs (duplicate-command suppression; see
        # _RECENT_COMMAND_ID_CAPACITY). Oldest first, evicted FIFO.
        self._recent_command_ids = []
        self._pending_core0_responses = []
        self._pending_connection_logs = []
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
        if not isinstance(target, str):
            return False
        if target == "*":
            return True
        # Target matching is case-insensitive: a casing variant of the
        # configured source addresses this device. Only the comparison is
        # normalized; the stored source keeps its configured casing.
        if target.lower() == self._config["source"].lower():
            return True
        return target == self._wifi.ip_address()

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

        if self._is_recent_command_id(command_id):
            # Duplicate suppression: this command_id was already accepted in
            # this runtime. Ignore the copy -- no execution, no Core 1 event,
            # no response, no change to a pending reboot. Expected transport
            # behavior (broker redelivery, sender retry), so DEBUG-only: not
            # a production warning.
            if DEBUG:
                print("[DEBUG] Duplicate command_id ignored: {}".format(command_id))
            return

        self._remember_command_id(command_id)

        if doc.get("message_schema_version") != MESSAGE_SCHEMA_VERSION:
            self._queue_core0_response({
                "command_id": command_id,
                "command": command,
                "success": False,
                "targeted": targeted,
                "error": {
                    "code": "invalid_message_schema_version",
                    "message": "Unsupported message_schema_version",
                },
            })
            return

        if "payload" not in doc:
            self._queue_core0_response({
                "command_id": command_id,
                "command": command,
                "success": False,
                "targeted": targeted,
                "error": {
                    "code": "invalid_payload",
                    "message": "command payload is required",
                },
            })
            return

        payload_obj = doc["payload"]
        if not isinstance(payload_obj, dict):
            self._queue_core0_response({
                "command_id": command_id,
                "command": command,
                "success": False,
                "targeted": targeted,
                "error": {
                    "code": "invalid_payload",
                    "message": "command payload must be an object",
                },
            })
            return

        if command == "reboot":
            if payload_obj:
                self._queue_core0_response({
                    "command_id": command_id,
                    "command": command,
                    "success": False,
                    "targeted": targeted,
                    "error": {
                        "code": "invalid_payload",
                        "message": "reboot payload must be {}",
                    },
                })
                return

            if self._pending_reboot is not None:
                self._queue_core0_response({
                    "command_id": command_id,
                    "command": command,
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
                "command": command,
                "targeted": targeted,
            }
            return

        event = {
            "command_id": command_id,
            "command": command,
            "payload": payload_obj,
            "targeted": targeted,
        }
        if not self._intercore.event_queue.put(event):
            # The event queue is heap-governed and has no count capacity: the
            # only rejection cause is a free-heap reserve that could not be
            # restored, so the error names that cause rather than a "full"
            # queue.
            self._queue_core0_response({
                "command_id": command_id,
                "command": command,
                "success": False,
                "targeted": targeted,
                "error": {
                    "code": "intercore_event_queue_memory_pressure",
                    "message": "Insufficient free heap to queue the Core 1 event",
                },
            })

    def _is_recent_command_id(self, command_id):
        """True if command_id was already accepted in this runtime (exact, case-sensitive)."""
        return command_id in self._recent_command_ids

    def _remember_command_id(self, command_id):
        """Record an accepted command_id; evict the oldest once over capacity (FIFO, not LRU)."""
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
            response["command"],
            response["success"],
            targeted=response.get("targeted", False),
            data=response.get("data"),
            error=response.get("error"),
            wire_sequence=wire_sequence,
            container=response,
        )
        if not published:
            # Permanent serialization failure: the response stays queued
            # (never discarded -- the command was accepted and its
            # acknowledgement is owed) and is retried on a later pass, the
            # same way a failed publish (which raises) leaves it pending.
            return
        self._pending_core0_responses.pop(0)

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

        The payload is never decoded or re-serialized here; the wire sequence is claimed once, before the first transmission attempt."""
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
        encoded = b"".join((body[:-1], b",", self._envelope_fragment(sequence), b"}"))
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

    def _publish_core0_command_response(
        self, command_id, command, success, targeted=True, data=None, error=None,
        wire_sequence=None, container=None
    ):
        """Build and publish a Core 0 command response (pre-serialized).

        ``wire_sequence``/``container`` let a retry keep the claimed identity and the same bytes. Returns True on publish, False on a permanent serialization failure (the caller keeps the message pending); MemoryError propagates."""
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
        self._publish_entry(entry)
        return True

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
        for _ in range(max(int(delay_sec * 10), 1)):
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
        poll_ms = self._config["mqtt_command_poll_ms"]

        while True:
            # First each pass: a dead Core 1 wedges the whole sensor (no
            # telemetry, no health) and cannot report itself, so Core 0
            # resets the board before doing any other work.
            self._watch_core_1_heartbeat()

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
                except Exception as err:
                    if DEBUG:
                        print("[DEBUG] Connection log publish failed: {}".format(err))

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
                except Exception as err:
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
                    except Exception as err:
                        # Keep the entry in flight so the next take() retries it:
                        # QoS 1 must not drop a message the broker has not PUBACKed.
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
                    except Exception as err:
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
