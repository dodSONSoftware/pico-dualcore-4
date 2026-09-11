# test_read_write_config_commands.py - read-config / write-config command contract
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""
Host-side tests for the read-config / write-config commands under the strict
command protocol contract, with the real configuration manager driving real
temporary files.

read-config: payload exactly {} (like reboot / get-details); the answer
carries the committed (PERSISTED) configuration and the in-boot
reboot_required flag. write-config: the payload is exactly
{"config": <complete candidate configuration>} (unknown payload keys are
named together; no patch/merge/partial-update shape); the answer carries
configuration_changed, the classification, the resulting reboot state, and
the deterministic change summary. There is no live apply: every changed
write commits and is pending a reboot, with the running firmware keeping
its boot values until then.
"""

import importlib
import json
import pathlib
import sys
import time as _real_time
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.modules.setdefault("machine", MagicMock())

ROOT = pathlib.Path(__file__).resolve().parents[1]


class FakeTime:
    """Controllable stand-in for MicroPython's time module."""

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
_MACHINE_MOCK = MagicMock()
_DEBUG_MOCK = MagicMock()
_DEBUG_MOCK.DEBUG = False
_WIFI_MOCK = MagicMock()
_MQTT_MOCK = MagicMock()


def _install_mocks():
    sys.modules["time"] = _FAKE_TIME
    sys.modules["machine"] = _MACHINE_MOCK
    sys.modules["debug"] = _DEBUG_MOCK
    sys.modules["wifi"] = _WIFI_MOCK
    sys.modules["mqtt"] = _MQTT_MOCK


# NOTE: the MicroPython stand-ins above must NOT be installed at collection
# time: other test modules import the real wifi/mqtt/time modules at
# collection, and mocked entries in sys.modules would shadow them.
from config import split_config  # noqa: E402
from config_manager import ConfigManager  # noqa: E402
from version import CONFIG_SCHEMA_VERSION, MESSAGE_SCHEMA_VERSION  # noqa: E402


def _full_config():
    return json.loads((ROOT / "tests" / "fixtures" / "config.json").read_text())


def _core0_config():
    config = _full_config()
    core0_config, _core1_config = split_config(config)
    return core0_config


class RecordingEventQueue:
    """Event queue that records admitted command events (get-details)."""

    def __init__(self):
        self.events = []

    def put(self, event):
        self.events.append(event)
        return True

    def take(self):
        if self.events:
            return self.events.pop(0)
        return None


class MockInterCore:
    def __init__(self):
        self.state_mailboxes = MagicMock()
        self.outbound_queue = MagicMock()
        self.event_queue = RecordingEventQueue()


@pytest.fixture
def make_core0(tmp_path):
    """Build a fresh Core0 with faked wifi/mqtt, a real split config, and a real
    configuration manager over an isolated config.json."""

    def _make():
        _FAKE_TIME.now_ms = 0
        _install_mocks()
        importlib.reload(importlib.import_module("uptime"))
        core0_mod = importlib.import_module("core0")
        importlib.reload(core0_mod)

        config_path = str(tmp_path / "config.json")
        with open(config_path, "w") as handle:
            handle.write(json.dumps(_full_config()))
        manager = ConfigManager(config_path)

        instance = core0_mod.Core0(
            MockInterCore(),
            _core0_config(),
            {"wifi_ssid": "test-ssid", "wifi_password": "test-password"},
            "test-runtime",
            0,
            MagicMock(),
            manager,
        )
        instance._wifi = MagicMock()
        instance._mqtt = MagicMock()
        return instance

    return _make


def _command(command, command_id, target="Test-Pico-2", payload=None):
    return {
        "message_type": "command",
        "message_schema_version": MESSAGE_SCHEMA_VERSION,
        "target": target,
        "command_id": command_id,
        "command": command,
        "payload": {} if payload is None else payload,
    }


def _send(core0, doc):
    core0._on_mqtt_message(core0._config["mqtt_topic_command"], json.dumps(doc))


def _last_response(core0):
    assert core0._pending_core0_responses
    return core0._pending_core0_responses[-1]


def _committed(tmp_path):
    return json.loads((tmp_path / "config.json").read_text())


# --- read-config -------------------------------------------------------------


def test_read_config_returns_committed_config_and_reboot_state(make_core0):
    core0 = make_core0()

    _send(core0, _command("read-config", "cfg-read-1"))

    response = _last_response(core0)
    assert response["success"] is True
    assert response["targeted"] is True
    assert response["data"]["config"] == _full_config()
    assert response["data"]["reboot_required"] is False
    assert core0._intercore.event_queue.events == []


def test_read_config_rejects_a_non_empty_payload(make_core0):
    core0 = make_core0()

    _send(core0, _command("read-config", "cfg-read-2", payload={"refresh": True}))

    response = _last_response(core0)
    assert response["success"] is False
    assert response["error"]["code"] == "unknown_fields"
    assert response["error"]["unknown_fields"] == ["refresh"]


def test_read_config_reports_pending_reboot(make_core0, tmp_path):
    core0 = make_core0()
    candidate = _full_config()
    candidate["source"] = "Other-Pico"
    _send(core0, _command("write-config", "cfg-wr-reboot",
                          payload={"config": candidate}))
    assert _last_response(core0)["success"] is True

    _send(core0, _command("read-config", "cfg-read-3"))

    response = _last_response(core0)
    assert response["success"] is True
    assert response["data"]["config"] == _committed(tmp_path)
    assert response["data"]["reboot_required"] is True


def test_read_config_accepts_source_ip_and_broadcast_targets(make_core0):
    core0 = make_core0()
    core0._wifi.ip_address.return_value = "10.0.0.42"

    for command_id, target, targeted in (
        ("cfg-read-t1", "test-pico-2", True),   # lowercase source
        ("cfg-read-t2", "TEST-PICO-2", True),   # uppercase source
        ("cfg-read-t3", "10.0.0.42", True),     # current IP
        ("cfg-read-t4", "*", False),            # broadcast is allowed
    ):
        _send(core0, _command("read-config", command_id, target=target))

        response = _last_response(core0)
        assert response["success"] is True
        assert response["targeted"] is targeted
        assert response["data"]["config"] == _full_config()


def test_read_config_never_includes_wifi_secrets(make_core0, tmp_path):
    core0 = make_core0()
    with open(str(tmp_path / "config-secrets.json"), "w") as handle:
        handle.write(json.dumps({
            "wifi_ssid": "secrets-ssid-xyz",
            "wifi_password": "secrets-pass-xyz",
        }))

    _send(core0, _command("read-config", "cfg-read-secrets"))

    response = _last_response(core0)
    assert response["success"] is True
    blob = json.dumps(response)
    assert "secrets-ssid-xyz" not in blob
    assert "secrets-pass-xyz" not in blob
    assert "wifi_ssid" not in response["data"]["config"]
    assert "wifi_password" not in response["data"]["config"]


def test_read_config_read_failure_returns_bounded_error(make_core0, tmp_path):
    core0 = make_core0()
    (tmp_path / "config.json").write_text("{ not valid json")

    _send(core0, _command("read-config", "cfg-read-fail"))

    response = _last_response(core0)
    assert response["success"] is False
    assert response["error"]["code"] == "config_unavailable"
    message = response["error"]["message"]
    assert "test-ssid" not in message
    assert "test-password" not in message
    assert len(message) < 200


def test_read_config_memory_error_propagates(make_core0, monkeypatch):
    core0 = make_core0()

    def _exhaust():
        raise MemoryError
    monkeypatch.setattr(core0._config_manager, "read_persisted", _exhaust)

    with pytest.raises(MemoryError):
        _send(core0, _command("read-config", "cfg-read-mem"))
    assert core0._pending_core0_responses == []


def test_read_config_duplicate_command_id_is_ignored(make_core0):
    core0 = make_core0()

    _send(core0, _command("read-config", "cfg-read-dup"))
    _send(core0, _command("read-config", "cfg-read-dup"))

    assert len(core0._pending_core0_responses) == 1
    assert core0._pending_core0_responses[0]["success"] is True


# --- write-config: UNCHANGED ---------------------------------------------------


def test_write_config_unchanged_writes_nothing(make_core0, tmp_path):
    core0 = make_core0()
    before = (tmp_path / "config.json").read_text()

    _send(core0, _command("write-config", "cfg-wr-unchanged",
                          payload={"config": _full_config()}))

    response = _last_response(core0)
    assert response["success"] is True
    assert response["data"]["configuration_changed"] is False
    assert response["data"]["classification"] == "UNCHANGED"
    assert response["data"]["changes"] == []
    assert response["data"]["reboot_required"] is False
    assert (tmp_path / "config.json").read_text() == before
    assert core0._intercore.event_queue.events == []
    assert core0._config_manager.reboot_required is False


def test_write_config_unchanged_logs_warning(make_core0, monkeypatch):
    """An UNCHANGED write is visible in the log, not just in the response."""
    core0 = make_core0()
    lines = []
    monkeypatch.setattr("builtins.print", lines.append)

    _send(core0, _command("write-config", "cfg-wr-unchanged-log",
                          payload={"config": _full_config()}))

    assert _last_response(core0)["success"] is True
    assert "[WARNING] write-config ignored: submitted configuration is " \
           "identical to persisted configuration" in lines


def test_write_config_unchanged_while_reboot_pending(make_core0, tmp_path, monkeypatch):
    """UNCHANGED preserves the pending reboot state (and may say so in the log)."""
    core0 = make_core0()
    lines = []
    monkeypatch.setattr("builtins.print", lines.append)

    reboot_candidate = _full_config()
    reboot_candidate["source"] = "Other-Pico"
    _send(core0, _command("write-config", "cfg-wr-reboot",
                          payload={"config": reboot_candidate}))
    assert core0._config_manager.reboot_required is True
    del lines[:]

    # The currently committed configuration, submitted again verbatim.
    _send(core0, _command("write-config", "cfg-wr-pending-unchanged",
                          payload={"config": _committed(tmp_path)}))

    response = _last_response(core0)
    assert response["success"] is True
    assert response["data"]["configuration_changed"] is False
    assert response["data"]["classification"] == "UNCHANGED"
    assert response["data"]["reboot_required"] is True
    assert response["data"]["changes"] == []
    assert core0._config_manager.reboot_required is True
    assert any("reboot-required configuration remains pending" in line
               for line in lines)


# --- write-config: changed writes (all REBOOT_REQUIRED) --------------------------


def test_write_config_changed_scalar_keys_commit_pending_reboot(make_core0, tmp_path):
    """Any changed key is REBOOT_REQUIRED: committed, no live apply, no
    automatic reboot, the change summary naming each setting with old/new
    values, sorted."""
    core0 = make_core0()
    candidate = _full_config()
    candidate["read_loop_sec"] = 40
    candidate["health_interval_sec"] = 120

    _send(core0, _command("write-config", "cfg-wr-changed",
                          payload={"config": candidate}))

    response = _last_response(core0)
    assert response["success"] is True
    assert response["data"]["configuration_changed"] is True
    assert response["data"]["classification"] == "REBOOT_REQUIRED"
    assert response["data"]["reboot_required"] is True
    # No automatic reboot: the explicit reboot command is still the only
    # way the board resets.
    assert core0._pending_reboot is None
    # No live apply: Core 0's own configuration keeps its boot values.
    assert core0._config["mqtt_command_poll_ms"] == 100
    # Committed, steady state
    assert core0._intercore.event_queue.events == []
    assert _committed(tmp_path) == candidate
    assert not (tmp_path / "config.json.old").exists()
    assert not (tmp_path / "config.json.tmp").exists()
    assert core0._config_manager.reboot_required is True
    # The change summary names both settings with old/new values, sorted
    assert response["data"]["changes"] == [
        {
            "setting": "health_interval_sec",
            "original_value": 60,
            "new_value": 120,
        },
        {
            "setting": "read_loop_sec",
            "original_value": 20,
            "new_value": 40,
        },
    ]


def test_write_config_same_candidate_twice_is_unchanged(make_core0, tmp_path):
    core0 = make_core0()
    candidate = _full_config()
    candidate["read_loop_sec"] = 40
    _send(core0, _command("write-config", "cfg-wr-first",
                          payload={"config": candidate}))
    assert _last_response(core0)["success"] is True

    _send(core0, _command("write-config", "cfg-wr-second",
                          payload={"config": candidate}))

    response = _last_response(core0)
    assert response["success"] is True
    assert response["data"]["classification"] == "UNCHANGED"
    assert response["data"]["changes"] == []
    assert response["data"]["reboot_required"] is True
    assert core0._intercore.event_queue.events == []


# --- write-config: REBOOT_REQUIRED ----------------------------------------------


def test_write_config_reboot_required_commits_without_applying(make_core0, tmp_path):
    core0 = make_core0()
    candidate = _full_config()
    candidate["source"] = "Other-Pico"
    candidate["read_loop_sec"] = 40  # a Core 1 scheduling key in the same write

    _send(core0, _command("write-config", "cfg-wr-reboot",
                          payload={"config": candidate}))

    response = _last_response(core0)
    assert response["success"] is True
    assert response["data"]["configuration_changed"] is True
    assert response["data"]["classification"] == "REBOOT_REQUIRED"
    assert response["data"]["reboot_required"] is True
    # No automatic reboot: the explicit reboot command is still the only
    # way the board resets.
    assert core0._pending_reboot is None
    # No live apply: Core 0's own running configuration keeps its boot
    # values until the explicit reboot.
    assert core0._config["source"] == "Test-Pico-2"
    assert core0._config["mqtt_command_poll_ms"] == 100
    assert core0._intercore.event_queue.events == []
    # The committed config is the candidate; steady state.
    assert _committed(tmp_path) == candidate
    assert not (tmp_path / "config.json.old").exists()
    assert core0._config_manager.reboot_required is True
    # The change summary lists both settings deterministically
    settings = [c["setting"] for c in response["data"]["changes"]]
    assert settings == sorted(settings)
    assert settings == ["read_loop_sec", "source"]


def test_write_config_device_changes_are_compact_entries(make_core0, tmp_path):
    """Device additions/removals/modifications are REBOOT_REQUIRED and reported
    as bounded whole-device entries, never the full definitions."""
    core0 = make_core0()
    original = _full_config()["devices"][0]

    candidate = _full_config()
    modified = json.loads(json.dumps(original))
    modified["config"]["sea_level_pressure_pa"] = 101000
    added = {"id": "aaAddedDevice", "device_type": "bme280",
             "config": {"i2c_bus": 0, "sea_level_pressure_pa": 101325}}
    candidate["devices"] = [added, modified]  # original id stays, one added

    _send(core0, _command("write-config", "cfg-wr-devices",
                          payload={"config": candidate}))

    response = _last_response(core0)
    assert response["success"] is True
    assert response["data"]["configuration_changed"] is True
    assert response["data"]["classification"] == "REBOOT_REQUIRED"
    assert response["data"]["reboot_required"] is True
    # No live apply: no automatic reboot is armed by a device change.
    assert core0._intercore.event_queue.events == []
    assert core0._pending_reboot is None
    devices_changes = [c for c in response["data"]["changes"] if c["setting"] == "devices"]
    assert [c["device_id"] for c in devices_changes] == [
        "aaAddedDevice", original["id"],
    ]
    assert devices_changes[0] == {
        "setting": "devices",
        "change_type": "ADDED",
        "device_id": "aaAddedDevice",
        "device_type": "bme280",
    }
    assert devices_changes[1]["change_type"] == "MODIFIED"
    assert devices_changes[1]["device_type"] == original["device_type"]
    for change in response["data"]["changes"]:
        assert "original_value" not in change
        assert "new_value" not in change


def test_write_config_repeated_pending_reboot_writes(make_core0, tmp_path):
    """A second reboot-required write while one is pending reports
    persisted-before -> candidate and keeps the reboot pending."""
    core0 = make_core0()

    first = _full_config()
    first["source"] = "Other-Pico"
    _send(core0, _command("write-config", "cfg-wr-pending-1",
                          payload={"config": first}))
    assert _last_response(core0)["data"]["classification"] == "REBOOT_REQUIRED"

    second = _full_config()
    second["source"] = "Third-Pico"
    _send(core0, _command("write-config", "cfg-wr-pending-2",
                          payload={"config": second}))

    response = _last_response(core0)
    assert response["success"] is True
    assert response["data"]["configuration_changed"] is True
    assert response["data"]["classification"] == "REBOOT_REQUIRED"
    assert response["data"]["reboot_required"] is True
    source_change = [c for c in response["data"]["changes"]
                     if c["setting"] == "source"][0]
    assert source_change["original_value"] == "Other-Pico"
    assert source_change["new_value"] == "Third-Pico"
    assert _committed(tmp_path) == second
    assert core0._pending_reboot is None


def test_write_config_response_stays_on_active_mqtt_identity(make_core0, tmp_path):
    """A candidate that changes reboot-only networking fields is committed, but
    the response is answered on the currently ACTIVE source/session/topics:
    no active networking setting is mutated before the REBOOT_REQUIRED
    response is sent."""
    core0 = make_core0()
    active = _core0_config()

    candidate = _full_config()
    candidate["source"] = "Other-Pico"
    candidate["mqtt_broker_ip_address"] = "10.9.9.9"
    candidate["mqtt_topic_command_response"] = "iot/v3/other/response"
    candidate["wifi_reconnect_delays_sec"] = [5, 5]

    _send(core0, _command("write-config", "cfg-wr-active-identity",
                          payload={"config": candidate}))

    response = _last_response(core0)
    assert response["success"] is True
    assert response["data"]["classification"] == "REBOOT_REQUIRED"
    # The live Core 0 networking configuration is untouched by the write.
    for key in (
        "source",
        "mqtt_broker_ip_address",
        "mqtt_topic_command_response",
        "wifi_reconnect_delays_sec",
    ):
        assert core0._config[key] == active[key]
    assert _committed(tmp_path) == candidate
    assert core0._config_manager.reboot_required is True


# --- write-config: invalid candidates ---------------------------------------------


def test_write_config_unknown_payload_fields_are_named_and_sorted(make_core0, tmp_path):
    """Unknown payload keys are returned together (sorted) without any
    configuration validation or filesystem operation."""
    core0 = make_core0()
    before = (tmp_path / "config.json").read_text()

    _send(core0, _command("write-config", "cfg-wr-bad-payload", payload={
        "config": _full_config(),
        "merge": True,
        "restart": 1,
    }))

    response = _last_response(core0)
    assert response["success"] is False
    assert response["error"]["code"] == "unknown_fields"
    assert response["error"]["message"] == "write-config payload contains unknown fields"
    assert response["error"]["unknown_fields"] == ["merge", "restart"]
    assert (tmp_path / "config.json").read_text() == before
    assert core0._intercore.event_queue.events == []
    assert core0._config_manager.reboot_required is False


def test_write_config_only_unknown_payload_fields_is_rejected(make_core0, tmp_path):
    core0 = make_core0()
    before = (tmp_path / "config.json").read_text()

    _send(core0, _command("write-config", "cfg-wr-bad-payload2", payload={
        "patch": {"source": "Other-Pico"},
    }))

    response = _last_response(core0)
    assert response["success"] is False
    assert response["error"]["code"] == "unknown_fields"
    assert response["error"]["unknown_fields"] == ["patch"]
    assert (tmp_path / "config.json").read_text() == before


def test_write_config_missing_config_key_is_rejected(make_core0, tmp_path):
    core0 = make_core0()
    before = (tmp_path / "config.json").read_text()

    _send(core0, _command("write-config", "cfg-wr-bad-nocfg", payload={}))

    response = _last_response(core0)
    assert response["success"] is False
    assert response["error"]["code"] == "missing_key"
    assert "config" in response["error"]["message"]
    assert (tmp_path / "config.json").read_text() == before


def test_write_config_non_object_config_is_rejected(make_core0, tmp_path):
    core0 = make_core0()
    before = (tmp_path / "config.json").read_text()

    _send(core0, _command("write-config", "cfg-wr-bad-cfgtype",
                          payload={"config": [1, 2, 3]}))

    response = _last_response(core0)
    assert response["success"] is False
    assert response["error"]["code"] == "invalid_value"
    assert (tmp_path / "config.json").read_text() == before


def test_write_config_unknown_config_fields_are_named_and_sorted(make_core0, tmp_path):
    core0 = make_core0()
    before = (tmp_path / "config.json").read_text()
    candidate = _full_config()
    candidate["zzz_option"] = 1
    candidate["aaa_option"] = 2

    _send(core0, _command("write-config", "cfg-wr-bad-unknown",
                          payload={"config": candidate}))

    response = _last_response(core0)
    assert response["success"] is False
    assert response["error"]["code"] == "unknown_config_fields"
    assert response["error"]["message"] == "Configuration contains unknown fields"
    assert response["error"]["unknown_fields"] == ["aaa_option", "zzz_option"]
    assert (tmp_path / "config.json").read_text() == before
    assert core0._config_manager.reboot_required is False


def test_write_config_unknown_device_fields_are_qualified(make_core0, tmp_path):
    core0 = make_core0()
    before = (tmp_path / "config.json").read_text()
    candidate = _full_config()
    candidate["top_level_typo"] = 1
    candidate["devices"][0]["bad_option"] = 1

    _send(core0, _command("write-config", "cfg-wr-bad-devunknown",
                          payload={"config": candidate}))

    response = _last_response(core0)
    assert response["success"] is False
    assert response["error"]["code"] == "unknown_config_fields"
    assert response["error"]["unknown_fields"] == [
        "devices[{}].bad_option".format(candidate["devices"][0]["id"]),
        "top_level_typo",
    ]
    assert (tmp_path / "config.json").read_text() == before


def test_write_config_missing_key_is_rejected(make_core0, tmp_path):
    core0 = make_core0()
    candidate = _full_config()
    del candidate["source"]

    _send(core0, _command("write-config", "cfg-wr-bad-missing",
                          payload={"config": candidate}))

    response = _last_response(core0)
    assert response["success"] is False
    assert response["error"]["code"] == "missing_key"


def test_write_config_wrong_schema_version_is_rejected(make_core0, tmp_path):
    core0 = make_core0()
    candidate = _full_config()
    candidate["config_schema_version"] = 6

    _send(core0, _command("write-config", "cfg-wr-bad-version",
                          payload={"config": candidate}))

    response = _last_response(core0)
    assert response["success"] is False
    assert response["error"]["code"] == "invalid_config_schema_version"
    assert response["error"]["message"] == "Unsupported config_schema_version"
    assert response["error"]["expected"] == CONFIG_SCHEMA_VERSION
    assert response["error"]["received"] == 6


def test_write_config_non_object_payload_is_rejected_by_common_contract(make_core0):
    core0 = make_core0()

    _send(core0, _command("write-config", "cfg-wr-bad-shape", payload=[1, 2, 3]))

    response = _last_response(core0)
    assert response["success"] is False
    assert response["error"]["code"] == "invalid_payload"
