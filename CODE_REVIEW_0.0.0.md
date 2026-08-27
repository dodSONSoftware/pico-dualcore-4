# Code Review — 0.0.0

## Review standard

The 0.0.0 baseline was reviewed for correctness, reliability, KISS/YAGNI/DRY, separation of concerns, exclusive core ownership, fail-fast validation, single source of truth, encapsulation, coupling/cohesion, MicroPython memory/runtime cost, explicit error handling, and AI-style overengineering.

The review intentionally avoided broad refactoring of working Core 1 device code and avoided adding speculative recovery mechanisms.

## Confirmed findings corrected

### P1 — Valid non-object JSON could break MQTT servicing

`core0.py` parsed JSON and immediately called dictionary methods. A valid JSON array/string on a subscribed topic could raise from the callback and incorrectly turn malformed application input into an MQTT connection failure.

**Correction:** require a JSON object before message dispatch and route traffic by the actual subscribed topic.

### P1 — Core 1 command responses could be silently lost

Core 1 removed an event from the private event queue and attempted to enqueue its command response. If the outbound queue was full of protected messages, admission failed and the response was discarded.

**Correction:** Core 1 keeps one generated response locally and retries admission before consuming another event. The command is not re-executed.

### P1 — Queued telemetry could contain contradictory time fields

0.0.0 captured `timestamp` when Core 1 created telemetry but assigned `uptime_ms` later on Core 0 when the queued message was published. During a network outage, one message could therefore describe two different moments.

**Correction:** Core 1 captures creation-time `uptime_ms` and `timestamp` from the same monotonic sample before enqueue. Core 0 preserves those fields while adding its authoritative MQTT envelope fields.

### P2 — Core 0 retained Core 1 configuration

0.0.0 passed the Core 1 configuration through Core 0 solely so Core 0 could start the second thread.

**Correction:** startup orchestration moved to `main.py`. Core 0 establishes Wi-Fi/MQTT; only then does `main.py` lazy-import and start Core 1 with its own configuration.

### P2 — Core 0 read its own network state through the inter-core mailbox

IP-target command matching queried the network snapshot mailbox, making a Core 0 output channel part of Core 0's own state path.

**Correction:** `Wifi.ip_address()` is the Core 0 authority. The network mailbox is output from Core 0 for Core 1 consumption only.

### P2 — MQTT routing information leaked into Core 1

0.0.0 gave Core 1 the telemetry and command-response MQTT topic names and required Core 1 to select a broker topic when placing an outbound message onto the inter-core queue.

**Correction:** Core 1 now knows only outbound message kinds. Core 0 exclusively maps those kinds to MQTT topics. MQTT topics no longer appear in Core 1 configuration or code.

### P2 — MQTT envelope metadata had competing writers

0.0.0 allowed Core 1 to populate fields such as `source`, firmware version, and message schema version while Core 0 also supplied defaults for them.

**Correction:** Core 0 writes source, runtime ID, firmware version, message schema version, and sequence authoritatively. Core 1 supplies sensor/domain fields and creation time only.

### P2 — MQTT sequence ownership crossed the Core 0 boundary

The outbound inter-core queue allocated MQTT envelope sequence numbers even though Core 0 also creates direct command responses. A direct Core 0 response could therefore be delivered before an older queued message with a lower sequence.

**Correction:** sequence numbers are owned and assigned by Core 0 in actual publication order. A failed outbound publish remains in the queue's single in-flight slot and is retried before that slot can advance.

### P2 — Configuration had hidden defaults and incomplete validation

Several runtime settings were silently defaulted in code even though they also existed in `config.json`, creating more than one source of truth. Queue sizes, MQTT timing, topics, schema version, and device structure were incompletely validated.

**Correction:** operational settings used by this baseline are explicit required config keys and are validated before startup. Unknown top-level keys are rejected.

### P2 — Exhausted retry sequences used an undocumented extra delay

After the configured Wi-Fi or MQTT retry sequence was exhausted, Core 0 added a hard-coded five-second wait before starting the next sequence.

**Correction:** the final configured retry delay is used between retry sequences. No second retry-delay source is introduced.

### P2 — UTC response validation was too permissive

0.0.0 accepted a matching request ID and integer epoch without validating message schema, source, target, or timestamp structure.

**Correction:** validate schema, source, target, request type/id, timestamp presence, and a positive non-boolean integer epoch. The stored local timestamp is normalized from the accepted epoch rather than trusting two independent time representations.

### P2 — `MemoryError` was swallowed in several broad exception paths

On constrained Pico hardware, treating allocation failure as a routine protocol/network error can leave the firmware in a degraded or misleading state.

**Correction:** normal recovery paths re-raise `MemoryError`; routine I/O/protocol errors continue to use their intended recovery behavior.

### P2 — Inter-core boundary types were not validated

A bad outbound kind or non-dictionary event/message could be admitted and fail later inside a consumer. An unsupported outbound kind could leave the single in-flight slot permanently stuck.

**Correction:** each lane fails fast on the small set of structural assumptions it requires: supported outbound kind, dictionary message/event, and dictionary state snapshot.

### P3 — Missing initial network state could produce synthetic null telemetry

The software sensor returned an all-null network object if no network snapshot existed.

**Correction:** absence of the initial Core 0 network snapshot is now treated as an invariant failure. Core 0 publishes the initial snapshot before Core 1 starts.

### P3 — Unused baseline surface area

0.0.0 carried an unused MQTT log topic, unused message constants, an unused device-factory capability function, and an unnecessary fallback dependency.

**Correction:** removed them rather than retaining speculative baseline functionality.

## Items deliberately not changed

- No generalized RPC/message framework was added.
- No watchdog or cross-core recovery feature was added.
- No physical sensors were added.
- No new ring-buffer/container abstraction was introduced; the configured queues are intentionally small.
- The retained `DeviceManager` was not broadly refactored merely for style.
- The original MQTT client was not modernized and remains byte-for-byte identical to the known-good original.
- No advanced QoS retransmission state machine was reintroduced.

## Remaining hardware risks to validate

Host tests cannot prove CYW43 warm-reset behavior, RP2040/RP2350 thread scheduling, actual Pico heap headroom, physical broker/network outage behavior, or behavior if the original blocking MQTT client loses a connection while waiting for PUBACK. Those are hardware acceptance tests, not justification for adding speculative machinery before the baseline is exercised.
