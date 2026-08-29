# mqtt.py - Core 0 exclusive MQTT owner
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import machine
import time

from debug import DEBUG
from mqtt_client import MQTTClient

# Bounded wait for PINGRESP so a dead link surfaces quickly instead of
# blocking the Core 0 run loop for the full keepalive window.
_MAX_PINGRESP_WAIT_SEC = 10


class Mqtt:
    """Small MQTT lifecycle based on the original working client."""

    def __init__(self, config, message_callback):
        self._broker = config["mqtt_broker_ip_address"]
        self._command_topic = config["mqtt_topic_command"]
        self._info_response_topic = config["mqtt_topic_info_response"]
        self._keepalive = config["mqtt_keepalive_sec"]
        # Bounded wait for PUBACK so a blackholed link surfaces as a
        # publish failure (and triggers network recovery) instead of
        # blocking the Core 0 run loop indefinitely.
        self._ack_timeout_ms = config["mqtt_broker_response_timeout_sec"] * 1000
        self._reconnect_delays = config["mqtt_reconnect_delays_sec"]
        self._message_callback = message_callback
        self._client = None
        self._connected = False
        self._connect_count = 0
        self._disconnect_count = 0
        self._last_activity_ms = time.ticks_ms()

        try:
            uid = machine.unique_id()
            suffix = "".join("{:02x}".format(value) for value in uid[-4:])
        except MemoryError:
            raise
        except Exception:
            suffix = "{:08x}".format(time.ticks_ms() & 0xFFFFFFFF)
        self._client_id = "pico_{}".format(suffix)

    def is_connected(self):
        return self._connected and self._client is not None

    def _touch(self):
        """Record outbound MQTT activity (any sent packet resets the keepalive)."""
        self._last_activity_ms = time.ticks_ms()

    def _new_client(self):
        client = MQTTClient(
            self._client_id,
            self._broker,
            keepalive=self._keepalive,
        )
        client.set_callback(self._message_callback)
        return client

    def _close_old_client(self):
        if self._client is None:
            return
        try:
            self._client.disconnect()
        except MemoryError:
            raise
        except Exception as err:
            if DEBUG:
                print("[DEBUG] MQTT disconnect cleanup failed: {}".format(err))
            try:
                if self._client.sock is not None:
                    self._client.sock.close()
            except MemoryError:
                raise
            except Exception as close_err:
                if DEBUG:
                    print("[DEBUG] MQTT socket close cleanup failed: {}".format(close_err))
        self._client = None

    def connect(self):
        for attempt_index, delay_sec in enumerate(self._reconnect_delays):
            try:
                self._close_old_client()
                self._client = self._new_client()
                if DEBUG:
                    print("[DEBUG] MQTT attempt {} to {}".format(
                        attempt_index + 1, self._broker
                    ))
                self._client.connect()
                self._client.subscribe(self._command_topic, qos=1)
                self._client.subscribe(self._info_response_topic, qos=1)
                self._connected = True
                self._connect_count += 1
                self._touch()
                print("[INFO] MQTT connected: {}".format(self._broker))
                return True
            except MemoryError:
                raise
            except Exception as err:
                if self._connected:
                    self._disconnect_count += 1
                self._connected = False
                if DEBUG:
                    print("[DEBUG] MQTT attempt failed: {}".format(err))
                if attempt_index < len(self._reconnect_delays) - 1:
                    if DEBUG:
                        print("[DEBUG] MQTT retry in {} sec".format(delay_sec))
                    time.sleep(delay_sec)
        return False

    def mark_disconnected(self):
        if self._connected:
            self._disconnect_count += 1
        self._connected = False

    def check_msg(self):
        if not self.is_connected():
            return
        try:
            self._client.check_msg()
        except MemoryError:
            raise
        except Exception:
            self.mark_disconnected()
            raise

    def publish_qos1(self, topic, message):
        """Publish one application message and wait for its matching PUBACK.

        The PUBACK wait is bounded by mqtt_broker_response_timeout_sec so a
        blackholed link fails fast and the run loop's network recovery can
        fire, instead of blocking here forever.
        """
        if not self.is_connected():
            raise OSError("MQTT is not connected")
        try:
            self._client.publish(
                topic, message, qos=1, timeout_ms=self._ack_timeout_ms
            )
        except MemoryError:
            raise
        except Exception:
            self.mark_disconnected()
            raise
        self._touch()

    def publish_qos1_with_packet_id(self, topic, message, packet_id, timeout_ms=None):
        """Publish one QoS 1 message with a specific packet ID and wait for matching PUBACK.

        Args:
            topic: MQTT topic
            message: Message body
            packet_id: Specific packet ID to use
            timeout_ms: Optional timeout in milliseconds

        Returns True if PUBACK received with matching ID, False on timeout/error.
        """
        if not self.is_connected():
            raise OSError("MQTT is not connected")

        try:
            # Pass timeout to mqtt_client's publish method
            self._client.publish(topic, message, qos=1, packet_id=packet_id, timeout_ms=timeout_ms)
            self._touch()
            return True
        except MemoryError:
            raise
        except Exception as err:
            if DEBUG:
                print("[DEBUG] QoS 1 publish with packet_id {} failed: {}".format(packet_id, err))
            self.mark_disconnected()
            return False

    def _ping_interval_sec(self):
        """Time between keepalive traffic and the mandatory PINGREQ.

        The broker tolerates 1.5 x keepalive, so pinging at keepalive / 2
        leaves a full interval of margin for jitter.
        """
        return max(self._keepalive // 2, 1)

    def ping_due(self):
        """True when keepalive traffic is due and the connection is alive."""
        if not self.is_connected():
            return False
        if self._keepalive <= 0:
            return False
        interval_ms = self._ping_interval_sec() * 1000
        return (
            time.ticks_diff(time.ticks_ms(), self._last_activity_ms) >= interval_ms
        )

    def ping(self):
        """Send PINGREQ and wait for PINGRESP to honor the advertised keepalive."""
        if not self.is_connected():
            raise OSError("MQTT is not connected")
        try:
            self._client.ping(timeout_sec=min(_MAX_PINGRESP_WAIT_SEC, self._ping_interval_sec()))
        except MemoryError:
            raise
        except Exception:
            self.mark_disconnected()
            raise
        self._touch()

    def get_next_packet_id(self):
        """Get the next packet ID to use for QoS 1 messages.

        Uses the client's current PID and increments it.
        """
        if self._client is None:
            return 1
        # Increment and wrap at 65535
        pid = self._client.pid + 1
        if pid > 65535:
            pid = 1
        return pid

    def status(self):
        return {
            "connected": self.is_connected(),
            "connect_count": self._connect_count,
            "disconnect_count": self._disconnect_count,
        }
