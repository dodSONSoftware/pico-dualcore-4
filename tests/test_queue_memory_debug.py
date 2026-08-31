# test_queue_memory_debug.py - Tests for the queue-memory debug instrumentation
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""
Host-side tests for the temporary DEBUG_QUEUE_MEMORY instrumentation in
intercore.py.

The instrumentation is a validation aid for the heap-reserve queues: it must
be silent by default (the production gate stays False) and, when enabled,
emit one grep-friendly line per meaningful queue event (admit, reject, evict,
memory-pressure entry, gc.collect() before/after, backlog drained) in the
documented field order. No fixed-capacity behavior is asserted here; those
contracts live in test_intercore.py.
"""

import gc
import pathlib
import sys
from unittest.mock import MagicMock

import pytest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.modules.setdefault("machine", MagicMock())

import intercore  # noqa: E402
from intercore import (  # noqa: E402
    RETENTION_PRIORITY_CRITICAL,
    RETENTION_PRIORITY_HEALTH,
    RETENTION_PRIORITY_TELEMETRY,
    InterCore,
    KIND_HEALTH,
    KIND_TELEMETRY,
)


RESERVE = 65536
HEAPY = 256 * 1024
KB = 1024
HEAP_ALLOC = 98765


class FakeHeap:
    """Host-side stand-in for the MicroPython heap seen through gc."""

    def __init__(self, free_bytes, garbage_bytes=0):
        self.free_bytes = free_bytes
        self._garbage = garbage_bytes

    def mem_free(self):
        return self.free_bytes

    def mem_alloc(self):
        return HEAP_ALLOC

    def collect(self):
        if self._garbage:
            self.free_bytes += self._garbage
            self._garbage = 0

    def install(self, monkeypatch):
        monkeypatch.setattr(gc, "mem_free", self.mem_free, raising=False)
        monkeypatch.setattr(gc, "mem_alloc", self.mem_alloc, raising=False)
        monkeypatch.setattr(gc, "collect", self.collect)


def _queue(monkeypatch, free_bytes=HEAPY, garbage_bytes=0):
    ic = InterCore(RESERVE)
    heap = FakeHeap(free_bytes, garbage_bytes)
    heap.install(monkeypatch)
    # Model MicroPython's refcounted heap: evicted entries release their
    # payload bytes immediately, which the reserve check then sees.
    queue = ic.outbound_queue
    original_evict = queue._evict_oldest_by_priority_locked

    def evict_and_release(priority):
        before = queue._queued_bytes
        evicted = original_evict(priority)
        if evicted:
            heap.free_bytes += before - queue._queued_bytes
        return evicted

    monkeypatch.setattr(queue, "_evict_oldest_by_priority_locked", evict_and_release)
    return ic, heap


def _enable(monkeypatch):
    monkeypatch.setattr(intercore, "DEBUG_QUEUE_MEMORY", True)


def _lines(capsys):
    out = capsys.readouterr().out
    return [line for line in out.splitlines() if line]


def _parse(line):
    """Split '[DEBUG] <prefix> key=value ...' into (prefix, pairs, order)."""
    parts = line.split()
    assert parts[0] == "[DEBUG]"
    prefix = parts[1]
    pairs = {}
    order = []
    for part in parts[2:]:
        key, sep, value = part.partition("=")
        assert sep == "=", "unparseable field: {}".format(part)
        pairs[key] = value
        order.append(key)
    return prefix, pairs, order


# The documented heap/queue field order (event and any event-specific
# fields -- reason, priority, evicted_priority -- precede it).
HEAP_FIELDS = (
    "heap_alloc_bytes",
    "heap_free_bytes",
    "minimum_free_heap_bytes",
    "heap_headroom_bytes",
    "queue_count",
    "queue_high_watermark",
)


def _check_queue_fields(pairs, order, prefix, expect_queued_bytes=True):
    expected = list(HEAP_FIELDS)
    if expect_queued_bytes:
        expected.append("queued_bytes")
    tail = [key for key in order if key in expected]
    assert tail == expected
    assert pairs["minimum_free_heap_bytes"] == str(RESERVE)
    assert pairs["heap_alloc_bytes"] == str(HEAP_ALLOC)
    assert int(pairs["heap_headroom_bytes"]) == (
        int(pairs["heap_free_bytes"]) - RESERVE
    )


def _events(lines, prefix):
    events = []
    for line in lines:
        line_prefix, pairs, order = _parse(line)
        _check_queue_fields(
            pairs, order, line_prefix, expect_queued_bytes=(prefix == "outbound_queue")
        )
        events.append((line_prefix, pairs))
    return events


# ---------------------------------------------------------------------------
# Gate and silence
# ---------------------------------------------------------------------------


def test_instrumentation_gate_defaults_off():
    """Production default: debug.py ships DEBUG_QUEUE_MEMORY = False, so the
    debug path never runs in production.

    Asserted from the source file: other test modules replace
    sys.modules["debug"] with a mock, so the imported binding is a test-order
    artifact, not the shipped default.
    """
    import re

    debug_path = pathlib.Path(__file__).resolve().parents[1] / "debug.py"
    match = re.search(
        r"^DEBUG_QUEUE_MEMORY\s*=\s*(\w+)", debug_path.read_text(), re.MULTILINE
    )
    assert match is not None
    assert match.group(1) == "False"


def test_silent_when_disabled(monkeypatch, capsys):
    """Flag explicitly off: pressure path, eviction, and drain produce no
    output (set explicitly, not relying on the default, because other test
    modules can stub sys.modules["debug"])."""
    monkeypatch.setattr(intercore, "DEBUG_QUEUE_MEMORY", False)
    ic, heap = _queue(monkeypatch)
    queue = ic.outbound_queue
    assert queue.put_with_kind(KIND_HEALTH, b"h" * 8 * KB, RETENTION_PRIORITY_HEALTH) is True
    heap.free_bytes = RESERVE - 8 * KB
    assert (
        queue.put_with_kind(KIND_TELEMETRY, b"c" * 4 * KB, RETENTION_PRIORITY_CRITICAL)
        is True
    )
    assert queue.put_with_kind(KIND_TELEMETRY, b"x", RETENTION_PRIORITY_TELEMETRY) is True
    assert queue.status()["pending"] == 2  # HEALTH was evicted, two entries remain
    queue.complete_in_flight(queue.take())
    queue.complete_in_flight(queue.take())
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# Outbound queue: event sequence and field format
# ---------------------------------------------------------------------------


def test_fast_path_admit_line(monkeypatch, capsys):
    ic, heap = _queue(monkeypatch)
    _enable(monkeypatch)
    queue = ic.outbound_queue
    payload = b'{"value":42}'
    assert queue.put_with_kind(KIND_TELEMETRY, payload, RETENTION_PRIORITY_TELEMETRY) is True

    lines = _lines(capsys)
    assert len(lines) == 1
    prefix, pairs, order = _parse(lines[0])
    assert prefix == "outbound_queue"
    _check_queue_fields(pairs, order, prefix)
    assert pairs["event"] == "admit"
    assert pairs["heap_free_bytes"] == str(HEAPY)
    assert pairs["queue_count"] == "1"
    assert pairs["queue_high_watermark"] == "1"
    assert pairs["queued_bytes"] == str(len(payload))


def test_pressure_gc_restores_event_sequence(monkeypatch, capsys):
    """Heap below the reserve with collectable garbage: gc alone restores it."""
    ic, heap = _queue(monkeypatch, free_bytes=RESERVE - 4096, garbage_bytes=8192)
    _enable(monkeypatch)
    assert ic.outbound_queue.put_with_kind(KIND_TELEMETRY, b'{"v":1}', RETENTION_PRIORITY_TELEMETRY) is True

    events = _events(_lines(capsys), "outbound_queue")
    assert [pairs["event"] for _, pairs in events] == [
        "memory_pressure",
        "gc_before_admission",
        "gc_after_admission",
        "admit",
    ]
    # The gc pair brackets the reclaim: before is below the reserve, after is
    # at or above it.
    before = events[1][1]
    after = events[2][1]
    assert int(before["heap_headroom_bytes"]) < 0
    assert int(after["heap_headroom_bytes"]) >= 0


def test_eviction_event_reports_evicted_priority(monkeypatch, capsys):
    ic, heap = _queue(monkeypatch)
    _enable(monkeypatch)
    queue = ic.outbound_queue
    assert queue.put_with_kind(KIND_HEALTH, b"h" * 8 * KB, RETENTION_PRIORITY_HEALTH) is True
    heap.free_bytes = RESERVE - 8 * KB  # no garbage to collect
    assert (
        queue.put_with_kind(KIND_TELEMETRY, b"c" * 4 * KB, RETENTION_PRIORITY_CRITICAL)
        is True
    )

    events = _events(_lines(capsys), "outbound_queue")
    assert [pairs["event"] for _, pairs in events] == [
        "admit",  # the HEALTH entry admitted before the pressure
        "memory_pressure",
        "gc_before_admission",
        "gc_after_admission",
        "evict",
        "admit",
    ]
    evict_pairs = events[4][1]
    assert evict_pairs["evicted_priority"] == str(RETENTION_PRIORITY_HEALTH)
    assert queue.status()["messages_evicted"] == 1


def test_reject_reports_memory_pressure_reason(monkeypatch, capsys):
    """Empty queue, unrecoverable pressure: reject, with the reason field."""
    ic, _ = _queue(monkeypatch, free_bytes=0)
    _enable(monkeypatch)
    assert (
        ic.outbound_queue.put_with_kind(KIND_TELEMETRY, b'{"v":1}', RETENTION_PRIORITY_TELEMETRY)
        is False
    )

    events = _events(_lines(capsys), "outbound_queue")
    assert [pairs["event"] for _, pairs in events] == [
        "memory_pressure",
        "gc_before_admission",
        "gc_after_admission",
        "reject",
    ]
    reject_pairs = events[3][1]
    assert reject_pairs["reason"] == "memory_pressure"
    assert reject_pairs["priority"] == str(RETENTION_PRIORITY_TELEMETRY)


def test_reject_reports_lower_priority_reason(monkeypatch, capsys):
    """Incoming is less important than everything queued: reject, different reason."""
    ic, heap = _queue(monkeypatch)
    _enable(monkeypatch)
    queue = ic.outbound_queue
    assert queue.put_with_kind(KIND_TELEMETRY, b"t" * 8 * KB, RETENTION_PRIORITY_TELEMETRY) is True
    heap.free_bytes = 0
    assert (
        queue.put_with_kind(KIND_HEALTH, b"h" * 8 * KB, RETENTION_PRIORITY_HEALTH) is False
    )

    events = _events(_lines(capsys), "outbound_queue")
    assert [pairs["event"] for _, pairs in events] == [
        "admit",  # the TELEMETRY entry admitted before the pressure
        "memory_pressure",
        "gc_before_admission",
        "gc_after_admission",
        "reject",
    ]
    reject_pairs = events[-1][1]
    assert reject_pairs["reason"] == "lower_priority_than_queued"
    assert reject_pairs["priority"] == str(RETENTION_PRIORITY_HEALTH)
    assert queue.status()["messages_rejected"] == 1
    assert queue.status()["messages_evicted"] == 0


# ---------------------------------------------------------------------------
# Outbound queue: drained event
# ---------------------------------------------------------------------------


def test_drained_event_after_backlog(monkeypatch, capsys):
    """A backlog that fully empties logs exactly one drained event."""
    ic, _ = _queue(monkeypatch)
    _enable(monkeypatch)
    queue = ic.outbound_queue
    for _ in range(3):
        assert queue.put_with_kind(KIND_TELEMETRY, b'{"v":1}', RETENTION_PRIORITY_TELEMETRY) is True
    for _ in range(3):
        queue.complete_in_flight(queue.take())

    events = _events(_lines(capsys), "outbound_queue")
    drained = [pairs for _, pairs in events if pairs["event"] == "drained"]
    assert len(drained) == 1
    # The drained event is the last one, at an empty queue.
    assert events[-1][1]["event"] == "drained"
    assert drained[0]["queue_count"] == "0"
    assert drained[0]["queued_bytes"] == "0"


def test_no_drained_event_for_single_entry(monkeypatch, capsys):
    """Steady-state single-entry completion must stay quiet."""
    ic, _ = _queue(monkeypatch)
    _enable(monkeypatch)
    queue = ic.outbound_queue
    assert queue.put_with_kind(KIND_TELEMETRY, b'{"v":1}', RETENTION_PRIORITY_TELEMETRY) is True
    queue.complete_in_flight(queue.take())

    events = _events(_lines(capsys), "outbound_queue")
    assert [pairs["event"] for _, pairs in events] == ["admit"]


# ---------------------------------------------------------------------------
# Event queue (Core 0 -> Core 1)
# ---------------------------------------------------------------------------


def test_event_queue_admit_line(monkeypatch, capsys):
    ic, _ = _queue(monkeypatch)
    _enable(monkeypatch)
    assert ic.event_queue.put({"seq": 1}) is True

    lines = _lines(capsys)
    assert len(lines) == 1
    prefix, pairs, order = _parse(lines[0])
    assert prefix == "event_queue"
    _check_queue_fields(pairs, order, prefix, expect_queued_bytes=False)
    assert "queued_bytes" not in pairs
    assert pairs["event"] == "admit"
    assert pairs["queue_count"] == "1"
    assert pairs["queue_high_watermark"] == "1"


def test_event_queue_reject_line_keeps_admitted_event(monkeypatch, capsys):
    ic, heap = _queue(monkeypatch)
    _enable(monkeypatch)
    assert ic.event_queue.put({"seq": 1}) is True
    heap.free_bytes = 0  # unrecoverable pressure
    assert ic.event_queue.put({"seq": 2}) is False

    events = _events(_lines(capsys), "event_queue")
    assert [pairs["event"] for _, pairs in events] == [
        "admit",
        "memory_pressure",
        "gc_before_admission",
        "gc_after_admission",
        "reject",
    ]
    reject_pairs = events[-1][1]
    assert reject_pairs["reason"] == "memory_pressure"
    # No eviction: the admitted event is untouched.
    assert ic.event_queue.take() == {"seq": 1}
    assert ic.event_queue.status()["rejected"] == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
