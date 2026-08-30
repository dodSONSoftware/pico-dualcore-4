# wifi.py - Core 0 exclusive Wi-Fi owner
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import time
import network

from debug import DEBUG


# Passive RSSI statistics sampling gate. snapshot() itself runs at most every
# network_snapshot_interval_sec (5 s); the statistics are gated even further so
# the min/max/moving average track link quality rather than the snapshot
# cadence. Internal constant: not user-configurable.
WIFI_RSSI_SAMPLE_INTERVAL_MS = 10_000

# Stable firmware-facing reconnect triggers. Only values Core 0 actually knows
# are ever recorded; 802.11 reason codes are never stored here.
_KNOWN_RECONNECT_TRIGGERS = ("wifi_disconnected", "network_probe_failure")

# WLAN.status() codes are port-specific integers; map this build's constants to
# stable firmware-facing strings instead of depending on their numeric values.
_STATUS_REASONS = (
    "STAT_IDLE",
    "STAT_CONNECTING",
    "STAT_WRONG_PASSWORD",
    "STAT_NO_AP_FOUND",
    "STAT_CONNECT_FAIL",
    "STAT_GOT_IP",
)
_STATUS_REASON_BY_CODE = None


def _status_reason_by_code():
    """Build (once) the WLAN.status() code -> stable string table."""
    global _STATUS_REASON_BY_CODE
    if _STATUS_REASON_BY_CODE is None:
        table = {}
        for name in _STATUS_REASONS:
            code = getattr(network.WLAN, name, None)
            if isinstance(code, int) and not isinstance(code, bool):
                table[code] = name[len("STAT_"):].lower()
        _STATUS_REASON_BY_CODE = table
    return _STATUS_REASON_BY_CODE


def _format_bssid(value):
    """Normalize a BSSID (6 binary bytes or colon-hex string) to lowercase
    aa:bb:cc:dd:ee:ff, or None if the value is not a valid BSSID."""
    if isinstance(value, (bytes, bytearray)) and len(value) == 6:
        return ":".join("{:02x}".format(byte) for byte in value)
    if isinstance(value, str):
        parts = value.lower().split(":")
        if len(parts) != 6:
            return None
        for part in parts:
            if len(part) != 2:
                return None
            for char in part:
                if char not in "0123456789abcdef":
                    return None
        return ":".join(parts)
    return None


class Wifi:
    """Original-style Wi-Fi lifecycle, owned exclusively by Core 0.

    Also owns the passive Wi-Fi quality diagnostics: RSSI min/max/moving
    average statistics, the reconnect duration spanning a full retry/backoff
    sequence, the connect-to-IP-ready (association + auth + DHCP) acquisition
    duration, the last meaningful WLAN status, the firmware-level reconnect
    trigger, and best-effort BSSID/channel association details. All values are
    lifetime state (reset only by reboot), are observational, and never drive
    recovery or degradation decisions.
    """

    def __init__(self, ssid, password, reconnect_delays_sec):
        self._ssid = ssid
        self._password = password
        self._reconnect_delays_sec = reconnect_delays_sec
        self._wlan = None
        self._connect_count = 0
        self._disconnect_count = 0
        self._was_connected = False

        # RSSI statistics (sampled at most every WIFI_RSSI_SAMPLE_INTERVAL_MS).
        self._rssi_min_dbm = None
        self._rssi_max_dbm = None
        self._rssi_avg_x8 = None
        self._rssi_sample_count = 0
        self._last_rssi_sample_ms = None

        # Reconnect / DHCP / status tracking.
        self._ever_connected = False
        self._reconnect_started_ms = None
        self._last_reconnect_duration_ms = 0
        self._last_dhcp_acquisition_duration_ms = 0
        self._last_status_reason = "unknown"
        self._last_reconnect_trigger = "unknown"

        # Association details (feature-detected once; never via wlan.scan()).
        self._association_supported = None
        self._bssid = None
        self._channel = None

        # DHCPv4 readiness query (feature-detected once).
        self._dhcp4_query_supported = None

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

    def gateway_address(self):
        """Gateway from the current interface configuration (None if unavailable)."""
        if not self.is_connected():
            return None
        try:
            return self._wlan.ifconfig()[2]
        except MemoryError:
            raise
        except Exception:
            return None

    def dns_address(self):
        """Primary DNS server from the current interface configuration."""
        if not self.is_connected():
            return None
        try:
            return self._wlan.ifconfig()[3]
        except MemoryError:
            raise
        except Exception:
            return None

    def set_reconnect_delays(self, delays):
        """Replace the reconnect backoff sequence (DYNAMIC config key).

        The value is already patch-validated by the transaction layer. A copy
        is kept so the caller's list (the committed config) is never aliased.
        Takes effect from the next reconnection sequence onward; an in-flight
        sequence keeps the delays it started with.
        """
        self._reconnect_delays_sec = list(delays)

    def note_reconnect_trigger(self, trigger):
        """Record why a reconnection sequence is being driven (Core 0 only).

        Called by Core 0 before re-establishing the network. Only the stable
        firmware-level values in _KNOWN_RECONNECT_TRIGGERS are stored; anything
        else leaves the last recorded value unchanged.
        """
        if trigger in _KNOWN_RECONNECT_TRIGGERS:
            self._last_reconnect_trigger = trigger

    def _observe_connection_state(self, connected):
        """Single owner of the connected/disconnected transition logic.

        Preserves the original connect/disconnect count semantics exactly (one
        increment per transition) and, on top of the same transitions, drives:
        - _ever_connected, distinguishing the initial boot connect from a
          reconnect (the initial connect is never a "reconnect");
        - the reconnect timer, which starts on the first observed disconnect
          after having been connected and spans the ENTIRE retry/backoff
          sequence (it is never restarted per attempt);
        - finalization of _last_reconnect_duration_ms on the next successful
          connection (initial boot connect leaves the duration at 0).
        """
        now = time.ticks_ms()
        if connected:
            if not self._was_connected:
                self._connect_count += 1
            self._was_connected = True
            if self._reconnect_started_ms is not None:
                self._last_reconnect_duration_ms = time.ticks_diff(
                    now, self._reconnect_started_ms)
                self._reconnect_started_ms = None
            self._ever_connected = True
        else:
            if self._was_connected:
                self._disconnect_count += 1
                self._was_connected = False
            if self._ever_connected and self._reconnect_started_ms is None:
                self._reconnect_started_ms = now

    def _dhcp4_ready(self):
        """Whether the port reports DHCPv4 ready (feature-detected once).

        When the port does not expose ipconfig("has_dhcp4"), the existing
        isconnected() condition is the readiness signal.
        """
        if self._wlan is None:
            return False
        if self._dhcp4_query_supported is None:
            try:
                self._wlan.ipconfig("has_dhcp4")
            except MemoryError:
                raise
            except Exception:
                self._dhcp4_query_supported = False
                return True
            self._dhcp4_query_supported = True
        if self._dhcp4_query_supported:
            try:
                return bool(self._wlan.ipconfig("has_dhcp4"))
            except MemoryError:
                raise
            except Exception:
                return False
        return True

    def _link_ready(self):
        """Connected AND (when the port reports it) DHCPv4 ready."""
        if self._wlan is None:
            return False
        try:
            if not self._wlan.isconnected():
                return False
        except MemoryError:
            raise
        except Exception:
            return False
        return self._dhcp4_ready()

    def _read_status_reason(self, default):
        """Map the current WLAN.status() to its stable string (best effort)."""
        if self._wlan is None:
            return default
        try:
            code = self._wlan.status()
        except MemoryError:
            raise
        except Exception as err:
            if DEBUG:
                print("[DEBUG] Wi-Fi status() failed: {}".format(err))
            return default
        return _status_reason_by_code().get(code, "unknown")

    def _record_rssi_sample(self, rssi):
        """Update the lifetime RSSI statistics from one valid reading.

        Gated to at most one sample per WIFI_RSSI_SAMPLE_INTERVAL_MS; a failed
        read (None) or an out-of-gate read leaves count/min/max untouched and
        never substitutes 0. Statistics are NOT reset on reconnect; only a
        reboot clears them.
        """
        if not isinstance(rssi, int) or isinstance(rssi, bool):
            return
        now = time.ticks_ms()
        if self._last_rssi_sample_ms is not None and \
                time.ticks_diff(now, self._last_rssi_sample_ms) < WIFI_RSSI_SAMPLE_INTERVAL_MS:
            return
        self._last_rssi_sample_ms = now
        self._rssi_sample_count += 1
        if self._rssi_min_dbm is None or rssi < self._rssi_min_dbm:
            self._rssi_min_dbm = rssi
        if self._rssi_max_dbm is None or rssi > self._rssi_max_dbm:
            self._rssi_max_dbm = rssi
        if self._rssi_avg_x8 is None:
            self._rssi_avg_x8 = rssi * 8
        else:
            # Integer fixed-point EMA: 1/8 new sample + 7/8 previous.
            self._rssi_avg_x8 = (self._rssi_avg_x8 * 7 + rssi * 8) // 8

    def _record_association(self):
        """Fetch BSSID/channel after a successful connection (best effort).

        Feature-detected once per build via the least-invasive direct STA
        queries; the values are refreshed after every successful connection
        (the association may have changed). Never calls wlan.scan(). When a
        value is not authoritatively available it is reported as None with
        association_details_supported False (never faked, never from a scan).
        """
        if self._association_supported is False:
            return
        if self._wlan is None:
            return
        channel = None
        try:
            raw_channel = self._wlan.config("channel")
        except MemoryError:
            raise
        except Exception:
            raw_channel = None
        if isinstance(raw_channel, int) and not isinstance(raw_channel, bool) \
                and 1 <= raw_channel <= 14:
            channel = raw_channel
        try:
            raw_bssid = self._wlan.config("bssid")
        except MemoryError:
            raise
        except Exception:
            raw_bssid = None
        bssid = _format_bssid(raw_bssid)
        if channel is not None and bssid is not None:
            self._association_supported = True
            self._channel = channel
            self._bssid = bssid
        else:
            self._association_supported = False
            self._channel = None
            self._bssid = None

    def connect(self):
        """Connect using the critical sequence from the original firmware."""
        for attempt_index, delay_sec in enumerate(self._reconnect_delays_sec):
            try:
                ready = False
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

                # Durations here are connect-to-IP-ready (association + auth +
                # DHCP), not an isolated DHCP DORA exchange: that is all this
                # port authoritatively exposes.
                ready_started_ms = time.ticks_ms()
                self._wlan.connect(self._ssid, self._password)

                ready = self._link_ready()
                wait_count = 0
                while not ready and wait_count < 200:
                    time.sleep_ms(100)
                    wait_count += 1
                    ready = self._link_ready()

                if ready:
                    self._last_dhcp_acquisition_duration_ms = time.ticks_diff(
                        time.ticks_ms(), ready_started_ms)
                    self._observe_connection_state(True)
                    self._last_status_reason = self._read_status_reason("got_ip")
                    self._record_association()
                    print("[INFO] Wi-Fi connected: {}".format(self._ssid))
                    return True

            except MemoryError:
                raise
            except Exception as err:
                if DEBUG:
                    print("[DEBUG] Wi-Fi attempt failed: {}".format(err))

            self._observe_connection_state(False)
            self._last_status_reason = self._read_status_reason("unknown")

            if attempt_index < len(self._reconnect_delays_sec) - 1:
                if DEBUG:
                    print("[DEBUG] Wi-Fi retry in {} sec".format(delay_sec))
                time.sleep(delay_sec)

        return False

    def snapshot(self, mqtt_connected):
        connected = self.is_connected()
        was_connected = self._was_connected
        self._observe_connection_state(connected)
        if connected and not was_connected:
            # A (re)connection was established without a connect() attempt
            # (port auto-rejoin): refresh status and association as connect()
            # does.
            self._last_status_reason = self._read_status_reason("got_ip")
            self._record_association()
        elif not connected and was_connected:
            self._last_status_reason = self._read_status_reason("unknown")

        rssi = None
        if connected:
            try:
                rssi = self._wlan.status("rssi")
            except MemoryError:
                raise
            except Exception as err:
                if DEBUG:
                    print("[DEBUG] Wi-Fi RSSI snapshot failed: {}".format(err))
            # Record before the dict is built so this pass's sample is already
            # included in the statistics the snapshot reports.
            self._record_rssi_sample(rssi)

        snapshot = {
            "wifi_connected": connected,
            "mqtt_connected": bool(mqtt_connected),
            "ssid": self._ssid if connected else None,
            "ip_address": None,
            "netmask": None,
            "gateway": None,
            "dns": None,
            "rssi": rssi,
            "wifi_connect_count": self._connect_count,
            "wifi_disconnect_count": self._disconnect_count,
            # Passive Wi-Fi quality diagnostics (lifetime; reboot resets).
            "rssi_min_dbm": self._rssi_min_dbm,
            "rssi_max_dbm": self._rssi_max_dbm,
            "rssi_moving_average_dbm": (
                self._rssi_avg_x8 // 8 if self._rssi_avg_x8 is not None else None
            ),
            "rssi_sample_count": self._rssi_sample_count,
            "last_reconnect_duration_ms": self._last_reconnect_duration_ms,
            "last_dhcp_acquisition_duration_ms": self._last_dhcp_acquisition_duration_ms,
            "last_status_reason": self._last_status_reason,
            "last_reconnect_trigger": self._last_reconnect_trigger,
            "association_details_supported": self._association_supported is True,
            "bssid": self._bssid,
            "channel": self._channel,
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

        return snapshot
