# CLAUDE.md — Project Guidelines for AI Assistants

## Project Overview

Dual-core MicroPython firmware for Raspberry Pi Pico W / Pico 2 W.

- **Core 0 (network core)** owns the network stack: Wi-Fi, MQTT (QoS 1), UTC sync, reboot, keepalive PINGREQ, mid-run network recovery, Core 0 diagnostics, the post-outage drain, and the Core 1 liveness watchdog.
- **Core 1 (sensor core)** owns devices/sensors: device lifecycle, sensor reads, telemetry, health messages, and the liveness heartbeat.
- Cores communicate over a **four-lane inter-core bus** (`intercore.py`): a pre-serialized outbound queue (Core 1 → Core 0), an event queue (Core 0 → Core 1), state mailboxes, and a lock-protected config transaction mailbox (Core 0 → Core 1 request / result) used only by the `write_config` transaction.

Version pins live in `version.py`: `FIRMWARE_VERSION`, `CONFIG_SCHEMA_VERSION`, `MESSAGE_SCHEMA_VERSION`.

### Document map

| Document | Role |
|----------|------|
| `ARCHITECTURE.md` | **Authoritative spec** — health protocol (full field reference), the inter-core lanes, ownership invariants, Core 0/Core 1 baselines, startup sequence, UTC, diagnostics, drain, runtime configuration management. Read the relevant section *before* changing that subsystem, and update it *before* coding behavior changes. |
| `CHANGELOG.md` | Full release notes, including each version's "Not included" scope boundaries — check those before proposing a feature. |
| `README.md` | User-facing: quick start, configuration, deployment. |
| `tests/` | Host-side unit tests; the coverage list below doubles as the behavioral contract. |

## Non-Negotiable Invariants

These are the failure classes this firmware has already paid for. Every change must preserve them:

1. **Strict ownership.** Core 0 = network; Core 1 = devices. No overlap. Core 1 never knows MQTT topic names — only message kinds. Core 0 never touches device drivers.
2. **Cross-core contract.** Only plain JSON-serializable data crosses cores; objects are immutable after transfer. Senders carry their *complete* message (including `uptime_ms`, `timestamp`) pre-serialized and must not carry envelope keys at top level; Core 0 splices in `sequence`, `runtime_id`, `source`, `firmware_version`, `message_schema_version` before the closing brace at publish time — the payload is **never decoded, parsed, or re-serialized** on the publish path.
3. **Fail-fast configuration.** All config is validated before the network starts; unknown top-level keys are rejected; `config_schema_version` in `config.json` must equal `CONFIG_SCHEMA_VERSION` in `version.py` (bump both together).
4. **Bounded MQTT waits.** Every blocking MQTT wait — CONNACK/SUBACK handshake, PUBLISH frame writes, PUBACK, PINGRESP — is deadline-limited (`mqtt_broker_response_timeout_sec`). Disposal of a failed client closes the TCP socket **directly, with no DISCONNECT write** into the dead link. A blackholed link must surface as a bounded failure into the existing recovery path, and must **never wedge Core 0** — a wedged Core 0 can't even run its own Core 1 watchdog.
5. **Bounded inbound packets.** A packet's remaining length must be within MQTT's four bytes and ≤ `MAX_INBOUND_PACKET_BYTES` (16 KiB) *before* its payload is allocated; an oversized or malformed frame drops the connection and lets recovery reconnect.
6. **QoS 1, one in flight.** Synchronous PUBACK. The wire `sequence` is claimed *before* the first transmission attempt and stamped on the logical object (queue entry, pending command response, or pending reboot); it is never rolled back. A retry of the *same* logical message reuses the stamped identity **and** re-publishes the frozen serialized bytes verbatim (byte-identical frames); a *different* message always gets a fresh number.
7. **Memory-safety boundary = the board free-heap reserve** (code constants in `hardware.py`: 64 KiB Pico W / 128 KiB Pico 2 W — not config). The shared lock-protected `MemoryStats` (in `intercore.py`, exactly one tracker) defends it at explicit checkpoints and owns the low-watermark minimum plus controlled-GC statistics. Admission defends the reserve at **any** queue depth: required `gc.collect()` (bypasses the 1-second optional-cooldown gate) → evict the oldest entry of the least-important priority class the incoming entry is at least as important as → collect again → repeat until restored; if it cannot be restored, **reject** the incoming entry and leave the queue relieved. A valid queued entry is never evicted to admit a less-important one; the in-flight entry is never an eviction candidate. `gc.collect()` is **never** run while holding the queue lock.
8. **`MemoryError` always propagates.** Heap exhaustion is fatal, not a failed optional diagnostic. Every allocation-heavy path re-raises `MemoryError` *before* any `except Exception`; nothing swallows it, retries it, converts it to `{"error": ...}` data, or runs on top of an exhausted heap.
9. **Diagnostics never change health.** Every historical/observational field — RSSI min/max/EMA, reconnect/DHCP durations, `gateway_reachable`/`dns_reachable`, broker latency, `wifi_bssid`/`wifi_channel`, MQTT reliability counters, drain metrics, `minimum_free_heap_observed_bytes`, `last_reset_cause` — is additive information only. None adds a degraded reason, changes `status`, or triggers a reboot. `null` means not-yet-tested or unsupported and is **never converted to `false`**. Current-state rules (Wi-Fi, MQTT, `free_heap < reserve`, Core 1 liveness, devices) are the only source of degraded reasons.
10. **One scheduling anchor.** `normal_runtime_start_ticks_ms` is captured exactly once, immediately after `system_startup_completed` is admitted. Telemetry (`anchor + n × read_loop_sec`) and health (`anchor + n × health_interval_sec`) deadlines are fixed boundaries from that anchor; missed boundaries (stall, outage, slow read) are **skipped, never replayed**. Reconnects, UTC resyncs, and device reinitialization never reset the anchor; only a reboot creates a new one.

## Critical Files

| File | Purpose |
|------|---------|
| `main.py` | Entry point; reads reset cause once, publishes the startup hardware snapshot, orchestrates startup |
| `core0.py` | Network stack: Wi-Fi, MQTT, UTC, reboot, keepalive, recovery, Core 1 watchdog, diagnostics scheduling, drain episodes, reliability-metric reporting |
| `core1.py` | Device lifecycle, sensor reads, telemetry, health messages, liveness heartbeat |
| `intercore.py` | Four-lane bus: `OutboundQueue` (admission, eviction, in-flight entry), `MemoryStats`, event queue, state mailboxes, `ConfigTransactionMailbox` (one config transaction at a time) |
| `device_manager.py` | Device lifecycle: init retries, read-failure threshold, reinit |
| `device_factory.py` | Device construction from config (`create_device()`) |
| `devices/device.py` | The `Device` interface (`initialize(config)`, `read()`) |
| `config.py` | Config loading, fail-fast validation, the single change-policy registry (`CONFIG_CHANGE_POLICIES`), staged/commit/boot-recovery persistence, `ConfigState`, `config_generation` + checksum |
| `hardware.py` | Hardware detection (Pico W / Pico 2 W), free-heap reserve constants, reset-cause translation |
| `system_information.py` | System state snapshots (built-in sensor data source; `get_queues()`/`get_memory()`/`get_communications()`) |
| `uptime.py` | Tick-wrap-safe boot-relative uptime (both cores) |
| `message_serializer.py` | JSON-safe validation and serialization; `MAX_OUTBOUND_MESSAGE_BYTES` |
| `message_protocol.py` | Inter-core message-protocol helpers |
| `mqtt.py` | Core 0 MQTT lifecycle: QoS 1, keepalive, subscriptions, reliability metrics (single source of truth) |
| `mqtt_client.py` | Low-level MQTT wire protocol: bounded reads/writes, inbound size guard, `MQTTPubackTimeout` |
| `wifi.py` | Wi-Fi connection management + passive quality diagnostics (RSSI stats, reconnect/DHCP durations, status strings, association details) |
| `network_diagnostics.py` | Bounded single-shot reachability probes (gateway ICMP echo, DNS query) |
| `led_manager.py` | Core 0 onboard LED state machine |
| `version.py` | `FIRMWARE_VERSION` / `CONFIG_SCHEMA_VERSION` / `MESSAGE_SCHEMA_VERSION` |
| `release.py` | Builds the deployable release artifact into `releases/` |

## Code Style

- 4-space indentation, no tabs; `snake_case` functions/variables, `PascalCase` classes
- All modules have a module-level docstring and copyright header
- Import order: standard library, then local modules
- All time math via `time.ticks_*` (monotonic, tick-wrap-safe); **integer-only** arithmetic in rate/EMA/utilization math (no floats)
- Bounded allocations; reuse dictionaries; avoid large string concatenation (256 KiB RAM total)
- `MemoryError` propagates — never place an `except Exception` over an allocation-heavy path

## Health Messages

Core 1 publishes to `iot/v3/health` at fixed boundaries from the normal-runtime anchor (invariant 10), gated during outages. The **full field reference is in `ARCHITECTURE.md`** (Status, Hardware, Network, Memory, Core Activity, Device, Queue, Post-Outage Drain, MQTT Reliability, Network Diagnostics, and UTC groups) — keep it in lockstep with `_build_health_payload()`.

Classification rules: `status` is `"healthy"` | `"degraded"`; `degraded_reasons` carries the current-state reasons; historical/observational fields are additive and never affect classification (invariant 9); `last_reset_cause` is historical information only.

## Common Tasks

### Adding a new device driver

1. Create `devices/<name>/`: `__init__.py` plus a driver module implementing the `Device` interface (`initialize(config)`, `read()`)
2. Register the `device_type` in `create_device()` in `device_factory.py`
3. Add the package to `REQUIRED_PACKAGES` in `release.py`
4. Register in `config.json` with a unique `id`
5. Add tests in `tests/` covering initialization, read, and reinitialization behavior

### Adding a health message field

1. Update `_build_health_payload()` in `core1.py`
2. Update `_build_health_payload_test()` in `tests/test_health.py` and add tests for the new field
3. Update the health section of `ARCHITECTURE.md`
4. If the field is historical/observational, say so explicitly (invariant 9)

### Modifying the inter-core protocol

1. Update the lane documentation in `ARCHITECTURE.md`
2. Update `intercore.py` and the `message_protocol.py` helpers
3. Test that **both** cores handle the change

### Changing MQTT topics

Core 0 owns topics (the `mqtt_topic_*` keys in `config.json`); Core 1 stays topic-agnostic.

### Releasing a version

1. Check `CHANGELOG.md` for the "Not included" boundaries that constrain the change
2. User-facing change → bump `FIRMWARE_VERSION` in `version.py`; config-surface change → bump `CONFIG_SCHEMA_VERSION` **and** `config_schema_version` in `config.json` together
3. Add the `CHANGELOG.md` entry: behavior change, wire/config impact, and what was *not* included
4. `python -m pytest tests/` green, then the `/git-commit` workflow

## Memory and GC

- Pico W: 256 KiB RAM; Pico 2 W: 264 KiB RAM, faster CPU
- `low_free_heap` (`free_heap < reserve`) is the **only** heap-based degraded reason; the low-watermark minimum never degrades health
- Controlled `gc.collect()` sits at five selected boundaries (boot, pre-Core-1, the `put()` serialization path, the Core 1 read cycle, the Core 0 `_publish_entry` wire-frame allocation) — never on every loop
- The optional path is gated by a 1-second cooldown (`CONTROLLED_GC_MIN_INTERVAL_MS`); the required path (reserve defense) bypasses it
- Never GC while holding the queue lock; never GC to make reported numbers look cleaner; never retry a `MemoryError`

## Scheduling and Timing

- **Anchor:** invariant 10 — one shared epoch, missed boundaries skipped, never replayed.
- **UTC at startup:** mandatory and self-healing — one pass of up to 3 bounded attempts; a failed pass re-establishes the network and retries the whole pass; Core 1 never starts until a clean pass succeeds; a `MemoryError` still fails fast.
- **UTC steady-state:** non-blocking — send the request, keep a `mqtt_broker_response_timeout_sec` deadline, never block on the answer; re-requests throttled to 30 s; a malformed-but-reachable answer re-keys the throttle to ~0.5 s.
- **Core 1 liveness:** Core 1 refreshes `core_1_activity_ms` on a ~5 s cadence. The Core 0 run loop checks it **first** on every pass: a no-op before the first stamp (Core 1 not started), and at or beyond the 30 s stale timeout it logs FATAL and calls `machine.reset()`.
- **Recovery:** `_recover_network_if_needed()` re-runs `establish_network()` — startup and recovery share one code path.

## Core 0 Diagnostics (observational only)

- **Passive side (`wifi.py`, always on, no sockets):** 10 s-gated RSSI min/max and an integer fixed-point EMA — valid reads only, a failed read never counts and never substitutes zero; statistics are lifetime and survive reconnects (reboot is the only reset). Reconnect duration spans the full retry/backoff sequence (a timer started once per disconnect sequence; the **initial boot connect is never a reconnect**). Connect-to-IP-ready acquisition duration (association + auth + DHCP — not an isolated DORA exchange). Stable `STAT_*` string mapping (never raw 802.11 reason codes). BSSID/channel via direct STA queries only — **`wlan.scan()` is never called**; when a value is not authoritatively available the field is null.
- **Active side (`network_diagnostics.py` + a staged Core 0 scheduler):** IDLE → GATEWAY → DNS → optional BROKER, at most **one** bounded stage per run-loop pass; first cycle one full interval after Core 0 is running (never at startup); only while the network is stable (startup complete, Wi-Fi up with a valid IP, MQTT connected); a mid-cycle loss of stability discards the partial cycle uncounted; a completed cycle is counted exactly once.
  - **Gateway:** feature-detected ICMP echo (expected unsupported on RP2/cyw43 → `supported=false` with null reachability); **never faked with a UDP send**; one 24-byte request with checksum and identifier validation.
  - **DNS:** one fixed 36-byte UDP query to the configured server:53 (transaction-id + QR-bit validation, any RCODE counts as reachable); **never `socket.getaddrinfo()`**.
  - **Broker (optional):** only when `network_diagnostics_broker_latency_enabled` is true **and** the QoS 1 path is completely idle (no in-flight entry, queue empty, no pending responses/logs/reboot). Busy means **skip the stage and complete the cycle — never delay real traffic**; it reuses the existing QoS 1 probe machinery, no second recovery policy.
  - Every probe socket gets a finite timeout (750 ms internal constant) and is closed in a `finally`; `MemoryError` always propagates.
- Config: `network_diagnostics_interval_sec` (default 300; `0` disables active probes — passive RSSI sampling continues) and `network_diagnostics_broker_latency_enabled` (default `false`).
- Results ride the existing network snapshot: no new mailbox, thread, or topic; never a degraded reason; null is never converted to false.

## Post-Outage Queue Drain (Core 0)

- A drain episode starts **only** after a runtime MQTT reconnect completes successfully with a non-empty outbound queue. The initial `start()` path never starts one; an empty queue leaves the last completed metrics unchanged; an in-flight entry retained across the failure participates normally.
- An MQTT failure mid-drain **cancels** the episode (state cleared), preserving the last completed metrics; the next reconnect starts a fresh episode at the then-current depth.
- Five metrics: `outbound_queue_drain_active` (current state only) plus the four historical scalars `outbound_queue_last_drain_start_depth`, `outbound_queue_last_drain_message_count` (completed entries — may exceed the start depth if Core 1 kept producing), `outbound_queue_last_drain_duration_ms`, and `outbound_queue_last_drain_rate_per_sec` (integer-only; all false/0 before the first completed drain).
- Optional rate limit `mqtt_post_outage_drain_rate_per_sec` (default `0` = disabled = unlimited drain): while an episode is active and the rate is positive, only the run loop's `take()`/publish step is gated by one non-blocking next-slot tick timestamp — a slot is consumed when the attempt *begins*, so a failed attempt consumes it too; the first entry is eligible immediately after reconnect. The limiter never sleeps and never delays connection establishment or subscriptions, connection logs, command/reboot responses, PINGREQ/PINGRESP, UTC handling, probes, or normal connected publishing.
- **Operational rule:** the configured rate must exceed the normal production rate or the queue may never drain — the firmware never auto-adjusts it.
- Historical/observational only: a past slow drain never degrades health and never triggers a reboot (invariant 9).

## Testing

- Run with `python -m pytest tests/` (host-side unit tests; the suite is the behavioral contract).
- Covered areas:
  - Core-ownership boundaries (AST checks): `test_architecture.py`
  - Config fail-fast validation: `test_config.py`
  - Inter-core bus semantics, the per-message ceiling on **both** admission paths (`put()` and `put_with_kind()`), heap-pressure admission (required collect → priority-class eviction → rejection), the shared `MemoryStats` low-watermark and GC statistics: `test_intercore.py`, `test_memory_stats.py`
  - `MemoryError` propagation on the Core 0/Core 1 message paths: `test_memory_error_propagation.py`
  - Health payload fields and classification: `test_health.py`; normal-runtime-anchored telemetry/health scheduling: `test_health_scheduling.py`, `test_scheduler_anchor.py`
  - Bounded MQTT connect/subscribe handshake, QoS 1 publish write/PUBACK exchange (`MQTTPubackTimeout` vs write failure), bounded reconnect cleanup (socket closed directly, no DISCONNECT into the dead link), inbound packet size limits, sequence identity and byte-identical retries: `test_mqtt.py`, `test_core0_recovery.py`
  - Self-healing startup, startup log, one-time full system log: `test_startup.py`, `test_startup_log.py`
  - Core 1 liveness heartbeat and the Core 0 stale-heartbeat watchdog: `test_core1_liveness.py`, `test_core0_heartbeat_watchdog.py`
  - Tick-wrap-safe uptime: `test_uptime.py`; UTC sync (startup + steady-state throttle): `test_utc_sync.py`
  - Reset-cause mapping (canonical strings only, `unknown` fallback) and its reporting: `test_hardware.py`
  - Passive Wi-Fi diagnostics (RSSI gates, reconnect/DHCP durations, stable status strings, `wlan.scan()` provably never called) and the bounded probes (ICMP echo feature-detection, fixed DNS query, 750 ms timeouts) with the staged Core 0 scheduling: `test_wifi_diagnostics.py`, `test_network_diagnostics.py`
  - Post-outage drain episodes, metrics, and the optional rate limit: `test_queue_drain.py`
  - LED state machine (Core 0-only): `test_led_manager.py`; device reinit suppression: `test_reinit_suppression.py`
- Hardware testing requires an actual Pico (deploy via the `_deploy-to-device-{1,2}.sh` scripts).

## Hardware Notes

- **Pico W:** Wi-Fi only, 256 KiB RAM
- **Pico 2 W:** Wi-Fi, 264 KiB RAM, faster CPU
- Onboard LED is Core 0-only via `LEDManager`
- **Never call `machine.reset()` from Core 1**

## Project Workflow

- `/git-commit` — the project commit workflow (MicroPython/Pico W tailoring; git is restricted to `status`/`diff`/`log`/`add`/`commit` — no push, branch, rebase, or reset; a failure leaves the working tree intact and is reported, not repaired).
- Quality gate before commit: `python -m pytest tests/` green.
