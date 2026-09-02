# test_config.py - Configuration loading and validation tests
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import copy
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from command_protocol import MAX_SOURCE_LENGTH
from config import (
    MAX_MQTT_KEEPALIVE_SEC,
    ConfigError,
    load_config,
    split_config,
    validate_config,
)


ROOT = pathlib.Path(__file__).resolve().parents[1]


def _base_config():
    return json.loads((ROOT / "config.json").read_text())


def _write(tmp_path, value):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(value))
    return str(path)


def test_config_loads_and_splits_ownership(tmp_path):
    config = load_config(_write(tmp_path, _base_config()))
    core0, core1 = split_config(config)

    assert "devices" not in core0
    assert "mqtt_broker_ip_address" not in core1
    assert "mqtt_topic_telemetry" not in core1
    assert "mqtt_topic_command_response" not in core1
    assert "source" not in core1  # Core 1 doesn't need source (it uses IP from network snapshot)
    assert core0["mqtt_topic_telemetry"] == config["mqtt_topic_telemetry"]
    assert core0["mqtt_topic_log"] == config["mqtt_topic_log"]
    assert core0["mqtt_topic_health"] == config["mqtt_topic_health"]


def test_unknown_top_level_key_fails_fast(tmp_path):
    config = _base_config()
    config["unused_future_option"] = True
    with pytest.raises(ConfigError, match="Configuration contains unknown fields"):
        load_config(_write(tmp_path, config))


def test_wrong_schema_version_fails_fast(tmp_path):
    config = _base_config()
    config["config_schema_version"] = 999
    with pytest.raises(ConfigError, match="config_schema_version"):
        load_config(_write(tmp_path, config))


def test_removed_queue_capacity_keys_are_rejected(tmp_path):
    """The retired bus capacity keys are unknown config keys (schema v7)."""
    for key in ("max_outbound_queue_entries", "max_intercore_event_entries"):
        config = _base_config()
        config[key] = 16
        with pytest.raises(ConfigError, match="Configuration contains unknown fields"):
            load_config(_write(tmp_path, config))


def test_outbound_publish_delay_loaded_and_core0_owned(tmp_path):
    """mqtt_outbound_publish_delay_ms is a required Core 0-only setting."""
    config = _base_config()
    assert config["mqtt_outbound_publish_delay_ms"] == 100

    loaded = load_config(_write(tmp_path, config))
    core0, core1 = split_config(loaded)
    assert core0["mqtt_outbound_publish_delay_ms"] == 100
    assert "mqtt_outbound_publish_delay_ms" not in core1


def test_outbound_publish_delay_zero_disables_pacing(tmp_path):
    """Zero is a valid value: it means pacing is disabled."""
    config = _base_config()
    config["mqtt_outbound_publish_delay_ms"] = 0

    loaded = load_config(_write(tmp_path, config))
    core0, _core1 = split_config(loaded)
    assert core0["mqtt_outbound_publish_delay_ms"] == 0


@pytest.mark.parametrize("bad_value", [-1, True, "100", 100.0, None])
def test_outbound_publish_delay_invalid_values_rejected(tmp_path, bad_value):
    config = _base_config()
    config["mqtt_outbound_publish_delay_ms"] = bad_value

    with pytest.raises(ConfigError, match="mqtt_outbound_publish_delay_ms"):
        load_config(_write(tmp_path, config))


def test_outbound_publish_delay_required_under_schema_7(tmp_path):
    """Schema v7 makes the key required: a config missing it fails fast."""
    config = _base_config()
    del config["mqtt_outbound_publish_delay_ms"]

    with pytest.raises(ConfigError, match="mqtt_outbound_publish_delay_ms"):
        load_config(_write(tmp_path, config))


def test_duplicate_device_ids_fail_fast(tmp_path):
    config = _base_config()
    config["devices"].append(copy.deepcopy(config["devices"][0]))
    with pytest.raises(ConfigError, match="Duplicate device id"):
        load_config(_write(tmp_path, config))


# ---------------------------------------------------------------------------
# validate_config(): the pure validation path shared by startup and write-config
# ---------------------------------------------------------------------------


def test_validate_config_accepts_valid_config_without_any_file():
    """validate_config() is pure: a dict that was never on disk validates."""
    config = _base_config()
    assert validate_config(config) is config


@pytest.mark.parametrize(
    "mutate,code",
    [
        (lambda config: config.update({"unused_future_option": True}), "unknown_config_fields"),
        (lambda config: config.pop("source"), "missing_key"),
        (lambda config: config.update({"config_schema_version": 999}), "invalid_config_schema_version"),
        (lambda config: config.update({"read_loop_sec": "20"}), "invalid_value"),
        (lambda config: config.update({"wifi_reconnect_delays_sec": []}), "invalid_value"),
        (lambda config: config.update({"devices": []}), "invalid_value"),
        (lambda config: config.update({"source": "S" * 16384}), "invalid_value"),
        (lambda config: config.update({"mqtt_keepalive_sec": MAX_MQTT_KEEPALIVE_SEC + 1}), "invalid_value"),
    ],
)
def test_validate_config_rejects_bad_configs_with_stable_codes(mutate, code):
    config = _base_config()
    mutate(config)
    with pytest.raises(ConfigError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == code
    # str(err) still carries the human-readable message for startup prints
    assert str(excinfo.value)


def test_validate_config_source_is_bounded_at_protocol_scale():
    """source is spliced into every Core 0 outbound envelope, so it carries
    an inclusive protocol-scale bound instead of an open-ended string length."""
    config = _base_config()
    config["source"] = "S" * MAX_SOURCE_LENGTH
    assert validate_config(config) is config

    config = _base_config()
    config["source"] = "S" * (MAX_SOURCE_LENGTH + 1)
    with pytest.raises(ConfigError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"
    assert str(excinfo.value) == "source must be at most {} characters".format(
        MAX_SOURCE_LENGTH
    )


def test_validate_config_keepalive_is_bounded_by_the_wire_limit():
    """Keep Alive is a 16-bit word on the wire, so the inclusive maximum
    connects while the next value can never form a CONNECT packet — and
    because the key is reboot-required, accepting it would persist a
    configuration that bricks the MQTT channel used to repair it."""
    config = _base_config()
    config["mqtt_keepalive_sec"] = MAX_MQTT_KEEPALIVE_SEC
    assert validate_config(config) is config

    config = _base_config()
    config["mqtt_keepalive_sec"] = MAX_MQTT_KEEPALIVE_SEC + 1
    with pytest.raises(ConfigError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"
    assert str(excinfo.value) == "mqtt_keepalive_sec must be at most {}".format(
        MAX_MQTT_KEEPALIVE_SEC
    )


def test_validate_config_unknown_fields_are_named_and_sorted():
    config = _base_config()
    config["zzz_option"] = 1
    config["aaa_option"] = 2
    with pytest.raises(ConfigError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "unknown_config_fields"
    assert excinfo.value.unknown_fields == ["aaa_option", "zzz_option"]


def test_validate_config_unknown_device_fields_are_qualified():
    """Unknown device-entry keys are reported together with top-level unknown
    keys, qualified by device id."""
    config = _base_config()
    config["zzz_option"] = 1
    config["devices"][0]["bad_option"] = 1
    with pytest.raises(ConfigError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "unknown_config_fields"
    assert excinfo.value.unknown_fields == [
        "devices[{}].bad_option".format(config["devices"][0]["id"]),
        "zzz_option",
    ]


@pytest.mark.parametrize(
    "mutate,code",
    [
        (lambda config: config.update({"unused_future_option": True}), "unknown_config_fields"),
        (lambda config: config.pop("source"), "missing_key"),
        (lambda config: config.update({"config_schema_version": 999}), "invalid_config_schema_version"),
        (lambda config: config.update({"read_loop_sec": "20"}), "invalid_value"),
        (lambda config: config.update({"source": "S" * (MAX_SOURCE_LENGTH + 1)}), "invalid_value"),
    ],
)
def test_load_and_validate_paths_agree_on_rejections(tmp_path, mutate, code):
    """Startup and write-config must reject the same candidates the same way."""
    config = _base_config()
    mutate(config)

    with pytest.raises(ConfigError) as load_err:
        load_config(_write(tmp_path, config))
    with pytest.raises(ConfigError) as validate_err:
        validate_config(config)

    assert load_err.value.code == validate_err.value.code == code
    assert str(load_err.value) == str(validate_err.value)


def test_validate_config_rejects_non_dict():
    with pytest.raises(ConfigError) as excinfo:
        validate_config([1, 2, 3])
    assert excinfo.value.code == "invalid_value"


def test_load_config_file_errors_carry_unreadable_code(tmp_path):
    with pytest.raises(ConfigError) as excinfo:
        load_config(str(tmp_path / "does-not-exist.json"))
    assert excinfo.value.code == "unreadable_file"
