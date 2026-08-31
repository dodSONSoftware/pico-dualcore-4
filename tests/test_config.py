# test_config.py - Configuration loading and validation tests
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import copy
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from config import ConfigError, load_config, split_config


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
    with pytest.raises(ConfigError, match="Unknown config key"):
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
        with pytest.raises(ConfigError, match="Unknown config key"):
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
