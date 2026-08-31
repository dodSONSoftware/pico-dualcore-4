# CLAUDE.md — Project Guidelines for AI Assistants

## Project Overview

This is a dual-core MicroPython firmware for Raspberry Pi Pico W/Pico 2 W. Core 0 handles network operations (Wi-Fi, MQTT, UTC sync, reboot), while Core 1 handles sensors and devices. Communication occurs over a three-lane inter-core bus.

### Health Message Fields

Core 1 periodically publishes health messages to `iot/v3/health` containing diagnostic information:

**Core Fields:**
- `status`: "healthy" or "degraded"
- `degraded_reasons`: Array of degradation reasons (e.g., "wifi_not_connected", "low_free_heap")

**Hardware Fields:**
- `hardware_type`: Canonical hardware type ("pico_w" or "pico_2_w")
- `machine`: Human-readable machine identifier

**Network Fields:**
- `network_stack_ready`: Boolean indicating the full startup contract has been verified
- `wifi_connected`: Boolean indicating the Wi-Fi link is up
- `wifi_rssi_dbm`: Current Wi-Fi signal strength (dBm)
- `mqtt_connected`: Boolean indicating the MQTT broker connection is up

**Memory Fields:**
- `free_heap_bytes`: Current free heap
- `minimum_free_heap_bytes`: Configured heap reserve
- `heap_headroom_bytes`: free_heap - minimum_free_heap (may be negative)

**Core Activity Fields:**
- `core_1_active`: Boolean indicating Core 1 liveness
- `core_1_activity_age_ms`: Time since last Core 1 activity report

**Device Fields:**
- `devices_configured`: Number of configured devices
- `devices_active`: Number of active/ready devices
- `device_failures`: devices_configured - devices_active

**Queue Fields:**
The queues are heap-governed (no fixed capacity), so these are observability metrics, not utilization against a limit:
- `outbound_queue_depth`: Current queued + in-flight entries
- `outbound_queued_bytes`: Retained payload bytes (queued FIFO plus the in-flight entry)
- `outbound_queue_high_watermark`: Peak queue depth since boot
- `outbound_queue_high_watermark_bytes`: Peak retained payload bytes since boot
- `outbound_evicted`: Entries evicted under memory pressure (all kinds)
- `telemetry_evicted`: Evicted entries of the telemetry kind
- `outbound_rejected`: Admissions rejected because the free-heap reserve could not be restored

**UTC Fields:**
- `utc_valid`: Boolean indicating UTC time is valid
- `utc_sync_age_sec`: Seconds since last successful UTC sync

## Key Architectural Principles

1. **Strict Ownership**: Core 0 owns the network stack; Core 1 owns devices/sensors. No overlap.
2. **Immutability After Transfer**: Objects moved across cores are immutable by contract.
3. **Plain Data Only**: Only JSON-serializable data crosses core boundaries.
4. **Fail-Fast Configuration**: Invalid config rejects before network starts.
5. **QoS 1 MQTT**: Synchronous PUBACK required; only one message in flight.
6. **Bounded broker waits**: Every blocking MQTT wait (the CONNACK/SUBACK handshake, PUBACK, PINGRESP) is deadline-limited; a dead or blackholed link surfaces as a failed connect, ping, or publish and triggers the reconnect backoff or mid-run network recovery instead of stalling Core 0.
7. **Bounded inbound packets**: An inbound MQTT packet's remaining length must not exceed `MAX_INBOUND_PACKET_BYTES` (16 KiB) before its payload is allocated, and the remaining-length field itself is bounded to MQTT's maximum four bytes. An oversized or malformed frame drops the connection (disconnect + recovery reconnect) instead of requesting a read that could exhaust Pico RAM.

## Critical Files

| File | Purpose |
|------|---------|
| `main.py` | Entry point, orchestrates startup sequence |
| `core0.py` | Network stack (Wi-Fi, MQTT, UTC, reboot, keepalive, recovery, Core 1 liveness watchdog) |
| `core1.py` | Device lifecycle, sensor reads, telemetry, health messages, liveness heartbeat |
| `intercore.py` | Three-lane message bus implementation |
| `device_manager.py` | Device lifecycle management (init retries, read failures, reinit) |
| `device_factory.py` | Device construction from config |
| `config.py` | Configuration loading and validation |
| `hardware.py` | Hardware detection (Pico W/Pico 2 W) |
| `system_information.py` | System state snapshots (Core 1 data source) |
| `uptime.py` | Accumulated boot-relative uptime (tick-wrap-safe; both cores) |
| `message_serializer.py` | JSON-safe message validation and serialization |
| `mqtt.py` | Core 0 MQTT lifecycle (QoS 1, keepalive PINGREQ, subscriptions) |
| `mqtt_client.py` | Low-level MQTT wire protocol client |
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
- All modules must have module-level docstring and copyright header
- Import order: standard library, then local modules
- Use `time.ticks_*` for all time calculations (monotonic)

### Configuration Changes

- `config_schema_version` in `version.py` must match `config.json`
- Unknown top-level keys in config are rejected (fail-fast)
- All config values validated before network starts

## Common Tasks

### Adding a New Device Driver

1. Create a `devices/my_sensor/` package: `__init__.py` plus a driver module (e.g. `my_sensor_device.py`) implementing the `Device` interface from `devices/device.py` (`initialize(config)`, `read()`)
2. Register the `device_type` in `create_device()` in `device_factory.py`
3. Add the package files to `REQUIRED_PACKAGES` in `release.py`
4. Register in `config.json` with unique `id`
5. Add tests in `tests/` covering initialization, read, and reinitialization behavior

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

- Core 0 logs to `mqtt_topic_log` on Wi-Fi/MQTT connect
- LED flashes 50ms on/50ms off until MQTT connected
- Check Wi-Fi retry delays in config
- Core 0 sends PINGREQ when idle for `keepalive / 2`; a blackholed link (TCP up, no PINGRESP/PUBACK) surfaces as a failed ping or publish
- A failed ping or publish marks the connection disconnected; `_recover_network_if_needed` re-runs `establish_network()` (startup and recovery share one code path)

### UTC Synchronization

- Startup is mandatory and self-healing: one pass of up to 3 attempts (each waiting up to `mqtt_broker_response_timeout_sec`); a failed pass re-establishes the network and retries, and Core 1 never starts until a clean pass succeeds (a `MemoryError` still fails fast)
- Steady-state re-sync is non-blocking: the run loop sends the request, keeps a `mqtt_broker_response_timeout_sec` deadline, and never blocks on the answer
- Re-requests are throttled to 30s; a malformed-but-reachable answer re-keys the throttle to ~0.5s

## Testing

- `tests/` contains host-side unit tests
- Run with: `python3 -m pytest tests/` (host tests run on CPython; `tests/conftest.py` shims the MicroPython-only `gc.mem_free`)
- The suite covers core-ownership boundaries (AST checks), config validation, inter-core bus semantics (including the per-message byte ceiling on both outbound admission paths, `put()` and `put_with_kind()`), health payloads, normal-runtime-anchored telemetry/health scheduling, MQTT keepalive, the bounded MQTT connect/subscribe handshake, the bounded QoS 1 publish write/PUBACK exchange, the bounded MQTT reconnect cleanup (a failed client's socket is closed directly, with no DISCONNECT write into the dead link), inbound MQTT packet size limits, UTC synchronization, network recovery, the self-healing startup verification, the Core 1 liveness heartbeat, the Core 0 stale-heartbeat watchdog (including its firing inside network connect/reconnect waits), the Core 0 publish-path envelope splice, sequence identity across an ambiguous QoS 1 failure (a failed-attempt sequence is never reused by a different message and a retry preserves it), the distinction between transient and permanent outbound rejections (False vs ValueError on both admission paths), the command-response channel invariant (a permanently oversized response is answered with a small `response_too_large` error response instead of stalling the channel), the startup log's fail-fast admission (a permanent rejection is never re-submitted or waited out, while a transient one keeps its single retry), the outbound publish pacing gate (`mqtt_outbound_publish_delay_ms`: first publish immediate, interval measured from QoS 1 completion via tick-wrap-safe `ticks_diff`, 0 disables, applied to all application publish paths without blocking the Core 0 run loop or pacing keepalive, reboot held for its response slot, and startup pacing including the 5 s stabilization), and the MemoryError propagation contract on the Core 0/Core 1 message paths
- Hardware testing requires an actual Pico device

## Hardware Notes

- **Pico W**: Wi-Fi only, 256KB RAM
- **Pico 2 W**: Wi-Fi, 264KB RAM, faster CPU
- Onboard LED is Core 0-only via `LEDManager`
- Never call `machine.reset()` from Core 1

## Version History

When modifying the project version or making release-worthy changes, update CHANGELOG.md. Do not maintain version history in CLAUDE.md.
