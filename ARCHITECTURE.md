# Rebuilt Dual-Core Architecture

## Ownership invariants

1. Core 0 exclusively owns the complete network stack: `network.WLAN`, CYW43 networking, IP/DNS, sockets, MQTT, QoS 1, subscriptions, reconnect, UTC acquisition, and reboot.
2. Core 1 exclusively owns the complete sensor/device stack: device drivers, I2C/SPI/UART/ADC, initialization, reads, device state, software sensors, and telemetry construction.
3. Only plain-data objects cross the core boundary.
4. Once an object is transferred into an inter-core lane it becomes immutable. Neither producer nor consumer may mutate it.
5. Live subsystem objects never cross cores.

## Three lanes

### 1. `outbound_queue`

Core 1 -> Core 0. Contains only data intended for MQTT.

- FIFO and bounded.
- Core 1 supplies only a message kind plus domain data; it does not know MQTT topics.
- Core 0 maps the kind to the authoritative MQTT topic, publishes with QoS 1, and owns the MQTT envelope sequence.
- Core 1 captures creation-time `uptime_ms`/`timestamp`; Core 0 preserves them while adding source/runtime/firmware/schema/sequence.
- The original MQTT client blocks until PUBACK, naturally enforcing one application QoS 1 publish in flight.
- Retention priority is explicit: lower numeric values are more important.
- Priority classes are: CRITICAL 10, ERROR 20, WARN 30, TELEMETRY 40, INFO 50, HEALTH 70.
- When full, the queue finds the least-important queued class (highest numeric priority). If the incoming message is more important, or equally important, the oldest entry in that least-important class is evicted. If the incoming message is less important, it is rejected.
- The current Core 1 command response uses CRITICAL 10; telemetry uses TELEMETRY 40.
- An in-flight QoS 1 entry counts toward the configured capacity but is never evicted.

### 2. `event_queue`

Core 0 -> Core 1. Contains private discrete commands/events.

- FIFO and bounded.
- Every admitted event matters.
- Entries are never automatically published to MQTT.
- Reboot never enters this lane; Core 0 owns reboot completely.

### 3. `state_mailboxes`

Core 0 -> Core 1 latest-value state.

- `network_snapshot`
- `utc_snapshot`

State is replaced, not accumulated. Core 1 keeps the latest immutable snapshot until Core 0 replaces it.

## Core 0 baseline

Core 0 intentionally follows the original known-good behavior:

- every Wi-Fi attempt starts with `network.WLAN(network.WLAN.IF_STA)`;
- `active(True)` is called unconditionally;
- `PM_NONE` is applied when available;
- Wi-Fi uses the original ~20-second connection observation and configured retry delays;
- MQTT uses the original small MQTT client;
- QoS 1 uses synchronous PUBLISH -> PUBACK;
- reboot is handled entirely by Core 0;
- reboot response is published, then the code waits 5 seconds + 1 second and calls `machine.reset()`;
- no pre-reset network shutdown is performed.

## Core 1 baseline

The current device framework is retained:

- `DeviceManager`
- `Device` interface
- `SystemInformationDevice`
- initialization retry behavior
- read-failure/reinitialization behavior

The baseline test device is the software-only `system-information` sensor.

Core 1 starts only after Core 0 has connected Wi-Fi and MQTT and published the initial network snapshot.

## Features intentionally not carried into the baseline

These should be added individually only after the baseline passes:

- startup network probes;
- advanced reboot-delivery state machines;
- advanced network recovery generations/state machines;
- health publishing;
- dynamic configuration;
- watchdog/cross-core recovery;
- additional Core 1 commands;
- physical sensors.

## Baseline acceptance tests

1. Cold boot on Pico W.
2. Wi-Fi connects.
3. MQTT connects.
4. Core 1 starts.
5. Software-sensor telemetry publishes via QoS 1.
6. 20 consecutive reboot-command cycles recover Wi-Fi, MQTT, Core 1, and telemetry.
7. MQTT broker outage: Core 1 continues, queue remains bounded, publishing resumes after broker recovery.
8. Wi-Fi outage: Core 1 continues, queue remains bounded, Wi-Fi/MQTT recover, publishing resumes.
9. Repeat the same matrix on Pico 2 W.

## LED ownership

The onboard LED belongs to Core 0. `LEDManager` is the only component that writes `machine.Pin("LED")`. Core 1 and the Wi-Fi implementation do not access the LED directly. The connection indication remains active until MQTT is connected; successful telemetry publication requests a non-blocking one-second pulse.
