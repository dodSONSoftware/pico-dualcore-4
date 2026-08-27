# Build Notes — 0.0.0

## Baseline

This remains the clean third codebase built from:

- the original working firmware as the behavioral authority for Core 0 Wi-Fi, the MQTT client, reconnect style, LED initialization, and reboot timing;
- the latest dual-core firmware as the reference for the Core 1 device lifecycle and software `system-information` sensor.

## 0.0.0 review changes

The architecture was not redesigned. The review corrected confirmed boundary, correctness, and maintainability defects found in 0.0.0:

- Core 0 no longer receives or retains the Core 1 configuration. `main.py` owns startup orchestration, establishes Core 0 first, then lazy-imports and starts Core 1.
- Core 0 no longer reads its own IP address back through the inter-core network mailbox. Wi-Fi remains the Core 0 authority for network state.
- Core 1 no longer receives MQTT topic names or chooses broker destinations. The outbound lane carries only a message kind plus domain data; Core 0 maps the kind to the authoritative MQTT topic.
- Core 1 no longer supplies MQTT-owned envelope metadata (`source`, firmware version, schema version). Core 0 writes these fields authoritatively.
- Queued message timing is internally consistent: Core 1 captures creation-time `uptime_ms` and `timestamp` from one monotonic sample. Core 0 no longer substitutes publish-time uptime for delayed telemetry.
- MQTT envelope sequence ownership moved from the inter-core queue to Core 0, so sequence values follow actual publication order.
- Configuration is fail-fast and `config.json` is the single source of truth for operational settings used by this baseline. Unknown top-level keys are rejected.
- Exhausted Wi-Fi/MQTT retry sequences use the final configured retry delay rather than an additional hidden hard-coded delay.
- `config_schema_version` is validated against the authoritative version constant.
- Device definitions are structurally validated and duplicate device IDs are rejected before either core starts.
- MQTT callbacks require decoded JSON objects, route by subscribed topic, and validate command schema/payload boundaries.
- UTC responses validate schema, source, target, request type, request ID, timestamp presence, and a positive integer epoch before replacing the UTC mailbox. The local timestamp is normalized from the accepted epoch.
- The info-response subscription uses QoS 1.
- Core 1 retains a generated command response locally until it is admitted to the MQTT-bound queue; a full protected queue no longer silently loses an admitted inter-core command response.
- Inter-core lanes now reject invalid message kinds and non-dictionary boundary objects before they can wedge a consumer.
- `MemoryError` is no longer swallowed by normal network/device recovery paths.
- Telemetry rejection is always visible on the console, not only when debug logging is enabled.
- Best-effort MQTT/Wi-Fi cleanup/snapshot failures are debug-visible instead of silently ignored.
- Unused baseline configuration/message constants and the unused device-factory capability query were removed.
- `SystemInformation.get_memory()` samples allocation/free values once per call rather than making duplicate heap queries.
- Missing initial network state is treated as an invariant failure rather than silently manufacturing an all-null startup network section.

## Intentionally unchanged

- Original `mqtt_client.py` remains byte-for-byte identical to the original working firmware.
- Wi-Fi still uses `WLAN(IF_STA) -> active(True) -> PM_NONE -> connect()` on every attempt.
- The original approximately 20-second Wi-Fi connection observation remains.
- Reboot still publishes its QoS 1 response, waits 5 seconds + 1 second without network cleanup, then calls `machine.reset()`.
- The retained `DeviceManager` lifecycle behavior was not broadly refactored for style.
- The three-lane inter-core architecture remains:
  - `outbound_queue`;
  - `event_queue`;
  - latest-value `state_mailboxes`.
- No watchdog, advanced QoS recovery state machine, physical sensor, health publisher, or dynamic configuration subsystem was added.

## Hardware validation still required

0.0.0 has host-side validation but is not declared hardware-proven. The first target remains Pico W with only the software sensor enabled.

### Priority-based outbound retention restored

The outbound queue again uses the proven retention-priority policy from the earlier firmware without restoring the old queue's unrelated serialization or memory-pressure machinery. Lower values are more important: CRITICAL 10, ERROR 20, WARN 30, TELEMETRY 40, INFO 50, HEALTH 70. Current command responses are CRITICAL and telemetry is TELEMETRY. When capacity is full, the oldest entry in the least-important queued class is replaced only by an incoming message of equal or greater importance.

### LED manager restored

The onboard LED is exclusively owned by Core 0 through `LEDManager`. It flashes 50 ms on / 50 ms off from application boot until MQTT is connected, resumes that indication while Wi-Fi/MQTT connectivity is being re-established, and produces a non-blocking pulse of at least one second after each successful QoS 1 telemetry publication. Rapid backlog drains extend/coalesce the pulse rather than delaying MQTT recovery.
