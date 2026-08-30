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
from version import CONFIG_SCHEMA_VERSION


ROOT = pathlib.Path(__file__).resolve().parents[1]


def _base_config():
    return json.loads((ROOT / "config.json").read_text())


def _write(tmp_path, value):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(value))
    return str(path)


def test_config_loads_and_splits_ownership(tmp_path):
    config = load_config(_write(tmp_path, _base_config()))
    core0, core1, bus = split_config(config)

    assert "devices" not in core0
    assert "mqtt_broker_ip_address" not in core1
    assert "mqtt_topic_telemetry" not in core1
    assert "mqtt_topic_command_response" not in core1
    assert "source" not in core1  # Core 1 doesn't need source (it uses IP from network snapshot)
    assert core0["mqtt_topic_telemetry"] == config["mqtt_topic_telemetry"]
    assert core0["mqtt_topic_log"] == config["mqtt_topic_log"]
    assert core0["mqtt_topic_health"] == config["mqtt_topic_health"]
    # The outbound queue's entry ceiling is no longer user-tunable, so the bus
    # config no longer carries it; the inter-core event-queue size still does.
    assert "max_outbound_queue_entries" not in bus
    assert bus["max_intercore_event_entries"] == config["max_intercore_event_entries"]


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


def test_base_config_is_current_schema_with_generation():
    """The checked-in config.json is on the current schema and carries the
    firmware-managed generation (bumped together with CONFIG_SCHEMA_VERSION)."""
    base = _base_config()
    assert base["config_schema_version"] == CONFIG_SCHEMA_VERSION
    assert base["config_generation"] == 0


def test_config_generation_is_required(tmp_path):
    config = _base_config()
    del config["config_generation"]
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, config))


def test_config_generation_must_be_non_negative_integer(tmp_path):
    """config_generation is firmware-managed bookkeeping: a non-negative
    integer. Negatives, bools, floats, strings, and null fail fast."""
    for bad in (-1, True, False, 1.0, "0", None):
        config = _base_config()
        config["config_generation"] = bad
        with pytest.raises(ConfigError, match="config_generation"):
            load_config(_write(tmp_path, config))

    for good in (0, 1, 42):
        config = _base_config()
        config["config_generation"] = good
        loaded = load_config(_write(tmp_path, config))
        assert loaded["config_generation"] == good


def test_obsolete_outbound_queue_key_is_rejected(tmp_path):
    # max_outbound_queue_entries was removed from the config schema: the entry
    # ceiling is now a fixed internal sanity guard, so a config that still
    # carries the obsolete key is an unknown key and fails fast.
    config = _base_config()
    config["max_outbound_queue_entries"] = 16
    with pytest.raises(ConfigError, match="Unknown config key"):
        load_config(_write(tmp_path, config))


def test_duplicate_device_ids_fail_fast(tmp_path):
    config = _base_config()
    config["devices"].append(copy.deepcopy(config["devices"][0]))
    with pytest.raises(ConfigError, match="Duplicate device id"):
        load_config(_write(tmp_path, config))


def test_diagnostics_interval_rejects_invalid_values(tmp_path):
    """network_diagnostics_interval_sec must be 0 or a whole-second count in
    [60, 86400]; bools, out-of-range ints, floats, strings, and null fail
    fast (True/False are ints in Python and must be rejected explicitly)."""
    for bad in (True, False, 1, 30, 59, -60, 86401, 300.5, "300", None):
        config = _base_config()
        config["network_diagnostics_interval_sec"] = bad
        with pytest.raises(ConfigError, match="network_diagnostics_interval_sec"):
            load_config(_write(tmp_path, config))


def test_diagnostics_interval_accepts_valid_values(tmp_path):
    """0 disables active probes; 60/300/3600 are within the conservative range."""
    for good in (0, 60, 300, 3600):
        config = _base_config()
        config["network_diagnostics_interval_sec"] = good
        loaded = load_config(_write(tmp_path, config))
        assert loaded["network_diagnostics_interval_sec"] == good


def test_drain_rate_rejects_invalid_values(tmp_path):
    """mqtt_post_outage_drain_rate_per_sec must be a non-negative integer;
    negatives, bools, floats, strings, and null fail fast (True/False are
    ints in Python and must be rejected explicitly)."""
    for bad in (-1, True, False, 1.5, "5", None):
        config = _base_config()
        config["mqtt_post_outage_drain_rate_per_sec"] = bad
        with pytest.raises(
            ConfigError, match="mqtt_post_outage_drain_rate_per_sec"
        ):
            load_config(_write(tmp_path, config))


def test_drain_rate_accepts_valid_values(tmp_path):
    """0 disables the limit (the default); positive integers enable it."""
    for good in (0, 1, 5, 10, 20):
        config = _base_config()
        config["mqtt_post_outage_drain_rate_per_sec"] = good
        loaded = load_config(_write(tmp_path, config))
        assert loaded["mqtt_post_outage_drain_rate_per_sec"] == good


def test_drain_rate_goes_to_core0_only(tmp_path):
    """The drain rate is a Core 0 (network) setting; Core 1 and the bus must
    not receive it (Core 1 only reads the metrics from the snapshot)."""
    config = load_config(_write(tmp_path, _base_config()))
    core0, core1, bus = split_config(config)

    assert core0["mqtt_post_outage_drain_rate_per_sec"] == config[
        "mqtt_post_outage_drain_rate_per_sec"
    ]
    assert "mqtt_post_outage_drain_rate_per_sec" not in core1
    assert "mqtt_post_outage_drain_rate_per_sec" not in bus


def test_diagnostics_broker_flag_must_be_boolean(tmp_path):
    """network_diagnostics_broker_latency_enabled must be a real bool."""
    for bad in (1, 0, "true", None):
        config = _base_config()
        config["network_diagnostics_broker_latency_enabled"] = bad
        with pytest.raises(
            ConfigError, match="network_diagnostics_broker_latency_enabled"
        ):
            load_config(_write(tmp_path, config))

    for good in (True, False):
        config = _base_config()
        config["network_diagnostics_broker_latency_enabled"] = good
        loaded = load_config(_write(tmp_path, config))
        assert loaded["network_diagnostics_broker_latency_enabled"] is good


def test_diagnostics_keys_go_to_core0_only(tmp_path):
    """Both diagnostics keys are Core 0 (network) settings; Core 1 must not
    receive them (it only reads the shared network snapshot)."""
    config = load_config(_write(tmp_path, _base_config()))
    core0, core1, _bus = split_config(config)

    assert core0["network_diagnostics_interval_sec"] == config[
        "network_diagnostics_interval_sec"
    ]
    assert core0["network_diagnostics_broker_latency_enabled"] == config[
        "network_diagnostics_broker_latency_enabled"
    ]
    assert "network_diagnostics_interval_sec" not in core1
    assert "network_diagnostics_broker_latency_enabled" not in core1
