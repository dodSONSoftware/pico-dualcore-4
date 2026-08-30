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
  "firmware_build_commit": "<short-sha>|unknown",
  "payload": {
    "status": "healthy|degraded",
    "degraded_reasons": ["<reason1>", "<reason2>"],
    "hardware_type": "pico_w|pico_2_w",
    "machine": "Raspberry Pi Pico W with RP2040",
    "last_reset_cause": "power_on_reset|hard_reset|watchdog_reset|deep_sleep_reset|soft_reset|unknown",
    "boot_reason": "power_on|watchdog_recovery|soft_reset|unknown",
    "network_stack_ready": true,
    "wifi_connected": true,
    "wifi_rssi_dbm": -45,
    "mqtt_connected": true,
    "core_1_active": true,
    "core_1_activity_age_ms": 42,
    "free_heap_bytes": 95728,
    "minimum_free_heap_bytes": 65536,
    "heap_headroom_bytes": 30192,
    "minimum_free_heap_observed_bytes": 95728,
    "devices_configured": 1,
    "devices_active": 1,
    "device_failures": 0,
    "outbound_queue_depth": 0,
    "outbound_queue_capacity": 64,
    "outbound_queue_utilization_percent": 0,
    "outbound_queued_bytes": 0,
    "outbound_queue_drain_active": false,
    "outbound_queue_last_drain_start_depth": 0,
    "outbound_queue_last_drain_message_count": 0,
    "outbound_queue_last_drain_duration_ms": 0,
    "outbound_queue_last_drain_rate_per_sec": 0,
    "utc_valid": true,
    "utc_sync_age_sec": 52,
    "mqtt_publish_attempt_count": 0,
    "mqtt_publish_retry_count": 0,
    "mqtt_puback_timeout_count": 0,
    "mqtt_connection_failure_count": 0,
    "mqtt_reconnect_success_count": 0,
    "mqtt_last_reconnect_duration_ms": 0,
    "mqtt_last_outage_duration_ms": 0,
    "wifi_rssi_min_dbm": -52,
    "wifi_rssi_max_dbm": -41,
    "wifi_rssi_moving_average_dbm": -47,
    "wifi_last_reconnect_duration_ms": 0,
    "wifi_last_dhcp_acquisition_duration_ms": 1830,
    "gateway_reachable": null,
    "dns_reachable": true,
    "mqtt_broker_last_round_trip_ms": null,
    "wifi_bssid": null,
    "wifi_channel": null
  }
}
```

### Status Fields

- `status`: "healthy" when no degradation reasons, "degraded" otherwise
- `degraded_reasons`: Array of active degradation reasons

### Hardware Fields

- `hardware_type`: Canonical hardware type from `detect_hardware()`
- `machine`: Raw machine string from `os.uname().machine`
- `last_reset_cause`: How the current boot began (`power_on_reset`, `hard_reset`, `watchdog_reset`, `deep_sleep_reset`, `soft_reset`, or `unknown` when it cannot be determined). Historical diagnostic information, captured once at startup from `machine.reset_cause()` and reported for the entire runtime — it never adds a degraded reason and never changes `status`.
- `boot_reason`: Firmware-known semantic reason for starting this runtime (`power_on`, `watchdog_recovery`, `soft_reset`, or `unknown`). Derived once at startup from `last_reset_cause` via `derive_boot_reason()` — the firmware never claims more certainty than the reset cause supports (there is no persisted `explicit_reboot`), and it never adds a degraded reason.

### Network Fields

- `network_stack_ready`: True once Core 0 has verified the complete startup contract (from the network snapshot)
- `wifi_connected`: Wi-Fi link is up (from the network snapshot)
- `wifi_rssi_dbm`: Current Wi-Fi RSSI from Core 0 network snapshot (may be null)
- `mqtt_connected`: MQTT broker connection is up (from the network snapshot)

### Memory Fields

- `free_heap_bytes`: Current `gc.mem_free()` value
- `minimum_free_heap_bytes`: Board-specific heap reserve (64KB Pico W, 128KB Pico 2 W), a code constant in `hardware.py` — not config and not a heap percentage
- `heap_headroom_bytes`: free_heap - minimum_free_heap (negative when below reserve)
- `minimum_free_heap_observed_bytes`: The lowest free heap observed at an explicit checkpoint since boot, from the shared `MemoryStats` low-watermark. Historical/diagnostic only: it is a checkpoint low-watermark (not a guaranteed "absolute minimum"), is recorded before the GC that follows it, and never adds a degraded reason or changes `status` (`free_heap < minimum_free_heap` is the only heap-based degraded reason).

### Core Activity Fields

- `core_1_active`: True if `core_1_activity_age_ms <= threshold`
- `core_1_activity_age_ms`: Monotonic elapsed time since last activity report

### Device Fields

- `devices_configured`: From `DeviceManager.get_status_snapshot()`
- `devices_active`: From `DeviceManager.get_status_snapshot()`
- `device_failures`: devices_configured - devices_active

### Queue Fields

- `outbound_queue_depth`: Queued + in-flight entries
- `outbound_queue_capacity`: The fixed entry ceiling from `OutboundQueue` (`MAX_OUTBOUND_QUEUE_ENTRIES = 64`, an internal pathological sanity guard — not a user-tunable size and not the memory-safety boundary; the free-heap reserve is)
- `outbound_queue_utilization_percent`: (depth * 100) // capacity (integer)
- `outbound_queued_bytes`: Diagnostic retained payload bytes (the queued FIFO plus the in-flight entry, matching the retained-payload accounting); no longer an enforced admission budget

### Post-Outage Queue Drain Fields

How fast the buffered outbound queue drained after the most recently *completed* MQTT-reconnect drain (see Post-outage queue drain). Owned by Core 0 and copied into the network snapshot; all are `false`/`0` before the first completed drain. Like the other historical fields, they are **observational only**: a slow past drain never adds a degraded reason or changes `status` (the current-state rules above remain the only source of degraded reasons).

- `outbound_queue_drain_active`: True while Core 0 is currently draining an outbound backlog that existed when MQTT returned after a runtime outage; false before the first drain, after it completes, and after it is interrupted by another failure
- `outbound_queue_last_drain_start_depth`: Outbound queue depth (queued + in-flight) observed when the last completed drain episode began — the backlog size right after the reconnect
- `outbound_queue_last_drain_message_count`: Entries successfully completed (PUBACK'd, then `complete_in_flight`) during the last completed episode — may exceed the start depth if Core 1 kept producing messages while the backlog drained
- `outbound_queue_last_drain_duration_ms`: Monotonic elapsed time (ticks-based) from episode start until the queue reached zero; includes time spent servicing higher-priority Core 0 work in between
- `outbound_queue_last_drain_rate_per_sec`: Average successful drain throughput, `message_count * 1000 // max(1, duration_ms)` (integer; zero is acceptable for a very small count over a long span — compute from the two raw fields for a more precise rate)

### UTC Fields

- `utc_valid`: True if UTC snapshot is available
- `utc_sync_age_sec`: Seconds since last UTC sync (integer, null if never synchronized)

### MQTT Reliability Fields

Runtime-lifetime MQTT reliability metrics, owned by the `Mqtt` class (the single source of truth) and copied by Core 0 into the network snapshot. All are integers, `0` before the first such event, and never null. They reset naturally on reboot and are **historical/diagnostic only**: they describe what happened and never add a degraded reason or change `status` on their own (the current-state rules above remain the only source of degraded reasons).

- `mqtt_publish_attempt_count`: +1 each time an actual QoS 1 publish begins (immediately before the low-level publish). Counts initial attempts and retries of the same message; never counts a message that is merely serialized, queued, gated, or rejected.
- `mqtt_publish_retry_count`: +1 only when the same logical message is re-attempted after an earlier failed/ambiguous attempt. Classified by Core 0 (the owner of logical message identity) and passed to `Mqtt` explicitly — never inferred from a packet id, topic, payload, or time. Always `<= mqtt_publish_attempt_count`.
- `mqtt_puback_timeout_count`: +1 only when the complete PUBLISH frame was written and the PUBACK wait reached its configured timeout. Not counted for write timeouts, TCP resets, CONNACK/SUBACK/PINGRESP timeouts, or ordinary connection failures.
- `mqtt_connection_failure_count`: +1 per failed connection attempt inside `Mqtt.connect()` (TCP + CONNECT + CONNACK + SUBSCRIBE/SUBACK). The final exhausted return is not an extra failure.
- `mqtt_reconnect_success_count`: +1 when MQTT becomes connected after at least one prior successful connection in the same runtime. The initial connection does not count.
- `mqtt_last_reconnect_duration_ms`: From the first reconnect attempt after a lost session to successful recovery. Spans failed attempts and backoff sleeps; does not restart per attempt. `0` until the first reconnect completes.
- `mqtt_last_outage_duration_ms`: From the detected connected→disconnected transition to successful recovery, including any Wi-Fi recovery time. `0` until the first outage completes.

### Network Diagnostics Fields

Wi-Fi quality and reachability diagnostics, sourced from the Core 0 network snapshot (passive Wi-Fi state owned by `wifi.py`, active reachability probes owned by Core 0). Like the reliability metrics above, they are **historical/observational only**: they describe what happened and never add a degraded reason or change `status` (the current-state rules above remain the only source of degraded reasons). Null means not-yet-tested or unsupported — it is never converted to `false`; `gateway_reachable` / `dns_reachable` are `true`/`false` only after a probe has actually run.

- `wifi_rssi_min_dbm`: Lowest RSSI sampled since boot (null until the first sample). Lifetime value; not reset on reconnect.
- `wifi_rssi_max_dbm`: Highest RSSI sampled since boot (null until the first sample).
- `wifi_rssi_moving_average_dbm`: Integer fixed-point EMA of RSSI (null until the first sample).
- `wifi_last_reconnect_duration_ms`: Full retry/backoff span of the last reconnect. `0` until the first reconnect completes (the initial boot connect is not a reconnect).
- `wifi_last_dhcp_acquisition_duration_ms`: connect-to-IP-ready duration (association + auth + DHCP, not an isolated DORA exchange) of the last connection. `0` until a connection completes.
- `gateway_reachable`: Gateway reachability from a bounded ICMP echo — `true`/`false` only after a probe ran, null otherwise. When the port cannot create a raw socket (expected on RP2/cyw43) it stays null; the probe is never faked with a UDP send.
- `dns_reachable`: DNS-server reachability from one bounded UDP query to the configured server — `true`/`false` only after a probe ran, null otherwise.
- `mqtt_broker_last_round_trip_ms`: Broker round-trip latency from the existing QoS 1 network probe, recorded only when `network_diagnostics_broker_latency_enabled` is true and the QoS 1 path is idle. Null when disabled or when the probe was skipped/failed.
- `wifi_bssid`: Associated BSSID (`aa:bb:cc:dd:ee:ff`) where the port can report it reliably, null when unavailable (never from a scan).
- `wifi_channel`: Wi-Fi channel (1–14) where the port can report it reliably, null when unavailable.

### Degradation Reasons

- `network_stack_not_ready`: Core 0 network not fully initialized
- `wifi_not_connected`: Wi-Fi disconnected
- `mqtt_not_connected`: MQTT broker connection lost
- `core_1_inactive`: Core 1 activity exceeds threshold (3x read_loop_sec, min 60s)
- `low_free_heap`: free_heap < minimum_free_heap (the only heap-based trigger; the low-watermark minimum `minimum_free_heap_observed_bytes` never adds a reason)
- `device_count_mismatch`: devices_active != devices_configured
- `outbound_queue_pressure`: entry utilization (depth / entry ceiling) >= 75%
- `utc_not_valid`: UTC snapshot unavailable

### Queue Priority

Health messages use `RETENTION_PRIORITY_HEALTH = 70`, the lowest priority class.

### Outage Behavior

Health messages are only generated when:
1. Network snapshot is available (`network_stack_ready = True`)
2. MQTT is connected (`mqtt_connected = True`)

This prevents health messages from accumulating during MQTT outages. A boundary skipped during an outage is never replayed after recovery: the scheduler waits for the next normal-runtime-relative boundary.

## Observability contract

Diagnostic events are stable, machine-readable, and low-noise. Every structured
diagnostic log event answers four separate questions:

- `event` — what happened (stable, `lowercase_snake_case`, describes the
  transition, not the cause),
- `reason_code` — why it happened (finite canonical vocabulary; `"none"` for a
  normal successful transition; never built from exception text),
- `message` — optional human-readable explanation (operators may read it;
  consumers must not depend on it),
- `data` — focused structured values directly useful to understanding that
  event (no full snapshots, no full config).

All event/reason/level constants are module-level string constants in
`observability.py` (no classes, no registries, no runtime validation), and
`build_event_payload()` is the single small producer of the log payload shape:

```json
{ "level": "INFO|WARNING|ERROR", "event": "...", "reason_code": "...",
  "message": "...", "data": { } }
```

`message` and `data` are omitted when empty — never forced. The former
`module` field is gone: the event-name domain prefix (`wifi_*`, `mqtt_*`,
`device_*`, `command_*`, `runtime_*`, `utc_*`) carries the domain.

Common envelope identity (`source`, `message_type`, `sequence`, `runtime_id`,
`firmware_version`, `firmware_build_commit`, `message_schema_version`,
`uptime_ms`, `timestamp`) is the one canonical location for identity:
producers never duplicate `runtime_id`, `uptime_ms`, `firmware_version`, or
`firmware_build_commit` inside `payload`/`data`.

**`firmware_build_commit`** is the Git commit used to build the release: a
short SHA (≥ 7 hex characters) injected by `release.py` into a generated
`build_info.py`, or `"unknown"` when the firmware runs outside the release
process. The Pico never runs `git`. It stays separate from `firmware_version`
(same semantic release + different development build remain distinguishable).

**Build identity in system information:** the `runtime` section carries
`firmware_version`, `firmware_build_commit`, `message_schema_version`, and
`runtime_id`; the `machine` section carries `last_reset_cause` and
`boot_reason`.

**Capabilities** — the `capabilities` system-information section answers "what
can this firmware build do", not "what is healthy" or "what is configured":
`devices` lists the supported driver types from the device factory registry
(`supported_device_types()` — the single source; currently
`["system-information"]`), and `features` is a small static tuple of
implemented firmware-wide capabilities (`health`, `commands`, `mqtt_qos1`,
`outage_buffering`, `network_diagnostics`, `heap_pressure_queue`,
`runtime_configuration`). Capabilities are static per build, advertised only
when genuinely implemented (`runtime_configuration` since 0.5.0; never planned
features such as `tls` or `broker_failover`), and are **not** part of sensor
telemetry payloads.

**Events emitted by the firmware** (transitions and meaningful failures only;
one event per transition, never per retry/cycle):

| Event | Level | Reason code | Producer |
|---|---|---|---|
| `runtime_started` | INFO | `none` | Core 1 one-time startup log; `data` carries `last_reset_cause` and `boot_reason` (plus the one-time startup/system-information snapshot) |
| `wifi_connection_established` | INFO | `none` | Core 0, initial connect |
| `wifi_reconnect_completed` | INFO | `wifi_reconnect_succeeded` | Core 0, recovery path |
| `mqtt_connection_established` | INFO | `none` | Core 0, initial connect |
| `mqtt_reconnect_completed` | INFO | `mqtt_reconnect_succeeded` | Core 0, recovery path; `data` carries the existing connect/reconnect/outage counters and durations |
| `utc_sync_completed` | INFO | `none` | Core 0, once after a clean startup UTC pass |
| `utc_sync_failed` | WARNING | `timeout` | Core 0, on a failed startup UTC pass (before self-heal retry) |
| `device_read_failed` | WARNING | `device_read_exception` | Core 1; `data`: device_id, device, consecutive_read_failures, `error` (human text) |
| `device_reinitialization_completed` | INFO | `none` | Core 1; `data`: device_id, device, reinitialization_attempts_used |
| `device_reinitialization_failed` | WARNING | `device_reinitialization_failed` | Core 1; first failure for a stuck device, repeats suppressed (existing policy) |
| `runtime_reboot_requested` | INFO | `none` | Core 0, on reboot command acceptance; `data`: command, command_id |
| `command_rejected` | WARNING | `command_invalid_envelope` / `command_invalid_payload` / `command_duplicate` / `command_unknown` | Core 0 and Core 1; `data`: command, command_id |
| `configuration_update_started` | INFO | `none` | Core 0 transaction coordinator, after validation and generation assignment; `data`: command_id, previous_generation, changed-key counts by policy |
| `configuration_update_completed` | INFO | `none` | Core 0 coordinator, when the transaction is committed; `data`: command_id, generation, changed-key counts by policy, duration_ms, reboot_required |
| `configuration_update_failed` | ERROR | `invalid_config_key` / `read_only_config_key` / `invalid_config_value` / `invalid_config_combination` / `configuration_stage_failed` / `configuration_reconfigure_failed` / `configuration_persistence_failed` / `configuration_rollback_failed` | Core 0 coordinator, on a rejected or failed transaction (runtime and `config.json` left at the previous state); `data`: command_id, previous_generation, changed-key counts, duration_ms |
| `configuration_rollback_completed` | INFO | `none` | Core 0 coordinator, when a failed activation is fully restored to the previous known-good state; `data`: command_id, generation (unchanged), duration_ms |
| `configuration_rollback_failed` | ERROR | `configuration_rollback_failed` | Core 0 coordinator, when restoring the previous state itself failed |
| `configuration_recovered` | WARNING | `configuration_primary_invalid` | boot (main, relayed by Core 0 after the network is up), when `config.json` was restored from `config.json.bak`; `data`: generation |
| `runtime_core1_stalled` | ERROR | `core1_heartbeat_timeout` | Core 0 watchdog, best-effort bounded publish before `machine.reset()` |

**Not emitted (vocabulary reserved):** connection-loss and per-attempt
events (`*_connection_lost`, `*_connection_attempt_*`) — outages are visible
through the recovery event plus the existing counters/durations in health and
system information, and per-attempt detail stays console-only;
`mqtt_publish_failed` / `mqtt_puback_timeout` as events (counted, then covered
by the recovery event); queue/memory events (counters in health/queues);
`command_received` (the `command_response` is the receipt); per-cycle
successes. Device **initialization** at boot is reported in the one-time
`runtime_started` snapshot, not as a per-device log burst.

**Command response error codes** (standardized; behavior unchanged):
`command_invalid_envelope`, `command_invalid_payload`,
`command_duplicate`, `command_execution_failed`, `command_unknown`. The command
name is structured data (`command`), never part of the event string.

**Configuration command error codes** (`read_config` / `write_config`,
standardized; see Runtime configuration management): rejection —
`invalid_config_key`, `read_only_config_key`, `invalid_config_value`,
`invalid_config_combination` (all atomic: nothing is applied and the
generation is unchanged); transaction — `configuration_update_in_progress`,
`configuration_stage_failed`, `configuration_reconfigure_failed`,
`configuration_persistence_failed`, `configuration_rollback_failed`.

**Boot reason derivation** (single mapping in `hardware.py`, aligned with the
reset-cause vocabulary — no second mapping exists):

| `last_reset_cause` | `boot_reason` |
|---|---|
| `power_on_reset` | `power_on` |
| `watchdog_reset` | `watchdog_recovery` |
| `soft_reset` | `soft_reset` |
| `hard_reset` | `unknown` |
| `deep_sleep_reset` | `unknown` |
| `unknown` | `unknown` |

**Message-type separation:** telemetry = measurements; health = compact
current + lifetime operational summary; system information = detailed
runtime diagnostics; log = discrete transitions/events;
`command_response` = command result. High-frequency state lives in
counters/metrics, never in the log stream.

**Event migration (0.4.28):** `system_startup_completed` →
`runtime_started`; `*_connection_established` on the recovery path →
`*_reconnect_completed`; `level "info"` → `"INFO"`; log payload `module`
field dropped; command error codes `invalid_payload` →
`command_invalid_payload`, `invalid_message_schema_version` →
`command_invalid_envelope`, `reboot_already_pending` → `command_duplicate`,
`intercore_event_queue_full` → `command_execution_failed`,
`unsupported_command` → `command_unknown`; device read/reinit failures
upgraded from console-only to `device_read_failed` /
`device_reinitialization_*` log events.

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
- `read_last_reset_cause()` — the single source of truth translating `machine.reset_cause()` into a stable firmware-facing string (`power_on_reset`, `hard_reset`, `watchdog_reset`, `deep_sleep_reset`, `soft_reset`, or `unknown` when it cannot be determined; the raw MicroPython integer is never published)
- `derive_boot_reason()` — the single mapping from `last_reset_cause` to the semantic `boot_reason` (`power_on`, `watchdog_recovery`, `soft_reset`, `unknown`); there is no second reset-cause vocabulary anywhere else

The reset cause describes the boot event, not the board. `main()` reads it once, immediately after `detect_hardware()`, stores it in the startup hardware snapshot, and publishes that snapshot to the hardware state mailbox before any core operation begins. The snapshot is immutable for the runtime: `SystemInformation.get_machine()` and the health builder both read `last_reset_cause` from the mailbox — never from `machine.reset_cause()` again — so every report for a given `runtime_id` carries the same value. A read failure degrades the value to `unknown`; it never fails startup. A previous `watchdog_reset` (or any other cause) is historical information and must never affect the current `status` or `degraded_reasons`.

See `hardware.py` for implementation details.

## Ownership invariants

1. Core 0 exclusively owns the complete network stack: `network.WLAN`, CYW43 networking, IP/DNS, sockets, MQTT, QoS 1, subscriptions, reconnect, UTC acquisition, reboot, and QoS 1 network probes for startup verification.
2. Core 1 exclusively owns the complete sensor/device stack: device drivers, I2C/SPI/UART/ADC, initialization, reads, device state, software sensors, and telemetry construction.
3. Only plain-data objects cross the core boundary.
4. Once an object is transferred into an inter-core lane it becomes immutable. Neither producer nor consumer may mutate it.
5. Live subsystem objects never cross cores.
6. Configuration: Core 0 owns the transaction coordinator, its own DYNAMIC/MQTT activation, persistence, and `ConfigState`; Core 1 applies its own DYNAMIC and `devices` changes. The config transaction mailbox is the only cross-core configuration channel, and a configuration change never reboots the device (the explicit `reboot` command is the only reboot path).

## Inter-core lanes

### 1. `outbound_queue`

Core 1 -> Core 0. Contains only data intended for MQTT.

- FIFO and bounded by the board's free-heap reserve (the memory-safety boundary, defended at admission — see Memory safety below) plus one internal entry ceiling (`MAX_OUTBOUND_QUEUE_ENTRIES = 64`, a pathological sanity guard, not a user knob). The former user-tunable `max_outbound_queue_entries` and the 32 KiB `DEFAULT_MAX_OUTBOUND_QUEUED_BYTES` byte budget are removed.
- Core 1 supplies only a message kind plus domain data; it does not know MQTT topics.
- Core 0 maps the kind to the authoritative MQTT topic, publishes with QoS 1, and owns the MQTT envelope (sequence, runtime_id, source, firmware_version, firmware_build_commit, message_schema_version). Kinds: TELEMETRY → `mqtt_topic_telemetry`, COMMAND_RESPONSE → `mqtt_topic_command_response`, HEALTH → `mqtt_topic_health`, LOG → `mqtt_topic_log`.
- The startup log and connection logs travel as KIND_LOG entries; no hardcoded topics cross into Core 1.
- The sender owns every message field, including `uptime_ms` and `timestamp` (the latter null when UTC is unsynchronized). A queued message must NOT carry any envelope key at the top level, or the wire document would repeat a member name.
- Core 0 injects the envelope at publish time by splicing its six members into the stored serialized object before its closing brace: the payload bytes are never decoded, parsed, or re-serialized on the publish path, so publishing allocates only the small envelope fragment plus the assembled frame.
- The MQTT client waits for the matching PUBACK before the next publish proceeds, naturally enforcing one application QoS 1 publish in flight. The wait is bounded by `mqtt_broker_response_timeout_sec`, so a blackholed link fails the publish (the entry stays in flight) instead of blocking the run loop.
- Retention priority is explicit: lower numeric values are more important.
- Priority classes are: CRITICAL 10, ERROR 20, WARN 30, TELEMETRY 40, INFO 50, HEALTH 70.
- **Heap-pressure admission (primary).** Before admitting an entry the queue observes the current free heap (a `MemoryStats` checkpoint). If it is at or below the reserve, a required `gc.collect()` runs (the optional-GC cooldown is bypassed — the reserve is a safety invariant), then the queue evicts the oldest entry in the least-important class the incoming entry is at least as important as, collecting after each eviction, and repeats until the reserve is restored. If the incoming entry is less important than everything queued it is rejected without evicting a more-important entry, and if the reserve cannot be restored with the entries available it is rejected and the queue is left in its relieved state. `gc.collect()` is never run while holding the queue lock (the lock guards queue mutation only).
- **Entry ceiling (sanity guard).** Independently of heap pressure, exceeding `MAX_OUTBOUND_QUEUE_ENTRIES` triggers the same priority eviction: the oldest entry in the least-important eligible class makes room, or the incoming entry is rejected if it is less important than everything queued. A valid queued entry is never dropped to admit a less-important one.
- The current Core 1 command response uses CRITICAL 10; telemetry uses TELEMETRY 40; health messages use HEALTH 70; the startup log uses INFO 50.
- An in-flight QoS 1 entry counts toward the entry ceiling and toward the retained-byte diagnostic (its payload is retained until its PUBACK) but is never evicted.
- A failed publish never discards the in-flight entry: it stays in flight and `take()` returns it again, so Core 0 retries until the broker PUBACKs (QoS 1 at-least-once delivery).
- **Post-outage drain policy stays in Core 0.** `OutboundQueue` owns storage, admission, eviction, and in-flight ownership; Core 0 alone decides *when* a queued entry may be published. The opt-in post-outage drain-rate limit (see Post-outage queue drain) gates only the `take()`/publish step in the Core 0 run loop — never the queue's own policies, which are unchanged.
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

The outbound path is defended on MCU-scale heap (Pico W: 256 KiB SRAM) by a live free-heap **reserve** — the memory-safety boundary — plus two static sanity limits. The reserve is a board-specific code constant in `hardware.py` (64 KiB Pico W, 128 KiB Pico 2 W): it is not user-configurable, not a heap percentage, and not derived from total heap or a per-message byte estimate.

- **Free-heap reserve (primary).** `intercore` reads `gc.mem_free()` at admission and, when the current free heap is at or below the reserve, runs a required `gc.collect()` and priority-evicts (oldest entry in the least-important eligible class) until the reserve is restored — or rejects the incoming entry. This bounds *retained* payload by the actual free heap at the moment of admission, at any queue depth, so a deep queue of large entries can no longer exhaust heap on its own during an MQTT outage. Because the incoming entry's payload is already resident (the caller serialized it), defending the reserve — not reserve-plus-incoming — is sufficient.
- **Per-message ceiling** — `message_serializer.MAX_OUTBOUND_MESSAGE_BYTES = 16 KiB`. Bounds a single message's transient peak (graph + str + bytes ≈ 3x the payload ≈ 48 KiB) and keeps the largest legitimate message (the one-shot startup log, the only payload that grows with device count) comfortably under the limit with margin. The queue enforces it on both admission paths: `put()` via the serialization step, and `put_with_kind()` via a direct byte-length check on the pre-serialized payload, so a caller bypassing `serialize_and_validate_message()` cannot admit a larger entry.
- **Entry ceiling (sanity guard)** — `intercore.MAX_OUTBOUND_QUEUE_ENTRIES = 64`. Bounds the number of retained entries in a degenerate case (healthy heap but relentless traffic). It is an internal fixed constant, never a user knob, and not the memory-safety boundary (the reserve is).

The shared `MemoryStats` object (owned by `InterCore`, passed to the queue so there is exactly one) is the single source of: (a) the low-watermark minimum `minimum_free_heap_observed_bytes` — the lowest free heap observed at an explicit checkpoint, recorded **before** the GC that follows it; and (b) the controlled-`gc.collect()` statistics (`gc_collect_count`, `gc_bytes_reclaimed`, `gc_total_reclaimed_bytes`, `gc_last_duration_ms`, `gc_max_duration_ms`) — a fixed set of scalars with no per-collect or per-message history. Controlled, conditional `gc.collect()` is applied at five selected boundaries (boot, pre-Core-1, the `put()` serialization path, the Core 1 read cycle, and the Core 0 `_publish_entry` wire-frame allocation) rather than on every loop: an *optional* path is gated by a one-second cooldown (`CONTROLLED_GC_MIN_INTERVAL_MS`) to leave headroom before known allocation peaks, while the *required* path (defending the reserve) bypasses the cooldown. `gc.collect()` is never run while holding the queue lock, and a `MemoryError` from a collect propagates — it is never swallowed behind `except Exception` or retried.

`OutboundQueue.status()` reports `queued_bytes` (the diagnostic retained-payload view: queued FIFO plus in-flight entry) and `max_entries` (the ceiling) for observability; it no longer reports a byte budget.

### 2. `event_queue`

Core 0 -> Core 1. Contains private discrete commands/events.

- FIFO and bounded.
- Every admitted event matters.
- Entries are never automatically published to MQTT.
- Reboot never enters this lane; Core 0 owns reboot completely.

### 3. `state_mailboxes`

Core 0 -> Core 1 latest-value state.

- `network_snapshot`: Wi-Fi status, IP, RSSI, connection counts, `network_stack_ready` flag, the passive Wi-Fi quality diagnostics (RSSI min/max/moving average + sample count, reconnect/DHCP durations, last status reason and reconnect trigger, BSSID/channel), the active reachability results (gateway/DNS reachability + latencies, broker round-trip latency), the diagnostics run count / last-run age, the MQTT reliability metrics, and the Core 0 post-outage drain metrics (`outbound_queue_drain_active` + the four `outbound_queue_last_drain_*` values)
- `utc_snapshot`: Current UTC time, ticks base, runtime start
- `hardware`: Detected hardware type, machine string, heap reserve, and `last_reset_cause` (the startup snapshot, published once by `main()` before core operation begins; immutable for the runtime)
- `core_1_activity_ms`: Timestamp of last Core 1 activity report (in milliseconds) — written by Core 1, read by Core 1 for health reporting and by Core 0 as the input to the liveness watchdog

State is replaced, not accumulated. Core 1 keeps the latest immutable snapshot until Core 0 replaces it.

### 4. `config_transaction_mailbox`

Core 0 → Core 1 request, Core 1 → Core 0 result. One lock-protected slot in
each direction; exactly one configuration transaction at a time
(see Runtime configuration management).

- A request carries a monotonic transaction id, an action (`apply`, `commit`, or `rollback`), and — for `apply` — the Core 1-owned change (classified keys + their candidate values). Plain JSON data only; no live objects cross.
- A result carries the transaction id, success/failure, and a stable error code on failure.
- No request may be posted while a previous request is still pending; a result may be posted only while a request is pending. `put_request()` returns `False` otherwise.
- Core 0 polls the result once per run-loop pass (non-blocking) with a bounded transaction timeout; it never busy-spins. Core 1 processes a request in its normal loop — no new thread, no cross-core call.

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
- no pre-reset network shutdown is performed;
- `read_config` / `write_config` are handled entirely by Core 0 (the transaction coordinator), and a configuration change never triggers a reboot — the explicit `reboot` command remains the only reboot path (see Runtime configuration management).

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

Recovery is also the boundary of the post-outage drain episode: a successful runtime re-establishment with a non-empty outbound queue *starts* a drain episode, and a new failure detected while one is active *interrupts* it (the partial episode is discarded and the last completed episode's metrics are preserved) — see Post-outage queue drain. The initial startup path never starts one.

## Post-outage queue drain (Core 0)

An MQTT outage buffers telemetry, health, and logs in the outbound queue; when the link returns, Core 0 drains the backlog at QoS 1 speed. This feature makes that drain measurable, and provides an opt-in configuration control that can smooth reconnect bursts in future deployments (more sensors, deeper outages) without changing normal behavior. The policy lives entirely in `core0.py` — no new class, queue, thread, or scheduler.

### Config

- `mqtt_post_outage_drain_rate_per_sec` (default `0`): an upper bound on outbound-queue publish *attempts per second* while a post-outage drain episode is active. Non-negative integer only (negatives, booleans, floats, strings, null fail validation); Core 0 only — Core 1 never sees it.
  - `0` (the default): **no rate limit** — the firmware drains exactly as fast as the existing QoS 1 publish path permits, exactly as before. No behavior change.
  - Positive integer `n`: minimum slot spacing of `max(1, (1000 + n - 1) // n)` ms between attempts — 1/s → 1000 ms, 2/s → 500 ms, 3/s → 334 ms, 5/s → 200 ms, 10/s → 100 ms.
  - The configured rate is an **upper bound, not guaranteed throughput**: QoS 1 waits for each PUBACK, so actual drain may be slower.
  - **Operational note:** the limit applies to the queue as a whole. If Core 1 produces messages faster than the configured rate, the queue can keep growing during the episode instead of reaching zero (and a throttled drain intentionally holds backlog in memory longer). Choose the rate above the expected message-production rate. The firmware never auto-adjusts it.

### Drain episode

An episode begins only when **both** hold:

1. a *runtime* MQTT reconnection completed successfully (inside `_recover_network_if_needed()`, after `establish_network()`), and
2. `outbound_queue.get_depth() > 0` (queued + in-flight).

It ends when the depth returns to zero after one or more successful completions. A reconnect with an empty queue creates no episode and leaves the last completed metrics untouched. The initial startup (`start()`) never starts an episode — the deterministic startup contract is unchanged.

A failure detected while an episode is active **interrupts** it: the active state is cancelled (no partial episode is ever published as "last completed"), the last completed episode's metrics are preserved, and the next successful reconnect starts a fresh episode at the then-current queue depth.

An in-flight entry retained across the failure (the ambiguous QoS 1 case) is the first entry of the new episode and consumes a drain slot like any other attempt — retries are never exempt from the configured ceiling.

### Rate limiter (non-blocking, disabled by default)

The limiter is one next-slot timestamp (`_queue_drain_next_publish_ms`), advanced with `time.ticks_add()` when an attempt *begins* (a failed attempt consumes its slot too — repeated failures cannot bypass the ceiling). The run loop simply skips the outbound-queue take/publish step until `time.ticks_diff(now, next_slot) >= 0`: no sleep, no scheduler, nothing blocked. While a slot is not yet due, every other Core 0 service stays eligible on every pass — heartbeat watchdog, pending reboot, network recovery, connection logs, command polling, command/reboot responses, PINGREQ, network diagnostics, snapshot publication, UTC handling. Only the one `take()`/publish call is gated.

Because episodes exist only after a runtime reconnect, and the limiter is inert when `0`:

- normal connected publishing is never delayed (it is not a global MQTT publish-rate limit);
- the startup sequence (probes, startup work drain, 5-second stabilization, UTC) is never delayed;
- connection logs, Core 0 command/reboot responses, UTC requests, network probes, and PINGREQ are never delayed.

### Metrics (Core 0 → shared network snapshot)

| Field | Meaning |
|---|---|
| `outbound_queue_drain_active` | `true` while an episode is in progress; `false` before the first drain, after completion, and after interruption |
| `outbound_queue_last_drain_start_depth` | Queue depth (queued + in-flight) at the start of the last completed episode |
| `outbound_queue_last_drain_message_count` | Entries completed (PUBACK'd, then `complete_in_flight`) in the last completed episode — may exceed the start depth if Core 1 kept producing |
| `outbound_queue_last_drain_duration_ms` | Monotonic span from episode start to queue empty, including time spent servicing other Core 0 work; `max(1, …)` guards the zero-duration sample |
| `outbound_queue_last_drain_rate_per_sec` | `message_count * 1000 // max(1, duration_ms)` — integer-only (no floating point) |

All are `false`/`0` before the first completed drain. All arithmetic uses `time.ticks_*` (wrap-safe). The state is a fixed set of scalars — no per-message lists, no outage history, no rate samples, no unbounded growth.

These fields are **historical/observational only**: they never add a degraded reason or change `status` (current queue pressure and connectivity remain the only sources of degraded reasons), never drive recovery, and are never a trigger for a reboot.

Core 0 copies the five values into the shared network snapshot on its existing cadence — no new mailbox, thread, or MQTT topic. Core 1 reports them unchanged in the health payload and in the system-information `queues` section (safe defaults `false`/`0` when the snapshot is unavailable).

## Network diagnostics (Core 0)

Lightweight, observational Wi-Fi quality and reachability diagnostics, owned entirely by Core 0. They report to the existing shared network snapshot — no new mailbox, no new thread, no new MQTT topic, and never a `degraded_reason`. The governing rule: **diagnostics observe the network without becoming a new source of network instability.**

### Ownership split

- **`wifi.py` (passive, always on)**: RSSI statistics, reconnect/DHCP durations, WLAN status reasons, reconnect trigger, association details. Pure bookkeeping on the connect/snapshot paths that already run; no sockets, no new traffic.
- **`core0.py` (active, staged)**: the gateway/DNS/broker probe cycle and its scheduling state machine.
- **`network_diagnostics.py` (probe helpers)**: `icmp_echo_supported()`, `probe_gateway()`, `probe_dns_server()` — bounded single-shot probes, nothing else.

### Config

- `network_diagnostics_interval_sec` (default 300): the cycle period. `0` disables active probes entirely — passive RSSI sampling continues and `network_diagnostics_run_count` stays 0. Otherwise an integer number of seconds in `[60, 86400]`; bools, floats, strings, and null fail config validation.
- `network_diagnostics_broker_latency_enabled` (default `false`): when false, the broker stage never runs. Both keys are Core 0 (network) settings — Core 1 only reads the shared snapshot.
- `NETWORK_DIAGNOSTIC_PROBE_TIMEOUT_MS = 750` (internal constant, not config): the bounded timeout on every gateway/DNS probe socket.

### Passive diagnostics (wifi.py)

- **RSSI statistics**: sampled in `snapshot()` (at most every 5 s) with a 10 s gate (`WIFI_RSSI_SAMPLE_INTERVAL_MS`). Lifetime min/max and an integer fixed-point EMA (`avg_x8 = ((avg_x8 * 7) + (rssi * 8)) // 8`, external value `avg_x8 // 8`). A valid read increments `rssi_sample_count`; a failed read leaves count/min/max untouched and never substitutes 0. Statistics are **not** reset on reconnect (reconnects only contribute new samples); a reboot is the only reset.
- **Reconnect duration**: `wifi_disconnected` (mid-run recovery) and the startup self-heal loop record the trigger via `note_reconnect_trigger()`; only the two firmware-known values (`wifi_disconnected`, `network_probe_failure`) are ever stored. The timer starts on the first attempt of a disconnect sequence (once the device has connected at least once) and spans the **entire** failed-attempt/backoff sequence to a successful connection — it is not restarted per attempt. The initial boot connect is never a reconnect (`0` until the first one completes).
- **DHCP/IP acquisition duration**: the span from `wlan.connect()` to link-ready (association + auth + DHCP), for every successful attempt. Readiness is `isconnected()` plus, if feature-detected once per instance, `ipconfig("has_dhcp4")`. This is **not** an isolated DHCP DORA exchange — that is not what this port authoritatively exposes.
- **WLAN status → stable string**: a per-instance mapping from the WLAN `STAT_*` constants to `idle` / `connecting` / `wrong_password` / `no_ap_found` / `connect_fail` / `got_ip`; an unmapped code reports `unknown`. Never a raw 802.11 reason code.
- **Association (BSSID/channel)**: after each successful connect, direct STA queries (`wlan.config("channel")`, `wlan.config("bssid")`) in try/except, capability cached. A valid channel is an int 1–14; a valid BSSID is 6 binary bytes (formatted `aa:bb:cc:dd:ee:ff`) or an already-formatted string. Refreshed after every successful connection. When unavailable: `association_details_supported=False`, `bssid=None`, `channel=None`. **`wlan.scan()` is never called.**
- `MemoryError` propagates on every path; ordinary failures degrade to no sample / last-good-or-None.

### Staged active probes (core0.py)

A cycle is `gateway → dns → optional broker`, advancing **at most one stage per run-loop pass** (`_service_network_diagnostics()`), so probes are never all three back-to-back:

1. **IDLE**: the first pass arms the deadline (the first cycle starts one full interval after Core 0 is running, never at startup). When due and the network is stable, run the **gateway stage** and advance to DNS.
2. **DNS**: run the **DNS stage**, then advance to the broker stage (if enabled) or complete the cycle.
3. **BROKER**: additionally require the QoS 1 path to be fully idle (no in-flight entry, queue empty, no pending Core 0 responses/connection logs/reboot) — busy means **skip the broker stage and complete the cycle** (recorded without a latency); real traffic is never delayed.

Stability preconditions for every stage: `network_stack_ready`, Wi-Fi connected with a valid IP, MQTT connected. A loss of stability mid-cycle **discards the partial cycle without counting it** (retry next interval). A completed cycle is counted exactly once: `network_diagnostics_run_count += 1`, `network_diagnostics_last_run_age_ms` = `ticks_diff(now, last_completed)`, next deadline = `now + interval`. All arithmetic is `time.ticks_*` (wrap-safe). An unexpected ordinary failure in the run loop discards the partial cycle; a `MemoryError` propagates.

**Gateway stage**: `icmp_echo_supported()` feature-detects raw-socket capability once (expected `False` on RP2/cyw43). Unsupported → `gateway_reachability_supported=False`, `gateway_reachable=None`, `gateway_last_latency_ms=None` — the probe is **never faked with a UDP send**. Supported: one 24-byte IPv4 Echo Request (type 8/code 0, fixed identifier, one's-complement checksum, 16-byte payload) to the interface's own gateway (`wifi.gateway_address()`); success = a reply with type 0/code 0 and the matching identifier; latency = tick-diff.

**DNS stage**: one fixed 36-byte UDP query (question `diagnostic.invalid.`, A/IN) to the interface's own DNS server port 53 (`wifi.dns_address()`) — never a public resolver, never `socket.getaddrinfo()`. Reachable when ≥3 bytes arrive with the matching transaction id and the QR bit set — **any RCODE counts** (NXDOMAIN/SERVFAIL still prove the server answered). Mismatched id or timeout → not reachable.

**Broker stage**: reuses the **existing** QoS 1 network-probe machinery — same client, same `mqtt_topic_network_probe`, same bounded PUBACK exchange, same failure → `mark_disconnected()` → existing recovery policy. There is no second recovery policy and no new probe topic. Latency recorded only on success.

### Semantics invariants

- **Null vs false**: a `null` reachability field means not-tested / unsupported / no such address on the interface — it is never converted to `false`. `true`/`false` appear only after a probe actually ran. `gateway_reachability_supported` is a bool (`false` until the capability is detected true).
- All results land in the existing network snapshot on its existing cadence (`network_snapshot_interval_sec`) — no new publish path.
- Diagnostics never drive recovery, never change health classification, and never add a thread or topic.

### System information (Core 1)

`SystemInformation.get_network()` reads the snapshot null-tolerantly and adds the network-quality fields; in that section the section-local names are canonical: the current RSSI is reported as `wifi_rssi_dbm` (snapshot key `rssi`) and the DNS server as `dns_server` (snapshot key `dns`). `get_communications()` carries the history fields (`wifi_last_reconnect_duration_ms`, `wifi_last_dhcp_acquisition_duration_ms`, `wifi_last_status_reason`, `wifi_last_reconnect_trigger`, `network_diagnostics_run_count`, `mqtt_broker_latency_enabled`) with safe defaults (integer zero, `"unknown"`, `false`) when the snapshot is older/incomplete.

## Core 0 run loop

Each Core 0 iteration (10 ms period) services, in order:

1. A pending reboot (publish the response, wait, `machine.reset()`).
2. Network recovery via `_recover_network_if_needed` — this runs even before any publishing, so a lost link is detected and repaired at the top of the loop.
3. Pending connection logs (when MQTT is connected).
4. The MQTT receive pump (`check_msg`) at the configured `mqtt_command_poll_ms` cadence — this is how UTC responses and commands arrive without blocking.
5. Config transaction polling (while a `write_config` transaction is awaiting its Core 1 result): take the mailbox result once, non-blocking. On a Core 1 success the remaining Core 0 activation, the file commit, and the response proceed in this pass; on a Core 1 failure or the bounded transaction timeout the rollback path is finalized. Every other step stays eligible while a transaction awaits Core 1 — Core 0 keeps servicing the network (see Runtime configuration management).
6. Pending Core 0 command responses (when MQTT is connected and no entry is in flight).
7. One outbound queue entry, published with QoS 1; a failed publish leaves the entry in flight for retry, and a successful one calls `complete_in_flight`. During an active post-outage drain episode with `mqtt_post_outage_drain_rate_per_sec > 0`, this step is the *only* step gated: if the next drain slot is not yet due the take/publish is skipped (non-blocking — every other step above and below stays eligible), and a slot is consumed when the attempt begins.
8. A PINGREQ when keepalive traffic is due — only when no outbound entry is being published (a PUBLISH itself resets the broker timer).
9. Network diagnostics: when the configured interval is due and the network is stable, advance at most one bounded probe stage (gateway → DNS → optional broker) — see Network diagnostics. An unexpected ordinary failure discards the partial cycle; a `MemoryError` propagates.
10. The network snapshot publish, rate-limited to `network_snapshot_interval_sec` — the diagnostics results from this pass land in this same snapshot.
11. UTC housekeeping: discard a pending request whose deadline passed, then send a new request if the sync is due and the retry throttle allows.

## Core 1 baseline

The current device framework is retained:

- `DeviceManager`
- `Device` interface
- `SystemInformationDevice`
- initialization retry behavior
- read-failure/reinitialization behavior
- reinitialization failure logging: the first reinit failure for a device is warned once; repeats while the same device stays in pending-reinit are suppressed until a successful reinit clears the flag, so a stuck device warns once instead of every cycle (a later independent failure warns again)
- configuration transaction handling: applying, committing, and rolling back Core 1-owned configuration changes inside the normal loop, via the config transaction mailbox (see Runtime configuration management)

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

Core 1's first action, before the first telemetry, is a one-time `runtime_started` log event (event `runtime_started`, reason_code `none`, level `INFO`). It is queued under KIND_LOG at INFO (50) retention priority; Core 0 maps the kind to `mqtt_topic_log` at publish time.

The payload carries:

- `last_reset_cause` and `boot_reason` in `data` — startup context directly relevant to the event (identity fields such as `firmware_version`/`firmware_build_commit` stay in the common envelope, never duplicated in `data`).
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

All periodic Core 1 runtime work — telemetry and health — is scheduled from one shared epoch, `normal_runtime_start_ticks_ms`, captured exactly once, immediately after `runtime_started` has been successfully admitted to the outbound queue. No periodic work begins before that admission.

- `boot_ticks_ms` remains the boot-lifetime reference: firmware uptime (`uptime_ms`), startup duration, and lifecycle diagnostics are all measured from boot. It is no longer the scheduling origin for periodic work.
- `normal_runtime_start_ticks_ms` anchors periodic operational work: telemetry boundaries at `anchor + n × read_loop_sec`, health boundaries at `anchor + n × health_interval_sec`. With a 13-second startup, `read_loop_sec = 20`, and `health_interval_sec = 60`, telemetry falls at 33/53/73/93... seconds of uptime and health at 73/133/193/253... seconds of uptime.
- The two schedulers share the epoch but stay independent: each keeps its own deadline, neither derives its deadline from the other, and neither may fire the other. When both are due (health interval is a multiple of the read loop), both process normally through the existing outbound queue at their existing priorities (TELEMETRY = 40, HEALTH = 70).
- The anchor is captured once per runtime. A Wi-Fi reconnect, MQTT reconnect, UTC resynchronization, device reinitialization, or queue drain must never re-capture it. Only a true reboot — a new `runtime_id` and new `boot_ticks_ms` — creates a new anchor.
- Deadlines advance from the previous scheduled deadline (deadline + interval), never from the moment a message was actually generated, so per-iteration processing delay cannot accumulate into drift.
- A runtime DYNAMIC change of `read_loop_sec` / `health_interval_sec` (via `write_config`) never re-captures the anchor and never replays boundaries: at the moment the change becomes active the affected scheduler's next deadline is rebased from *now* with the new interval (prospective), and the skip-never-replay policy continues from there — a shorter interval must not produce a catch-up burst.

## Uptime accounting

Every message envelope carries `uptime_ms`, and both cores compute it from `uptime.py`: each core seeds a small state from `boot_ticks_ms` and advances it by the delta between consecutive recent samples (`create_uptime_state` / `current_uptime_ms`). No code diffs the original boot tick against the current tick in one step — `time.ticks_diff()` is only guaranteed correct within half a tick period, so the one-shot form wraps on a long-running device while the accumulated form stays monotonic and correct across a wrap.

## Periodic Telemetry

Telemetry cadence is anchored to the normal-runtime anchor: `read_loop_sec` defines fixed boundaries at `anchor + n × read_loop_sec` (a 20-second read loop produces boundaries at +20s, +40s, +60s relative to normal-runtime start). Telemetry remains historical sensor/runtime data: during an MQTT outage it may continue to enter the bounded outbound queue under the existing retention/eviction rules. Telemetry generation is gated on startup-log admission — no telemetry before `runtime_started` is admitted.

- **Missed boundaries are skipped, never replayed** (same policy as health): if a device read stalls long enough to cross one or more boundaries, the scheduler performs the current sample once and advances directly to the next future boundary — it does not fire catch-up reads for the elapsed boundaries. Telemetry is a current sample, not a replayable record, so a stall cannot reconstruct the missed samples; a catch-up burst would only add near-duplicate samples, JSON work, and queue admissions immediately after an overload. This is distinct from outage buffering (above), which still applies: an MQTT outage queues telemetry rather than dropping it.

## Periodic Health Messages

Core 1 generates health messages on a normal-runtime-anchored cadence: `health_interval_sec` defines fixed boundaries counted from `normal_runtime_start_ticks_ms` (a 60-second interval produces boundaries at +60s, +120s, +180s, +240s relative to normal-runtime start), independent of when startup merely completed and independent of the telemetry scheduler. No immediate health message is generated after `runtime_started`. The health message contains current-state diagnostic fields without turning the payload into a full system information report.

### Scheduling

- **Normal-runtime-anchored**: the first deadline is `normal_runtime_start_ticks_ms + health_interval_sec`, where the anchor is captured once, immediately after successful `runtime_started` admission.
- **Missed boundaries are skipped, never replayed**: boundaries that elapsed during any bounded delay (e.g. a stalled loop) are not emitted as catch-up reports; the scheduler advances directly to the next future boundary.
- **No cumulative drift**: after a boundary the deadline advances from the previous deadline (deadline + interval), not from the moment the message was actually generated, so per-iteration processing delay cannot accumulate.
- **At most one message per boundary**: when a boundary is due, at most one current-state health report is emitted (subject to the generation rules below), then the deadline advances past any elapsed boundaries.

### Generation Rules

1. **Network ready required**: Health messages are only generated when `network_stack_ready = True` and `mqtt_connected = True`. This prevents accumulation during MQTT outages.

2. **Authoritative data sources**:
   - Hardware: `StateMailboxes.get_hardware()` (canonical detected state)
   - RSSI: `StateMailboxes.get_network_snapshot().get("rssi")`
   - Core 1 activity: `StateMailboxes.get_core_1_activity_ms()`
   - Heap: `gc.mem_free()` (current measurement), submitted to the shared `MemoryStats` whose low-watermark minimum feeds `minimum_free_heap_observed_bytes`
   - Devices: `SystemInformation.get_devices()` (backed by `DeviceManager.get_status_snapshot()`)
   - Queue: `OutboundQueue.get_health_metrics()` (entry-ceiling and retained-byte diagnostic views)
   - UTC: `StateMailboxes.get_utc_snapshot()`

3. **Monotonic time calculations**:
   - All age calculations use `time.ticks_diff()` for monotonic elapsed time
   - Uptime since boot is accumulated from deltas between recent samples (`uptime.py`), never as a single `ticks_diff(now, boot)` — that one-shot form is only guaranteed within half a tick period and wraps on long-running devices
   - UTC sync age: integer division of milliseconds by 1000

4. **Queue pressure threshold**: entry utilization >= 75% — `outbound_queue_utilization_percent = (depth * 100) // entry_ceiling >= 75`. The retained-byte view is a diagnostic, not a budget, so it no longer drives the trigger. Heap pressure is defended at admission (the reserve) and surfaced as a separate current-state condition via `low_free_heap` when the *current* free heap is below the reserve.

5. **Low-priority retention**: Uses `RETENTION_PRIORITY_HEALTH = 70`, the lowest priority class

### Degradation Triggers

Health status is "degraded" when any of these conditions are true:
- `network_stack_not_ready`: Core 0 network not initialized
- `wifi_not_connected`: Wi-Fi disconnected
- `mqtt_not_connected`: MQTT broker connection lost
- `core_1_inactive`: Activity age exceeds threshold (3x read_loop_sec, minimum 60 seconds)
- `low_free_heap`: Free heap below the board reserve (the only heap-based trigger; the low-watermark minimum never adds a reason)
- `device_count_mismatch`: Active devices don't match configured count
- `outbound_queue_pressure`: Entry utilization (depth / entry ceiling) >= 75%
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

## Runtime configuration management

Configuration is transactional at runtime: a `write_config` command either becomes fully active **and** persisted, or it leaves the runtime and `config.json` exactly where they were. There is no partial application, no automatic reboot, no background configuration thread, and no deep merge.

### Commands (Core 0)

- **`read_config`** — payload `{}` (anything else is `command_invalid_payload`). Returns the committed, non-secret configuration: the entire `config.json` document, including `config_schema_version` and `config_generation`, plus `config_checksum_sha256`, `reboot_required`, and `pending_restart_keys`. It never returns Wi-Fi credentials: those live in `config-secrets.json`, which is a separate file that never enters `config.json`.
- **`write_config`** — a partial top-level patch. Top-level properties are replaced as complete values (a list or object in the patch replaces the whole property). The entire transaction is rejected, atomically, when any key is unknown (`invalid_config_key`), read-only (`read_only_config_key`), or invalid (`invalid_config_value`); a merged candidate that fails cross-field validation is rejected with `invalid_config_combination`. An empty patch is rejected as an invalid command payload.

### Change-policy registry

`config.py` holds the single authoritative mapping — `CONFIG_CHANGE_POLICIES` — from every valid top-level key to exactly one policy. The required/allowed key set is derived from it (there is no second list).

| Policy | Keys | Activation |
|---|---|---|
| `DYNAMIC` | `source`, `read_loop_sec`, `health_interval_sec`, `device_initialization_attempts`, `device_initialization_retry_delay_ms`, `device_read_failure_threshold`, `mqtt_command_poll_ms`, `datetime_sync_interval_min`, `network_snapshot_interval_sec`, `network_probe_timeout_sec`, `mqtt_topic_telemetry`, `mqtt_topic_log`, `mqtt_topic_command_response`, `mqtt_topic_info_request`, `mqtt_topic_network_probe`, `mqtt_topic_health`, `wifi_reconnect_delays_sec`, `mqtt_reconnect_delays_sec`, `mqtt_post_outage_drain_rate_per_sec`, `network_diagnostics_interval_sec`, `network_diagnostics_broker_latency_enabled` | active once the transaction commits; a scheduler-interval change rebases the next deadline from now with the new interval (prospective, no catch-up burst — see Normal-runtime scheduling anchor) |
| `RECONFIGURE` | Core 1: `devices`. Core 0 (one MQTT operation): `mqtt_broker_ip_address`, `mqtt_keepalive_sec`, `mqtt_broker_response_timeout_sec`, `mqtt_topic_command`, `mqtt_topic_info_response` | the owning subsystem is deliberately reconfigured and the reconfiguration must prove operational; on failure the previous known-good state is restored and the transaction fails |
| `RESTART_REQUIRED` | `max_intercore_event_entries` | persisted only; `reboot_required` is set and `pending_restart_keys` lists it. The firmware **never** reboots itself because of a configuration change — a separate explicit `reboot` command controls reboot behavior |
| `READ_ONLY` | `config_schema_version`, `config_generation` | any patch entry is rejected (`read_only_config_key`) |

### Generation, checksum, config state

- **`config_generation`** — a firmware-managed non-negative integer stored in `config.json`: incremented exactly once per successful changed transaction; no-op and failed transactions do not advance it.
- **`config_checksum_sha256`** — SHA-256 (`hashlib.sha256` + `binascii.hexlify`) over the exact committed `config.json` bytes. It never covers `config-secrets.json`, and `read_config` never returns secrets.
- **`ConfigState`** (in `config.py`, attached to `InterCore`) — the one small lock-protected authoritative view both cores may read: committed snapshot, generation, checksum, `reboot_required`, `pending_restart_keys`. It owns no Wi-Fi/MQTT/device state. At boot `reboot_required` is `false`; it is set by a committed RESTART_REQUIRED change and cleared when all pending restart keys have reverted to their *active boot values* (so those boot-time active values are retained for comparison).

### Transaction lifecycle

Exactly one transaction at a time; a concurrent `write_config` receives `configuration_update_in_progress`. The steps run in order, and a failure at any step rolls back everything already done, discards the staged file, and leaves runtime + `config.json` at the previous state:

1. Validate the patch; build the candidate (committed config + patch); run the **same** `validate_config()` the boot path uses on the merged candidate — startup and runtime writes share one validator.
2. Classify the changed keys by policy; detect a no-op (patch equals the committed config ⇒ success with `changed_keys: []`, generation unchanged — a redelivered already-committed command is therefore idempotent and cannot double-increment the generation).
3. Assign `generation + 1`.
4. **Stage** the candidate to `config.json.tmp`: serialize, write, `flush`, `os.sync()` when supported, read back, full validation, checksum comparison. The staged file is never auto-promoted.
5. If Core 1-owned keys are present, post an `apply` request to the config transaction mailbox (lane 4) and await the result — Core 0 keeps servicing the network loop, polling once per pass with a bounded transaction timeout (no busy spin).
6. Core 0 DYNAMIC activation: its config view and the derived scalars are refreshed (network-diagnostics interval/enablement, drain rate, poll cadence, Wi-Fi reconnect delays).
7. If MQTT RECONFIGURE keys are present, **one** MQTT operation: capture the previous connection config, dispose the old session with the existing bounded cleanup policy (direct socket close, no DISCONNECT write), apply the candidate, then `connect()` + subscribe under the existing bounded handshake. On failure the previous config is restored and the old session re-established and verified; if that restore fails the transaction reports `configuration_rollback_failed`.
8. **Commit**: rename `config.json` → `config.json.bak`, rename the staged file → `config.json`, read/validate/checksum the new primary, delete the backup. A crash at any point leaves a valid primary or a valid backup — never a partial JSON as the active file (the active file is never the file currently being constructed).
9. Update `ConfigState` (new generation, checksum, `reboot_required`, `pending_restart_keys`), send the `commit` instruction to Core 1 (discarding its rollback snapshot), emit `configuration_update_completed`, and publish the success response.

If the Core 1 apply fails (or the bounded timeout elapses), Core 0 issues the `rollback` instruction before finalizing the failure; if a Core 0 activation step fails, the previous Core 0 values are restored and Core 1 is rolled back.

The response is published through the existing pending-response machinery: a response-delivery failure never undoes a committed configuration — the response stays pending and is retried with its stamped wire identity (QoS 1, byte-identical retransmit).

### Core 1 side (apply / commit / rollback)

Core 1 processes the mailbox request inside its normal loop (no new thread, no cross-core call):

- **DYNAMIC**: update its config view and the `DeviceManager` scalars; `read_loop_sec` / `health_interval_sec` rebase their next deadline from *now* with the new interval (the skip-never-replay policy continues — no catch-up burst).
- **`devices`**: unchanged instances are retained; candidates for new or changed definitions are built and initialized **while the old active set keeps serving** (a controlled-GC boundary precedes the candidate builds). Any candidate failure discards the candidates and keeps the old set — stricter than boot, where a failed init is survivable; at runtime a failed candidate init is a transaction failure. Success atomically swaps the active device set. The rollback snapshot (previous device set + the dynamic values) is retained until the `commit` or `rollback` instruction.
- **`commit`** discards the rollback snapshot; **`rollback`** restores the previous device set and dynamic values (and rebases the schedulers with the previous intervals), then reports the result.

### Persistence and boot recovery

- `config.json` is staged **before** runtime activation and committed **after** it. After an interrupted transaction the last known-good primary/backup wins; a stale `config.json.tmp` is never promoted automatically.
- At boot, before Core 0 starts (`main.py`): a valid `config.json` wins (stale `.tmp`/`.bak` cleaned); otherwise a valid `config.json.bak` is renamed to primary and one `configuration_recovered` event (reason `configuration_primary_invalid`) is emitted after the network is up; if **neither** file is valid, startup fails clearly — no default configuration is silently invented.
- `config_schema_version` in `config.json` must equal `CONFIG_SCHEMA_VERSION` (`version.py`) — fail-fast, as at every boot.

### System information and capabilities

The `configuration` system-information section (and `include: ["configuration"]` on the system-information device) carries compact fields only: `config_schema_version`, `config_generation`, `config_checksum_sha256`, `reboot_required`, `pending_restart_keys` — never the full config. The checksum is **not** added to health messages. `capabilities.features` gains `runtime_configuration`.

### Errors, events, logging

Rejection codes: `invalid_config_key`, `read_only_config_key`, `invalid_config_value`, `invalid_config_combination` (atomic — nothing applied, generation unchanged). Transaction codes: `configuration_update_in_progress`, `configuration_stage_failed`, `configuration_reconfigure_failed`, `configuration_persistence_failed`, `configuration_rollback_failed`. Events: `configuration_update_started` / `configuration_update_completed` / `configuration_update_failed`, `configuration_rollback_completed` / `configuration_rollback_failed`, `configuration_recovered`. Log `data` carries changed key names/counts, policy classes, generation, and duration — never full patch values and never secrets. `MemoryError` always propagates on these paths.

### Not included

Automatic reboot on `RESTART_REQUIRED`; runtime secret editing (Wi-Fi credentials are unchanged by `write_config` and absent from `read_config`); deep merge; concurrent transactions; generation history; the checksum in health or periodic system-information payloads beyond the compact `configuration` section; YAML/TOML; remote or arbitrary file editing.

## Features intentionally not carried into the baseline

These should be added individually only after the baseline passes:

- advanced reboot-delivery state machines;
- advanced network recovery generations/state machines;
- advanced watchdog/cross-core recovery (the minimal Core 0 stale-heartbeat reset is present since 0.4.9);
- additional Core 1 commands;
- physical sensors.

Runtime configuration (`read_config` / `write_config`, 0.5.0) is now part of
the baseline — see Runtime configuration management.

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
