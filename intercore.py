# intercore.py - Three-lane inter-core communication boundary
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import _thread
import gc

from debug import DEBUG_QUEUE_MEMORY


KIND_TELEMETRY = "telemetry"
KIND_COMMAND_RESPONSE = "command_response"
KIND_HEALTH = "health"
KIND_LOG = "log"

# Lower numeric values are more important and are retained preferentially.
RETENTION_PRIORITY_CRITICAL = 10
RETENTION_PRIORITY_ERROR = 20
RETENTION_PRIORITY_WARN = 30
RETENTION_PRIORITY_TELEMETRY = 40
RETENTION_PRIORITY_INFO = 50
RETENTION_PRIORITY_HEALTH = 70

RETENTION_PRIORITY_MIN = RETENTION_PRIORITY_CRITICAL
RETENTION_PRIORITY_MAX = RETENTION_PRIORITY_HEALTH


def _require_positive_integer(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("{} must be a positive integer".format(name))


def _debug_queue_memory(prefix, queue, event, extra=None):
    """Temporary heap-reserve validation instrumentation (DEBUG_QUEUE_MEMORY); remove after validation.

    One grep-friendly line of heap and queue state at a meaningful queue event; call while holding the queue's lock where the event touches queue state."""
    if not DEBUG_QUEUE_MEMORY:
        return
    heap_free_bytes = gc.mem_free()
    parts = ["event={}".format(event)]
    if extra:
        for key, value in extra:
            parts.append("{}={}".format(key, value))
    parts.extend(
        (
            "heap_alloc_bytes={}".format(gc.mem_alloc()),
            "heap_free_bytes={}".format(heap_free_bytes),
            "minimum_free_heap_bytes={}".format(queue._minimum_free_heap_bytes),
            "heap_headroom_bytes={}".format(
                heap_free_bytes - queue._minimum_free_heap_bytes
            ),
            "queue_count={}".format(len(queue._queue)),
            "queue_high_watermark={}".format(queue._high_watermark),
        )
    )
    # The event queue does not track retained payload bytes (events are not
    # pre-serialized), so that field is omitted for it.
    queued_bytes = getattr(queue, "_queued_bytes", None)
    if queued_bytes is not None:
        parts.append("queued_bytes={}".format(queued_bytes))
    print("[DEBUG] {} {}".format(prefix, " ".join(parts)))


class OutboundMessageTooLargeError(ValueError):
    """A serialized message beyond MAX_OUTBOUND_MESSAGE_BYTES was rejected.

    A ValueError subclass so permanent-rejection handlers keep working, while a caller can tell a size failure apart from validation/serialization failures."""


class OutboundQueue:
    """Core 1 -> Core 0 queue for MQTT-bound messages only.

    Once put() succeeds the payload bytes are immutable (pre-serialized UTF-8 JSON); the Core 0 envelope keys are injected at publish time and must not be carried at the top level. No fixed capacity: admission is governed by two heap thresholds. The preferred free-heap reserve marks the beginning of memory-pressure handling -- gc.collect() first, then an increased willingness to reclaim low-retention eligible entries -- but it is never a rejection wall by itself; the minimum free-heap threshold is the hard survival floor that admission must protect, and only below it (after GC) may expendable incoming traffic be rejected. Reclamation -- explicit heap pressure, or an append whose own allocations cross the floor (never the in-flight QoS 1 entry) -- always uses the existing retention-priority eligibility, and all decisions run under the shared heap-admission lock. CRITICAL is the non-evictable retention floor: an admitted CRITICAL entry cannot be displaced by another CRITICAL (its producer no longer owns it once admitted, so the new one is rejected for the producer to retry). The hard floor is a post-admission invariant: an entry may be retained only while gc.mem_free() is at or above it, re-measured after the append's own allocations."""

    def __init__(self, minimum_free_heap_bytes, heap_admission_lock,
                 preferred_free_heap_bytes=None):
        _require_positive_integer(minimum_free_heap_bytes, "minimum_free_heap_bytes")
        if preferred_free_heap_bytes is None:
            # Single-threshold callers keep the old behavior: no pressure
            # band, the hard floor is also the preferred reserve.
            preferred_free_heap_bytes = minimum_free_heap_bytes
        _require_positive_integer(
            preferred_free_heap_bytes, "preferred_free_heap_bytes"
        )
        if preferred_free_heap_bytes < minimum_free_heap_bytes:
            raise ValueError(
                "preferred_free_heap_bytes must be >= minimum_free_heap_bytes"
            )
        self._minimum_free_heap_bytes = minimum_free_heap_bytes
        self._preferred_free_heap_bytes = preferred_free_heap_bytes
        self._heap_admission_lock = heap_admission_lock
        self._queue = []
        self._in_flight = None
        self._lock = _thread.allocate_lock()
        # Observability only -- metrics, not capacity limits. The watermark
        # is the peak of the depth metric (queued + in-flight), as
        # _high_watermark_bytes is the peak of the retained-bytes metric.
        self._high_watermark = 0
        self._high_watermark_bytes = 0
        self._queued_bytes = 0
        # Entries admitted since the queue last fully drained: supports the
        # DEBUG_QUEUE_MEMORY "drained" event without logging steady-state
        # single-entry completion (validation instrumentation only).
        self._backlog_depth = 0
        self._messages_evicted = 0
        self._telemetry_evicted = 0
        self._messages_rejected = 0
        self._serialization_rejected = 0
        self._oversized_rejected = 0
        # Discarded at publish time: the body was admitted at or under the
        # ceiling, but the spliced Core 0 envelope pushed the final wire
        # length over it. Permanent for the entry (retrying re-fails), so it
        # is discarded instead of held in flight.
        self._oversized_discarded = 0

    def _reserve_restored(self):
        return gc.mem_free() >= self._minimum_free_heap_bytes

    def _preferred_restored(self):
        return gc.mem_free() >= self._preferred_free_heap_bytes

    def _evict_oldest_by_priority_locked(self, retention_priority):
        for index, entry in enumerate(self._queue):
            if entry["retention_priority"] == retention_priority:
                evicted = self._queue.pop(index)
                self._queued_bytes -= len(evicted["payload_bytes"])
                self._messages_evicted += 1
                if evicted["kind"] == KIND_TELEMETRY:
                    self._telemetry_evicted += 1
                return True
        return False

    def _append_locked(self, kind, payload_bytes, retention_priority):
        """Append under the queue lock; return False (having undone the append) if the append's own allocations cross the hard floor.

        The documented invariant is post-admission -- an entry may be retained only while gc.mem_free() is at or above the hard floor -- so the heap is measured after the append (which allocates the entry dict and any list growth), not only before it."""
        entry = {
            "kind": kind,
            "retention_priority": retention_priority,
            "payload_bytes": payload_bytes,
        }
        self._queue.append(entry)
        if not self._reserve_restored():
            # The append crossed the reserve: undo it (counters above are
            # untouched, so the admission is fully reversed) and reclaim, so
            # the heap is measurable again for the next admission decision.
            self._queue.pop()
            gc.collect()
            return False
        self._queued_bytes += len(payload_bytes)
        self._backlog_depth += 1
        # Same retained-entry definition as the current depth metric: queued
        # plus the in-flight entry, so the peak can never be below the depth.
        depth = len(self._queue) + (1 if self._in_flight is not None else 0)
        if depth > self._high_watermark:
            self._high_watermark = depth
        if self._queued_bytes > self._high_watermark_bytes:
            self._high_watermark_bytes = self._queued_bytes
        return True

    def _evict_one_eligible_locked(self, retention_priority):
        """Evict the oldest entry in the least-important class this priority may displace; caller holds self._lock.

        The one shared eligibility rule for both the admission pressure path and the serialization-recovery path: an empty queue has nothing to reclaim, an incoming entry less important than everything queued may not evict, and CRITICAL is the non-evictable retention floor (an admitted CRITICAL is never displaced, not even by another CRITICAL). Returns (True, worst_priority) if an entry was evicted (counted by the regular eviction metrics; worst_priority names the displaced class), else (False, reason) with reason "memory_pressure" (queue empty), "lower_priority_than_queued", or "critical_not_evictable" -- the caller decides what a non-eviction means (a transient admission rejection, or an unrecovered serialization MemoryError that must propagate)."""
        if not self._queue:
            return (False, "memory_pressure")
        # Explicit loop: no generator/list allocation in the
        # pressure path (MCU-safe).
        worst_priority = RETENTION_PRIORITY_MIN
        for entry in self._queue:
            if entry["retention_priority"] > worst_priority:
                worst_priority = entry["retention_priority"]
        # Incoming is less important than everything queued: no eviction.
        if retention_priority > worst_priority:
            return (False, "lower_priority_than_queued")
        # CRITICAL is the non-evictable retention floor: never displace an
        # admitted CRITICAL for another CRITICAL.
        if (
            worst_priority == RETENTION_PRIORITY_CRITICAL
            and retention_priority == RETENTION_PRIORITY_CRITICAL
        ):
            return (False, "critical_not_evictable")
        self._evict_oldest_by_priority_locked(worst_priority)
        return (True, worst_priority)

    def _serialize_with_recovery(self, message, retention_priority):
        """Serialize the message, recovering a MemoryError without discarding queued data first.

        The serializer works on the actual message -- no fixed worst-case threshold stands in for it. Only a MemoryError from the serializer triggers recovery (validation, size, and serialization failures raise immediately, as before). The first failure runs gc.collect() and retries: fragmentation or collectable garbage can resolve it with no data loss. Only if serialization still fails does the queue reclaim memory -- one eviction per attempt, the oldest entry in the least-important class this message may displace under the same eligibility rule as admission (CRITICAL never displaced), each re-claimed with gc.collect() -- until serialization succeeds or nothing eligible remains, in which case the MemoryError propagates to the firmware recovery boundary. The loop is bounded by the number of eligible retained entries, so it terminates without a retry count. No locks are held across the serializer or gc.collect(): only the eviction step takes the queue lock."""
        from message_serializer import (
            serialize_and_validate_message,
            MessageTooLargeError,
            UnsupportedValueError,
            NonStringKeyError,
            NonFiniteFloatError,
            SerializationError,
        )
        gc_attempted = False
        while True:
            try:
                # Returning here exits the retry loop on success; the
                # MemoryError branch below falls back to the top of the loop.
                return serialize_and_validate_message(message)
            except MemoryError:
                if not gc_attempted:
                    # First failure: reclaim collectable garbage before
                    # discarding any queued data.
                    _debug_queue_memory(
                        "outbound_queue", self, "serialization_memory_error"
                    )
                    gc.collect()
                    gc_attempted = True
                    continue
                with self._lock:
                    evicted, evicted_priority = self._evict_one_eligible_locked(
                        retention_priority
                    )
                if not evicted:
                    # No queue-owned memory this message may reclaim: the
                    # failure is not queue pressure -- propagate it, do not
                    # convert it into a transient rejection.
                    _debug_queue_memory(
                        "outbound_queue",
                        self,
                        "serialization_memory_error",
                        (("outcome", "unrecovered"),),
                    )
                    raise
                _debug_queue_memory(
                    "outbound_queue",
                    self,
                    "evict",
                    (("evicted_priority", evicted_priority),),
                )
                gc.collect()
            except (UnsupportedValueError, NonStringKeyError, NonFiniteFloatError) as err:
                # Validation errors are raised immediately
                raise ValueError("Message validation failed: {}".format(err))
            except MessageTooLargeError as err:
                # Oversized is a permanent failure of the message (the ceiling
                # is a static invariant): raise, so a caller can distinguish
                # it from the False (transient, retry later) return. Queue
                # state is untouched, as before. OutboundMessageTooLargeError
                # (a ValueError subclass) lets a caller tell this size failure
                # apart from the other permanent rejections.
                self._oversized_rejected += 1
                raise OutboundMessageTooLargeError("Message too large: {}".format(err))
            except SerializationError as err:
                # A serialization failure is likewise permanent for this message.
                self._serialization_rejected += 1
                raise ValueError("Message serialization failed: {}".format(err))

    def _admit_heap_governed(self, kind, payload_bytes, retention_priority):
        """Apply the two-threshold heap admission decision. No locks are held on entry.

        NORMAL (free heap at/above the preferred reserve): fast path -- admit, no gc.collect(), no pressure eviction. MEMORY PRESSURE (free heap at or above the hard floor but below the preferred reserve): gc.collect() first -- if it restores the preferred reserve the admission is normal -- otherwise one eligible lower-priority entry may be reclaimed (increased willingness to discard low-retention traffic; the preferred reserve is not a rejection wall) and the entry is still admitted. HARD PRESSURE (free heap below the hard floor after GC, or an append whose own allocations cross it): reclaim eligible entries one at a time, gc.collect() after each, until the entry can be retained with the hard floor intact -- or reject it as transient (the producer retains and retries) when nothing eligible remains. The hard floor is re-measured after the append itself: an admission whose own allocations cross it is undone and the same priority-displacement rules decide -- a more important admission is not rejected while a less important entry stays retained, and no state may ever evict a retained CRITICAL."""
        with self._heap_admission_lock:
            if not self._preferred_restored():
                # Memory pressure (preferred reserve or hard floor crossed):
                # give GC the first chance to reclaim unreachable objects,
                # before any queued data is considered.
                _debug_queue_memory("outbound_queue", self, "memory_pressure")
                _debug_queue_memory("outbound_queue", self, "gc_before_admission")
                gc.collect()
                _debug_queue_memory("outbound_queue", self, "gc_after_admission")
            if self._preferred_restored():
                # NORMAL: normal admission (the append still carries the
                # post-admission hard-floor check in _append_locked).
                with self._lock:
                    if self._append_locked(kind, payload_bytes, retention_priority):
                        # Log after the append: the line reports the
                        # post-admission state (count and bytes include the
                        # admitted entry), matching the gc_after pair above it.
                        _debug_queue_memory("outbound_queue", self, "admit")
                        return True
                # The append's own allocations crossed the hard floor and
                # were undone: the displacement path below decides (its
                # priority rules and rejection conditions apply unchanged).
                _debug_queue_memory(
                    "outbound_queue",
                    self,
                    "append_crossed",
                    (("priority", retention_priority),),
                )
            elif self._reserve_restored():
                # MEMORY PRESSURE (soft): the device is still operational, so
                # the preferred reserve does not reject an otherwise-valid
                # entry. Reclaim one eligible entry, if any, then admit --
                # not a bulk flush, and never of a class this priority may
                # not displace.
                with self._lock:
                    evicted, evicted_priority = self._evict_one_eligible_locked(
                        retention_priority
                    )
                if evicted:
                    _debug_queue_memory(
                        "outbound_queue",
                        self,
                        "evict",
                        (("evicted_priority", evicted_priority),),
                    )
                with self._lock:
                    if self._append_locked(kind, payload_bytes, retention_priority):
                        _debug_queue_memory("outbound_queue", self, "admit")
                        return True
                # The append's own allocations still cross the hard floor
                # (the soft-reclamation did not suffice, or the append
                # crossed it on its own): the displacement path below
                # decides.
                _debug_queue_memory(
                    "outbound_queue",
                    self,
                    "append_crossed",
                    (("priority", retention_priority),),
                )

            # HARD PRESSURE -- explicit (below the hard floor after GC), or
            # an append whose own allocations cross it: displace the
            # least-important eligible entries one at a time, reclaiming
            # after each, until the hard floor is restored and the entry is
            # retained, or nothing eligible remains.
            while True:
                with self._lock:
                    evicted, detail = self._evict_one_eligible_locked(
                        retention_priority
                    )
                    if not evicted:
                        # Nothing eligible can be displaced (queue empty, the
                        # incoming entry is less important than everything
                        # queued, or both are CRITICAL): reject as transient --
                        # the producer retains and retries.
                        self._messages_rejected += 1
                        _debug_queue_memory(
                            "outbound_queue",
                            self,
                            "reject",
                            (("reason", detail),
                             ("priority", retention_priority)),
                        )
                        return False
                _debug_queue_memory(
                    "outbound_queue",
                    self,
                    "evict",
                    (("evicted_priority", detail),),
                )
                gc.collect()
                if self._reserve_restored():
                    with self._lock:
                        if self._append_locked(kind, payload_bytes, retention_priority):
                            _debug_queue_memory("outbound_queue", self, "admit")
                            return True
                    # The eviction restored the floor, but the append's own
                    # allocations still cross it: displace the next eligible
                    # entry (or reject once none remain) rather than
                    # retaining lower-priority entries while rejecting this
                    # one.
                    _debug_queue_memory(
                        "outbound_queue",
                        self,
                        "append_crossed",
                        (("priority", retention_priority),),
                    )

    def put(self, kind, message, retention_priority):
        """Admit one MQTT-bound message after validation, serialization, encoding, and size check.

        Serialization works on the actual message: a MemoryError from it runs gc.collect() first, then (if it persists) reclaims one eligible queued entry at a time under the same retention policy as admission, retrying after each, until serialization succeeds or no eligible entry remains -- in which case the MemoryError propagates to the firmware recovery boundary. The two-threshold heap-governed admission then decides whether the serialized payload may be retained (NORMAL above the preferred reserve; MEMORY PRESSURE between the preferred reserve and the hard floor, where GC and one eligible reclamation run but the entry is still admitted; HARD PRESSURE below the hard floor after GC, where expendable traffic may be rejected). Returns True if admitted, False on transient heap pressure (a later retry may succeed). Raises ValueError on a permanent failure of the message itself (unsupported value, serialization, or size beyond MAX_OUTBOUND_MESSAGE_BYTES); the oversized case raises OutboundMessageTooLargeError, a ValueError subclass."""
        if kind not in (KIND_TELEMETRY, KIND_COMMAND_RESPONSE, KIND_HEALTH, KIND_LOG):
            raise ValueError("Unsupported outbound message kind: {}".format(kind))
        if not isinstance(message, dict):
            raise ValueError("outbound message must be a dictionary")
        if isinstance(retention_priority, bool) or not isinstance(retention_priority, int):
            raise ValueError("retention_priority must be an integer")
        if not RETENTION_PRIORITY_MIN <= retention_priority <= RETENTION_PRIORITY_MAX:
            raise ValueError(
                "retention_priority must be between {} and {}".format(
                    RETENTION_PRIORITY_MIN, RETENTION_PRIORITY_MAX
                )
            )

        # Serialize the actual message (with MemoryError recovery: gc first,
        # then one eligible queued entry per persistent failure, under the
        # same retention policy as admission). Admission below keeps its own
        # post-serialization reserve check.
        payload_bytes = self._serialize_with_recovery(message, retention_priority)

        return self._admit_heap_governed(kind, payload_bytes, retention_priority)

    def put_with_kind(self, kind, payload_bytes, retention_priority):
        """Admit one MQTT-bound message from pre-serialized, UTF-8 encoded JSON bytes.

        The per-message ceiling is enforced here, not by the caller: a payload beyond MAX_OUTBOUND_MESSAGE_BYTES raises OutboundMessageTooLargeError (a ValueError subclass). Returns True if admitted, False on transient heap pressure."""
        if kind not in (KIND_TELEMETRY, KIND_COMMAND_RESPONSE, KIND_HEALTH, KIND_LOG):
            raise ValueError("Unsupported outbound message kind: {}".format(kind))
        if not isinstance(payload_bytes, (bytes, bytearray)):
            raise ValueError("payload_bytes must be bytes")
        if isinstance(retention_priority, bool) or not isinstance(retention_priority, int):
            raise ValueError("retention_priority must be an integer")
        if not RETENTION_PRIORITY_MIN <= retention_priority <= RETENTION_PRIORITY_MAX:
            raise ValueError(
                "retention_priority must be between {} and {}".format(
                    RETENTION_PRIORITY_MIN, RETENTION_PRIORITY_MAX
                )
            )

        # Enforce the same per-message ceiling the put() serialization path
        # enforces, so the queue's own boundary holds on both admission paths.
        # No re-parsing or JSON allocation: the bytes are already final, only
        # their length matters.
        from message_serializer import MAX_OUTBOUND_MESSAGE_BYTES
        if len(payload_bytes) > MAX_OUTBOUND_MESSAGE_BYTES:
            # Oversized is a permanent failure of the message: raise, the
            # same as the put() serialization path. Queue state is untouched.
            # OutboundMessageTooLargeError (a ValueError subclass) keeps this
            # size failure distinguishable for callers, as in put().
            self._oversized_rejected += 1
            raise OutboundMessageTooLargeError(
                "Message too large: {} bytes exceeds the {} byte per-message limit".format(
                    len(payload_bytes), MAX_OUTBOUND_MESSAGE_BYTES
                )
            )

        return self._admit_heap_governed(kind, payload_bytes, retention_priority)

    def take(self):
        """Return the current in-flight entry or move one queued entry into it."""
        with self._lock:
            if self._in_flight is not None:
                return self._in_flight
            if not self._queue:
                return None
            self._in_flight = self._queue.pop(0)
            # The entry left the queued FIFO, but its payload is still retained
            # (freed only when complete_in_flight releases it), so it stays
            # counted in the retained-byte metric.
            return self._in_flight

    def complete_in_flight(self, entry, discarded=False):
        """Release the in-flight entry: a completed publish, or a permanent publish-time discard (``discarded=True``, counted separately)."""
        with self._lock:
            if self._in_flight is entry:
                self._in_flight = None
                # The in-flight payload is released only now (PUBACK received,
                # or the entry is discarded), so its bytes leave the
                # retained-byte metric here.
                self._queued_bytes -= len(entry["payload_bytes"])
                if discarded:
                    self._oversized_discarded += 1
                if not self._queue:
                    # The queue is fully drained. Log only if a backlog (more
                    # than this one in-flight entry) flowed through since the
                    # last drain, so steady-state single-message completion
                    # stays quiet even with DEBUG_QUEUE_MEMORY enabled.
                    if self._backlog_depth > 1:
                        _debug_queue_memory("outbound_queue", self, "drained")
                    self._backlog_depth = 0
                return True
            return False

    def has_in_flight(self):
        with self._lock:
            return self._in_flight is not None

    def status(self):
        """Return the queue's observability metrics (metrics, not capacity limits).

        depth/pending are the entry view, queued_bytes the retained-payload view; both include the in-flight entry, and each high watermark is the peak of its metric since boot, read under one lock."""
        with self._lock:
            in_flight = self._in_flight is not None
            return {
                "pending": len(self._queue),
                "depth": len(self._queue) + (1 if in_flight else 0),
                "in_flight": in_flight,
                "queued_bytes": self._queued_bytes,
                "high_watermark": self._high_watermark,
                "high_watermark_bytes": self._high_watermark_bytes,
                "messages_evicted": self._messages_evicted,
                "telemetry_evicted": self._telemetry_evicted,
                "messages_rejected": self._messages_rejected,
                "serialization_rejected": self._serialization_rejected,
                "oversized_rejected": self._oversized_rejected,
                "oversized_discarded": self._oversized_discarded,
            }

    def get_depth(self):
        with self._lock:
            return len(self._queue) + (1 if self._in_flight is not None else 0)


class InterCoreEventQueue:
    """Private FIFO for discrete Core 0 -> Core 1 events (never MQTT-bound by queue membership).

    No fixed capacity: admission is governed by the same hard free-heap floor under the same shared heap-admission lock, and the floor is the same post-admission invariant as on the outbound queue (re-measured after the append's own allocations). Admitted events are never evicted -- under pressure, the new event is rejected instead."""

    def __init__(self, minimum_free_heap_bytes, heap_admission_lock):
        _require_positive_integer(minimum_free_heap_bytes, "minimum_free_heap_bytes")
        self._minimum_free_heap_bytes = minimum_free_heap_bytes
        self._heap_admission_lock = heap_admission_lock
        self._queue = []
        self._lock = _thread.allocate_lock()
        self._high_watermark = 0
        self._rejected = 0

    def put(self, event):
        """Admit one event, or reject it under memory pressure (never evicting an admitted event)."""
        if not isinstance(event, dict):
            raise ValueError("inter-core event must be a dictionary")

        # After successful admission, event is immutable.
        with self._heap_admission_lock:
            if gc.mem_free() < self._minimum_free_heap_bytes:
                _debug_queue_memory("event_queue", self, "memory_pressure")
                _debug_queue_memory("event_queue", self, "gc_before_admission")
                gc.collect()
                _debug_queue_memory("event_queue", self, "gc_after_admission")
            if gc.mem_free() >= self._minimum_free_heap_bytes:
                with self._lock:
                    self._queue.append(event)
                    # Post-admission invariant, as on the outbound queue: the
                    # append itself may allocate (list growth), so the
                    # reserve must hold with the event retained, not only
                    # before the append.
                    if gc.mem_free() < self._minimum_free_heap_bytes:
                        self._queue.pop()
                        gc.collect()
                        self._rejected += 1
                        _debug_queue_memory(
                            "event_queue",
                            self,
                            "reject",
                            (("reason", "post_admission_reserve"),),
                        )
                        return False
                    depth = len(self._queue)
                    if depth > self._high_watermark:
                        self._high_watermark = depth
                    _debug_queue_memory("event_queue", self, "admit")
                    return True
            # Admitted events are never evicted: reject the new event and let
            # the caller report the memory-pressure failure.
            self._rejected += 1
            _debug_queue_memory(
                "event_queue",
                self,
                "reject",
                (("reason", "memory_pressure"),),
            )
            return False

    def take(self):
        with self._lock:
            if not self._queue:
                return None
            return self._queue.pop(0)

    def status(self):
        with self._lock:
            return {
                "pending": len(self._queue),
                "high_watermark": self._high_watermark,
                "rejected": self._rejected,
            }


class StateMailboxes:
    """Latest-value immutable snapshots shared between cores; state replaces rather than queues."""

    def __init__(self):
        self._lock = _thread.allocate_lock()
        self._network_snapshot = None
        self._utc_snapshot = None
        self._core_1_activity_ms = None
        self._hardware = None

    def set_network_snapshot(self, snapshot):
        if not isinstance(snapshot, dict):
            raise ValueError("network snapshot must be a dictionary")
        with self._lock:
            self._network_snapshot = snapshot

    def get_network_snapshot(self):
        with self._lock:
            return self._network_snapshot

    def set_utc_snapshot(self, snapshot):
        if not isinstance(snapshot, dict):
            raise ValueError("UTC snapshot must be a dictionary")
        with self._lock:
            self._utc_snapshot = snapshot

    def set_core_1_activity_ms(self, activity_ms):
        if isinstance(activity_ms, bool) or not isinstance(activity_ms, int):
            raise ValueError("activity_ms must be an integer")
        with self._lock:
            self._core_1_activity_ms = activity_ms

    def get_core_1_activity_ms(self):
        with self._lock:
            return self._core_1_activity_ms

    def set_hardware(self, hardware):
        if not isinstance(hardware, dict):
            raise ValueError("hardware must be a dictionary")
        with self._lock:
            self._hardware = hardware

    def get_hardware(self):
        with self._lock:
            return self._hardware

    def get_utc_snapshot(self):
        with self._lock:
            return self._utc_snapshot


class ConfigUpdateLane:
    """A narrow request/result lane for the HOT_RELOADED configuration apply.

    This is internal runtime control, not an external command: Core 0 posts a
    request carrying the Core 1-owned hot subset (only the keys that changed,
    plus a monotonic ``generation``), and Core 1 posts exactly one result for
    that generation (success, or a bounded failure descriptor). Latest-value
    mailboxes -- state replaces rather than queues -- under the same
    allocate_lock discipline as StateMailboxes, so a reader never spins on
    unlocked shared state. One transaction is in flight at a time (Core 0
    enforces it), and the generation keeps a stale result from being mistaken
    for the current one."""

    def __init__(self):
        self._lock = _thread.allocate_lock()
        self._request = None
        self._result = None

    def post_request(self, request):
        if not isinstance(request, dict):
            raise ValueError("config update request must be a dictionary")
        with self._lock:
            self._request = request

    def take_request(self):
        """The pending request (cleared on read), or None when none is pending."""
        with self._lock:
            request = self._request
            self._request = None
            return request

    def post_result(self, result):
        if not isinstance(result, dict):
            raise ValueError("config update result must be a dictionary")
        with self._lock:
            self._result = result

    def take_result_for(self, generation):
        """The result posted for this generation (cleared on a match), else None.

        A result for a different generation is left in place for its owner; a
        match is consumed so it is read exactly once."""
        with self._lock:
            result = self._result
            if result is not None and result.get("generation") == generation:
                self._result = None
                return result
            return None


class InterCore:
    """Container exposing the explicit inter-core communication lanes.

    The two FIFO lanes are heap-governed by the same board-specific free-heap thresholds -- the preferred reserve (start of pressure handling) and the minimum (hard survival floor) -- serialized on one shared heap-admission lock (the heap is global to both cores); the latest-value lanes (state snapshots, config-update request/result) are plain allocate_lock-guarded mailboxes."""

    def __init__(self, minimum_free_heap_bytes, preferred_free_heap_bytes=None):
        _require_positive_integer(minimum_free_heap_bytes, "minimum_free_heap_bytes")
        if preferred_free_heap_bytes is None:
            # Single-threshold callers keep the old behavior: the hard floor
            # is also the preferred reserve (no pressure band).
            preferred_free_heap_bytes = minimum_free_heap_bytes
        _require_positive_integer(
            preferred_free_heap_bytes, "preferred_free_heap_bytes"
        )
        if preferred_free_heap_bytes < minimum_free_heap_bytes:
            raise ValueError(
                "preferred_free_heap_bytes must be >= minimum_free_heap_bytes"
            )
        self.minimum_free_heap_bytes = minimum_free_heap_bytes
        self.preferred_free_heap_bytes = preferred_free_heap_bytes
        self._heap_admission_lock = _thread.allocate_lock()
        self.outbound_queue = OutboundQueue(
            minimum_free_heap_bytes,
            self._heap_admission_lock,
            preferred_free_heap_bytes,
        )
        # The event lane has no evictable retained traffic (admitted events
        # are never displaced), so it gates on the hard survival floor alone.
        self.event_queue = InterCoreEventQueue(
            minimum_free_heap_bytes, self._heap_admission_lock
        )
        self.state_mailboxes = StateMailboxes()
        self.config_update_lane = ConfigUpdateLane()
