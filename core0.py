# core0.py - Core 0 exclusive network owner
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import json
import machine
import time

from debug import DEBUG
from intercore import KIND_COMMAND_RESPONSE, KIND_TELEMETRY
from message_protocol import format_utc_epoch_ms
from mqtt import Mqtt
from version import FIRMWARE_VERSION, MESSAGE_SCHEMA_VERSION
from wifi import Wifi


_MAX_PENDING_CORE0_RESPONSES = 4
_MAX_PENDING_CONNECTION_LOGS = 4


class Core0:
    """Own the complete network stack and all MQTT operations."""

    def __init__(self, intercore, config, wifi_config, runtime_id, boot_ticks_ms, led_manager):
        self._intercore = intercore
        self._config = config
        self._runtime_id = runtime_id
        self._boot_ticks_ms = boot_ticks_ms
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
        self._utc_snapshot = None
        self._last_network_snapshot_ms = None
        self._last_command_poll_ms = time.ticks_ms()
        self._next_sequence = 0
        self._network_stack_ready = False

    def _uptime_ms(self):
        return time.ticks_diff(time.ticks_ms(), self._boot_ticks_ms)

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

        entry = {
            "topic": self._config["mqtt_topic_log"],
            "message": self._pending_connection_logs[0],
        }
        self._publish_entry(entry)
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
            self._queue_core0_response({
                "command_id": command_id,
                "command": command,
                "success": False,
                "targeted": targeted,
                "error": {
                    "code": "intercore_event_queue_full",
                    "message": "Core 1 event queue is full",
                },
            })

    def _service_pending_core0_response(self):
        if not self._pending_core0_responses:
            return

        response = self._pending_core0_responses[0]
        self._publish_core0_command_response(
            response["command_id"],
            response["command"],
            response["success"],
            targeted=response.get("targeted", False),
            data=response.get("data"),
            error=response.get("error"),
        )
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

        payload = doc.get("payload")
        if not isinstance(payload, dict):
            return

        timestamp = payload.get("timestamp")
        utc_epoch_ms = payload.get("utc_epoch_ms")
        if not isinstance(timestamp, str) or not timestamp:
            return
        if (
            isinstance(utc_epoch_ms, bool)
            or not isinstance(utc_epoch_ms, int)
            or utc_epoch_ms <= 0
        ):
            return

        try:
            normalized_timestamp = format_utc_epoch_ms(utc_epoch_ms)
        except MemoryError:
            raise
        except Exception as err:
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
        self._pending_utc_request_id = None
        print("[INFO] UTC synchronized: {}".format(normalized_timestamp))

    def _topic_for_kind(self, kind):
        if kind == KIND_TELEMETRY:
            return self._config["mqtt_topic_telemetry"]
        if kind == KIND_COMMAND_RESPONSE:
            return self._config["mqtt_topic_command_response"]
        raise ValueError("Unsupported outbound message kind: {}".format(kind))

    def _make_envelope(self, entry, sequence):
        source = entry["message"]
        envelope = {}
        for key, value in source.items():
            envelope[key] = value

        envelope["sequence"] = sequence
        envelope["runtime_id"] = self._runtime_id
        if "uptime_ms" not in envelope:
            envelope["uptime_ms"] = self._uptime_ms()
        if "timestamp" not in envelope:
            envelope["timestamp"] = self._current_utc_timestamp()
        envelope["firmware_version"] = FIRMWARE_VERSION
        envelope["message_schema_version"] = MESSAGE_SCHEMA_VERSION
        envelope["source"] = self._config["source"]
        return envelope

    def _publish_entry(self, entry):
        sequence = self._next_sequence
        encoded = json.dumps(self._make_envelope(entry, sequence))
        topic = entry.get("topic")
        if topic is None:
            topic = self._topic_for_kind(entry["kind"])
        self._mqtt.publish_qos1(topic, encoded)
        if entry.get("kind") == KIND_TELEMETRY:
            self._led_manager.telemetry_sent()
        self._next_sequence += 1
        if DEBUG:
            print("[DEBUG] QoS 1 published: seq={}".format(sequence))

    def _publish_core0_command_response(
        self, command_id, command, success, targeted=True, data=None, error=None
    ):
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

        entry = {
            "topic": self._config["mqtt_topic_command_response"],
            "message": {
                "message_type": "command_response",
                "payload": payload,
            },
        }
        self._publish_entry(entry)

    def _perform_reboot(self):
        request = self._pending_reboot
        if request is None:
            return True
        if self._intercore.outbound_queue.has_in_flight():
            return False

        try:
            self._publish_core0_command_response(
                request["command_id"],
                request["command"],
                True,
                targeted=request.get("targeted", False),
                data={"rebooting": True},
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
        time.sleep_ms(1000)
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

    def _request_utc(self):
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
            raise
        except Exception as err:
            self._pending_utc_request_id = None
            if DEBUG:
                print("[DEBUG] UTC request publish failed: {}".format(err))
            return

        timeout_ms = self._config["mqtt_broker_response_timeout_sec"] * 1000
        start_ms = time.ticks_ms()
        while self._pending_utc_request_id is not None:
            if time.ticks_diff(time.ticks_ms(), start_ms) >= timeout_ms:
                self._pending_utc_request_id = None
                return
            try:
                self._mqtt.check_msg()
            except MemoryError:
                raise
            except Exception as err:
                self._pending_utc_request_id = None
                if DEBUG:
                    print("[DEBUG] UTC response wait failed: {}".format(err))
                return
            time.sleep_ms(20)

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
        """Synchronize UTC during startup. Must succeed for startup to complete."""
        for attempt in range(self._config["mqtt_broker_response_timeout_sec"]):
            self._request_utc()
            if self._utc_snapshot is not None:
                return True
            # Wait before retry
            time.sleep_ms(500)

        print("[ERROR] UTC synchronization failed during startup")
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
            time.sleep(delay_sec)

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
            time.sleep(delay_sec)

    def _recover_network_if_needed(self):
        if not self._wifi.is_connected():
            self._mqtt.mark_disconnected()
            self._publish_network_snapshot(force=True)
            self.establish_network()
            self._publish_network_snapshot(force=True)
            return

        if not self._mqtt.is_connected():
            self._publish_network_snapshot(force=True)
            self.establish_network()
            self._publish_network_snapshot(force=True)

    def start(self):
        """Establish Core 0 network services before Core 1 is started.

        This method performs the complete deterministic startup contract:
        1. Establish Wi-Fi
        2. Establish MQTT + subscriptions
        3. Run QoS 1 network probe #1
        4. Drain startup MQTT work
        5. Wait 5 seconds
        6. Run QoS 1 network probe #2
        7. Acquire UTC
        8. Publish initial UTC snapshot
        9. Publish initial network snapshot
        10. Stop connection LED

        Returns only when the entire startup contract has succeeded.
        """
        self._led_manager.set_connecting(True)

        # Step 1: Establish Wi-Fi
        self._wifi_connected = False
        while not self._wifi_connected:
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
                self._wifi_connected = True
                break
            delay_sec = self._config["wifi_reconnect_delays_sec"][-1]
            print("[WARNING] Wi-Fi connection sequence exhausted; retrying in {} sec".format(delay_sec))
            time.sleep(delay_sec)

        # Step 2: Establish MQTT + subscriptions
        self._mqtt_connected = False
        while not self._mqtt_connected:
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
                self._mqtt_connected = True
                break
            delay_sec = self._config["mqtt_reconnect_delays_sec"][-1]
            print("[WARNING] MQTT connection sequence exhausted; retrying in {} sec".format(delay_sec))
            time.sleep(delay_sec)

        # Step 3: QoS 1 network probe #1
        if not self._perform_network_probe():
            raise RuntimeError("Network probe #1 failed - MQTT QoS 1 path not verified")

        # Step 4: Drain startup MQTT work
        if not self._drain_startup_mqtt_work():
            print("[WARNING] Startup MQTT work drain did not complete")

        # Step 5: Wait 5 seconds for stabilization
        time.sleep_ms(5000)

        # Step 6: QoS 1 network probe #2
        if not self._perform_network_probe():
            raise RuntimeError("Network probe #2 failed - MQTT QoS 1 path not verified")

        # Step 7: Acquire UTC (mandatory before Core 1 starts)
        if not self._synchronize_utc_required():
            raise RuntimeError("UTC synchronization failed during startup")

        # Step 8: Publish initial UTC snapshot
        self._publish_utc_snapshot(force=True)

        # Step 9: Network startup proven complete - set ready flag
        self._network_stack_ready = True

        # Step 10: Publish initial network snapshot with ready flag
        self._publish_network_snapshot(force=True)

        # Step 11: Stop connection LED
        self._led_manager.set_connecting(False)
        self._led_manager.set_connecting(False)

        print("[INFO] Core 0 startup complete - network stack verified and ready")

    def _publish_utc_snapshot(self, force=False):
        """Publish the current UTC snapshot to the state mailbox."""
        if self._utc_snapshot is None:
            return
        self._intercore.state_mailboxes.set_utc_snapshot(self._utc_snapshot)

    def run(self):
        """Run the Core 0 network/MQTT service loop."""
        self._request_utc()
        poll_ms = self._config["mqtt_command_poll_ms"]

        while True:
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
                entry = self._intercore.outbound_queue.take()
                if entry is not None:
                    try:
                        self._publish_entry(entry)
                        self._intercore.outbound_queue.complete_in_flight(entry)
                    except MemoryError:
                        raise
                    except Exception as err:
                        if DEBUG:
                            print("[DEBUG] MQTT publish failed; in-flight message retained: {}".format(err))

            self._publish_network_snapshot()

            if self._mqtt.is_connected() and self._utc_sync_due():
                self._request_utc()

            time.sleep_ms(10)
