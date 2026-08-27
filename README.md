# Sensor Firmware v4 — Dual-Core Embedded System

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

## Features

- **QoS 1 MQTT**: Synchronous PUBLISH → PUBACK, one in-flight message
- **UTC Synchronization**: Requests time from server on boot and periodically
- **LED Status**: Flashing during connection, pulse on telemetry send
- **Reboot Command**: JSON command triggers clean reboot with acknowledgment
- **Device Lifecycle**: Auto-retry initialization and read failures
- **Memory-Efficient**: Designed for 256KB RAM constraint

## Configuration

### Schema Version

The firmware expects `config_schema_version: 4`. Unknown top-level keys are rejected.

### Key Settings

| Setting | Description |
|---------|-------------|
| `read_loop_sec` | Telemetry read interval (seconds) |
| `device_initialization_attempts` | Retry count for device init |
| `device_read_failure_threshold` | Consecutive failures before reinit |
| `mqtt_keepalive_sec` | MQTT keepalive interval |
| `datetime_sync_interval_min` | UTC sync interval (minutes) |

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
