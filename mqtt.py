# mqtt.py - Core 0 exclusive MQTT owner
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import machine
import time

from debug import DEBUG
from mqtt_client import MQTTClient, MQTTPubackTimeout

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

        # Runtime-lifetime MQTT reliability metrics (all reset naturally on
        # reboot). The Mqtt object is the single source of truth: no counter is
        # duplicated in Core 0/Core 1/SystemInformation/queue/socket, and no
        # per-attempt or outage history is retained (only scalars plus two
        # active timing stamps).
        # Integer counters.
        self._publish_attempt_count = 0
        self._publish_retry_count = 0
        self._puback_timeout_count = 0
        self._connection_failure_count = 0
        self._reconnect_success_count = 0
        # Active timing stamps (None when no outage/reconnect is in progress);
        # the finished durations, zero until the first such event completes.
        self._outage_started_ms = None
        self._reconnect_started_ms = None
        self._last_reconnect_duration_ms = 0
        self._last_outage_duration_ms = 0

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
        # Dispose of the old client by closing its TCP socket directly. This
        # path only runs when the session is not connected (connect() is only
        # entered while not connected), so the client is always failed or
        # never established and a graceful MQTT DISCONNECT frame is not
        # warranted. Writing one into a socket whose link has already failed
        # is exactly what must not happen: after a bounded exchange fails,
        # its finally has restored the socket to infinite-blocking mode, and
        # a DISCONNECT write on a blackholed link would wedge Core 0 inside
        # sock.write() with no timeout -- and with Core 0 wedged, its Core 1
        # heartbeat watchdog could never run either. A direct socket close
        # performs no write at all.
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
        """Connect and subscribe, with the whole handshake time-bounded.

        The CONNACK wait and both SUBACK waits run under
        mqtt_broker_response_timeout_sec: MQTTClient.connect(timeout=...)
        installs the finite socket timeout, and subscribe() relies on the
        timeout connect() leaves in place. A broker that accepts the TCP
        connection and then stops responding therefore fails the attempt
        (retried with the reconnect backoff) instead of wedging Core 0.
        On success the socket returns to normal blocking mode; every later
        operation (PUBACK, PINGRESP) installs and restores its own timeout.
        """
        # Reconnect start: once this runtime has connected at least once, an
        # entry to connect() is a reconnect. The timer starts on the first
        # attempt after the session was lost and is NOT restarted by each
        # subsequent attempt, nor reset if Core 0 exhausts one connect() and
        # calls it again for the same outage (the existing timestamp is kept).
        if self._connect_count > 0 and self._reconnect_started_ms is None:
            self._reconnect_started_ms = time.ticks_ms()
            # Defensive fallback: a reconnect that somehow reached here without
            # a prior mark_disconnected() still gets a valid outage span,
            # anchored to the reconnect start.
            if self._outage_started_ms is None:
                self._outage_started_ms = self._reconnect_started_ms
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
                # Handshake complete: restore normal blocking mode.
                try:
                    self._client.sock.settimeout(None)
                except MemoryError:
                    raise
                except Exception as err:
                    if DEBUG:
                        print("[DEBUG] MQTT blocking-mode restore failed: {}".format(err))
                self._connected = True
                self._connect_count += 1
                self._touch()
                # A success after a prior successful connection is a reconnect.
                # Record the just-completed reconnect and outage spans (both use
                # ticks_diff, safe across the tick-counter wrap) and clear the
                # active stamps. The initial connection (connect_count still 1)
                # skips this: it leaves the reconnect count and both durations
                # at zero.
                if self._connect_count > 1:
                    now_ms = time.ticks_ms()
                    self._reconnect_success_count += 1
                    if self._reconnect_started_ms is not None:
                        self._last_reconnect_duration_ms = time.ticks_diff(
                            now_ms, self._reconnect_started_ms
                        )
                    if self._outage_started_ms is not None:
                        self._last_outage_duration_ms = time.ticks_diff(
                            now_ms, self._outage_started_ms
                        )
                    self._reconnect_started_ms = None
                    self._outage_started_ms = None
                print("[INFO] MQTT connected: {}".format(self._broker))
                return True
            except MemoryError:
                raise
            except Exception as err:
                # Every failed connection/handshake/subscription attempt counts
                # exactly once; MemoryError (handled above) does not. The final
                # exhausted return False below is not an attempt, so it is not
                # double-counted.
                self._connection_failure_count += 1
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
            # A known connected->disconnected transition starts the outage
            # timer. It runs only when a connection had previously succeeded
            # (this runtime was actually up) and no timer is already active, so
            # the several recovery paths that can mark the SAME outage (a
            # publish/ping/check failure, then a Wi-Fi loss, ...) count it
            # exactly once and never restart the clock mid-outage.
            if self._connect_count > 0 and self._outage_started_ms is None:
                self._outage_started_ms = time.ticks_ms()
            return
        self._connected = False

    def update_connection_config(self, values):
        """Apply the MQTT RECONFIGURE key set (values already patch-validated).

        Updates only the connection parameters the next connect() will use:
        broker address, keepalive, the two topics connect() subscribes to,
        and the bounded broker-response timeout (read per call, so it is live
        immediately). Takes effect only when combined with the existing
        reconfigure operation: mark_disconnected() -> this -> connect(),
        which runs the bounded handshake and both subscriptions.
        """
        for key, value in values.items():
            if key == "mqtt_broker_ip_address":
                self._broker = value
            elif key == "mqtt_keepalive_sec":
                self._keepalive = value
            elif key == "mqtt_broker_response_timeout_sec":
                self._ack_timeout_ms = value * 1000
            elif key == "mqtt_topic_command":
                self._command_topic = value
            elif key == "mqtt_topic_info_response":
                self._info_response_topic = value
            elif key == "mqtt_reconnect_delays_sec":
                self._reconnect_delays = value
            else:
                raise ValueError("unknown MQTT connection key: {}".format(key))

    def mqtt_config_snapshot(self):
        """The connection parameters, for the coordinator's restore-on-failure."""
        return {
            "mqtt_broker_ip_address": self._broker,
            "mqtt_keepalive_sec": self._keepalive,
            "mqtt_broker_response_timeout_sec": self._ack_timeout_ms // 1000,
            "mqtt_topic_command": self._command_topic,
            "mqtt_topic_info_response": self._info_response_topic,
            "mqtt_reconnect_delays_sec": list(self._reconnect_delays),
        }

    def check_msg(self):
        """Poll for one pending inbound packet and deliver it to the callback.

        The parse of a ready packet runs under the broker response timeout (a
        finite bound), so a link that stalls after the first frame byte fails
        this poll instead of hanging the run loop or short-reading a corrupt
        frame.
        """
        if not self.is_connected():
            return
        try:
            self._client.check_msg(self._ack_timeout_ms / 1000.0)
        except MemoryError:
            raise
        except Exception:
            self.mark_disconnected()
            raise

    def publish_qos1(self, topic, message, is_retry=False):
        """Publish one application message and wait for its matching PUBACK.

        The PUBACK wait is bounded by mqtt_broker_response_timeout_sec so a
        blackholed link fails fast and the run loop's network recovery can
        fire, instead of blocking here forever.

        ``is_retry`` is the Core 0 logical-message classification (the Core 0
        owner knows whether the same logical message was attempted before); it
        is never inferred here from a packet id, topic, or payload.
        """
        if not self.is_connected():
            raise OSError("MQTT is not connected")
        # The attempt begins here, immediately before the low-level publish:
        # a message that is merely serialized, queued, gated, or rejected has
        # not been attempted, while every actual low-level invocation is.
        self._publish_attempt_count += 1
        if is_retry:
            self._publish_retry_count += 1
        try:
            self._client.publish(
                topic, message, qos=1, timeout_ms=self._ack_timeout_ms
            )
        except MemoryError:
            raise
        except MQTTPubackTimeout:
            # The PUBLISH frame went out and the PUBACK wait expired: count it
            # precisely (not approximated from a generic publish failure) and
            # mark the disconnect for recovery.
            self._puback_timeout_count += 1
            self.mark_disconnected()
            raise
        except Exception:
            self.mark_disconnected()
            raise
        self._touch()

    def publish_qos1_with_packet_id(
        self, topic, message, packet_id, timeout_ms=None, is_retry=False
    ):
        """Publish one QoS 1 message with a specific packet ID and wait for matching PUBACK.

        Args:
            topic: MQTT topic
            message: Message body
            packet_id: Specific packet ID to use
            timeout_ms: Optional timeout in milliseconds
            is_retry: Core 0 logical-message retry classification (see
                publish_qos1).

        Returns True if PUBACK received with matching ID, False on timeout/error.
        """
        if not self.is_connected():
            raise OSError("MQTT is not connected")
        # Count the actual low-level publish invocation, as in publish_qos1.
        self._publish_attempt_count += 1
        if is_retry:
            self._publish_retry_count += 1

        try:
            # Pass timeout to mqtt_client's publish method
            self._client.publish(topic, message, qos=1, packet_id=packet_id, timeout_ms=timeout_ms)
            self._touch()
            return True
        except MemoryError:
            raise
        except MQTTPubackTimeout:
            # Count the PUBACK timeout precisely before converting it into this
            # method's existing False return (callers use the return value).
            self._puback_timeout_count += 1
            if DEBUG:
                print("[DEBUG] QoS 1 publish with packet_id {} PUBACK timed out".format(packet_id))
            self.mark_disconnected()
            return False
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
        """Advance and return the next packet ID to use for a QoS 1 message.

        Delegates to the client's single increment helper so the 1..65535 wrap
        is defined in exactly one place. Advancing (not peeking) means the ID
        is consumed, so the next auto-increment cannot reuse it.
        """
        if self._client is None:
            return 1
        return self._client.next_packet_id()

    def status(self):
        """Authoritative read interface for connection state and reliability metrics.

        The internal names stay short; Core 0 copies them into the shared
        network snapshot under the canonical ``mqtt_*`` external names.
        """
        return {
            "connected": self.is_connected(),
            "connect_count": self._connect_count,
            "disconnect_count": self._disconnect_count,
            "publish_attempt_count": self._publish_attempt_count,
            "publish_retry_count": self._publish_retry_count,
            "puback_timeout_count": self._puback_timeout_count,
            "connection_failure_count": self._connection_failure_count,
            "reconnect_success_count": self._reconnect_success_count,
            "last_reconnect_duration_ms": self._last_reconnect_duration_ms,
            "last_outage_duration_ms": self._last_outage_duration_ms,
        }
