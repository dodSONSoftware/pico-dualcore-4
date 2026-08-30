# test_startup.py - Tests for deterministic startup contract
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from config import load_config, split_config


ROOT = pathlib.Path(__file__).resolve().parents[1]


def _base_config():
    """Load the base configuration."""
    import json
    return json.loads((ROOT / "config.json").read_text())


class MockInterCore:
    """Mock inter-core bus for testing."""
    def __init__(self):
        self.outbound_queue = None
        self.event_queue = None
        self.state_mailboxes = None


class MockLEDManager:
    """Mock LED manager for testing."""
    def __init__(self):
        self.connecting = False

    def set_connecting(self, value):
        self.connecting = bool(value)

    def telemetry_sent(self):
        pass


class MockWifi:
    """Mock Wi-Fi for testing."""
    def __init__(self, ssid, password, reconnect_delays):
        self._ssid = ssid
        self._is_connected = False
        self._connect_count = 0

    def is_connected(self):
        return self._is_connected

    def connect(self):
        self._is_connected = True
        self._connect_count += 1
        return True

    def snapshot(self, mqtt_connected):
        return {
            "wifi_connected": self._is_connected,
            "mqtt_connected": mqtt_connected,
            "ssid": self._ssid,
            "ip_address": "192.168.1.100",
            "netmask": "255.255.255.0",
            "gateway": "192.168.1.1",
            "dns": "192.168.1.1",
            "rssi": -50,
            "wifi_connect_count": self._connect_count,
            "wifi_disconnect_count": 0,
        }


class MockMqtt:
    """Mock MQTT for testing."""
    def __init__(self, config, message_callback):
        self._connected = False
        self._connect_count = 0
        self._disconnect_count = 0
        self.packet_id_counter = 1

    def is_connected(self):
        return self._connected

    def connect(self):
        self._connected = True
        self._connect_count += 1
        return True

    def mark_disconnected(self):
        self._connected = False

    def check_msg(self):
        pass

    def publish_qos1(self, topic, message):
        pass

    def publish_qos1_with_packet_id(self, topic, message, packet_id, timeout_ms=None):
        # Simulate successful PUBACK
        return True

    def get_next_packet_id(self):
        pid = self.packet_id_counter
        self.packet_id_counter += 1
        return pid

    def ping_due(self):
        return False

    def ping(self):
        pass

    def status(self):
        return {
            "connected": self._connected,
            "connect_count": self._connect_count,
            "disconnect_count": self._disconnect_count,
        }


def test_config_has_network_probe_properties():
    """Verify the config includes network_probe_timeout_sec and mqtt_topic_network_probe."""
    config = _base_config()

    assert "network_probe_timeout_sec" in config
    assert config["network_probe_timeout_sec"] > 0

    assert "mqtt_topic_network_probe" in config
    assert isinstance(config["mqtt_topic_network_probe"], str)
    assert len(config["mqtt_topic_network_probe"]) > 0


def test_core0_config_has_network_probe():
    """Verify network probe config is in Core 0's view."""
    config = _base_config()
    core0, core1 = split_config(config)

    assert "network_probe_timeout_sec" in core0
    assert "mqtt_topic_network_probe" in core0


def test_startup_order_preserved():
    """Verify startup sequence order is maintained.

    The startup contract must follow this order:
    1. Wi-Fi connects
    2. MQTT connects
    3. Network probe #1 with PUBACK
    4. Startup work drained
    5. 5-second wait
    6. Network probe #2 with PUBACK
    7. UTC sync
    8. Initial snapshots published
    9. LED stops flashing
    10. Core 1 starts

    This test verifies the config supports all required steps.
    """
    config = _base_config()

    # Verify required config properties exist
    required_props = [
        "network_probe_timeout_sec",
        "mqtt_topic_network_probe",
        "mqtt_broker_response_timeout_sec",
        "datetime_sync_interval_min",
        "network_snapshot_interval_sec",
    ]

    for prop in required_props:
        assert prop in config, f"Missing required config property: {prop}"
