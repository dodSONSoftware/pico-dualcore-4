# TwinCore Sensor Firmware

Series 4 — Dual-Core Embedded System

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
- Communication between cores uses four lanes with strict ownership rules (the two FIFO lanes are heap-governed: admitted against the board's two heap thresholds — the preferred reserve where memory-pressure handling begins and the hard minimum floor that must stay intact; the latest-value lanes carry state snapshots and the config-update request/result)

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

Or build a single deployable artifact containing the required file set:

```bash
python release.py
# -> releases/sensor-firmware-<version>.tar.gz
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

The queue is heap-governed and additionally bounded by a configured entry-count ceiling (`outbound_queue_max_messages`, 1–256): admission is decided against the board's heap thresholds first (the heap stays authoritative — the count ceiling can only add a rejection or a retention-aware eviction, never admit what the heap would reject), and once the heap floor is satisfied the entry count (queued + in-flight) is checked against the ceiling. At the limit it evicts the oldest message of the least-important eligible priority class to make room, and rejects the new message transiently (for the producer to retry) when no eligible entry remains.

### 2. Event Queue (Core 0 → Core 1)

Core 0 forwards MQTT commands to Core 1 via discrete events.

### 3. State Mailboxes

Latest-value snapshots:
- `network_snapshot`: Wi-Fi status, IP, RSSI, connection counts
- `utc_snapshot`: Current UTC time, ticks base, runtime start
- `hardware`: Detected hardware type, machine string, and the board's preferred/minimum heap thresholds
- `core_1_activity_ms`: Timestamp of last Core 1 activity

## Health Messages

Core 1 periodically publishes health messages to `iot/v3/health` with the following fields:

### Status
- `status`: "healthy" or "degraded"
- `degraded_reasons`: Array of degradation reasons (e.g., "wifi_not_connected", "low_free_heap")

### Hardware
- `hardware_type`: Canonical hardware type ("pico_w" or "pico_2_w")
- `machine`: Human-readable machine identifier
- `cpu_temperature_c`: On-chip die temperature (°C, 0.1 °C resolution) via the datasheet conversion (Vbe = 0.706 V at 27 °C, slope −1.721 mV/°C; VREF- and device-sensitive, roughly ±5 °C — a trend indicator, not a calibrated absolute); `null` when the ADC core-temp channel is unavailable

### Network
- `wifi_rssi_dbm`: Current Wi-Fi signal strength (dBm)
- `network_stack_ready`: Core 0 network stack initialization status
- `wifi_connected`: Wi-Fi connection status
- `mqtt_connected`: MQTT broker connection status

### Memory
- `free_heap_bytes`: Current free heap
- `preferred_free_heap_bytes`: Preferred reserve — where memory-pressure handling begins (64KB Pico W, 144KB Pico 2 W); not a rejection wall
- `minimum_free_heap_bytes`: Hard survival floor that admission must protect (48KB Pico W, 128KB Pico 2 W)
- `heap_headroom_bytes`: free_heap - minimum_free_heap (may be negative)

### Core Activity
- `core_1_active`: Boolean indicating Core 1 liveness
- `core_1_activity_age_ms`: Milliseconds since last Core 1 activity report

### Devices
- `devices_configured`: Number of configured devices
- `devices_active`: Number of active/ready devices
- `device_failures`: devices_configured - devices_active

### Queue
The outbound queue is heap-governed **and** bounded by the `outbound_queue_max_messages` entry-count ceiling (evaluated after the heap policy, in-flight included), so these are observability metrics: the depth and high-water-mark are utilization against that ceiling (≤ `outbound_queue_max_messages`), while the byte metrics remain heap-governed only:
- `outbound_queue_depth`: Current queued + in-flight entries (≤ `outbound_queue_max_messages`)
- `outbound_queued_bytes`: Retained payload bytes (queued FIFO plus in-flight entry)
- `outbound_queue_high_watermark`: Peak queue depth since boot (≤ `outbound_queue_max_messages`)
- `outbound_queue_high_watermark_bytes`: Peak retained payload bytes since boot
- `outbound_evicted`: Entries evicted under memory pressure or to relieve the count ceiling (all kinds)
- `telemetry_evicted`: Evicted entries of the telemetry kind
- `outbound_rejected`: Admissions rejected because the hard free-heap floor could not be restored or no eligible entry was available to relieve the count ceiling

### UTC
- `utc_valid`: Boolean indicating UTC time is valid
- `utc_sync_age_sec`: Seconds since last successful UTC sync

### Degradation Triggers

The health status is "degraded" when any of these conditions are true:
- `network_stack_not_ready`: Core 0 network not fully initialized
- `wifi_not_connected`: Wi-Fi disconnected
- `mqtt_not_connected`: MQTT broker connection lost
- `core_1_inactive`: Core 1 activity exceeds threshold (3x read_loop_sec, min 60s)
- `low_free_heap`: free_heap < minimum_free_heap (below the hard floor)
- `device_count_mismatch`: devices_active != devices_configured
- `utc_not_valid`: UTC snapshot unavailable

Health messages are only generated when MQTT is connected to prevent stale messages during outages.

### Configuration

Health messages are controlled by:
- `mqtt_topic_health`: MQTT topic for health messages (default: `iot/v3/health`)
- `health_interval_sec`: Interval between health messages (default: 60 seconds)

The cadence is anchored: boundaries fall at `anchor + n × health_interval_sec` from a single runtime anchor captured once, after the startup event log is admitted. Telemetry shares the same anchor (`anchor + n × read_loop_sec`) but keeps its own independent scheduler. Boundaries missed during startup or an outage are skipped, never replayed, and deadlines advance from the previous deadline so processing delay cannot accumulate drift. See the scheduling sections in [`ARCHITECTURE.md`](ARCHITECTURE.md).

## Features

- **QoS 1 MQTT**: Synchronous PUBLISH → PUBACK, one in-flight message
- **MQTT Keepalive**: Explicit PINGREQ at keepalive/2 keeps the broker session alive
- **Network Recovery**: Mid-run Wi-Fi/MQTT loss — including blackholed links — is detected and re-established automatically
- **Health Messages**: Periodic diagnostic messages with status and 17+ fields
- **Anchored Scheduling**: Telemetry and health boundaries are fixed to one shared runtime anchor captured at startup; missed boundaries are skipped, never replayed
- **Tick-Wrap-Safe Uptime**: Uptime is accumulated from recent sample deltas, staying correct on long-running devices
- **UTC Synchronization**: Mandatory at startup; non-blocking steady-state re-sync with deadline and retry throttling
- **Core 1 Liveness**: Deadline-based heartbeat drives the `core_1_active` health field; the Core 0 watchdog resets the board if Core 1 stops refreshing — including while Core 0 is stuck in network recovery
- **Core 0 Hardware Watchdog**: `machine.WDT` (8 s, armed once the startup contract has passed) is fed only from Core 0's own execution, so a Core 0 that is alive but no longer making progress resets the board — complementing the exception boundary that covers Core 0 failures that raise
- **Startup Log**: One-time startup log stream published before telemetry — the `system_startup_completed` event log (the gate that halts boot if it cannot be admitted) followed by best-effort per-section `system_information` diagnostics, each a small log that skips on failure instead of taking the whole log down
- **LED Status**: Flashing during connection, pulse on telemetry send
- **Reboot Command**: JSON command triggers clean reboot with acknowledgment
- **Device Lifecycle**: Auto-retry initialization and read failures
- **Pre-serialized Queue**: Outbound messages validated and serialized before admission
- **Hardware Detection**: Automatic Pico W vs Pico 2 W detection
- **Memory-Efficient**: Designed for 256KB RAM constraint
- **Two-Threshold Heap Admission**: Each board splits its free-heap reserve into a preferred pressure band (64KB Pico W / 144KB Pico 2 W) and a hard survival floor (48KB / 128KB); queue admission reclaims under pressure instead of rejecting, so a Pico W admits normal telemetry in steady state

## Configuration

### Schema Version

The firmware expects `config_schema_version: 8`. Unknown top-level keys are rejected.

### Key Settings

| Setting | Description |
|---------|-------------|
| `read_loop_sec` | Telemetry read interval (seconds) |
| `device_initialization_attempts` | Retry count for device init |
| `device_initialization_retry_delay_ms` | Delay between device init retries (ms) |
| `device_read_failure_threshold` | Consecutive failures before reinit |
| `outbound_queue_max_messages` | Outbound queue entry-count ceiling (1–256); the heap policy is evaluated first |
| `mqtt_keepalive_sec` | MQTT keepalive interval (seconds) |
| `mqtt_command_poll_ms` | MQTT receive pump interval (ms) |
| `mqtt_outbound_publish_delay_ms` | Minimum delay (ms) after a successful outbound MQTT PUBLISH before another may begin; 0 disables pacing |
| `mqtt_broker_response_timeout_sec` | Bounded PUBACK/UTC-response wait (seconds) |
| `network_probe_timeout_sec` | Startup probe PUBACK wait (seconds) |
| `datetime_sync_interval_min` | UTC sync interval (minutes) |
| `health_interval_sec` | Health message interval (seconds) |
| `mqtt_topic_health` | MQTT topic for health messages |
| `network_snapshot_interval_sec` | Network snapshot update interval |
| `wifi_reconnect_delays_sec` | Wi-Fi reconnect backoff sequence (seconds) |
| `mqtt_reconnect_delays_sec` | MQTT reconnect backoff sequence (seconds) |

See [`config.json`](config.json) for complete example.

## Command Target Matching

`target` is matched against the configured `source`, the device's IP address,
or `"*"`. The comparison against the source name is case-insensitive —
`test-pico-2`, `TEST-PICO-2`, and `Test-Pico-2` all address a device
configured as `Test-Pico-2` — and the device always responds with its
configured casing unchanged.

## Command ID Deduplication

`command_id` identifies one logical command. The device retains a short
in-memory history of the 16 most recently accepted IDs and ignores a command
whose ID is still present — it is not executed and no response is sent.
Senders must generate a new ID for every new logical command, including a
corrected retry of a malformed command. A suppressed ID is available again
once 16 newer distinct IDs have been accepted, or after a reboot.

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

## Get Details Command

`get-details` requests a current full system-information snapshot from Core 1.
The command takes no options, so `payload` must be an empty object.

Send to `mqtt_topic_command`:

```json
{
  "message_type": "command",
  "message_schema_version": 3,
  "target": "<device-source>",
  "command_id": "details-001",
  "command": "get-details",
  "payload": {}
}
```

A successful `command_response` returns the system-information object directly
in `payload.data`. It always contains every section in the authoritative
`SYSTEM_INFORMATION_SECTIONS` list, independent of the configured
`system-information` device `include` list.

```json
{
  "message_type": "command_response",
  "payload": {
    "command_id": "details-001",
    "command": "get-details",
    "targeted": true,
    "success": true,
    "data": {
      "network": {},
      "memory": {},
      "runtime": {},
      "devices": {},
      "cpu": {},
      "machine": {},
      "communications": {},
      "queues": {},
      "device_status": []
    }
  }
}
```

The normal Core 0 MQTT envelope fields (`sequence`, `runtime_id`, `source`,
`firmware_version`, and `message_schema_version`) plus `uptime_ms` and
`timestamp` are also present on the published response. If an individual
section cannot be collected, that section contains an `error` object and the
remaining sections are still returned.


## Built-in Device

There is one built-in device, it has a device type of "system_information".

Example:
```
{
  "id": "p5h3DLqmWjCkLcXUtaFRq8yBsucEuY4A",
  "device_type": "system-information",
  "name": "System Information Sensor",
  "config": {
    "include": [
      "communications",  
      "cpu",  
      "device_status",  
      "devices",  
      "machine",  
      "memory",  
      "network",  
      "queues",  
      "runtime"
    ]
  }
}
```

An empty `"include": []` list means all sections.

| Section          | Information returned                         |
| ---------------- | -------------------------------------------- |
| `communications` | Wi-Fi/MQTT connection state and counters     |
| `cpu`            | CPU frequency and on-chip die temperature    |
| `device_status`  | Detailed status for each configured device   |
| `devices`        | Aggregate device counts/status               |
| `machine`        | Hardware/platform/MicroPython information    |
| `memory`         | MicroPython heap allocation/free/total       |
| `network`        | Current Wi-Fi/network addressing and RSSI    |
| `queues`         | Inter-core/outbound queue state and counters |
| `runtime`        | Runtime timing/configuration information     |

## Development

### Project Structure

```
├── main.py            # Entry point, orchestrates Core 0 and Core 1
├── core0.py           # Core 0: Wi-Fi, MQTT, keepalive, recovery, UTC, reboot
├── core1.py           # Core 1: sensors, devices, telemetry, health, liveness
├── intercore.py       # Three-lane inter-core bus
├── config.py          # Configuration loading and validation
├── device_manager.py  # Device lifecycle management
├── device_factory.py  # Device construction from config
├── devices/           # Device driver packages
│   ├── __init__.py
│   ├── device.py      # Device interface (initialize, read)
│   └── system_information/
│       ├── __init__.py
│       └── system_information_device.py
├── led_manager.py     # Core 0 LED state machine
├── wifi.py            # Core 0 Wi-Fi connection management
├── mqtt.py            # Core 0 MQTT lifecycle (QoS 1, keepalive PINGREQ)
├── mqtt_client.py     # Low-level MQTT wire protocol client
├── message_protocol.py # Message formatting helpers
├── message_serializer.py # Message validation and pre-serialization
├── system_information.py # System state snapshots (Core 1 data source)
├── uptime.py            # Accumulated boot-relative uptime (tick-wrap-safe)
├── hardware.py        # Hardware detection (Pico W/Pico 2 W)
├── debug.py           # Debug print switch
├── release.py         # Release artifact builder
├── config.json        # Runtime configuration
├── config-secrets-example.json # Wi-Fi credential template
└── version.py         # Version constants
```

### Adding a Device Driver

1. Create a `devices/my_sensor/` package implementing the `Device` interface from `devices/device.py` (`initialize(config)`, `read()`)
2. Register the `device_type` in the `device_factory.py` factory
3. Add the package files to `REQUIRED_PACKAGES` in `release.py`
4. Register in `config.json` devices array

See [`devices/system_information/system_information_device.py`](devices/system_information/system_information_device.py) for reference.

### Testing

Host-side validation runs before hardware deployment:

```bash
python -m pytest tests/
```

See tests in [`tests/`](tests/) — covering core-ownership boundaries, configuration, inter-core bus semantics, health payloads, normal-runtime-anchored telemetry/health scheduling, tick-wrap-safe uptime, MQTT keepalive, UTC synchronization, network recovery, and Core 1 liveness.

## Hardware Status

The firmware is software-tested against the full host-side test suite (run `python -m pytest tests/`) and hardware-validated on a Pico 2 W with the system-information sensor: a live capture (taken on firmware 0.4.61) shows successful startup, telemetry, and health, an MQTT-outage backlog reaching the outbound queue high-watermark (125 messages / 118,292 queued payload bytes), and a successful reconnect with complete drain — zero queue rejections and zero evictions in that run. Queue eviction itself was not exercised by the capture and is covered by the host-side test suite. The current firmware version is defined in a single place — see `FIRMWARE_VERSION` in [`version.py`](version.py) — rather than being restated here.

## License

MIT License - see [`LICENSE`](LICENSE) for details.

## Credits

- Architecture design and implementation: dodson Software ( dodson labs )
- Copyright (c) 2026 dodson Software ( dodson labs )
