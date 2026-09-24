# config.py - Configuration loading and validation
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import json

from command_protocol import MAX_SOURCE_LENGTH
from device_factory import (
    DEVICE_DEFINITION_KEYS,
    _validation_module,
    allowed_config_keys,
    i2c_bus_identity,
    validate_device_definition,
)
from devices.device import DeviceValidationError
from devices.rp2_i2c import effective_rp2_i2c_pins
from version import CONFIG_SCHEMA_VERSION

# Keep Alive is a 16-bit word in CONNECT: above 65535 s the packet is
# unrepresentable. The key is reboot-required, so a rejected write would
# brick the MQTT channel used to fix it.
MAX_MQTT_KEEPALIVE_SEC = 65535

# The ping interval floors to 1 s (max(keepalive // 2, 1)) and each
# PINGREQ/PINGRESP exchange re-stamps activity AFTER the response, so the
# broker-visible traffic gap is interval + RTT and must stay inside the
# broker's 1.5 x keepalive tolerance. 5 s is the smallest value where
# interval + realistic link RTT clears that tolerance; below it the broker
# disconnects a healthy client into a reconnect flap.
MIN_MQTT_KEEPALIVE_SEC = 5

# subscribe() encodes the SUBSCRIBE Remaining Length in one byte (valid
# through 127); the body is topic + 5, so above 122 topic bytes the length
# byte gains the continuation bit and the subscription never completes. Both
# subscribed topics are the repair channel for a bad configuration, so the
# bound sits at the config boundary and applies to every topic.
MAX_MQTT_TOPIC_BYTES = 122

# The Wi-Fi secrets sit in config-secrets.json (boot provisioning, not
# write-config) and feed network.WLAN.connect(). The SSID bound is IEEE
# 802.11's 32-octet limit; the password covers a 63-character WPA2-PSK
# passphrase and a 64-hex raw PSK. Measured in UTF-8 bytes like the other
# protocol-scale strings: a misprovisioned file must fail fast here instead
# of deep in the Wi-Fi retry machinery.
MAX_WIFI_SSID_BYTES = 32
MAX_WIFI_PASSWORD_BYTES = 64

# The RP2 builds give time.ticks_* 30-bit values: ticks_diff only expresses
# deltas below half the period (2^29 - 1 ms, ~6.21 days) and ticks_add raises
# OverflowError at it. Any value that becomes a ticks_diff threshold or
# ticks_add delta must stay under this ceiling, or the threshold can never be
# reached (or the deadline/re-anchor raises) -- hence the bound.
MAX_TICKS_SAFE_INTERVAL_MS = (1 << 29) - 1

# Retries ride out a transient driver.initialize() failure; a device that
# fails all of them is broken, so a handful is enough (shipped: 3). Above
# this, a misconfigured value stalls startup for attempts x retry delay.
MAX_DEVICE_INITIALIZATION_ATTEMPTS = 10

# Reconnect delays are bounded for operational liveness, not the ticks
# ceiling (they only drive sliced sleeps): a delay or sequence long enough to
# loop for days makes recovery indistinguishable from a hang.
MAX_RECONNECT_DELAY_SEC = 600
MAX_RECONNECT_ATTEMPTS = 32

# Operational liveness bounds — in contrast to MAX_TICKS_SAFE_INTERVAL_MS
# above, a representability bound: these stop a representable value from
# defeating recovery. Shipped values sit well below every one of these.

# Scales every bounded MQTT wait (the CONNACK/SUBACK handshake, the PUBACK,
# check_msg completion, the UTC request deadline). Each single wait must stay
# under the Core 0 hardware watchdog budget (core0.py WDT_TIMEOUT_MS, 8 s),
# so a stalled link times out on its own before the watchdog can fire.
MAX_MQTT_BROKER_RESPONSE_TIMEOUT_SEC = 5

# Startup-only (the network probe runs before the watchdog arms): bounds how
# long one verification attempt can hold the device out of service.
MAX_NETWORK_PROBE_TIMEOUT_SEC = 30

# Paces Core 0's startup publishes: _wait_for_mqtt_publish_slot blocks for up
# to the full delay between them, in 10 ms slices, and startup is deliberately
# unsupervised (no watchdog, no heartbeat) — so only the config bound can
# keep a multi-day value from holding startup in a paced stall.
MAX_MQTT_OUTBOUND_PUBLISH_DELAY_MS = 60 * 1000

# Core 0's run loop pumps inbound commands (check_msg) only when this
# interval has elapsed: a huge but schema-valid value leaves an apparently
# healthy device (telemetry, health, keepalive, and the watchdog all
# normal) that silently stops servicing commands — including the
# write-config command that would repair the value. An operational liveness
# bound on command-repair channel latency, not a ticks-width limit.
MAX_MQTT_COMMAND_POLL_MS = 10 * 1000

# Core 1's initialization retry pacing (a sliced sleep that refreshes the
# liveness stamp, so no ticks or watchdog pressure): purely operational.
MAX_DEVICE_INITIALIZATION_RETRY_DELAY_MS = 60 * 1000

# Consecutive read failures before a device reinit: above this, automatic
# reinit is effectively disabled for any realistic read loop.
MAX_DEVICE_READ_FAILURE_THRESHOLD = 1000

# Core 1 builds a per-device status structure before anything can be rejected
# at the serialized-size ceiling (the bounded startup-log fallback and the
# read-config response both carry it). The string fields are byte-bounded, so
# at this device count a worst-case valid configuration stays under
# MAX_OUTBOUND_MESSAGE_BYTES (16 KiB) serialized.
MAX_DEVICES = 16

# Deterministic ceiling on the number of outbound-queue entries retained
# (queued + in-flight). An observability/stability bound, NOT a memory bound:
# the heap policy (preferred reserve / hard floor) remains the memory guard
# and is evaluated first, so a memory-constrained board is limited by heap
# pressure well before this count. The ceiling is the maximum a config may
# set (1..256), not the maximum the queue can ever hold.
MAX_OUTBOUND_QUEUE_MAX_MESSAGES = 256


class ConfigError(Exception):
    """Configuration load/validation failure with a stable machine-readable
    code: ``code`` and ``unknown_fields`` let the command handler answer with
    a stable cause without parsing the message; ``details`` is a small flat
    key/value map merged as-is (e.g. expected/received schema versions).
    ``code`` is None only outside this module."""

    def __init__(self, message, code=None, unknown_fields=None, details=None):
        super().__init__(message)
        self.code = code
        self.unknown_fields = unknown_fields
        self.details = details


class WifiConfigError(Exception):
    pass


_REQUIRED_KEYS = (
    "config_schema_version",
    "source",
    "read_loop_sec",
    "device_initialization_attempts",
    "device_initialization_retry_delay_ms",
    "device_read_failure_threshold",
    "devices",
    "mqtt_broker_ip_address",
    "mqtt_keepalive_sec",
    "mqtt_command_poll_ms",
    "mqtt_outbound_publish_delay_ms",
    "mqtt_broker_response_timeout_sec",
    "datetime_sync_interval_min",
    "network_snapshot_interval_sec",
    "network_probe_timeout_sec",
    "mqtt_topic_telemetry",
    "mqtt_topic_log",
    "mqtt_topic_command",
    "mqtt_topic_command_response",
    "mqtt_topic_info_request",
    "mqtt_topic_info_response",
    "mqtt_topic_network_probe",
    "mqtt_topic_health",
    "health_interval_sec",
    "wifi_reconnect_delays_sec",
    "mqtt_reconnect_delays_sec",
    "outbound_queue_max_messages",
)

_ALLOWED_KEYS = frozenset(_REQUIRED_KEYS)

# The eight protocol channels. Topic identity is the inbound routing
# invariant (dispatch matches delivered topics by exact equality), so the
# set also drives the pairwise-distinctness rule below.
_MQTT_TOPIC_KEYS = (
    "mqtt_topic_telemetry",
    "mqtt_topic_log",
    "mqtt_topic_command",
    "mqtt_topic_command_response",
    "mqtt_topic_info_request",
    "mqtt_topic_info_response",
    "mqtt_topic_network_probe",
    "mqtt_topic_health",
)


def _unknown_config_paths(config):
    """All unknown configuration paths, sorted: top-level keys,
    device-definition keys, and (for a supported type) device-config keys,
    qualified ``devices[<id>].<key>`` / ``devices[<id>].config.<key>`` (the
    index when the entry has no usable id). Runs before any other check, so
    every unknown field is reported together in one sorted array."""
    unknown = set(config.keys()) - _ALLOWED_KEYS
    devices = config.get("devices")
    if isinstance(devices, list):
        for index, device in enumerate(devices):
            if not isinstance(device, dict):
                continue
            device_id = device.get("id")
            qualifier = device_id if isinstance(device_id, str) and device_id else str(index)
            unknown.update(
                "devices[{}].{}".format(qualifier, key)
                for key in set(device.keys()) - DEVICE_DEFINITION_KEYS
            )
            # The device config dict is driver-owned; only a supported type has
            # a known allowed-key set to check against.
            device_config = device.get("config")
            if isinstance(device_config, dict):
                allowed = allowed_config_keys(device.get("device_type"))
                if allowed is not None:
                    unknown.update(
                        "devices[{}].config.{}".format(qualifier, key)
                        for key in set(device_config) - allowed
                    )
    return sorted(unknown)


def _read_json(path, error_type):
    try:
        with open(path, "r") as handle:
            value = json.load(handle)
    except MemoryError:
        raise
    except (OSError, ValueError) as err:
        raise error_type("Unable to read {}: {}".format(path, err))

    if not isinstance(value, dict):
        raise error_type("{} must contain a JSON object".format(path))
    return value


def _require_non_empty_string(config, key):
    value = config[key]
    if not isinstance(value, str) or not value:
        raise ConfigError(
            "{} must be a non-empty string".format(key), code="invalid_value"
        )


def _require_mqtt_topic(config, key):
    """A configured topic must be 1..MAX_MQTT_TOPIC_BYTES ASCII bytes with no
    NUL (see the bound's comment); ASCII-only keeps byte length equal to
    character count for the single-byte Remaining Length arithmetic.
    Wildcards (+/#) are invalid in a PUBLISH Topic Name, and inbound dispatch
    matches delivered topics by exact equality."""
    value = config[key]
    if not isinstance(value, str) or not value:
        raise ConfigError(
            "{} must be a non-empty string".format(key), code="invalid_value"
        )
    if len(value) > MAX_MQTT_TOPIC_BYTES or any(
        ch < "\u0001" or ch > "\u007f" or ch in "+#" for ch in value
    ):
        raise ConfigError(
            "{} must be 1-{} ASCII bytes with no NUL or wildcard (+/#)".format(
                key, MAX_MQTT_TOPIC_BYTES
            ),
            code="invalid_value",
        )


def _require_ipv4_address(config, key):
    """A numeric IPv4 dotted quad in canonical form (four dot-separated
    parts, each 1-3 ASCII digits, no leading zero, value 0-255). The
    handshake's getaddrinfo() lookup runs OUTSIDE the socket timeout and
    only a literal parses without a DNS query: a hostname's query could
    stretch past the 8 s Core 0 watchdog instead of failing the attempt
    into the bounded reconnect path. A leading zero is ambiguous across
    resolvers; IPv6 is outside the AF_INET/SOCK_STREAM lookup profile."""
    value = config[key]
    parts = value.split(".")
    if len(parts) != 4 or any(
        not 1 <= len(part) <= 3
        or any(ch < "0" or ch > "9" for ch in part)
        or (len(part) > 1 and part[0] == "0")
        or int(part) > 255
        for part in parts
    ):
        raise ConfigError(
            "{} must be a numeric IPv4 address (dotted quad)".format(key),
            code="invalid_value",
        )


def _require_distinct_mqtt_topics(config):
    # Inbound dispatch matches by exact equality and the first matching
    # branch wins: a shared name silently disables one channel (equal
    # command/info_response names make the command path unreachable on a
    # healthy-looking device) or self-echoes the device's own publications.
    seen = {}
    for key in _MQTT_TOPIC_KEYS:
        value = config[key]
        owner = seen.get(value)
        if owner is not None:
            raise ConfigError(
                "Duplicate MQTT topic '{}' used by {} and {}".format(
                    value, owner, key
                ),
                code="invalid_value",
            )
        seen[value] = key


def _require_positive_integer(config, key):
    value = config[key]
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(
            "{} must be a positive integer".format(key), code="invalid_value"
        )


def _require_ticks_safe_interval(config, key, ms_per_unit):
    """value * ms_per_unit must fit a MicroPython ticks delta (see MAX_TICKS_SAFE_INTERVAL_MS)."""
    value = config[key]
    max_value = MAX_TICKS_SAFE_INTERVAL_MS // ms_per_unit
    if value * ms_per_unit > MAX_TICKS_SAFE_INTERVAL_MS:
        raise ConfigError(
            "{} must be at most {} (MicroPython ticks intervals are limited "
            "to {} ms)".format(key, max_value, MAX_TICKS_SAFE_INTERVAL_MS),
            code="invalid_value",
        )


def _require_nonnegative_integer(config, key):
    value = config[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError(
            "{} must be a non-negative integer".format(key), code="invalid_value"
        )


def _validate_delays(config, key):
    delays = config[key]
    if not isinstance(delays, list) or not delays:
        raise ConfigError(
            "{} must be a non-empty list".format(key), code="invalid_value"
        )
    if len(delays) > MAX_RECONNECT_ATTEMPTS:
        raise ConfigError(
            "{} must contain at most {} entries".format(
                key, MAX_RECONNECT_ATTEMPTS
            ),
            code="invalid_value",
        )
    for index, delay in enumerate(delays):
        if isinstance(delay, bool) or not isinstance(delay, int) or delay < 0:
            raise ConfigError(
                "{}[{}] must be a non-negative integer".format(key, index),
                code="invalid_value",
            )
        if delay > MAX_RECONNECT_DELAY_SEC:
            raise ConfigError(
                "{}[{}] must be at most {} seconds".format(
                    key, index, MAX_RECONNECT_DELAY_SEC
                ),
                code="invalid_value",
            )


def _validate_devices(devices):
    if not isinstance(devices, list) or not devices:
        raise ConfigError("devices must be a non-empty list", code="invalid_value")
    if len(devices) > MAX_DEVICES:
        raise ConfigError(
            "devices must contain at most {} entries".format(MAX_DEVICES),
            code="invalid_value",
        )

    seen_ids = set()
    for index, device in enumerate(devices):
        if not isinstance(device, dict):
            raise ConfigError(
                "devices[{}] must be an object".format(index), code="invalid_value"
            )

        # Duplicate ids span the whole list, so they are checked here before
        # the per-device pure validation.
        device_id = device.get("id")
        if isinstance(device_id, str) and device_id:
            if device_id in seen_ids:
                raise ConfigError(
                    "Duplicate device id: {}".format(device_id), code="invalid_value"
                )
            seen_ids.add(device_id)

        # Generic shape, supported device_type, and device-specific config are
        # pure and live in device_factory's registry; a DeviceValidationError
        # maps onto a ConfigError with its stable code.
        try:
            validate_device_definition(device)
        except DeviceValidationError as err:
            raise ConfigError(str(err), code=err.code) from err

    # Same-bus conflicts span the whole list, like duplicate ids above: on
    # the RP2 port machine.I2C(bus) configures the physical controller
    # itself, so a second construction with a different pin or clock setting
    # would silently reconfigure the controller under the first device
    # (flaky reads that look like a failing sensor, not a configuration
    # error). The effective settings must be identical within a bus.
    bus_owners = {}
    for index, device in enumerate(devices):
        identity = i2c_bus_identity(device)
        if identity is None:
            continue
        bus, setting = identity[0], identity[1:]
        device_id = device.get("id")
        qualifier = device_id if isinstance(device_id, str) and device_id else str(index)
        owner = bus_owners.get(bus)
        if owner is None:
            bus_owners[bus] = (qualifier, setting)
        elif owner[1] != setting:
            raise ConfigError(
                "Conflicting I2C configuration on i2c_bus {}: devices '{}' "
                "and '{}' must use identical i2c_sda_pin / i2c_scl_pin / "
                "i2c_freq_hz (one bus is one physical controller, and a "
                "second construction reconfigures it under the first device)"
                .format(bus, owner[0], qualifier),
                code="invalid_value",
            )

    # Cross-bus GPIO overlap spans the whole list, like the same-bus rule
    # above: one GPIO cannot carry two bus protocols (a 1-Wire data pin on an
    # I2C SDA/SCL pin would drive the same line in two protocols at once --
    # flaky reads on both sensors). An omitted I2C pin still drives a physical
    # line at runtime (the port default for that bus), so each side is resolved
    # to the GPIO it actually uses before the overlap check -- a 1-Wire device
    # on a bus's default SDA/SCL is the same collision as one on an explicit
    # pin. Two ds18b20 devices on the same pin remain legal -- a 1-Wire
    # multidrop bus.
    onewire_owners = {}
    for index, device in enumerate(devices):
        if device.get("device_type") != "ds18b20":
            continue
        pin = device["config"]["pin"]
        device_id = device.get("id")
        qualifier = device_id if isinstance(device_id, str) and device_id else str(index)
        onewire_owners.setdefault(pin, qualifier)

    # GPIO ownership for the yl69_fc28 soil sensor spans the whole list,
    # like the rules above: none of its three pins (the AO analog input,
    # the DO comparator input, the power-switch control) can be shared --
    # each carries a distinct signal, and one of them driven onto another
    # device's line (or a bus line) would read as flaky readings on both
    # sensors, not as a configuration error. Two devices may each use a
    # DISTINCT ADC-capable pin (GP26/27/28 are three channels of the one
    # ADC peripheral); the same pin is never legal, unlike 1-Wire
    # multidrop. Per-device validation (type, the within-device
    # distinctness) already ran, so the pins here are validated ints.
    soil_pin_owners = {}
    for index, device in enumerate(devices):
        if device.get("device_type") != "yl69_fc28":
            continue
        device_config = device["config"]
        device_id = device.get("id")
        qualifier = device_id if isinstance(device_id, str) and device_id else str(index)
        for pin_key in ("adc_pin", "digital_pin", "power_pin"):
            if pin_key not in device_config:
                continue
            pin = device_config[pin_key]
            owner = soil_pin_owners.get(pin)
            if owner is not None:
                raise ConfigError(
                    "Conflicting GPIO pin {}: device '{}' (yl69_fc28 {}) and "
                    "device '{}' (yl69_fc28 {}) share one pin; one GPIO "
                    "cannot carry two signals"
                    .format(pin, owner[0], owner[1], qualifier, pin_key),
                    code="invalid_value",
                )
            onewire_owner = onewire_owners.get(pin)
            if onewire_owner is not None:
                raise ConfigError(
                    "Conflicting GPIO pin {}: device '{}' (yl69_fc28 {}) and "
                    "device '{}' (1-Wire DQ) share one pin; one GPIO cannot "
                    "carry two signals"
                    .format(pin, qualifier, pin_key, onewire_owner),
                    code="invalid_value",
                )
            soil_pin_owners[pin] = (qualifier, pin_key)

    for index, device in enumerate(devices):
        device_config = device.get("config")
        if not isinstance(device_config, dict) or "i2c_bus" not in device_config:
            continue
        device_id = device.get("id")
        qualifier = device_id if isinstance(device_id, str) and device_id else str(index)
        # The bus is already a validated 0/1 (the per-device pure validation
        # ran first), so the default-pin mapping is in range. An explicit pin
        # resolves to itself; an omitted pin resolves to the default the
        # runtime drives, so an explicit non-default routing does not reserve
        # an unused default.
        effective_sda, effective_scl = effective_rp2_i2c_pins(
            device_config["i2c_bus"],
            device_config.get("i2c_sda_pin"),
            device_config.get("i2c_scl_pin"),
        )
        for pin_key, effective_pin in (
            ("i2c_sda_pin", effective_sda),
            ("i2c_scl_pin", effective_scl),
        ):
            owner = onewire_owners.get(effective_pin)
            if owner is not None:
                raise ConfigError(
                    "Conflicting GPIO pin {}: device '{}' (I2C {}) and device "
                    "'{}' (1-Wire DQ) share one pin; a GPIO cannot carry two bus "
                    "protocols"
                    .format(effective_pin, qualifier, pin_key, owner),
                    code="invalid_value",
                )
            soil_owner = soil_pin_owners.get(effective_pin)
            if soil_owner is not None:
                raise ConfigError(
                    "Conflicting GPIO pin {}: device '{}' (I2C {}) and device "
                    "'{}' (yl69_fc28 {}) share one pin; one GPIO cannot carry "
                    "two signals"
                    .format(effective_pin, qualifier, pin_key, soil_owner[0], soil_owner[1]),
                    code="invalid_value",
                )

    # Physical-sensor uniqueness spans the whole list: a logical device id is
    # unique, but two definitions can still name the same physical I2C chip.
    # The LTR390 has a fixed address (0x53), so a second LTR390 on one bus
    # necessarily targets the same directly-attached sensor. The BME280 and
    # the SHT35 each probe their candidate list in order and bind the first
    # responder, so with more than one such sensor of one type on a bus a
    # list naming more than one address (including the two-address default)
    # is ambiguous -- it may bind either address -- and two devices naming
    # the same single address target the same chip. Either way one physical
    # sensor would publish under two device ids (plausible telemetry with the
    # wrong identity), so both are configuration errors here, not runtime
    # conditions. A single sensor of such a type on a bus keeps its full
    # candidate-list behavior (including the two-address default). The rule
    # is per type per bus: the candidate-list types never address each
    # other's chips (BME280 0x76/0x77 vs SHT35 0x44/0x45).
    candidate_address_labels = {"bme280": "BME280", "sht35": "SHT35"}
    ltr390_owners = {}
    candidate_sensors = {}
    for index, device in enumerate(devices):
        device_config = device.get("config")
        if not isinstance(device_config, dict) or "i2c_bus" not in device_config:
            continue
        device_type = device.get("device_type")
        device_id = device.get("id")
        qualifier = device_id if isinstance(device_id, str) and device_id else str(index)
        if device_type == "ltr390":
            owner = ltr390_owners.get(device_config["i2c_bus"])
            if owner is not None:
                raise ConfigError(
                    "Multiple LTR390 devices cannot share i2c_bus {} because "
                    "the sensor address is fixed: '{}' and '{}' would both "
                    "target the same chip"
                    .format(device_config["i2c_bus"], owner, qualifier),
                    code="invalid_value",
                )
            ltr390_owners[device_config["i2c_bus"]] = qualifier
        elif device_type in candidate_address_labels:
            candidate_sensors.setdefault(device_type, {}).setdefault(
                device_config["i2c_bus"], []
            ).append(
                (qualifier, device_config.get("i2c_address_candidates"))
            )

    for device_type, by_bus in candidate_sensors.items():
        # Imported only when the type is present (the residency rule: a type a
        # board does not configure must not be resident from startup).
        default_candidates = (
            _validation_module(device_type).DEFAULT_I2C_ADDRESS_CANDIDATES
        )
        label = candidate_address_labels[device_type]
        for bus, entries in by_bus.items():
            if len(entries) < 2:
                continue
            addresses = {}
            conflict = False
            for qualifier, candidates in entries:
                # Effective list: the explicit one, or the two-address default
                # the driver applies when the key is omitted.
                effective = (
                    candidates
                    if candidates is not None
                    else default_candidates
                )
                if len(effective) != 1 or effective[0] in addresses:
                    conflict = True
                    break
                addresses[effective[0]] = qualifier
            if conflict:
                raise ConfigError(
                    "Multiple {} devices on i2c_bus {} ({}) must each "
                    "specify one distinct i2c_address_candidates address"
                    .format(
                        label, bus, ", ".join(qualifier for qualifier, _ in entries)
                    ),
                    code="invalid_value",
                )


def validate_config(config):
    """Pure validation of a complete configuration dict (no filesystem, no
    hardware). Startup (``load_config``) and the write-config command both run
    candidates through this single path, so a booted config and a committed
    one are validated by exactly the same rules. Returns the validated dict;
    raises ``ConfigError`` (stable ``code``) on violation."""
    if not isinstance(config, dict):
        raise ConfigError("Config must be a JSON object", code="invalid_value")

    unknown = _unknown_config_paths(config)
    if unknown:
        raise ConfigError(
            "Configuration contains unknown fields",
            code="unknown_config_fields",
            unknown_fields=unknown,
        )

    for key in _REQUIRED_KEYS:
        if key not in config:
            raise ConfigError(
                "Missing required config key: {}".format(key), code="missing_key"
            )

    schema_version = config["config_schema_version"]
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise ConfigError(
            "config_schema_version must be an integer", code="invalid_value"
        )
    if schema_version != CONFIG_SCHEMA_VERSION:
        # A configuration invariant, not a change policy: the mismatch is
        # answered with both sides named so the sender can correct it.
        raise ConfigError(
            "Unsupported config_schema_version",
            code="invalid_config_schema_version",
            details={
                "expected": CONFIG_SCHEMA_VERSION,
                "received": schema_version,
            },
        )

    for key in ("source", "mqtt_broker_ip_address"):
        _require_non_empty_string(config, key)

    for key in _MQTT_TOPIC_KEYS:
        _require_mqtt_topic(config, key)
    _require_distinct_mqtt_topics(config)

    if len(config["source"].encode("utf-8")) > MAX_SOURCE_LENGTH:
        raise ConfigError(
            "source must be at most {} bytes".format(MAX_SOURCE_LENGTH),
            code="invalid_value",
        )

    _require_ipv4_address(config, "mqtt_broker_ip_address")

    for key in (
        "read_loop_sec",
        "device_initialization_attempts",
        "device_read_failure_threshold",
        "mqtt_keepalive_sec",
        "mqtt_command_poll_ms",
        "mqtt_broker_response_timeout_sec",
        "datetime_sync_interval_min",
        "network_snapshot_interval_sec",
        "network_probe_timeout_sec",
        "health_interval_sec",
        "outbound_queue_max_messages",
    ):
        _require_positive_integer(config, key)

    if config["mqtt_keepalive_sec"] < MIN_MQTT_KEEPALIVE_SEC:
        raise ConfigError(
            "mqtt_keepalive_sec must be at least {}".format(MIN_MQTT_KEEPALIVE_SEC),
            code="invalid_value",
        )
    if config["mqtt_keepalive_sec"] > MAX_MQTT_KEEPALIVE_SEC:
        raise ConfigError(
            "mqtt_keepalive_sec must be at most {}".format(MAX_MQTT_KEEPALIVE_SEC),
            code="invalid_value",
        )

    if config["device_initialization_attempts"] > MAX_DEVICE_INITIALIZATION_ATTEMPTS:
        raise ConfigError(
            "device_initialization_attempts must be at most {}".format(
                MAX_DEVICE_INITIALIZATION_ATTEMPTS
            ),
            code="invalid_value",
        )

    _require_nonnegative_integer(config, "device_initialization_retry_delay_ms")
    _require_nonnegative_integer(config, "mqtt_outbound_publish_delay_ms")

    if config["mqtt_broker_response_timeout_sec"] > MAX_MQTT_BROKER_RESPONSE_TIMEOUT_SEC:
        raise ConfigError(
            "mqtt_broker_response_timeout_sec must be at most {}".format(
                MAX_MQTT_BROKER_RESPONSE_TIMEOUT_SEC
            ),
            code="invalid_value",
        )
    if config["network_probe_timeout_sec"] > MAX_NETWORK_PROBE_TIMEOUT_SEC:
        raise ConfigError(
            "network_probe_timeout_sec must be at most {}".format(
                MAX_NETWORK_PROBE_TIMEOUT_SEC
            ),
            code="invalid_value",
        )
    if config["mqtt_outbound_publish_delay_ms"] > MAX_MQTT_OUTBOUND_PUBLISH_DELAY_MS:
        raise ConfigError(
            "mqtt_outbound_publish_delay_ms must be at most {}".format(
                MAX_MQTT_OUTBOUND_PUBLISH_DELAY_MS
            ),
            code="invalid_value",
        )
    if config["mqtt_command_poll_ms"] > MAX_MQTT_COMMAND_POLL_MS:
        raise ConfigError(
            "mqtt_command_poll_ms must be at most {}".format(
                MAX_MQTT_COMMAND_POLL_MS
            ),
            code="invalid_value",
        )
    if (
        config["device_initialization_retry_delay_ms"]
        > MAX_DEVICE_INITIALIZATION_RETRY_DELAY_MS
    ):
        raise ConfigError(
            "device_initialization_retry_delay_ms must be at most {}".format(
                MAX_DEVICE_INITIALIZATION_RETRY_DELAY_MS
            ),
            code="invalid_value",
        )
    if config["device_read_failure_threshold"] > MAX_DEVICE_READ_FAILURE_THRESHOLD:
        raise ConfigError(
            "device_read_failure_threshold must be at most {}".format(
                MAX_DEVICE_READ_FAILURE_THRESHOLD
            ),
            code="invalid_value",
        )
    if config["outbound_queue_max_messages"] > MAX_OUTBOUND_QUEUE_MAX_MESSAGES:
        raise ConfigError(
            "outbound_queue_max_messages must be at most {}".format(
                MAX_OUTBOUND_QUEUE_MAX_MESSAGES
            ),
            code="invalid_value",
        )

    # Values that become ticks_diff thresholds or ticks_add deltas are bounded
    # by the ticks ceiling; values that only drive sliced sleeps are not.
    _require_ticks_safe_interval(config, "read_loop_sec", 1000)
    _require_ticks_safe_interval(config, "health_interval_sec", 1000)
    _require_ticks_safe_interval(config, "network_snapshot_interval_sec", 1000)
    _require_ticks_safe_interval(config, "mqtt_broker_response_timeout_sec", 1000)
    _require_ticks_safe_interval(config, "datetime_sync_interval_min", 60 * 1000)
    _require_ticks_safe_interval(config, "mqtt_command_poll_ms", 1)
    _require_ticks_safe_interval(config, "mqtt_outbound_publish_delay_ms", 1)

    _validate_delays(config, "wifi_reconnect_delays_sec")
    _validate_delays(config, "mqtt_reconnect_delays_sec")
    _validate_devices(config["devices"])

    return config


def load_config(path="config.json"):
    try:
        config = _read_json(path, ConfigError)
    except MemoryError:
        raise
    except ConfigError as err:
        raise ConfigError(str(err), code="unreadable_file") from err
    return validate_config(config)


def load_wifi_config(path="config-secrets.json"):
    config = _read_json(path, WifiConfigError)
    unknown = set(config.keys()) - {"wifi_ssid", "wifi_password"}
    if unknown:
        raise WifiConfigError(
            "Unknown Wi-Fi config key(s): {}".format(", ".join(sorted(unknown)))
        )

    ssid = config.get("wifi_ssid")
    password = config.get("wifi_password")
    if not isinstance(ssid, str) or not ssid:
        raise WifiConfigError("wifi_ssid must be a non-empty string")
    if not isinstance(password, str):
        raise WifiConfigError("wifi_password must be a string")
    # An embedded NUL would be truncated by the C radio driver's string
    # handling: the connection would then fail with an opaque driver error
    # instead of a provisioning error that names the field.
    if "\x00" in ssid:
        raise WifiConfigError("wifi_ssid must not contain an embedded NUL")
    if "\x00" in password:
        raise WifiConfigError("wifi_password must not contain an embedded NUL")
    if len(ssid.encode("utf-8")) > MAX_WIFI_SSID_BYTES:
        raise WifiConfigError(
            "wifi_ssid must be at most {} bytes".format(MAX_WIFI_SSID_BYTES)
        )
    # The empty string stays legal: an open network has no passphrase.
    if len(password.encode("utf-8")) > MAX_WIFI_PASSWORD_BYTES:
        raise WifiConfigError(
            "wifi_password must be at most {} bytes".format(
                MAX_WIFI_PASSWORD_BYTES
            )
        )

    return {"wifi_ssid": ssid, "wifi_password": password}


def split_config(config):
    """Create the immutable-by-convention per-core startup configuration. The
    bus is not part of the per-core split: its heap-reserve bound is a board
    property owned by hardware.py and its outbound count ceiling is
    outbound_queue_max_messages, both read by main.py and passed to InterCore
    at construction."""
    core0 = {
        "source": config["source"],
        "mqtt_broker_ip_address": config["mqtt_broker_ip_address"],
        "mqtt_topic_telemetry": config["mqtt_topic_telemetry"],
        "mqtt_topic_log": config["mqtt_topic_log"],
        "mqtt_topic_command": config["mqtt_topic_command"],
        "mqtt_topic_command_response": config["mqtt_topic_command_response"],
        "mqtt_topic_info_request": config["mqtt_topic_info_request"],
        "mqtt_topic_info_response": config["mqtt_topic_info_response"],
        "wifi_reconnect_delays_sec": config["wifi_reconnect_delays_sec"],
        "mqtt_reconnect_delays_sec": config["mqtt_reconnect_delays_sec"],
        "mqtt_keepalive_sec": config["mqtt_keepalive_sec"],
        "mqtt_command_poll_ms": config["mqtt_command_poll_ms"],
        "mqtt_outbound_publish_delay_ms": config["mqtt_outbound_publish_delay_ms"],
        "mqtt_broker_response_timeout_sec": config["mqtt_broker_response_timeout_sec"],
        "datetime_sync_interval_min": config["datetime_sync_interval_min"],
        "network_snapshot_interval_sec": config["network_snapshot_interval_sec"],
        "network_probe_timeout_sec": config["network_probe_timeout_sec"],
        "mqtt_topic_network_probe": config["mqtt_topic_network_probe"],
        "mqtt_topic_health": config["mqtt_topic_health"],
    }

    core1 = {
        "read_loop_sec": config["read_loop_sec"],
        "device_initialization_attempts": config["device_initialization_attempts"],
        "device_initialization_retry_delay_ms": config["device_initialization_retry_delay_ms"],
        "device_read_failure_threshold": config["device_read_failure_threshold"],
        "health_interval_sec": config["health_interval_sec"],
        "devices": config["devices"],
    }

    return core0, core1
