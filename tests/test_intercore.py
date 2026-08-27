# Host-side behavioral tests for the three-lane transport.

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from intercore import (
    InterCore,
    KIND_TELEMETRY,
    KIND_COMMAND_RESPONSE,
    RETENTION_PRIORITY_CRITICAL,
    RETENTION_PRIORITY_ERROR,
    RETENTION_PRIORITY_TELEMETRY,
    RETENTION_PRIORITY_INFO,
    RETENTION_PRIORITY_HEALTH,
)


def put(bus, kind, message, priority):
    return bus.outbound_queue.put(kind, message, priority)


def drain(bus):
    values = []
    while True:
        entry = bus.outbound_queue.take()
        if entry is None:
            break
        values.append(entry["message"]["id"])
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
    assert first["message"]["id"] == 1

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
