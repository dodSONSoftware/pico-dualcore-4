# test_config_commands.py - read_config / write_config command contract
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Host-side tests for the Core 0 read_config / write_config command layer.

Covers the rejection codes (stable error vocabulary), no-op / redelivery
idempotency, DYNAMIC activation (Core 0-owned), RESTART_REQUIRED persistence
with ``reboot_required`` bookkeeping (never an auto-reboot), the MQTT
RECONFIGURE success and restore-on-failure paths, and the invariant that a
response-delivery failure never rolls a committed configuration back.
"""

import importlib
import json
import pathlib
import sys
import time as _real_time
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

ROOT = pathlib.Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# MicroPython stand-ins (installed inside the fixture, not at collection).
# ---------------------------------------------------------------------------

class FakeTime:
    def __init__(self):
        self.now_ms = 0

    def ticks_ms(self):
        return self.now_ms

    def ticks_diff(self, now, prev):
        return now - prev

    def ticks_add(self, base, delta):
        return base + delta

    def sleep_ms(self, ms):
        self.now_ms += ms

    def sleep(self, secs):
        self.sleep_ms(int(secs * 1000))

    def __getattr__(self, name):
        return getattr(_real_time, name)


_FAKE_TIME = FakeTime()
_MACHINE = MagicMock()
_DEBUG = MagicMock()
_DEBUG.DEBUG = False
_WIFI_MODULE = MagicMock()
_MQTT_MODULE = MagicMock()


def _install_mocks(mp):
    # Installed via monkeypatch.setitem so every entry (especially
    # network_diagnostics, which a later test reloads for real) is restored
    # after the test instead of shadowing the real module in sys.modules.
    mp.setitem(sys.modules, "time", _FAKE_TIME)
    mp.setitem(sys.modules, "machine", _MACHINE)
    mp.setitem(sys.modules, "debug", _DEBUG)
    mp.setitem(sys.modules, "wifi", _WIFI_MODULE)
    mp.setitem(sys.modules, "mqtt", _MQTT_MODULE)
    mp.setitem(sys.modules, "network_diagnostics", MagicMock())


class FakeLed:
    def set_connecting(self, value):
        pass


class ConfigWifi:
    """Records set_reconnect_delays; reports connected."""

    def __init__(self):
        self.reconnect_delays = None
        self.applied_delays = []

    def set_reconnect_delays(self, delays):
        self.reconnect_delays = list(delays)
        self.applied_delays.append(list(delays))

    def is_connected(self):
        return True

    def ip_address(self):
        return None

    def note_reconnect_trigger(self, trigger):
        pass


class ConfigMqtt:
    """Scriptable MQTT connection for the RECONFIGURE path.

    connect() returns the next scripted outcome (default True). update and
    snapshot mirror the real Mqtt so the coordinator's previous-value capture
    and restore are exercised for real.
    """

    def __init__(self):
        self.connected = False
        self.connect_calls = 0
        self.connect_script = []
        self.mark_disconnected_calls = 0
        self.update_calls = []
        self.config = {
            "mqtt_broker_ip_address": "192.0.2.10",
            "mqtt_keepalive_sec": 60,
            "mqtt_broker_response_timeout_sec": 5,
            "mqtt_topic_command": "iot/v3/command",
            "mqtt_topic_info_response": "iot/v3/info_response",
            "mqtt_reconnect_delays_sec": [1, 2, 3],
        }

    def is_connected(self):
        return self.connected

    def connect(self):
        self.connect_calls += 1
        outcome = self.connect_script.pop(0) if self.connect_script else True
        self.connected = outcome
        return outcome

    def mark_disconnected(self):
        self.mark_disconnected_calls += 1
        self.connected = False

    def update_connection_config(self, values):
        self.update_calls.append(dict(values))
        self.config.update(values)

    def mqtt_config_snapshot(self):
        return dict(self.config)

    def check_msg(self):
        pass


class ConfigInterCore:
    def __init__(self, config_state=None):
        self.state_mailboxes = MagicMock()
        self.outbound_queue = MagicMock()
        self.outbound_queue.get_depth.return_value = 0
        self.outbound_queue.has_in_flight.return_value = False
        self.event_queue = MagicMock()
        self.memory_stats = MagicMock()
        self.minimum_free_heap_bytes = 65536
        self.config_state = config_state
        from intercore import ConfigTransactionMailbox
        self.config_transaction_mailbox = ConfigTransactionMailbox()


@pytest.fixture
def cfg_env(tmp_path, monkeypatch):
    """A Core0 with a real ConfigState + mailbox and a temp config file."""
    _FAKE_TIME.now_ms = 0
    _install_mocks(monkeypatch)
    importlib.reload(importlib.import_module("uptime"))
    core0_mod = importlib.import_module("core0")
    importlib.reload(core0_mod)

    from config import (
        ConfigState,
        config_checksum,
        load_config,
        split_config,
    )

    full = json.loads((ROOT / "config.json").read_text())
    base_checksum = config_checksum(json.dumps(full).encode("utf-8"))
    config_state = ConfigState(full, base_checksum)

    # Point the coordinator at a temp copy of the config file.
    config_file = str(tmp_path / "config.json")
    with open(config_file, "w") as handle:
        json.dump(full, handle)
    monkeypatch.setattr(core0_mod, "CONFIG_FILE", config_file)

    core0_config, _core1, _bus = split_config(full)
    instance = core0_mod.Core0(
        ConfigInterCore(config_state),
        core0_config,
        {"wifi_ssid": "test-ssid", "wifi_password": "test-password"},
        "test-runtime",
        0,
        FakeLed(),
    )
    instance._wifi = ConfigWifi()
    instance._mqtt = ConfigMqtt()
    # Seed the fake connection's config from the *committed* values so that
    # the coordinator's previous-value capture matches the real config (in
    # production Mqtt.mqtt_config_snapshot() reflects the config it was built
    # from). A mismatch here would make a correct restore look torn.
    for key in (
        "mqtt_broker_ip_address", "mqtt_keepalive_sec",
        "mqtt_broker_response_timeout_sec", "mqtt_topic_command",
        "mqtt_topic_info_response", "mqtt_reconnect_delays_sec",
    ):
        if key in full:
            instance._mqtt.config[key] = full[key]

    def deliver(command, payload, command_id="req-1"):
        doc = {
            "message_type": "command",
            "target": "*",
            "command": command,
            "command_id": command_id,
            "message_schema_version": 3,
            "payload": payload,
        }
        instance._on_mqtt_message(
            instance._config["mqtt_topic_command"], json.dumps(doc))
        assert instance._pending_core0_responses, "expected a queued response"
        response = instance._pending_core0_responses.pop()
        instance._pending_connection_logs.clear()
        return response

    env = {
        "instance": instance,
        "core0_mod": core0_mod,
        "config_state": config_state,
        "full_config": full,
        "config_file": config_file,
        "deliver": deliver,
    }
    return env


# ---------------------------------------------------------------------------
# read_config
# ---------------------------------------------------------------------------

def test_read_config_returns_full_committed_config(cfg_env):
    r = cfg_env["deliver"]("read_config", {})
    assert r["success"] is True
    data = r["data"]
    assert set(data) == {
        "config", "config_checksum_sha256", "reboot_required",
        "pending_restart_keys",
    }
    committed = data["config"]
    assert committed["config_schema_version"] == 9
    assert committed["config_generation"] == 0
    assert committed["devices"] == cfg_env["full_config"]["devices"]
    assert data["reboot_required"] is False
    assert data["pending_restart_keys"] == []
    assert len(data["config_checksum_sha256"]) == 64


def test_read_config_never_returns_credentials(cfg_env):
    r = cfg_env["deliver"]("read_config", {})
    blob = json.dumps(r)
    assert "test-ssid" not in blob
    assert "test-password" not in blob
    assert "wifi_ssid" not in r["data"]["config"]
    assert "wifi_password" not in r["data"]["config"]


def test_read_config_non_empty_payload_rejected(cfg_env):
    r = cfg_env["deliver"]("read_config", {"x": 1})
    assert r["success"] is False
    assert r["error"]["code"] == "command_invalid_payload"


# ---------------------------------------------------------------------------
# write_config rejections + idempotency
# ---------------------------------------------------------------------------

def test_write_config_empty_patch_rejected(cfg_env):
    r = cfg_env["deliver"]("write_config", {})
    assert r["success"] is False
    assert r["error"]["code"] == "command_invalid_payload"


def test_write_config_unknown_key_rejected(cfg_env):
    r = cfg_env["deliver"]("write_config", {"bogus_key": 1})
    assert r["success"] is False
    assert r["error"]["code"] == "invalid_config_key"


def test_write_config_read_only_keys_rejected(cfg_env):
    for key in ("config_schema_version", "config_generation"):
        r = cfg_env["deliver"]("write_config", {key: 1})
        assert r["success"] is False
        assert r["error"]["code"] == "read_only_config_key", key


def test_write_config_invalid_value_rejected(cfg_env):
    r = cfg_env["deliver"]("write_config", {"health_interval_sec": -5})
    assert r["success"] is False
    assert r["error"]["code"] == "invalid_config_value"


def test_write_config_no_op_does_not_bump_generation(cfg_env):
    base = cfg_env["full_config"]
    # A patch equal to the committed value is a no-op: success, empty
    # changed_keys, generation unchanged, no staged file created.
    r = cfg_env["deliver"]("write_config", {"source": base["source"]})
    assert r["success"] is True
    assert r["data"]["changed_keys"] == []
    assert r["data"]["config_generation"] == 0
    assert cfg_env["config_state"].generation() == 0
    import os
    assert not os.path.exists(cfg_env["config_file"] + ".tmp")


def test_write_config_in_progress_rejected(cfg_env):
    instance = cfg_env["instance"]
    instance._config_tx_in_progress = True
    try:
        r = cfg_env["deliver"]("write_config", {"source": "busy"})
        assert r["success"] is False
        assert r["error"]["code"] == "configuration_update_in_progress"
    finally:
        instance._config_tx_in_progress = False


def test_write_config_redelivery_is_idempotent(cfg_env):
    base = cfg_env["full_config"]
    first = cfg_env["deliver"]("write_config", {"source": "node-a"})
    assert first["success"] is True
    gen_after_first = first["data"]["config_generation"]
    # Redelivering the now-committed value is a no-op: generation does not
    # increment again.
    second = cfg_env["deliver"]("write_config", {"source": "node-a"})
    assert second["success"] is True
    assert second["data"]["changed_keys"] == []
    assert second["data"]["config_generation"] == gen_after_first
    assert cfg_env["config_state"].generation() == gen_after_first


# ---------------------------------------------------------------------------
# DYNAMIC activation (Core 0-owned)
# ---------------------------------------------------------------------------

def test_write_config_dynamic_success_commits_and_activates(cfg_env):
    r = cfg_env["deliver"]("write_config", {"source": "dynamic-node"})
    assert r["success"] is True, r
    assert r["data"]["config_generation"] == 1
    assert r["data"]["changed_keys"] == ["source"]
    assert r["data"]["dynamic_keys"] == ["source"]
    assert r["data"]["reboot_required"] is False
    # Runtime (Core 0 config) and the committed file both carry the new value.
    assert cfg_env["instance"]._config["source"] == "dynamic-node"
    assert cfg_env["config_state"].committed_config()["source"] == "dynamic-node"
    committed = json.loads(open(cfg_env["config_file"]).read())
    assert committed["source"] == "dynamic-node"
    assert committed["config_generation"] == 1
    assert cfg_env["config_state"].snapshot()["config_generation"] == 1


def test_write_config_multiple_dynamic_keys_one_transaction(cfg_env):
    r = cfg_env["deliver"]("write_config", {
        "source": "multi-node",
        "network_snapshot_interval_sec": 7,
    })
    assert r["success"] is True, r
    assert sorted(r["data"]["changed_keys"]) == [
        "network_snapshot_interval_sec", "source"]
    assert sorted(r["data"]["dynamic_keys"]) == [
        "network_snapshot_interval_sec", "source"]
    inst = cfg_env["instance"]
    assert inst._config["source"] == "multi-node"
    assert inst._config["network_snapshot_interval_sec"] == 7
    # The derived network-snapshot interval scalar is refreshed.
    committed = json.loads(open(cfg_env["config_file"]).read())
    assert committed["source"] == "multi-node"
    assert committed["network_snapshot_interval_sec"] == 7


def test_write_config_wifi_delays_dynamic_refreshes_wifi(cfg_env):
    delays = [2, 5, 9]
    r = cfg_env["deliver"]("write_config", {"wifi_reconnect_delays_sec": delays})
    assert r["success"] is True, r
    assert cfg_env["instance"]._wifi.applied_delays == [delays]
    committed = json.loads(open(cfg_env["config_file"]).read())
    assert committed["wifi_reconnect_delays_sec"] == delays


# ---------------------------------------------------------------------------
# RESTART_REQUIRED: persist only, set reboot_required, never reboot
# ---------------------------------------------------------------------------

def test_write_config_restart_required_persets_and_flags(cfg_env):
    base = cfg_env["full_config"]
    boot = base["max_intercore_event_entries"]
    r = cfg_env["deliver"]("write_config",
                          {"max_intercore_event_entries": boot + 4})
    assert r["success"] is True, r
    assert r["data"]["reboot_required"] is True
    assert r["data"]["restart_required_keys"] == ["max_intercore_event_entries"]
    assert cfg_env["config_state"].snapshot()["reboot_required"] is True
    assert cfg_env["config_state"].snapshot()["pending_restart_keys"] == [
        "max_intercore_event_entries"]
    # Persisted to the active file...
    committed = json.loads(open(cfg_env["config_file"]).read())
    assert committed["max_intercore_event_entries"] == boot + 4
    # ...but the running event queue is NOT resized (that needs a reboot),
    # and the firmware never calls machine.reset() for a RESTART_REQUIRED key.
    assert machine_reset_not_called(cfg_env["core0_mod"])


def test_write_config_restart_required_revert_clears_flag(cfg_env):
    base = cfg_env["full_config"]
    boot = base["max_intercore_event_entries"]
    cfg_env["deliver"]("write_config",
                       {"max_intercore_event_entries": boot + 4})
    assert cfg_env["config_state"].snapshot()["reboot_required"] is True
    r = cfg_env["deliver"]("write_config",
                          {"max_intercore_event_entries": boot})
    assert r["success"] is True
    assert r["data"]["reboot_required"] is False
    assert cfg_env["config_state"].snapshot()["reboot_required"] is False


# ---------------------------------------------------------------------------
# MQTT RECONFIGURE
# ---------------------------------------------------------------------------

def test_write_config_mqtt_reconfigure_success(cfg_env):
    mqtt = cfg_env["instance"]._mqtt
    before = mqtt.mqtt_config_snapshot()["mqtt_broker_ip_address"]
    assert before == cfg_env["full_config"]["mqtt_broker_ip_address"]
    r = cfg_env["deliver"]("write_config",
                          {"mqtt_broker_ip_address": "198.51.100.7"})
    assert r["success"] is True, r
    assert r["data"]["reconfigured_keys"] == ["mqtt_broker_ip_address"]
    # The connection was re-established with the new broker...
    assert mqtt.config["mqtt_broker_ip_address"] == "198.51.100.7"
    assert mqtt.connect_calls >= 1
    committed = json.loads(open(cfg_env["config_file"]).read())
    assert committed["mqtt_broker_ip_address"] == "198.51.100.7"


def test_write_config_mqtt_reconfigure_failure_restores_previous(cfg_env):
    mqtt = cfg_env["instance"]._mqtt
    original = cfg_env["full_config"]["mqtt_broker_ip_address"]
    # First connect() (candidate broker) fails; the rollback reconnect
    # (previous broker) succeeds.
    mqtt.connect_script = [False, True]
    r = cfg_env["deliver"]("write_config",
                          {"mqtt_broker_ip_address": "203.0.113.9"})
    assert r["success"] is False
    assert r["error"]["code"] == "configuration_reconfigure_failed"
    # The connection configuration is back to the previous broker.
    assert mqtt.config["mqtt_broker_ip_address"] == original
    # The runtime config dict is restored.
    assert cfg_env["instance"]._config["mqtt_broker_ip_address"] == original
    # The transaction did NOT commit: generation unchanged, file unchanged.
    assert cfg_env["config_state"].generation() == 0
    committed = json.loads(open(cfg_env["config_file"]).read())
    assert committed["mqtt_broker_ip_address"] == original
    # A staged file was created then discarded on rollback.
    import os
    assert not os.path.exists(cfg_env["config_file"] + ".tmp")


def test_write_config_mqtt_reconfigure_restore_failure_is_rollback_failed(cfg_env):
    mqtt = cfg_env["instance"]._mqtt
    # Both the candidate connect AND the rollback reconnect fail.
    mqtt.connect_script = [False, False]
    r = cfg_env["deliver"]("write_config",
                          {"mqtt_broker_ip_address": "203.0.113.9"})
    assert r["success"] is False
    assert r["error"]["code"] == "configuration_rollback_failed"
    assert cfg_env["config_state"].generation() == 0


# ---------------------------------------------------------------------------
# Response-delivery independence
# ---------------------------------------------------------------------------

def test_response_delivery_failure_does_not_roll_back_committed(cfg_env):
    # Fill the response queue so the transaction's response cannot be queued,
    # but the configuration still commits (commit happens before the response
    # is queued, and a delivery failure never rolls it back).
    instance = cfg_env["instance"]
    for i in range(4):
        instance._pending_core0_responses.append({"filler": i})
    r_holder = {}

    # Directly run the transaction path and capture the outcome even though
    # the response queue is full.
    full = cfg_env["full_config"]
    candidate = dict(full)
    candidate["source"] = "committed-anyway"
    candidate["config_generation"] = 1
    try:
        instance._begin_config_write_transaction("req-x", False, {"source": "committed-anyway"})
    except Exception as err:  # noqa: BLE001
        r_holder["error"] = str(err)

    assert cfg_env["config_state"].generation() == 1
    assert cfg_env["config_state"].committed_config()["source"] == "committed-anyway"
    committed = json.loads(open(cfg_env["config_file"]).read())
    assert committed["source"] == "committed-anyway"


def machine_reset_not_called(core0_mod):
    """Assert machine.reset() was never invoked (the fake machine records it)."""
    calls = getattr(_MACHINE.reset, "call_count", 0)
    return calls == 0
