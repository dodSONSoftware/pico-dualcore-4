# mqtt.py - Core 0 exclusive MQTT owner
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import machine
import time

from debug import DEBUG
from mqtt_client import MQTTClient, MQTTException

# Bounded wait for PINGRESP so a dead link surfaces quickly instead of
# blocking the Core 0 run loop for the full keepalive window.
_MAX_PINGRESP_WAIT_SEC = 10

# Expected failure classes at the MQTT boundary: transport (OSError) and
# wire-protocol (MQTTException) failures are link conditions — mark the
# session down and let Core 0's recovery reconnect. Anything else escaping
# the client is a programming failure: it must reach main.py's controlled
# reset instead of being retried into the same fault and hidden.
MQTT_TRANSPORT_ERRORS = (OSError, MQTTException)


class Mqtt:
    """Small MQTT lifecycle based on the original working client."""

    def __init__(self, config, message_callback, wait_service=None):
        self._broker = config["mqtt_broker_ip_address"]
        self._command_topic = config["mqtt_topic_command"]
        self._info_response_topic = config["mqtt_topic_info_response"]
        self._keepalive = config["mqtt_keepalive_sec"]
        # Bounded wait for PUBACK: a blackholed link surfaces as a publish
        # failure (recovery) instead of blocking the Core 0 run loop.
        self._ack_timeout_ms = config["mqtt_broker_response_timeout_sec"] * 1000
        self._reconnect_delays = config["mqtt_reconnect_delays_sec"]
        self._message_callback = message_callback
        # Optional Core 0 servicing hook (the Core 1 heartbeat watchdog),
        # invoked at each 100 ms slice of the retry backoffs.
        self._wait_service = wait_service
        self._client = None
        self._connected = False
        self._connect_count = 0
        self._disconnect_count = 0
        # The final expected transport/protocol error from the most recent
        # failed connect() attempt-sequence (None on success / before any
        # attempt); cleared on success, used only to name the cause in
        # Core 0's exhaustion warning.
        self._last_connect_error = None
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

    def _service_wait(self):
        if self._wait_service is not None:
            self._wait_service()

    def _sleep_interruptible(self, delay_sec):
        if delay_sec <= 0:
            return

        for _ in range(int(delay_sec * 10)):
            self._service_wait()
            time.sleep_ms(100)

    def _new_client(self):
        client = MQTTClient(
            self._client_id,
            self._broker,
            keepalive=self._keepalive,
        )
        client.set_callback(self._message_callback)
        return client

    def _close_old_client(self):
        # Close the old client's TCP socket directly. This path only runs
        # while not connected, so the client is failed or never established
        # and a graceful DISCONNECT frame is not warranted — a DISCONNECT
        # write on a blackholed link (the socket is back in infinite-blocking
        # mode after a failed bounded exchange) would wedge Core 0 inside
        # sock.write() with no timeout, and the Core 1 watchdog could never
        # run either. A direct close performs no write at all.
        client = self._client
        self._client = None

        if client is None or client.sock is None:
            return

        try:
            client.sock.close()
        except MemoryError:
            raise
        except Exception as err:
            if DEBUG:
                print("[DEBUG] MQTT socket cleanup failed: {}".format(err))

    def connect(self):
        """Connect and subscribe, handshake time-bounded: the CONNACK/SUBACK
        waits run under mqtt_broker_response_timeout_sec, so an unresponsive
        broker fails the attempt (retried with backoff) instead of wedging
        Core 0. On success the socket returns to normal blocking mode. On a
        fully failed sequence the last attempt's cause is retained on
        self.last_connect_error; a success clears it."""
        self._last_connect_error = None
        for attempt_index, delay_sec in enumerate(self._reconnect_delays):
            try:
                self._close_old_client()
                self._client = self._new_client()
                if DEBUG:
                    print("[DEBUG] MQTT attempt {} to {}".format(
                        attempt_index + 1, self._broker
                    ))
                # Bound the handshake: the finite timeout installed here also
                # carries across the two SUBACK waits below.
                self._client.connect(timeout=self._ack_timeout_ms / 1000.0)
                self._client.subscribe(self._command_topic, qos=1)
                self._client.subscribe(self._info_response_topic, qos=1)
                # Handshake complete: restore normal blocking mode. Not
                # best-effort — the later bounded waits assume it, so a
                # failed restoration fails this attempt instead of marking a
                # broken link healthy.
                self._client.sock.settimeout(None)
                self._connected = True
                self._connect_count += 1
                self._touch()
                print("[INFO] MQTT connected: {}".format(self._broker))
                return True
            except MemoryError:
                raise
            except MQTT_TRANSPORT_ERRORS as err:
                # Only a transport/protocol failure is a failed attempt
                # (retried with backoff); a programming failure escapes to
                # main.py's boundary instead of retrying the same fault.
                if self._connected:
                    self._disconnect_count += 1
                self._connected = False
                # Retain the final cause so the caller's exhaustion warning can
                # name it (ECONNREFUSED, ETIMEDOUT, CONNACK/SUBACK failure, ...).
                self._last_connect_error = err
                if DEBUG:
                    print("[DEBUG] MQTT attempt failed: {}".format(err))
                if attempt_index < len(self._reconnect_delays) - 1:
                    if DEBUG:
                        print("[DEBUG] MQTT retry in {} sec".format(delay_sec))
                    self._sleep_interruptible(delay_sec)
        return False

    def mark_disconnected(self):
        if self._connected:
            self._disconnect_count += 1
        self._connected = False

    def check_msg(self):
        """Poll for one pending inbound packet and deliver it to the callback;
        the parse runs under the broker response timeout, so a stall after the
        first frame byte fails this poll instead of hanging. A callback bug is
        a programming failure (propagates without marking the session down);
        a stalled/corrupt stream is a transport failure that fails the poll."""
        if not self.is_connected():
            return
        try:
            self._client.check_msg(self._ack_timeout_ms / 1000.0)
        except MemoryError:
            raise
        except MQTT_TRANSPORT_ERRORS:
            self.mark_disconnected()
            raise

    def publish_qos1(self, topic, message):
        """Publish one application message and wait for its PUBACK (bounded by
        mqtt_broker_response_timeout_sec, so a blackholed link fails fast)."""
        if not self.is_connected():
            raise OSError("MQTT is not connected")
        try:
            self._client.publish(
                topic, message, qos=1, timeout_ms=self._ack_timeout_ms
            )
        except MemoryError:
            raise
        except MQTT_TRANSPORT_ERRORS:
            self.mark_disconnected()
            raise
        self._touch()

    def publish_qos1_with_packet_id(self, topic, message, packet_id, timeout_ms=None):
        """Publish one QoS 1 message with a specific packet ID; True if PUBACK received with matching ID, False on timeout/error."""
        if not self.is_connected():
            raise OSError("MQTT is not connected")

        try:
            self._client.publish(topic, message, qos=1, packet_id=packet_id, timeout_ms=timeout_ms)
            self._touch()
            return True
        except MemoryError:
            raise
        except MQTT_TRANSPORT_ERRORS as err:
            if DEBUG:
                print("[DEBUG] QoS 1 publish with packet_id {} failed: {}".format(packet_id, err))
            self.mark_disconnected()
            return False

    def _ping_interval_sec(self):
        """Time between keepalive traffic and the mandatory PINGREQ (keepalive / 2, leaving a full interval of jitter margin)."""
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
        except MQTT_TRANSPORT_ERRORS:
            self.mark_disconnected()
            raise
        self._touch()

    def get_next_packet_id(self):
        """Advance and return the next QoS 1 packet ID via the client's single
        increment helper (1..65535 wrap defined in one place); the ID is
        consumed and cannot be reused."""
        if self._client is None:
            return 1
        return self._client.next_packet_id()

    @property
    def last_connect_error(self):
        """The final expected transport/protocol error from the most recent
        failed connect() attempt-sequence, or None (before any attempt, or
        after a success) — exposed so Core 0's exhaustion warning can name
        the cause instead of a generic 'sequence exhausted'."""
        return self._last_connect_error

    def status(self):
        return {
            "connected": self.is_connected(),
            "connect_count": self._connect_count,
            "disconnect_count": self._disconnect_count,
        }
