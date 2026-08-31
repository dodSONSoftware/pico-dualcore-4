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
    """Temporary heap-reserve validation instrumentation (DEBUG_QUEUE_MEMORY).

    Reports heap and queue state at one meaningful queue event (admit, reject,
    evict, memory-pressure entry, gc.collect() before/after, backlog drained).
    Grep-friendly: [DEBUG] <prefix> event=<event> <extra...> heap_alloc_bytes=
    <n> heap_free_bytes=<n> minimum_free_heap_bytes=<n> heap_headroom_bytes=<n>
    queue_count=<n> queue_high_watermark=<n> [queued_bytes=<n>].

    Gated by debug.DEBUG_QUEUE_MEMORY (production default False), so the
    string allocation below is validation-only; remove with the
    instrumentation after validation. Call while holding the queue's own
    lock where the event touches queue state, so the snapshot is consistent.
    """
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

    A subclass of ValueError, so existing permanent-rejection handlers
    (which catch ValueError) keep working unchanged, while a caller that
    needs to report the actual cause -- such as Core 1's command response
    channel -- can distinguish a size failure from the other permanent
    ValueErrors (validation or serialization failure).
    """


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

    Capacity rule (heap-governed):
    The queue has NO fixed entry-count or retained-byte capacity. Admission is
    governed by the global minimum free-heap reserve (a board property owned
    by hardware.py): an entry may be retained only while gc.mem_free() is at
    or above the reserve. The queue tracks depth, retained payload bytes
    (queued FIFO plus in-flight entry), and high watermarks for observability
    -- they are metrics, not capacity limits.

    Admission rule (heap-governed):
    Fast path: if gc.mem_free() >= the reserve, the entry is admitted with no
    garbage collection. Pressure path: if the heap is below the reserve,
    gc.collect() runs once; if the reserve is still not restored, the oldest
    entry in the least-important queued priority class is evicted (only when
    the incoming entry is at least as important), gc.collect() runs again, and
    the heap is rechecked. Eviction repeats until the reserve is restored or
    no eligible lower-priority entry remains, then the incoming entry is
    admitted or rejected. The in-flight QoS 1 entry is retained until its
    PUBACK and is never an eviction candidate.

    Concurrency rule:
    The heap, and therefore the reserve, is global to both cores. The
    heap measurement, pressure-path gc.collect(), eviction decisions, and
    admission all run while the shared heap-admission lock (owned by InterCore
    and shared with the event queue) is held, so the two queues cannot both
    admit against the same stale free-heap reading.
    """

    def __init__(self, minimum_free_heap_bytes, heap_admission_lock):
        _require_positive_integer(minimum_free_heap_bytes, "minimum_free_heap_bytes")
        self._minimum_free_heap_bytes = minimum_free_heap_bytes
        self._heap_admission_lock = heap_admission_lock
        self._queue = []
        self._in_flight = None
        self._lock = _thread.allocate_lock()
        # Observability only -- metrics, not capacity limits.
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

    def _reserve_restored(self):
        return gc.mem_free() >= self._minimum_free_heap_bytes

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
        entry = {
            "kind": kind,
            "retention_priority": retention_priority,
            "payload_bytes": payload_bytes,
        }
        self._queue.append(entry)
        self._queued_bytes += len(payload_bytes)
        self._backlog_depth += 1
        depth = len(self._queue)
        if depth > self._high_watermark:
            self._high_watermark = depth
        if self._queued_bytes > self._high_watermark_bytes:
            self._high_watermark_bytes = self._queued_bytes
        return True

    def _admit_heap_governed(self, kind, payload_bytes, retention_priority):
        """Apply the heap-reserve admission decision. No locks are held on entry.

        Fast path: reserve satisfied -> admit, no garbage collection.
        Pressure path: gc.collect() once; if the reserve is still not restored,
        evict the oldest entry in the least-important eligible queued class,
        gc.collect(), and recheck -- repeating until the reserve is restored or
        no eligible lower-priority entry remains, then admit or reject.
        """
        with self._heap_admission_lock:
            if not self._reserve_restored():
                _debug_queue_memory("outbound_queue", self, "memory_pressure")
                _debug_queue_memory("outbound_queue", self, "gc_before_admission")
                gc.collect()
                _debug_queue_memory("outbound_queue", self, "gc_after_admission")
            if self._reserve_restored():
                with self._lock:
                    admitted = self._append_locked(kind, payload_bytes, retention_priority)
                    # Log after the append: the line reports the
                    # post-admission state (count and bytes include the
                    # admitted entry), matching the gc_after pair above it.
                    _debug_queue_memory("outbound_queue", self, "admit")
                    return admitted

            # Genuine retained-memory pressure: displace the least-important
            # eligible entries one at a time, reclaiming after each, until the
            # reserve is restored or nothing eligible remains.
            with self._lock:
                while True:
                    if not self._queue:
                        self._messages_rejected += 1
                        _debug_queue_memory(
                            "outbound_queue",
                            self,
                            "reject",
                            (("reason", "memory_pressure"),
                             ("priority", retention_priority)),
                        )
                        return False
                    # Explicit loop: no generator/list allocation in the
                    # pressure path (MCU-safe).
                    worst_priority = RETENTION_PRIORITY_MIN
                    for entry in self._queue:
                        if entry["retention_priority"] > worst_priority:
                            worst_priority = entry["retention_priority"]
                    # Incoming is less important than everything queued: reject
                    # rather than evict a more important retained entry.
                    if retention_priority > worst_priority:
                        self._messages_rejected += 1
                        _debug_queue_memory(
                            "outbound_queue",
                            self,
                            "reject",
                            (("reason", "lower_priority_than_queued"),
                             ("priority", retention_priority)),
                        )
                        return False
                    self._evict_oldest_by_priority_locked(worst_priority)
                    _debug_queue_memory(
                        "outbound_queue",
                        self,
                        "evict",
                        (("evicted_priority", worst_priority),),
                    )
                    gc.collect()
                    if self._reserve_restored():
                        admitted = self._append_locked(kind, payload_bytes, retention_priority)
                        _debug_queue_memory("outbound_queue", self, "admit")
                        return admitted

    def put(self, kind, message, retention_priority):
        """Admit one MQTT-bound message after validation, serialization, and encoding.

        The message is validated, serialized to JSON, UTF-8 encoded, and
        size-checked before admission. The queue stores the final payload
        bytes, not the original dictionary.

        Admission is heap-governed (see the class docstring): the entry is
        retained only while the global free-heap reserve is satisfied, evicting
        the least-important eligible entries under memory pressure.

        The in-flight QoS 1 entry is retained until its PUBACK and is never an
        eviction candidate.

        Returns:
            bool: True if admitted; False if admission failed transiently
            (heap pressure), in which case a later retry may succeed.

        Raises:
            ValueError: if the message can never be admitted -- an unsupported
            value, a serialization failure, or a serialized size beyond
            MAX_OUTBOUND_MESSAGE_BYTES. These are permanent failures of the
            message itself, not of the queue: retrying the same message cannot
            succeed, so they are raised instead of returned as False.

            OutboundMessageTooLargeError (a ValueError subclass): for the
            oversized case specifically, so a caller can report a size
            failure distinctly from a validation or serialization failure.
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
        )
        try:
            payload_bytes = serialize_and_validate_message(message)
        except (UnsupportedValueError, NonStringKeyError, NonFiniteFloatError) as err:
            # Validation errors are raised immediately
            raise ValueError("Message validation failed: {}".format(err))
        except MessageTooLargeError as err:
            # Oversized is a permanent failure of the message (the ceiling is
            # a static invariant): raise, so a caller can distinguish it from
            # the False (transient, retry later) return. Queue state is
            # untouched, as before. OutboundMessageTooLargeError (a ValueError
            # subclass) lets a caller tell this size failure apart from the
            # other permanent ValueError rejections (validation, serialization).
            self._oversized_rejected += 1
            raise OutboundMessageTooLargeError("Message too large: {}".format(err))
        except SerializationError as err:
            # A serialization failure is likewise permanent for this message.
            self._serialization_rejected += 1
            raise ValueError("Message serialization failed: {}".format(err))

        return self._admit_heap_governed(kind, payload_bytes, retention_priority)

    def put_with_kind(self, kind, payload_bytes, retention_priority):
        """Admit one MQTT-bound message with a specific kind (e.g., health, log).

        The caller guarantees the bytes are pre-serialized, UTF-8 encoded JSON.
        The per-message ceiling is enforced here, not by the caller: a payload
        longer than MAX_OUTBOUND_MESSAGE_BYTES (16 KiB) is a permanent failure
        of the message and is raised (ValueError), the same as the put()
        serialization path. Admission is then heap-governed (see the class
        docstring).

        Args:
            kind: Message kind (KIND_TELEMETRY, KIND_COMMAND_RESPONSE, KIND_HEALTH, KIND_LOG)
            payload_bytes: Pre-serialized, UTF-8 encoded payload
            retention_priority: Priority level for retention management

        Returns:
            bool: True if admitted; False if admission failed transiently
            (heap pressure), in which case a later retry may succeed.

        Raises:
            OutboundMessageTooLargeError (a ValueError subclass): if the
            payload is longer than MAX_OUTBOUND_MESSAGE_BYTES: a permanent
            failure of the message, retrying the same payload cannot succeed.
            Raised as the size-specific type so a caller can report a size
            failure distinctly from a validation or serialization failure.
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

    def complete_in_flight(self, entry):
        with self._lock:
            if self._in_flight is entry:
                self._in_flight = None
                # The in-flight payload is released only now (PUBACK received),
                # so its bytes leave the retained-byte metric here.
                self._queued_bytes -= len(entry["payload_bytes"])
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
        """Return the queue's observability metrics.

        depth/pending are the entry view (the in-flight entry counts toward
        depth); queued_bytes is the retained-payload view (the queued FIFO plus
        the in-flight entry, retained until its PUBACK). Both include the
        in-flight entry and are read under the same lock so the snapshot is
        consistent. None of these are capacity limits -- admission is governed
        by the global free-heap reserve.
        """
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
            }

    def get_depth(self):
        """Return current queue depth (queued + in-flight entries)."""
        with self._lock:
            return len(self._queue) + (1 if self._in_flight is not None else 0)


class InterCoreEventQueue:
    """Private FIFO for discrete Core 0 -> Core 1 events.

    These events are never MQTT-bound merely because they are in this queue.

    Capacity rule (heap-governed):
    The queue has NO fixed entry-count capacity. Admission is governed by the
    same global minimum free-heap reserve the outbound queue uses, under the
    same shared heap-admission lock: an event may be retained only while
    gc.mem_free() is at or above the reserve.

    No-eviction rule:
    An admitted event is a discrete control operation and is never evicted to
    make room for a newer one. When the reserve cannot be satisfied (even
    after gc.collect()), the new event is rejected and the caller reports the
    memory-pressure failure; already-admitted events are untouched.
    """

    def __init__(self, minimum_free_heap_bytes, heap_admission_lock):
        _require_positive_integer(minimum_free_heap_bytes, "minimum_free_heap_bytes")
        self._minimum_free_heap_bytes = minimum_free_heap_bytes
        self._heap_admission_lock = heap_admission_lock
        self._queue = []
        self._lock = _thread.allocate_lock()
        self._high_watermark = 0
        self._rejected = 0

    def put(self, event):
        """Admit one event, or reject it under memory pressure.

        Fast path: reserve satisfied -> admit, no garbage collection.
        Pressure path: gc.collect() once; if the reserve is still not
        restored, the new event is rejected -- never at the cost of an
        already-admitted event.
        """
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


class InterCore:
    """Container exposing the three explicit communication lanes.

    The two FIFO lanes are heap-governed: both admit only while gc.mem_free()
    stays at or above the board-specific minimum free-heap reserve (single
    source of truth: hardware.py), and they serialize that check -- together
    with the pressure-path gc.collect() and the outbound eviction decisions --
    on one shared heap-admission lock, because the MicroPython heap (and its
    reserve) is global to both cores.
    """

    def __init__(self, minimum_free_heap_bytes):
        _require_positive_integer(minimum_free_heap_bytes, "minimum_free_heap_bytes")
        self.minimum_free_heap_bytes = minimum_free_heap_bytes
        self._heap_admission_lock = _thread.allocate_lock()
        self.outbound_queue = OutboundQueue(
            minimum_free_heap_bytes, self._heap_admission_lock
        )
        self.event_queue = InterCoreEventQueue(
            minimum_free_heap_bytes, self._heap_admission_lock
        )
        self.state_mailboxes = StateMailboxes()
