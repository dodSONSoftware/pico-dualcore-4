# Rebuilt Dual-Core Architecture

## Health Message Protocol

### Message Type: `health`

Core 1 publishes health messages to `iot/v3/health` with the following payload structure:

```json
{
  "message_schema_version": 3,
  "runtime_id": "<runtime-uuid>",
  "uptime_ms": <milliseconds>,
  "timestamp": "<utc-iso8601>",
  "source": "<device-source>",
  "message_type": "health",
  "firmware_version": "0.4.4",
  "payload": {
    "status": "healthy|degraded",
    "degraded_reasons": ["<reason1>", "<reason2>"],
    "hardware_type": "pico_w|pico_2_w",
    "machine": "Raspberry Pi Pico W with RP2040",
    "network_stack_ready": true,
    "wifi_connected": true,
    "wifi_rssi_dbm": -45,
    "mqtt_connected": true,
    "core_1_active": true,
    "core_1_activity_age_ms": 42,
    "free_heap_bytes": 95728,
    "minimum_free_heap_bytes": 65536,
    "heap_headroom_bytes": 30192,
    "devices_configured": 1,
    "devices_active": 1,
    "device_failures": 0,
    "outbound_queue_depth": 0,
    "outbound_queue_capacity": 16,
    "outbound_queue_utilization_percent": 0,
    "utc_valid": true,
    "utc_sync_age_sec": 52
  }
}
```

### Status Fields

- `status`: "healthy" when no degradation reasons, "degraded" otherwise
- `degraded_reasons`: Array of active degradation reasons

### Hardware Fields

- `hardware_type`: Canonical hardware type from `detect_hardware()`
- `machine`: Raw machine string from `os.uname().machine`

### Network Fields

- `network_stack_ready`: True once Core 0 has verified the complete startup contract (from the network snapshot)
- `wifi_connected`: Wi-Fi link is up (from the network snapshot)
- `wifi_rssi_dbm`: Current Wi-Fi RSSI from Core 0 network snapshot (may be null)
- `mqtt_connected`: MQTT broker connection is up (from the network snapshot)

### Memory Fields

- `free_heap_bytes`: Current `gc.mem_free()` value
- `minimum_free_heap_bytes`: Board-specific heap reserve (64KB Pico W, 128KB Pico 2 W)
- `heap_headroom_bytes`: free_heap - minimum_free_heap (negative when below reserve)

### Core Activity Fields

- `core_1_active`: True if `core_1_activity_age_ms <= threshold`
- `core_1_activity_age_ms`: Monotonic elapsed time since last activity report

### Device Fields

- `devices_configured`: From `DeviceManager.get_status_snapshot()`
- `devices_active`: From `DeviceManager.get_status_snapshot()`
- `device_failures`: devices_configured - devices_active

### Queue Fields

- `outbound_queue_depth`: Queued + in-flight entries
- `outbound_queue_capacity`: Queue capacity from `OutboundQueue`
- `outbound_queue_utilization_percent`: (depth * 100) // capacity (integer)

### UTC Fields

- `utc_valid`: True if UTC snapshot is available
- `utc_sync_age_sec`: Seconds since last UTC sync (integer, null if never synchronized)

### Degradation Reasons

- `network_stack_not_ready`: Core 0 network not fully initialized
- `wifi_not_connected`: Wi-Fi disconnected
- `mqtt_not_connected`: MQTT broker connection lost
- `core_1_inactive`: Core 1 activity exceeds threshold (3x read_loop_sec, min 60s)
- `low_free_heap`: free_heap < minimum_free_heap
- `device_count_mismatch`: devices_active != devices_configured
- `outbound_queue_pressure`: utilization >= 75%
- `utc_not_valid`: UTC snapshot unavailable

### Queue Priority

Health messages use `RETENTION_PRIORITY_HEALTH = 70`, the lowest priority class.

### Outage Behavior

Health messages are only generated when:
1. Network snapshot is available (`network_stack_ready = True`)
2. MQTT is connected (`mqtt_connected = True`)

This prevents health messages from accumulating during MQTT outages.

## Hardware detection

The firmware explicitly identifies the hardware at startup using `os.uname().machine`.

- Raspberry Pi Pico W: `RPI_PICO_W with RP2040` → canonical type `pico_w`, heap reserve 64 KiB
- Raspberry Pi Pico 2 W: `RPI_PICO2_W with RP2350` → canonical type `pico_2_w`, heap reserve 128 KiB
- Unsupported hardware raises a clear `RuntimeError` during startup

The hardware module provides:
- Canonical hardware type identifiers (`HARDWARE_TYPE_PICO_W`, `HARDWARE_TYPE_PICO_2_W`)
- Board-specific minimum free-heap reserves (`PICO_W_MIN_FREE_HEAP_BYTES`, `PICO_2_W_MIN_FREE_HEAP_BYTES`)
- Detection function `detect_hardware()` returning immutable result dict

See `hardware.py` for implementation details.

## Ownership invariants

1. Core 0 exclusively owns the complete network stack: `network.WLAN`, CYW43 networking, IP/DNS, sockets, MQTT, QoS 1, subscriptions, reconnect, UTC acquisition, reboot, and QoS 1 network probes for startup verification.
2. Core 1 exclusively owns the complete sensor/device stack: device drivers, I2C/SPI/UART/ADC, initialization, reads, device state, software sensors, and telemetry construction.
3. Only plain-data objects cross the core boundary.
4. Once an object is transferred into an inter-core lane it becomes immutable. Neither producer nor consumer may mutate it.
5. Live subsystem objects never cross cores.

## Three lanes

### 1. `outbound_queue`

Core 1 -> Core 0. Contains only data intended for MQTT.

- FIFO and bounded.
- Core 1 supplies only a message kind plus domain data; it does not know MQTT topics.
- Core 0 maps the kind to the authoritative MQTT topic, publishes with QoS 1, and owns the MQTT envelope sequence. Kinds: TELEMETRY → `mqtt_topic_telemetry`, COMMAND_RESPONSE → `mqtt_topic_command_response`, HEALTH → `mqtt_topic_health`, LOG → `mqtt_topic_log`.
- The startup log and connection logs travel as KIND_LOG entries; no hardcoded topics cross into Core 1.
- Core 1 captures creation-time `uptime_ms`/`timestamp`; Core 0 preserves them while adding source/runtime/firmware/schema/sequence.
- The MQTT client waits for the matching PUBACK before the next publish proceeds, naturally enforcing one application QoS 1 publish in flight. The wait is bounded by `mqtt_broker_response_timeout_sec`, so a blackholed link fails the publish (the entry stays in flight) instead of blocking the run loop.
- Retention priority is explicit: lower numeric values are more important.
- Priority classes are: CRITICAL 10, ERROR 20, WARN 30, TELEMETRY 40, INFO 50, HEALTH 70.
- When full, the queue finds the least-important queued class (highest numeric priority). If the incoming message is more important, or equally important, the oldest entry in that least-important class is evicted. If the incoming message is less important, it is rejected.
- The current Core 1 command response uses CRITICAL 10; telemetry uses TELEMETRY 40; health messages use HEALTH 70; the startup log uses INFO 50.
- An in-flight QoS 1 entry counts toward the configured capacity but is never evicted.
- A failed publish never discards the in-flight entry: it stays in flight and `take()` returns it again, so Core 0 retries until the broker PUBACKs (QoS 1 at-least-once delivery).

#### Pre-serialized message storage

Messages are fully validated, serialized to JSON, UTF-8 encoded, and size-checked before admission:

```
message created
    ↓
validate JSON-safe values (dict, list, tuple, str, int, float, bool, None)
    ↓
serialize to JSON
    ↓
encode UTF-8
    ↓
validate maximum payload size (MAX_OUTBOUND_MESSAGE_BYTES = 128KB)
    ↓
admit serialized bytes to outbound queue
    ↓
later MQTT publish uses stored bytes for envelope construction
```

After this change, any message present in the outbound queue is guaranteed to be:
- JSON-safe (only supported value types)
- Valid JSON-serializable
- UTF-8 encoded
- Within the configured application payload limit

The queue entry stores:
- `kind`: message kind (TELEMETRY, COMMAND_RESPONSE, HEALTH, LOG)
- `retention_priority`: numeric priority for eviction
- `payload_bytes`: pre-serialized, UTF-8 encoded JSON payload

The original message dictionary is not retained after queue admission. Any mutations to the original message after `put()` returns `True` have no effect on the queued payload.

Validation failures (unsupported value types, non-string keys, non-finite floats) raise a `ValueError` immediately. Oversized messages are silently rejected without affecting queue state.

### 2. `event_queue`

Core 0 -> Core 1. Contains private discrete commands/events.

- FIFO and bounded.
- Every admitted event matters.
- Entries are never automatically published to MQTT.
- Reboot never enters this lane; Core 0 owns reboot completely.

### 3. `state_mailboxes`

Core 0 -> Core 1 latest-value state.

- `network_snapshot`: Wi-Fi status, IP, RSSI, connection counts, `network_stack_ready` flag
- `utc_snapshot`: Current UTC time, ticks base, runtime start
- `hardware`: Detected hardware type, machine string, heap reserve
- `core_1_activity_ms`: Timestamp of last Core 1 activity report (in milliseconds)

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

### Boot when the network never appears

The connect loops in `establish_network()` are intentionally unbounded (a watchdog is listed under "Features intentionally not carried into the baseline"). If the configured SSID is absent, or the broker is unreachable, Core 0 retries forever: each Wi-Fi attempt observes the link for up to 20 s (200 × 100 ms), sleeps the configured backoff delays between attempts, then the whole sequence restarts. Core 1 never starts, and the flashing connection LED (50 ms on / 50 ms off) is the only visible state of this boot loop.

## QoS 1 network probe

Core 0 uses QoS 1 MQTT to verify the network path during startup. The probe message is published to `mqtt_topic_network_probe` with a unique packet ID. Core 0 waits for the matching PUBACK from the broker before proceeding.

The probe payload is minimal:
```json
{
  "message_type": "network_probe",
  "runtime_id": "<runtime_id>",
  "uptime_ms": <milliseconds>,
  "packet_id": <packet_id>
}
```

Two probes are performed during startup:
1. After Wi-Fi and MQTT connection, before draining startup work
2. After the 5-second stabilization wait

Both probes must succeed with matching PUBACKs before Core 1 starts and before the network snapshot marks `network_stack_ready = True`.

## MQTT keepalive

The CONNPACK advertises `mqtt_keepalive_sec` (30 s default), so the broker disconnects the client if it sees no client packet within 1.5 x keepalive (45 s). Application traffic alone does not cover this gap — the health interval is 60 s — so Core 0 sends PINGREQ explicitly:

- `Mqtt` tracks the last outbound MQTT activity (connect, PUBLISH, PINGREQ).
- When idle for `keepalive / 2` (15 s default), `ping_due()` returns true and the Core 0 run loop sends a PINGREQ while no outbound entry is being published (a PUBLISH itself resets the broker's keepalive timer).
- `ping()` waits for PINGRESP with a bounded timeout (capped at 10 s) so a dead link surfaces as a disconnect within one interval instead of blocking the run loop.
- A failed PINGREQ marks the connection disconnected; `_recover_network_if_needed` reconnects.

## Network recovery

When the run loop detects a lost link (Wi-Fi down, or MQTT down with Wi-Fi up), `_recover_network_if_needed` re-establishes it before any further processing. A blackholed broker (TCP up, but no PINGRESP or PUBACK ever arrives) is detected the same way: every blocking broker wait is bounded, so a dead link surfaces as a failed ping or publish, marks the connection disconnected, and recovery fires on the next loop iteration.

1. `network_stack_ready` is cleared and a forced network snapshot is published with `network_stack_ready = False`, so Core 1 health gating reflects the outage immediately.
2. `establish_network()` re-establishes Wi-Fi and MQTT with the normal backoff and connection logs. The connection LED flashes while this happens because `establish_network()` arms it.
3. `network_stack_ready` is restored, the connection LED stops, and a forced network snapshot is published with `network_stack_ready = True`.

The startup path and the recovery path share `establish_network()`, so the connect loops, backoff, logging, and LED behavior live in one place and cannot diverge.

## Core 0 run loop

Each Core 0 iteration (10 ms period) services, in order:

1. A pending reboot (publish the response, wait, `machine.reset()`).
2. Network recovery via `_recover_network_if_needed` — this runs even before any publishing, so a lost link is detected and repaired at the top of the loop.
3. Pending connection logs (when MQTT is connected).
4. The MQTT receive pump (`check_msg`) at the configured `mqtt_command_poll_ms` cadence — this is how UTC responses and commands arrive without blocking.
5. Pending Core 0 command responses (when MQTT is connected and no entry is in flight).
6. One outbound queue entry, published with QoS 1; a failed publish leaves the entry in flight for retry, and a successful one calls `complete_in_flight`.
7. A PINGREQ when keepalive traffic is due — only when no outbound entry is being published (a PUBLISH itself resets the broker timer).
8. The network snapshot publish, rate-limited to `network_snapshot_interval_sec`.
9. UTC housekeeping: discard a pending request whose deadline passed, then send a new request if the sync is due and the retry throttle allows.

## Core 1 baseline

The current device framework is retained:

- `DeviceManager`
- `Device` interface
- `SystemInformationDevice`
- initialization retry behavior
- read-failure/reinitialization behavior

The baseline test device is the software-only `system-information` sensor.

Core 1 starts only after Core 0 has completed the deterministic startup contract:

1. Wi-Fi connected
2. MQTT connected with subscriptions
3. QoS 1 network probe #1 with matching PUBACK received
4. Startup MQTT work drained
5. 5-second stabilization wait
6. QoS 1 network probe #2 with matching PUBACK received
7. UTC synchronization completed with valid snapshot
8. Initial network snapshot published with `network_stack_ready = True`

The `network_stack_ready` flag in the network snapshot indicates the complete startup contract has been verified. It is also cleared while a mid-run link outage is being recovered and restored after a successful re-establishment (see Network recovery).

## Startup log

Core 1's first action, before the first telemetry, is a one-time `system_startup_completed` log message. It is queued under KIND_LOG at INFO (50) retention priority; Core 0 maps the kind to `mqtt_topic_log` at publish time.

The payload carries:

- Startup status per subsystem (hardware, Wi-Fi, MQTT, subscriptions, UTC, Core 0, Core 1). Subscription readiness is reported without topic names — topic ownership stays in Core 0.
- Device counts (configured / ready / initialization-failed) plus per-device ready and failed lists (device, name, sensor type).
- A full `system_information` snapshot (all sections collected through `SystemInformation`).

Telemetry admission is gated on startup-log admission: if the first admission attempt is rejected, Core 1 waits 100 ms and retries once. If it is still rejected, Core 1 raises and halts — no telemetry goes out without the startup log being admitted.

## Core 1 liveness heartbeat

Core 1 refreshes the `core_1_activity_ms` state mailbox on a 5-second deadline, independent of read-loop phase: the refresh fires on any 20 ms loop iteration once its deadline has passed, rather than on a fixed grid aligned to the loop step. A deadline-based refresh cannot starve even when the actual loop period (sleep plus processing) does not divide the refresh window evenly.

Health messages report `core_1_active` as true while the stamp age is within threshold (`3 × read_loop_sec`, minimum 60 seconds), and add `core_1_inactive` to the degradation reasons when it is exceeded.

## Periodic Health Messages

Core 1 generates health messages periodically (every `health_interval_sec`) and immediately after startup completes. The health message contains current-state diagnostic fields without turning the payload into a full system information report.

### Generation Rules

1. **Network ready required**: Health messages are only generated when `network_stack_ready = True` and `mqtt_connected = True`. This prevents accumulation during MQTT outages.

2. **Authoritative data sources**:
   - Hardware: `StateMailboxes.get_hardware()` (canonical detected state)
   - RSSI: `StateMailboxes.get_network_snapshot().get("rssi")`
   - Core 1 activity: `StateMailboxes.get_core_1_activity_ms()`
   - Heap: `gc.mem_free()` (current measurement)
   - Devices: `SystemInformation.get_devices()` (backed by `DeviceManager.get_status_snapshot()`)
   - Queue: `OutboundQueue.get_depth_with_capacity()`
   - UTC: `StateMailboxes.get_utc_snapshot()`

3. **Monotonic time calculations**:
   - All age calculations use `time.ticks_diff()` for monotonic elapsed time
   - UTC sync age: integer division of milliseconds by 1000

4. **Queue pressure threshold**: 75% utilization (`outbound_queue_utilization_percent >= 75`)

5. **Low-priority retention**: Uses `RETENTION_PRIORITY_HEALTH = 70`, the lowest priority class

### Degradation Triggers

Health status is "degraded" when any of these conditions are true:
- `network_stack_not_ready`: Core 0 network not initialized
- `wifi_not_connected`: Wi-Fi disconnected
- `mqtt_not_connected`: MQTT broker connection lost
- `core_1_inactive`: Activity age exceeds threshold (3x read_loop_sec, minimum 60 seconds)
- `low_free_heap`: Free heap below configured reserve
- `device_count_mismatch`: Active devices don't match configured count
- `outbound_queue_pressure`: Queue utilization >= 75%
- `utc_not_valid`: UTC snapshot unavailable

If no degradation reasons exist, status is "healthy".

## Deterministic startup sequence

Core 0 performs the following sequence during `start()` before returning and allowing Core 1 to start:

1. **LED starts connecting**: Flashing 50ms ON / 50ms OFF
2. **Wi-Fi connection**: Blocks until Wi-Fi is connected
3. **MQTT connection**: Blocks until MQTT is connected and subscriptions established
4. **Network probe #1**: Publish QoS 1 probe message and wait for matching PUBACK
5. **Drain startup work**: Service pending connection logs
6. **5-second wait**: Stabilization period
7. **Network probe #2**: Publish QoS 1 probe message and wait for matching PUBACK
8. **UTC synchronization**: Block until a valid UTC response is received or 3 bounded attempts are exhausted (each attempt waits up to `mqtt_broker_response_timeout_sec`)
9. **Publish initial snapshots**: UTC and network snapshots to state mailboxes
10. **Stop connection LED**: LED turns off
11. **Return**: Core 0.start() returns, Core 1 starts

If any step fails (Wi-Fi, MQTT, either probe, or UTC sync), Core 0 raises an exception and Core 1 never starts.

## UTC time synchronization

Core 0 is the only UTC acquirer. Requests are published to `mqtt_topic_info_request` with a unique `request_id`; the server is expected to answer on `mqtt_topic_info_response` echoing that `request_id`.

- **Startup (required, bounded)**: `_synchronize_utc_required()` makes a fixed number of attempts (3 — a constant, deliberately not derived from the timeout setting), each waiting up to `mqtt_broker_response_timeout_sec` for a valid response while pumping MQTT. If all attempts fail, startup raises and Core 1 never starts.
- **Steady state (non-blocking)**: when the snapshot is older than `datetime_sync_interval_min`, the run loop sends a request and records a `mqtt_broker_response_timeout_sec` deadline. The run loop keeps servicing the outbound queue, network recovery, and command responses; the answer arrives through the regular `check_msg()` pump. The run loop never blocks on a UTC request.
- **Timeout and retry**: a pending request whose deadline passed is discarded, and re-requests are throttled to at most one per 30 seconds until a valid response arrives. An unresponsive time server therefore cannot stall the run loop or flood the broker. A malformed-but-reachable answer gets a much shorter backoff (~0.5s) since the server demonstrably answered us.
- **Response validation**: a response is accepted only if it matches the current schema version, is targeted at this device, carries the matching `request_id`, and contains a valid `timestamp` and positive integer `utc_epoch_ms`. A malformed answer to *our own* pending request clears that request and re-keys the retry throttle to a short ~0.5s backoff (instead of the full 30s measured from the original send); a response for any other request id is ignored without disturbing pending state (our response may still be in flight).

## Features intentionally not carried into the baseline

These should be added individually only after the baseline passes:

- advanced reboot-delivery state machines;
- advanced network recovery generations/state machines;
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

The onboard LED belongs to Core 0. `LEDManager` is the only component that writes `machine.Pin("LED")`. Core 1 and the Wi-Fi implementation do not access the LED directly.

The connection indication (50ms ON / 50ms OFF) remains active throughout the entire deterministic startup sequence:
- Wi-Fi connection
- MQTT connection
- QoS 1 network probe #1
- Startup MQTT work drain
- 5-second stabilization wait
- QoS 1 network probe #2
- UTC synchronization

The connection LED stops only after the complete startup contract has been verified and the initial state snapshots have been published.

The same flashing indication is reused after any mid-run outage: `establish_network()` arms it during recovery, and recovery stops it once the link is restored — it never remains flashing after a successful re-establishment.

Successful telemetry publication requests a non-blocking one-second pulse.
