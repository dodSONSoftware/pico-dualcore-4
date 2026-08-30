# intercore.py - Four-lane inter-core communication boundary
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import _thread
import gc
import time


KIND_TELEMETRY = "telemetry"
KIND_COMMAND_RESPONSE = "command_response"
KIND_HEALTH = "health"
KIND_LOG = "log"

# Config transaction mailbox actions (write_config transaction coordination).
CONFIG_TX_ACTION_APPLY = "apply"
CONFIG_TX_ACTION_COMMIT = "commit"
CONFIG_TX_ACTION_ROLLBACK = "rollback"

# Lower numeric values are more important and are retained preferentially.
RETENTION_PRIORITY_CRITICAL = 10
RETENTION_PRIORITY_ERROR = 20
RETENTION_PRIORITY_WARN = 30
RETENTION_PRIORITY_TELEMETRY = 40
RETENTION_PRIORITY_INFO = 50
RETENTION_PRIORITY_HEALTH = 70

RETENTION_PRIORITY_MIN = RETENTION_PRIORITY_CRITICAL
RETENTION_PRIORITY_MAX = RETENTION_PRIORITY_HEALTH

# Pathological sanity guard on how many entries the outbound queue may hold.
#
# This is NOT a user-tunable queue size and NOT the memory-safety boundary.
# Admission is governed by the board's free-heap reserve (see MemoryStats and
# OutboundQueue._relieve_memory_pressure): the reserve is defended by a
# required collect and by priority eviction at ANY queue depth. The ceiling
# exists only to stop an unbounded queue in a degenerate case (healthy heap but
# relentless traffic), bounding worst-case retained entries without relying on a
# per-message byte assumption or a heap percentage. It must not be exposed as a
# config knob.
MAX_OUTBOUND_QUEUE_ENTRIES = 64

# Free-heap reserve used ONLY when the caller does not supply the detected
# board's reserve (e.g. host tests). This is the conservative Pico W value and
# is NOT a second source of truth: production always passes the hardware-detected
# reserve (hardware.py) at boot, and the board-specific constants there remain
# the single source of truth for real boards.
DEFAULT_MINIMUM_FREE_HEAP_BYTES = 64 * 1024

# Minimum spacing between OPTIONAL (non-pressure) controlled gc.collect() calls,
# so a busy core never collects twice in quick succession. The memory-pressure
# path treats the reserve as a safety invariant and bypasses this cooldown.
CONTROLLED_GC_MIN_INTERVAL_MS = 1000


class MemoryStats:
    """Lock-protected runtime memory statistics shared by both cores.

    Tracks one historical low-watermark scalar -- the lowest free heap observed
    at an explicit checkpoint (``minimum_free_heap_observed_bytes``) -- plus the
    controlled ``gc.collect()`` statistics. It is the single shared source of
    truth for the reserve the outbound queue defends.

    The minimum is a diagnostic low-watermark, not a guarantee: it is updated at
    explicit checkpoints (the health report, the Core 0 publish boundary, and
    every controlled collect) and is NOT guaranteed to capture a trough that
    occurs and recovers between two checkpoints. It must therefore never be
    called an "absolute minimum," and it must never degrade current health
    (``free_heap < reserve`` is the only heap-based degraded reason).

    Rules this class enforces:
      * ``gc.collect()`` is never called while holding this object's lock.
      * No observation performs a collect; only ``collect``/``collect_if_below`` do.
      * ``MemoryError`` propagates (heap exhaustion is fatal) and is never
        swallowed or retried behind a broad ``except Exception``.
      * The state is a fixed set of scalars -- no per-collect history, no
        per-message memory history, no dynamic sample lists.
    """

    def __init__(self, initial_free_heap_bytes=None):
        self._lock = _thread.allocate_lock()
        if initial_free_heap_bytes is not None:
            self._validate_free_bytes(initial_free_heap_bytes)
            self._minimum_free_heap_observed = initial_free_heap_bytes
        else:
            self._minimum_free_heap_observed = None
        self._gc_collect_count = 0
        self._gc_bytes_reclaimed = 0
        self._gc_total_reclaimed_bytes = 0
        self._gc_last_duration_ms = 0
        self._gc_max_duration_ms = 0
        self._last_optional_gc_ms = None

    @staticmethod
    def _validate_free_bytes(value):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("free heap bytes must be a non-negative integer")

    def _observe_locked(self, free_bytes):
        """Update the low-watermark minimum from an explicit checkpoint. Lock held."""
        current = self._minimum_free_heap_observed
        if current is None or free_bytes < current:
            self._minimum_free_heap_observed = free_bytes

    def observe_free_heap(self, free_bytes):
        """Record one explicit free-heap checkpoint (no GC performed).

        Called by the health builder (Core 1), the Core 0 publish boundary, and
        the queue admission path with an explicitly read ``gc.mem_free()`` value.
        Returns the value just observed (the current reading) -- NOT the
        low-watermark minimum -- so the admission path can act on the live heap
        after a collect frees memory. The minimum is a separate query (see
        ``minimum_free_heap_observed``).
        """
        self._validate_free_bytes(free_bytes)
        with self._lock:
            self._observe_locked(free_bytes)
        return free_bytes

    def observe_current_free_heap(self):
        """Read ``gc.mem_free()`` now and record it as an explicit checkpoint.

        Returns the current ``gc.mem_free()`` reading (the value just observed),
        so callers compare the live heap -- not the historical minimum.
        """
        return self.observe_free_heap(gc.mem_free())

    def minimum_free_heap_observed(self):
        """Return the low-watermark minimum, or None before any checkpoint."""
        with self._lock:
            return self._minimum_free_heap_observed

    def collect(self):
        """Run one authoritative ``gc.collect()`` and record before/after heap.

        This is the *required* path (the boot boundary and the memory-pressure
        path): it is not subject to the optional-GC cooldown. The minimum is
        observed from the PRE-collect free heap (the important low point) and
        from the post-collect value. ``gc.collect()`` runs with the lock released;
        its ``MemoryError`` propagates. Returns the post-collect free heap.
        """
        before = gc.mem_free()
        self._validate_free_bytes(before)
        with self._lock:
            self._observe_locked(before)
        started = time.ticks_ms()
        gc.collect()
        after = gc.mem_free()
        self._validate_free_bytes(after)
        reclaimed = after - before
        if reclaimed < 0:
            reclaimed = 0
        duration = time.ticks_diff(time.ticks_ms(), started)
        if duration < 0:
            duration = 0
        with self._lock:
            self._observe_locked(after)
            self._gc_collect_count += 1
            self._gc_bytes_reclaimed = reclaimed
            self._gc_total_reclaimed_bytes += reclaimed
            self._gc_last_duration_ms = duration
            if duration > self._gc_max_duration_ms:
                self._gc_max_duration_ms = duration
        return after

    def collect_if_below(self, threshold, force=False):
        """Collect if the current free heap is at or below ``threshold``.

        Returns True if a collect ran, False if it was skipped.

        ``force`` (the memory-pressure path) always collects when the reserve is
        breached: the reserve is a safety invariant and the optional-GC cooldown
        does not apply. Otherwise the optional path applies
        ``CONTROLLED_GC_MIN_INTERVAL_MS`` so we never collect twice in quick
        succession.
        """
        if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 0:
            raise ValueError("threshold must be a non-negative integer")
        current = gc.mem_free()
        self._validate_free_bytes(current)
        self.observe_free_heap(current)
        if current > threshold:
            return False
        if not force:
            now = time.ticks_ms()
            with self._lock:
                last = self._last_optional_gc_ms
            if last is not None and time.ticks_diff(now, last) < CONTROLLED_GC_MIN_INTERVAL_MS:
                return False
            with self._lock:
                self._last_optional_gc_ms = now
        self.collect()
        return True

    def snapshot(self):
        """Return a plain dict of the statistics for reporting (no GC performed)."""
        with self._lock:
            return {
                "minimum_free_heap_observed_bytes": self._minimum_free_heap_observed,
                "gc_collect_count": self._gc_collect_count,
                "gc_bytes_reclaimed": self._gc_bytes_reclaimed,
                "gc_total_reclaimed_bytes": self._gc_total_reclaimed_bytes,
                "gc_last_duration_ms": self._gc_last_duration_ms,
                "gc_max_duration_ms": self._gc_max_duration_ms,
            }


class OutboundQueue:
    """Core 1 -> Core 0 queue for MQTT-bound messages only.

    Hard ownership rule:
    Once put() succeeds, the message bytes are immutable. The queue stores
    pre-serialized, UTF-8 encoded payload bytes.

    Envelope rule:
    payload_bytes must be a serialized JSON object. The sender carries only its
    own message fields, including uptime_ms and timestamp (the latter null when
    UTC is unsynchronized). The envelope keys (sequence, runtime_id, source,
    firmware_version, message_schema_version) are owned by Core 0, which
    injects them at publish time; a queued message must not carry any of them
    at the top level, or the wire document would repeat a member name.

    Memory-safety rule (primary):
    The board's free-heap reserve is the memory-safety boundary, defended at
    ANY queue depth. Before admitting an entry the queue observes the current
    free heap (a MemoryStats checkpoint) and, if it is at or below the reserve,
    runs a required collect (cooldown bypassed) and then evicts the oldest entry
    in the least-important priority class the incoming entry is at least as
    important as, collecting after each eviction, until the reserve is restored.
    If the reserve cannot be restored, the incoming entry is rejected and the
    queue is left in its relieved state. ``gc.collect()`` is never run while
    holding the queue lock; the lock protects queue mutation only.

    Capacity rule (sanity guard):
    The queue is additionally bounded by MAX_OUTBOUND_QUEUE_ENTRIES (a fixed
    pathological guard, not a user knob). Exceeding it triggers the same
    priority eviction; an entry is never evicted to admit a less-important one.

    Retention rule:
    Lower numeric retention priorities are more important. Eviction always
    removes the oldest entry in the least-important class eligible for the
    incoming entry's priority. A valid queued entry is never dropped to admit a
    less important one. An in-flight QoS 1 entry is never an eviction candidate
    and its retained bytes stay counted until complete_in_flight.
    """

    def __init__(self, minimum_free_heap_bytes, memory_stats,
                 entry_ceiling=MAX_OUTBOUND_QUEUE_ENTRIES):
        if (
            isinstance(minimum_free_heap_bytes, bool)
            or not isinstance(minimum_free_heap_bytes, int)
            or minimum_free_heap_bytes < 0
        ):
            raise ValueError("minimum_free_heap_bytes must be a non-negative integer")
        if memory_stats is None or not isinstance(memory_stats, MemoryStats):
            raise ValueError("memory_stats must be a MemoryStats instance")
        if (
            isinstance(entry_ceiling, bool)
            or not isinstance(entry_ceiling, int)
            or entry_ceiling <= 0
        ):
            raise ValueError("entry_ceiling must be a positive integer")
        self._minimum_free_heap_bytes = minimum_free_heap_bytes
        self._memory_stats = memory_stats
        self._entry_ceiling = entry_ceiling
        self._queue = []
        self._in_flight = None
        self._lock = _thread.allocate_lock()
        self._high_watermark = 0
        self._queued_bytes = 0
        self._messages_evicted = 0
        self._telemetry_evicted = 0
        self._messages_rejected = 0
        self._serialization_rejected = 0
        self._oversized_rejected = 0

    def _current_free_heap(self):
        """Record and return a fresh free-heap checkpoint (no GC performed)."""
        return self._memory_stats.observe_current_free_heap()

    def _evict_oldest_in_class_locked(self, retention_priority):
        """Evict the oldest queued entry in one priority class. Lock must be held.

        Returns True if an entry was evicted, False if the class is absent.
        """
        for index, entry in enumerate(self._queue):
            if entry["retention_priority"] == retention_priority:
                evicted = self._queue.pop(index)
                self._queued_bytes -= len(evicted["payload_bytes"])
                self._messages_evicted += 1
                if evicted["kind"] == KIND_TELEMETRY:
                    self._telemetry_evicted += 1
                return True
        return False

    def _evict_one_for_memory_pressure(self, incoming_priority):
        """Evict the oldest entry in the least-important class the incoming
        entry is at least as important as. The lock guards the mutation only
        (no GC while held). Returns True if an entry was evicted.
        """
        with self._lock:
            if not self._queue:
                return False
            worst_priority = max(entry["retention_priority"] for entry in self._queue)
            # Never evict a more-important entry to keep a less-important one.
            if incoming_priority > worst_priority:
                return False
            return self._evict_oldest_in_class_locked(worst_priority)

    def _relieve_memory_pressure(self, incoming_priority):
        """Defend the reserve before admitting an entry (no lock held during GC).

        If the current free heap is already above the reserve, nothing is done.
        Otherwise run a required collect (cooldown bypassed) and, while the
        reserve is still breached and evictable entries remain, evict the oldest
        least-important eligible entry and collect again to release it. Returns
        True if the reserve was (or is) satisfied, False if it cannot be restored
        with the entries available.
        """
        reserve = self._minimum_free_heap_bytes
        if self._current_free_heap() > reserve:
            return True
        # The reserve is a safety invariant: a required collect, cooldown bypassed.
        self._memory_stats.collect_if_below(reserve, force=True)
        while self._current_free_heap() <= reserve:
            if not self._evict_one_for_memory_pressure(incoming_priority):
                return False
            # The evicted buffer is now unreferenced; collect (required) to
            # actually release it, then re-check.
            self._memory_stats.collect_if_below(reserve, force=True)
        return True

    def _admit_after_pressure(self, kind, payload_bytes, retention_priority):
        """Relieve pressure, then admit one entry under the ceiling/policy.

        The incoming entry's payload bytes are already resident (the caller
        serialized them), so defending the reserve is sufficient for safety.
        If the reserve cannot be restored, the entry is rejected and the queue
        is left relieved.
        """
        if not self._relieve_memory_pressure(retention_priority):
            with self._lock:
                self._messages_rejected += 1
            return False
        with self._lock:
            return self._admit_locked(kind, payload_bytes, retention_priority)

    def _admit_locked(self, kind, payload_bytes, retention_priority):
        """Admit one entry under the entry ceiling + retention policy. Lock held.

        The entry ceiling (``entry_ceiling``) is the pathological sanity guard;
        memory pressure is relieved OUTSIDE the lock (see put/put_with_kind) so
        that gc.collect() is never run while the queue lock is held. When the
        ceiling would be exceeded, evict the oldest entry in the least-important
        class the incoming entry is at least as important as; reject otherwise.
        """
        new_bytes = len(payload_bytes)
        occupied = len(self._queue) + (1 if self._in_flight is not None else 0)
        if occupied >= self._entry_ceiling:
            if not self._queue:
                self._messages_rejected += 1
                return False
            worst_priority = max(entry["retention_priority"] for entry in self._queue)
            # Incoming is less important than everything queued: do not evict.
            if retention_priority > worst_priority:
                self._messages_rejected += 1
                return False
            # Evict one oldest in the least-important class to make room.
            self._evict_oldest_in_class_locked(worst_priority)

        entry = {
            "kind": kind,
            "retention_priority": retention_priority,
            "payload_bytes": payload_bytes,
        }
        self._queue.append(entry)
        self._queued_bytes += new_bytes

        depth = len(self._queue)
        if depth > self._high_watermark:
            self._high_watermark = depth
        return True

    def put(self, kind, message, retention_priority):
        """Admit one MQTT-bound message after validation, serialization, and encoding.

        The message is validated, serialized to JSON, UTF-8 encoded, and size-checked
        before admission. The queue stores the final payload bytes, not the original
        dictionary.

        Before serializing, an OPTIONAL controlled collect (cooldown-gated) leaves
        headroom for the serialization peak. Admission then defends the board's
        free-heap reserve (required collect + priority eviction, never under the
        queue lock) before applying the entry ceiling.
        """
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

        # Validate, serialize, and encode before queue admission
        from message_serializer import (
            serialize_and_validate_message,
            MessageTooLargeError,
            UnsupportedValueError,
            NonStringKeyError,
            NonFiniteFloatError,
            SerializationError,
            SERIALIZATION_GC_HEADROOM_BYTES,
        )

        # Boundary (optional GC): leave headroom for the serialization peak
        # (graph + str + bytes) before allocating it. Cooldown applies.
        self._memory_stats.collect_if_below(
            self._minimum_free_heap_bytes + SERIALIZATION_GC_HEADROOM_BYTES
        )

        try:
            payload_bytes = serialize_and_validate_message(message)
        except (UnsupportedValueError, NonStringKeyError, NonFiniteFloatError) as err:
            # Validation errors are raised immediately
            raise ValueError("Message validation failed: {}".format(err))
        except MessageTooLargeError:
            # Oversized messages are rejected (do not affect queue state)
            self._oversized_rejected += 1
            return False
        except SerializationError as err:
            # Other serialization errors (e.g., JSON encoding issues)
            # are rejected without affecting queue state
            self._serialization_rejected += 1
            return False

        return self._admit_after_pressure(kind, payload_bytes, retention_priority)

    def put_with_kind(self, kind, payload_bytes, retention_priority):
        """Admit one MQTT-bound message with a specific kind (e.g., health, log).

        The caller guarantees the bytes are pre-serialized, UTF-8 encoded JSON.
        The per-message ceiling is enforced here, not by the caller: a payload
        longer than MAX_OUTBOUND_MESSAGE_BYTES (16 KiB) is rejected without
        affecting queue state, the same as the put() serialization path.

        Args:
            kind: Message kind (KIND_TELEMETRY, KIND_COMMAND_RESPONSE, KIND_HEALTH, KIND_LOG)
            payload_bytes: Pre-serialized, UTF-8 encoded payload
            retention_priority: Priority level for retention management

        Returns:
            bool: True if message was admitted, False otherwise
        """
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
            # Oversized messages are rejected (do not affect queue state)
            self._oversized_rejected += 1
            return False

        return self._admit_after_pressure(kind, payload_bytes, retention_priority)

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
            # counted in the retained-byte diagnostic: the diagnostic reflects
            # ALL retained payload, not just the FIFO.
            return self._in_flight

    def complete_in_flight(self, entry):
        with self._lock:
            if self._in_flight is entry:
                self._in_flight = None
                # The in-flight payload is released only now (PUBACK received),
                # so its bytes leave the retained-byte diagnostic here, not at take().
                self._queued_bytes -= len(entry["payload_bytes"])
                return True
            return False

    def has_in_flight(self):
        with self._lock:
            return self._in_flight is not None

    def status(self):
        with self._lock:
            return {
                "pending": len(self._queue),
                "in_flight": self._in_flight is not None,
                # The fixed entry ceiling (sanity guard), not a user-tunable size.
                "max_entries": self._entry_ceiling,
                # Diagnostic: retained payload bytes (queued FIFO + in-flight).
                "queued_bytes": self._queued_bytes,
                "high_watermark": self._high_watermark,
                "messages_evicted": self._messages_evicted,
                "telemetry_evicted": self._telemetry_evicted,
                "messages_rejected": self._messages_rejected,
                "serialization_rejected": self._serialization_rejected,
                "oversized_rejected": self._oversized_rejected,
            }

    def get_depth(self):
        """Return current queue depth (queued + in-flight entries)."""
        with self._lock:
            return len(self._queue) + (1 if self._in_flight is not None else 0)

    def get_depth_with_capacity(self):
        """Return queue depth and the entry ceiling for health reporting."""
        with self._lock:
            depth = len(self._queue) + (1 if self._in_flight is not None else 0)
            return depth, self._entry_ceiling

    def get_health_metrics(self):
        """Return (depth, entry_ceiling, retained_bytes) for health reporting.

        depth and entry_ceiling are the entry view (the in-flight entry counts
        toward depth, matching the entry ceiling in _admit_locked). retained_bytes
        is the diagnostic retained-payload view: the queued FIFO plus the in-flight
        entry (retained until its PUBACK). Both are read under the same lock so the
        pair is consistent.
        """
        with self._lock:
            depth = len(self._queue) + (1 if self._in_flight is not None else 0)
            return (
                depth,
                self._entry_ceiling,
                self._queued_bytes,
            )


class InterCoreEventQueue:
    """Private FIFO for discrete Core 0 -> Core 1 events.

    These events are never MQTT-bound merely because they are in this queue.
    """

    def __init__(self, max_entries):
        if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries <= 0:
            raise ValueError("max_entries must be a positive integer")
        self._max_entries = max_entries
        self._queue = []
        self._lock = _thread.allocate_lock()
        self._high_watermark = 0
        self._rejected = 0

    def put(self, event):
        if not isinstance(event, dict):
            raise ValueError("inter-core event must be a dictionary")

        # After successful admission, event is immutable.
        with self._lock:
            if len(self._queue) >= self._max_entries:
                self._rejected += 1
                return False
            self._queue.append(event)
            depth = len(self._queue)
            if depth > self._high_watermark:
                self._high_watermark = depth
            return True

    def take(self):
        with self._lock:
            if not self._queue:
                return None
            return self._queue.pop(0)

    def status(self):
        with self._lock:
            return {
                "pending": len(self._queue),
                "max": self._max_entries,
                "high_watermark": self._high_watermark,
                "rejected": self._rejected,
            }


class StateMailboxes:
    """Latest-value immutable snapshots shared between cores.

    State replaces rather than queues. Core 0 creates a new snapshot object,
    publishes it, and never mutates that object again. Core 1 holds the latest
    reference until Core 0 replaces it with a newer immutable snapshot.
    """

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
        """Record Core 1 activity timestamp in milliseconds."""
        if isinstance(activity_ms, bool) or not isinstance(activity_ms, int):
            raise ValueError("activity_ms must be an integer")
        with self._lock:
            self._core_1_activity_ms = activity_ms

    def get_core_1_activity_ms(self):
        with self._lock:
            return self._core_1_activity_ms

    def set_hardware(self, hardware):
        """Set hardware detection result."""
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


class ConfigTransactionMailbox:
    """Single-transaction request/result mailbox for configuration transactions.

    Core 0 posts a request (apply / commit / rollback); Core 1 takes it once
    (it becomes in-flight and cannot be taken again), processes it inside its
    normal loop, and posts a result; Core 0 takes the result, which ends the
    transaction. Exactly one transaction at a time: while any part of one is
    outstanding (queued, in flight, or result waiting) a new ``put_request``
    is rejected (returns False), and a ``put_result`` is only accepted for an
    in-flight transaction (returns False otherwise). One lock guards all
    slots; only plain JSON data crosses. Transactions never busy-spin:
    Core 0 polls once per run-loop pass, Core 1 once per loop pass.
    """

    def __init__(self):
        self._lock = _thread.allocate_lock()
        self._request = None
        self._in_flight = None
        self._result = None

    @staticmethod
    def _require_transaction_id(value):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("transaction_id must be a non-negative integer")

    def _validate_request(self, request):
        if not isinstance(request, dict):
            raise ValueError("config transaction request must be a dictionary")
        self._require_transaction_id(request.get("transaction_id"))
        if request.get("action") not in (
            CONFIG_TX_ACTION_APPLY,
            CONFIG_TX_ACTION_COMMIT,
            CONFIG_TX_ACTION_ROLLBACK,
        ):
            raise ValueError("action must be apply, commit, or rollback")
        changes = request.get("changes")
        if changes is not None and not isinstance(changes, dict):
            raise ValueError("changes must be an object")

    def put_request(self, request):
        """Core 0 -> Core 1. Returns False while any transaction is outstanding."""
        self._validate_request(request)
        with self._lock:
            if self._request is not None or self._in_flight is not None or self._result is not None:
                return False
            self._request = request
            return True

    def take_request(self):
        """Core 1: take the queued request exactly once, or None.

        Taking it moves the transaction in-flight: it cannot be taken again,
        and no new request is accepted until the result is taken.
        """
        with self._lock:
            request = self._request
            self._request = None
            if request is not None:
                self._in_flight = True
            return request

    def _validate_result(self, result):
        if not isinstance(result, dict):
            raise ValueError("config transaction result must be a dictionary")
        self._require_transaction_id(result.get("transaction_id"))
        if not isinstance(result.get("success"), bool):
            raise ValueError("success must be a boolean")
        error = result.get("error")
        if error is not None and not isinstance(error, str):
            raise ValueError("error must be a string")

    def put_result(self, result):
        """Core 1 -> Core 0. Only for an in-flight transaction; False otherwise."""
        self._validate_result(result)
        with self._lock:
            if self._in_flight is None:
                return False
            self._result = result
            return True

    def take_result(self):
        """Core 0: return the pending result (or None) and end the transaction."""
        with self._lock:
            result = self._result
            self._request = None
            self._in_flight = None
            self._result = None
            return result

    def has_pending_request(self):
        with self._lock:
            return self._request is not None

    def status(self):
        with self._lock:
            return {
                "pending_request": self._request is not None,
                "in_flight": self._in_flight is not None,
                "pending_result": self._result is not None,
            }


class InterCore:
    """Container exposing the four explicit communication lanes.

    Owns ONE shared ``MemoryStats`` (the single source of truth for the free-heap
    reserve and the controlled-GC statistics) and passes it to the outbound queue,
    so the queue never independently creates a second tracker.

    ``config_state`` is the slot for the shared committed-configuration view
    (config.ConfigState) assigned by main.py at boot; it is None until then and
    readers must tolerate that (older host-test doubles).
    """

    def __init__(self, minimum_free_heap_bytes=DEFAULT_MINIMUM_FREE_HEAP_BYTES,
                 event_max=4,
                 initial_free_heap_bytes=None,
                 outbound_max=MAX_OUTBOUND_QUEUE_ENTRIES):
        if (
            isinstance(minimum_free_heap_bytes, bool)
            or not isinstance(minimum_free_heap_bytes, int)
            or minimum_free_heap_bytes < 0
        ):
            raise ValueError("minimum_free_heap_bytes must be a non-negative integer")
        self.minimum_free_heap_bytes = minimum_free_heap_bytes
        self.memory_stats = MemoryStats(initial_free_heap_bytes=initial_free_heap_bytes)
        self.outbound_queue = OutboundQueue(
            minimum_free_heap_bytes, self.memory_stats, outbound_max
        )
        self.event_queue = InterCoreEventQueue(event_max)
        self.state_mailboxes = StateMailboxes()
        self.config_transaction_mailbox = ConfigTransactionMailbox()
        self.config_state = None
