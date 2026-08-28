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
- `wifi_rssi_dbm`: Current Wi-Fi signal strength (dBm)

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

## Critical Files

| File | Purpose |
|------|---------|
| `main.py` | Entry point, orchestrates startup sequence |
| `core0.py` | Network stack (Wi-Fi, MQTT, UTC, reboot) |
| `core1.py` | Device lifecycle, sensor reads, telemetry, health messages |
| `intercore.py` | Three-lane message bus implementation |
| `device_manager.py` | Device lifecycle management |
| `config.py` | Configuration loading and validation |
| `hardware.py` | Hardware detection (Pico W/Pico 2 W) |
| `system_information.py` | System state snapshots |
| `message_serializer.py` | JSON-safe message validation and serialization |

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

1. Create `devices/my_sensor.py` with `Device` interface
2. Add to `device_factory.py` factory function
3. Register in `config.json` with unique `id`
4. Test with `tests/test_device_manager.py`

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

## Testing

- `tests/` contains host-side unit tests
- Run with: `python -m pytest tests/`
- Hardware testing requires actual Pico device

## Hardware Notes

- **Pico W**: Wi-Fi only, 256KB RAM
- **Pico 2 W**: Wi-Fi, 264KB RAM, faster CPU
- Onboard LED is Core 0-only via `LEDManager`
- Never call `machine.reset()` from Core 1

## Version History

- **0.4.0**: Extended health messages with 8 additional operational fields: hardware_type, machine, wifi_rssi_dbm, heap_headroom_bytes, core_1_activity_age_ms, utc_sync_age_sec, device_failures, outbound_queue_utilization_percent. Added health message queueing support.

- **0.3.0**: Pre-serialized outbound MQTT queue with QoS 1

- **0.2.0**: Hardware detection for Pico W and Pico 2 W

- **0.1.0**: QoS 1 network probes and deterministic startup contract

- **0.0.0**: Baseline dual-core rebuild with system-information sensor only
