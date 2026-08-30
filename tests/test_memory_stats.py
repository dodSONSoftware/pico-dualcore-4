# Host-side tests for the shared MemoryStats and the heap-pressure admission.
#
# These run under a controllable clock (MicroPython's time.ticks_* is absent on
# CPython) and a stateful fake GC that models the two ways the reserve is
# restored: a collect freeing other garbage, and evicting an entry releasing its
# retained payload bytes.

import gc
import importlib
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


class FakeTime:
    """A controllable monotonic millisecond clock exposing MicroPython's ticks_* API."""

    def __init__(self):
        self._now_ms = 0

    def ticks_ms(self):
        return self._now_ms

    def ticks_diff(self, a, b):
        return a - b

    def ticks_add(self, a, b):
        return a + b

    def advance(self, ms):
        self._now_ms += ms


FAKE_TIME = FakeTime()


class StatefulGC:
    """Model free heap as headroom minus retained queue bytes, plus GC reclaim.

    ``mem_free()`` = H - (retained payload bytes) + (collects * reclaim).
    Admitting an entry raises the retained bytes (free drops); evicting one
    lowers them (free rises); a collect adds its reclaim (free rises). This lets
    the tests assert exactly which path (collect vs. eviction) restored the
    reserve.
    """

    def __init__(self, queue, headroom, reclaim_per_collect, collect_duration_ms=0):
        self._queue = queue
        self._headroom = headroom
        self._reclaim_per_collect = reclaim_per_collect
        self._collect_duration_ms = collect_duration_ms
        self.collect_count = 0

    def mem_free(self):
        retained = self._queue.status()["queued_bytes"]
        return (
            self._headroom
            - retained
            + self.collect_count * self._reclaim_per_collect
        )

    def collect(self):
        FAKE_TIME.advance(self._collect_duration_ms)
        self.collect_count += 1
        return 0


class ScriptedGC:
    """Return one value before collect() and another after, to pin the
    pre/post observations for the low-watermark test."""

    def __init__(self, before, after):
        self._before = before
        self._after = after
        self._state = "before"
        self.collect_count = 0

    def mem_free(self):
        return self._before if self._state == "before" else self._after

    def collect(self):
        self.collect_count += 1
        self._state = "after"
        return 0


@pytest.fixture(scope="module")
def fakes():
    """Install the fake clock, reload intercore to bind it, and restore on exit."""
    saved_time = sys.modules.get("time")
    had_mem_free = hasattr(gc, "mem_free")
    saved_mem_free = getattr(gc, "mem_free", None)
    saved_collect = gc.collect

    FAKE_TIME._now_ms = 0
    sys.modules["time"] = FAKE_TIME
    gc.mem_free = lambda: 1000000  # healthy default; tests override
    gc.collect = lambda: 0

    import intercore
    importlib.reload(intercore)
    try:
        yield intercore
    finally:
        if saved_time is not None:
            sys.modules["time"] = saved_time
        else:
            del sys.modules["time"]
        if had_mem_free:
            gc.mem_free = saved_mem_free
        elif hasattr(gc, "mem_free"):
            del gc.mem_free
        gc.collect = saved_collect


def _set_gc(mem_free, collect):
    gc.mem_free = mem_free
    gc.collect = collect


def test_minimum_is_a_low_watermark_observed_before_gc(fakes):
    """The minimum records the PRE-collect value (the important low point)."""
    stats = fakes.MemoryStats()
    fake = ScriptedGC(before=500, after=2000)
    _set_gc(fake.mem_free, fake.collect)
    stats.collect()
    # 500 is the pre-collect low point; 2000 (post) must not raise the minimum.
    assert stats.minimum_free_heap_observed() == 500
    assert stats.snapshot()["minimum_free_heap_observed_bytes"] == 500


def test_collect_tracks_count_reclaim_and_duration(fakes):
    stats = fakes.MemoryStats()
    fake = ScriptedGC(before=1000, after=1500)

    def _collect_advancing(ms):
        # Advance the clock (measured as the collect's duration) and move the
        # scripted heap from the "before" to the "after" reading.
        def _c():
            FAKE_TIME.advance(ms)
            fake.collect()  # flips _state to "after"
            return 0
        return _c

    _set_gc(fake.mem_free, _collect_advancing(25))
    stats.collect()

    snap = stats.snapshot()
    assert snap["gc_collect_count"] == 1
    assert snap["gc_bytes_reclaimed"] == 500
    assert snap["gc_total_reclaimed_bytes"] == 500
    assert snap["gc_last_duration_ms"] == 25
    assert snap["gc_max_duration_ms"] == 25

    # A shorter second collect updates "last" but not "max".
    fake._state = "before"  # reset the scripted state so it collects again
    _set_gc(fake.mem_free, _collect_advancing(10))
    stats.collect()
    snap = stats.snapshot()
    assert snap["gc_collect_count"] == 2
    assert snap["gc_last_duration_ms"] == 10
    assert snap["gc_max_duration_ms"] == 25
    assert snap["gc_total_reclaimed_bytes"] == 1000


def test_optional_gc_respects_cooldown(fakes):
    """Two optional collects within the cooldown window collapse to one."""
    stats = fakes.MemoryStats()
    # Heap below the threshold so the optional path wants to collect.
    _set_gc(lambda: 100, lambda: 0)
    FAKE_TIME.advance(0)

    assert stats.collect_if_below(200) is True        # first optional collect
    FAKE_TIME.advance(500)                              # still within the 1s window
    assert stats.collect_if_below(200) is False        # cooldown suppresses it
    FAKE_TIME.advance(700)                              # now past the window
    assert stats.collect_if_below(200) is True         # allowed again


def test_forced_pressure_gc_bypasses_cooldown(fakes):
    """The required (reserve) path collects even inside the cooldown window."""
    stats = fakes.MemoryStats()
    _set_gc(lambda: 100, lambda: 0)
    FAKE_TIME.advance(0)

    assert stats.collect_if_below(200, force=True) is True
    FAKE_TIME.advance(100)                              # well inside the cooldown
    assert stats.collect_if_below(200, force=True) is True


def test_observe_does_not_collect(fakes):
    stats = fakes.MemoryStats()
    calls = {"collect": 0}
    _set_gc(lambda: 1234, lambda: (calls.__setitem__("collect", calls["collect"] + 1) or 0))
    stats.observe_free_heap(999)
    assert calls["collect"] == 0
    assert stats.minimum_free_heap_observed() == 999


def test_collect_propagates_memory_error(fakes):
    """MemoryError from gc.collect() must propagate, never be swallowed."""
    stats = fakes.MemoryStats()
    _set_gc(lambda: 1000, lambda: (_ for _ in ()).throw(MemoryError("no memory")))
    with pytest.raises(MemoryError):
        stats.collect()


# --- OutboundQueue heap-pressure admission ---

def _make_bus(fakes, reserve, headroom, reclaim_per_collect):
    bus = fakes.InterCore(minimum_free_heap_bytes=reserve, event_max=4)
    fake = StatefulGC(bus.outbound_queue, headroom, reclaim_per_collect)
    _set_gc(fake.mem_free, fake.collect)
    return bus, fake


def _put(bus, priority, size, kind="health"):
    return bus.outbound_queue.put_with_kind(
        kind, b"{" + b"x" * (size - 2) + b"}", priority
    )


def test_healthy_heap_admits_beyond_sixteen_up_to_ceiling(fakes):
    """A healthy heap admits well past the old 16-entry size, up to the ceiling."""
    reserve = 1000
    ceiling = 32
    bus = fakes.InterCore(minimum_free_heap_bytes=reserve, event_max=4, outbound_max=ceiling)
    fake = StatefulGC(bus.outbound_queue, 100000, 0)
    _set_gc(fake.mem_free, fake.collect)
    for _ in range(ceiling):
        assert _put(bus, 40, 100) is True      # 32 entries, all TELEMETRY class
    # 33rd: ceiling full, same class -> evict oldest, ceiling held.
    assert _put(bus, 40, 100) is True
    status = bus.outbound_queue.status()
    assert status["pending"] == ceiling
    assert status["messages_evicted"] == 1
    assert status["messages_rejected"] == 0


def test_pressure_relieved_by_gc_without_eviction(fakes):
    """Below the reserve, a collect restores it -- no queued entry is discarded."""
    reserve = 1000
    bus, fake = _make_bus(fakes, reserve, headroom=900, reclaim_per_collect=500)
    assert _put(bus, 70, 100) is True
    status = bus.outbound_queue.status()
    assert status["pending"] == 1
    assert status["messages_evicted"] == 0        # GC alone restored the reserve
    assert status["messages_rejected"] == 0
    assert fake.collect_count >= 1


def test_pressure_relieved_by_gc_then_priority_eviction(fakes):
    """GC is tried first; if it is insufficient, the least-important entry goes."""
    reserve = 1000
    bus, fake = _make_bus(fakes, reserve, headroom=950, reclaim_per_collect=100)
    assert _put(bus, 70, 200) is True             # A: HEALTH (70)
    # Admitting B pushes the heap under the reserve; GC alone is not enough, so
    # the oldest least-important entry (A) is evicted and B (CRITICAL) is kept.
    assert _put(bus, 10, 200) is True             # B: CRITICAL (10)
    status = bus.outbound_queue.status()
    assert status["pending"] == 1
    assert status["messages_evicted"] == 1
    assert status["messages_rejected"] == 0
    # The survivor is the more-important CRITICAL entry.
    entry = bus.outbound_queue.take()
    assert entry["retention_priority"] == 10
    assert bus.outbound_queue.complete_in_flight(entry)


def test_pressure_never_evicts_more_important_to_keep_less(fakes):
    """A less-important incoming entry is rejected, not the critical entry evicted."""
    reserve = 1000
    bus, fake = _make_bus(fakes, reserve, headroom=980, reclaim_per_collect=50)
    assert _put(bus, 10, 100) is True             # A: CRITICAL (10)
    # B is HEALTH (70), less important than A; under pressure it must be
    # rejected rather than evict the critical entry.
    assert _put(bus, 70, 100) is False
    status = bus.outbound_queue.status()
    assert status["pending"] == 1
    assert status["messages_evicted"] == 0
    assert status["messages_rejected"] == 1
    entry = bus.outbound_queue.take()
    assert entry["retention_priority"] == 10
    assert bus.outbound_queue.complete_in_flight(entry)


def test_pressure_cannot_be_restored_rejects_incoming(fakes):
    """If neither GC nor eviction can restore the reserve, the entry is rejected."""
    reserve = 1000
    # Headroom just under the reserve; reclaim is tiny; nothing is queued yet, so
    # there is no eligible entry to evict.
    bus, fake = _make_bus(fakes, reserve, headroom=500, reclaim_per_collect=50)
    assert _put(bus, 10, 100) is False            # cannot restore reserve -> reject
    status = bus.outbound_queue.status()
    assert status["pending"] == 0
    assert status["messages_rejected"] == 1
    assert status["messages_evicted"] == 0
