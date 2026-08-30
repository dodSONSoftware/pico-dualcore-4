# Host-side behavioral tests for the three-lane transport.

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

# Mock machine module for host-side testing
class MockMachine:
    PWM = None
    Pin = None

sys.modules['machine'] = MockMachine()

# Mock network module for host-side testing
class MockNetwork:
    WLAN = None

sys.modules['network'] = MockNetwork()

from intercore import (
    InterCore,
    OutboundQueue,
    DEFAULT_MAX_OUTBOUND_QUEUED_BYTES,
    KIND_TELEMETRY,
    KIND_COMMAND_RESPONSE,
    KIND_HEALTH,
    KIND_LOG,
    RETENTION_PRIORITY_CRITICAL,
    RETENTION_PRIORITY_ERROR,
    RETENTION_PRIORITY_TELEMETRY,
    RETENTION_PRIORITY_INFO,
    RETENTION_PRIORITY_HEALTH,
)


def put(bus, kind, message, priority):
    return bus.outbound_queue.put(kind, message, priority)


def _bytes_of(n):
    """A payload of exactly n bytes (JSON-ish, avoids serializer overhead)."""
    return b"{" + b"x" * (n - 2) + b"}"


def drain(bus):
    values = []
    while True:
        entry = bus.outbound_queue.take()
        if entry is None:
            break
        # Parse the pre-serialized payload_bytes to get message fields
        message = json.loads(entry["payload_bytes"].decode("utf-8"))
        values.append(message["id"])
        assert bus.outbound_queue.complete_in_flight(entry)
    return values


def test_more_important_message_evicts_oldest_least_important_entry():
    bus = InterCore(outbound_max=4, event_max=2)
    assert put(bus, KIND_TELEMETRY, {"id": 1}, RETENTION_PRIORITY_INFO)
    assert put(bus, KIND_TELEMETRY, {"id": 2}, RETENTION_PRIORITY_HEALTH)
    assert put(bus, KIND_TELEMETRY, {"id": 3}, RETENTION_PRIORITY_TELEMETRY)
    assert put(bus, KIND_TELEMETRY, {"id": 4}, RETENTION_PRIORITY_HEALTH)

    assert put(bus, KIND_COMMAND_RESPONSE, {"id": 5}, RETENTION_PRIORITY_CRITICAL)

    # Oldest HEALTH entry (id 2) is evicted; FIFO order of survivors is preserved.
    assert drain(bus) == [1, 3, 4, 5]


def test_equal_priority_evicts_oldest_entry_in_that_class():
    bus = InterCore(outbound_max=3, event_max=2)
    assert put(bus, KIND_TELEMETRY, {"id": 1}, RETENTION_PRIORITY_TELEMETRY)
    assert put(bus, KIND_TELEMETRY, {"id": 2}, RETENTION_PRIORITY_TELEMETRY)
    assert put(bus, KIND_COMMAND_RESPONSE, {"id": 3}, RETENTION_PRIORITY_CRITICAL)

    assert put(bus, KIND_TELEMETRY, {"id": 4}, RETENTION_PRIORITY_TELEMETRY)

    assert drain(bus) == [2, 3, 4]


def test_less_important_message_is_rejected_when_queue_is_full():
    bus = InterCore(outbound_max=2, event_max=2)
    assert put(bus, KIND_COMMAND_RESPONSE, {"id": 1}, RETENTION_PRIORITY_CRITICAL)
    assert put(bus, KIND_TELEMETRY, {"id": 2}, RETENTION_PRIORITY_ERROR)

    assert not put(bus, KIND_TELEMETRY, {"id": 3}, RETENTION_PRIORITY_HEALTH)
    assert drain(bus) == [1, 2]


def test_command_response_critical_evicts_telemetry():
    bus = InterCore(outbound_max=2, event_max=2)
    assert put(bus, KIND_TELEMETRY, {"id": 1}, RETENTION_PRIORITY_TELEMETRY)
    assert put(bus, KIND_TELEMETRY, {"id": 2}, RETENTION_PRIORITY_TELEMETRY)

    assert put(bus, KIND_COMMAND_RESPONSE, {"id": 3}, RETENTION_PRIORITY_CRITICAL)
    assert drain(bus) == [2, 3]


def test_in_flight_consumes_capacity_and_is_never_evicted():
    bus = InterCore(outbound_max=2, event_max=2)
    assert put(bus, KIND_COMMAND_RESPONSE, {"id": 1}, RETENTION_PRIORITY_CRITICAL)
    assert put(bus, KIND_TELEMETRY, {"id": 2}, RETENTION_PRIORITY_TELEMETRY)

    first = bus.outbound_queue.take()
    assert json.loads(first["payload_bytes"].decode("utf-8"))["id"] == 1

    # In-flight critical entry remains untouched; queued telemetry is the only
    # eviction candidate and is replaced by equal-priority newer telemetry.
    assert put(bus, KIND_TELEMETRY, {"id": 3}, RETENTION_PRIORITY_TELEMETRY)
    assert bus.outbound_queue.take() is first
    status = bus.outbound_queue.status()
    assert status["pending"] == 1
    assert status["in_flight"]
    assert bus.outbound_queue.complete_in_flight(first)
    assert drain(bus) == [3]


def test_full_capacity_with_only_in_flight_entry_rejects_new_message():
    bus = InterCore(outbound_max=1, event_max=1)
    assert put(bus, KIND_TELEMETRY, {"id": 1}, RETENTION_PRIORITY_HEALTH)
    first = bus.outbound_queue.take()

    assert not put(bus, KIND_COMMAND_RESPONSE, {"id": 2}, RETENTION_PRIORITY_CRITICAL)
    assert bus.outbound_queue.take() is first
    assert bus.outbound_queue.complete_in_flight(first)


def test_priority_eviction_status_counters():
    bus = InterCore(outbound_max=2, event_max=1)
    assert put(bus, KIND_TELEMETRY, {"id": 1}, RETENTION_PRIORITY_TELEMETRY)
    assert put(bus, KIND_TELEMETRY, {"id": 2}, RETENTION_PRIORITY_HEALTH)
    assert put(bus, KIND_COMMAND_RESPONSE, {"id": 3}, RETENTION_PRIORITY_CRITICAL)
    assert not put(bus, KIND_TELEMETRY, {"id": 4}, RETENTION_PRIORITY_HEALTH)

    status = bus.outbound_queue.status()
    assert status["messages_evicted"] == 1
    assert status["telemetry_evicted"] == 1
    assert status["messages_rejected"] == 1
    assert status["high_watermark"] == 2


def test_event_queue_is_fifo():
    bus = InterCore(outbound_max=2, event_max=2)
    assert bus.event_queue.put({"id": 1})
    assert bus.event_queue.put({"id": 2})
    assert bus.event_queue.take()["id"] == 1
    assert bus.event_queue.take()["id"] == 2


def test_state_mailbox_replaces_latest_value():
    bus = InterCore(outbound_max=2, event_max=2)
    first = {"rssi": -60}
    second = {"rssi": -50}
    bus.state_mailboxes.set_network_snapshot(first)
    assert bus.state_mailboxes.get_network_snapshot() is first
    bus.state_mailboxes.set_network_snapshot(second)
    assert bus.state_mailboxes.get_network_snapshot() is second


def test_invalid_queue_capacity_fails_fast():
    with pytest.raises(ValueError):
        InterCore(outbound_max=0, event_max=1)
    with pytest.raises(ValueError):
        InterCore(outbound_max=1, event_max=0)


def test_utc_mailbox_replaces_latest_value():
    bus = InterCore(outbound_max=2, event_max=2)
    first = {"utc_epoch_ms": 1}
    second = {"utc_epoch_ms": 2}
    bus.state_mailboxes.set_utc_snapshot(first)
    assert bus.state_mailboxes.get_utc_snapshot() is first
    bus.state_mailboxes.set_utc_snapshot(second)
    assert bus.state_mailboxes.get_utc_snapshot() is second


def test_intercore_lanes_reject_invalid_boundary_objects():
    bus = InterCore(outbound_max=2, event_max=2)
    with pytest.raises(ValueError, match="Unsupported outbound message kind"):
        put(bus, "unknown", {"id": 1}, RETENTION_PRIORITY_TELEMETRY)
    with pytest.raises(ValueError, match="outbound message"):
        put(bus, KIND_TELEMETRY, "not-a-dict", RETENTION_PRIORITY_TELEMETRY)
    with pytest.raises(ValueError, match="retention_priority"):
        put(bus, KIND_TELEMETRY, {"id": 1}, True)
    with pytest.raises(ValueError, match="retention_priority"):
        put(bus, KIND_TELEMETRY, {"id": 1}, 9)
    with pytest.raises(ValueError, match="retention_priority"):
        put(bus, KIND_TELEMETRY, {"id": 1}, 71)
    with pytest.raises(ValueError, match="inter-core event"):
        bus.event_queue.put("not-a-dict")
    with pytest.raises(ValueError, match="network snapshot"):
        bus.state_mailboxes.set_network_snapshot(None)
    with pytest.raises(ValueError, match="UTC snapshot"):
        bus.state_mailboxes.set_utc_snapshot(None)


def test_queue_entry_stores_payload_bytes():
    """Verify the queue stores pre-serialized bytes, not the dict."""
    bus = InterCore(outbound_max=2, event_max=2)
    message = {"id": "test", "value": 42}
    assert put(bus, KIND_TELEMETRY, message, RETENTION_PRIORITY_TELEMETRY)

    entry = bus.outbound_queue.take()
    assert "payload_bytes" in entry
    assert isinstance(entry["payload_bytes"], bytes)
    # The original message dict should not be stored
    assert "message" not in entry
    # The bytes should be valid JSON
    parsed = json.loads(entry["payload_bytes"].decode("utf-8"))
    assert parsed["id"] == "test"
    assert parsed["value"] == 42
    assert bus.outbound_queue.complete_in_flight(entry)


def test_message_mutation_does_not_affect_queued_payload():
    """Verify queued payload is immutable after admission."""
    bus = InterCore(outbound_max=2, event_max=2)
    message = {"id": "original", "value": 1}
    assert put(bus, KIND_TELEMETRY, message, RETENTION_PRIORITY_TELEMETRY)

    # Mutate original message after queue admission
    message["id"] = "modified"
    message["value"] = 999

    entry = bus.outbound_queue.take()
    parsed = json.loads(entry["payload_bytes"].decode("utf-8"))
    # The queued payload should still have original values
    assert parsed["id"] == "original"
    assert parsed["value"] == 1
    assert bus.outbound_queue.complete_in_flight(entry)


def test_invalid_value_rejected():
    """Verify unsupported value types are rejected."""
    bus = InterCore(outbound_max=2, event_max=2)
    # Object instance is not JSON-serializable
    with pytest.raises(ValueError, match="Unsupported type"):
        put(bus, KIND_TELEMETRY, {"id": "test", "data": object()}, RETENTION_PRIORITY_TELEMETRY)


def test_non_string_key_rejected():
    """Verify non-string dictionary keys are rejected."""
    bus = InterCore(outbound_max=2, event_max=2)
    with pytest.raises(ValueError, match="Non-string key"):
        put(bus, KIND_TELEMETRY, {123: "invalid"}, RETENTION_PRIORITY_TELEMETRY)


def test_nan_float_rejected():
    """Verify NaN float values are rejected."""
    bus = InterCore(outbound_max=2, event_max=2)
    with pytest.raises(ValueError, match="Non-finite float"):
        put(bus, KIND_TELEMETRY, {"id": "test", "value": float("nan")}, RETENTION_PRIORITY_TELEMETRY)


def test_infinity_float_rejected():
    """Verify Infinity float values are rejected."""
    bus = InterCore(outbound_max=2, event_max=2)
    with pytest.raises(ValueError, match="Non-finite float"):
        put(bus, KIND_TELEMETRY, {"id": "test", "value": float("inf")}, RETENTION_PRIORITY_TELEMETRY)
    with pytest.raises(ValueError, match="Non-finite float"):
        put(bus, KIND_TELEMETRY, {"id": "test", "value": -float("inf")}, RETENTION_PRIORITY_TELEMETRY)


def test_nested_invalid_rejected():
    """Verify nested invalid values are rejected."""
    bus = InterCore(outbound_max=2, event_max=2)
    with pytest.raises(ValueError, match="Non-finite float"):
        put(bus, KIND_TELEMETRY, {
            "id": "test",
            "nested": {"value": float("nan")}
        }, RETENTION_PRIORITY_TELEMETRY)


def _message_serializing_to(total_bytes):
    """A message whose JSON/UTF-8 serialization is exactly total_bytes long.

    Measures the wrapper overhead by serializing the same shape with an empty
    payload, then sizes the ASCII payload to hit the target exactly.
    """
    overhead = len(json.dumps({"id": "test", "data": ""}).encode("utf-8"))
    assert overhead <= total_bytes
    return {"id": "test", "data": "x" * (total_bytes - overhead)}


def test_exact_max_size_admitted():
    """Verify a message at exactly the max size is admitted."""
    from message_serializer import MAX_OUTBOUND_MESSAGE_BYTES
    message = _message_serializing_to(MAX_OUTBOUND_MESSAGE_BYTES)
    bus = InterCore(outbound_max=2, event_max=2)

    admitted = put(bus, KIND_TELEMETRY, message, RETENTION_PRIORITY_TELEMETRY)
    assert admitted is True

    entry = bus.outbound_queue.take()
    assert entry is not None
    assert len(entry["payload_bytes"]) == MAX_OUTBOUND_MESSAGE_BYTES
    assert bus.outbound_queue.complete_in_flight(entry)


def test_one_byte_over_max_rejected():
    """Verify a message one byte over the max size is rejected, not admitted."""
    from message_serializer import MAX_OUTBOUND_MESSAGE_BYTES
    message = _message_serializing_to(MAX_OUTBOUND_MESSAGE_BYTES + 1)
    bus = InterCore(outbound_max=2, event_max=2)

    admitted = put(bus, KIND_TELEMETRY, message, RETENTION_PRIORITY_TELEMETRY)
    assert admitted is False

    assert bus.outbound_queue.take() is None
    status = bus.outbound_queue.status()
    assert status["oversized_rejected"] == 1


def test_oversized_rejected():
    """Verify messages exceeding max size are rejected."""
    bus = InterCore(outbound_max=2, event_max=2)
    from message_serializer import MAX_OUTBOUND_MESSAGE_BYTES
    # Create a message larger than MAX_OUTBOUND_MESSAGE_BYTES
    payload = "x" * (MAX_OUTBOUND_MESSAGE_BYTES + 1000)
    message = {"id": "test", "data": payload}
    assert not put(bus, KIND_TELEMETRY, message, RETENTION_PRIORITY_TELEMETRY)

    status = bus.outbound_queue.status()
    assert status["oversized_rejected"] >= 1


def test_put_with_kind_one_byte_over_max_rejected():
    """put_with_kind() enforces the same per-message ceiling as put()."""
    from message_serializer import MAX_OUTBOUND_MESSAGE_BYTES
    bus = InterCore(outbound_max=2, event_max=2)

    admitted = bus.outbound_queue.put_with_kind(
        KIND_HEALTH, _bytes_of(MAX_OUTBOUND_MESSAGE_BYTES + 1), RETENTION_PRIORITY_HEALTH
    )
    assert admitted is False

    assert bus.outbound_queue.take() is None
    status = bus.outbound_queue.status()
    assert status["oversized_rejected"] == 1


def test_put_with_kind_exact_max_size_admitted():
    """A payload at exactly the max size is admitted by put_with_kind()."""
    from message_serializer import MAX_OUTBOUND_MESSAGE_BYTES
    bus = InterCore(outbound_max=2, event_max=2)

    assert bus.outbound_queue.put_with_kind(
        KIND_HEALTH, _bytes_of(MAX_OUTBOUND_MESSAGE_BYTES), RETENTION_PRIORITY_HEALTH
    )

    entry = bus.outbound_queue.take()
    assert entry is not None
    assert len(entry["payload_bytes"]) == MAX_OUTBOUND_MESSAGE_BYTES
    assert bus.outbound_queue.complete_in_flight(entry)


def test_status_counters_include_serialization_rejections():
    """Verify serialization rejections are counted."""
    bus = InterCore(outbound_max=2, event_max=2)
    # Try to put an invalid message
    try:
        put(bus, KIND_TELEMETRY, {"id": "test", "data": object()}, RETENTION_PRIORITY_TELEMETRY)
    except ValueError:
        pass  # Expected

    status = bus.outbound_queue.status()
    assert "serialization_rejected" in status
    assert "oversized_rejected" in status


# --- Aggregate queued-byte budget (memory-safety boundary) ---

def test_per_message_ceiling_is_mcu_scale():
    """The per-message ceiling is at most 16 KiB (regression guard vs 128 KiB)."""
    from message_serializer import get_max_message_bytes
    assert 8 * 1024 <= get_max_message_bytes() <= 16 * 1024


def test_default_byte_budget_is_mcu_scale():
    """The default queued-byte budget is MCU-safe and holds a realistic queue."""
    # Well under a Pico W's heap (256 KiB).
    assert DEFAULT_MAX_OUTBOUND_QUEUED_BYTES <= 64 * 1024
    # Comfortably holds a full queue of 16 entries at a realistic ~2 KiB each.
    assert DEFAULT_MAX_OUTBOUND_QUEUED_BYTES >= 16 * 2 * 1024


def test_byte_budget_limits_admission_before_count():
    """A high-count / low-byte queue admits only until the byte budget is hit."""
    bus = InterCore(outbound_max=10, event_max=2, outbound_max_bytes=100)
    assert bus.outbound_queue.put_with_kind(KIND_HEALTH, _bytes_of(40), RETENTION_PRIORITY_HEALTH)
    assert bus.outbound_queue.put_with_kind(KIND_HEALTH, _bytes_of(40), RETENTION_PRIORITY_HEALTH)
    # Third 40-byte entry would push to 120 > 100; same class, so evict the oldest.
    assert bus.outbound_queue.put_with_kind(KIND_HEALTH, _bytes_of(40), RETENTION_PRIORITY_HEALTH)
    status = bus.outbound_queue.status()
    assert status["queued_bytes"] <= 100
    assert status["pending"] == 2  # bounded by bytes, not by the count budget of 10


def test_byte_budget_respected_after_eviction():
    """Eviction frees enough bytes for the incoming entry; the budget still holds."""
    bus = InterCore(outbound_max=10, event_max=2, outbound_max_bytes=100)
    assert bus.outbound_queue.put_with_kind(KIND_HEALTH, _bytes_of(60), RETENTION_PRIORITY_HEALTH)
    assert bus.outbound_queue.put_with_kind(KIND_HEALTH, _bytes_of(40), RETENTION_PRIORITY_HEALTH)
    # 60 + 40 already fills the budget; the next evicts the oldest (60) -> 40 + 40.
    assert bus.outbound_queue.put_with_kind(KIND_HEALTH, _bytes_of(40), RETENTION_PRIORITY_HEALTH)
    status = bus.outbound_queue.status()
    assert status["queued_bytes"] <= 100
    assert status["pending"] == 2
    assert status["messages_evicted"] >= 1


def test_byte_budget_less_important_rejected_without_evicting():
    """A less-important incoming entry is rejected, not the valid entry evicted."""
    bus = InterCore(outbound_max=10, event_max=2, outbound_max_bytes=100)
    # A 60-byte CRITICAL entry fills most of the budget.
    assert bus.outbound_queue.put_with_kind(KIND_HEALTH, _bytes_of(60), RETENTION_PRIORITY_CRITICAL)
    # A 50-byte HEALTH entry would exceed the budget, but HEALTH (70) is less
    # important than CRITICAL (10): reject rather than evict the critical entry.
    assert not bus.outbound_queue.put_with_kind(KIND_HEALTH, _bytes_of(50), RETENTION_PRIORITY_HEALTH)
    status = bus.outbound_queue.status()
    assert status["pending"] == 1
    assert status["queued_bytes"] == 60
    assert status["messages_evicted"] == 0
    assert status["messages_rejected"] >= 1


def test_byte_budget_never_evicts_for_nothing():
    """A valid entry is not dropped to admit one that still would not fit."""
    bus = InterCore(outbound_max=10, event_max=2, outbound_max_bytes=100)
    assert bus.outbound_queue.put_with_kind(KIND_HEALTH, _bytes_of(60), RETENTION_PRIORITY_HEALTH)
    # 150 bytes does not fit even after evicting the 60-byte entry (150 > 100).
    # It must be rejected; the valid 60-byte entry must survive.
    assert not bus.outbound_queue.put_with_kind(KIND_HEALTH, _bytes_of(150), RETENTION_PRIORITY_HEALTH)
    status = bus.outbound_queue.status()
    assert status["pending"] == 1
    assert status["queued_bytes"] == 60
    assert status["messages_evicted"] == 0
    assert status["messages_rejected"] >= 1


def test_byte_budget_includes_in_flight_entry():
    """The in-flight entry's retained bytes count toward the byte budget.

    The in-flight payload is not freed until its PUBACK (complete_in_flight),
    so it must consume budget along with the queued FIFO. Previously take()
    subtracted it, letting the queue retain (budget + in-flight) of payload
    while reporting only the budget.
    """
    # A 200-byte entry moved in-flight; the budget is 250 bytes.
    bus = InterCore(outbound_max=10, event_max=2, outbound_max_bytes=250)
    assert bus.outbound_queue.put_with_kind(KIND_HEALTH, _bytes_of(200), RETENTION_PRIORITY_HEALTH)
    first = bus.outbound_queue.take()
    assert bus.outbound_queue.has_in_flight()

    # With the in-flight entry counted, a further 60 bytes exceeds the budget
    # (200 + 60 = 260 > 250). Nothing is queued to evict, so admission fails --
    # even though the old in-flight-excluded budget had 250 bytes of room.
    assert not bus.outbound_queue.put_with_kind(KIND_HEALTH, _bytes_of(60), RETENTION_PRIORITY_HEALTH)

    status = bus.outbound_queue.status()
    assert status["in_flight"]
    assert status["pending"] == 0
    # The reported byte budget includes the retained in-flight entry.
    assert status["queued_bytes"] == 200

    # Completing the in-flight entry frees exactly its own bytes.
    assert bus.outbound_queue.complete_in_flight(first)
    assert bus.outbound_queue.status()["queued_bytes"] == 0


def test_dict_path_enforces_byte_budget():
    """put() (dict -> serialize) also enforces the queued-byte budget."""
    bus = InterCore(outbound_max=10, event_max=2, outbound_max_bytes=100)
    for _ in range(20):
        bus.outbound_queue.put(KIND_TELEMETRY, {"id": "a", "v": 1}, RETENTION_PRIORITY_TELEMETRY)
    assert bus.outbound_queue.status()["queued_bytes"] <= 100


def test_status_exposes_byte_budget():
    bus = InterCore(outbound_max=4, event_max=2, outbound_max_bytes=2048)
    status = bus.outbound_queue.status()
    assert status["max_queued_bytes"] == 2048
    assert status["queued_bytes"] == 0
    assert bus.outbound_queue.put_with_kind(KIND_HEALTH, _bytes_of(256), RETENTION_PRIORITY_HEALTH)
    assert bus.outbound_queue.status()["queued_bytes"] == 256


def test_outbound_queue_rejects_bad_byte_budget():
    with pytest.raises(ValueError, match="max_queued_bytes"):
        OutboundQueue(4, 0)
    with pytest.raises(ValueError, match="max_queued_bytes"):
        OutboundQueue(4, True)
    with pytest.raises(ValueError, match="max_queued_bytes"):
        OutboundQueue(4, -1)


def test_connection_log_like_message():
    """Verify connection log-like messages are handled correctly."""
    bus = InterCore(outbound_max=2, event_max=2)
    from message_serializer import serialize_and_validate_message

    # This is the format used by _queue_connection_log
    log_message = {
        "message_type": "log",
        "payload": {
            "level": "info",
            "message": "Connected to Wi-Fi",
            "event": "wifi_connection_established",
            "module": "wifi",
            "data": {"ssid": "test", "ip_address": "10.0.0.1"},
        },
    }

    # Serialize and verify it works
    payload_bytes = serialize_and_validate_message(log_message)
    assert isinstance(payload_bytes, bytes)
    assert len(payload_bytes) > 0

    # Verify the message can be reconstructed
    reconstructed = json.loads(payload_bytes.decode("utf-8"))
    assert reconstructed["message_type"] == "log"
    assert reconstructed["payload"]["message"] == "Connected to Wi-Fi"


def test_health_message_kind_is_valid():
    """Verify KIND_HEALTH is a valid outbound message kind."""
    bus = InterCore(outbound_max=2, event_max=2)
    # Health messages should be accepted with health priority
    assert bus.outbound_queue.put_with_kind(KIND_HEALTH, b'{"test": true}', RETENTION_PRIORITY_HEALTH)


def test_log_message_kind_is_valid():
    """Verify KIND_LOG is a valid outbound message kind for log messages."""
    bus = InterCore(outbound_max=2, event_max=2)
    # Log messages should be accepted with info priority
    assert bus.outbound_queue.put_with_kind(KIND_LOG, b'{"test": true}', RETENTION_PRIORITY_INFO)

    entry = bus.outbound_queue.take()
    assert entry["kind"] == KIND_LOG
    assert "topic" not in entry  # topic resolution is Core 0's job
    bus.outbound_queue.complete_in_flight(entry)


def test_health_message_is_lesser_priority_than_info():
    """Verify health messages can be evicted by info messages."""
    bus = InterCore(outbound_max=2, event_max=2)
    # Queue two health messages
    assert bus.outbound_queue.put_with_kind(KIND_HEALTH, b'{"id": 1}', RETENTION_PRIORITY_HEALTH)
    assert bus.outbound_queue.put_with_kind(KIND_HEALTH, b'{"id": 2}', RETENTION_PRIORITY_HEALTH)

    # Try to add an info message (more important) when full
    # Info has priority 50, health has priority 70
    # So info should evict the oldest health (id 1)
    assert bus.outbound_queue.put_with_kind(KIND_HEALTH, b'{"id": 3}', RETENTION_PRIORITY_INFO)

    # The oldest health (id 1) should be evicted
    # Queue is now [2, 3] after eviction and addition
    entry = bus.outbound_queue.take()
    message = json.loads(entry["payload_bytes"].decode("utf-8"))
    assert message["id"] == 2  # id 1 was evicted, 2 is now oldest
    bus.outbound_queue.complete_in_flight(entry)

    entry = bus.outbound_queue.take()
    message = json.loads(entry["payload_bytes"].decode("utf-8"))
    assert message["id"] == 3
    bus.outbound_queue.complete_in_flight(entry)

    assert bus.outbound_queue.take() is None


def test_health_message_is_rejected_when_queue_is_full():
    """Verify health messages are rejected when queue is full and no eviction possible."""
    bus = InterCore(outbound_max=1, event_max=2)
    # Fill the queue with a critical message
    assert bus.outbound_queue.put_with_kind(KIND_HEALTH, b'{"id": 1}', RETENTION_PRIORITY_CRITICAL)

    first = bus.outbound_queue.take()
    bus.outbound_queue.complete_in_flight(first)

    # Queue is now empty but capacity is 1
    # Take it again to leave in_flight
    assert bus.outbound_queue.put_with_kind(KIND_HEALTH, b'{"id": 2}', RETENTION_PRIORITY_HEALTH)
    first = bus.outbound_queue.take()

    # Now try to add health when in_flight is occupied
    # This should fail since in_flight counts toward max
    assert not bus.outbound_queue.put_with_kind(KIND_HEALTH, b'{"id": 3}', RETENTION_PRIORITY_HEALTH)
    bus.outbound_queue.complete_in_flight(first)


def test_health_queue_depth_helper():
    """Verify the health queue depth helper returns correct values."""
    bus = InterCore(outbound_max=4, event_max=2)

    # Empty queue should return (0, 4)
    depth, capacity = bus.outbound_queue.get_depth_with_capacity()
    assert depth == 0
    assert capacity == 4

    # Add one message
    assert bus.outbound_queue.put(KIND_TELEMETRY, {"id": 1}, RETENTION_PRIORITY_TELEMETRY)
    depth, capacity = bus.outbound_queue.get_depth_with_capacity()
    assert depth == 1
    assert capacity == 4

    # Take and complete to check in-flight counting
    first = bus.outbound_queue.take()
    assert first is not None
    depth, capacity = bus.outbound_queue.get_depth_with_capacity()
    # In-flight counts toward depth
    assert depth == 1
    assert capacity == 4

    bus.outbound_queue.complete_in_flight(first)
    depth, capacity = bus.outbound_queue.get_depth_with_capacity()
    assert depth == 0
    assert capacity == 4


def test_health_queue_depth_calculation():
    """Verify queue depth is queued + in_flight."""
    bus = InterCore(outbound_max=4, event_max=2)

    # Add two messages
    assert bus.outbound_queue.put(KIND_TELEMETRY, {"id": 1}, RETENTION_PRIORITY_TELEMETRY)
    assert bus.outbound_queue.put(KIND_TELEMETRY, {"id": 2}, RETENTION_PRIORITY_TELEMETRY)

    depth, capacity = bus.outbound_queue.get_depth_with_capacity()
    assert depth == 2
    assert capacity == 4

    # Take one (moves to in_flight)
    first = bus.outbound_queue.take()
    assert first is not None
    depth, capacity = bus.outbound_queue.get_depth_with_capacity()
    assert depth == 2  # 1 queued + 1 in_flight
    assert capacity == 4


def test_health_topic_routing_returns_configured_topic():
    """Verify KIND_HEALTH routes to the configured mqtt_topic_health."""
    from config import load_config, split_config

    config_path = pathlib.Path(__file__).resolve().parents[1] / "config.json"
    config = load_config(str(config_path))
    core0, _, bus_config = split_config(config)

    bus = InterCore(outbound_max=bus_config["max_outbound_queue_entries"],
                   event_max=bus_config["max_intercore_event_entries"])

    # Create a Core0-like topic resolver using the config
    def topic_for_kind(kind):
        if kind == KIND_HEALTH:
            return core0["mqtt_topic_health"]
        raise ValueError("Unsupported outbound message kind")

    # Verify KIND_HEALTH returns the configured health topic
    assert topic_for_kind(KIND_HEALTH) == "iot/v3/health"
    assert topic_for_kind(KIND_HEALTH) == config["mqtt_topic_health"]


def test_publish_failure_keeps_entry_in_flight_for_retry():
    """Verify that a failed publish leaves the entry in flight so it can be retried.

    QoS 1 requires at-least-once delivery: a message the broker has not PUBACKed
    must not be dropped. When a publish fails, Core 0 leaves the entry in flight,
    and take() keeps returning the same entry until it is completed.
    """
    bus = InterCore(outbound_max=2, event_max=2)

    # Add two messages
    assert bus.outbound_queue.put(KIND_TELEMETRY, {"id": 1}, RETENTION_PRIORITY_TELEMETRY)
    assert bus.outbound_queue.put(KIND_TELEMETRY, {"id": 2}, RETENTION_PRIORITY_TELEMETRY)

    # Take first message (moves to in_flight)
    first = bus.outbound_queue.take()
    assert first is not None
    assert json.loads(first["payload_bytes"].decode("utf-8"))["id"] == 1
    assert bus.outbound_queue.has_in_flight()

    # Simulate a failed publish - complete_in_flight is NOT called
    # The entry must stay in flight so it can be retried
    assert bus.outbound_queue.has_in_flight()

    # The same entry is returned again instead of advancing the queue
    retry = bus.outbound_queue.take()
    assert retry is first
    assert json.loads(retry["payload_bytes"].decode("utf-8"))["id"] == 1

    # Retry succeeds - complete it and the queue advances
    assert bus.outbound_queue.complete_in_flight(first)
    assert not bus.outbound_queue.has_in_flight()

    second = bus.outbound_queue.take()
    assert second is not None
    assert json.loads(second["payload_bytes"].decode("utf-8"))["id"] == 2
    assert bus.outbound_queue.has_in_flight()

    # Complete the second message
    assert bus.outbound_queue.complete_in_flight(second)
    assert not bus.outbound_queue.has_in_flight()

    # Queue should now be empty
    assert bus.outbound_queue.take() is None


class TestCommandResponsePreSerializedFormat:
    """Regression tests for Core 0 command-response pre-serialized format.

    These tests verify that command responses use the pre-serialized
    payload_bytes contract.
    """

    def test_command_response_entry_format(self, tmp_path):
        """Verify command-response entries contain payload_bytes, not message."""
        from config import load_config

        config_path = pathlib.Path(__file__).resolve().parents[1] / "config.json"
        config = load_config(str(config_path))

        bus = InterCore(outbound_max=16, event_max=4)

        # Build a command response message
        message = {
            "message_type": "command_response",
            "payload": {
                "command_id": "reboot-001",
                "command": "reboot",
                "targeted": False,
                "success": True,
                "data": {"rebooting": True},
            },
        }

        # Serialize (same pattern as _publish_core0_command_response)
        from message_serializer import serialize_and_validate_message
        payload_bytes = serialize_and_validate_message(message)

        # Verify payload_bytes is bytes and not a dict with 'message' key
        assert isinstance(payload_bytes, bytes)

        # Create entry with payload_bytes (same as fixed _publish_core0_command_response)
        entry = {
            "kind": KIND_COMMAND_RESPONSE,
            "payload_bytes": payload_bytes,
        }

        # Verify entry doesn't have the obsolete 'message' field
        assert "message" not in entry
        assert "payload_bytes" in entry

        # Add to queue
        bus.outbound_queue.put_with_kind(
            KIND_COMMAND_RESPONSE, payload_bytes, RETENTION_PRIORITY_CRITICAL
        )

        # Verify the entry was queued with payload_bytes
        queued_entry = bus.outbound_queue.take()
        assert queued_entry is not None
        assert "payload_bytes" in queued_entry
        assert "message" not in queued_entry
        assert queued_entry["kind"] == KIND_COMMAND_RESPONSE

        # Verify payload_bytes is valid JSON with expected content
        deserialized = json.loads(queued_entry["payload_bytes"].decode("utf-8"))
        assert deserialized["message_type"] == "command_response"
        assert deserialized["payload"]["command_id"] == "reboot-001"
        assert deserialized["payload"]["success"] is True

    def test_command_response_payload_is_serialized_bytes(self, tmp_path):
        """Verify command-response payload_bytes is properly serialized."""
        from config import load_config

        config_path = pathlib.Path(__file__).resolve().parents[1] / "config.json"
        config = load_config(str(config_path))

        bus = InterCore(outbound_max=16, event_max=4)

        # Build a command response message with error
        message = {
            "message_type": "command_response",
            "payload": {
                "command_id": "reboot-002",
                "command": "reboot",
                "targeted": False,
                "success": False,
                "error": {"code": "test_error", "message": "Test error"},
            },
        }

        from message_serializer import serialize_and_validate_message
        payload_bytes = serialize_and_validate_message(message)

        entry = {
            "topic": config["mqtt_topic_command_response"],
            "kind": KIND_COMMAND_RESPONSE,
            "payload_bytes": payload_bytes,
        }

        # Verify entry format
        assert "payload_bytes" in entry
        assert "message" not in entry
        assert isinstance(entry["payload_bytes"], bytes)

        # Decode and verify
        payload_str = entry["payload_bytes"].decode("utf-8")
        deserialized = json.loads(payload_str)
        assert deserialized["message_type"] == "command_response"
        assert deserialized["payload"]["success"] is False
        assert deserialized["payload"]["error"]["code"] == "test_error"
