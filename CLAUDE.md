# CLAUDE.md — Project Guidelines for AI Assistants

## Project Overview

This is a dual-core MicroPython firmware for Raspberry Pi Pico W/Pico 2 W. Core 0 handles network operations (Wi-Fi, MQTT, UTC sync, reboot), while Core 1 handles sensors and devices. Communication occurs over a three-lane inter-core bus (two heap-governed FIFO lanes, plus the latest-value state snapshots).

### Health Messages

Core 1 periodically publishes health messages to `iot/v3/health`. The full payload contract — every field, the degradation reasons, the retention priority, and the generation rules — is documented in the "Health Message Protocol" section of `ARCHITECTURE.md`.

## Key Architectural Principles

The full rationale and failure history for each principle live in `ARCHITECTURE.md` (see the pointers).

1. **Strict Ownership**: Core 0 owns the network stack; Core 1 owns devices/sensors. No overlap.
2. **Immutability After Transfer**: Objects moved across cores are immutable by contract.
3. **Plain Data Only**: Only JSON-serializable data crosses core boundaries.
4. **Fail-Fast Configuration**: Invalid config rejects before network starts.
5. **QoS 1 MQTT**: Synchronous PUBACK required; only one message in flight.
6. **Bounded broker waits**: Every blocking MQTT wait (the CONNACK/SUBACK handshake, PUBACK, PINGRESP) is deadline-limited; a dead or blackholed link surfaces as a failed connect, ping, or publish and triggers the reconnect backoff or mid-run network recovery instead of stalling Core 0 (see "MQTT wire client").
7. **Bounded inbound packets**: An inbound frame's remaining length must not exceed `MAX_INBOUND_PACKET_BYTES` (20 KiB) before its payload is allocated, and the remaining-length field itself is bounded to four bytes; an oversized or malformed frame drops the connection instead of requesting a read that could exhaust Pico RAM. The parse peak is backstopped by the `MemoryError` → controlled-reset boundary, and a control frame no wait loop consumes aborts the connection via `_abort_corrupt_inbound` (see "Inbound packet size limit").
8. **Final recovery boundary**: Deterministic startup validation (hardware, config) fails fast and stays visible — never a reboot loop. Core 1's worker is spawned in `main.py` before the Core 0 import, and the core1 chain is imported on the main thread before the core0 import (a spawn or import that late `MemoryError`s into the silent reset boundary on the Pico W); the worker stays idle until the startup contract is verified, and `core1_main()` still runs on Core 1 itself. A worker death after the spawn is recovered by Core 0's heartbeat watchdog; an unrecoverable Core 0 exception once operational runtime has begun is a controlled `machine.reset()`, not permanent application termination. The hardware watchdog (`machine.WDT`, 8 s, armed at the end of `Core0.start()`, fed only from Core 0's own execution) recovers a Core 0 that is alive but no longer making progress — the exception boundary covers what raises, the watchdog covers what never does (see "Core 0 runtime recovery boundary" and "Core 0 hardware watchdog").
9. **Trusted-network security model**: The broker and devices are assumed to sit on a trusted private network, so MQTT command authorization is provided by network-level access control, not by any in-firmware credential or per-command policy. The broker must not be exposed to untrusted or public networks; the firmware defends the integrity and boundedness of inbound frames, not the identity of the sender — the absence of per-command authorization is an intentional trust boundary, not a gap (see "Security model").

## Critical Files

| File | Purpose |
|------|---------|
| `main.py` | Entry point, orchestrates startup sequence |
| `core0.py` | Network stack (Wi-Fi, MQTT, UTC, reboot, keepalive, recovery, Core 1 liveness watchdog, read-config/write-config command execution) |
| `core1.py` | Device lifecycle, sensor reads, telemetry, health messages, liveness heartbeat |
| `intercore.py` | Three-lane message bus (two heap-governed FIFOs and the latest-value state snapshots) |
| `device_manager.py` | Device lifecycle management (init retries, read failures, reinit, boot-failure late recovery; the `OSError`-only operational failure domain — contract errors escape to Core 1's recovery boundary) |
| `device_factory.py` | Device construction from config; the supported `device_type` registry (validation packages resolved by import at first use, never module-top); the pure whole-device validation entry point (`validate_device_definition`) |
| `devices/bme280/bme280_device.py` | `bme280` device (first hardware/I2C sensor): `BME280` register-protocol + Bosch compensation class and the `BME280Device` `Device` adapter (offsets, derived `altitude_m`); the I2C bus itself is Core 1's, built by the lazy per-device factory in `core1.py` |
| `devices/bme280/validation.py` | Pure `bme280` config validation (host-importable, no `machine`): per-device I2C bus keys (routing via the shared `devices/rp2_i2c.py`), `i2c_address_candidates`, oversampling/filter bounds, cross-field channel rules |
| `devices/ltr390/ltr390_device.py` | `ltr390` device (second hardware/I2C sensor): `LTR390` register-protocol + lux/UVI conversion class (part-nibble ID, sequential ALS/UV sampling, bounded data-ready waits, standby at end) and the `LTR390Device` `Device` adapter (offsets on converted channels only); shares the Core 1 I2C bus with the `bme280` on the shipped config |
| `devices/ltr390/validation.py` | Pure `ltr390` config validation (host-importable, no `machine`): per-device I2C bus keys (routing via the shared `devices/rp2_i2c.py`), gain/resolution/rate membership sets, the `measurement_rate_ms` ≥ ADC-conversion-time cross-field rule, `window_factor` ≥ 1.0, `offsets` |
| `devices/ds18b20/ds18b20_device.py` | `ds18b20` device (third hardware sensor, first 1-Wire): `DS18B20` register-free protocol class (bus scan + ROM identification, the two-stage convert/wait/read cycle, the −55…+125 °C range gate with the 85 °C power-on value treated as a valid temperature) and the `DS18B20Device` `Device` adapter (offset); the 1-Wire bus itself is Core 1's, built by the lazy per-pin factory in `core1.py` |
| `devices/ds18b20/validation.py` | Pure `ds18b20` config validation (host-importable, no `machine`): the data `pin` (any of the board's 30 GPIOs), the 16-hex-char `rom` (family code `28` first, case-insensitive), `conversion_ms` ≥ 750 (the 12-bit maximum conversion time the driver must cover), `offsets` |
| `devices/rp2_i2c.py` | Shared pure RP2 I2C pin-routing validation (host-importable, no `machine`): the single source of the rule that a pin is only valid for the SDA/SCL role of the controller its GPIO mux group belongs to (matching the RP2 port's `machine.I2C(...)` constructor check); used by both I2C sensor validators so a non-routable pin is a config error, not an operational device failure |
| `config.py` | Configuration schema validation (pure `validate_config`, including device validation) and per-core splitting |
| `config_manager.py` | Core 0 configuration persistence: write transactions (`.tmp`/`.old`), boot recovery; every committed change is pending a reboot (no live apply) |
| `hardware.py` | Hardware detection (Pico W/Pico 2 W) and the board heap thresholds (preferred reserve / hard floor) |
| `system_information.py` | System state snapshots (Core 1 data source for health/get-details); authoritative `SYSTEM_INFORMATION_SECTIONS` |
| `uptime.py` | Accumulated boot-relative uptime (tick-wrap-safe; both cores) |
| `message_serializer.py` | JSON-safe message validation and serialization |
| `mqtt.py` | Core 0 MQTT lifecycle (QoS 1, keepalive PINGREQ, subscriptions) |
| `mqtt_client.py` | Low-level MQTT wire protocol client |
| `network_wait.py` | Core 0's sliced-wait primitive: the long network waits (Wi-Fi/MQTT backoffs, Core 0 run-loop waits) sleep in 100 ms slices with the servicing hook invoked between slices |
| `wifi.py` | Core 0 Wi-Fi connection management |
| `led_manager.py` | Core 0 onboard LED state machine |
| `release.py` | Builds the deployable release artifact (`releases/`) |

## Development Guidelines

### Adding a New Feature

1. **Determine ownership**: Does it belong in Core 0 (network) or Core 1 (devices)?
2. **Update architecture**: Document changes in `ARCHITECTURE.md` before coding
3. **Update tests**: Add tests in `tests/` directory
4. **Update version**: Bump `FIRMWARE_VERSION` in `version.py` if user-facing

### Code Style

- 4 spaces indentation, no tabs
- `snake_case` for functions/variables, `PascalCase` for classes
- Every module starts with the three-line `#` header (file title, copyright, SPDX-License-Identifier); a module-level docstring is added only where there is substantive module-level context, otherwise docstrings live on the functions and classes whose contract needs stating
- Import order: standard library, then local modules
- Use `time.ticks_*` for all time calculations (monotonic)

### Configuration Changes

- `config_schema_version` in `version.py` must match `config.json`
- Unknown top-level keys in config are rejected (fail-fast)
- MQTT topics (`mqtt_topic_*`) are exact channel names: `+`/`#` wildcards are rejected, and the eight channels must be pairwise distinct — inbound dispatch matches delivered topics by exact equality and the first matching branch wins, so a shared name silently disables a channel
- Protocol-scale strings are bounded in **UTF-8 bytes**, not characters (the safety contract is the 16 KiB wire ceiling): `source` and device `id`/`name` at 64 bytes; the Wi-Fi secrets in `config-secrets.json` at `wifi_ssid` 32 bytes (the IEEE 802.11 SSID limit) and `wifi_password` 64 bytes (an embedded NUL is rejected in either, the empty password still legal for open networks)
- `mqtt_broker_ip_address` is a numeric IPv4 dotted quad in canonical form, not a byte-bounded string: the handshake's `getaddrinfo()` lookup runs outside the socket timeout, so a hostname's DNS query would be bounded only by lwIP's retry logic and could stretch past the 8 s watchdog instead of failing a bounded connect attempt
- Recovery-timing keys carry **operational liveness bounds**: `mqtt_broker_response_timeout_sec` at 5 s (every bounded MQTT wait it sizes must fail under the 8 s watchdog budget), `network_probe_timeout_sec` at 30 s, `device_initialization_retry_delay_ms` at 60 s, `mqtt_outbound_publish_delay_ms` at 60 s (the startup publish-slot wait it paces is unsupervised, so a multi-day value must not hold startup), `device_read_failure_threshold` at 1000, and `mqtt_keepalive_sec` floored at 5 s (below that, the broker-visible ping gap exceeds the broker's 1.5× tolerance)
- All config values validated before network starts
- A serialized-size invariant test pins the worst-case valid configuration's read-config response (envelope splice included) at or under `MAX_OUTBOUND_MESSAGE_BYTES` and its worst serialized write-config command under `MAX_INBOUND_PACKET_BYTES`, so a spec-valid command is never dropped at the wire gate (see "Configuration management" and "Inbound packet size limit")

## Common Tasks

### Adding a New Device Driver

1. Create a `devices/my_sensor/` package: `__init__.py`, a **pure** config validator module (host-importable, no `machine` — `validate_config(config)` plus its `ALLOWED_CONFIG_KEYS`, raising `DeviceValidationError` with a stable `code`), and a driver module (e.g. `my_sensor_device.py`) implementing the `Device` interface from `devices/device.py` (`initialize(config)` should call the same pure validator before touching hardware, `read()`)
2. Register the `device_type` in `device_factory.py`: add it to the `_DEVICE_REGISTRY` (pure validator + allowed config keys) **and** to `create_device()` (construction)
3. Add the package files to `REQUIRED_PACKAGES` in `release.py`
4. Register in `config.json` with unique `id`
5. Add tests in `tests/` covering the pure validation (unknown keys, invalid values, unsupported type), `initialize()` reuse, and read/reinitialization behavior

### Modifying Inter-Core Protocol

1. Update lane documentation in `ARCHITECTURE.md`
2. Update `intercore.py` with new message format
3. Update `message_protocol.py` helpers
4. Test both cores handle the change

### Adding Health Message Fields

1. Update `_build_health_payload()` in `core1.py` to include new fields
2. Update `_build_health_payload_test()` in `tests/test_health.py`
3. Add tests for new fields
4. Update `ARCHITECTURE.md` health section with new field descriptions

### MQTT Topic Changes

- Core 0 owns MQTT topics (via `config.json`)
- Core 1 never knows topic names—only message kind
- Update `mqtt_topic_*` settings in `config.json`

## Debugging Tips

### Memory Issues

Pico has 256KB RAM. Common fixes:

- Use `gc.collect()` periodically (Core 1 does this)
- Avoid large string concatenations
- Reuse dictionaries where possible

### Inter-Core Deadlocks

Check for:

- Full outbound queue blocking Core 1
- Core 0 waiting for Core 1 command response
- Circular dependencies in state mailboxes

### Connection Problems

The detailed behavior lives in `ARCHITECTURE.md`:

- "Deterministic startup sequence" — the startup contract, the LED flashing (50 ms on/50 ms off) until it is verified, and the self-healing verification passes
- "Boot when the network never appears" — the unbounded boot retries, the exhaustion warnings, and the one-time `system_startup_completed` event log with its `reset_cause`
- "Network recovery" — a failed ping or publish marks the connection disconnected; `_recover_network_if_needed` re-runs `establish_network()` (startup and recovery share one code path)
- "MQTT keepalive" — Core 0 sends PINGREQ when idle for `keepalive / 2`; a blackholed link (TCP up, no PINGRESP/PUBACK) surfaces as a failed ping or publish

### UTC Synchronization

Startup sync is mandatory and self-healing; steady-state re-sync is non-blocking, with re-requests throttled to 30 s — see "UTC time synchronization" in `ARCHITECTURE.md`.

## Testing

- `tests/` contains host-side unit tests
- Run with: `python3 -m pytest tests/` (host tests run on CPython; `tests/conftest.py` shims the MicroPython-only `gc.mem_free`)
- Tests use the canonical fixture `tests/fixtures/config.json` (stable `source: "Test-Pico-2"`) as their base configuration, never the repository's deployment `config.json` — so changing a physical device's identity (or any deployment value) cannot change the suite's pass/fail. `config.json` at the repo root is deployment-only.
- Coverage (see `tests/` for per-area detail):
  - core-ownership boundaries (AST checks) and the `main.py` startup-order invariants
  - config validation: the `device_type` registry, pure whole-device validation, the cross-device I2C bus rule, protocol-scale byte bounds, the operational liveness bounds on the recovery-timing keys (with the WDT budget invariant), and the config-boundary serialized-size invariant
  - inter-core bus semantics: heap admission against the board thresholds, the count ceiling, the retention floors, the transient/permanent rejection distinction, and the serializer `MemoryError` recovery
  - MQTT: the bounded connect/subscribe handshake, QoS 1 publish, keepalive, the inbound packet-size and QoS gates, and the boundary exception taxonomy
  - health payloads, normal-runtime-anchored scheduling, UTC synchronization, network recovery, the Core 1 liveness heartbeat and the Core 0 watchdogs, the command protocol boundary, and the configuration-manager contract
- Hardware testing requires an actual Pico device

## Hardware Notes

- **Pico W**: Wi-Fi only, 256KB RAM
- **Pico 2 W**: Wi-Fi, 520KB RAM (RP2350), faster CPU
- Onboard LED is Core 0-only via `LEDManager`
- Never call `machine.reset()` from Core 1

## Version History

When modifying the project version or making release-worthy changes, update CHANGELOG.md. Do not maintain version history in CLAUDE.md.
