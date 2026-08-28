# Sensor Firmware — Refit 4 — Dual-Core Embedded System

[![Dodson Labs](https://img.shields.io/badge/dodson%20labs-2026-purple?labelColor=gray)](https://github.com/dodSONSoftware)
[![MicroPython](https://img.shields.io/badge/MicroPython-1.20+-00897B?logo=micropython&logoColor=white)](https://micropython.org)
[![Raspberry Pi Pico W](https://img.shields.io/badge/Hardware-Pico%20W-blue.svg)](https://www.raspberrypi.com/products/raspberry-pi-pico/)
[![Raspberry Pi Pico 2 W](https://img.shields.io/badge/Hardware-Pico%202%20W-blue.svg)](https://www.raspberrypi.com/products/rp2040/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A dual-core firmware for Raspberry Pi Pico W/Pico 2 W that separates network operations (Core 0) from sensor/device management (Core 1) using an inter-core message bus.

## Overview

This firmware implements a clean architecture where:

- **Core 0** exclusively owns Wi-Fi, MQTT, sockets, UTC time synchronization, and system reboot
- **Core 1** exclusively owns device drivers, sensor reads, device lifecycle management, and telemetry construction
- Communication between cores uses three bounded lanes with strict ownership rules

### Architecture

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for detailed architecture documentation.

```
┌───────────────────────────────────────────────────────────────────────┐
│                           Raspberry Pi Pico                           │
├───────────────────────────────────────────────────────────────────────┤
│                                                                       │
│    ┌─────────────────────────┐         ┌─────────────────────────┐    │
│    │     Core 0 (Wi-Fi/MQTT) │◄───────►│    Core 1 (Sensors)     │    │
│    │  - Wi-Fi Connection     │  Events │  - Device Drivers       │    │
│    │  - MQTT Client (QoS 1)  │         │  - Sensor Reads         │    │
│    │  - UTC Synchronization  │         │  - Telemetry Build      │    │
│    │  - Reboot Control       │         │  - Device Lifecycle     │    │
│    └─────────────────────────┘         └─────────────────────────┘    │
│           │                                      │                    │
│           ▼                                      ▼                    │
│    ┌──────────────────┐                  ┌──────────────────┐         │
│    │  Inter-Core Bus  │  Outbound Queue  │  Inter-Core Bus  │         │
│    │  - Outbound      │  (Core1→Core0)   │  - Event Queue   │         │
│    │  - Event         │  - MQTT Topics   │  (Core0→Core1)   │         │
│    │  - State         │  - Prioritized   │  - Commands      │         │
│    └──────────────────┘                  └──────────────────┘         │
└───────────────────────────────────────────────────────────────────────┘
```

## Quick Start

### Prerequisites

- Raspberry Pi Pico W or Pico 2 W
- MicroPython 1.20+ firmware
- VS Code with Pico extension or `mpremote` for deployment

### Configuration

1. Copy the secrets template:

```bash
cp config-secrets-example.json config-secrets.json
```

2. Edit `config-secrets.json` with your local Wi-Fi credentials:

```json
{
  "wifi_ssid": "your_network_name",
  "wifi_password": "your_password"
}
```

3. Edit `config.json` for your MQTT broker:

```json
{
  "mqtt_broker_ip_address": "192.168.1.100",
  "source": "my-pico-device"
}
```

### Deployment

Deploy all `.py` files and the `devices/` directory to the Pico:

```bash
mpremote cp main.py :main.py
mpremote cp core0.py :core0.py
mpremote cp core1.py :core1.py
mpremote cp -r devices/ :devices/
# etc for all modules...
```

### Running

After deployment, run from REPL:

```python
import main
main.main()
```

Or execute directly:

```python
exec(open("main.py").read())
```

The firmware starts automatically on boot if `main.py` is present.

## Inter-Core Communication

Three message lanes govern Core 0 ↔ Core 1 communication:

### 1. Outbound Queue (Core 1 → Core 0)

Core 1 queues messages for MQTT; Core 0 publishes them.

| Priority Class | Value | Use Case |
|----------------|-------|----------|
| CRITICAL | 10 | Command responses |
| ERROR | 20 | Error conditions |
| WARN | 30 | Warning conditions |
| TELEMETRY | 40 | Sensor data |
| INFO | 50 | Informational messages |
| HEALTH | 70 | Health checks |

When full, the queue evicts the oldest message from the least important priority class that has capacity.

### 2. Event Queue (Core 0 → Core 1)

Core 0 forwards MQTT commands to Core 1 via discrete events.

### 3. State Mailboxes

Latest-value snapshots:
- `network_snapshot`: Wi-Fi status, IP, RSSI, connection counts
- `utc_snapshot`: Current UTC time, ticks base, runtime start
- `hardware`: Detected hardware type and heap reserve
- `core_1_activity_ms`: Timestamp of last Core 1 activity

## Health Messages

Core 1 periodically publishes health messages to `iot/v3/health` with the following fields:

### Status
- `status`: "healthy" or "degraded"
- `degraded_reasons`: Array of degradation reasons (e.g., "wifi_not_connected", "outbound_queue_pressure")

### Hardware
- `hardware_type`: Canonical hardware type ("pico_w" or "pico_2_w")
- `machine`: Human-readable machine identifier

### Network
- `wifi_rssi_dbm`: Current Wi-Fi signal strength (dBm)
- `network_stack_ready`: Core 0 network stack initialization status
- `wifi_connected`: Wi-Fi connection status
- `mqtt_connected`: MQTT broker connection status

### Memory
- `free_heap_bytes`: Current free heap
- `minimum_free_heap_bytes`: Configured heap reserve (64KB Pico W, 128KB Pico 2 W)
- `heap_headroom_bytes`: free_heap - minimum_free_heap (may be negative)

### Core Activity
- `core_1_active`: Boolean indicating Core 1 liveness
- `core_1_activity_age_ms`: Milliseconds since last Core 1 activity report

### Devices
- `devices_configured`: Number of configured devices
- `devices_active`: Number of active/ready devices
- `device_failures`: devices_configured - devices_active

### Queue
- `outbound_queue_depth`: Current queued + in-flight entries
- `outbound_queue_capacity`: Maximum queue entries
- `outbound_queue_utilization_percent`: (depth * 100) // capacity

### UTC
- `utc_valid`: Boolean indicating UTC time is valid
- `utc_sync_age_sec`: Seconds since last successful UTC sync

### Degradation Triggers

The health status is "degraded" when any of these conditions are true:
- `network_stack_not_ready`: Core 0 network not fully initialized
- `wifi_not_connected`: Wi-Fi disconnected
- `mqtt_not_connected`: MQTT broker connection lost
- `core_1_inactive`: Core 1 activity exceeds threshold (3x read_loop_sec, min 60s)
- `low_free_heap`: free_heap < minimum_free_heap
- `device_count_mismatch`: devices_active != devices_configured
- `outbound_queue_pressure`: utilization >= 75%
- `utc_not_valid`: UTC snapshot unavailable

Health messages are only generated when MQTT is connected to prevent stale messages during outages.

### Configuration

Health messages are controlled by:
- `mqtt_topic_health`: MQTT topic for health messages (default: `iot/v3/health`)
- `health_interval_sec`: Interval between health messages (default: 60 seconds)

## Features

- **QoS 1 MQTT**: Synchronous PUBLISH → PUBACK, one in-flight message
- **Health Messages**: Periodic diagnostic messages with status and 17+ fields
- **UTC Synchronization**: Requests time from server on boot and periodically
- **LED Status**: Flashing during connection, pulse on telemetry send
- **Reboot Command**: JSON command triggers clean reboot with acknowledgment
- **Device Lifecycle**: Auto-retry initialization and read failures
- **Pre-serialized Queue**: Outbound messages validated and serialized before admission
- **Hardware Detection**: Automatic Pico W vs Pico 2 W detection
- **Memory-Efficient**: Designed for 256KB RAM constraint

## Configuration

### Schema Version

The firmware expects `config_schema_version: 5`. Unknown top-level keys are rejected.

### Key Settings

| Setting | Description |
|---------|-------------|
| `read_loop_sec` | Telemetry read interval (seconds) |
| `device_initialization_attempts` | Retry count for device init |
| `device_read_failure_threshold` | Consecutive failures before reinit |
| `mqtt_keepalive_sec` | MQTT keepalive interval |
| `datetime_sync_interval_min` | UTC sync interval (minutes) |
| `health_interval_sec` | Health message interval (seconds) |
| `mqtt_topic_health` | MQTT topic for health messages |
| `max_outbound_queue_entries` | Maximum queued messages |
| `network_snapshot_interval_sec` | Network snapshot update interval |

See [`config.json`](config.json) for complete example.

## Reboot Command

Send to `mqtt_topic_command`:

```json
{
  "message_type": "command",
  "message_schema_version": 3,
  "target": "*",
  "command_id": "reboot-001",
  "command": "reboot",
  "payload": {}
}
```

The device responds with a command response, waits 6 seconds, then reboots.

## Development

### Project Structure

```
├── main.py          # Entry point, orchestrates Core 0 and Core 1
├── core0.py         # Core 0: Wi-Fi, MQTT, network stack
├── core1.py         # Core 1: Sensors, devices, telemetry
├── intercore.py     # Three-lane inter-core bus
├── config.py        # Configuration loading and validation
├── device_manager.py # Device lifecycle management
├── devices/         # Device driver modules
│   ├── __init__.py
│   └── system_information.py
├── led_manager.py   # LED state machine
├── wifi.py          # Wi-Fi connection management
├── mqtt.py          # MQTT client wrapper
├── message_protocol.py # Message formatting helpers
├── message_serializer.py # Message validation and pre-serialization
├── hardware.py      # Hardware detection (Pico W/Pico 2 W)
├── system_information.py # System state snapshots
└── version.py       # Version constants
```

### Adding a Device Driver

1. Create `devices/my_sensor.py` implementing the `Device` interface
2. Add to `device_factory.py` factory
3. Register in `config.json` devices array

See [`devices/system_information.py`](devices/system_information.py) for reference.

### Testing

Host-side validation runs before hardware deployment. See tests in [`tests/`](tests/).

## Hardware Status

The 0.0.0 baseline is software-tested and passes host-side syntax and configuration validation. Hardware validation on Pico W/Pico 2 W with the system-information sensor is pending.

## License

MIT License - see [`LICENSE`](LICENSE) for details.

## Credits

- Architecture design and implementation: dodson Software ( dodson labs )
- Copyright (c) 2026 dodson Software ( dodson labs )
