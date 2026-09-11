# intercore.py - Four-lane inter-core communication boundary
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import _thread
import gc

from message_serializer import (
    MAX_OUTBOUND_MESSAGE_BYTES,
    MessageTooLargeError,
    NonFiniteFloatError,
    NonStringKeyError,
    SerializationError,
    UnsupportedValueError,
    serialize_and_validate_message,
)


KIND_TELEMETRY = "telemetry"
KIND_COMMAND_RESPONSE = "command_response"
KIND_HEALTH = "health"
KIND_LOG = "log"

# The admitted message kinds (the put()/put_with_kind() validation target).
_KNOWN_KINDS = (KIND_TELEMETRY, KIND_COMMAND_RESPONSE, KIND_HEALTH, KIND_LOG)

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


class OutboundMessageTooLargeError(ValueError):
    """A serialized message beyond MAX_OUTBOUND_MESSAGE_BYTES was rejected.

    A ValueError subclass so permanent-rejection handlers keep working, while a caller can tell a size failure apart from validation/serialization failures."""


class OutboundQueue:
    """Core 1 -> Core 0 queue for MQTT-bound messages only. Payload bytes are
    immutable once put() succeeds (pre-serialized UTF-8 JSON); the Core 0
    envelope keys are injected at publish time. No fixed capacity: the
    preferred reserve marks where pressure handling begins and is never a
    rejection wall, the hard floor is the survival boundary admission must
    protect, CRITICAL the non-evictable retention floor. All decisions run
    under the shared heap-admission lock."""

    def __init__(self, minimum_free_heap_bytes, heap_admission_lock,
                 preferred_free_heap_bytes=None, max_messages=None):
        _require_positive_integer(minimum_free_heap_bytes, "minimum_free_heap_bytes")
        if preferred_free_heap_bytes is None:
            # Single-threshold callers: no pressure band; the hard floor is
            # also the preferred reserve.
            preferred_free_heap_bytes = minimum_free_heap_bytes
        _require_positive_integer(
            preferred_free_heap_bytes, "preferred_free_heap_bytes"
        )
        if preferred_free_heap_bytes < minimum_free_heap_bytes:
            raise ValueError(
                "preferred_free_heap_bytes must be >= minimum_free_heap_bytes"
            )
        # None is unbounded (the historical behavior); a positive integer is a
        # deterministic count ceiling subordinate to the heap policy.
        if max_messages is not None:
            _require_positive_integer(max_messages, "max_messages")
        self._minimum_free_heap_bytes = minimum_free_heap_bytes
        self._preferred_free_heap_bytes = preferred_free_heap_bytes
        self._heap_admission_lock = heap_admission_lock
        self._max_messages = max_messages
        self._queue = []
        self._in_flight = None
        self._lock = _thread.allocate_lock()
        # Observability only -- metrics, not capacity limits: each watermark
        # is the peak of its metric (depth, or retained bytes).
        self._high_watermark = 0
        self._high_watermark_bytes = 0
        self._queued_bytes = 0
        self._messages_evicted = 0
        self._telemetry_evicted = 0
        self._messages_rejected = 0
        self._serialization_rejected = 0
        self._oversized_rejected = 0
        # Discarded at publish time: admitted at or under the ceiling, but the
        # spliced envelope pushed the final wire length over it (permanent
        # for the entry).
        self._oversized_discarded = 0

    def _reserve_restored(self):
        return gc.mem_free() >= self._minimum_free_heap_bytes

    def _preferred_restored(self):
        return gc.mem_free() >= self._preferred_free_heap_bytes

    def _below_count_limit_locked(self):
        """True when one more append keeps the retained-entry depth (queued
        plus the in-flight entry, the same definition as depth/high_watermark)
        at or under the count ceiling; unbounded is always below. Caller
        holds self._lock."""
        if self._max_messages is None:
            return True
        depth = len(self._queue) + (1 if self._in_flight is not None else 0)
        return depth < self._max_messages

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
        """Append under the queue lock; return False (having undone the
        append) if the append's own allocations cross the hard floor (the
        floor is re-measured after the append, since the append allocates)."""
        entry = {
            "kind": kind,
            "retention_priority": retention_priority,
            "payload_bytes": payload_bytes,
        }
        self._queue.append(entry)
        if not self._reserve_restored():
            # The append crossed the reserve: undo it (the counters are
            # untouched, so the admission is fully reversed) and reclaim.
            self._queue.pop()
            gc.collect()
            return False
        self._queued_bytes += len(payload_bytes)
        # Same retained-entry definition as the current depth metric: queued
        # plus the in-flight entry, so the peak can never be below the depth.
        depth = len(self._queue) + (1 if self._in_flight is not None else 0)
        if depth > self._high_watermark:
            self._high_watermark = depth
        if self._queued_bytes > self._high_watermark_bytes:
            self._high_watermark_bytes = self._queued_bytes
        return True

    def _try_append_with_count_gate(self, kind, payload_bytes, retention_priority):
        """Append one entry after making count room (caller holds the
        heap-admission lock, NOT the queue lock). The heap floor is checked by
        _append_locked; the count ceiling is enforced here, subordinate to it
        (only reached where the heap floor already allowed the append): at the
        ceiling, displace one eligible entry under the same retention rule,
        else the append is not made. True if appended, False if the append's
        own allocations crossed the hard floor or the ceiling left nothing
        eligible (the displacement path is the single rejection point, so a
        rejected message is counted exactly once)."""
        with self._lock:
            if self._below_count_limit_locked():
                # Room available: the append carries the post-admission floor
                # check and watermark update in _append_locked.
                return self._append_locked(kind, payload_bytes, retention_priority)
            evicted, _ = self._evict_one_eligible_locked(retention_priority)
            if not evicted:
                return False
        # Count relief reclaimed an entry: reclaim its garbage before the
        # append re-measures the floor, as the heap path does after each
        # displacement (gc is never run under the queue lock).
        gc.collect()
        with self._lock:
            return self._append_locked(kind, payload_bytes, retention_priority)

    def _evict_one_eligible_locked(self, retention_priority):
        """Evict the oldest entry in the least-important class this priority
        may displace (caller holds self._lock). Shared eligibility for
        admission and serialization recovery: no eviction on an empty queue,
        an incoming entry less important than everything queued, or CRITICAL
        (non-evictable floor). Returns (True, worst_priority) on eviction,
        else (False, reason)."""
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
        """Serialize the message, recovering a MemoryError before discarding
        queued data. Only a serializer MemoryError triggers recovery (other
        failures raise immediately): gc.collect() and retry first, then one
        eligible entry per persistent attempt (same eligibility as admission,
        CRITICAL never displaced), gc.collect() after each, until
        serialization succeeds or nothing eligible remains -- then the
        MemoryError propagates to the recovery boundary. No locks are held
        across the serializer or gc.collect()."""
        gc_attempted = False
        while True:
            try:
                return serialize_and_validate_message(message)
            except MemoryError:
                if not gc_attempted:
                    # First failure: reclaim collectable garbage before
                    # discarding any queued data.
                    gc.collect()
                    gc_attempted = True
                    continue
                with self._lock:
                    evicted, _ = self._evict_one_eligible_locked(
                        retention_priority
                    )
                if not evicted:
                    # Nothing this message may reclaim: not queue pressure --
                    # propagate it, do not convert it into a transient
                    # rejection.
                    raise
                gc.collect()
            except (UnsupportedValueError, NonStringKeyError, NonFiniteFloatError) as err:
                raise ValueError("Message validation failed: {}".format(err))
            except MessageTooLargeError as err:
                # Oversized is a permanent failure of the message: raise it,
                # so it is distinguishable from the False (transient) return
                # and from the other permanent rejections.
                self._oversized_rejected += 1
                raise OutboundMessageTooLargeError("Message too large: {}".format(err))
            except SerializationError as err:
                # A serialization failure is likewise permanent for this message.
                self._serialization_rejected += 1
                raise ValueError("Message serialization failed: {}".format(err))

    def _admit_heap_governed(self, kind, payload_bytes, retention_priority):
        """Apply the two-threshold heap admission decision (no locks held on
        entry). The preferred reserve is not a rejection wall: pressure
        reclaims (GC first, then at most one eligible entry) and still admits.
        The hard floor is re-measured before every displacement, so no
        eviction is decided on a pre-collection measurement. A configured
        count ceiling is a second, subordinate constraint, enforced at each
        append via _try_append_with_count_gate() only where the heap floor
        already allows the append: it can add a rejection or a retention-aware
        eviction to make room but never admit what the heap policy would
        reject. False (nothing eligible remained) is the only rejection, and
        is transient: the producer retains and retries."""
        with self._heap_admission_lock:
            if not self._preferred_restored():
                # Pressure band crossed: give GC the first chance to reclaim
                # unreachable objects, before any queued data.
                gc.collect()
            if self._preferred_restored():
                # NORMAL: normal admission (the append still carries the
                # post-admission hard-floor check in _append_locked).
                if self._try_append_with_count_gate(
                    kind, payload_bytes, retention_priority
                ):
                    return True
                # The append's own allocations crossed the hard floor and
                # were undone: the displacement path below decides.
            elif self._reserve_restored():
                # MEMORY PRESSURE (soft): the preferred reserve does not
                # reject an otherwise-valid entry -- reclaim one eligible
                # entry, if any, then admit.
                with self._lock:
                    evicted, _ = self._evict_one_eligible_locked(retention_priority)
                if evicted:
                    # Reclaim before the append measures the floor, as the
                    # hard-pressure path does after each displacement.
                    gc.collect()
                if self._try_append_with_count_gate(
                    kind, payload_bytes, retention_priority
                ):
                    return True
                # The append still crosses the hard floor: the displacement
                # path below decides.

            # HARD PRESSURE: measure the floor before displacing anything --
            # the last rollback/collect may have restored it. Displace the
            # least-important eligible entry, reclaim, and repeat, until the
            # entry is retained or nothing eligible remains.
            while True:
                if self._reserve_restored():
                    if self._try_append_with_count_gate(
                        kind, payload_bytes, retention_priority
                    ):
                        return True
                    # The floor is intact, but the append's own allocations
                    # still cross it: displace the next eligible entry (or
                    # reject once none remain).
                with self._lock:
                    evicted, _ = self._evict_one_eligible_locked(
                        retention_priority
                    )
                    if not evicted:
                        # Nothing eligible can be displaced: reject as
                        # transient -- the producer retains and retries.
                        self._messages_rejected += 1
                        return False
                gc.collect()

    def put(self, kind, message, retention_priority):
        """Admit one MQTT-bound message after validation, serialization, and
        size check. A serializer MemoryError is recovered per
        _serialize_with_recovery() before any queued data is discarded.
        True if admitted, False on transient heap pressure; ValueError
        (OutboundMessageTooLargeError for the size case) on a permanent
        failure of the message itself."""
        if kind not in _KNOWN_KINDS:
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

        # Serialize the actual message (recovery per _serialize_with_recovery);
        # admission below keeps its own post-serialization reserve check.
        payload_bytes = self._serialize_with_recovery(message, retention_priority)

        return self._admit_heap_governed(kind, payload_bytes, retention_priority)

    def put_with_kind(self, kind, payload_bytes, retention_priority):
        """Admit one MQTT-bound message from pre-serialized, UTF-8 encoded JSON bytes.

        The per-message ceiling is enforced here, not by the caller: a payload beyond MAX_OUTBOUND_MESSAGE_BYTES raises OutboundMessageTooLargeError (a ValueError subclass). Returns True if admitted, False on transient heap pressure."""
        if kind not in _KNOWN_KINDS:
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
        # enforces; the bytes are already final, only their length matters.
        if len(payload_bytes) > MAX_OUTBOUND_MESSAGE_BYTES:
            # Oversized is a permanent failure of the message: raise, the
            # same as the put() serialization path (queue state untouched).
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
                # The configured count ceiling (None when unbounded); the
                # high_watermark it bounds is the metric just below.
                "max_messages": self._max_messages,
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
                gc.collect()
            if gc.mem_free() >= self._minimum_free_heap_bytes:
                with self._lock:
                    self._queue.append(event)
                    # Post-admission invariant: the append itself may
                    # allocate, so the reserve must hold with the event
                    # retained.
                    if gc.mem_free() < self._minimum_free_heap_bytes:
                        self._queue.pop()
                        gc.collect()
                        self._rejected += 1
                        return False
                    depth = len(self._queue)
                    if depth > self._high_watermark:
                        self._high_watermark = depth
                    return True
            # Admitted events are never evicted: reject the new event and let
            # the caller report the memory-pressure failure.
            self._rejected += 1
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


class InterCore:
    """Container exposing the explicit inter-core communication lanes.

    The two FIFO lanes are heap-governed by the same board-specific free-heap thresholds -- the preferred reserve (start of pressure handling) and the minimum (hard survival floor) -- serialized on one shared heap-admission lock (the heap is global to both cores); the latest-value state snapshots are plain allocate_lock-guarded mailboxes."""

    def __init__(self, minimum_free_heap_bytes, preferred_free_heap_bytes=None,
                 outbound_queue_max_messages=None):
        _require_positive_integer(minimum_free_heap_bytes, "minimum_free_heap_bytes")
        if preferred_free_heap_bytes is None:
            # Single-threshold callers: the hard floor is also the preferred
            # reserve.
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
            outbound_queue_max_messages,
        )
        # The event lane has no evictable retained traffic (admitted events
        # are never displaced), so it gates on the hard survival floor alone.
        self.event_queue = InterCoreEventQueue(
            minimum_free_heap_bytes, self._heap_admission_lock
        )
        self.state_mailboxes = StateMailboxes()
