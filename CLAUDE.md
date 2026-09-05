# CLAUDE.md — Project Guidelines for AI Assistants

## Project Overview

This is a dual-core MicroPython firmware for Raspberry Pi Pico W/Pico 2 W. Core 0 handles network operations (Wi-Fi, MQTT, UTC sync, reboot), while Core 1 handles sensors and devices. Communication occurs over a four-lane inter-core bus (two heap-governed FIFO lanes, plus latest-value state snapshots and the config-update request/result lane).

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
- `preferred_free_heap_bytes`: Board-specific preferred reserve — where memory-pressure handling (GC / reclaiming low-retention entries) begins; not a rejection wall
- `minimum_free_heap_bytes`: Board-specific hard survival floor that admission must protect
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
- `outbound_rejected`: Admissions rejected because the hard free-heap floor could not be restored

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
7. **Bounded inbound packets**: An inbound MQTT packet's remaining length must not exceed `MAX_INBOUND_PACKET_BYTES` (20 KiB — derived as the worst-case valid write-config command, 16,865 bytes in the conservative wire form, plus margin) before its payload is allocated, and the remaining-length field itself is bounded to MQTT's maximum four bytes. An oversized or malformed frame drops the connection (disconnect + recovery reconnect) instead of requesting a read that could exhaust Pico RAM. The frame ceiling bounds the socket read; the `json.loads()` peak (decoded string + object graph, a bounded multiple of the ceiling) is backstopped by the `MemoryError` → controlled-reset boundary, and the decoded string is freed as soon as the parse succeeds.
8. **Final recovery boundary**: Deterministic startup validation (hardware, config) fails fast and stays visible — never a reboot loop. Once operational runtime has begun (`Core0.start()` through `Core0.run()` in `main.py`), an unrecoverable Core 0 exception (`MemoryError` or otherwise) is a controlled `machine.reset()`, not permanent application termination — the reverse of Core 0's heartbeat watchdog, which recovers a dead Core 1.
9. **Trusted-network security model**: The broker and devices are assumed to sit on a trusted private network, so MQTT command authorization is provided by network-level access control, not by any in-firmware credential or per-command policy. The broker must not be exposed to untrusted or public networks. The firmware defends the *integrity and boundedness* of inbound frames (wire/QoS gates, schema gate, command-validation order, bounded payloads), not the identity of the sender. This makes the absence of per-command authorization an intentional trust boundary, not a gap — see the Security model section of `ARCHITECTURE.md`.

## Critical Files

| File | Purpose |
|------|---------|
| `main.py` | Entry point, orchestrates startup sequence |
| `core0.py` | Network stack (Wi-Fi, MQTT, UTC, reboot, keepalive, recovery, Core 1 liveness watchdog, read-config/write-config command execution) |
| `core1.py` | Device lifecycle, sensor reads, telemetry, health messages, liveness heartbeat |
| `intercore.py` | Four-lane message bus (two heap-governed FIFOs, latest-value state snapshots, and the config-update request/result lane) |
| `device_manager.py` | Device lifecycle management (init retries, read failures, reinit) |
| `device_factory.py` | Device construction from config; the supported `device_type` registry; the pure whole-device validation entry point (`validate_device_definition`) |
| `devices/system_information/validation.py` | Pure `system-information` config validation (host-importable, no `machine`); authoritative `SYSTEM_INFORMATION_SECTIONS` |
| `config.py` | Configuration schema validation (pure `validate_config`, including device validation) and per-core splitting |
| `config_manager.py` | Core 0 configuration persistence: ACTIVE-vs-PERSISTED state, change policy table, write transactions (`.tmp`/`.old`), boot recovery |
| `hardware.py` | Hardware detection (Pico W/Pico 2 W) and the board heap thresholds (preferred reserve / hard floor) |
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
- MQTT topics (`mqtt_topic_*`) are exact channel names: `+`/`#` wildcards are rejected (invalid in a PUBLISH Topic Name; inbound dispatch matches delivered topics by exact equality) and the eight channels must be pairwise distinct (dispatch matches by exact equality and the first matching branch wins, so a shared name silently disables a channel — equal command/info_response names make the command path unreachable — and a name shared with a locally published topic self-echoes the device's own traffic)
- Protocol-scale strings are bounded in **UTF-8 bytes**, not characters (the safety contract is the 16 KiB wire ceiling): `source` and device `id`/`name`/`sensor_type` at 64 bytes, `mqtt_broker_ip_address` at 253 bytes (DNS hostname maximum — `socket.connect()` resolves hostnames); a serialized-size invariant test pins the worst-case valid configuration's read-config response (envelope splice included) at or under `MAX_OUTBOUND_MESSAGE_BYTES` and its worst serialized write-config command (character-bounded `command_id`/`target` envelope) under `MAX_INBOUND_PACKET_BYTES`, so a spec-valid command is never dropped at the wire gate
- All config values validated before network starts

## Common Tasks

### Adding a New Device Driver

1. Create a `devices/my_sensor/` package: `__init__.py`, a **pure** config validator module (host-importable, no `machine` — `validate_config(config)` plus its `ALLOWED_CONFIG_KEYS`, raising `DeviceValidationError` with a stable `code`), and a driver module (e.g. `my_sensor_device.py`) implementing the `Device` interface from `devices/device.py` (`initialize(config)` should call the same pure validator before touching hardware, `read()`)
2. Register the `device_type` in `device_factory.py`: add it to the `_DEVICE_REGISTRY` (pure validator + allowed config keys) **and** to `create_device()` (construction)
3. Add the package files to `REQUIRED_PACKAGES` in `release.py`
4. Register in `config.json` with unique `id`
5. Add tests in `tests/` covering the pure validation (unknown keys, invalid values, unsupported type), `initialize()` reuse, and read/reinitialization behavior

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
- An exhausted MQTT connection sequence logs one warning naming the final cause retained in `Mqtt.last_connect_error` (reset/timeout/CONNACK/SUBACK failure) before the retry delay; the Wi-Fi exhaustion warning carries the same shape without a cause

### UTC Synchronization

- Startup is mandatory and self-healing: one pass of up to 3 attempts (each waiting up to `mqtt_broker_response_timeout_sec`); a failed pass re-establishes the network and retries, and Core 1 never starts until a clean pass succeeds (a `MemoryError` still fails fast)
- Steady-state re-sync is non-blocking: the run loop sends the request, keeps a `mqtt_broker_response_timeout_sec` deadline, and never blocks on the answer
- Re-requests are throttled to 30s; a malformed-but-reachable answer re-keys the throttle to ~0.5s

## Testing

- `tests/` contains host-side unit tests
- Run with: `python3 -m pytest tests/` (host tests run on CPython; `tests/conftest.py` shims the MicroPython-only `gc.mem_free`)
- Tests use the canonical fixture `tests/fixtures/config.json` (stable `source: "Test-Pico-2"`) as their base configuration, never the repository's deployment `config.json` — so changing a physical device's identity (or any deployment value) cannot change the suite's pass/fail. `config.json` at the repo root is deployment-only.
- The suite covers core-ownership boundaries (AST checks), config validation (including whole-device pure validation: the `device_type` registry, `validate_device_definition()` never constructing a driver, the pure `system-information` validator — including its unknown-config-key rejection — reused by `initialize()`, unknown device-definition and device-config fields aggregated into qualified `devices[<id>].<key>` / `devices[<id>].config.<key>` `unknown_config_fields` paths, unsupported device types rejected with `unsupported_device_type`, and a valid definition with absent hardware still passing write-time validation, the protocol-scale string byte bounds — `source`/device `id`/`name`/`sensor_type` at 64 UTF-8 bytes and `mqtt_broker_ip_address` at 253 bytes, multibyte values over the byte bound rejected — and the config-boundary serialized-size invariant (the worst-case valid configuration, every string field at its byte bound in the worst serialized form, still serializes its read-config response, envelope splice included, at or under `MAX_OUTBOUND_MESSAGE_BYTES`)), inter-core bus semantics (including the per-message byte ceiling on both outbound admission paths, `put()` and `put_with_kind()`, the two-threshold heap admission (the preferred reserve — 64 KiB Pico W / 144 KiB Pico 2 W — opening memory-pressure handling: `gc.collect()` first, then at most one eligible reclamation while the entry is still admitted, never a rejection wall by itself; the hard floor — 48 KiB / 128 KiB — as the survival boundary below which nothing may be retained, re-measured after the append's own allocations; the board values are pinned in `tests/test_hardware.py` and both thresholds flow from `classify_machine()`/`detect_hardware()` through `main.py` into both queues and the health payload), and the `put()` path's serialization `MemoryError` recovery — `put()` serializes the actual message with no fixed worst-case pre-serialization gate (a Pico W at ~85 KiB free heap — above its 64 KiB preferred reserve and 48 KiB hard floor — admits normal small telemetry); a `MemoryError` from the serializer first runs `gc.collect()` and retries before any data is discarded, then (only if it persists) reclaims one eligible retained entry at a time under the same retention-policy eligibility as admission — never a higher-priority entry, never CRITICAL — with `gc.collect()` between retries, until serialization succeeds or no eligible entry remains, at which point the `MemoryError` propagates to the firmware recovery boundary (it is not a transient `False` rejection); non-`MemoryError` serializer failures (validation, size, serialization) propagate unchanged; `put_with_kind()` on already-final bytes never serializes at all)), health payloads, normal-runtime-anchored telemetry/health scheduling, MQTT keepalive, the bounded MQTT connect/subscribe handshake, the named-cause MQTT exhaustion warning (`Mqtt.last_connect_error` retaining the final expected transport/protocol cause of a failed connect sequence and clearing it on a successful one; `establish_network()` naming it once per exhausted sequence and falling back to the prior generic message when no cause was retained), the bounded QoS 1 publish write/PUBACK exchange, the non-best-effort restoration of socket blocking mode (a failed restore of normal blocking mode after the connect handshake, a ping, or a QoS 1 publish fails the attempt/operation into Core 0's recovery instead of being swallowed and marking the link healthy), MQTT exception classification at the boundary (a programming failure — a bug in message handling, callback code, or state handling — propagates through `Mqtt.connect()`/`check_msg()`/`publish_qos1()`/`ping()` WITHOUT marking the session down, and escapes the Core 0 run loop to `main.py`'s controlled-reset boundary instead of being reclassified as a network outage, retried, and hidden; the expected transport failures — `OSError`, `MQTTException` — still mark the session down and drive the retry/recovery paths; with the run-loop contrast — a callback bug escapes `run()` while a transport failure on the same path keeps it alive — and the simulated "PUBACK lost" failures raising the genuine transport type), the MQTT exception taxonomy on the startup and reboot paths (a programming failure in `_utc_send_request()`/`_utc_wait_response()`, the QoS 1 startup probe, or the pending-reboot acknowledgement publish escapes to `main.py`'s controlled-reset boundary instead of being reclassified as a failed attempt retried into the same deterministic fault, while a transport failure still rolls back the armed UTC request ID, fails the probe/wait, or holds the reboot pending for a later pass — and `_drain_startup_mqtt_work()` no longer wraps the log service in a broad `except Exception` that would swallow a failure while the un-removed head log stayed queued for an infinite retry), the Wi-Fi boundary's exception taxonomy (only a transport failure — `OSError` — is a link condition that fails a `Wifi.connect()` attempt or reads the state as unknown, while a `MemoryError` and a programming failure — a deterministic `AttributeError`/`TypeError` or unexpected driver API incompatibility — escape `Wifi` to `main.py`'s controlled-reset boundary instead of being retried into the same deterministic fault by `establish_network()`, across `connect()`, `is_connected()`, `_current_status()`, and `snapshot()`; the one deliberate broad catch, the optional `PM_NONE` power-management probe, still tolerates whatever type a radio lacking it raises), the bounded MQTT reconnect cleanup (a failed client's socket is closed directly, with no DISCONNECT write into the dead link), inbound MQTT packet size limits, the inbound QoS profile (an inbound QoS 2 PUBLISH and the spec-invalid QoS 3 are rejected at the wire layer from the opcode byte — before the payload is read and before the callback can run, dropping the connection — so a nonconforming frame can no longer reach the command protocol before its rejection), UTC synchronization, network recovery, the self-healing startup verification, the Core 1 liveness heartbeat (including startup progress-boundary stamp refreshes, per-device normal-read boundary refreshes — the telemetry pass refreshes once per device before dispatching it, so the watchdog measures one device operation (a wedge) rather than the cumulative pass, and several legitimate sub-bound reads that together exceed Core 0's 30 s bound can no longer false-reset the board while a single wedged read of 30 s or more still does, runtime reinitialization boundary refreshes before each attempt, stepped in-sleep refreshes on both initialization paths — `device_initialization_retry_delay_ms` is unbounded, so the retry sleep runs in steps of at most 100 ms with the stamp refreshed at each step boundary, and a long configured delay can no longer age the stamp past Core 0's watchdog bound into a false reset — and the fresh-clock heartbeat stamped after a slow device read), the Core 0 stale-heartbeat watchdog (including its firing inside network connect/reconnect waits), the Core 0 publish-path envelope splice, sequence identity across an ambiguous QoS 1 failure (a failed-attempt sequence is never reused by a different message and a retry preserves it), the distinction between transient and permanent outbound rejections (False vs ValueError on both admission paths), the CRITICAL non-evictable retention floor (an admitted CRITICAL entry — a command response Core 1 no longer owns — cannot be evicted by another CRITICAL, so the new CRITICAL admission is rejected for the producer to retain and retry, while CRITICAL still evicts lower-priority entries and equal-priority replacement remains for the replaceable lower priorities), the global command protocol boundary (the global `message_schema_version` gate that ignores an unsupported-version message before any command or `info_response` handling — no response, no debounce entry, no UTC state change — and the staged validation order: target → bounded `command_id` → debounce claim → envelope unknown fields (all named, sorted) → required fields/types → command-name bound → supported-command registry → per-command broadcast policy → command-specific dispatch; the four-command registry — Core 0-owned `reboot`, `read-config`, `write-config` and Core 1-owned `get-details` — with `write-config` for `*` silently ignored while `*` is valid for the other commands; case-insensitive source/IP target matching; the 128-character `command_id`/`target` and 32-character `command` bounds, an over-long command answered with a bounded `invalid_command` that never echoes the name, and an over-long `command_id` dropped at ingress (no response possible without a bounded ID); the shared exactly-`{}` payload contract for `reboot`/`get-details` answering any unknown key with a sorted `unknown_fields`; the Core 0 `unsupported_command` answer preserving the actual name in `payload.command` for a command outside the registry, Core 1 no longer acting as the generic fallback for arbitrary command names; and the command-ID debounce cache — 16-entry FIFO, RAM-only, the first bounded ID claimed before deeper validation so a malformed duplicate is suppressed after the first response), the command-response channel invariant (a permanently rejected response is answered with a small error response instead of stalling the channel, and the code states the actual cause — `response_too_large` for an oversized response, `response_invalid` for a validation/serialization failure — on both Core 1's admission path and Core 0's pending-response service path, where an unsendable head response is replaced in place by the substitute so the responses behind it keep moving while a transient QoS 1 failure still leaves the original pending; the queue raises `OutboundMessageTooLargeError`, a `ValueError` subclass, only in the size case on both admission paths), the startup log's fail-fast admission (a permanent rejection is never re-submitted or waited out, while a transient one keeps its single retry) and its bounded fallback (only the size rejection is answerable by a different object: the detailed log's `MessageTooLargeError` raises the typed `StartupLogTooLargeError` and is answered by a bounded summary with no `ready_devices[]`/`failed_devices[]`/`system_information` that is admitted instead, so the verbose diagnostics cannot keep an otherwise valid configuration from entering normal operation; a non-size permanent rejection escapes with its actual reason and never builds the fallback), the outbound publish pacing gate (`mqtt_outbound_publish_delay_ms`: first publish immediate, interval measured from QoS 1 completion via tick-wrap-safe `ticks_diff`, 0 disables, applied to all application publish paths without blocking the Core 0 run loop or pacing keepalive, reboot held for its response slot, and startup pacing including the 5 s stabilization), the explicit publish-outcome contract on Core 0 command responses (a permanent serialization failure is reported `False` instead of being indistinguishable from success, with its cause recorded so the servicing path answers with the matching bounded substitute instead of retrying the same unsendable bytes at the head of the FIFO, and a reboot is held rather than resetting without its published acknowledgement), the outbound serializer's allocation-light validation fast path (a valid message passes `is_json_safe()` without the path-producing validator; the fast pass and the detailed validator accept and reject exactly the same values; detailed errors retain nested path diagnostics), the MemoryError propagation contract on the Core 0/Core 1 message paths, and the configuration manager contract (`validate_config` shared by startup and write-config with structured `ConfigError` codes, the complete per-key change-policy table with a test that no classifiable key can lack a policy, and a test pinning the classified-HOT set to exactly the two live apply sets on both cores — HOT_RELOADED promises a live effect, so a key no steady-state runtime consumes (`network_probe_timeout_sec`, startup verification only) is REBOOT_REQUIRED, the ACTIVE snapshot lifecycle — serialized before the first reboot-required promotion, unchanged by later writes, discarded by a successful HOT commit, with `reboot_required` derived solely from the snapshot's presence —, UNCHANGED writes performing no filesystem modification, the boot-recovery priority order over `config.json`/`config.json.old`/`config.json.tmp` with the steady state exactly one valid `config.json`, the `.tmp` read-back re-validation before promotion, the recovery path-exists check mapping only an `ENOENT` stat to "absent" (any other `OSError` — a genuine flash/LittleFS storage failure — propagates with its error context instead of letting recovery select a different artifact on a false premise), HOT commit/rollback restoring the committed configuration and the pre-transaction reboot state, the cross-core HOT_RELOADED apply as one single-flight transaction on the dedicated config-update request/result lane (Core 0 live config update with a `mqtt_command_poll_ms` poll-stamp re-anchor, Core 1 re-anchoring the read/health schedulers from the reload instant and refreshing its own config copy for the health threshold, commit/rollback resolved from Core 1's acknowledgement in the Core 0 run loop, a pending transaction refusing a second write with `config_update_in_progress`, a mixed reboot+hot candidate applying nothing on either core, and the Core 1 apply-failure rollback restoring prior values and reporting a bounded failure), and the `read-config`/`write-config` response contracts — the write-config payload exactly `{"config": <complete candidate configuration>}` with unknown payload keys named together and unknown configuration fields (top-level or qualified `devices[<id>].<key>`) returned as a sorted array, a schema-version mismatch answered with `expected`/`received`, UNCHANGED writes logging the warning and preserving a pending reboot, the `configuration_changed`/`classification`/`reboot_required`/`changes` data shape, no automatic reboot, the response staying on the active source/MQTT identity after a reboot-only change, and the sorted change summary with compact whole-device `ADDED`/`REMOVED`/`MODIFIED` entries)
- Hardware testing requires an actual Pico device

## Hardware Notes

- **Pico W**: Wi-Fi only, 256KB RAM
- **Pico 2 W**: Wi-Fi, 264KB RAM, faster CPU
- Onboard LED is Core 0-only via `LEDManager`
- Never call `machine.reset()` from Core 1

## Version History

When modifying the project version or making release-worthy changes, update CHANGELOG.md. Do not maintain version history in CLAUDE.md.
