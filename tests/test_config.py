# test_config.py - Configuration loading and validation tests
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import copy
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from command_protocol import MAX_COMMAND_ID_LENGTH, MAX_SOURCE_LENGTH
from config import (
    MAX_DEVICE_INITIALIZATION_ATTEMPTS,
    MAX_DEVICE_INITIALIZATION_RETRY_DELAY_MS,
    MAX_DEVICE_READ_FAILURE_THRESHOLD,
    MAX_DEVICES,
    MAX_MQTT_BROKER_ADDRESS_BYTES,
    MAX_MQTT_BROKER_RESPONSE_TIMEOUT_SEC,
    MAX_MQTT_KEEPALIVE_SEC,
    MAX_MQTT_TOPIC_BYTES,
    MAX_NETWORK_PROBE_TIMEOUT_SEC,
    MAX_OUTBOUND_QUEUE_MAX_MESSAGES,
    MAX_RECONNECT_ATTEMPTS,
    MAX_RECONNECT_DELAY_SEC,
    MAX_TICKS_SAFE_INTERVAL_MS,
    ConfigError,
    _MQTT_TOPIC_KEYS,
    load_config,
    split_config,
    validate_config,
)
from device_factory import MAX_DEVICE_ID_LENGTH, MAX_DEVICE_NAME_LENGTH
from message_serializer import MAX_OUTBOUND_MESSAGE_BYTES, serialize_and_validate_message
from mqtt_client import MAX_INBOUND_PACKET_BYTES
from version import FIRMWARE_VERSION, MESSAGE_SCHEMA_VERSION


ROOT = pathlib.Path(__file__).resolve().parents[1]


def _base_config():
    return json.loads((ROOT / "tests" / "fixtures" / "config.json").read_text())


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
        (lambda config: config.update({"wifi_reconnect_delays_sec": [MAX_RECONNECT_DELAY_SEC + 1]}), "invalid_value"),
        (lambda config: config.update({"wifi_reconnect_delays_sec": [5] * (MAX_RECONNECT_ATTEMPTS + 1)}), "invalid_value"),
        (lambda config: config.update({"mqtt_reconnect_delays_sec": [MAX_RECONNECT_DELAY_SEC + 1]}), "invalid_value"),
        (lambda config: config.update({"mqtt_reconnect_delays_sec": [5] * (MAX_RECONNECT_ATTEMPTS + 1)}), "invalid_value"),
        (lambda config: config.update({"devices": []}), "invalid_value"),
        (lambda config: config.update(
            {"devices": [dict(config["devices"][0], id="device-{}".format(i)) for i in range(MAX_DEVICES + 1)]}
        ), "invalid_value"),
        (lambda config: config["devices"][0].__setitem__("id", "i" * (MAX_DEVICE_ID_LENGTH + 1)), "invalid_value"),
        (lambda config: config.update({"source": "S" * 16384}), "invalid_value"),
        (lambda config: config.update({"mqtt_keepalive_sec": MAX_MQTT_KEEPALIVE_SEC + 1}), "invalid_value"),
        (lambda config: config.update({"device_initialization_attempts": MAX_DEVICE_INITIALIZATION_ATTEMPTS + 1}), "invalid_value"),
        (lambda config: config.update({"mqtt_topic_command": "t" * (MAX_MQTT_TOPIC_BYTES + 1)}), "invalid_value"),
        (lambda config: config.update({"mqtt_topic_command": "a\x00b"}), "invalid_value"),
        (lambda config: config.update({"read_loop_sec": MAX_TICKS_SAFE_INTERVAL_MS // 1000 + 1}), "invalid_value"),
        (lambda config: config.update({"mqtt_outbound_publish_delay_ms": MAX_TICKS_SAFE_INTERVAL_MS + 1}), "invalid_value"),
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


@pytest.mark.parametrize(
    "key,wildcard",
    [
        (key, wildcard)
        for key in (
            "mqtt_topic_telemetry",
            "mqtt_topic_log",
            "mqtt_topic_command",
            "mqtt_topic_command_response",
            "mqtt_topic_info_request",
            "mqtt_topic_info_response",
            "mqtt_topic_network_probe",
            "mqtt_topic_health",
        )
        for wildcard in ("+", "#")
    ],
)
def test_validate_config_rejects_wildcards_in_all_topics(key, wildcard):
    """Every configured topic is an exact protocol channel: +/# are invalid
    in a PUBLISH Topic Name and inbound dispatch matches delivered topics by
    exact equality, so a wildcard could never work for any of the eight."""
    config = _base_config()
    config[key] = "iot/v3/topic" + wildcard
    with pytest.raises(ConfigError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"


def test_validate_config_rejects_identical_command_and_info_response_topics():
    """The fatal pair: inbound dispatch checks the info_response topic first
    and returns for any other message type, so an identical command name is
    never reached — the whole command path dies (including write-config, the
    repair channel) while the device still reports healthy."""
    config = _base_config()
    config["mqtt_topic_info_response"] = config["mqtt_topic_command"]
    with pytest.raises(ConfigError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"
    assert str(excinfo.value) == "Duplicate MQTT topic '{}' used by {} and {}".format(
        config["mqtt_topic_command"],
        "mqtt_topic_command",
        "mqtt_topic_info_response",
    )


@pytest.mark.parametrize(
    "owner,other",
    [
        (owner, other)
        for index, owner in enumerate(_MQTT_TOPIC_KEYS)
        for other in _MQTT_TOPIC_KEYS[index + 1 :]
    ],
)
def test_validate_config_rejects_any_pair_of_topics_sharing_a_name(owner, other):
    """Every pair, not just the fatal one: a name shared with a locally
    published topic re-delivers every own publication inbound (MQTT 3.1.1
    self-echo), and dispatch can never disambiguate two channels by name."""
    config = _base_config()
    config[other] = config[owner]
    with pytest.raises(ConfigError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"
    assert str(excinfo.value) == "Duplicate MQTT topic '{}' used by {} and {}".format(
        config[owner], owner, other
    )


def test_validate_config_accepts_renamed_but_distinct_topics():
    """The rule is distinctness, not the shipped names: renaming one channel
    to a fresh name stays valid."""
    config = _base_config()
    config["mqtt_topic_command"] = "iot/v3/commands"
    assert validate_config(config) is config


def test_validate_config_reconnect_delay_bounds_are_inclusive():
    """The liveness bounds are inclusive: exactly MAX_RECONNECT_DELAY_SEC per
    delay and exactly MAX_RECONNECT_ATTEMPTS entries still validate; only
    values beyond them are misconfiguration."""
    config = _base_config()
    config["wifi_reconnect_delays_sec"] = [MAX_RECONNECT_DELAY_SEC]
    config["mqtt_reconnect_delays_sec"] = [5] * MAX_RECONNECT_ATTEMPTS
    assert validate_config(config) is config


def test_validate_config_source_is_bounded_at_protocol_scale():
    """source is spliced into every Core 0 outbound envelope, so it carries
    an inclusive protocol-scale bound instead of an open-ended string length.
    The ceiling is a wire bound, so the bound is UTF-8 bytes: a 64-character
    source of 4-byte code points is 256 bytes and must be rejected."""
    config = _base_config()
    config["source"] = "S" * MAX_SOURCE_LENGTH
    assert validate_config(config) is config

    # The worst serialized form under the serializer's escaped output: 4-byte
    # code points, exactly 64 UTF-8 bytes — the inclusive maximum.
    config = _base_config()
    config["source"] = "\U0001F600" * (MAX_SOURCE_LENGTH // 4)
    assert validate_config(config) is config

    config = _base_config()
    config["source"] = "S" * (MAX_SOURCE_LENGTH + 1)
    with pytest.raises(ConfigError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"
    assert str(excinfo.value) == "source must be at most {} bytes".format(
        MAX_SOURCE_LENGTH
    )

    # 64 characters but 256 UTF-8 bytes: over the bound even though it is
    # within a character-based reading of the same number.
    config = _base_config()
    config["source"] = "\U0001F600" * MAX_SOURCE_LENGTH
    with pytest.raises(ConfigError, match="source must be at most"):
        validate_config(config)


def test_validate_config_broker_address_is_byte_bounded():
    """mqtt_broker_ip_address feeds socket.connect() (which also resolves
    hostnames) and is spliced into the read-config response and connect logs;
    it had no bound at all, so one ~15 KiB value made the read-config
    response unsendable. DNS's hostname maximum is the inclusive bound, in
    UTF-8 bytes."""
    config = _base_config()
    config["mqtt_broker_ip_address"] = "b" * MAX_MQTT_BROKER_ADDRESS_BYTES
    assert validate_config(config) is config

    config = _base_config()
    config["mqtt_broker_ip_address"] = "b" * (MAX_MQTT_BROKER_ADDRESS_BYTES + 1)
    with pytest.raises(ConfigError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"
    assert str(excinfo.value) == (
        "mqtt_broker_ip_address must be at most {} bytes".format(
            MAX_MQTT_BROKER_ADDRESS_BYTES
        )
    )

    # 253 characters of 2-byte code points is 506 bytes: rejected.
    config = _base_config()
    config["mqtt_broker_ip_address"] = "\u00e9" * MAX_MQTT_BROKER_ADDRESS_BYTES
    with pytest.raises(ConfigError, match="mqtt_broker_ip_address must be at most"):
        validate_config(config)


def test_max_valid_configuration_serializes_under_the_outbound_ceiling():
    """The config-boundary invariant: the worst-case valid configuration —
    every string field at its byte bound in the worst serialized form, every
    list at its entry bound, every device at every bound — still serializes
    its read-config response (including the Core 0 envelope splice) at or
    under MAX_OUTBOUND_MESSAGE_BYTES, and the worst serialized write-config
    command carrying it (character-bounded command_id/target envelope) under
    MAX_INBOUND_PACKET_BYTES. A valid configuration must be one the firmware
    can send back AND receive back; this pins that for every currently
    supported device type, so a future field or driver string that breaks it
    fails here instead of wedging read-config on a device or dropping a
    spec-valid write-config at the wire gate."""
    # 16 x 4-byte code points: 64 UTF-8 bytes, the inclusive field maximum,
    # and the worst serialized form (each code point escapes to 12 bytes).
    field_max = "\U0001F600" * (MAX_SOURCE_LENGTH // 4)
    assert len(field_max.encode("utf-8")) == MAX_SOURCE_LENGTH

    config = _base_config()
    config["source"] = field_max
    config["mqtt_broker_ip_address"] = "b" * MAX_MQTT_BROKER_ADDRESS_BYTES
    for index, key in enumerate(_MQTT_TOPIC_KEYS):
        config[key] = "t" * (MAX_MQTT_TOPIC_BYTES - 1) + str(index)
    for key in ("wifi_reconnect_delays_sec", "mqtt_reconnect_delays_sec"):
        config[key] = [MAX_RECONNECT_DELAY_SEC] * MAX_RECONNECT_ATTEMPTS
    # ids must be pairwise distinct: 15 full code points + a unique one-char
    # ASCII suffix keeps each id at exactly 64 UTF-8 bytes.
    id_suffixes = [str(i) for i in range(10)] + ["a", "b", "c", "d", "e", "f"]
    config["devices"] = [
        {
            "id": field_max[:15] + id_suffixes[index],
            "device_type": "system-information",
            "config": {
                "include": [
                    "network", "memory", "runtime", "devices", "cpu",
                    "machine", "communications", "queues", "device_status",
                ],
            },
            "name": field_max,
        }
        for index in range(MAX_DEVICES)
    ]
    validate_config(config)

    # The read-config response shape (core0._handle_read_config_command):
    # the committed configuration plus derived reboot state.
    response = {
        "command_id": "invariant-check",
        "command": "read-config",
        "success": True,
        "targeted": True,
        "data": {"config": config, "reboot_required": False},
    }
    body = serialize_and_validate_message(response)
    assert len(body) <= MAX_OUTBOUND_MESSAGE_BYTES

    # The wire boundary is the spliced envelope, not the admitted body:
    # mirror core0._envelope_fragment() (a 24-hex-char runtime_id is the
    # unique_id + nonce shape main._runtime_id() produces) and check the
    # final length the splice would produce.
    fragment = json.dumps({
        "sequence": 0,
        "runtime_id": "0123456789abcdef01234567",
        "source": config["source"],
        "firmware_version": FIRMWARE_VERSION,
        "message_schema_version": MESSAGE_SCHEMA_VERSION,
    })[1:-1].encode("utf-8")
    spliced = len(body) - 1 + 1 + len(fragment) + 1  # body minus "}" + "," + fragment + "}"
    assert spliced <= MAX_OUTBOUND_MESSAGE_BYTES

    # Inbound half of the invariant: the largest spec-valid inbound frame is
    # a write-config command carrying this configuration, and its envelope
    # bounds are CHARACTER-based (command_protocol: a 128-character
    # command_id), so the worst serialized command must still pass
    # mqtt_client's wire gate — a gate set below it (16,384 once was)
    # disconnects on a command that validation would have accepted.
    command = {
        "message_type": "command",
        "message_schema_version": MESSAGE_SCHEMA_VERSION,
        # An executable target matches the 253-byte broker address.
        "target": config["mqtt_broker_ip_address"],
        # 128 4-byte code points: at the character bound, worst serialized form.
        "command_id": "\U0001F600" * MAX_COMMAND_ID_LENGTH,
        "command": "write-config",
        "payload": {"config": config},
    }
    # The inbound frame is serialized by the PEER, not this firmware, so it
    # is measured in the conservative wire form (ASCII-escaped, the larger of
    # the two legal serializations) and deliberately NOT run through
    # serialize_and_validate_message: a valid write-config command can
    # legitimately exceed the 16 KiB OUTBOUND ceiling, which rejects it.
    command_body = json.dumps(command).encode("utf-8")
    assert len(command_body) <= MAX_INBOUND_PACKET_BYTES


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


def test_validate_config_device_initialization_attempts_is_bounded():
    """Retries ride out a transient driver.initialize() failure; a device that
    fails them all is broken, so the count carries an inclusive upper bound
    (the shipped value 3 validates) instead of only the positive-integer lower
    bound — a large value would stall startup and grow retained init
    diagnostics on the startup-failure path."""
    config = _base_config()
    config["device_initialization_attempts"] = MAX_DEVICE_INITIALIZATION_ATTEMPTS
    assert validate_config(config) is config

    config = _base_config()
    config["device_initialization_attempts"] = MAX_DEVICE_INITIALIZATION_ATTEMPTS + 1
    with pytest.raises(ConfigError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"
    assert str(excinfo.value) == "device_initialization_attempts must be at most {}".format(
        MAX_DEVICE_INITIALIZATION_ATTEMPTS
    )


@pytest.mark.parametrize(
    "key,max_value",
    [
        ("mqtt_broker_response_timeout_sec", MAX_MQTT_BROKER_RESPONSE_TIMEOUT_SEC),
        ("network_probe_timeout_sec", MAX_NETWORK_PROBE_TIMEOUT_SEC),
        ("device_initialization_retry_delay_ms", MAX_DEVICE_INITIALIZATION_RETRY_DELAY_MS),
        ("device_read_failure_threshold", MAX_DEVICE_READ_FAILURE_THRESHOLD),
    ],
)
def test_validate_config_operational_liveness_bounds(key, max_value):
    """These keys gate recovery timing, so they carry operational liveness
    bounds on top of their type/positivity checks: a value can be
    representable (even ticks-safe) while still defeating recovery — a
    multi-day broker-response timeout, an all-day initialization retry
    delay. The inclusive maximum validates; one more is rejected with the
    stable code and exact message, and the shipped values sit well below
    every bound."""
    config = _base_config()
    config[key] = max_value
    assert validate_config(config) is config

    config = _base_config()
    config[key] = max_value + 1
    with pytest.raises(ConfigError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"
    assert str(excinfo.value) == "{} must be at most {}".format(key, max_value)


@pytest.mark.parametrize("value", [1, 32, 64, 256])
def test_validate_config_outbound_queue_max_messages_valid(value):
    """The inclusive range 1..256 validates (the shipped value is 64): a
    positive integer at or under the ceiling is accepted and preserved."""
    config = _base_config()
    config["outbound_queue_max_messages"] = value
    assert validate_config(config) is config
    assert config["outbound_queue_max_messages"] == value


@pytest.mark.parametrize(
    "bad_value",
    [0, -1, 257, 1.5, "64", True, None],
)
def test_validate_config_outbound_queue_max_messages_invalid(bad_value):
    """Anything outside the positive-integer 1..256 range is rejected with the
    stable code: zero/negative (no queue), above the ceiling, a float, a
    string, a boolean, or null."""
    config = _base_config()
    config["outbound_queue_max_messages"] = bad_value
    with pytest.raises(ConfigError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"
    assert "outbound_queue_max_messages" in str(excinfo.value)


def test_validate_config_outbound_queue_max_messages_required_under_schema_8():
    """Schema v8 makes the key required: a config missing it fails fast (the
    bus is constructed with it at boot, so there is no code-side default)."""
    config = _base_config()
    del config["outbound_queue_max_messages"]
    with pytest.raises(ConfigError, match="outbound_queue_max_messages"):
        validate_config(config)


@pytest.mark.parametrize(
    "key,ms_per_unit",
    [
        ("read_loop_sec", 1000),
        ("health_interval_sec", 1000),
        ("network_snapshot_interval_sec", 1000),
        # mqtt_broker_response_timeout_sec is NOT here: its operational
        # liveness bound (MAX_MQTT_BROKER_RESPONSE_TIMEOUT_SEC) is far tighter
        # than the ticks ceiling and is pinned in
        # test_validate_config_operational_liveness_bounds.
        ("datetime_sync_interval_min", 60 * 1000),
        ("mqtt_command_poll_ms", 1),
        ("mqtt_outbound_publish_delay_ms", 1),
    ],
)
def test_validate_config_ticks_backed_intervals_are_bounded_by_the_ticks_limit(
    key, ms_per_unit
):
    """Values that become ticks_diff thresholds or ticks_add deltas must stay
    under the RP2 ticks delta ceiling: above it the threshold can never be
    reached or the deadline raises OverflowError, and a mixed REBOOT_REQUIRED
    candidate would persist that into the next boot."""
    max_value = MAX_TICKS_SAFE_INTERVAL_MS // ms_per_unit
    assert max_value * ms_per_unit <= MAX_TICKS_SAFE_INTERVAL_MS
    assert (max_value + 1) * ms_per_unit > MAX_TICKS_SAFE_INTERVAL_MS

    config = _base_config()
    config[key] = max_value
    assert validate_config(config) is config

    config = _base_config()
    config[key] = max_value + 1
    with pytest.raises(ConfigError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"
    assert "{} must be at most {}".format(key, max_value) in str(excinfo.value)
    assert str(excinfo.value)


def test_validate_config_device_count_is_bounded():
    """The device list is a non-empty list of at most MAX_DEVICES entries: the
    bound is inclusive, unique ids at the bound validate, and one more raises
    with the stable code and exact message. The bound keeps a valid
    configuration's per-device structures (startup-log fallback included) and
    its read-config response under the message-size ceiling."""
    template = copy.deepcopy(_base_config()["devices"][0])

    config = _base_config()
    config["devices"] = [
        dict(template, id="device-{}".format(i)) for i in range(MAX_DEVICES)
    ]
    assert validate_config(config) is config

    config = _base_config()
    config["devices"] = [
        dict(template, id="device-{}".format(i)) for i in range(MAX_DEVICES + 1)
    ]
    with pytest.raises(ConfigError) as excinfo:
        validate_config(config)
    assert excinfo.value.code == "invalid_value"
    assert str(excinfo.value) == "devices must contain at most {} entries".format(
        MAX_DEVICES
    )


def test_validate_config_zero_publish_delay_stays_valid():
    """0 disables outbound pacing and must stay a valid ticks-safe value."""
    config = _base_config()
    config["mqtt_outbound_publish_delay_ms"] = 0
    assert validate_config(config) is config


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
        (lambda config: config.update(
            {"devices": [dict(config["devices"][0], id="device-{}".format(i)) for i in range(MAX_DEVICES + 1)]}
        ), "invalid_value"),
        (lambda config: config["devices"][0].__setitem__("id", "i" * (MAX_DEVICE_ID_LENGTH + 1)), "invalid_value"),
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
