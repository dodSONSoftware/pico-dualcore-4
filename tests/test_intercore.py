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

import intercore  # noqa: E402
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
from message_serializer import (  # noqa: E402
    MAX_OUTBOUND_MESSAGE_BYTES,
)


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


def _queue(monkeypatch, free_bytes=HEAPY, garbage_bytes=0, alloc_per_entry=0, minimum=RESERVE, preferred=None, max_messages=None):
    """An InterCore bus whose fake heap starts at free_bytes (each retained entry optionally costing alloc_per_entry). preferred=None means single-threshold (the floor is also the preferred reserve); pass the board's two thresholds to exercise the soft band (minimum <= free < preferred). max_messages=None leaves the count ceiling unbounded (the historical behavior); a positive integer sets it."""
    if preferred is None:
        preferred = minimum
    ic = InterCore(minimum, preferred, max_messages)
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

    ic, _ = _queue(monkeypatch)

    def _exhaust(*args, **kwargs):
        raise MemoryError

    monkeypatch.setattr(intercore, "serialize_and_validate_message", _exhaust)
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
    """Post-admission pressure: heap below the reserve with collectable
    garbage, gc.collect() alone restores it and the entry is admitted.
    Driven via put_with_kind() (already-final bytes) so it exercises the
    post-admission reserve path -- the put() serialization path separately
    gates on the working-set headroom, tested below."""
    ic, heap = _queue(monkeypatch, free_bytes=RESERVE - 4096, garbage_bytes=8192)
    assert (
        ic.outbound_queue.put_with_kind(
            KIND_TELEMETRY, b'{"v":1}', RETENTION_PRIORITY_TELEMETRY
        )
        is True
    )
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
    # reserve with nothing retained by the queue. Two collects: the
    # rolled-back append's own reclaim, then the pre-eviction re-measure's
    # retry (which crosses again -- the 512 B headroom does not cover the
    # 1 KiB append) reclaiming its own undo.
    assert heap.collects == 2
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


# ---------------------------------------------------------------------------
# Two-threshold soft band (minimum <= free < preferred)
# ---------------------------------------------------------------------------


def test_soft_band_eviction_collects_before_admitting(monkeypatch):
    """The production two-threshold soft band: an eligible entry is
    reclaimed, gc.collect() runs after the reclamation (as the
    hard-pressure path does after each displacement), the entry is
    admitted, and nothing further is evicted or rejected."""
    MIN, PREFERRED = 48 * KB, 64 * KB
    ic, heap = _queue(monkeypatch, minimum=MIN, preferred=PREFERRED)
    queue = ic.outbound_queue
    assert queue.put_with_kind(KIND_HEALTH, b"h" * 8 * KB, RETENTION_PRIORITY_HEALTH) is True
    # The heap lands in the soft band: below the preferred reserve, at the
    # floor.
    heap.free_bytes = MIN + 4 * KB
    heap.collects = 0
    assert (
        queue.put_with_kind(KIND_COMMAND_RESPONSE, b'{"r":1}', RETENTION_PRIORITY_CRITICAL)
        is True
    )
    status = queue.status()
    assert status["pending"] == 1
    assert status["queued_bytes"] == len(b'{"r":1}')
    assert status["messages_evicted"] == 1
    assert status["telemetry_evicted"] == 0
    assert status["messages_rejected"] == 0
    # Two collections: the pressure-band entry collect, then the one after
    # the reclamation (the pre-fix soft path ran only the former).
    assert heap.collects == 2
    taken = queue.take()
    assert taken["payload_bytes"] == b'{"r":1}'
    queue.complete_in_flight(taken)


def test_appends_own_reclaim_admits_without_second_eviction(monkeypatch):
    """An append whose own allocations cross the hard floor rolls back and
    reclaims its garbage; the floor is re-measured before any further
    displacement, so an entry whose rollback made it admissible is admitted
    without a transient rejection (pre-fix: the displacement loop rejected
    it because the queue had nothing eligible to evict)."""
    MIN, PREFERRED = 48 * KB, 64 * KB
    ic, heap = _queue(
        monkeypatch,
        free_bytes=PREFERRED,
        garbage_bytes=8 * KB,
        alloc_per_entry=20 * KB,
        minimum=MIN,
        preferred=PREFERRED,
    )
    queue = ic.outbound_queue
    assert (
        queue.put_with_kind(KIND_COMMAND_RESPONSE, b'{"r":1}', RETENTION_PRIORITY_CRITICAL)
        is True
    )
    status = queue.status()
    assert status["pending"] == 1
    assert status["messages_evicted"] == 0
    assert status["messages_rejected"] == 0
    taken = queue.take()
    assert taken["payload_bytes"] == b'{"r":1}'
    queue.complete_in_flight(taken)


def test_soft_band_rejects_when_nothing_eligible_and_append_crosses(monkeypatch):
    """Soft-band rejection path: nothing the incoming entry may displace is
    queued (CRITICAL floor), and the entry's own allocations cross the hard
    floor even after the reclamation -- rejected as transient, with no
    eviction and no further collection."""
    MIN, PREFERRED = 48 * KB, 64 * KB
    ic, heap = _queue(
        monkeypatch, alloc_per_entry=8 * KB, minimum=MIN, preferred=PREFERRED
    )
    queue = ic.outbound_queue
    assert (
        queue.put_with_kind(KIND_COMMAND_RESPONSE, b'{"r":1}', RETENTION_PRIORITY_CRITICAL)
        is True
    )
    # Soft band once the retained entry's allocations are counted (free -
    # 8 KiB = 52 KiB: at the floor, below the preferred reserve), and the
    # incoming entry's own allocations cross the floor.
    heap.free_bytes = MIN + 12 * KB
    heap.collects = 0
    assert (
        queue.put_with_kind(KIND_TELEMETRY, b'{"v":1}', RETENTION_PRIORITY_TELEMETRY)
        is False
    )
    status = queue.status()
    assert status["pending"] == 1
    assert status["queued_bytes"] == len(b'{"r":1}')
    assert status["messages_evicted"] == 0
    assert status["messages_rejected"] == 1
    # Three collects: the pressure-band entry collect, the soft append's
    # rollback, and the pre-eviction re-measure's retry (which crosses
    # again). The rejection itself collects nothing.
    assert heap.collects == 3


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
# Outbound admission: serialization with MemoryError recovery
#
# put() serializes the actual message -- no fixed worst-case threshold stands
# in for it. A MemoryError from the serializer first triggers gc.collect()
# (no data loss); if it persists, the queue reclaims one eligible retained
# entry at a time under the same retention policy as admission pressure (a
# lower-priority incoming may not evict a more important entry, and CRITICAL
# is never displaced), retrying after each reclamation. When no eligible
# entry remains, the MemoryError propagates to the firmware recovery boundary
# (it is not a transient rejection). put_with_kind() never serializes: its
# bytes are already final, so only the existing admission rules apply.
# ---------------------------------------------------------------------------


def test_pico_w_steady_state_small_telemetry_admitted(monkeypatch):
    """The real Pico W condition: ~85 KiB free heap against the 64 KiB
    reserve, an empty queue, a small telemetry message. put() must admit it --
    the old fixed gate demanded reserve + 48 KiB (≈ 112 KiB) of free heap
    before the serializer was even allowed to run, so this steady state was
    rejected indefinitely on supported hardware."""
    ic, heap = _queue(monkeypatch, free_bytes=85 * KB)
    queue = ic.outbound_queue
    assert (
        queue.put(KIND_TELEMETRY, {"v": 1}, RETENTION_PRIORITY_TELEMETRY) is True
    )
    assert json.loads(queue.take()["payload_bytes"]) == {"v": 1}
    assert heap.mem_free() >= RESERVE


def test_put_does_not_evict_for_the_obsolete_worst_case_gate(monkeypatch):
    """Heap above the reserve but below the old reserve + 48 KiB threshold,
    with a retained entry queued: the incoming message serializes
    successfully, so nothing may be evicted to satisfy the obsolete fixed
    gate -- the post-serialization admission decides final retention."""
    ic, heap = _queue(monkeypatch)
    queue = ic.outbound_queue
    assert (
        queue.put_with_kind(KIND_HEALTH, b"h" * (16 * KB), RETENTION_PRIORITY_HEALTH)
        is True
    )
    heap.free_bytes = RESERVE + 40000  # above the reserve, below the old gate
    assert (
        queue.put(KIND_TELEMETRY, {"v": 1}, RETENTION_PRIORITY_TELEMETRY) is True
    )
    status = queue.status()
    assert status["pending"] == 2           # both retained
    assert status["messages_evicted"] == 0  # nothing displaced
    assert status["messages_rejected"] == 0
    first = queue.take()
    assert first["payload_bytes"] == b"h" * (16 * KB)
    assert queue.complete_in_flight(first) is True
    assert json.loads(queue.take()["payload_bytes"]) == {"v": 1}


def test_serialization_memory_error_recovers_with_gc_before_eviction(monkeypatch):
    """First serialization MemoryError: gc.collect() alone resolves it, so no
    queued data is discarded -- the retry happens before any eviction."""

    ic, heap = _queue(monkeypatch)
    queue = ic.outbound_queue
    # An evictable entry is present, so success is only credible if GC (not
    # eviction) recovered the failure.
    assert (
        queue.put_with_kind(
            KIND_TELEMETRY, b"t" * (8 * KB), RETENTION_PRIORITY_TELEMETRY
        )
        is True
    )
    original = intercore.serialize_and_validate_message
    attempts = {"n": 0}

    def _oom_then_ok(message):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise MemoryError
        return original(message)

    monkeypatch.setattr(
        intercore, "serialize_and_validate_message", _oom_then_ok
    )
    assert (
        queue.put(KIND_TELEMETRY, {"v": 1}, RETENTION_PRIORITY_TELEMETRY) is True
    )
    status = queue.status()
    assert status["pending"] == 2
    assert status["messages_evicted"] == 0
    assert status["messages_rejected"] == 0
    assert heap.collects >= 1


def test_serialization_memory_error_reclaims_one_eligible_entry(monkeypatch):
    """A serialization MemoryError that survives the gc.collect() retry
    reclaims one eligible lower-retention-priority entry (TELEMETRY, via the
    regular eviction metrics) and then succeeds -- eviction only after GC."""

    ic, heap = _queue(monkeypatch)
    queue = ic.outbound_queue
    assert (
        queue.put_with_kind(
            KIND_TELEMETRY, b"t" * (8 * KB), RETENTION_PRIORITY_TELEMETRY
        )
        is True
    )
    original = intercore.serialize_and_validate_message
    attempts = {"n": 0}

    def _oom_twice_then_ok(message):
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise MemoryError
        return original(message)

    monkeypatch.setattr(
        intercore, "serialize_and_validate_message", _oom_twice_then_ok
    )
    # Incoming is more important than the queued TELEMETRY entry, so the
    # retention policy allows the displacement.
    assert (
        queue.put(KIND_TELEMETRY, {"v": 1}, RETENTION_PRIORITY_ERROR) is True
    )
    status = queue.status()
    assert status["pending"] == 1
    assert status["messages_evicted"] == 1
    assert status["telemetry_evicted"] == 1
    assert status["messages_rejected"] == 0
    assert json.loads(queue.take()["payload_bytes"]) == {"v": 1}
    assert heap.mem_free() >= RESERVE


def test_serialization_memory_error_reclaims_exactly_as_many_entries_as_needed(monkeypatch):
    """Recovery is incremental, not bulk-destructive: one entry per failed
    attempt, stopping as soon as serialization succeeds -- here exactly two
    of the three eligible entries are reclaimed."""

    ic, _ = _queue(monkeypatch)
    queue = ic.outbound_queue
    seeded = (
        (KIND_TELEMETRY, RETENTION_PRIORITY_TELEMETRY, b"t" * (8 * KB)),
        (KIND_HEALTH, RETENTION_PRIORITY_HEALTH, b"h" * (8 * KB)),
        (KIND_TELEMETRY, RETENTION_PRIORITY_INFO, b"i" * (8 * KB)),
    )
    for kind, priority, payload in seeded:
        assert queue.put_with_kind(kind, payload, priority) is True
    original = intercore.serialize_and_validate_message
    attempts = {"n": 0}

    def _fail_until_two_evictions(message):
        # The first failure is answered by gc.collect() alone; each later
        # failure triggers one eviction. So exactly three failures = GC plus
        # two evictions, then serialization succeeds.
        attempts["n"] += 1
        if attempts["n"] <= 3:
            raise MemoryError
        return original(message)

    monkeypatch.setattr(
        intercore,
        "serialize_and_validate_message",
        _fail_until_two_evictions,
    )
    assert (
        queue.put(KIND_COMMAND_RESPONSE, {"id": "r"}, RETENTION_PRIORITY_CRITICAL)
        is True
    )
    status = queue.status()
    assert status["pending"] == 2          # TELEMETRY + the incoming CRITICAL
    assert status["messages_evicted"] == 2  # HEALTH then INFO, oldest least-important first
    assert status["telemetry_evicted"] == 1
    assert status["messages_rejected"] == 0
    taken = queue.take()
    assert taken["payload_bytes"] == b"t" * (8 * KB)


def test_serialization_recovery_does_not_evict_a_critical_entry(monkeypatch):
    """An incoming lower-priority message whose serialization keeps failing
    must not displace a retained CRITICAL: nothing eligible remains, so the
    MemoryError propagates instead of the retention floor being discarded or
    the incoming message silently rejected."""

    ic, _ = _queue(monkeypatch)
    queue = ic.outbound_queue
    existing = b'{"id":"response-a"}'
    assert (
        queue.put_with_kind(
            KIND_COMMAND_RESPONSE, existing, RETENTION_PRIORITY_CRITICAL
        )
        is True
    )

    def _always_oom(*args):
        raise MemoryError

    monkeypatch.setattr(
        intercore, "serialize_and_validate_message", _always_oom
    )
    with pytest.raises(MemoryError):
        queue.put(KIND_TELEMETRY, {"v": 1}, RETENTION_PRIORITY_TELEMETRY)
    status = queue.status()
    assert status["pending"] == 1
    assert status["queued_bytes"] == len(existing)
    assert status["messages_evicted"] == 0
    assert status["messages_rejected"] == 0
    assert queue.take()["payload_bytes"] == existing


def test_serialization_recovery_does_not_evict_critical_for_critical(monkeypatch):
    """The retention floor also holds on the recovery path: an admitted
    CRITICAL cannot be displaced by another CRITICAL's failed serialization --
    the MemoryError propagates rather than discarding the earlier response."""

    ic, _ = _queue(monkeypatch)
    queue = ic.outbound_queue
    existing = b'{"id":"response-a"}'
    assert (
        queue.put_with_kind(
            KIND_COMMAND_RESPONSE, existing, RETENTION_PRIORITY_CRITICAL
        )
        is True
    )

    def _always_oom(*args):
        raise MemoryError

    monkeypatch.setattr(
        intercore, "serialize_and_validate_message", _always_oom
    )
    with pytest.raises(MemoryError):
        queue.put(
            KIND_COMMAND_RESPONSE, {"id": "response-b"}, RETENTION_PRIORITY_CRITICAL
        )
    status = queue.status()
    assert status["pending"] == 1
    assert status["messages_evicted"] == 0
    assert status["messages_rejected"] == 0
    assert queue.take()["payload_bytes"] == existing


def test_serialization_recovery_equal_priority_matches_admission_policy(monkeypatch):
    """Equal-priority replacement: the existing admission policy allows an
    incoming entry to displace a same-priority queued entry, and the
    serialization recovery path uses that same rule -- no new equal-priority
    semantics are invented for recovery."""

    ic, _ = _queue(monkeypatch)
    queue = ic.outbound_queue
    assert (
        queue.put_with_kind(
            KIND_TELEMETRY, b"o" * (8 * KB), RETENTION_PRIORITY_TELEMETRY
        )
        is True
    )
    original = intercore.serialize_and_validate_message
    attempts = {"n": 0}

    def _oom_twice_then_ok(message):
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise MemoryError
        return original(message)

    monkeypatch.setattr(
        intercore, "serialize_and_validate_message", _oom_twice_then_ok
    )
    assert (
        queue.put(KIND_TELEMETRY, {"v": 1}, RETENTION_PRIORITY_TELEMETRY) is True
    )
    status = queue.status()
    assert status["pending"] == 1
    assert status["messages_evicted"] == 1
    assert status["telemetry_evicted"] == 1
    assert json.loads(queue.take()["payload_bytes"]) == {"v": 1}


def test_unrecoverable_serialization_memory_error_propagates(monkeypatch):
    """Empty queue: no queue-owned memory to reclaim, so an unrecovered
    serialization MemoryError propagates to the firmware recovery boundary
    rather than being converted into a transient (False) queue rejection."""

    ic, _ = _queue(monkeypatch)

    def _always_oom(*args):
        raise MemoryError

    monkeypatch.setattr(
        intercore, "serialize_and_validate_message", _always_oom
    )
    with pytest.raises(MemoryError):
        ic.outbound_queue.put(
            KIND_TELEMETRY, {"v": 1}, RETENTION_PRIORITY_TELEMETRY
        )
    assert ic.outbound_queue.get_depth() == 0


def test_non_memory_serializer_failures_propagate_without_eviction(monkeypatch):
    """Only MemoryError triggers reclamation: a serializer defect (a TypeError
    or ValueError from the serializer itself) propagates unchanged, evicting
    nothing and never being converted into queue pressure -- fail-fast is
    preserved."""

    ic, _ = _queue(monkeypatch)
    queue = ic.outbound_queue
    assert (
        queue.put_with_kind(
            KIND_TELEMETRY, b"t" * (8 * KB), RETENTION_PRIORITY_TELEMETRY
        )
        is True
    )

    def _type_error(*args):
        raise TypeError("serializer defect")

    monkeypatch.setattr(
        intercore, "serialize_and_validate_message", _type_error
    )
    with pytest.raises(TypeError):
        queue.put(KIND_TELEMETRY, {"v": 1}, RETENTION_PRIORITY_TELEMETRY)

    def _value_error(*args):
        raise ValueError("serializer defect")

    monkeypatch.setattr(
        intercore, "serialize_and_validate_message", _value_error
    )
    with pytest.raises(ValueError):
        queue.put(KIND_TELEMETRY, {"v": 1}, RETENTION_PRIORITY_TELEMETRY)

    status = queue.status()
    assert status["pending"] == 1
    assert status["messages_evicted"] == 0
    assert status["messages_rejected"] == 0
    assert queue.take()["payload_bytes"] == b"t" * (8 * KB)


def test_put_post_serialization_admission_still_enforces_the_reserve(monkeypatch):
    """A message that serializes successfully still goes through the
    heap-governed admission: heap below the reserve with nothing to reclaim
    is a transient (False) rejection exactly as before -- the recovery change
    touches only the serialization working-set stage."""
    ic, _ = _queue(monkeypatch, free_bytes=0)
    queue = ic.outbound_queue
    assert (
        queue.put(KIND_TELEMETRY, {"v": 1}, RETENTION_PRIORITY_TELEMETRY) is False
    )
    status = queue.status()
    assert status["pending"] == 0
    assert status["messages_rejected"] == 1
    assert status["messages_evicted"] == 0


def test_put_with_kind_never_serializes(monkeypatch):
    """put_with_kind() receives already-final bytes: the serializer and the
    serialization-recovery path are never invoked, and only the existing
    admission rules apply."""

    ic, _ = _queue(monkeypatch)
    queue = ic.outbound_queue

    def _boom(*args):
        raise AssertionError("the serializer must not run on put_with_kind()")

    monkeypatch.setattr(
        intercore, "serialize_and_validate_message", _boom
    )
    assert (
        queue.put_with_kind(
            KIND_TELEMETRY, b'{"v":1}', RETENTION_PRIORITY_TELEMETRY
        )
        is True
    )


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
# Outbound admission: the count ceiling (outbound_queue_max_messages)
#
# A second, deterministic constraint subordinate to the heap policy: the heap
# floor is evaluated first, and the count ceiling is enforced only at each
# append, reusing the same retention-aware eviction. It can add a rejection or
# an eviction to make room, but never admit what the heap policy rejects.
# ---------------------------------------------------------------------------


def test_count_below_limit_appends_without_eviction(monkeypatch):
    """Below the ceiling: admissions append with no count-driven eviction, up
    to and including exactly the limit."""
    ic, _ = _queue(monkeypatch, max_messages=4)
    queue = ic.outbound_queue
    for i in range(3):
        assert queue.put_with_kind(KIND_TELEMETRY, b'{"i":%d}' % i, RETENTION_PRIORITY_TELEMETRY) is True
    status = queue.status()
    assert status["depth"] == 3
    assert status["messages_evicted"] == 0
    assert status["messages_rejected"] == 0
    # The fourth admission lands exactly at the limit.
    assert queue.put_with_kind(KIND_TELEMETRY, b'{"i":3}', RETENTION_PRIORITY_TELEMETRY) is True
    assert queue.get_depth() == 4
    assert queue.status()["messages_evicted"] == 0


def test_count_at_limit_evicts_one_eligible_and_admits(monkeypatch):
    """At the ceiling: one eligible entry is displaced (the retention rule),
    the incoming entry is admitted, and the count stays at the limit."""
    ic, _ = _queue(monkeypatch, max_messages=4)
    queue = ic.outbound_queue
    for i in range(4):
        assert queue.put_with_kind(KIND_TELEMETRY, b'{"i":%d}' % i, RETENTION_PRIORITY_TELEMETRY) is True
    assert queue.get_depth() == 4
    # Incoming CRITICAL (more important than the queued TELEMETRY) at the limit.
    assert queue.put_with_kind(KIND_COMMAND_RESPONSE, b'{"r":1}', RETENTION_PRIORITY_CRITICAL) is True
    status = queue.status()
    assert status["depth"] == 4
    assert status["messages_evicted"] == 1
    assert status["telemetry_evicted"] == 1
    assert status["messages_rejected"] == 0
    _assert_watermark_at_least_depth(queue)


def test_count_eviction_preserves_retention_selection(monkeypatch):
    """The count ceiling reuses the existing retention selection (not FIFO):
    a higher-priority incoming entry displaces the least-important eligible
    class, here HEALTH (70), not the FIFO-head TELEMETRY (40)."""
    ic, _ = _queue(monkeypatch, max_messages=3)
    queue = ic.outbound_queue
    assert queue.put_with_kind(KIND_TELEMETRY, b"t", RETENTION_PRIORITY_TELEMETRY) is True
    assert queue.put_with_kind(KIND_HEALTH, b"h", RETENTION_PRIORITY_HEALTH) is True
    assert queue.put_with_kind(KIND_TELEMETRY, b"i", RETENTION_PRIORITY_INFO) is True
    assert queue.get_depth() == 3
    # Incoming ERROR (20): worst queued priority is HEALTH (70); that is evicted.
    assert queue.put_with_kind(KIND_COMMAND_RESPONSE, b"e", RETENTION_PRIORITY_ERROR) is True
    payloads = []
    for _ in range(3):
        entry = queue.take()
        payloads.append(entry["payload_bytes"])
        queue.complete_in_flight(entry)
    assert payloads == [b"t", b"i", b"e"]  # HEALTH (b"h") was displaced
    status = queue.status()
    assert status["messages_evicted"] == 1
    assert status["telemetry_evicted"] == 0  # the displaced entry was HEALTH


def test_count_at_limit_rejects_when_nothing_eligible(monkeypatch):
    """At the ceiling with nothing the incoming entry may displace (all queued
    entries more important): the incoming entry is rejected, the count is
    unchanged, and the rejection is counted exactly once."""
    ic, _ = _queue(monkeypatch, max_messages=3)
    queue = ic.outbound_queue
    for i in range(3):
        assert queue.put_with_kind(KIND_COMMAND_RESPONSE, b'{"r":%d}' % i, RETENTION_PRIORITY_CRITICAL) is True
    assert queue.get_depth() == 3
    assert queue.put_with_kind(KIND_TELEMETRY, b"t", RETENTION_PRIORITY_TELEMETRY) is False
    status = queue.status()
    assert status["depth"] == 3
    assert status["messages_evicted"] == 0
    assert status["messages_rejected"] == 1


def test_count_critical_saturation_rejects_without_overflow(monkeypatch):
    """CRITICAL saturation: a queue full of non-evictable CRITICAL entries
    rejects a further CRITICAL (the non-evictable floor) and the count never
    exceeds the ceiling -- no hidden overflow or reserved emergency slot."""
    ic, _ = _queue(monkeypatch, max_messages=4)
    queue = ic.outbound_queue
    for i in range(4):
        assert queue.put_with_kind(KIND_COMMAND_RESPONSE, b'{"r":%d}' % i, RETENTION_PRIORITY_CRITICAL) is True
    assert queue.get_depth() == 4
    assert queue.put_with_kind(KIND_COMMAND_RESPONSE, b'{"r":4}', RETENTION_PRIORITY_CRITICAL) is False
    status = queue.status()
    assert status["depth"] == 4
    assert status["high_watermark"] == 4
    assert status["messages_rejected"] == 1
    assert status["messages_evicted"] == 0


def test_count_and_heap_pressure_evict_once_not_twice(monkeypatch):
    """The load-bearing ordering guard: with the queue at the count limit AND
    under heap pressure, the heap stage evicts one entry (restoring the floor),
    which also brings the count below the limit, so the count stage performs NO
    additional eviction -- exactly one eviction total and the final count
    equals the limit. Never two evictions because both constraints were active."""
    MIN = RESERVE  # single threshold: the soft band does not apply
    ic, heap = _queue(monkeypatch, free_bytes=HEAPY, minimum=MIN, preferred=MIN, max_messages=3)
    queue = ic.outbound_queue
    for i in range(3):
        assert queue.put_with_kind(KIND_TELEMETRY, b"t" * 8 * KB, RETENTION_PRIORITY_TELEMETRY) is True
    assert queue.get_depth() == 3
    # Heap falls below the floor (one 8 KiB entry short of the reserve).
    heap.free_bytes = RESERVE - 8 * KB
    assert queue.put_with_kind(KIND_COMMAND_RESPONSE, b'{"r":1}', RETENTION_PRIORITY_CRITICAL) is True
    status = queue.status()
    assert status["depth"] == 3
    assert status["messages_evicted"] == 1  # one (heap), not two
    assert status["telemetry_evicted"] == 1
    assert status["messages_rejected"] == 0
    _assert_watermark_at_least_depth(queue)


def test_count_does_not_override_heap_rejection(monkeypatch):
    """Heap rejection is authoritative: with the heap unrecoverable and the
    queue below the count limit, the message is rejected -- the count ceiling
    cannot admit what the heap policy rejects."""
    ic, _ = _queue(monkeypatch, free_bytes=0, max_messages=8)
    queue = ic.outbound_queue
    # Queue is empty (count 0 < 8) but the heap cannot be restored and there is
    # nothing to evict.
    assert queue.put_with_kind(KIND_TELEMETRY, b'{"v":1}', RETENTION_PRIORITY_TELEMETRY) is False
    status = queue.status()
    assert status["depth"] == 0
    assert status["messages_rejected"] == 1
    assert status["messages_evicted"] == 0


def test_high_watermark_never_exceeds_count_limit(monkeypatch):
    """Repeated admissions past the ceiling each displace one entry to stay at
    the limit, so the depth high watermark never exceeds the configured max."""
    ic, _ = _queue(monkeypatch, max_messages=4)
    queue = ic.outbound_queue
    for i in range(10):
        assert queue.put_with_kind(KIND_TELEMETRY, b'{"i":%d}' % i, RETENTION_PRIORITY_TELEMETRY) is True
    status = queue.status()
    assert status["depth"] == 4
    assert status["high_watermark"] == 4  # == max, never above
    _assert_watermark_at_least_depth(queue)


def test_heap_governs_before_count_on_small_heap(monkeypatch):
    """Pico-W shape: a large count ceiling (256) that never binds, but heap
    pressure is reached first. The heap policy evicts to restore the floor
    while the queue holds only a handful of entries -- the count ceiling must
    not let the queue retain what the heap policy reclaims."""
    MIN, PREFERRED = 48 * KB, 64 * KB
    ic, heap = _queue(monkeypatch, free_bytes=HEAPY, minimum=MIN, preferred=PREFERRED, max_messages=256)
    queue = ic.outbound_queue
    assert queue.put_with_kind(KIND_HEALTH, b"h" * 16 * KB, RETENTION_PRIORITY_HEALTH) is True
    assert queue.put_with_kind(KIND_TELEMETRY, b"t" * 16 * KB, RETENTION_PRIORITY_TELEMETRY) is True
    assert queue.get_depth() == 2
    # Heap falls hard below the floor: admission is governed by heap reclaim
    # (evicting the least-important entry), far below the 256 count ceiling.
    heap.free_bytes = MIN - 16 * KB
    assert queue.put_with_kind(KIND_COMMAND_RESPONSE, b'{"r":1}', RETENTION_PRIORITY_CRITICAL) is True
    status = queue.status()
    assert status["depth"] == 2  # HEALTH evicted; TELEMETRY + CRITICAL remain
    assert status["messages_evicted"] == 1
    assert status["telemetry_evicted"] == 0  # the displaced entry was HEALTH
    assert status["depth"] < 256  # the count ceiling never engaged


def test_in_flight_entry_counts_toward_count_limit(monkeypatch):
    """The in-flight entry is retained by the outbound queue, so it counts
    toward the ceiling: with one queued and one in flight (depth 2 == max), a
    new admission displaces a queued entry (never the in-flight one) so depth
    stays at the limit rather than exceeding it."""
    ic, _ = _queue(monkeypatch, max_messages=2)
    queue = ic.outbound_queue
    assert queue.put_with_kind(KIND_TELEMETRY, b'{"a":1}', RETENTION_PRIORITY_TELEMETRY) is True
    assert queue.put_with_kind(KIND_TELEMETRY, b'{"b":2}', RETENTION_PRIORITY_TELEMETRY) is True
    in_flight = queue.take()  # one in flight, one queued: depth 2 == max
    assert queue.has_in_flight() is True
    # depth is 2 (queued 1 + in-flight 1). A new CRITICAL admission is at the
    # limit: it displaces the queued TELEMETRY, so depth stays at 2, never 3.
    assert queue.put_with_kind(KIND_COMMAND_RESPONSE, b'{"r":1}', RETENTION_PRIORITY_CRITICAL) is True
    status = queue.status()
    assert status["depth"] == 2
    assert status["high_watermark"] == 2
    assert status["messages_evicted"] == 1
    assert queue.has_in_flight() is True  # the in-flight entry was not displaced
    queue.complete_in_flight(in_flight)
    _assert_watermark_at_least_depth(queue)


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
