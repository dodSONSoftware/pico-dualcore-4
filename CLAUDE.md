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
- `outbound_queued_bytes`: Queued FIFO payload bytes (in-flight excluded, matching the byte admission budget)
- `outbound_max_queued_bytes`: Maximum queued payload bytes (32 KiB)
- `outbound_queue_byte_utilization_percent`: (queued_bytes * 100) // max_queued_bytes

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
- Run with: `python -m pytest tests/`
- The suite covers core-ownership boundaries (AST checks), config validation, inter-core bus semantics, health payloads, normal-runtime-anchored telemetry/health scheduling, MQTT keepalive, the bounded MQTT connect/subscribe handshake, the bounded QoS 1 publish write/PUBACK exchange, inbound MQTT packet size limits, UTC synchronization, network recovery, the self-healing startup verification, the Core 1 liveness heartbeat, the Core 0 stale-heartbeat watchdog, the Core 0 publish-path envelope splice, sequence identity across an ambiguous QoS 1 failure (a failed-attempt sequence is never reused by a different message and a retry preserves it), and the MemoryError propagation contract on the Core 0/Core 1 message paths
- Hardware testing requires an actual Pico device

## Hardware Notes

- **Pico W**: Wi-Fi only, 256KB RAM
- **Pico 2 W**: Wi-Fi, 264KB RAM, faster CPU
- Onboard LED is Core 0-only via `LEDManager`
- Never call `machine.reset()` from Core 1

## Version History

- **0.4.18**: Made the queued-byte budget visible to system telemetry and health, so the queue's memory-protection boundary can no longer be full while health reports no pressure. The byte budget (0.4.8: 32 KiB of queued payload bytes, enforced alongside the 16-entry count) was only ever reported as entry counts: `SystemInformation.get_queues()` (the startup-log source) and the health payload both carried no byte view, and `outbound_queue_pressure` was computed from entry utilization alone — a handful of large entries (e.g. 4 × ~8 KiB) could sit at 25% entry utilization while the byte ceiling was at 100%, the exact condition the budget exists to flag. The health payload now carries `outbound_queued_bytes`, `outbound_max_queued_bytes`, and `outbound_queue_byte_utilization_percent` (sourced from a new single-lock `OutboundQueue.get_health_metrics()` reading both budget views consistently), `SystemInformation.get_queues()` now exports the two byte fields, and `outbound_queue_pressure` fires at `max(entry_utilization, byte_utilization) >= 75%`. Queued bytes exclude the in-flight entry, matching the byte admission budget in `_admit_locked`; the entry-depth field still counts it, as before. The byte-utilization computation is integer-only (MCU-safe), no new config or schema surface. Additive wire change (three new health payload fields); no `config_schema_version` change.

- **0.4.17**: Made the wire `sequence` reliable across an ambiguous QoS 1 failure. The sequence was consumed only after a successful PUBACK, so in the ambiguous case — the PUBLISH reached the broker but the PUBACK was lost — the number was never consumed and was handed out again: a `mqtt_connection_established` log published after the reconnect took the in-flight telemetry's sequence (a `(runtime_id, sequence)` collision on the wire), and the telemetry retry then took a still-newer one, so the same logical sample appeared under two identities and an unrelated message reused the sample's number. The sequence is now claimed before the first transmission attempt and stamped on the logical object — the queue entry, or Core 0's persistent response/reboot dict for its own retryable messages (`_claim_wire_sequence` in `core0.py`) — and is never rolled back: a retry of the *same* logical message reuses the stamped number (both copies identify one message, legitimate QoS 1 duplicate delivery), while a *different* message always gets a fresh number. `(runtime_id, sequence)` is therefore now a safe unique event identity and a retry is recognizable as the same message. The wire format is unchanged (same top-level keys, same Core 0 values) and the QoS 1 at-least-once delivery guarantee is unchanged. Failure-path behavior change (no `config_schema_version` change).

- **0.4.16**: Bounded the QoS 1 PUBLISH frame writes, not just the PUBACK wait. `MQTTClient.publish()` installed the socket timeout only *after* all four socket writes (header, topic, packet id, payload), so a link failure that leaves TCP apparently established while sends stop making progress wedged Core 0 inside `sock.write()` — and with Core 0 wedged, its Core 1 heartbeat check (the 0.4.9 watchdog) never ran, leaving no recovery path. The timeout is now installed before byte 1 of the frame and restored only after the exchange finishes, so the frame writes and the PUBACK wait share one bounded deadline: a stalled write surfaces as a bounded error into `Mqtt.publish_qos1`'s disconnect + network recovery (the entry stays in flight, at-least-once semantics unchanged). Wire order (header, topic, packet id, payload) and the QoS 0 path are unchanged; a failed timeout installation now aborts before any byte is transmitted instead of after the full frame. Failure-path behavior change (no `config_schema_version` change).

- **0.4.15**: Made `MemoryError` propagation consistent on the Core 0/Core 1 message paths. Heap exhaustion is a fatal condition, not a failed optional diagnostic, but several allocation-heavy paths swallowed it in a generic `except Exception` handler and kept the core running on top of an exhausted heap: `core1._collect_system_information_full()` (a failed section was recorded as `{"error": ...}`), `core1._try_queue_startup_log()` and `core1._try_queue_health_message()` (the message was silently discarded as if serialization had merely failed), `core1._build_health_payload()` (heap/hardware fallbacks), the Core 1 startup hardware storage, `core0._service_pending_connection_log()`, and `core0._publish_core0_command_response()` (whose handler was `except (MessageTooLargeError, Exception)` — effectively `except Exception`). Every one of these now re-raises `MemoryError` before the generic handler, matching the convention every other module already followed. Heap exhaustion now stops the core with a diagnosable error instead of obscuring the root cause or making fragmentation worse, and the callers' existing `except MemoryError: raise` clauses (e.g. `_perform_reboot`) can now actually fire. Ordinary (non-`MemoryError`) failures are still handled as before: reported and the message discarded. Failure-path behavior change only (no `config_schema_version` change).

- **0.4.14**: Stopped the telemetry scheduler from replaying missed boundaries as catch-up reads. When a device read stalled long enough to cross one or more `read_loop_sec` boundaries, the deadline advanced by a single interval per pass, so the loop fired an extra read almost immediately — and another, until it caught up: a burst of near-identical current samples, JSON work, and queue admissions landing right after the overload. Telemetry is a current sample, not a replayable record, so a stall cannot reconstruct the missed samples; the scheduler now performs the current sample once and skips directly to the next future boundary — the same missed-boundary policy the health scheduler already used. The skip is evaluated against a clock re-read after the read completes, because the loop's captured clock is stale by however long the read took and would otherwise under-advance and leave a catch-up read for the next pass. Outage buffering is unchanged (an MQTT outage still queues telemetry rather than dropping it); this only changes the pathological slow-read case. Static behavior change (no `config_schema_version` change).

- **0.4.13**: Eliminated the double serialization on the publish path. A queued message was serialized by its sender and then, on Core 0, decoded, parsed back into a dict, merged with Core 0's envelope fields, and re-serialized as a whole document (`dict → str → bytes → queue → str → dict → str → MQTT`): the message body was serialized twice, and the transient allocations, CPU, and fragmentation risk accumulated exactly where the queue was supposed to improve memory predictability. The contract is now: the sender carries its complete message — including `uptime_ms` and `timestamp` (null when UTC is unsynchronized) — and must not carry any envelope key at the top level (`sequence`, `runtime_id`, `source`, `firmware_version`, `message_schema_version`); Core 0 injects those five members at publish time by splicing them into the stored serialized object before its closing brace. The payload is never decoded, parsed, or re-serialized on the publish path, so publishing allocates only the small envelope fragment plus the assembled frame. The wire format is unchanged (same top-level keys, same Core 0 values) and the queue still stores immutable pre-serialized bytes — the frame is a fresh allocation, the entry is untouched. Core 1 no longer pre-embeds `runtime_id`/`source`/`firmware_version`/`message_schema_version` (they were always overwritten by Core 0 anyway), and the health/startup-log `timestamp` is computed from the shared UTC snapshot instead of being left null for Core 0 to fill (which required the parse it could no longer afford). Static behavior change (no `config_schema_version` change).

- **0.4.12**: Bounded the inbound MQTT packet size. `wait_msg()` now rejects a PUBLISH whose remaining length exceeds `MAX_INBOUND_PACKET_BYTES` (16 KiB — the same MCU-scale ceiling as the outbound limit) *before* allocating its payload: previously `sock.read(sz)` was handed whatever the broker's remaining-length field claimed, and a broker-side bug publishing an oversized command/info response could request an allocation large enough to exhaust the Pico W's RAM (no hostile traffic required). An oversized frame — like a fifth remaining-length byte (an MQTT protocol violation that was previously an unbounded read loop) — now closes the socket and raises `MQTTException`, which the `Mqtt` layer turns into a marked disconnect and Core 0's existing recovery path reconnects, instead of trying to buffer or drain the corrupt stream. Static constant (no `config_schema_version` change).

- **0.4.11**: Fixed the MQTT receive path parsing incoming packets in non-blocking socket mode. `check_msg()` now decides *readiness* with a non-blocking `select` poll — one readable byte means a packet has started; nothing returns `None` immediately and leaves the socket mode untouched — and only then parses the packet on a blocking-with-finite-timeout socket bounded by `mqtt_broker_response_timeout_sec`. Previously the whole multi-byte parse ran non-blocking, and because MicroPython documents that `read(n)` may return fewer bytes than requested in non-blocking mode, a PUBLISH split across TCP reads could short-read its topic or payload and deliver a corrupt frame (or raise a spurious disconnect) instead of waiting for the rest of the packet. Now a link that stalls after the first frame byte surfaces as a bounded timeout into the existing recovery path, and a ready packet is always fully read. `wait_msg()` and the bounded-wait invariants are otherwise unchanged; `check_msg()` now installs and restores its own finite timeout like `publish`/`ping`. Static behavior change (no `config_schema_version` change).

- **0.4.10**: Made startup verification self-healing to match the (already) unbounded connect loops. Previously the QoS 1 probes and UTC sync were the only fail-fast steps: a single transient blip after connection — a dropped PUBACK on a probe, or a brief UTC-server outage across the 3 bounded attempts — raised `RuntimeError` out of `core0.start()`, exited `main()`, and left the device stopped until a physical reset, even though a broker that was *never* reachable would have looped forever. `start()` now keeps the verification steps (probe #1 → drain → 5 s wait → probe #2 → UTC) in a new `_verify_startup_contract()` pass inside a retry loop: on any pass failure it drops the MQTT session (`mark_disconnected()`), re-establishes the network, and retries the whole pass, reusing the existing `mqtt_reconnect_delays_sec` backoff. Core 1 stays gated because `start()` has not returned and `_network_stack_ready` is set only after a clean pass; a `MemoryError` still propagates (fail-fast) so an out-of-memory device is not looped. Static behavior change (no `config_schema_version` change).

- **0.4.9**: Core 0 became the independent consumer of the Core 1 liveness heartbeat, closing the P1 failure mode where a dead or wedged Core 1 thread (an unhandled `MemoryError`/`Exception`) wedged the whole sensor — telemetry and health silently stopped, a dead Core 1 could not build the health message that would report `core_1_inactive`, and Core 0 kept running indefinitely with no recovery. The Core 0 run loop now checks `core_1_activity_ms` first on every pass (`_watch_core_1_heartbeat` in `core0.py`): before any stamp exists (Core 1 has not started) the check is a no-op, so the unbounded startup connect loops are unaffected; once a stamp exists, age at or beyond `_CORE_1_HEARTBEAT_STALE_TIMEOUT_MS` (30 s — more than six missed 5 s refreshes, and below the 60 s `core_1_inactive` diagnostic threshold) logs FATAL and calls `machine.reset()`. Static constant (no `config_schema_version` change).

- **0.4.8**: Made the outbound message/queue limits MCU-safe. The per-message ceiling dropped from 128 KiB to 16 KiB (`message_serializer.MAX_OUTBOUND_MESSAGE_BYTES`) — the check runs after `json.dumps()` + UTF-8 encode, so the object graph, serialized `str`, and encoded `bytes` are all resident at once and a 128 KiB payload could not be admitted safely on a Pico W; 16 KiB bounds a single message's transient peak (~48 KiB) with margin over the largest legitimate message (the startup log). `OutboundQueue` is now bounded by BOTH entry count and an aggregate queued-byte budget (`DEFAULT_MAX_OUTBOUND_QUEUED_BYTES` = 32 KiB), so 16 retained payloads can no longer exhaust heap on their own; admission enforces both budgets and only evicts when it frees enough room (by count or bytes), never dropping a valid entry to admit one that would not fit. Both bounds are static constants (no `config_schema_version` change); `OutboundQueue.status()` now also reports `queued_bytes` and `max_queued_bytes`.

- **0.4.7**: Fixed the MQTT handshake blocking indefinitely: `Mqtt.connect()` now passes `mqtt_broker_response_timeout_sec` to `MQTTClient.connect()`, bounding the CONNACK wait — and, since `subscribe()` inherits that socket timeout, both SUBACK waits. A broker that accepts the TCP connection and then stops responding now fails the connect attempt (retried with the reconnect backoff) instead of wedging Core 0 forever; the socket returns to normal blocking mode once both subscriptions succeed.

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
