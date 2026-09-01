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

    Once put() succeeds the payload bytes are immutable (pre-serialized UTF-8 JSON); the Core 0 envelope keys are injected at publish time and must not be carried at the top level. No fixed capacity: admission is governed by the global minimum free-heap reserve, evicting the least-important eligible entries under pressure (never the in-flight QoS 1 entry), all under the shared heap-admission lock."""

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

        Fast path: admit, no gc.collect(). Pressure path: gc.collect() once, then evict the least-important eligible entries one at a time until the reserve is restored, then admit or reject."""
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
        """Admit one MQTT-bound message after validation, serialization, encoding, and size check.

        Returns True if admitted, False on transient heap pressure (a later retry may succeed). Raises ValueError on a permanent failure of the message itself (unsupported value, serialization, or size beyond MAX_OUTBOUND_MESSAGE_BYTES); the oversized case raises OutboundMessageTooLargeError, a ValueError subclass."""
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
        """Return the queue's observability metrics (metrics, not capacity limits).

        depth/pending are the entry view, queued_bytes the retained-payload view; both include the in-flight entry, read under one lock."""
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
        with self._lock:
            return len(self._queue) + (1 if self._in_flight is not None else 0)


class InterCoreEventQueue:
    """Private FIFO for discrete Core 0 -> Core 1 events (never MQTT-bound by queue membership).

    No fixed capacity: admission is governed by the same global free-heap reserve under the same shared heap-admission lock. Admitted events are never evicted -- under pressure the new event is rejected instead."""

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


class InterCore:
    """Container exposing the three explicit communication lanes.

    The two FIFO lanes are heap-governed by the same global free-heap reserve, serialized on one shared heap-admission lock (the heap is global to both cores)."""

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
