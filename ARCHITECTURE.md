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
  "firmware_version": "<firmware-version>",
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
    "outbound_queued_bytes": 0,
    "outbound_queue_high_watermark": 0,
    "outbound_queue_high_watermark_bytes": 0,
    "outbound_evicted": 0,
    "telemetry_evicted": 0,
    "outbound_rejected": 0,
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

The queues are heap-governed (no fixed capacity), so these are observability metrics, not utilization against a limit:

- `outbound_queue_depth`: Queued + in-flight entries (current)
- `outbound_queued_bytes`: Retained payload bytes (the queued FIFO plus the in-flight entry)
- `outbound_queue_high_watermark`: Peak queue depth since boot
- `outbound_queue_high_watermark_bytes`: Peak retained payload bytes since boot
- `outbound_evicted`: Entries evicted under memory pressure (all kinds)
- `telemetry_evicted`: Evicted entries of the telemetry kind
- `outbound_rejected`: Admissions rejected because the free-heap reserve could not be restored

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
- `utc_not_valid`: UTC snapshot unavailable

### Queue Priority

Health messages use `RETENTION_PRIORITY_HEALTH = 70`, the lowest priority class.

### Outage Behavior

Health messages are only generated when:
1. Network snapshot is available (`network_stack_ready = True`)
2. MQTT is connected (`mqtt_connected = True`)

This prevents health messages from accumulating during MQTT outages. A boundary skipped during an outage is never replayed after recovery: the scheduler waits for the next normal-runtime-relative boundary.

## Hardware detection

The firmware explicitly identifies the hardware at startup using `os.uname().machine`.

- Raspberry Pi Pico W: `RPI_PICO_W with RP2040` → canonical type `pico_w`, heap reserve 64 KiB
- Raspberry Pi Pico 2 W: `RPI_PICO2_W with RP2350` → canonical type `pico_2_w`, heap reserve 128 KiB
- Unsupported hardware raises a clear `RuntimeError` during startup

The hardware module provides:
- Canonical hardware type identifiers (`HARDWARE_TYPE_PICO_W`, `HARDWARE_TYPE_PICO_2_W`, `HARDWARE_TYPE_UNKNOWN`)
- Board-specific minimum free-heap reserves (`PICO_W_MIN_FREE_HEAP_BYTES`, `PICO_2_W_MIN_FREE_HEAP_BYTES`)
- `classify_machine()` — the single source of truth mapping a machine string to canonical type and board heap reserve. Both `detect_hardware()` (startup) and `SystemInformation.get_machine()` (telemetry/health) classify through it, so they can never disagree.
- Detection function `detect_hardware()` returning an immutable result dict, failing fast on unknown hardware

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

- FIFO and heap-governed: no fixed entry count or byte budget — admission is decided against the board's minimum free-heap reserve (see Memory safety below), so the queue retains whatever the heap can safely hold.
- Core 1 supplies only a message kind plus domain data; it does not know MQTT topics.
- Core 0 maps the kind to the authoritative MQTT topic, publishes with QoS 1, and owns the MQTT envelope (sequence, runtime_id, source, firmware_version, message_schema_version). Kinds: TELEMETRY → `mqtt_topic_telemetry`, COMMAND_RESPONSE → `mqtt_topic_command_response`, HEALTH → `mqtt_topic_health`, LOG → `mqtt_topic_log`.
- The startup log and connection logs travel as KIND_LOG entries; no hardcoded topics cross into Core 1.
- The sender owns every message field, including `uptime_ms` and `timestamp` (the latter null when UTC is unsynchronized). A queued message must NOT carry any envelope key at the top level, or the wire document would repeat a member name.
- Core 0 injects the envelope at publish time by splicing its five members into the stored serialized object before its closing brace: the payload bytes are never decoded, parsed, or re-serialized on the publish path, so publishing allocates only the small envelope fragment plus the assembled frame.
- The MQTT client waits for the matching PUBACK before the next publish proceeds, naturally enforcing one application QoS 1 publish in flight. The wait is bounded by `mqtt_broker_response_timeout_sec`, so a blackholed link fails the publish (the entry stays in flight) instead of blocking the run loop.
- Retention priority is explicit: lower numeric values are more important.
- Priority classes are: CRITICAL 10, ERROR 20, WARN 30, TELEMETRY 40, INFO 50, HEALTH 70.
- Under memory pressure (free heap below the reserve even after `gc.collect()`), the queue finds the least-important queued class (highest numeric priority). If the incoming message is at least as important, the oldest entry in that least-important class is evicted, the heap is reclaimed and rechecked, and this repeats until the reserve is restored or no eligible lower-priority entry remains. If the incoming message is less important than everything queued, or the queue is empty, it is rejected without dropping a valid entry.
- The current Core 1 command response uses CRITICAL 10; telemetry uses TELEMETRY 40; health messages use HEALTH 70; the startup log uses INFO 50.
- An in-flight QoS 1 entry is retained until its PUBACK (its payload bytes stay counted in the retained-bytes metric) and is never an eviction candidate.
- A failed publish never discards the in-flight entry: it stays in flight and `take()` returns it again, so Core 0 retries until the broker PUBACKs (QoS 1 at-least-once delivery).
- **Sequence identity across an ambiguous failure.** QoS 1 has an ambiguous failure mode: the PUBLISH frame can reach the broker while the PUBACK is lost, so a failed publish attempt may still have been delivered. The `sequence` envelope member is therefore claimed *before* the first transmission attempt and stamped on the logical object — the queue entry, or Core 0's persistent response/reboot dict for its own retryable messages — and is never rolled back or reused by a different message. A retry of the *same* logical message reuses its stamped number (both copies identify one message — legitimate QoS 1 duplicate delivery), while a *different* message (e.g. a `mqtt_connection_established` log published after a reconnect) always receives a fresh number. This makes `(runtime_id, sequence)` a safe unique event identity and lets a receiver recognize a retry of the same logical message. Claiming happens in `core0._claim_wire_sequence`, invoked from `_publish_entry` (queue/connection-log path) and from the response/reboot retry paths.

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
validate maximum payload size (MAX_OUTBOUND_MESSAGE_BYTES = 16 KiB)
    ↓
admit serialized bytes to outbound queue
    ↓
later MQTT publish splices Core 0's envelope members into the stored bytes
(the message body itself is never parsed or re-serialized)
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

#### Memory safety

Two rules keep the outbound path safe on MCU-scale heap (Pico W: 256 KiB SRAM). The per-message size check happens after `json.dumps()` + UTF-8 `encode`, so at peak allocation the object graph, the serialized `str`, and the encoded `bytes` are all resident at once — a large payload can therefore exhaust heap before a limit is even reached:

- **Per-message ceiling** — `message_serializer.MAX_OUTBOUND_MESSAGE_BYTES = 16 KiB`. Bounds a single message's transient peak (graph + str + bytes ≈ 3x the payload ≈ 48 KiB) and keeps the largest legitimate message (the one-shot startup log, the only payload that grows with device count) comfortably under the limit with margin. The queue enforces it on both admission paths: `put()` via the serialization step, and `put_with_kind()` via a direct byte-length check on the pre-serialized payload, so a caller bypassing `serialize_and_validate_message()` cannot admit a larger entry.
- **Global free-heap reserve** — the board's minimum free heap (`hardware.py`: 64 KiB Pico W, 128 KiB Pico 2 W), the single source of truth for queue memory safety. An entry may be retained only while `gc.mem_free()` is at or above the reserve, so the queue cannot exhaust heap on its own during an MQTT outage, regardless of how many entries it holds.

Admission is heap-governed under one shared heap-admission lock (the heap is global to both cores, and both queues share the lock): fast path — reserve satisfied, admit, no garbage collection; pressure path — `gc.collect()` once, and if the reserve is still not restored, evict the oldest entry in the least-important eligible queued class, reclaim, and recheck, repeating until the reserve is restored or nothing eligible remains — then admit or reject. A valid queued entry is never dropped to admit a less important one, and the in-flight entry is never evicted. `OutboundQueue.status()` reports `queued_bytes`, the depth/bytes high watermarks, and the eviction/rejection counters for observability.

### 2. `event_queue`

Core 0 -> Core 1. Contains private discrete commands/events. `get-details` is
owned by Core 1 because the authoritative `SystemInformation` instance and
device-manager state live there. The command requires an empty payload and
returns a current snapshot containing every entry in
`SYSTEM_INFORMATION_SECTIONS` as `command_response.payload.data`.

- FIFO and heap-governed: the same global free-heap reserve and shared heap-admission lock as the outbound queue, but with NO eviction — an admitted event is a discrete control operation and is never displaced by a newer one. Under memory pressure the new event is rejected and the caller reports the `intercore_event_queue_memory_pressure` failure.
- Every admitted event matters.
- Entries are never automatically published to MQTT.
- Reboot never enters this lane; Core 0 owns reboot completely.

### 3. `state_mailboxes`

Core 0 -> Core 1 latest-value state.

- `network_snapshot`: Wi-Fi status, IP, RSSI, connection counts, `network_stack_ready` flag
- `utc_snapshot`: Current UTC time, ticks base, runtime start
- `hardware`: Detected hardware type, machine string, heap reserve
- `core_1_activity_ms`: Timestamp of last Core 1 activity report (in milliseconds) — written by Core 1, read by Core 1 for health reporting and by Core 0 as the input to the liveness watchdog

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

The connect loops in `establish_network()` are intentionally unbounded (a watchdog is listed under "Features intentionally not carried into the baseline"). If the configured SSID is absent, or the broker is unreachable, Core 0 retries forever: each Wi-Fi attempt observes the link for up to 20 s (200 × 100 ms), sleeps the configured backoff delays between attempts, then the whole sequence restarts. Core 1 never starts, and the flashing connection LED (50 ms on / 50 ms off) is the only visible state of this boot loop. The startup verification steps behave the same way: a failed probe or a failed UTC pass drops the MQTT session, re-establishes the network, and retries the pass, so a transient failure after connection is recovered rather than fatal (see Deterministic startup sequence).

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

Both probes must succeed with matching PUBACKs before Core 1 starts and before the network snapshot marks `network_stack_ready = True`. A failed probe is not fatal: the MQTT session is dropped, the network is re-established, and the whole verification pass retried (a `MemoryError` still fails fast).

## MQTT keepalive

The CONNPACK advertises `mqtt_keepalive_sec` (30 s default), so the broker disconnects the client if it sees no client packet within 1.5 x keepalive (45 s). Application traffic alone does not cover this gap — the health interval is 60 s — so Core 0 sends PINGREQ explicitly:

- `Mqtt` tracks the last outbound MQTT activity (connect, PUBLISH, PINGREQ).
- When idle for `keepalive / 2` (15 s default), `ping_due()` returns true and the Core 0 run loop sends a PINGREQ while no outbound entry is being published (a PUBLISH itself resets the broker's keepalive timer).
- `ping()` waits for PINGRESP with a bounded timeout (capped at 10 s) so a dead link surfaces as a disconnect within one interval instead of blocking the run loop.
- A failed PINGREQ marks the connection disconnected; `_recover_network_if_needed` reconnects.

## MQTT wire client

Three invariants in `mqtt_client.py` keep the bounded-wait contract sound (every blocking broker wait must be deadline-limited, so a dead link surfaces as a failure instead of stalling the run loop):

- **One packet ID helper**: `next_packet_id()` advances the counter and wraps 65535 back to 1 (0 is reserved). QoS 1 publish, subscribe, and `Mqtt.get_next_packet_id()` (the network-probe path) all draw IDs from this single helper, so every message takes the next of 1, 2, ..., 65535, 1, 2, ... and the wrap is defined in exactly one place.
- **Socket mode is the caller's contract**: `wait_msg()` reads in whatever socket mode it finds and never changes it, and every caller runs it in blocking-with-timeout mode — so a read always returns a full length and a link that stalls after the first frame byte surfaces as a timeout instead of blocking forever. `publish`/`ping`/`subscribe` bound their waits with their own timeouts — and `publish` installs the QoS 1 timeout *before* the first PUBLISH frame byte is written, so a blackholed link whose send stops making progress fails the publish bounded (into recovery) instead of wedging Core 0 inside `sock.write()`, where the Core 1 heartbeat check could never run. `check_msg()` is the poller: it first decides *readiness* with a non-blocking `select` poll (one readable byte means a packet has started; nothing returns `None` immediately, leaving the socket mode untouched), and only then parses the packet — that parse runs under the finite `mqtt_broker_response_timeout_sec` rather than non-blocking, because a non-blocking multi-byte read can short-read a PUBLISH split across TCP reads and deliver a corrupt frame. `check_msg()` restores normal blocking mode on the way out; in MicroPython `setblocking(True)` is identical to `settimeout(None)`, so a poll that left the socket non-blocking, or a wait that forced blocking mid-read, would silently clear the next operation's timeout.
- **The handshake is bounded the same way**: `Mqtt.connect()` calls `MQTTClient.connect(timeout=mqtt_broker_response_timeout_sec)`, which installs the finite socket timeout that the CONNACK wait runs under. `subscribe()` does not install its own timeout — it relies on the one `connect()` left in place — so that same finite timeout carries across both SUBACK waits. A broker that accepts the TCP connection and then goes silent fails the attempt within the timeout (the reconnect backoff then retries) instead of wedging Core 0. Once both subscriptions succeed, `Mqtt.connect()` restores normal blocking mode; every later operation (PUBACK, PINGRESP) installs and restores its own timeout.
- **Disposal of a failed client never writes**: `Mqtt._close_old_client()` runs only at the start of a `connect()` attempt, and `connect()` is only entered while the session is not connected — so the client it disposes is always failed or never established. It therefore closes the stale socket directly instead of sending an MQTT DISCONNECT frame: after a failed bounded exchange the socket's `finally` has restored it to infinite-blocking mode, and a DISCONNECT write into that already-failed, blackholed link would be an unbounded `sock.write()` that wedges Core 0 forever — and with Core 0 wedged, its Core 1 heartbeat watchdog could never run either. No write at all means nothing to wedge; the broker drops the clean session when the TCP connection closes.

## Network recovery

When the run loop detects a lost link (Wi-Fi down, or MQTT down with Wi-Fi up), `_recover_network_if_needed` re-establishes it before any further processing. A blackholed broker (TCP up, but no PINGRESP or PUBACK ever arrives) is detected the same way: every blocking broker wait is bounded, so a dead link surfaces as a failed ping or publish, marks the connection disconnected, and recovery fires on the next loop iteration. Disposing of the stale client as part of that recovery is itself bounded by construction — it closes the failed socket directly instead of writing a DISCONNECT frame into the dead link, so the cleanup cannot wedge Core 0 (and thereby skip its Core 1 heartbeat watchdog) either.

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
- reinitialization failure logging: the first reinit failure for a device is warned once; repeats while the same device stays in pending-reinit are suppressed until a successful reinit clears the flag, so a stuck device warns once instead of every cycle (a later independent failure warns again)

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
- Startup duration as `duration_ms` — explicitly named so it is not confused with the envelope's device uptime (`uptime_ms`).
- Device counts (configured / ready / initialization-failed) plus per-device ready and failed lists (device, name, sensor type).
- A full `system_information` snapshot (all sections collected through `SystemInformation`).

Telemetry admission is gated on startup-log admission: if the first admission attempt is rejected, Core 1 waits 100 ms and retries once. If it is still rejected, Core 1 raises and halts — no telemetry goes out without the startup log being admitted.

## Core 1 liveness heartbeat

Core 1 refreshes the `core_1_activity_ms` state mailbox on a 5-second deadline, independent of read-loop phase: the refresh fires on any 20 ms loop iteration once its deadline has passed, rather than on a fixed grid aligned to the loop step. A deadline-based refresh cannot starve even when the actual loop period (sleep plus processing) does not divide the refresh window evenly.

Health messages report `core_1_active` as true while the stamp age is within threshold (`3 × read_loop_sec`, minimum 60 seconds), and add `core_1_inactive` to the degradation reasons when it is exceeded.

Core 0 is the independent consumer of the heartbeat. The Core 0 run loop checks the stamp first on every pass (`_watch_core_1_heartbeat`): before any stamp exists (Core 1 has not started) the check is a no-op, so the unbounded startup connect loops are unaffected; once a stamp exists, age at or beyond `_CORE_1_HEARTBEAT_STALE_TIMEOUT_MS` (30 s, a static constant in `core0.py`, not a config key) resets the MCU via `machine.reset()`. The 30 s bound is more than six missed 5 s refreshes — far beyond any live-loop processing gap — and below the 60 s diagnostic threshold. This closes the failure mode where a dead Core 1 wedges the whole sensor (telemetry and health stopped, nothing to recover them) without Core 0 ever noticing: a dead Core 1 can no longer build the health message that would report `core_1_inactive`, so it cannot report its own death. A reboot starts clean: the mailbox is empty until the new Core 1 registers, and the check stays a no-op meanwhile.

## Normal-runtime scheduling anchor

All periodic Core 1 runtime work — telemetry and health — is scheduled from one shared epoch, `normal_runtime_start_ticks_ms`, captured exactly once, immediately after `system_startup_completed` has been successfully admitted to the outbound queue. No periodic work begins before that admission.

- `boot_ticks_ms` remains the boot-lifetime reference: firmware uptime (`uptime_ms`), startup duration, and lifecycle diagnostics are all measured from boot. It is no longer the scheduling origin for periodic work.
- `normal_runtime_start_ticks_ms` anchors periodic operational work: telemetry boundaries at `anchor + n × read_loop_sec`, health boundaries at `anchor + n × health_interval_sec`. With a 13-second startup, `read_loop_sec = 20`, and `health_interval_sec = 60`, telemetry falls at 33/53/73/93... seconds of uptime and health at 73/133/193/253... seconds of uptime.
- The two schedulers share the epoch but stay independent: each keeps its own deadline, neither derives its deadline from the other, and neither may fire the other. When both are due (health interval is a multiple of the read loop), both process normally through the existing outbound queue at their existing priorities (TELEMETRY = 40, HEALTH = 70).
- The anchor is captured once per runtime. A Wi-Fi reconnect, MQTT reconnect, UTC resynchronization, device reinitialization, or queue drain must never re-capture it. Only a true reboot — a new `runtime_id` and new `boot_ticks_ms` — creates a new anchor.
- Deadlines advance from the previous scheduled deadline (deadline + interval), never from the moment a message was actually generated, so per-iteration processing delay cannot accumulate into drift.

## Uptime accounting

Every message envelope carries `uptime_ms`, and both cores compute it from `uptime.py`: each core seeds a small state from `boot_ticks_ms` and advances it by the delta between consecutive recent samples (`create_uptime_state` / `current_uptime_ms`). No code diffs the original boot tick against the current tick in one step — `time.ticks_diff()` is only guaranteed correct within half a tick period, so the one-shot form wraps on a long-running device while the accumulated form stays monotonic and correct across a wrap.

## Periodic Telemetry

Telemetry cadence is anchored to the normal-runtime anchor: `read_loop_sec` defines fixed boundaries at `anchor + n × read_loop_sec` (a 20-second read loop produces boundaries at +20s, +40s, +60s relative to normal-runtime start). Telemetry remains historical sensor/runtime data: during an MQTT outage it may continue to enter the bounded outbound queue under the existing retention/eviction rules. Telemetry generation is gated on startup-log admission — no telemetry before `system_startup_completed` is admitted.

- **Missed boundaries are skipped, never replayed** (same policy as health): if a device read stalls long enough to cross one or more boundaries, the scheduler performs the current sample once and advances directly to the next future boundary — it does not fire catch-up reads for the elapsed boundaries. Telemetry is a current sample, not a replayable record, so a stall cannot reconstruct the missed samples; a catch-up burst would only add near-duplicate samples, JSON work, and queue admissions immediately after an overload. This is distinct from outage buffering (above), which still applies: an MQTT outage queues telemetry rather than dropping it.

## Periodic Health Messages

Core 1 generates health messages on a normal-runtime-anchored cadence: `health_interval_sec` defines fixed boundaries counted from `normal_runtime_start_ticks_ms` (a 60-second interval produces boundaries at +60s, +120s, +180s, +240s relative to normal-runtime start), independent of when startup merely completed and independent of the telemetry scheduler. No immediate health message is generated after `system_startup_completed`. The health message contains current-state diagnostic fields without turning the payload into a full system information report.

### Scheduling

- **Normal-runtime-anchored**: the first deadline is `normal_runtime_start_ticks_ms + health_interval_sec`, where the anchor is captured once, immediately after successful `system_startup_completed` admission.
- **Missed boundaries are skipped, never replayed**: boundaries that elapsed during any bounded delay (e.g. a stalled loop) are not emitted as catch-up reports; the scheduler advances directly to the next future boundary.
- **No cumulative drift**: after a boundary the deadline advances from the previous deadline (deadline + interval), not from the moment the message was actually generated, so per-iteration processing delay cannot accumulate.
- **At most one message per boundary**: when a boundary is due, at most one current-state health report is emitted (subject to the generation rules below), then the deadline advances past any elapsed boundaries.

### Generation Rules

1. **Network ready required**: Health messages are only generated when `network_stack_ready = True` and `mqtt_connected = True`. This prevents accumulation during MQTT outages.

2. **Authoritative data sources**:
   - Hardware: `StateMailboxes.get_hardware()` (canonical detected state)
   - RSSI: `StateMailboxes.get_network_snapshot().get("rssi")`
   - Core 1 activity: `StateMailboxes.get_core_1_activity_ms()`
   - Heap: `gc.mem_free()` (current measurement)
   - Devices: `SystemInformation.get_devices()` (backed by `DeviceManager.get_status_snapshot()`)
   - Queue: `OutboundQueue.status()` (depth, retained bytes, high watermarks, eviction/rejection counters)
   - UTC: `StateMailboxes.get_utc_snapshot()`

3. **Monotonic time calculations**:
   - All age calculations use `time.ticks_diff()` for monotonic elapsed time
   - Uptime since boot is accumulated from deltas between recent samples (`uptime.py`), never as a single `ticks_diff(now, boot)` — that one-shot form is only guaranteed within half a tick period and wraps on long-running devices
   - UTC sync age: integer division of milliseconds by 1000

4. **Memory pressure**: free heap below the board reserve (`low_free_heap`). Both queues are admitted against the same global reserve (see Memory safety), so a below-reserve heap is itself the queue-pressure condition — no separate queue-utilization threshold exists.

5. **Low-priority retention**: Uses `RETENTION_PRIORITY_HEALTH = 70`, the lowest priority class

### Degradation Triggers

Health status is "degraded" when any of these conditions are true:
- `network_stack_not_ready`: Core 0 network not initialized
- `wifi_not_connected`: Wi-Fi disconnected
- `mqtt_not_connected`: MQTT broker connection lost
- `core_1_inactive`: Activity age exceeds threshold (3x read_loop_sec, minimum 60 seconds)
- `low_free_heap`: Free heap below configured reserve
- `device_count_mismatch`: Active devices don't match configured count
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
8. **UTC synchronization**: One pass of up to 3 bounded attempts (each waiting up to `mqtt_broker_response_timeout_sec`); a failed pass is retried after re-establishing the network
9. **Publish initial snapshots**: UTC and network snapshots to state mailboxes
10. **Stop connection LED**: LED turns off
11. **Return**: Core 0.start() returns, Core 1 starts

Every step is self-healing: the connect steps (Wi-Fi, MQTT) retry forever, and the verification steps (each probe, the UTC sync) drop the MQTT session, re-establish the network, and retry the whole verification pass when one fails. Core 1 never starts until a complete, clean pass succeeds, so a transient blip after connection (a dropped PUBACK, a brief UTC-server outage) is recovered rather than halting the device until a reset. A `MemoryError` still propagates out of `start()` (fail-fast), so an out-of-memory device is not looped.

## UTC time synchronization

Core 0 is the only UTC acquirer. Requests are published to `mqtt_topic_info_request` with a unique `request_id`; the server is expected to answer on `mqtt_topic_info_response` echoing that `request_id`.

- **Startup (required, self-healing)**: one pass of `_synchronize_utc_required()` makes a fixed number of attempts (3 — a constant, deliberately not derived from the timeout setting), each waiting up to `mqtt_broker_response_timeout_sec` for a valid response while pumping MQTT. A failed pass is not fatal: the network is re-established and the whole verification pass retried (see Deterministic startup sequence), so a transient UTC-server outage no longer halts startup until a reset.
- **Steady state (non-blocking)**: when the snapshot is older than `datetime_sync_interval_min`, the run loop sends a request and records a `mqtt_broker_response_timeout_sec` deadline. The run loop keeps servicing the outbound queue, network recovery, and command responses; the answer arrives through the regular `check_msg()` pump. The run loop never blocks on a UTC request.
- **Timeout and retry**: a pending request whose deadline passed is discarded, and re-requests are throttled to at most one per 30 seconds until a valid response arrives. An unresponsive time server therefore cannot stall the run loop or flood the broker. A malformed-but-reachable answer gets a much shorter backoff (~0.5s) since the server demonstrably answered us.
- **Response validation**: a response is accepted only if it matches the current schema version, is targeted at this device, carries the matching `request_id`, and contains a valid `timestamp` and positive integer `utc_epoch_ms`. A malformed answer to *our own* pending request clears that request and re-keys the retry throttle to a short ~0.5s backoff (instead of the full 30s measured from the original send); a response for any other request id is ignored without disturbing pending state (our response may still be in flight).

## Features intentionally not carried into the baseline

These should be added individually only after the baseline passes:

- advanced reboot-delivery state machines;
- advanced network recovery generations/state machines;
- dynamic configuration;
- advanced watchdog/cross-core recovery (the minimal Core 0 stale-heartbeat reset is present since 0.4.9);
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
