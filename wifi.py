# wifi.py - Core 0 exclusive Wi-Fi owner
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import time
import network

from debug import DEBUG


class Wifi:
    """Original-style Wi-Fi lifecycle, owned exclusively by Core 0."""

    def __init__(self, ssid, password, reconnect_delays_sec, wait_service=None):
        self._ssid = ssid
        self._password = password
        self._reconnect_delays_sec = reconnect_delays_sec
        # Optional Core 0 servicing hook (the Core 1 heartbeat watchdog),
        # invoked at each 100 ms wait slice below.
        self._wait_service = wait_service
        self._wlan = None
        self._connect_count = 0
        self._disconnect_count = 0
        self._was_connected = False

    def _service_wait(self):
        if self._wait_service is not None:
            self._wait_service()

    def _sleep_interruptible(self, delay_sec):
        if delay_sec <= 0:
            return

        for _ in range(int(delay_sec * 10)):
            self._service_wait()
            time.sleep_ms(100)

    def _current_status(self):
        """The current WLAN association state, or None if it cannot be read;
        callers treat None as "unknown" (the observation timeout governs),
        never as a failure. Only OSError reads as unknown; a programming
        failure escapes, as at the MQTT boundary, not masked as "no state"."""
        try:
            status = self._wlan.status()
        except MemoryError:
            raise
        except OSError:
            return None
        return status if isinstance(status, int) else None

    def _terminal_failure_statuses(self):
        """The WLAN association states that end a connect attempt, as ints.
        WRONG_PASSWORD, NO_AP_FOUND, CONNECT_FAIL are final for the current
        connect() call. The STAT_* constants live on the network module
        (Pico W: -3, -2, -1) — read them from there; inventing literals is
        how 3 (STAT_GOT_IP) almost became a "failure"."""
        return (
            network.STAT_WRONG_PASSWORD,
            network.STAT_NO_AP_FOUND,
            network.STAT_CONNECT_FAIL,
        )

    def is_connected(self):
        # Same taxonomy as connect(): OSError reads as "not connected"; a
        # programming failure escapes instead of masking itself as False.
        try:
            return self._wlan is not None and self._wlan.isconnected()
        except MemoryError:
            raise
        except OSError:
            return False

    def ip_address(self):
        if not self.is_connected():
            return None
        try:
            return self._wlan.ifconfig()[0]
        except MemoryError:
            raise
        except OSError:
            return None

    def connect(self):
        """Connect using the critical sequence from the original firmware."""
        for attempt_index, delay_sec in enumerate(self._reconnect_delays_sec):
            try:
                self._wlan = network.WLAN(network.WLAN.IF_STA)
                self._wlan.active(True)

                # The PM_NONE probe keeps a deliberately broad catch: an
                # optional power-management feature whose absence a driver
                # reports with no single portable exception type. This is the
                # one broad catch in this module — everything else follows the
                # transport-failure taxonomy.
                try:
                    self._wlan.config(pm=self._wlan.PM_NONE)
                except MemoryError:
                    raise
                except Exception as err:
                    if DEBUG:
                        print("[DEBUG] Wi-Fi PM_NONE unavailable: {}".format(err))

                if DEBUG:
                    print("[DEBUG] Wi-Fi attempt {} to {}".format(
                        attempt_index + 1, self._ssid
                    ))

                self._wlan.connect(self._ssid, self._password)

                terminal_statuses = self._terminal_failure_statuses()
                terminal_status = None
                wait_count = 0
                while not self._wlan.isconnected() and wait_count < 200:
                    # Service Core 0 (its Core 1 watchdog) on every 100 ms
                    # slice of the up-to-20-second observation window.
                    self._service_wait()
                    # A terminal association state (wrong password, missing
                    # AP, connect failure) means the driver will not recover
                    # within this window: stop observing instead of waiting
                    # out the full 20 s (a still-connecting state is not
                    # terminal; the timeout remains the fallback).
                    status = self._current_status()
                    if status is not None and status in terminal_statuses:
                        terminal_status = status
                        break
                    time.sleep_ms(100)
                    wait_count += 1

                if self._wlan.isconnected():
                    if not self._was_connected:
                        self._connect_count += 1
                    self._was_connected = True
                    print("[INFO] Wi-Fi connected: {}".format(self._ssid))
                    return True

                if DEBUG and terminal_status is not None:
                    print("[DEBUG] Wi-Fi attempt {} ended early (WLAN status {})".format(
                        attempt_index + 1, terminal_status
                    ))

            except MemoryError:
                raise
            except OSError as err:
                # Same taxonomy the MQTT boundary follows: a transport
                # failure (OSError) is a link condition to retry, but a
                # programming failure must escape to main.py's controlled
                # reset — a broad catch would hand establish_network()
                # infinite retries into the same deterministic fault.
                if DEBUG:
                    print("[DEBUG] Wi-Fi attempt failed: {}".format(err))

            if self._was_connected:
                self._disconnect_count += 1
                self._was_connected = False

            if attempt_index < len(self._reconnect_delays_sec) - 1:
                if DEBUG:
                    print("[DEBUG] Wi-Fi retry in {} sec".format(delay_sec))
                self._sleep_interruptible(delay_sec)

        return False

    def snapshot(self, mqtt_connected):
        connected = self.is_connected()
        if self._was_connected and not connected:
            self._disconnect_count += 1
            self._was_connected = False
        elif connected and not self._was_connected:
            self._connect_count += 1
            self._was_connected = True

        snapshot = {
            "wifi_connected": connected,
            "mqtt_connected": bool(mqtt_connected),
            "ssid": self._ssid if connected else None,
            "ip_address": None,
            "netmask": None,
            "gateway": None,
            "dns": None,
            "rssi": None,
            "wifi_connect_count": self._connect_count,
            "wifi_disconnect_count": self._disconnect_count,
        }

        if connected:
            try:
                values = self._wlan.ifconfig()
                snapshot["ip_address"] = values[0]
                snapshot["netmask"] = values[1]
                snapshot["gateway"] = values[2]
                snapshot["dns"] = values[3]
            except MemoryError:
                raise
            except OSError as err:
                if DEBUG:
                    print("[DEBUG] Wi-Fi ifconfig snapshot failed: {}".format(err))
            try:
                snapshot["rssi"] = self._wlan.status("rssi")
            except MemoryError:
                raise
            except OSError as err:
                if DEBUG:
                    print("[DEBUG] Wi-Fi RSSI snapshot failed: {}".format(err))

        return snapshot
