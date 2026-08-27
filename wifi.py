# wifi.py - Core 0 exclusive Wi-Fi owner
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import time
import network

from debug import DEBUG


class Wifi:
    """Original-style Wi-Fi lifecycle, owned exclusively by Core 0."""

    def __init__(self, ssid, password, reconnect_delays_sec):
        self._ssid = ssid
        self._password = password
        self._reconnect_delays_sec = reconnect_delays_sec
        self._wlan = None
        self._connect_count = 0
        self._disconnect_count = 0
        self._was_connected = False

    def is_connected(self):
        try:
            return self._wlan is not None and self._wlan.isconnected()
        except MemoryError:
            raise
        except Exception:
            return False

    def ip_address(self):
        if not self.is_connected():
            return None
        try:
            return self._wlan.ifconfig()[0]
        except MemoryError:
            raise
        except Exception:
            return None

    def connect(self):
        """Connect using the critical sequence from the original firmware."""
        for attempt_index, delay_sec in enumerate(self._reconnect_delays_sec):
            try:
                self._wlan = network.WLAN(network.WLAN.IF_STA)
                self._wlan.active(True)

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

                wait_count = 0
                while not self._wlan.isconnected() and wait_count < 200:
                    time.sleep_ms(100)
                    wait_count += 1

                if self._wlan.isconnected():
                    if not self._was_connected:
                        self._connect_count += 1
                    self._was_connected = True
                    print("[INFO] Wi-Fi connected: {}".format(self._ssid))
                    return True

            except MemoryError:
                raise
            except Exception as err:
                if DEBUG:
                    print("[DEBUG] Wi-Fi attempt failed: {}".format(err))

            if self._was_connected:
                self._disconnect_count += 1
                self._was_connected = False

            if attempt_index < len(self._reconnect_delays_sec) - 1:
                if DEBUG:
                    print("[DEBUG] Wi-Fi retry in {} sec".format(delay_sec))
                time.sleep(delay_sec)

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
            except Exception as err:
                if DEBUG:
                    print("[DEBUG] Wi-Fi ifconfig snapshot failed: {}".format(err))
            try:
                snapshot["rssi"] = self._wlan.status("rssi")
            except MemoryError:
                raise
            except Exception as err:
                if DEBUG:
                    print("[DEBUG] Wi-Fi RSSI snapshot failed: {}".format(err))

        return snapshot
