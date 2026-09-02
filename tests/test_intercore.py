# test_intercore.py - Tests for the four-lane inter-core bus
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""
Host-side tests for the inter-core bus.

Both FIFO lanes are heap-governed: admission is decided against the global minimum free-heap reserve, with gc.collect() run only on the pressure path and (only the outbound queue) evicting the least-important eligible entries. The tests exercise that policy through a fake heap standing in for gc.mem_free()/gc.collect(), which CPython does not provide.
"""

import gc
import json
import pathlib
import sys
from unittest.mock import MagicMock

import pytest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.modules.setdefault("machine", MagicMock())

from intercore import (  # noqa: E402
    RETENTION_PRIORITY_CRITICAL,
    RETENTION_PRIORITY_ERROR,
    RETENTION_PRIORITY_HEALTH,
    RETENTION_PRIORITY_INFO,
    RETENTION_PRIORITY_TELEMETRY,
    ConfigUpdateLane,
    InterCore,
    InterCoreEventQueue,
    KIND_COMMAND_RESPONSE,
    KIND_HEALTH,
    KIND_TELEMETRY,
    OutboundQueue,
    StateMailboxes,
)
from message_serializer import MAX_OUTBOUND_MESSAGE_BYTES  # noqa: E402


# Pico W reserve, and a heap with plenty of headroom over it.
RESERVE = 65536
HEAPY = 256 * 1024
KB = 1024


class FakeHeap:
    """Host-side stand-in for the MicroPython heap seen through gc.

    garbage_bytes models collectable garbage: the first gc.collect() that runs while garbage remains releases it. Evicted queue entries release their payload bytes immediately (reference counting), matching MicroPython's refcounted heap. alloc_per_entry models the append's own allocations (the entry dict and list growth), so a mem_free() measured after an append sees them."""

    def __init__(self, free_bytes, garbage_bytes=0, alloc_per_entry=0):
        self.free_bytes = free_bytes
        self._garbage = garbage_bytes
        self.collects = 0
        self.alloc_per_entry = alloc_per_entry

    def mem_free(self):
        return self.free_bytes

    def collect(self):
        self.collects += 1
        if self._garbage:
            self.free_bytes += self._garbage
            self._garbage = 0

    def install(self, monkeypatch, queue):
        # gc.mem_free does not exist on the CPython host; add it for the test.
        def mem_free():
            return self.mem_free() - self.alloc_per_entry * len(queue._queue)

        monkeypatch.setattr(gc, "mem_free", mem_free, raising=False)
        monkeypatch.setattr(gc, "collect", self.collect)
        original_evict = queue._evict_oldest_by_priority_locked

        def evict_and_release(priority):
            before = queue._queued_bytes
            evicted = original_evict(priority)
            if evicted:
                self.free_bytes += before - queue._queued_bytes
            return evicted

        monkeypatch.setattr(queue, "_evict_oldest_by_priority_locked", evict_and_release)


def _queue(monkeypatch, free_bytes=HEAPY, garbage_bytes=0, alloc_per_entry=0):
    """An InterCore bus whose fake heap starts at free_bytes (each retained entry optionally costing alloc_per_entry)."""
    ic = InterCore(RESERVE)
    heap = FakeHeap(free_bytes, garbage_bytes, alloc_per_entry)
    heap.install(monkeypatch, ic.outbound_queue)
    return ic, heap


def _assert_watermark_at_least_depth(queue):
    """Peak-depth invariant: the high watermark and the depth use the same
    retained-entry definition (queued + in-flight), so the historical peak
    can never be below the current depth."""
    status = queue.status()
    assert status["high_watermark"] >= status["depth"]


# ---------------------------------------------------------------------------
# Construction and configuration
# ---------------------------------------------------------------------------


def test_intercore_requires_positive_reserve():
    for bad in (0, -1, "64", 1.5, True):
        with pytest.raises(ValueError):
            InterCore(bad)


def test_outbound_requires_positive_reserve():
    lock = InterCore(RESERVE)._heap_admission_lock
    for bad in (0, -1, "64", True):
        with pytest.raises(ValueError):
            OutboundQueue(bad, lock)


def test_event_queue_requires_positive_reserve():
    lock = InterCore(RESERVE)._heap_admission_lock
    for bad in (0, -1, "64", True):
        with pytest.raises(ValueError):
            InterCoreEventQueue(bad, lock)


def test_facade_exposes_three_lanes_and_reserve():
    ic = InterCore(RESERVE)
    assert ic.minimum_free_heap_bytes == RESERVE
    assert isinstance(ic.outbound_queue, OutboundQueue)
    assert isinstance(ic.event_queue, InterCoreEventQueue)
    assert isinstance(ic.state_mailboxes, StateMailboxes)


# ---------------------------------------------------------------------------
# Shared heap-admission lock
# ---------------------------------------------------------------------------


def test_both_queues_share_one_heap_admission_lock():
    ic = InterCore(RESERVE)
    lock = ic.outbound_queue._heap_admission_lock
    assert lock is ic.event_queue._heap_admission_lock
    assert lock is ic._heap_admission_lock
    # It is a distinct lock from each queue's internal list lock.
    assert lock is not ic.outbound_queue._lock
    assert lock is not ic.event_queue._lock


def test_heap_lock_held_during_admission(monkeypatch):
    ic, heap = _queue(monkeypatch)
    lock = ic._heap_admission_lock
    observed = []
    monkeypatch.setattr(
        gc, "mem_free", lambda: observed.append(lock.locked()) or heap.mem_free()
    )
    assert ic.outbound_queue.put(KIND_TELEMETRY, {"v": 1}, RETENTION_PRIORITY_TELEMETRY) is True
    # Every heap measurement was taken under the shared lock...
    assert observed and all(observed)
    # ...and the lock is released when admission returns.
    assert lock.locked() is False


def test_heap_lock_released_on_rejection(monkeypatch):
    ic, heap = _queue(monkeypatch, free_bytes=0)
    lock = ic._heap_admission_lock
    observed = []
    monkeypatch.setattr(
        gc, "mem_free", lambda: observed.append(lock.locked()) or heap.mem_free()
    )
    assert ic.outbound_queue.put(KIND_TELEMETRY, {"v": 1}, RETENTION_PRIORITY_TELEMETRY) is False
    assert observed and all(observed)
    assert lock.locked() is False
    # The lock is still usable after the rejected admission.
    lock.acquire()
    lock.release()


def test_heap_lock_released_on_exception(monkeypatch):
    ic, heap = _queue(monkeypatch)
    lock = ic._heap_admission_lock
    queue = ic.outbound_queue

    def _boom(*args):
        raise RuntimeError("append failed")

    monkeypatch.setattr(queue, "_append_locked", _boom)
    with pytest.raises(RuntimeError):
        queue.put(KIND_TELEMETRY, {"v": 1}, RETENTION_PRIORITY_TELEMETRY)
    assert lock.locked() is False
    lock.acquire()
    lock.release()


# ---------------------------------------------------------------------------
# Outbound admission: put()
# ---------------------------------------------------------------------------


def test_put_non_dict_rejected(monkeypatch):
    ic, _ = _queue(monkeypatch)
    with pytest.raises(ValueError):
        ic.outbound_queue.put(KIND_TELEMETRY, "not a dict", RETENTION_PRIORITY_TELEMETRY)


def test_put_rejects_invalid_kind(monkeypatch):
    ic, _ = _queue(monkeypatch)
    with pytest.raises(ValueError):
        ic.outbound_queue.put("bogus", {"v": 1}, RETENTION_PRIORITY_TELEMETRY)


def test_put_rejects_invalid_priority(monkeypatch):
    ic, _ = _queue(monkeypatch)
    for bad in (True, 5, 80, "10"):
        with pytest.raises(ValueError):
            ic.outbound_queue.put(KIND_TELEMETRY, {"v": 1}, bad)


def test_put_serializes(monkeypatch):
    ic, _ = _queue(monkeypatch)
    queue = ic.outbound_queue
    assert queue.put(KIND_TELEMETRY, {"value": 42, "label": "x"}, RETENTION_PRIORITY_TELEMETRY) is True
    entry = queue.take()
    assert json.loads(entry["payload_bytes"]) == {"value": 42, "label": "x"}
    assert entry["retention_priority"] == RETENTION_PRIORITY_TELEMETRY


def test_put_rejects_non_string_keys(monkeypatch):
    ic, _ = _queue(monkeypatch)
    with pytest.raises(ValueError):
        ic.outbound_queue.put(KIND_TELEMETRY, {1: "x"}, RETENTION_PRIORITY_TELEMETRY)


def test_put_rejects_nan(monkeypatch):
    ic, _ = _queue(monkeypatch)
    with pytest.raises(ValueError):
        ic.outbound_queue.put(KIND_TELEMETRY, {"v": float("nan")}, RETENTION_PRIORITY_TELEMETRY)


def test_put_rejects_infinity(monkeypatch):
    ic, _ = _queue(monkeypatch)
    with pytest.raises(ValueError):
        ic.outbound_queue.put(KIND_TELEMETRY, {"v": float("inf")}, RETENTION_PRIORITY_TELEMETRY)


def test_put_rejects_unsupported_type(monkeypatch):
    ic, _ = _queue(monkeypatch)
    with pytest.raises(ValueError):
        ic.outbound_queue.put(KIND_TELEMETRY, {"v": object()}, RETENTION_PRIORITY_TELEMETRY)


def test_put_memoryerror_from_serialization_propagates(monkeypatch):
    import message_serializer

    ic, _ = _queue(monkeypatch)

    def _exhaust(*args, **kwargs):
        raise MemoryError

    monkeypatch.setattr(message_serializer, "serialize_and_validate_message", _exhaust)
    with pytest.raises(MemoryError):
        ic.outbound_queue.put(KIND_TELEMETRY, {"v": 1}, RETENTION_PRIORITY_TELEMETRY)


def test_put_oversized_message_raises(monkeypatch):
    """Oversize is a permanent failure of the message, not a transient
    queue rejection: put() raises ValueError so a caller can distinguish
    it from the False (retry later) return."""
    ic, _ = _queue(monkeypatch)
    queue = ic.outbound_queue
    big = {"blob": "x" * (MAX_OUTBOUND_MESSAGE_BYTES + 1)}
    with pytest.raises(ValueError):
        queue.put(KIND_TELEMETRY, big, RETENTION_PRIORITY_TELEMETRY)
    assert queue.get_depth() == 0
    assert queue.status()["oversized_rejected"] == 1


def test_more_than_sixteen_small_messages_retained(monkeypatch):
    """No fixed entry count: 20 small messages are all retained."""
    ic, _ = _queue(monkeypatch)
    queue = ic.outbound_queue
    for i in range(20):
        payload = json.dumps({"i": i}).encode("utf-8")
        assert queue.put_with_kind(KIND_TELEMETRY, payload, RETENTION_PRIORITY_TELEMETRY) is True
    assert queue.get_depth() == 20
    status = queue.status()
    assert status["pending"] == 20
    assert status["high_watermark"] == 20
    assert status["queued_bytes"] == sum(len(json.dumps({"i": i})) for i in range(20))


def test_fifo_order(monkeypatch):
    ic, _ = _queue(monkeypatch)
    queue = ic.outbound_queue
    for i in range(3):
        assert queue.put_with_kind(KIND_TELEMETRY, json.dumps({"i": i}).encode(), RETENTION_PRIORITY_TELEMETRY)
    for i in range(3):
        entry = queue.take()
        assert json.loads(entry["payload_bytes"]) == {"i": i}
        assert queue.complete_in_flight(entry) is True
        _assert_watermark_at_least_depth(queue)


# ---------------------------------------------------------------------------
# Outbound admission: the heap-reserve policy
# ---------------------------------------------------------------------------


def test_fast_path_admits_without_gc(monkeypatch):
    ic, heap = _queue(monkeypatch)
    assert ic.outbound_queue.put(KIND_TELEMETRY, {"v": 1}, RETENTION_PRIORITY_TELEMETRY) is True
    assert heap.collects == 0


def test_pressure_path_runs_gc_once_and_admits(monkeypatch):
    """Heap below the reserve with collectable garbage: gc alone restores it."""
    ic, heap = _queue(monkeypatch, free_bytes=RESERVE - 4096, garbage_bytes=8192)
    assert ic.outbound_queue.put(KIND_TELEMETRY, {"v": 1}, RETENTION_PRIORITY_TELEMETRY) is True
    assert heap.collects == 1
    assert heap.mem_free() >= RESERVE


def test_gc_restores_reserve_without_eviction(monkeypatch):
    ic, heap = _queue(monkeypatch)
    queue = ic.outbound_queue
    assert queue.put_with_kind(KIND_TELEMETRY, b'{"a":1}', RETENTION_PRIORITY_TELEMETRY) is True
    # The heap drops below the reserve, leaving collectable garbage.
    heap.free_bytes = RESERVE - 2048
    heap._garbage = 4096
    assert queue.put_with_kind(KIND_TELEMETRY, b'{"b":2}', RETENTION_PRIORITY_TELEMETRY) is True
    status = queue.status()
    assert status["pending"] == 2
    assert status["messages_evicted"] == 0
    assert status["messages_rejected"] == 0


def test_rejected_when_reserve_cannot_be_restored(monkeypatch):
    """Empty queue, heap below reserve and no garbage: admission is rejected."""
    ic, heap = _queue(monkeypatch, free_bytes=0)
    queue = ic.outbound_queue
    assert queue.put_with_kind(KIND_TELEMETRY, b'{"a":1}', RETENTION_PRIORITY_TELEMETRY) is False
    status = queue.status()
    assert status["pending"] == 0
    assert status["messages_rejected"] == 1
    assert status["messages_evicted"] == 0


def test_post_admission_invariant_undoes_a_crossing_append(monkeypatch):
    """The reserve is a post-admission invariant: the append itself allocates
    (the entry dict, list growth), and if that crosses the reserve the
    admission is undone and rejected -- not admitted at a pre-admission check."""
    # 512 B of headroom, but the append costs 1 KiB: below the reserve
    # before the append, under it once the entry is retained.
    ic, heap = _queue(monkeypatch, free_bytes=RESERVE + 512, alloc_per_entry=1024)
    queue = ic.outbound_queue
    assert (
        queue.put_with_kind(KIND_TELEMETRY, b'{"a":1}', RETENTION_PRIORITY_TELEMETRY) is False
    )
    status = queue.status()
    assert status["pending"] == 0
    assert status["queued_bytes"] == 0
    assert status["high_watermark"] == 0
    assert status["high_watermark_bytes"] == 0
    assert status["messages_rejected"] == 1
    assert status["messages_evicted"] == 0
    # The undo was reclaimed, so the heap is measurable and back above the
    # reserve with nothing retained by the queue.
    assert heap.collects == 1
    assert gc.mem_free() >= RESERVE


def test_post_admission_invariant_admits_at_the_reserve(monkeypatch):
    """Boundary control: the append allocates, but the reserve holds with the
    entry retained (at, not above, the reserve) -- admitted."""
    ic, heap = _queue(monkeypatch, free_bytes=RESERVE + 1024, alloc_per_entry=1024)
    queue = ic.outbound_queue
    assert (
        queue.put_with_kind(KIND_TELEMETRY, b'{"a":1}', RETENTION_PRIORITY_TELEMETRY) is True
    )
    status = queue.status()
    assert status["pending"] == 1
    assert status["high_watermark"] == 1
    assert status["messages_rejected"] == 0


def test_post_admission_invariant_holds_after_eviction(monkeypatch):
    """The invariant also governs the pressure path: an admission whose own
    allocations cross the reserve is undone even though eviction just
    restored it -- the heap cannot retain the evicted entry either."""
    # 6 KiB short of the reserve; the queued HEALTH entry releases 8 KiB on
    # eviction (+2 KiB of headroom), but the append costs 4 KiB and crosses.
    ic, heap = _queue(monkeypatch, alloc_per_entry=4 * KB)
    queue = ic.outbound_queue
    assert (
        queue.put_with_kind(KIND_HEALTH, b"h" * 8 * KB, RETENTION_PRIORITY_HEALTH) is True
    )
    heap.free_bytes = RESERVE - 6 * KB
    assert (
        queue.put_with_kind(KIND_TELEMETRY, b"t" * 1 * KB, RETENTION_PRIORITY_TELEMETRY) is False
    )
    status = queue.status()
    assert status["pending"] == 0
    assert status["queued_bytes"] == 0
    assert status["messages_evicted"] == 1
    assert status["messages_rejected"] == 1
    assert gc.mem_free() >= RESERVE


def test_append_crossing_displaces_lower_priority_entry(monkeypatch):
    """An append-induced reserve crossing is memory pressure too: a CRITICAL
    admission whose own allocations cross the reserve displaces the queued
    lower-priority entry instead of being rejected while it stays retained
    (pre-admission heap above the reserve)."""
    # Headroom covers the queued HEALTH entry, but not the incoming entry's
    # own allocations as well: the fast-path append crosses and is undone.
    ic, heap = _queue(monkeypatch, free_bytes=RESERVE + 1536, alloc_per_entry=1024)
    queue = ic.outbound_queue
    assert queue.put_with_kind(KIND_HEALTH, b"h" * 8 * KB, RETENTION_PRIORITY_HEALTH) is True
    assert (
        queue.put_with_kind(
            KIND_COMMAND_RESPONSE, b'{"r":1}', RETENTION_PRIORITY_CRITICAL
        )
        is True
    )
    status = queue.status()
    assert status["pending"] == 1
    assert status["queued_bytes"] == len(b'{"r":1}')
    assert status["messages_evicted"] == 1
    assert status["telemetry_evicted"] == 0
    assert status["messages_rejected"] == 0
    taken = queue.take()
    assert taken["payload_bytes"] == b'{"r":1}'
    # The displacement restored the post-admission invariant.
    assert gc.mem_free() >= RESERVE


def test_append_crossing_still_rejects_lower_priority_incoming(monkeypatch):
    """The displacement path keeps its priority rule on the append-crossing
    fallthrough: an incoming entry less important than everything queued is
    rejected, and nothing more important is evicted."""
    ic, heap = _queue(monkeypatch, free_bytes=RESERVE + 1536, alloc_per_entry=1024)
    queue = ic.outbound_queue
    assert (
        queue.put_with_kind(KIND_TELEMETRY, b"t" * 8 * KB, RETENTION_PRIORITY_TELEMETRY)
        is True
    )
    assert queue.put_with_kind(KIND_HEALTH, b'{"h":1}', RETENTION_PRIORITY_HEALTH) is False
    status = queue.status()
    assert status["pending"] == 1
    assert status["queued_bytes"] == len(b"t" * 8 * KB)
    assert status["messages_evicted"] == 0
    assert status["messages_rejected"] == 1


def test_critical_evicts_lower_priority_until_reserve_restored(monkeypatch):
    ic, heap = _queue(monkeypatch)
    queue = ic.outbound_queue
    telemetry_payload = b"t" * 12 * KB
    health_payload = b"h" * 8 * KB
    assert queue.put_with_kind(KIND_TELEMETRY, telemetry_payload, RETENTION_PRIORITY_TELEMETRY) is True
    assert queue.put_with_kind(KIND_HEALTH, health_payload, RETENTION_PRIORITY_HEALTH) is True
    # Pressure: 8 KiB short of the reserve, no garbage to collect.
    heap.free_bytes = RESERVE - 8 * KB
    incoming = b"c" * 4 * KB
    assert queue.put_with_kind(KIND_TELEMETRY, incoming, RETENTION_PRIORITY_CRITICAL) is True
    status = queue.status()
    # The least-important entry (HEALTH, 8 KiB) was evicted; TELEMETRY kept.
    assert status["pending"] == 2
    assert status["messages_evicted"] == 1
    assert status["telemetry_evicted"] == 0
    assert status["messages_rejected"] == 0
    first = queue.take()
    assert first["payload_bytes"] == telemetry_payload
    assert queue.complete_in_flight(first) is True
    second = queue.take()
    assert second["payload_bytes"] == incoming
    _assert_watermark_at_least_depth(queue)


def test_multiple_evictions_allowed(monkeypatch):
    ic, heap = _queue(monkeypatch)
    queue = ic.outbound_queue
    for kind, priority in (
        (KIND_TELEMETRY, RETENTION_PRIORITY_TELEMETRY),
        (KIND_TELEMETRY, RETENTION_PRIORITY_INFO),
        (KIND_HEALTH, RETENTION_PRIORITY_HEALTH),
    ):
        assert queue.put_with_kind(kind, b"x" * 8 * KB, priority) is True
    # 16 KiB short: exactly two 8 KiB evictions restore the reserve.
    heap.free_bytes = RESERVE - 16 * KB
    assert queue.put_with_kind(KIND_TELEMETRY, b"c" * 2 * KB, RETENTION_PRIORITY_CRITICAL) is True
    status = queue.status()
    assert status["pending"] == 2  # original TELEMETRY + incoming CRITICAL
    assert status["messages_evicted"] == 2
    assert status["telemetry_evicted"] == 1  # only the TELEMETRY-kind one
    assert status["messages_rejected"] == 0
    _assert_watermark_at_least_depth(queue)


def test_less_important_cannot_evict_more_important(monkeypatch):
    ic, heap = _queue(monkeypatch)
    queue = ic.outbound_queue
    assert queue.put_with_kind(KIND_TELEMETRY, b"t" * 8 * KB, RETENTION_PRIORITY_TELEMETRY) is True
    heap.free_bytes = 0  # unrecoverable pressure
    assert (
        queue.put_with_kind(KIND_HEALTH, b"h" * 8 * KB, RETENTION_PRIORITY_HEALTH) is False
    )
    status = queue.status()
    assert status["pending"] == 1
    assert status["messages_rejected"] == 1
    assert status["messages_evicted"] == 0
    assert queue.take()["payload_bytes"] == b"t" * 8 * KB


def test_equal_priority_can_evict(monkeypatch):
    """An incoming entry may displace queued entries of the same priority."""
    ic, heap = _queue(monkeypatch)
    queue = ic.outbound_queue
    assert queue.put_with_kind(KIND_TELEMETRY, b"o" * 1024, RETENTION_PRIORITY_TELEMETRY) is True
    heap.free_bytes = RESERVE - 1024
    assert (
        queue.put_with_kind(KIND_TELEMETRY, b"new", RETENTION_PRIORITY_TELEMETRY) is True
    )
    assert queue.status()["pending"] == 1
    assert queue.status()["messages_evicted"] == 1
    assert queue.take()["payload_bytes"] == b"new"


def test_critical_cannot_evict_existing_critical(monkeypatch):
    """CRITICAL is a non-evictable retention floor: an admitted CRITICAL entry
    (a command response) cannot be displaced by another CRITICAL, so the
    incoming entry is rejected for the producer to retain and retry instead."""
    ic, heap = _queue(monkeypatch)
    queue = ic.outbound_queue
    existing = b'{"id":"response-a"}'
    assert (
        queue.put_with_kind(KIND_COMMAND_RESPONSE, existing, RETENTION_PRIORITY_CRITICAL)
        is True
    )
    heap.free_bytes = 0  # unrecoverable pressure
    assert (
        queue.put_with_kind(
            KIND_COMMAND_RESPONSE, b'{"id":"response-b"}', RETENTION_PRIORITY_CRITICAL
        )
        is False
    )
    status = queue.status()
    assert status["pending"] == 1
    assert status["queued_bytes"] == len(existing)
    assert status["messages_evicted"] == 0
    assert status["messages_rejected"] == 1
    # The retained entry is the one admitted first.
    assert queue.take()["payload_bytes"] == existing


def test_critical_rejection_retains_original_payload(monkeypatch):
    """Newest-CRITICAL-wins is not an acceptable implementation: the original
    admitted response survives a rejected same-priority admission unchanged."""
    ic, heap = _queue(monkeypatch)
    queue = ic.outbound_queue
    existing = b'{"id":"response-a"}'
    incoming = b'{"id":"response-b"}'
    assert (
        queue.put_with_kind(KIND_COMMAND_RESPONSE, existing, RETENTION_PRIORITY_CRITICAL)
        is True
    )
    heap.free_bytes = 0  # unrecoverable pressure
    assert (
        queue.put_with_kind(KIND_COMMAND_RESPONSE, incoming, RETENTION_PRIORITY_CRITICAL)
        is False
    )
    retained = queue.take()
    assert retained["payload_bytes"] == existing
    assert retained["payload_bytes"] != incoming
    # The FIFO is empty: the rejected entry was never inserted.
    assert queue.get_depth() == 1  # only the in-flight entry taken above


def test_lower_priority_cannot_evict_critical(monkeypatch):
    """A lower-priority incoming entry is rejected while a CRITICAL entry is
    queued, even though equal-priority replacement applies to the replaceable
    lower priorities (the invariant CRITICAL never evicts CRITICAL)."""
    ic, heap = _queue(monkeypatch)
    queue = ic.outbound_queue
    existing = b'{"id":"response-a"}'
    assert (
        queue.put_with_kind(KIND_COMMAND_RESPONSE, existing, RETENTION_PRIORITY_CRITICAL)
        is True
    )
    heap.free_bytes = 0  # unrecoverable pressure
    assert (
        queue.put_with_kind(KIND_TELEMETRY, b"t" * 8 * KB, RETENTION_PRIORITY_TELEMETRY)
        is False
    )
    status = queue.status()
    assert status["pending"] == 1
    assert status["queued_bytes"] == len(existing)
    assert status["messages_evicted"] == 0
    assert status["messages_rejected"] == 1
    assert queue.take()["payload_bytes"] == existing


def test_critical_evicts_lower_then_rejects_rather_than_evicting_critical(monkeypatch):
    """CRITICAL may displace lower-priority entries, but once nothing but
    CRITICAL entries remains, the incoming entry is rejected rather than
    evicting an admitted CRITICAL one."""
    ic, heap = _queue(monkeypatch)
    queue = ic.outbound_queue
    existing = b'{"id":"response-a"}'
    assert (
        queue.put_with_kind(KIND_COMMAND_RESPONSE, existing, RETENTION_PRIORITY_CRITICAL)
        is True
    )
    assert (
        queue.put_with_kind(KIND_TELEMETRY, b"t" * 8 * KB, RETENTION_PRIORITY_TELEMETRY)
        is True
    )
    # 20 KiB short: evicting the 8 KiB telemetry entry is not enough to
    # restore the reserve, and the CRITICAL entry must not be displaced.
    heap.free_bytes = RESERVE - 20 * KB
    assert (
        queue.put_with_kind(
            KIND_COMMAND_RESPONSE, b'{"id":"response-b"}', RETENTION_PRIORITY_CRITICAL
        )
        is False
    )
    status = queue.status()
    assert status["pending"] == 1
    assert status["queued_bytes"] == len(existing)
    assert status["messages_evicted"] == 1  # only the lower-priority entry
    assert status["telemetry_evicted"] == 1
    assert status["messages_rejected"] == 1
    assert queue.take()["payload_bytes"] == existing


def test_eviction_is_oldest_first_within_priority_class(monkeypatch):
    ic, heap = _queue(monkeypatch)
    queue = ic.outbound_queue
    payloads = {i: b"t%d" % i + b"p" * (8 * KB - 2) for i in range(3)}
    for i in range(3):
        assert queue.put_with_kind(KIND_TELEMETRY, payloads[i], RETENTION_PRIORITY_TELEMETRY) is True
    # 16 KiB short: the two oldest same-priority entries must be the evicted ones.
    heap.free_bytes = RESERVE - 16 * KB
    assert queue.put_with_kind(KIND_TELEMETRY, b"c", RETENTION_PRIORITY_CRITICAL) is True
    first = queue.take()
    assert first["payload_bytes"] == payloads[2]  # youngest original survived
    assert queue.complete_in_flight(first) is True
    second = queue.take()
    assert second["payload_bytes"] == b"c"         # incoming admitted last
    assert queue.status()["telemetry_evicted"] == 2
    assert queue.status()["messages_evicted"] == 2


def test_in_flight_entry_is_never_evicted(monkeypatch):
    ic, heap = _queue(monkeypatch)
    queue = ic.outbound_queue
    assert queue.put_with_kind(KIND_TELEMETRY, b"t" * 16 * KB, RETENTION_PRIORITY_TELEMETRY) is True
    in_flight = queue.take()
    assert queue.has_in_flight() is True
    _assert_watermark_at_least_depth(queue)
    # Unrecoverable pressure and an empty FIFO: only the in-flight entry
    # exists, and it is not an eviction candidate.
    heap.free_bytes = 0
    assert (
        queue.put_with_kind(KIND_TELEMETRY, b"c" * 4 * KB, RETENTION_PRIORITY_CRITICAL) is False
    )
    assert queue.has_in_flight() is True
    assert queue.status()["messages_evicted"] == 0
    assert queue.status()["messages_rejected"] == 1
    _assert_watermark_at_least_depth(queue)
    assert queue.complete_in_flight(in_flight) is True
    assert in_flight["payload_bytes"] == b"t" * 16 * KB
    _assert_watermark_at_least_depth(queue)


# ---------------------------------------------------------------------------
# Outbound in-flight accounting
# ---------------------------------------------------------------------------


def test_take_empty_queue(monkeypatch):
    ic, _ = _queue(monkeypatch)
    assert ic.outbound_queue.take() is None


def test_take_returns_in_flight_again(monkeypatch):
    ic, _ = _queue(monkeypatch)
    queue = ic.outbound_queue
    assert queue.put_with_kind(KIND_TELEMETRY, b'{"a":1}', RETENTION_PRIORITY_TELEMETRY) is True
    entry = queue.take()
    assert queue.take() is entry
    _assert_watermark_at_least_depth(queue)


def test_complete_in_flight_releases_retained_bytes(monkeypatch):
    ic, _ = _queue(monkeypatch)
    queue = ic.outbound_queue
    payload = b'{"a":1}'
    assert queue.put_with_kind(KIND_TELEMETRY, payload, RETENTION_PRIORITY_TELEMETRY) is True
    entry = queue.take()
    # Retained in flight: still counted.
    assert queue.status()["queued_bytes"] == len(payload)
    assert queue.get_depth() == 1
    _assert_watermark_at_least_depth(queue)
    assert queue.complete_in_flight(entry) is True
    assert queue.status()["queued_bytes"] == 0
    assert queue.get_depth() == 0
    _assert_watermark_at_least_depth(queue)


def test_complete_in_flight_wrong_entry_rejected(monkeypatch):
    ic, _ = _queue(monkeypatch)
    queue = ic.outbound_queue
    assert queue.put_with_kind(KIND_TELEMETRY, b'{"a":1}', RETENTION_PRIORITY_TELEMETRY) is True
    assert queue.complete_in_flight({"other": True}) is False


def test_has_in_flight_empty(monkeypatch):
    ic, _ = _queue(monkeypatch)
    assert ic.outbound_queue.has_in_flight() is False


def test_get_depth_empty(monkeypatch):
    ic, _ = _queue(monkeypatch)
    assert ic.outbound_queue.get_depth() == 0


def test_high_watermark_tracks_peak(monkeypatch):
    ic, _ = _queue(monkeypatch)
    queue = ic.outbound_queue
    for i in range(5):
        assert queue.put_with_kind(KIND_TELEMETRY, b"p", RETENTION_PRIORITY_TELEMETRY) is True
    assert queue.status()["high_watermark"] == 5
    _assert_watermark_at_least_depth(queue)
    for _ in range(5):
        queue.complete_in_flight(queue.take())
    assert queue.status()["high_watermark"] == 5  # watermark is a peak, not current
    assert queue.status()["high_watermark_bytes"] >= 5
    _assert_watermark_at_least_depth(queue)


def test_high_watermark_counts_in_flight_like_depth(monkeypatch):
    """The high watermark uses the same retained-entry definition as depth
    (queued + in-flight): an admission made while an entry is in flight
    counts both, so the runtime state depth=2 with a peak of 1 cannot occur."""
    ic, _ = _queue(monkeypatch)
    queue = ic.outbound_queue
    assert queue.put_with_kind(KIND_TELEMETRY, b'{"a":1}', RETENTION_PRIORITY_TELEMETRY) is True
    in_flight = queue.take()
    assert queue.put_with_kind(KIND_TELEMETRY, b'{"b":2}', RETENTION_PRIORITY_TELEMETRY) is True
    status = queue.status()
    assert status["depth"] == 2
    assert status["high_watermark"] == 2
    _assert_watermark_at_least_depth(queue)
    assert queue.complete_in_flight(in_flight) is True
    _assert_watermark_at_least_depth(queue)


# ---------------------------------------------------------------------------
# put_with_kind()
# ---------------------------------------------------------------------------


def test_put_with_kind_non_bytes_rejected(monkeypatch):
    ic, _ = _queue(monkeypatch)
    with pytest.raises(ValueError):
        ic.outbound_queue.put_with_kind(KIND_TELEMETRY, "not bytes", RETENTION_PRIORITY_TELEMETRY)


def test_put_with_kind_rejects_oversized_payload(monkeypatch):
    """The 16 KiB per-message ceiling holds on the pre-serialized path too."""
    ic, _ = _queue(monkeypatch)
    queue = ic.outbound_queue
    oversized = b"x" * (MAX_OUTBOUND_MESSAGE_BYTES + 1)
    # The same permanent-failure contract as the put() serialization path:
    # oversize raises, False is reserved for transient (heap-pressure) rejection.
    with pytest.raises(ValueError):
        queue.put_with_kind(KIND_TELEMETRY, oversized, RETENTION_PRIORITY_TELEMETRY)
    assert queue.get_depth() == 0
    assert queue.status()["oversized_rejected"] == 1


def test_memoryerror_during_admission_propagates(monkeypatch):
    ic, heap = _queue(monkeypatch)

    def _oom(*args):
        raise MemoryError

    monkeypatch.setattr(ic.outbound_queue, "_append_locked", _oom)
    with pytest.raises(MemoryError):
        ic.outbound_queue.put(KIND_TELEMETRY, {"v": 1}, RETENTION_PRIORITY_TELEMETRY)
    assert ic.outbound_queue.get_depth() == 0


# ---------------------------------------------------------------------------
# Inter-core event queue
# ---------------------------------------------------------------------------


def test_event_put_non_dict_raises(monkeypatch):
    ic, _ = _queue(monkeypatch)
    with pytest.raises(ValueError):
        ic.event_queue.put("not a dict")


def test_event_take_empty(monkeypatch):
    ic, _ = _queue(monkeypatch)
    assert ic.event_queue.take() is None


def test_event_queue_retains_more_than_four_events(monkeypatch):
    """No fixed entry count: 6 events are all retained."""
    ic, _ = _queue(monkeypatch)
    for i in range(6):
        assert ic.event_queue.put({"seq": i}) is True
    status = ic.event_queue.status()
    assert status["pending"] == 6
    assert status["high_watermark"] == 6
    assert status["rejected"] == 0


def test_event_queue_gc_restores_admission(monkeypatch):
    ic, heap = _queue(monkeypatch)
    heap.free_bytes = RESERVE - 1024
    heap._garbage = 2048
    assert ic.event_queue.put({"seq": 1}) is True
    assert heap.collects == 1


def test_event_queue_pressure_rejects_new_event(monkeypatch):
    ic, _ = _queue(monkeypatch, free_bytes=0)
    assert ic.event_queue.put({"seq": 1}) is False
    status = ic.event_queue.status()
    assert status["pending"] == 0
    assert status["rejected"] == 1


def test_event_queue_never_evicts_admitted_events(monkeypatch):
    ic, heap = _queue(monkeypatch)
    assert ic.event_queue.put({"seq": 1}) is True
    heap.free_bytes = 0  # unrecoverable pressure
    assert ic.event_queue.put({"seq": 2}) is False
    # The admitted event is untouched; the new one was rejected.
    assert ic.event_queue.take() == {"seq": 1}
    assert ic.event_queue.status()["pending"] == 0
    assert ic.event_queue.status()["rejected"] == 1


def test_event_post_admission_invariant_undoes_a_crossing_append(monkeypatch):
    """Same post-admission invariant on the event queue: the append itself may
    allocate (list growth), and an admission that crosses the reserve with the
    event retained is undone and rejected."""
    ic, heap = _queue(monkeypatch)
    eq = ic.event_queue
    # 512 B of headroom, but retaining an event costs 1 KiB.
    base = RESERVE + 512
    alloc_per_event = 1024
    monkeypatch.setattr(
        gc, "mem_free", lambda: base - alloc_per_event * len(eq._queue), raising=False
    )
    assert eq.put({"seq": 1}) is False
    status = eq.status()
    assert status["pending"] == 0
    assert status["high_watermark"] == 0
    assert status["rejected"] == 1
    assert heap.collects == 1
    assert gc.mem_free() >= RESERVE


def test_event_post_admission_invariant_admits_at_the_reserve(monkeypatch):
    """Boundary control: the reserve holds with the event retained (at, not
    above) -- admitted."""
    ic, _ = _queue(monkeypatch)
    eq = ic.event_queue
    alloc_per_event = 1024
    monkeypatch.setattr(
        gc, "mem_free", lambda: RESERVE + alloc_per_event * len(eq._queue), raising=False
    )
    assert eq.put({"seq": 1}) is True
    status = eq.status()
    assert status["pending"] == 1
    assert status["rejected"] == 0


def test_event_queue_memoryerror_propagates(monkeypatch):
    ic, _ = _queue(monkeypatch)

    def _oom():
        raise MemoryError

    monkeypatch.setattr(gc, "mem_free", _oom, raising=False)
    with pytest.raises(MemoryError):
        ic.event_queue.put({"seq": 1})


# ---------------------------------------------------------------------------
# State mailboxes
# ---------------------------------------------------------------------------


def test_state_mailboxes_default_none():
    boxes = StateMailboxes()
    assert boxes.get_network_snapshot() is None
    assert boxes.get_utc_snapshot() is None
    assert boxes.get_core_1_activity_ms() is None
    assert boxes.get_hardware() is None


def test_state_mailboxes_set_get_network():
    boxes = StateMailboxes()
    snapshot = {"ssid": "test", "ip_address": "1.2.3.4"}
    boxes.set_network_snapshot(snapshot)
    assert boxes.get_network_snapshot() is snapshot


def test_state_mailboxes_set_get_utc():
    boxes = StateMailboxes()
    snapshot = {"utc_epoch_ms": 1234567890000}
    boxes.set_utc_snapshot(snapshot)
    assert boxes.get_utc_snapshot() is snapshot


def test_state_mailboxes_replacement_semantics():
    boxes = StateMailboxes()
    first = {"v": 1}
    second = {"v": 2}
    boxes.set_network_snapshot(first)
    boxes.set_network_snapshot(second)
    assert boxes.get_network_snapshot() is second


def test_state_mailboxes_type_validation():
    boxes = StateMailboxes()
    for setter in (
        boxes.set_network_snapshot,
        boxes.set_utc_snapshot,
        boxes.set_hardware,
    ):
        with pytest.raises(ValueError):
            setter("not a dict")
    with pytest.raises(ValueError):
        boxes.set_core_1_activity_ms("not an int")
    with pytest.raises(ValueError):
        boxes.set_core_1_activity_ms(True)


def test_state_mailboxes_core_1_activity():
    boxes = StateMailboxes()
    boxes.set_core_1_activity_ms(12345)
    assert boxes.get_core_1_activity_ms() == 12345


# --- config_update_lane: request/result for the HOT_RELOADED apply -----------


def test_config_update_lane_default_empty():
    lane = ConfigUpdateLane()
    assert lane.take_request() is None
    assert lane.take_result_for(1) is None


def test_config_update_lane_request_set_take():
    lane = ConfigUpdateLane()
    request = {"generation": 1, "read_loop_sec": 40}
    lane.post_request(request)
    assert lane.take_request() is request
    assert lane.take_request() is None  # cleared on read


def test_config_update_lane_result_matched_by_generation():
    lane = ConfigUpdateLane()
    lane.post_result({"generation": 1, "success": True})
    assert lane.take_result_for(1) == {"generation": 1, "success": True}
    assert lane.take_result_for(1) is None  # consumed exactly once


def test_config_update_lane_ignores_a_stale_generation():
    lane = ConfigUpdateLane()
    # A result from a different (superseded) transaction is not read as this
    # one, and is left in place for its owner.
    lane.post_result({"generation": 9, "success": True})
    assert lane.take_result_for(1) is None
    assert lane.take_result_for(9) == {"generation": 9, "success": True}


def test_config_update_lane_replacement_semantics():
    lane = ConfigUpdateLane()
    first = {"generation": 1, "read_loop_sec": 10}
    second = {"generation": 2, "health_interval_sec": 30}
    lane.post_request(first)
    lane.post_request(second)
    assert lane.take_request() is second  # latest value wins


def test_config_update_lane_type_validation():
    lane = ConfigUpdateLane()
    with pytest.raises(ValueError):
        lane.post_request("not a dict")
    with pytest.raises(ValueError):
        lane.post_result("not a dict")


def test_intercore_exposes_config_update_lane():
    ic = InterCore(minimum_free_heap_bytes=RESERVE)
    assert isinstance(ic.config_update_lane, ConfigUpdateLane)
