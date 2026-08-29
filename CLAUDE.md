# CLAUDE.md — Project Guidelines for AI Assistants

## Project Overview

This is a dual-core MicroPython firmware for Raspberry Pi Pico W/Pico 2 W. Core 0 handles network operations (Wi-Fi, MQTT, UTC sync, reboot), while Core 1 handles sensors and devices. Communication occurs over a three-lane inter-core bus.

### Health Message Fields

Core 1 periodically publishes health messages to `iot/v3/health` containing diagnostic information:

**Core Fields:**
- `status`: "healthy" or "degraded"
- `degraded_reasons`: Array of degradation reasons (e.g., "wifi_not_connected", "outbound_queue_pressure")

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
- `outbound_queue_depth`: Current queued + in-flight entries
- `outbound_queue_capacity`: Maximum queue entries
- `outbound_queue_utilization_percent`: (depth * 100) // capacity

**UTC Fields:**
- `utc_valid`: Boolean indicating UTC time is valid
- `utc_sync_age_sec`: Seconds since last successful UTC sync

## Key Architectural Principles

1. **Strict Ownership**: Core 0 owns the network stack; Core 1 owns devices/sensors. No overlap.
2. **Immutability After Transfer**: Objects moved across cores are immutable by contract.
3. **Plain Data Only**: Only JSON-serializable data crosses core boundaries.
4. **Fail-Fast Configuration**: Invalid config rejects before network starts.
5. **QoS 1 MQTT**: Synchronous PUBACK required; only one message in flight.
6. **Bounded broker waits**: Every blocking MQTT wait (PUBACK, PINGRESP) is deadline-limited; a dead link surfaces as a failed ping or publish and triggers mid-run network recovery instead of stalling the run loop.

## Critical Files

| File | Purpose |
|------|---------|
| `main.py` | Entry point, orchestrates startup sequence |
| `core0.py` | Network stack (Wi-Fi, MQTT, UTC, reboot, keepalive, recovery) |
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

- Startup is mandatory and bounded: 3 attempts, each waiting up to `mqtt_broker_response_timeout_sec`; if all fail, Core 1 never starts
- Steady-state re-sync is non-blocking: the run loop sends the request, keeps a `mqtt_broker_response_timeout_sec` deadline, and never blocks on the answer
- Re-requests are throttled to 30s; a malformed-but-reachable answer re-keys the throttle to ~0.5s

## Testing

- `tests/` contains host-side unit tests
- Run with: `python -m pytest tests/`
- The suite covers core-ownership boundaries (AST checks), config validation, inter-core bus semantics, health payloads, normal-runtime-anchored telemetry/health scheduling, MQTT keepalive, UTC synchronization, network recovery, and the Core 1 liveness heartbeat
- Hardware testing requires an actual Pico device

## Hardware Notes

- **Pico W**: Wi-Fi only, 256KB RAM
- **Pico 2 W**: Wi-Fi, 264KB RAM, faster CPU
- Onboard LED is Core 0-only via `LEDManager`
- Never call `machine.reset()` from Core 1

## Version History

- **0.4.6**: One shared normal-runtime scheduling epoch for all periodic Core 1 work. `normal_runtime_start_ticks_ms` is captured exactly once, immediately after `system_startup_completed` is admitted to the outbound queue; both the telemetry deadline (`anchor + n × read_loop_sec`) and the health deadline (`anchor + n × health_interval_sec`) derive their fixed boundaries from that anchor. Telemetry and health remain independent schedulers (shared epoch, no execution dependency); neither deadline derives from the other. `boot_ticks_ms` remains the boot-lifetime reference for uptime (`uptime_ms`) and startup-duration measurement only. Reconnects, UTC resyncs, and device reinitialization never reset the anchor; only a reboot creates a new one. Health outage policy is unchanged (missed intervals skipped, never replayed); telemetry outage buffering is unchanged.

- **0.4.5**: Health scheduling anchored to boot time: `health_interval_sec` now defines fixed uptime-based boundaries from firmware boot (60s interval → first health at ~60s uptime, then 120s, 180s, ...). The immediate post-startup health message is removed; boundaries missed during startup or an MQTT outage are skipped, never replayed; deadlines advance from the previous boundary so processing delay cannot accumulate drift.

- **0.4.4**: Fixed UTC retry throttle blocking its own prompt retry: a malformed-but-reachable time-server answer now re-keys the retry backoff to ~0.5s (previously the 30s interval, measured from the original send, blocked the resend even though the server had just answered); a silent server still gets the full 30s throttle.

- **0.4.3**: MQTT keepalive PINGREQ and mid-run network recovery, non-blocking steady-state UTC synchronization with deadline and retry throttling, deadline-based Core 1 liveness heartbeat, and health-message generation gating during outages.

- **0.4.2**: Added MQTT subscriptions to the startup log and fixed device counts.

- **0.4.1**: Health message support with 17+ diagnostic fields, including the extended operational fields (hardware_type, machine, wifi_rssi_dbm, heap_headroom_bytes, core_1_activity_age_ms, utc_sync_age_sec, device_failures, outbound_queue_utilization_percent) and health message queueing support.

- **0.4.0**: One-time full system startup log before telemetry.

- **0.3.0**: Pre-serialized outbound MQTT queue with QoS 1

- **0.2.0**: Hardware detection for Pico W and Pico 2 W

- **0.1.0**: QoS 1 network probes and deterministic startup contract

- **0.0.0**: Baseline dual-core rebuild with system-information sensor only
