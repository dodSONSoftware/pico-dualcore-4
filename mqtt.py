# mqtt.py - Core 0 exclusive MQTT owner
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import machine
import time

from debug import DEBUG
from mqtt_client import MQTTClient


class Mqtt:
    """Small MQTT lifecycle based on the original working client."""

    def __init__(self, config, message_callback):
        self._broker = config["mqtt_broker_ip_address"]
        self._command_topic = config["mqtt_topic_command"]
        self._info_response_topic = config["mqtt_topic_info_response"]
        self._keepalive = config["mqtt_keepalive_sec"]
        self._reconnect_delays = config["mqtt_reconnect_delays_sec"]
        self._message_callback = message_callback
        self._client = None
        self._connected = False
        self._connect_count = 0
        self._disconnect_count = 0

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
        """Publish one application message and wait for its matching PUBACK."""
        if not self.is_connected():
            raise OSError("MQTT is not connected")
        try:
            self._client.publish(topic, message, qos=1)
        except MemoryError:
            raise
        except Exception:
            self.mark_disconnected()
            raise

    def status(self):
        return {
            "connected": self.is_connected(),
            "connect_count": self._connect_count,
            "disconnect_count": self._disconnect_count,
        }
