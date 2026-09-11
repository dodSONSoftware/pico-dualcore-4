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
    "cpu_temperature_c": 42.3,
    "network_stack_ready": true,
    "wifi_connected": true,
    "wifi_rssi_dbm": -45,
    "mqtt_connected": true,
    "core_1_active": true,
    "core_1_activity_age_ms": 42,
    "free_heap_bytes": 95728,
    "preferred_free_heap_bytes": 65536,
    "minimum_free_heap_bytes": 49152,
    "heap_headroom_bytes": 46576,
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
- `cpu_temperature_c`: On-chip die temperature in °C (0.1 °C resolution) read from the ADC core-temp channel via `SystemInformation.get_cpu_temperature()` with the datasheet conversion (Vbe = 0.706 V at 27 °C, slope −1.721 mV/°C — the RP2040 and RP2350 datasheets state the same calibration, so one formula serves both boards); the conversion is VREF-sensitive (≈4 °C per 1 % VREF) and the sensor varies device-to-device, so the field is a trend indicator at roughly ±5 °C, not a calibrated absolute; `null` when the channel is unavailable (never a degradation trigger)

### Network Fields

- `network_stack_ready`: True once Core 0 has verified the complete startup contract (from the network snapshot)
- `wifi_connected`: Wi-Fi link is up (from the network snapshot)
- `wifi_rssi_dbm`: Current Wi-Fi RSSI from Core 0 network snapshot (may be null)
- `mqtt_connected`: MQTT broker connection is up (from the network snapshot)

### Memory Fields

- `free_heap_bytes`: Current `gc.mem_free()` value
- `preferred_free_heap_bytes`: Board-specific preferred reserve (64 KiB Pico W, 144 KiB Pico 2 W) — where memory-pressure handling (GC, then reclamation of low-retention entries) begins; never a rejection wall by itself
- `minimum_free_heap_bytes`: Board-specific hard survival floor that admission must protect (48 KiB Pico W, 128 KiB Pico 2 W)
- `heap_headroom_bytes`: free_heap - minimum_free_heap (negative when below the hard floor)

### Core Activity Fields

- `core_1_active`: True if `core_1_activity_age_ms <= threshold`
- `core_1_activity_age_ms`: Monotonic elapsed time since last activity report

### Device Fields

- `devices_configured`: From `DeviceManager.get_status_snapshot()`
- `devices_active`: From `DeviceManager.get_status_snapshot()`
- `device_failures`: devices_configured - devices_active

### Queue Fields

The outbound queue is heap-governed **and** bounded by a deterministic entry-count ceiling (`outbound_queue_max_messages`, see the lane below), so these are observability metrics: the depth and high-water-mark are utilization against that count ceiling (the byte metrics have no count limit — the heap policy remains the byte/memory guard):

- `outbound_queue_depth`: Queued + in-flight entries (current) — always `≤ outbound_queue_max_messages`
- `outbound_queued_bytes`: Retained payload bytes (the queued FIFO plus the in-flight entry)
- `outbound_queue_high_watermark`: Peak queue depth since boot — always `≤ outbound_queue_max_messages`
- `outbound_queue_high_watermark_bytes`: Peak retained payload bytes since boot
- `outbound_evicted`: Entries evicted under memory pressure (all kinds), including count-ceiling relief
- `telemetry_evicted`: Evicted entries of the telemetry kind
- `outbound_rejected`: Admissions rejected because the hard free-heap floor could not be restored or no eligible entry was available to relieve the count ceiling

### UTC Fields

- `utc_valid`: True if UTC snapshot is available
- `utc_sync_age_sec`: Seconds since last UTC sync (integer, null if never synchronized)

### Degradation Reasons

- `network_stack_not_ready`: Core 0 network not fully initialized
- `wifi_not_connected`: Wi-Fi disconnected
- `mqtt_not_connected`: MQTT broker connection lost
- `core_1_inactive`: Core 1 activity exceeds threshold (3x read_loop_sec, min 60s)
- `low_free_heap`: free_heap < minimum_free_heap (below the hard floor)
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

- Raspberry Pi Pico W: `RPI_PICO_W with RP2040` → canonical type `pico_w`, preferred reserve 64 KiB, hard floor 48 KiB
- Raspberry Pi Pico 2 W: `RPI_PICO2_W with RP2350` → canonical type `pico_2_w`, preferred reserve 144 KiB, hard floor 128 KiB
- Unsupported hardware raises a clear `RuntimeError` during startup

The hardware module provides:
- Canonical hardware type identifiers (`HARDWARE_TYPE_PICO_W`, `HARDWARE_TYPE_PICO_2_W`, `HARDWARE_TYPE_UNKNOWN`)
- Board-specific free-heap thresholds — the preferred reserve (`PICO_W_PREFERRED_FREE_HEAP_BYTES`, `PICO_2_W_PREFERRED_FREE_HEAP_BYTES`) where memory-pressure handling begins, and the hard floor (`PICO_W_MIN_FREE_HEAP_BYTES`, `PICO_2_W_MIN_FREE_HEAP_BYTES`) that admission must protect
- `classify_machine()` — the single source of truth mapping a machine string to canonical type and both board heap thresholds. Both `detect_hardware()` (startup) and `SystemInformation.get_machine()` (telemetry/health) classify through it, so they can never disagree.
- Detection function `detect_hardware()` returning an immutable result dict, failing fast on unknown hardware

See `hardware.py` for implementation details.

## Ownership invariants

1. Core 0 exclusively owns the complete network stack: `network.WLAN`, CYW43 networking, IP/DNS, sockets, MQTT, QoS 1, subscriptions, reconnect, UTC acquisition, reboot, and QoS 1 network probes for startup verification.
2. Core 1 exclusively owns the complete sensor/device stack: device drivers, I2C/SPI/UART/ADC, initialization, reads, device state, software sensors, and telemetry construction.
3. Only plain-data objects cross the core boundary.
4. Once an object is transferred into an inter-core lane it becomes immutable. Neither producer nor consumer may mutate it.
5. Live subsystem objects never cross cores.

## Security model

The firmware is designed for a **trusted private network**. This is a deliberate trust boundary, not an omission:

- **Trusted private network.** The MQTT broker and the firmware devices are assumed to operate on a private network under the operator's control. The firmware does not itself authenticate or authorize MQTT peers.
- **Command authorization is network-level.** The command protocol (`reboot`, `read-config`, `write-config`, `get-details`) is authorized by *network access control* — by who can reach the broker and publish on its subscribed topics — not by any credential, token, or per-command policy inside the firmware. A peer that can publish a well-formed, in-boundary command frame to a subscribed topic can drive the device.
- **Operational requirement.** The broker must not be exposed to untrusted or public networks. Exposing it removes the boundary this design relies on, and every command — including `write-config` (arbitrary configuration) and `reboot` — becomes reachable by that network's peers.

This is why the firmware defends the **integrity and boundedness** of inbound frames (the MQTT wire/QoS gates, the global schema gate, the staged command-validation order, and the bounded payloads) rather than authenticating their source. The absence of per-command authorization is the threat model, not a gap.

## Inter-core lanes

### 1. `outbound_queue`

Core 1 -> Core 0. Contains only data intended for MQTT.

- FIFO, heap-governed, and bounded by an entry count: no byte budget — admission is decided against the board's two heap thresholds (see Memory safety below) **first**, the preferred reserve opening memory-pressure handling and the minimum being the hard survival floor no retained entry may cross, and **then** against the configured `outbound_queue_max_messages` count ceiling (1–256, required, `REBOOT_REQUIRED`). The heap policy remains the memory guard and is authoritative: the count ceiling can add a rejection or a retention-aware eviction but never admits what the heap policy would reject. It is a deterministic observability/stability bound, not a memory bound — the heap can never actually retain 256 × 16 KiB.
- Core 1 supplies only a message kind plus domain data; it does not know MQTT topics.
- Core 0 maps the kind to the authoritative MQTT topic, publishes with QoS 1, and owns the MQTT envelope (sequence, runtime_id, source, firmware_version, message_schema_version). Kinds: TELEMETRY → `mqtt_topic_telemetry`, COMMAND_RESPONSE → `mqtt_topic_command_response`, HEALTH → `mqtt_topic_health`, LOG → `mqtt_topic_log`.
- The startup log and connection logs travel as KIND_LOG entries; no hardcoded topics cross into Core 1.
- The sender owns every message field, including `uptime_ms` and `timestamp` (the latter null when UTC is unsynchronized). A queued message must NOT carry any envelope key at the top level, or the wire document would repeat a member name.
- Core 0 injects the envelope at publish time by splicing its five members into the stored serialized object before its closing brace: the payload bytes are never decoded, parsed, or re-serialized on the publish path, so publishing allocates only the small envelope fragment plus the assembled frame.
- **The splice is size-bounded before it allocates.** The body was admitted at or under `MAX_OUTBOUND_MESSAGE_BYTES`, but the spliced envelope is added on top of it, so `core0._publish_entry` checks the *final* wire length (`body + comma + fragment + closing brace`) against `MAX_OUTBOUND_MESSAGE_BYTES` before `bytes.join()` runs and raises `OutboundMessageTooLargeError` if it would exceed it. That failure is permanent for the entry (its bytes are fixed): the run loop discards it (queue counter `oversized_discarded`) and answers a command response with the bounded `response_too_large` substitute (Core 0's `_answer_discarded_command_response`), while telemetry/health/log entries and connection logs are dropped with a warning — never held in flight and never retried.
- The MQTT client waits for the matching PUBACK before the next publish proceeds, naturally enforcing one application QoS 1 publish in flight. The wait is bounded by `mqtt_broker_response_timeout_sec`, so a blackholed link fails the publish (the entry stays in flight) instead of blocking the run loop.
- Retention priority is explicit: lower numeric values are more important.
- Priority classes are: CRITICAL 10, ERROR 20, WARN 30, TELEMETRY 40, INFO 50, HEALTH 70.
- Admission is three-banded against the two thresholds: at or above the preferred reserve the entry is admitted on the fast path (no `gc.collect()`, no eviction); between the preferred reserve and the hard floor (memory pressure) the queue runs `gc.collect()` first — if that restores the preferred reserve the admission proceeds as normal, otherwise it reclaims the oldest entry of the least-important queued class the incoming message may displace (an increased willingness to discard low-retention traffic, since the preferred reserve is not a rejection wall) and still admits; below the hard floor after GC (hard pressure), or when the append's own allocations (the entry dict and list growth) cross the floor when re-measured after the append, it reclaims eligible entries one at a time, reclaiming and rechecking after each, until the entry is retained with the hard floor intact, or no eligible entry remains. If the incoming message is less important than everything queued, or the queue is empty, it is rejected as transient (the producer retains and retries) without dropping a valid entry.
- **The count ceiling is evaluated at the append sites, subordinate to the heap.** Each of the three heap admission bands (normal / soft / hard) appends once the heap floor it is responsible for is satisfied; at that moment the entry count — queued + in-flight, the same depth as the high-water-mark metric — is checked against `outbound_queue_max_messages`. If there is room, the entry is appended (heap already cleared). If the count is at its limit, the queue runs the **same** retention-aware `_evict_one_eligible_locked()` the heap path uses — no second eviction policy, no FIFO/pop — to free one slot, runs `gc.collect()`, and appends; if nothing is eligible to evict it is rejected as transient (the producer retains and retries), counted exactly once through the hard loop's single rejection point. Because heap-first is mandatory, a count-relief pass can never reject a message the heap policy would have admitted, and a heap rejection while below the count limit is never overridden. A full-of-CRITICAL queue rejects (the CRITICAL floor is absolute) rather than overflowing or reserving slots.
- **CRITICAL is a non-evictable retention floor.** An admitted CRITICAL entry (a command response) has no remaining owner once Core 1 has handed it off, so evicting it to make room for another CRITICAL entry would permanently lose the response — and with the command-ID debounce suppressing the retry, the client would lose the answer to a command that was processed. A CRITICAL admission that cannot fit without evicting an existing CRITICAL entry is therefore rejected (the producer retains it and retries), while a CRITICAL entry still displaces lower-priority ones, and equal-priority replacement is unchanged for the replaceable lower priorities.
- The current Core 1 command response uses CRITICAL 10; telemetry uses TELEMETRY 40; health messages use HEALTH 70; the startup log uses INFO 50.
- An in-flight QoS 1 entry is retained until its PUBACK (its payload bytes stay counted in the retained-bytes metric) and is never an eviction candidate.
- A failed publish never discards the in-flight entry: it stays in flight and `take()` returns it again, so Core 0 retries until the broker PUBACKs (QoS 1 at-least-once delivery).
- **Sequence identity across an ambiguous failure.** QoS 1 has an ambiguous failure mode: the PUBLISH frame can reach the broker while the PUBACK is lost, so a failed publish attempt may still have been delivered. The `sequence` envelope member is therefore claimed *before* the first transmission attempt and stamped on the logical object — the queue entry, or Core 0's persistent response/reboot dict for its own retryable messages — and is never rolled back or reused by a different message. A retry of the *same* logical message reuses its stamped number (both copies identify one message — legitimate QoS 1 duplicate delivery), while a *different* message (e.g. a `mqtt_connection_established` log published after a reconnect) always receives a fresh number. This makes `(runtime_id, sequence)` a safe unique event identity and lets a receiver recognize a retry of the same logical message. Claiming happens in `core0._claim_wire_sequence`, invoked from `_publish_entry` (queue/connection-log path) and from the response/reboot retry paths.
- **Publish outcome is explicit; a failed response is never silently discarded.** `core0._publish_core0_command_response` returns `True` when the response was published (PUBACK received) and `False` on a permanent (non-`MemoryError`) serialization failure; a `MemoryError` propagates to the final recovery boundary. Callers act on the distinction: a queued Core 0 command response that fails to serialize is NOT discarded — the command was accepted and its acknowledgement is owed — so it stays in the pending queue and is retried on a later pass, exactly as a failed publish (which raises) leaves it pending. A reboot whose response fails to serialize is held: no `machine.reset()` without a published success acknowledgement, and the reboot is retried on a later pass. Before this, both callers treated a failed serialization identically to a successful publish — the response was discarded and a reboot could complete without ever answering the command.

#### Outbound publish pacing

`mqtt_outbound_publish_delay_ms` (Core 0-only) sets a minimum gap between consecutive outbound **application** PUBLISHes. Core 0 remains the exclusive MQTT owner; the policy lives in `core0.py` as two small pieces of state: the configured interval and one timestamp — the completion time of the most recent successful outbound QoS 1 publish.

- The interval begins when the previous QoS 1 publish **completes** (matching PUBACK received), not when it starts — broker and PUBACK latency are outside the configured delay. A failed publish completes nothing and records no timestamp.
- The first publish after startup, reconnect, or an idle period longer than the interval begins immediately. There is no delay added to the first message, and a value of `0` disables pacing entirely.
- All outbound application PUBLISHes share the one gate and the one timestamp: queued telemetry/health/log/command responses, connection logs, Core 0 command responses, UTC info requests, and the startup network probes. MQTT protocol-control traffic (CONNECT, SUBSCRIBE, PINGREQ, DISCONNECT) is never paced and never starts or resets the interval.
- During the normal runtime the gate is **state, not a sleep**: while it is closed, `Core0.run()` keeps looping — Core 1 heartbeat watchdog, MQTT command polling, keepalive, network recovery, and the 10 ms step all proceed — and simply does not begin another PUBLISH (nor dequeue the next in-flight entry) until it reopens. At most one application PUBLISH therefore begins per pacing interval, and a backlogged queue drained after a reconnect flows progressively instead of as a broker-speed burst. Only the sequential startup contract may wait for a slot (bounded 10 ms slices), because a specific publish must complete before startup continues.
- Pacing constrains *when* a PUBLISH may begin; it changes nothing else: queue FIFO order, admission, heap-reserve bounds, eviction, in-flight retention, QoS 1 retry identity, and run-loop message priority (reboot response → connection logs → command polling → Core 0 responses → outbound queue → UTC work) are all unchanged. A pending reboot holds (without blocking and without resetting) until the link is up and its response publish slot opens, then proceeds through the existing 5-second grace and `machine.reset()`.
- Tick handling uses `time.ticks_ms()` / `time.ticks_diff()`, so the gate is correct across the 30-bit tick wrap.

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

The validation step is two-phase: an allocation-light `is_json_safe()` pass enforces the rules without building per-node diagnostic path strings (the common valid-message case pays no transient allocations), and only on failure does the path-producing validator re-walk the message to raise the precise error naming the offending path. The two passes must accept and reject exactly the same values — `tests/test_message_serializer.py` pins that agreement.

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

Admission failures are distinguished by outcome, and both admission paths (`put()` and `put_with_kind()`) share the same contract:

- **Transient — returns `False`**: heap-pressure rejection (the hard free-heap floor could not be restored, or the incoming entry is less important than everything queued). A later retry may succeed.
- **Permanent — raises `ValueError`**: the message itself can never be admitted — an unsupported value, a non-string key, a non-finite float, a serialization failure, or a serialized size beyond `MAX_OUTBOUND_MESSAGE_BYTES`. Retrying the same message cannot succeed, so these are raised rather than returned as `False`. Queue state is untouched in both cases.
- **Serializer `MemoryError` — propagates after its own recovery**: a `MemoryError` from the serializer first runs `gc.collect()` and retries, then (only if it persists) reclaims one eligible entry per failure, `gc.collect()` between retries, until serialization succeeds or nothing eligible remains — at which point the `MemoryError` propagates, not as a transient `False` (the queue's state is untouched). This is the Pico W's documented fragmentation regime: the heap holds enough in total but no contiguous run for the serialized form. The Core 1 command path answers it per-command (below); every other producer lets it reach the recovery boundary.

Callers act on the distinction: telemetry and health discard a permanently rejected message (current-state data is not retryable), the startup log treats any admission failure as fatal, and the Core 1 command path answers a permanently rejected response with a small error response for the same command, then moves on — so an individual command response can never permanently block the command channel behind it. The substitute's code states the actual cause, so a firmware defect is never misreported to the command sender as a size problem: an oversized response (`OutboundQueue` raises `OutboundMessageTooLargeError`, a `ValueError` subclass, for the ceiling case on both admission paths) gets `error.code: "response_too_large"`, while a validation or serialization failure (plain `ValueError`) gets `error.code: "response_invalid"`. A transiently rejected response stays pending and is retried on later passes. The same permanent-failure distinction holds at the wire boundary: an entry whose spliced wire length exceeds the ceiling (the body was admitted at or under it, the envelope pushed it over) is a size failure of the same cause class, and is handled the same way — command responses answered with `response_too_large`, everything else discarded and counted.

The persistent serializer `MemoryError` is answered on the same principle — never escaped, never a silent loss: a `get-details` full snapshot is the one response large enough to hit that wall on a memory-tight board (the 0.4.91 Pico W: 2,360 bytes serialized with ~57 KiB free — before this, the `MemoryError` escaped the admission, killed Core 1's worker thread, and Core 0's heartbeat watchdog reset the board 30 s later, the command unanswered). Core 1 now answers it one section smaller at a time, dropping the device sections in order — `device_status` first (the only section carrying unbounded `failure_reason` strings), then `devices` — each retry following the queue's own `gc.collect()`, so the pool is coalesced when the strictly smaller form is retried, until one is admitted. Every bounded form stays a `success: true` response and names what it no longer carries in an `omitted_sections` marker (a missing section is a detectable gap), so the consumer always knows the snapshot is partial. The transient/permanent distinction is preserved at every tier (a transiently rejected bounded form stays pending and is retried as-is; a permanent rejection of it takes the usual substitute), and any other response — or a `get-details` whose device sections are already gone — takes the small `response_invalid` error substitute. A `MemoryError` on the last bounded form's or the substitute's own admission propagates to the recovery boundary (a diagnostic must never loop).

#### Memory safety

Three rules keep the outbound path safe on MCU-scale heap (Pico W: 256 KiB SRAM). The per-message size check happens after `json.dumps()` + UTF-8 `encode`, so at peak allocation the object graph, the serialized `str`, and the encoded `bytes` are all resident at once — a large payload can therefore exhaust heap before a limit is even reached:

- **Per-message ceiling** — `message_serializer.MAX_OUTBOUND_MESSAGE_BYTES = 16 KiB`. Bounds a single message's transient peak (graph + str + bytes ≈ 3x the payload ≈ 48 KiB) and keeps the largest legitimate messages (the startup event log — the only payload that grows with device count) comfortably under the limit with margin. Config-derived growth is bounded at the config boundary — the device count (`config.MAX_DEVICES`) and the string fields, all measured in **UTF-8 bytes** because the ceiling is a wire bound: `source` and device `id`/`name` at 64 bytes (`MAX_SOURCE_LENGTH`, `device_factory.MAX_DEVICE_*_LENGTH`), `mqtt_broker_ip_address` at 253 bytes (`MAX_MQTT_BROKER_ADDRESS_BYTES`, the DNS hostname maximum — `socket.connect()` resolves hostnames), and the topics at 122 ASCII bytes — and a serialized-size invariant test pins that the worst-case valid configuration's read-config response, envelope splice included, stays at or under the ceiling, so a future field or driver string that breaks it fails the host suite instead of wedging read-config on a device — so the event log is the one that can legitimately exceed the ceiling only through non-config content (its per-device lists and driver failure reasons); when it does, Core 1 answers with the bounded fallback summary, instead of failing startup (see Startup log). The queue enforces it on both admission paths: `put()` via the serialization step, and `put_with_kind()` via a direct byte-length check on the pre-serialized payload, so a caller bypassing `serialize_and_validate_message()` cannot admit a larger entry. The ceiling is enforced a second time at the wire boundary, against the final spliced length (`core0._publish_entry`), so the 16 KiB bound holds for the actual MQTT payload even though the envelope is added after admission.
- **Two heap thresholds** — `hardware.py` splits each board's reserve into a preferred pressure band and a hard survival floor (Pico W: 64 KiB preferred / 48 KiB floor; Pico 2 W: 144 KiB preferred / 128 KiB floor), the single source of truth for queue memory safety. The preferred reserve marks where memory-pressure handling begins — `gc.collect()` first, then an increased willingness to reclaim low-retention entries — and is never a rejection wall by itself; the hard floor is what admission must protect: an entry may be retained only while `gc.mem_free()` is at or above it, re-measured after the append's own allocations, so the queue cannot exhaust heap on its own during an MQTT outage, regardless of how many entries it holds.
- **Serialization with `MemoryError` recovery** — the `put()` path (the only admission path that serializes) works on the *actual* message's working set: it attempts `json.dumps()` + `encode()` directly, with no fixed worst-case threshold standing in for it, so a small telemetry message on a Pico W at ~85 KiB free heap (above the 64 KiB preferred reserve, 48 KiB hard floor) serializes and is admitted on the fast path. A `MemoryError` from the serializer is a recovery trigger, not a rejection: first `gc.collect()` and retry (fragmentation or collectable garbage can resolve it with no data loss); only if serialization still fails does the queue reclaim memory — one eviction per attempt, the oldest entry in the least-important class this message may displace under the *same* eligibility rule as admission pressure (a lower-priority incoming may not evict a more important entry; CRITICAL is never displaced), each re-claimed with `gc.collect()` — until serialization succeeds or no eligible entry remains, in which case the `MemoryError` propagates to the firmware recovery boundary (it is not converted into a transient rejection, and nothing is swallowed). The loop is bounded by the number of eligible retained entries, so it terminates without a retry count. Non-`MemoryError` serializer failures (validation, size, serialization) propagate immediately, as before. `put_with_kind()` takes already-final bytes, so it never serializes and never enters recovery; the post-admission hard-floor check above still applies to both.

Admission is heap-governed under one shared heap-admission lock (the heap is global to both cores, and both queues share the lock). The `put()` path first serializes the actual message — recovering a `MemoryError` with `gc.collect()`, then one eligible queued entry per persistent failure under the retention policy, with the queue lock held only for the eviction step (never across the serializer or `gc.collect()`) — and only then admits; `put_with_kind()` skips that step (its bytes are already final). Both then apply the admission decision against the two thresholds: **NORMAL** (free heap at or above the preferred reserve) — admit with no garbage collection and no pressure eviction; **memory pressure** (at or above the hard floor but below the preferred reserve) — `gc.collect()` first, and if that restores the preferred reserve the admission is normal, otherwise one eligible entry of the least-important class this message may displace is reclaimed and the entry is still admitted (the preferred reserve opens pressure handling but is not a rejection wall); **hard pressure** (below the hard floor after GC, or an append whose own allocations cross it) — evict eligible entries one at a time, reclaim and recheck after each, until the entry is retained with the floor intact or nothing eligible remains — then admit, or reject as transient for the producer to retain and retry. The hard floor is a **post-admission invariant**, not a pre-admission threshold: it is re-measured after the append itself (which allocates the entry and any list growth) on both admission paths, and an admission whose own allocations cross it is undone (entry removed, heap reclaimed) and fed into the same priority-displacement decision — an eligible less-important queued entry may be evicted for it, and only when none remains is it rejected as transient — so no retained entry can ever sit below the floor. A valid queued entry is never dropped to admit a less important one, and the in-flight entry is never evicted. `OutboundQueue.status()` reports `queued_bytes`, the depth/bytes high watermarks, and the eviction/rejection counters for observability. On top of this heap policy the outbound queue is also bounded by the deterministic `outbound_queue_max_messages` entry-count ceiling (see the lane above) — a subordinate observability/stability bound the heap is evaluated first against, never a relaxation of the memory guard.

### 2. `event_queue`

Core 0 -> Core 1. Contains private discrete commands/events. `get-details` is
owned by Core 1 because the authoritative `SystemInformation` instance and
device-manager state live there: Core 0 validates the command at the protocol
boundary and dispatches a validated bounded event, and Core 1 executes it and
returns a current snapshot containing every entry in
`SYSTEM_INFORMATION_SECTIONS` as `command_response.payload.data` — unrestricted
by the configured scheduled `system-information` device `include` list. The
command requires an empty payload (any key is an unknown field, named sorted,
at the Core 0 boundary — the same contract as `reboot`).

- FIFO and heap-governed: the same hard free-heap floor and shared heap-admission lock as the outbound queue, but with NO eviction — an admitted event is a discrete control operation and is never displaced by a newer one. Under memory pressure the new event is rejected and the caller reports the `intercore_event_queue_memory_pressure` failure.
- Every admitted event matters.
- Entries are never automatically published to MQTT.
- Reboot never enters this lane; Core 0 owns reboot completely.
- Only supported Core 1-owned commands (currently `get-details`) are dispatched. Core 1 no longer acts as the generic fallback for arbitrary command names: a command outside the supported registry is answered by Core 0 and never crosses to Core 1 (see Supported-command registry).

#### Inbound packet size limit

`wait_msg()` (`mqtt_client.py`) bounds an inbound packet's remaining length at `MAX_INBOUND_PACKET_BYTES` (20 KiB) **before the payload is allocated** — a frame larger than the bound fails the connection (`_abort_corrupt_inbound`, recovery reconnects) instead of letting `sock.read(sz)` request an allocation that could exhaust Pico RAM. The remaining-length field itself is bounded to MQTT's maximum four varint bytes, so a corrupt stream cannot declare a larger length at all.

The acknowledgement frames are shape-checked the same way, one by one: CONNACK must be opcode 0x20 with its fixed 2-byte body, SUBACK must declare exactly the 3-byte body of this client's single-topic subscription (2-byte packet id + 1 return code) — read **before** the body, so a declared length that is too long leaves no stray byte in the stream and one that is too short over-consumes none of the next packet's bytes — with the return code gated to the granted QoS codes 0x00–0x02 or 0x80 (failure; a reserved code is a protocol violation, not a grant), PUBACK must be exactly its 2-byte packet id, and PINGRESP must carry zero remaining length. A frame that does not match its declared shape drops the connection via `_abort_corrupt_inbound`: accepting it would desynchronize the stream, so the failure is a dropped connection (recovery reconnects), not a reported protocol error.

The 20 KiB value is derived from the protocol, not arbitrary: the largest spec-valid inbound frame is a `write-config` command carrying the worst-case valid configuration (every byte-bounded identity at 4-byte code points, which serialize 3× in the escaped form, the **character**-bounded 128-character `command_id`/`target`, the 253-byte broker address, all 16 devices, all lists at their bounds) — 16,865 bytes in the conservative wire form (ASCII-escaped, default separators). The serialized-size invariant test in `tests/test_config.py` pins both halves of the config boundary: the worst-case read-config **response** (envelope splice included) at or under the 16 KiB outbound ceiling, and the worst-case write-config **command** under the 20 KiB inbound ceiling — a ceiling set below the largest valid command (16,384 once was) would drop a spec-valid command at the wire layer, never letting validation reject or accept it.

The frame ceiling bounds the socket read, not the parse peak: `json.loads()` holds the decoded string and the object graph (≈ 3× the frame) concurrently, so the parse transient is a bounded multiple of the ceiling, not of the free heap. The decoded string is released as soon as the parse succeeds (`core0._on_mqtt_message`) so the command handler's own allocations no longer pay for it. If the parse transient still cannot be met, the `MemoryError` is not swallowed or reclassified — it escapes to `main.py`'s controlled-reset boundary, the same backstop as any other unrecoverable Core 0 exception. One caveat: a QoS 1 inbound is PUBACKed only after the callback returns, so a parse `MemoryError` resets the board before the ACK and the broker redelivers the same message after reconnect — a loop a peer that keeps publishing near-ceiling valid JSON to the command topic can sustain. That exposure is bounded by the trusted-network model (a misbehaving peer on the private network, not an untrusted one), and the ceiling is the lever: every frame the parser may see is at most 20 KiB.

#### Inbound QoS profile

Below the application gates, `wait_msg()` (`mqtt_client.py`) accepts inbound PUBLISH only at QoS 0 and QoS 1: QoS 2 is outside this client's protocol profile and QoS 3 is invalid for PUBLISH by the MQTT spec. Both are rejected from the opcode byte, **before the payload is read and before the application callback runs** — `_abort_corrupt_inbound` closes the socket and raises, and Core 0's recovery reconnects. The ordering is a security property, not a style choice: the callback is not passive — it feeds the command protocol — so a nonconforming peer must not be able to deliver a `write-config` / `reboot` / `get-details` frame that the application executes before the MQTT layer announces the rejection. QoS 1 inbound is acknowledged with a PUBACK; the firmware subscribes at QoS 1, so a conforming broker never sends QoS 2 in the first place.

#### Global inbound message-schema gate

Every decoded inbound MQTT object — not only commands — passes one global gate immediately after decoding and the dictionary check: `message_schema_version` must be exactly `MESSAGE_SCHEMA_VERSION` (currently 3). A missing, wrong-typed, older, or newer version is ignored for the whole message:

- no response is sent — the firmware does not attempt to interpret a wire protocol version it does not support (there is no `invalid_message_schema_version` answer);
- no command ID enters the debounce cache;
- no UTC/request state is altered;
- no message-type or topic-specific processing continues — inbound `info_response` is included.

#### Command validation order

A command is validated in one fixed staged order, and each failure point has one bounded outcome:

1. the global gate above, then the command topic, then `message_type == "command"`;
2. **target** — a non-empty string of at most 128 characters (`command_protocol.MAX_TARGET_LENGTH`) that matches this device: the configured `source` (case-insensitive), the current IP address (the same case-insensitive string comparison), or the `*` broadcast. Another device's command is ignored silently and consumes no cache entry;
3. **command_id** — a non-empty string of at most 128 characters (`command_protocol.MAX_COMMAND_ID_LENGTH`). A missing / non-string / empty / over-long ID is dropped silently — a standard response requires a bounded `command_id` — and is never cached;
4. **debounce** — a `command_id` already claimed in this runtime is ignored silently; otherwise the ID is claimed **before** deeper validation, so a malformed duplicate cannot generate a second validation response;
5. **envelope** — any top-level key outside the six v3 command fields (`command_protocol.COMMAND_ENVELOPE_KEYS`) is unknown: all of them are named in one **sorted** `unknown_fields` array (`error.code: "unknown_fields"`). A `command` that is not a non-empty string is `invalid_command` (never echoed); a missing `payload` is `invalid_payload`;
6. **command name bound** — longer than 32 characters (`command_protocol.MAX_COMMAND_LENGTH`) is a bounded `invalid_command` answer; the over-long name is not echoed into the response (not even partially). A `payload` that is not an object is `invalid_payload`;
7. **registry** — a bounded name outside the supported set is `unsupported_command`, with the actual name preserved in the standard `payload.command` field (not duplicated inside `error`);
8. **broadcast policy** — `write-config` for `*` is ignored silently (no configuration validation, no filesystem operation, no response); the claimed ID remains cached;
9. **dispatch** — the command's own contract: `reboot` / `get-details` with the exactly-`{}` payload, `read-config` with `{}`, and `write-config` with the exactly-`{"config": <complete candidate configuration>}` payload (see Configuration management).

#### Supported-command registry

Core 0 owns the command protocol boundary and the set of commands this device answers. The registry lives in `command_protocol.py` (the constants and pure validation helpers both cores share): `CORE0_OWNED_COMMANDS` / `CORE1_OWNED_COMMANDS` / `SUPPORTED_COMMANDS`.

| Command | Owner | Broadcast `*` |
|---|---|---:|
| `reboot` | Core 0 (executed on this core; `machine.reset()` is Core 0's) | yes |
| `get-details` | Core 1 (dispatched as a validated bounded event) | yes |
| `read-config` | Core 0 / configuration manager (executed on Core 0) | yes |
| `write-config` | Core 0 / configuration manager (executed on Core 0) | **no** |

- **Only `get-details` crosses.** Core 1 no longer acts as the generic fallback for arbitrary command names: a name outside the registry is answered on Core 0 and never crosses to Core 1. The configuration hot-reload handshake is internal control traffic, not an external command.
- **Dispatched event is validated and bounded.** Core 0 sends Core 1 only `{command_id, command, payload, targeted}` where `payload` is the validated empty `{}` — the raw (possibly non-empty) request payload never crosses. Core 1 trusts the payload is `{}` and no longer re-checks it.
- **Shared empty-payload contract.** Both `reboot` and `get-details` require an exactly-`{}` payload. Any key is unknown for the command, so a non-empty object is answered with `error.code: "unknown_fields"` carrying every offending key in a **sorted** `unknown_fields` array (the missing / non-object cases are the common contract and still get a bounded `invalid_payload`). A `get-details` with a non-empty payload no longer reaches Core 1 — Core 0 answers it before dispatch.
- **String bounds are enforced at the protocol boundary** (independent of, and additional to, the overall per-message MQTT size ceiling): `command_id` ≤ 128 characters, `command` ≤ 32, `target` ≤ 128, all non-empty when required. A command response carries the identifying `command` field only while it is bounded and valid — the over-long-name error is bounded and never reproduces the string, so a single command can never build an oversized error substitute and stall the channel.
- **Authorization is by design, not a gap.** Nothing in this registry authenticates the sender: which commands a peer can run is bounded by who can reach the broker and its topics. See [Security model](#security-model) — a trusted private network, network-level access control, and a broker that must not be exposed to untrusted or public networks.

#### Command-ID debounce cache

Core 0 owns duplicate-command debouncing at command ingress — after the global gate, topic/`message_type`, target, and bounded-`command_id` checks, and before envelope, registry, payload, reboot, and event-admission validation — so it covers Core 0 commands (reboot) and Core 1 commands (get-details) alike. Core 1 performs no debouncing.

The cache is a **short-lived debounce mechanism**, not durable idempotency or exactly-once execution: it stops repeated copies of the same message (broker redelivery, sender retry) from generating repeated validation responses and repeated executions.

- `command_id` is the debounce key: exact, case-sensitive, and opaque (never normalized). The command name, payload, and target casing are irrelevant to the check; a sender that wants a new logical command generates a new ID.
- The device retains the 16 most recently claimed command IDs (`core0._RECENT_COMMAND_ID_CAPACITY`) in a fixed-size FIFO in RAM. A command whose ID is still retained is silently ignored — no execution, no event admission, no response, and no change to a pending reboot. This is debouncing, not response replay: no prior response is retained or resent when a duplicate arrives.
- The first bounded use of an ID claims it **before deeper validation** — a malformed command still claims its ID, and repeated copies of the same malformed message cannot generate repeated validation responses. A duplicate receipt does not refresh its position (the cache holds the last claimed distinct IDs, not an LRU access order). An ID evicted by 16 newer distinct IDs may be processed again.
- Never claimed: messages failing the global version gate, traffic for another target, and missing / non-string / empty / over-long `command_id`s.
- The cache is RAM-only: reboot clears it, and it is never persisted to flash. A suppressed ID emits a DEBUG-only diagnostic, not a production warning.

#### Reboot command

`reboot` is the one command Core 0 executes itself: it never crosses to Core 1, and `machine.reset()` is Core 0's. It is validated on the same ingress path as every other command — the shared protocol checks (message type, schema version, case-insensitive target, command/command_id identity, command-ID debounce, and the payload-is-an-object requirement) run first — and then the command's own contract is applied in `core0._handle_reboot_command`.

- **Target** — the configured `source` (case-insensitive), the device's current IP address (case-insensitive string comparison), or `*`, like every command except `write-config`. A `*` command is handled non-targeted; a named/IP target is handled targeted.
- **Payload** — exactly `{}`. Any key is unknown for this command, so a non-empty object is answered with `error.code: "unknown_fields"` carrying the offending keys in a sorted `unknown_fields` array. A missing payload, or one that is not an object, is the common contract and is answered with a bounded `error.code: "invalid_payload"` (rejected before the command-specific check).
- **Single pending reboot** — one accepted request arms a pending reboot. A *distinct* valid request arriving while one is already pending is answered with `error.code: "reboot_already_pending"` and does not replace the pending one. A duplicate `command_id` never reaches this check — debounce suppresses it first.
- **Execution** — the success acknowledgement (`success: true`, `data: { "rebooting": true }`) is published first, under the existing pacing and retry semantics (a failed publish or a permanent serialization failure holds the reboot pending rather than resetting without an answer); only once the response is out does Core 0 wait the existing 5-second grace period and call `machine.reset()`.
- **Configuration** — there is no persistent `needs_reboot` flag or file. A reboot that follows a `reboot_required` configuration change needs no special branch: the reset clears the RAM-only debounce cache and any in-memory configuration snapshot, and startup reloads the authoritative persisted `config.json` with `reboot_required == false`.

#### get-details command

`get-details` is the one supported command Core 0 dispatches to Core 1 (rather than executing itself), because the authoritative `SystemInformation` instance and device-manager state live on Core 1. It is validated on the same ingress path as `reboot` — the shared protocol checks (message type, schema version, case-insensitive target, command/command_id identity, command-ID debounce, the length bounds, and the payload-is-an-object requirement) run first — and then the command's own contract is applied in `core0._handle_get_details_command`, which dispatches the validated bounded event.

- **Target** — the configured `source` (case-insensitive), the device's current IP address (case-insensitive string comparison), or `*`, like every command except `write-config`. A `*` command is dispatched non-targeted; a named/IP target is dispatched targeted.
- **Payload** — exactly `{}`, the shared empty-payload contract. Any key is unknown for this command, so a non-empty object is answered (on Core 0, before dispatch) with `error.code: "unknown_fields"` carrying the offending keys in a sorted `unknown_fields` array. No filtering/options (`include`, `sections`, `compact`) are supported — YAGNI. A missing payload, or one that is not an object, is the common contract and is answered with a bounded `error.code: "invalid_payload"`.
- **Dispatch** — a `{}` payload dispatches `{command_id, command: "get-details", payload: {}, targeted}` to the event queue. If admission fails (heap pressure) Core 0 answers with `error.code: "intercore_event_queue_memory_pressure"`.
- **Execution** — Core 1 executes the event and returns the full system-information snapshot (every `SYSTEM_INFORMATION_SECTIONS` entry) as `command_response.payload.data`, **unrestricted by the configured scheduled `system-information` device `include` list** (that list limits scheduled telemetry reads only). If Core 1 cannot provide the snapshot it reports the existing `error.code: "system_information_unavailable"`. The response-size handling (Core 1's `response_too_large` / `response_invalid` substitution) is preserved, and the command/ID length bounds guarantee the identifying fields themselves cannot make the substitute oversized. A persistent serializer `MemoryError` on the full snapshot (a memory-tight board's fragmented pool) is answered the same way the size case is — with the bounded get-details forms (device sections dropped in order, `omitted_sections` naming the gap) described in the admission section above — so the unrestricted snapshot never becomes a hard failure.

### 3. `state_mailboxes`

Core 0 -> Core 1 latest-value state.

- `network_snapshot`: Wi-Fi status, IP, RSSI, connection counts, `network_stack_ready` flag
- `utc_snapshot`: Current UTC time anchored to the shared boot base — the sync epoch (`utc_epoch_ms`), the sync's accumulated uptime (`sync_uptime_ms`), and the runtime-start epoch (`runtime_start_epoch_ms`). Each core advances it with its own accumulated uptime (`runtime_start_epoch_ms + current_uptime_ms`, and sync age as `current_uptime_ms - sync_uptime_ms`), never a raw `ticks_diff(now, sync_ticks)` — so it stays correct across the tick wrap even when the snapshot is older than half a tick period
- `hardware`: Detected hardware type, machine string, and the board's preferred/minimum heap thresholds
- `core_1_activity_ms`: Timestamp of last Core 1 activity report (in milliseconds) — written by Core 1, read by Core 1 for health reporting and by Core 0 as the input to the liveness watchdog

State is replaced, not accumulated. Core 1 keeps the latest immutable snapshot until Core 0 replaces it.

### 4. `config_update_lane`

Core 0 -> Core 1 -> Core 0 latest-value request/result, for the HOT_RELOADED configuration apply (see Configuration management). Internal runtime control, not a user command, and never mixed with the `event_queue`.

- `request`: `{generation, read_loop_sec?, health_interval_sec?}` — only the changed Core 1-owned HOT keys, plus a monotonic `generation` so a stale result can never be read as the current one.
- `result`: `{generation, success, code?}` — exactly one per request; `success: true`, or a bounded failure descriptor (e.g. `code: core1_apply_failed`).
- Latest-value mailboxes (state replaces, not queues), `allocate_lock`-guarded like `state_mailboxes` — no heap admission and no busy-spin on unlocked shared state.
- One transaction in flight at a time (Core 0 enforces it via the config manager's `transaction_active`), so the pending request and its held response are unambiguous.

## Core 0 baseline

Core 0 intentionally follows the original known-good behavior:

- every Wi-Fi attempt starts with `network.WLAN(network.WLAN.IF_STA)`;
- `active(True)` is called unconditionally;
- `PM_NONE` is applied when available;
- Wi-Fi uses the original ~20-second connection observation and configured retry delays, but ends an attempt immediately once the radio reports a terminal association state (wrong password, missing AP, known connect failure) instead of waiting out the full window; a still-connecting state keeps observing until the timeout fallback;
- MQTT uses the original small MQTT client;
- QoS 1 uses synchronous PUBLISH -> PUBACK;
- reboot is handled entirely by Core 0;
- reboot response is published, then the code waits 5 seconds + 1 second and calls `machine.reset()`;
- no pre-reset network shutdown is performed.

### Boot when the network never appears

The connect loops in `establish_network()` are intentionally unbounded at boot (the hardware watchdog is armed only after the startup contract passes — see Core 0 hardware watchdog — and would otherwise reset them into the same waits). If the configured SSID is absent, or the broker is unreachable, Core 0 retries forever: each Wi-Fi attempt observes the link for up to 20 s (200 × 100 ms) — or ends the attempt early the moment the radio reports a terminal association state (a wrong password, a missing AP, or a known connect failure, so a misconfigured SSID no longer burns the full window before each retry) — then sleeps the configured backoff delays between attempts, then the whole sequence restarts. The *values* of those delays are bounded at the config boundary for operational liveness — `config.MAX_RECONNECT_DELAY_SEC` (600 s) per delay and `config.MAX_RECONNECT_ATTEMPTS` (32) entries per sequence, inclusive, on both `wifi_reconnect_delays_sec` and `mqtt_reconnect_delays_sec` — so a misconfigured delay long enough to outlast any human diagnosis, or a sequence long enough to loop for days, is rejected at startup (and by `write-config`) instead of making recovery look permanently hung; the shipped 3/5/10/20/40 s sequences are nowhere near either bound. These waits are not monolithic: every one of them sleeps or observes in 100 ms slices with Core 0's servicing hook (`_service_wait`, the Core 1 heartbeat check) invoked between slices (see Core 1 liveness heartbeat). At boot the check is a no-op — Core 1 has not registered yet — but on the mid-run recovery path it is what keeps a dead Core 1 from hiding behind a network outage. Core 1 never starts, and the flashing connection LED (50 ms on / 50 ms off) is the only visible state of this boot loop. The startup verification steps behave the same way: a failed probe or a failed UTC pass drops the MQTT session, re-establishes the network, and retries the pass, so a transient failure after connection is recovered rather than fatal (see Deterministic startup sequence).

The exhaustion is observable, not silent: when an MQTT connection sequence uses up its attempts, `establish_network()` logs one warning naming the final cause the client retained — `Mqtt.last_connect_error`, the last expected transport/protocol error of the sequence (an ECONNREFUSED or ETIMEDOUT socket error, a connection reset, a CONNACK failure, or a SUBACK failure) — followed by the retry delay, falling back to the generic exhausted-sequence warning when no cause was retained. The Wi-Fi exhaustion warning has the same shape without a cause (the Wi-Fi layer retains no per-attempt error).

The Wi-Fi boundary follows the same exception taxonomy as the MQTT boundary: only a transport failure (`OSError`) is a link condition that fails the attempt or reads the state as unknown (the observation timeout then governs) and is eligible for retry; a `MemoryError` and a programming failure (a deterministic `AttributeError`/`TypeError`, an unexpected driver API incompatibility) escape `Wifi` to `main.py`'s controlled-reset boundary instead of being retried into the same deterministic fault. The one deliberate broad catch is the optional `PM_NONE` power-management probe, whose compatibility fallback has no portable exception type — a radio lacking it simply proceeds without it.

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
- `ping()` waits for PINGRESP with a bounded timeout (capped at 5 s — under the Core 0 hardware watchdog budget, so a stalled link fails on its own timeout first; see Core 0 hardware watchdog) so a dead link surfaces as a disconnect within one interval instead of blocking the run loop.
- A failed PINGREQ marks the connection disconnected; `_recover_network_if_needed` reconnects.

## MQTT wire client

Three invariants in `mqtt_client.py` keep the bounded-wait contract sound (every blocking broker wait must be deadline-limited, so a dead link surfaces as a failure instead of stalling the run loop):

- **One packet ID helper**: `next_packet_id()` advances the counter and wraps 65535 back to 1 (0 is reserved). QoS 1 publish, subscribe, and `Mqtt.get_next_packet_id()` (the network-probe path) all draw IDs from this single helper, so every message takes the next of 1, 2, ..., 65535, 1, 2, ... and the wrap is defined in exactly one place.
- **Socket mode is the caller's contract**: `wait_msg()` reads in whatever socket mode it finds and never changes it, and every caller runs it in blocking-with-timeout mode — so a read always returns a full length and a link that stalls after the first frame byte surfaces as a timeout instead of blocking forever. `publish`/`ping`/`subscribe` bound their waits with their own timeouts — and `publish` installs the QoS 1 timeout *before* the first PUBLISH frame byte is written, so a blackholed link whose send stops making progress fails the publish bounded (into recovery) instead of wedging Core 0 inside `sock.write()`, where the Core 1 heartbeat check could never run. `check_msg()` is the poller: it first decides *readiness* with a non-blocking `select` poll (one readable byte means a packet has started; nothing returns `None` immediately, leaving the socket mode untouched), and only then parses the packet — that parse runs under the finite `mqtt_broker_response_timeout_sec` rather than non-blocking, because a non-blocking multi-byte read can short-read a PUBLISH split across TCP reads and deliver a corrupt frame. `check_msg()` restores normal blocking mode on the way out; in MicroPython `setblocking(True)` is identical to `settimeout(None)`, so a poll that left the socket non-blocking, or a wait that forced blocking mid-read, would silently clear the next operation's timeout.
- **The handshake is bounded the same way**: `Mqtt.connect()` calls `MQTTClient.connect(timeout=mqtt_broker_response_timeout_sec)`, which installs the finite socket timeout that the CONNACK wait runs under. Before the handshake, the broker address is resolved with `getaddrinfo()` filtered to `AF_INET`/`SOCK_STREAM` — the profile the default socket constructs — so a configured hostname (the config contract allows up to 253 bytes, `config.MAX_MQTT_BROKER_ADDRESS_BYTES`) that resolves to multiple records cannot hand `connect()` an unusable first record (an IPv6 or datagram result) even when a usable one follows; a lookup with no matching record fails as `OSError`, the ordinary transport failure, into the reconnect backoff. `mqtt_broker_response_timeout_sec` is operationally bounded at the config boundary (`config.MAX_MQTT_BROKER_RESPONSE_TIMEOUT_SEC`, 5 s) so every wait it sizes — handshake, PUBACK, `check_msg` completion, UTC request deadline — stays under the Core 0 hardware watchdog budget (see Core 0 hardware watchdog). `subscribe()` does not install its own timeout — it relies on the one `connect()` left in place — so that same finite timeout carries across both SUBACK waits. A broker that accepts the TCP connection and then goes silent fails the attempt within the timeout (the reconnect backoff then retries) instead of wedging Core 0. Once both subscriptions succeed, `Mqtt.connect()` restores normal blocking mode; every later operation (PUBACK, PINGRESP) installs and restores its own timeout.

- **Restoring blocking mode is not best-effort**: the socket-mode contract above means a socket left out of blocking mode would silently defeat the next bounded wait, so a *failed* restoration of normal blocking mode is a connection failure, not a logged-and-ignored one. The handshake's restore failing fails the attempt (the reconnect backoff retries, the broken client closed) instead of marking the link connected; `ping()` and `publish()` treat a failed restore the same way — the operation fails into Core 0's recovery instead of reporting success — matching `check_msg()`, whose restoration always propagated.
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
- `BME280Device`
- initialization retry behavior
- read-failure/reinitialization behavior
- reinitialization failure logging: the first reinit failure for a device is warned once; repeats while the same device stays in pending-reinit are suppressed until a successful reinit clears the flag, so a stuck device warns once instead of every cycle (a later independent failure warns again)

The supported `device_type` registry (`device_factory._DEVICE_REGISTRY`) maps each type to its pure config validator and allowed config keys. Three types are registered:

- `system-information` — the software-only baseline test device; no hardware.
- `bme280` — the first hardware (I2C) device: a Bosch BME280 temperature/pressure/humidity sensor.
- `ltr390` — the second hardware (I2C) device: a Lite-On LTR-390UV-01 ambient-light/UV sensor.

### BME280 device

The `bme280` device is the first I2C sensor, split into a low-level protocol class and a `Device` adapter:

- `BME280` (`devices/bme280/bme280_device.py`) owns the register protocol and Bosch compensation: chip-ID verification (register `0xD0` must read `0x60`; a BMP280 shares the addresses, so an ACK alone is not enough), the software reset, the finite NVM-copy wait, the two-block calibration read (little-endian, signed 16/12/8-bit; `dig_H4`/`dig_H5` share register `0xE5`), the `ctrl_hum` → `config` → `ctrl_meas` write-latch order, the temperature/`t_fine`-then-pressure/humidity compensation, and the single coherent 8-byte burst read from `0xF7`. It returns `(temperature_c, pressure_pa, humidity_percent)` with a skipped channel as `None` (never a sentinel).
- `BME280Device` is the `Device` adapter that applies the application-layer policy the spec keeps out of the fundamental compensation: user offsets (`offsets.{temperature_c, humidity_percent, pressure_pascal}`) applied after Bosch compensation, and a derived `altitude_m` from the offset-adjusted pressure and `sea_level_pressure_pa`. Telemetry is `{temperature_c, pressure_pa, humidity_percent, altitude_m}` (pressure stays in Pa; the presentation layer converts to hPa).
- Both classes stay host-importable: `BME280` receives an injected I2C object and `BME280Device` imports no `machine` API, so the pure compensation and decode logic are unit-testable without hardware.

**I2C bus ownership.** Core 1 owns its I2C buses (see Ownership invariants); a driver never creates one. The bus is configured **per device** (`i2c_bus`, optional `i2c_sda_pin`/`i2c_scl_pin`/`i2c_freq_hz`). `core1_main` hands `DeviceManager` a bus factory that builds one `machine.I2C` per distinct `(bus, sda, scl, freq)` and caches it, so two devices on the same bus share one object; the factory imports `machine` lazily (inside the closure) so a config with no I2C device allocates no bus or pins. `create_device` resolves a device's bus through the factory (the factory is injected, so `device_factory`/`device_manager` stay host-importable). The bus is created at driver construction (peripheral + pins only); the sensor protocol — and therefore the failure that a missing/miswired sensor surfaces — runs in `initialize()`, which the retry/reinit machinery wraps, so an absent BME280 degrades to a failed device rather than a boot halt.

The device defaults to the spec's recommended forced-mode profile (temperature/pressure/humidity ×1, IIR filter off); each `read()` triggers one forced conversion and leaves the sensor asleep. Oversampling and the filter are per-device config knobs; the pure validator enforces that a fresh pressure/humidity compensation always has the temperature channel enabled (its `t_fine` feeds both) and that at least one channel is enabled.

### LTR390 device

The `ltr390` device is the second I2C sensor, split the same way: a low-level protocol class and a `Device` adapter.

- `LTR390` (`devices/ltr390/ltr390_device.py`) owns the register protocol and conversion: part identification (the `PART_ID` register's **part-number nibble must be 0xB**; the low nibble is the silicon revision, recorded but never gated on), the sequential ALS/UV sampling, the bounded data-ready waits, the coherent 3-byte burst reads (little-endian 20-bit, reserved high nibble masked), and the lux / UVI conversion. It returns `(als_raw, uv_raw, lux, uv_index)`.
- `LTR390Device` is the `Device` adapter that applies the application-layer policy the conversion keeps out: user offsets (`offsets.{lux, uv_index}`) applied after the conversion, to the converted channels only. Telemetry is `{lux, uv_index, als_raw, uv_raw}` — the raw counts are reported unmodified so ADC saturation (approaching 0xFFFFF) stays visible.
- Both classes stay host-importable: `LTR390` receives an injected I2C object and `LTR390Device` imports no `machine` API, so the pure decode and conversion logic are unit-testable without hardware.

**The ALS/UV sequential invariant.** The sensor has separate ALS and UV photodiodes, but `MAIN_CTRL` selects which channel is *actively converting* — it does not measure both simultaneously. A combined sample is therefore, per channel: mode switch (ALS active `0x02` / UVS active `0x0A`), wait for a **fresh** conversion, read. The freshness boundary is the data-ready bit (`MAIN_STATUS` bit 3): it is cleared by reading the channel's data register, and a mode change restarts the conversion — so the enable → wait-for-set → read sequence can never accept a stale value from the previously selected channel. Every wait is bounded (5 ms poll steps, timeout = 10 ms wake-up allowance + the selected resolution's ADC conversion time + 50 ms guard, ticks-safe, `OSError` on timeout).

**No software reset.** The `MAIN_CTRL` reset bit is deliberately never used — reset behavior has been observed to cause problematic I2C behavior on some hardware combinations. `init()` instead explicitly writes every register the driver depends on (standby → measurement rate/resolution → gain → interrupt-disable) and ends in standby; it also discards one read of each data register to clear any latched data-ready (power-on state, a previous user, a half-finished read). That makes the sequence re-runnable: the read-failure reinit path repeats it over the held bus regardless of what mode a previously failed read left the sensor in.

The device defaults to the spec's recommended environmental-telemetry profile (gain ×3, 18-bit, 200 ms rate, window factor 1.0); gain/resolution/rate are membership-validated (the reserved register codes are rejected), and the pure validator enforces the cross-field timing rule that `measurement_rate_ms` is not shorter than the selected resolution's ADC conversion time. `window_factor` is ≥ 1.0 (attenuation compensation only). Each `read()` ends in standby (~1 µA vs ~110 µA active), like the BME280 profile leaving the sensor asleep.

**Bus sharing.** The shipped `config.json` places the `ltr390` on the same bus/pins as the `bme280` (bus 0, GPIO 0/1): the lazy bus factory dedupes on `(bus, sda, scl, freq)`, so both sensors share one `machine.I2C` object (the addresses do not collide: 0x53 vs 0x76/0x77).

Core 1 runs in a worker thread that `main.py` spawns **first — before the core1 and core0 module imports and the network bring-up**: the thread's default ~4 KiB stack is one contiguous allocation from the GC pool, and on the memory-tight Pico W (256 KB) no such run survives the import sets plus the CYW43/lwIP buffers — a spawn that late raises `MemoryError` into `main.py`'s silent reset boundary and reboot-loops the board (the Pico 2 W's larger pool always has a run, which is why the late spawn only failed on the Pico W). Spawning before the core1 import gives the stack the cleanest pool state of the startup — the core1 chain's code objects are not yet interleaved into the pool, the state the Pico W validated in 0.4.87. The worker stamps Core 1's liveness at spawn and on every 100 ms poll, then waits for the network snapshot to report the full startup contract verified. Core 1 starts only after that contract has completed:

1. Wi-Fi connected
2. MQTT connected with subscriptions
3. QoS 1 network probe #1 with matching PUBACK received
4. Startup MQTT work drained
5. 5-second stabilization wait
6. QoS 1 network probe #2 with matching PUBACK received
7. UTC synchronization completed with valid snapshot
8. Initial network snapshot published with `network_stack_ready = True`

The `network_stack_ready` flag in the network snapshot indicates the complete startup contract has been verified — it is what releases the waiting worker, which then runs `core1_main()`. The core1 chain is **imported by `main.py` on the main thread, after the worker spawn and before the `core0` import and the network bring-up**: importing parses the module source, and that parse working buffer is a C-heap (non-GC) allocation, while `gc.mem_free()` reports only the GC heap — after Core 0's imports plus the CYW43/lwIP buffers the C heap has no run for even a ~2 KiB parse while tens of KiB still show as free, so a late import `MemoryErrors` where a late spawn cannot (the spawn's ~4 KiB stack comes from the GC pool, the import's parse buffer from the C heap). Importing while the C heap is clear keeps the worker's ready path to a `sys.modules` cache hit. The modules have no import-time side effects, and `core1_main()` still **runs** on the worker thread, so Core 1's device ownership is unchanged. A `gc.collect()` immediately before the `core0` import reclaims the startup garbage accumulated since the boot collect — the nulled configuration graph, the configuration-recovery parse residue, both import sets' compile temporaries, and the thread-spawn residue — so the import's code-object allocations see the coalesced free runs: the 0.4.90 Pico W `MemoryError`ed a 1336-byte import-machinery allocation with 88,176 bytes of heap free (the pool fragmented into no contiguous run), and the import sits above the recovery boundary, so that failure escaped into the silent reset and reboot-looped the board (fixed in 0.4.91). The flag is also cleared while a mid-run link outage is being recovered and restored after a successful re-establishment (see Network recovery); the worker waits only at startup, so a mid-run clearance never gates a running Core 1.

## Startup log

Core 1's first action, before the first telemetry, is a one-time `system_startup_completed` **startup event log**, queued under KIND_LOG at INFO (50) retention priority; Core 0 maps the kind to `mqtt_topic_log` at publish time. It is the only startup log — the detailed system information is available on demand via `get-details`, not streamed at boot.

### Event log (`system_startup_completed`) — the gate

The payload carries:

- Startup status per subsystem (hardware, Wi-Fi, MQTT, subscriptions, UTC, Core 0, Core 1). Subscription readiness is reported without topic names — topic ownership stays in Core 0.
- Startup duration as `duration_ms` — explicitly named so it is not confused with the envelope's device uptime (`uptime_ms`).
- Device counts (configured / ready / initialization-failed) plus per-device ready and failed lists (device, name; failed entries also carry the driver's `failure_reason` — the one diagnosis that matters at boot).

Admission is fail-fast: a permanent rejection fails fast with its actual reason, and a transient one is retried once (100 ms). The event log is still the one startup payload that can exceed the per-message ceiling, but only through its non-config content: device definitions carry a count bound (`config.MAX_DEVICES`) and length bounds for `id`/`name` (`device_factory.MAX_DEVICE_*_LENGTH`), while the per-device lists and driver `failure_reason` strings remain unbounded by configuration. When it exceeds the ceiling, the rejection is permanent *for that object* — Core 1 rebuilds the bounded fallback summary (the same startup statuses and device counts, but no `ready_devices`/`failed_devices`), which stays far under the limit, and admits that one. A serialization `MemoryError` from the event log gets the same answer: on a memory-tight board (the Pico W after the device-init pass) the pool can hold tens of KiB in total yet no run large enough for the serialized form, so the size check never sees the bytes — the bounded summary is the smaller object that answers it. `core1_main()` runs an allocation-light `gc.collect()` before building the stream, so a reclaimable pool admits the full forms on the boards they fit. Every other permanent rejection (validation, other serialization failures) fails fast with its actual reason, and a `MemoryError` on the fallback itself propagates to the recovery boundary — nothing is admitted, nothing loops. Both the event log and the fallback keep the single-transient-retry admission discipline below.

Telemetry admission is gated on event-log admission: if the first admission attempt is rejected, Core 1 waits 100 ms and retries once. If it is still rejected, Core 1 raises and halts — no telemetry goes out without the startup log being admitted.

## Core 0 hardware watchdog

Every layer now has a supervisor: Core 0's heartbeat watchdog (above) recovers a dead Core 1, and the `machine.WDT` hardware watchdog — armed by `Core0.start()` once the startup contract has passed — recovers a Core 0 that is alive but no longer making progress (a wedged network driver, a deadlock, a native hang). The exception boundary in `main.py` is complementary: it catches an unrecoverable Core 0 *exception*; the hardware watchdog catches a Core 0 that never raises.

- **Arming is deferred to the end of `start()`** — after the deliberately unbounded connect/verification loops, which a watchdog would reset into the same waits. Before arming, nothing in the startup path is supervised; after arming, every path is (below).
- **The budget is derived, not arbitrary**: `WDT_TIMEOUT_MS` (8 s) sits under the RP2 hardware maximum (8388 ms, `WDT_TIMEOUT_MAX` in the rp2 port) while exceeding every single blocking operation Core 0 performs. The longest such operation is one bounded MQTT exchange wait, and both sources of those waits are bounded at the config/constant layer — `config.MAX_MQTT_BROKER_RESPONSE_TIMEOUT_SEC` (5 s, capping the CONNACK/SUBACK, PUBACK, `check_msg`-completion, and UTC-request waits) and `mqtt._MAX_PINGRESP_WAIT_SEC` (5 s) — with `tests/test_core0_wdt.py` pinning both under the watchdog budget. A stalled link therefore fails on its own timeout before the watchdog can fire: a watchdog reset means "Core 0 is wedged", never "the broker was slow".
- **Longer waits are sliced, not blocked**: every long wait in Core 0's paths (reconnect backoffs, the Wi-Fi observation window, the startup-retry delay) sleeps in 100 ms slices with `_service_wait()` between them, and `_service_wait()` feeds the watchdog. Feeding comes only from Core 0's own execution — the run loop (once per pass) and the slices it drives — never from an independent timer, IRQ, or Core 1: another subsystem keeping the watchdog alive could mask a dead Core 0, which is exactly what the watchdog exists to detect.
- **Degradation, not a reset loop**: arming is one intentional capability probe (the Wi-Fi `PM_NONE` precedent). A build without `machine.WDT` keeps the firmware running without hardware supervision and prints a warning; making absence fatal would reboot into the same missing attribute forever. A `feed()` failure, by contrast, is a real failure and escapes to `main.py`'s controlled-reset boundary (no catch on the feed path).
- **A watchdog reset is silent**: the board resets with no retained cause (unlike `Mqtt.last_connect_error`), visible on the next boot only as the absence of a shutdown log. That is the accepted trade for independent supervision.

## Core 1 liveness heartbeat

Core 1 refreshes the `core_1_activity_ms` state mailbox on a 5-second deadline, independent of read-loop phase: the refresh fires on any 20 ms loop iteration once its deadline has passed, rather than on a fixed grid aligned to the loop step. A deadline-based refresh cannot starve even when the actual loop period (sleep plus processing) does not divide the refresh window evenly.

Health messages report `core_1_active` as true while the stamp age is within threshold (`3 × read_loop_sec`, minimum 60 seconds), and add `core_1_inactive` to the degradation reasons when it is exceeded.

Coverage begins at the worker spawn: `main.py` stamps before the worker starts waiting, `core1_main()` registers the first stamp at the top of its body before `SystemInformation`/`DeviceManager` construction and before `initialize_devices()`. Without that, a Core 1 that died waiting for the ready snapshot, or that wedged inside the core1 import or a driver constructor or a device `initialize()` call, would be indistinguishable from a Core 1 that had not started at all (the watchdog is a no-op until the first stamp exists), and the watchdog could never fire. Conversely, a *legitimate* long initialization (several devices × attempts × retry delays) must not age the stamp past the 30 s bound, so Core 1 hands `DeviceManager` an optional `activity_refresh` callback — the same optional-callback pattern `Wifi`/`Mqtt` receive for `wait_service` — and `DeviceManager` invokes it at each progress boundary: before each device, before each `initialize()` attempt, and at each step boundary inside each retry sleep (`device_initialization_retry_delay_ms` is bounded operationally at `config.MAX_DEVICE_INITIALIZATION_RETRY_DELAY_MS` (60 s) but has no smaller structural bound, so the delay is slept in steps of at most 100 ms — a monolithic sleep would age the stamp past the 30 s bound for a long configured delay and produce a false watchdog reset). The refresh stops precisely when a driver call stops returning, so a wedge is still caught; a `None` callback (the default) leaves behavior unchanged.

Runtime device work gets the same progress-boundary treatment. A device pending reinitialization (read failures reaching the threshold, retrying `initialize()` mid-run) invokes the same callback at the same boundaries — before each attempt and at each step inside each retry sleep — so a legitimate reinitialization retry sequence cannot age the stamp past the 30 s bound either. And the periodic heartbeat itself uses a fresh clock: it re-captures `time.ticks_ms()` at the moment of comparing and stamping, not the `now_ms` captured before the device read (the same stale-clock correction the read-boundary skip applies via `skip_now_ms`), so a healthy but slow read that carries a heartbeat boundary due in flight stamps the post-read time instead of the pre-read one. What the strategy deliberately does NOT cover is a driver wedged inside a single `read()`/`initialize()` call: the refreshes stop, the stamp ages past the bound, and the watchdog resets the board — exactly what a wedge should do.

Core 0 is the independent consumer of the heartbeat. The Core 0 run loop checks the stamp first on every pass (`_watch_core_1_heartbeat`): before any stamp exists (Core 1 has not started) the check is a no-op, so the unbounded startup connect loops are unaffected; once a stamp exists, age at or beyond `_CORE_1_HEARTBEAT_STALE_TIMEOUT_MS` (30 s, a static constant in `core0.py`, not a config key) resets the MCU via `machine.reset()`. The 30 s bound is more than six missed 5 s refreshes — far beyond any live-loop processing gap — and below the 60 s diagnostic threshold. This closes the failure mode where a dead Core 1 wedges the whole sensor (telemetry and health stopped, nothing to recover them) without Core 0 ever noticing: a dead Core 1 can no longer build the health message that would report `core_1_inactive`, so it cannot report its own death. A reboot starts clean: the mailbox is empty until the new Core 1 registers, and the check stays a no-op meanwhile.

The watchdog is not blind while Core 0 is stuck reconnecting. The `establish_network()` connect loops and retry backoffs (shared by startup and mid-run recovery) are the longest waits on Core 0 — each Wi-Fi attempt observes the link for up to 20 s, backoffs run up to 40 s, and the whole sequence repeats — and exactly that window is when a Core 1 that died *with* the network (an outage is the likeliest common cause) would otherwise go unnoticed: the check would next run only when the network returned, or never if it did not. Core 0 therefore exposes one servicing hook, `_service_wait()` (which runs the heartbeat check), and passes it to `Wifi` and `Mqtt` as an optional `wait_service` callback and uses it itself: every one of those waits now sleeps or observes in 100 ms slices with the hook invoked between slices, and Wi-Fi's per-attempt polling loop invokes it on each 100 ms iteration. A stale heartbeat therefore resets the board *during* the recovery wait; because the check is a no-op before Core 1's first stamp, the hook is equally safe in the unbounded startup connect loops. `machine.reset()` never returns on hardware, so the hook cannot be swallowed by the connect loops' exception handlers.

## Core 0 runtime recovery boundary

The liveness watchdog above supervises Core 1; the boundary in `main.py` closes the reverse direction. Core 0's fail-fast policy (a `MemoryError` re-raised through every generic handler) is a *local* rule: continuing to allocate on an exhausted heap is unsafe. Without a system-level policy the supervision was one-way — a Core 0 that terminated (a `MemoryError`, an unexpected parser failure, any exception escaping `start()` or `run()`) would leave the board with no networking, no MQTT, no Core 1 supervision, and no recovery: a transient failure becomes a permanent outage until something external resets the Pico.

The boundary therefore distinguishes two phases with different contracts:

- **Deterministic startup validation** (hardware detection, config loading/splitting, Core 0 construction) stays *outside* the boundary and fails fast: a misconfigured or unsupported board must stay down with a diagnosable error rather than reboot forever.
- **Operational runtime** — `Core0.start()` through `Core0.run()` — is *inside* the boundary: an unrecoverable Core 0 exception there is a controlled `machine.reset()` instead of permanent application termination. The Core 1 worker spawn sits *outside* it, in deterministic startup (fail-fast: at that point the pool is still intact, and a spawn `MemoryError` would be deterministic — resetting into it would reboot-loop). Core 1 gating is preserved by the ready snapshot: if `start()` fails, the board resets at the boundary before that snapshot is published, so the waiting worker never starts Core 1. A worker that dies *after* the spawn (in the wait or the import) is not at the boundary at all — its liveness stamp goes stale and Core 0's heartbeat watchdog resets the board (observed on the Pico W as `[FATAL] Core 1 heartbeat stale (30003 ms) - resetting`).

The `MemoryError` handler performs no allocation — it resets immediately — because a diagnostic line would itself be an allocation on an exhausted heap, and a handler that raised would defeat the reset it exists to provide. The generic `Exception` handler logs one diagnostic line first (safe: by construction the failure is not heap exhaustion) and then resets. `machine.reset()` never returns on hardware, so neither handler falls through.

## Normal-runtime scheduling anchor

All periodic Core 1 runtime work — telemetry and health — is scheduled from one shared epoch, `normal_runtime_start_ticks_ms`, captured exactly once, after the `system_startup_completed` event log is admitted (the gate). No periodic work begins before that admission. In addition, one initial sample — a telemetry read pass and a health report — is emitted at the anchor moment, immediately after admission and before the run loop, so a subscriber that connects at boot sees current data without waiting a full interval. It is a one-shot, not a second grid: the periodic boundaries below are unchanged.

- `boot_ticks_ms` remains the boot-lifetime reference: firmware uptime (`uptime_ms`), startup duration, and lifecycle diagnostics are all measured from boot. It is no longer the scheduling origin for periodic work.
- `normal_runtime_start_ticks_ms` anchors the initial sample and the periodic operational work: telemetry boundaries at `anchor + n × read_loop_sec`, health boundaries at `anchor + n × health_interval_sec`. With a 13-second startup, `read_loop_sec = 20`, and `health_interval_sec = 60`, telemetry falls at 13 (immediate), then 33/53/73/93... seconds of uptime, and health at 13 (immediate), then 73/133/193/253... seconds of uptime.
- The two schedulers share the epoch but stay independent: each keeps its own deadline, neither derives its deadline from the other, and neither may fire the other. When both are due (health interval is a multiple of the read loop), both process normally through the existing outbound queue at their existing priorities (TELEMETRY = 40, HEALTH = 70).
- The anchor is captured once per runtime. A Wi-Fi reconnect, MQTT reconnect, UTC resynchronization, device reinitialization, or queue drain must never re-capture it. Only a true reboot — a new `runtime_id` and new `boot_ticks_ms` — creates a new anchor.
- Deadlines advance from the previous scheduled deadline (deadline + interval), never from the moment a message was actually generated, so per-iteration processing delay cannot accumulate into drift.

## Uptime accounting

Every message envelope carries `uptime_ms`, and both cores compute it from `uptime.py`: each core seeds a small state from `boot_ticks_ms` and advances it by the delta between consecutive recent samples (`create_uptime_state` / `current_uptime_ms`). No code diffs the original boot tick against the current tick in one step — `time.ticks_diff()` is only guaranteed correct within half a tick period, so the one-shot form wraps on a long-running device while the accumulated form stays monotonic and correct across a wrap.

## Periodic Telemetry

Telemetry cadence is anchored to the normal-runtime anchor: `read_loop_sec` defines fixed boundaries at `anchor + n × read_loop_sec` (a 20-second read loop produces boundaries at +20s, +40s, +60s relative to normal-runtime start), and one initial read pass runs at the anchor moment, immediately after `system_startup_completed` is admitted. Telemetry remains historical sensor/runtime data: during an MQTT outage it may continue to enter the bounded outbound queue under the existing retention/eviction rules. Telemetry generation is gated on startup-log admission — no telemetry before `system_startup_completed` is admitted.

- **Missed boundaries are skipped, never replayed** (same policy as health): if a device read stalls long enough to cross one or more boundaries, the scheduler performs the current sample once and advances directly to the next future boundary — it does not fire catch-up reads for the elapsed boundaries. Telemetry is a current sample, not a replayable record, so a stall cannot reconstruct the missed samples; a catch-up burst would only add near-duplicate samples, JSON work, and queue admissions immediately after an overload. This is distinct from outage buffering (above), which still applies: an MQTT outage queues telemetry rather than dropping it.

## Periodic Health Messages

Core 1 generates health messages on a normal-runtime-anchored cadence: `health_interval_sec` defines fixed boundaries counted from `normal_runtime_start_ticks_ms` (a 60-second interval produces boundaries at +60s, +120s, +180s, +240s relative to normal-runtime start), independent of when startup merely completed and independent of the telemetry scheduler. One immediate health report is generated at the anchor moment, right after `system_startup_completed` admission (telemetry sample first, so the health report reflects queue state that already includes the fresh samples), subject to the same network-ready/MQTT-connected gate as the periodic reports. The health message contains current-state diagnostic fields without turning the payload into a full system information report.

### Scheduling

- **Normal-runtime-anchored**: one immediate report is emitted at the anchor (captured once, immediately after successful `system_startup_completed` admission), then the first periodic deadline is `normal_runtime_start_ticks_ms + health_interval_sec`.
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
   - Short, bounded deltas (Core 1 activity age, publish pacing, retry throttles) use `time.ticks_diff()` — correct because the two samples are always close in time
   - Long-duration elapsed (the current UTC timestamp and the UTC sync age) is derived from the accumulated uptime (`uptime.py`): the timestamp is `runtime_start_epoch_ms + current_uptime_ms`, and the sync age is `current_uptime_ms - sync_uptime_ms`. It never diffs the current tick against the sync tick in one step — that one-shot form is only guaranteed within half a tick period, so once the snapshot is that old (a long network/server outage) it goes stale or negative and, in `_utc_sync_due()`, would suppress the very resync that repairs the clock
   - Uptime since boot is accumulated from deltas between recent samples (`uptime.py`), never as a single `ticks_diff(now, boot)` — that one-shot form is only guaranteed within half a tick period and wraps on long-running devices
   - UTC sync age: integer division of milliseconds by 1000

4. **Memory pressure**: free heap below the hard floor — `minimum_free_heap_bytes` — adds `low_free_heap`. Both queues are admitted against the same two thresholds (see Memory safety), so a below-floor heap is itself the queue-pressure condition — no separate queue-utilization threshold exists.

5. **Low-priority retention**: Uses `RETENTION_PRIORITY_HEALTH = 70`, the lowest priority class

### Degradation Triggers

Health status is "degraded" when any of these conditions are true:
- `network_stack_not_ready`: Core 0 network not initialized
- `wifi_not_connected`: Wi-Fi disconnected
- `mqtt_not_connected`: MQTT broker connection lost
- `core_1_inactive`: Activity age exceeds threshold (3x read_loop_sec, minimum 60 seconds)
- `low_free_heap`: Free heap below the hard floor (`minimum_free_heap_bytes`)
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
11. **Return**: Core 0.start() returns; the ready snapshot (step 9) releases Core 1's waiting worker, which starts Core 1 (its modules are already imported pre-network)

Every step is self-healing: the connect steps (Wi-Fi, MQTT) retry forever, and the verification steps (each probe, the UTC sync) drop the MQTT session, re-establish the network, and retry the whole verification pass when one fails. Core 1 never starts until a complete, clean pass succeeds, so a transient blip after connection (a dropped PUBACK, a brief UTC-server outage) is recovered rather than halting the device until a reset. A `MemoryError` still propagates out of `start()` (fail-fast, so the device never keeps allocating on an exhausted heap) and terminates in `main()`'s recovery boundary as a controlled board reset (see Core 0 runtime recovery boundary).

## UTC time synchronization

Core 0 is the only UTC acquirer. Requests are published to `mqtt_topic_info_request` with a unique `request_id`; the server is expected to answer on `mqtt_topic_info_response` echoing that `request_id`.

- **Startup (required, self-healing)**: one pass of `_synchronize_utc_required()` makes a fixed number of attempts (3 — a constant, deliberately not derived from the timeout setting), each waiting up to `mqtt_broker_response_timeout_sec` for a valid response while pumping MQTT. A failed pass is not fatal: the network is re-established and the whole verification pass retried (see Deterministic startup sequence), so a transient UTC-server outage no longer halts startup until a reset.
- **Steady state (non-blocking)**: when the snapshot is older than `datetime_sync_interval_min`, the run loop sends a request and records a `mqtt_broker_response_timeout_sec` deadline. The run loop keeps servicing the outbound queue, network recovery, and command responses; the answer arrives through the regular `check_msg()` pump. The run loop never blocks on a UTC request.
- **Timeout and retry**: a pending request whose deadline passed is discarded, and re-requests are throttled to at most one per 30 seconds until a valid response arrives. An unresponsive time server therefore cannot stall the run loop or flood the broker. A malformed-but-reachable answer gets a much shorter backoff (~0.5s) since the server demonstrably answered us.
- **Response validation**: a response is accepted only if it matches the current schema version, is targeted at this device, carries the matching `request_id`, and contains a valid `timestamp` and positive integer `utc_epoch_ms`. A malformed answer to *our own* pending request clears that request and re-keys the retry throttle to a short ~0.5s backoff (instead of the full 30s measured from the original send); a response for any other request id is ignored without disturbing pending state (our response may still be in flight).

## Configuration management

`config.py` is the single source of truth for configuration **schema validation** (required/unknown keys, value checks, device-list structure) and per-core splitting; `validate_config(config)` is its pure, I/O-free validation path, shared by startup (`load_config()`) and the `write-config` command. The eight `mqtt_topic_*` channels are exact channel names (no wildcards or NUL, bounded for the subscribe Remaining Length) and **pairwise distinct**: inbound dispatch matches delivered topics by exact equality and the first matching branch wins, so a shared name can never be disambiguated by content — equal command/info_response names silently disable the entire command path (including write-config, the repair channel) on a device that still reports healthy, and a name shared with a locally published topic re-delivers the device's own publications inbound (MQTT 3.1.1 self-echo). `ConfigError` carries a stable machine-readable `code` (`unknown_config_fields`, `missing_key`, `invalid_value`, `invalid_config_schema_version`, `unsupported_device_type`, `unreadable_file`), and where applicable a sorted `unknown_fields` list (top-level keys, device-entry keys qualified as `devices[<id>].<key>`, and device-config keys qualified as `devices[<id>].config.<key>`) or a flat `details` map (e.g. the expected/received `config_schema_version`); `str(err)` remains the human-readable message.

### Whole-device configuration validation

A candidate device is one atomic configuration unit identified by `id`: completely valid and accepted for the next boot, or invalid and rejected. No field-level device patches. `write-config` and startup must reject an invalid device definition **before persistence**, without touching hardware.

- **Registry + pure entry point** (`device_factory.py`): each supported `device_type` maps to a **pure config validator** and its **allowed config keys**. `validate_device_definition()` is the pure per-device validator — generic definition shape, unknown definition fields, supported `device_type`, then dispatch to the type's pure validator. It **never constructs or initializes a hardware resource**: a valid definition with no physical backing passes, leaving physical absence to the boot-time `initialization_failed` outcome (an operational device failure, not a schema failure).
- **Pure device validator** (`devices/system_information/validation.py`, host-importable / no `machine`): the authoritative `system-information` rules — `include` is a list of unique strings drawn from `SYSTEM_INFORMATION_SECTIONS` (an empty list is the "all sections" shorthand, expanded in `SystemInformationDevice.initialize()`), and **unknown config keys are rejected**. `SYSTEM_INFORMATION_SECTIONS` now lives here; `system_information.py` and the driver read it from there. `SystemInformationDevice.initialize()` calls the same pure validator, so startup and write-time share one rule set.
- **Exception** (`devices.device.DeviceValidationError`, a `ValueError` with a stable `code`): the pure-validation failure type; `config.py` maps it onto `ConfigError` (preserving `code`). Because it is a `ValueError`, the device manager's per-device retry path still records it as `initialization_failed` if it ever reaches runtime.
- **Boot semantics unchanged**: each device initializes independently; one failure does not invalidate the others; tolerant startup and the existing retry/reinit behavior continue. Any `devices` change is `REBOOT_REQUIRED` (no live `DeviceManager` mutation from `write-config`).

`config_manager.py` (Core 0-owned; the manager is passed to `Core0` and **never shared with Core 1**) owns configuration **persistence and change policy**:

- **PERSISTED vs ACTIVE**: `config.json` is the committed configuration — always "what the next reboot will load" — and is what `read-config` returns. ACTIVE is what the running firmware uses. `reboot_required` is **derived** from a compact serialized ACTIVE snapshot (`snapshot exists <=> ACTIVE differs from PERSISTED`); it is never a sticky flag, and a reboot clears it by clearing RAM (no persistent flag). The manager retains no second permanent full config object in the normal state (the snapshot is a serialized string, deserialized only for a classification comparison).
- **Change policy table** (`config_manager._CHANGE_POLICY`, the single source of truth for classification): every required key except the `config_schema_version` invariant is exactly `HOT_RELOADED` or `REBOOT_REQUIRED`. HOT: `read_loop_sec`, `health_interval_sec`, `datetime_sync_interval_min`, `network_snapshot_interval_sec`, `mqtt_command_poll_ms`, `mqtt_outbound_publish_delay_ms`. REBOOT: `source`, `device_initialization_attempts`, `device_initialization_retry_delay_ms`, `device_read_failure_threshold`, `outbound_queue_max_messages`, `mqtt_broker_ip_address`, `mqtt_keepalive_sec`, `network_probe_timeout_sec`, `mqtt_broker_response_timeout_sec`, all eight `mqtt_topic_*`, `wifi_reconnect_delays_sec`, `mqtt_reconnect_delays_sec`, `devices`. A test asserts the table covers exactly `_REQUIRED_KEYS - {config_schema_version}`, and a second test pins the classified-HOT set to exactly the two live apply sets — HOT_RELOADED promises a live effect, so a key whose value no steady-state runtime consumes (the network probe timeout runs only in the startup verification contract) must be REBOOT_REQUIRED.
- **Classification**: a candidate semantically equal to PERSISTED is `UNCHANGED` (no filesystem write, reboot state preserved); otherwise it is compared against ACTIVE (PERSISTED, or the deserialized snapshot when one exists) — any difference under reboot policy is `REBOOT_REQUIRED`, else `HOT_RELOADED`. The response `changes` summary describes **this** write: PERSISTED-before vs candidate, deterministically sorted, scalar `original_value`/`new_value` pairs, and compact whole-device entries for `devices` (`change_type` `ADDED`/`REMOVED`/`MODIFIED` with `device_id` and `device_type`; a difference no id accounts for — a list-order change — is one bounded list-level `MODIFIED` entry, never the arrays).
- **File roles**: `config.json` committed; `config.json.tmp` fully-written uncommitted candidate; `config.json.old` previous committed config retained only while a HOT transaction awaits runtime application. Steady state after any successful boot/transaction is exactly `config.json`.
- **Write transaction** (one at a time; a second write while one is pending is refused): `validate_config(candidate)` → load/validate PERSISTED → classify → (first reboot transition only) serialize the ACTIVE snapshot **before** any file change → write `.tmp`, close, `os.sync()` → **read `.tmp` back and re-validate** (a corrupted write aborts before promotion) → `config.json`→`.old`, `.tmp`→`config.json`, `os.sync()`. REBOOT deletes `.old` and is complete. HOT keeps `.old` until Core 0 reports the runtime application outcome: **commit** (discard any pending snapshot, delete `.old`, sync) or **rollback** (`.old`→`config.json`, sync; the pre-transaction snapshot state is restored — `begin_write` only ever *creates* a snapshot, for REBOOT). A cancelling HOT write (candidate becomes ACTIVE without reboot) discards the pending reboot on commit. `MemoryError` propagates to the fail-fast boundary, never converted into an ordinary failure.
- **Boot recovery** (`ConfigManager.recover()`, run in `main.py` before `split_config`): a valid `.old` is authoritative — its presence means a promotion was interrupted before its commit point, so it is restored even over a valid but uncommitted `config.json` (the previous committed config, stale `.tmp` removed **after** that decision); else a valid `config.json` is the committed steady state (an invalid `.old` released); else a valid `.tmp` is promoted; else startup fails with `ConfigError`. Artifacts are never deleted before the recovery decision.
- **Commands**: `read-config` (payload exactly `{}`) answers `data: {config, reboot_required}` — the committed configuration, no Wi-Fi secrets. `write-config` (payload exactly `{"config": <complete candidate configuration>}` — unknown payload keys are named together as `unknown_fields`; no patch/merge/partial-update shape; `*` broadcast silently ignored) answers `data: {configuration_changed, classification, reboot_required, changes}` on success, or a bounded error carrying the `ConfigError` code (plus sorted `unknown_fields` when the cause is unknown keys, or the expected/received schema version when it mismatches). A `REBOOT_REQUIRED` answer is published on the currently active source/MQTT session — the candidate's reboot-only networking values are not live until the caller issues `reboot` (write-config never reboots by itself).
- **HOT_RELOADED application** (Core 0 orchestrates; the manager never touches subsystems): one logical transaction across both cores, single-flight. Core 0 updates its four live HOT settings in its own config dict (`mqtt_command_poll_ms` is read per run-loop pass so it applies immediately; when it changes, the last-poll stamp is re-anchored to the reload instant so the new cadence starts cleanly) and posts one **config-update request** on the dedicated `config_update_lane` (`{generation, read_loop_sec?, health_interval_sec?}` — only the changed Core 1 keys) — internal control traffic, not an external command, and never on the user-command event queue. Core 1 takes the request, **re-anchors** each changed interval from the reload instant (`next = now + interval` — a new scheduling boundary, deliberately not re-gridded to the boot anchor; no catch-up sample for a shortened interval), refreshes its own `config` copy (so the health activity threshold, which reads `read_loop_sec`, uses the new value), and posts exactly one result for that generation. Core 0 holds the response and the file commit until it reads the result in the run loop: **success** → commit (release the retained `.old`) and send the success response; **failure** → restore the applied Core 0 values, roll the file back (`.old`→`config.json`), and answer the bounded `error.code` Core 1 posted (default `core1_apply_failed`). A dead Core 1 never acks, but that is bounded by the existing liveness watchdog (board reset; boot recovery restores the retained `.old`), so no unbounded wait and no success response before the runtime update is complete.

## Features intentionally not carried into the baseline

These should be added individually only after the baseline passes:

- advanced reboot-delivery state machines;
- advanced network recovery generations/state machines;
- full dynamic configuration (the read-config/write-config subset with the policy-table hot reload is present since 0.4.43);
- advanced watchdog/cross-core recovery (the minimal Core 0 stale-heartbeat reset is present since 0.4.9, and the Core 0 hardware watchdog that supervises Core 0 itself is present since 0.4.81 — see Core 0 hardware watchdog);
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
