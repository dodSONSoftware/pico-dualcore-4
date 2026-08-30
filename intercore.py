# intercore.py - Three-lane inter-core communication boundary
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import _thread


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

# Aggregate retained-payload budget for the outbound queue (32 KiB).
#
# Entry count alone is not a memory-safety boundary: 16 retained 16 KiB payloads
# would be 256 KiB, exhausting a Pico W's heap. This budget caps total retained
# payload bytes so the queue is bounded by BOTH entry count and bytes. Sized to
# hold a realistic full queue (16 entries x ~2 KiB). The single in-flight entry
# is retained until its PUBACK (take() does not free it), so it counts against
# this budget along with the queued FIFO -- a full queue plus its in-flight
# entry still cannot exceed it. Per-message size is bounded separately by
# message_serializer.MAX_OUTBOUND_MESSAGE_BYTES (16 KiB).
DEFAULT_MAX_OUTBOUND_QUEUED_BYTES = 32 * 1024


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

    Capacity rule:
    The queue is bounded by BOTH entry count (max_entries) and retained payload
    bytes (max_queued_bytes, which include the in-flight entry). Either budget
    being exceeded is a full condition.

    Retention rule:
    Lower numeric retention priorities are more important. When a budget is
    full, the oldest entry in the least-important queued priority class is
    evicted only when the incoming entry is at least as important AND evicting
    it frees enough room (by count or by bytes) for the incoming entry. A valid
    queued entry is never dropped to admit one that still would not fit. An
    in-flight QoS 1 entry consumes both budgets (its payload is retained until
    its PUBACK) but is never an eviction candidate.
    """

    def __init__(self, max_entries, max_queued_bytes=DEFAULT_MAX_OUTBOUND_QUEUED_BYTES):
        if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries <= 0:
            raise ValueError("max_entries must be a positive integer")
        if (
            isinstance(max_queued_bytes, bool)
            or not isinstance(max_queued_bytes, int)
            or max_queued_bytes <= 0
        ):
            raise ValueError("max_queued_bytes must be a positive integer")
        self._max_entries = max_entries
        self._max_queued_bytes = max_queued_bytes
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

    def _oldest_entry_size_for_priority_locked(self, retention_priority):
        """Return the byte size of the oldest queued entry in a priority class.

        Used to feasibility-check eviction BEFORE it happens so a valid entry is
        never dropped to admit one that still would not fit. Returns None when
        the class is absent.
        """
        for entry in self._queue:
            if entry["retention_priority"] == retention_priority:
                return len(entry["payload_bytes"])
        return None

    def _admit_locked(self, kind, payload_bytes, retention_priority):
        """Apply the admission decision for an entry. Lock must be held.

        Enforces BOTH the entry-count budget and the queued-byte budget under the
        retention/eviction policy. Evicts the oldest entry in the least-important
        queued class only when the incoming entry is at least as important AND the
        eviction frees enough room (by count or by bytes). Returns True if the
        entry was admitted, False if it was rejected.
        """
        new_bytes = len(payload_bytes)
        occupied = len(self._queue) + (1 if self._in_flight is not None else 0)
        count_full = occupied >= self._max_entries
        bytes_over = (self._queued_bytes + new_bytes) > self._max_queued_bytes

        if count_full or bytes_over:
            if not self._queue:
                self._messages_rejected += 1
                return False

            worst_priority = max(entry["retention_priority"] for entry in self._queue)
            # Incoming is less important than everything queued: do not evict.
            if retention_priority > worst_priority:
                self._messages_rejected += 1
                return False

            evicted_size = self._oldest_entry_size_for_priority_locked(worst_priority)
            if evicted_size is None:
                self._messages_rejected += 1
                return False

            # Evict only if it frees enough room for the incoming entry;
            # otherwise reject without disturbing the valid queued entry.
            if (self._queued_bytes - evicted_size + new_bytes) > self._max_queued_bytes:
                self._messages_rejected += 1
                return False

            self._evict_oldest_by_priority_locked(worst_priority)

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

        When full, compare the incoming priority with the least-important
        queued priority (highest numeric value):
          * incoming more important: evict oldest least-important entry
          * incoming equally important: evict oldest entry in that class
          * incoming less important: reject incoming entry

        The in-flight QoS 1 entry counts toward max_entries but cannot be
        evicted. If no queued entry is available for eviction, admission fails.
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
            # Oversized messages are rejected (do not affect queue state)
            self._oversized_rejected += 1
            return False
        except SerializationError as err:
            # Other serialization errors (e.g., JSON encoding issues)
            # are rejected without affecting queue state
            self._serialization_rejected += 1
            return False

        with self._lock:
            return self._admit_locked(kind, payload_bytes, retention_priority)

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

        with self._lock:
            return self._admit_locked(kind, payload_bytes, retention_priority)

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
            # counted toward the byte budget: the budget caps ALL retained
            # payload, not just the FIFO.
            return self._in_flight

    def complete_in_flight(self, entry):
        with self._lock:
            if self._in_flight is entry:
                self._in_flight = None
                # The in-flight payload is released only now (PUBACK received),
                # so its bytes return to the budget here, not at take().
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
                "max": self._max_entries,
                "queued_bytes": self._queued_bytes,
                "max_queued_bytes": self._max_queued_bytes,
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
        """Return queue depth and capacity tuple for health reporting."""
        with self._lock:
            depth = len(self._queue) + (1 if self._in_flight is not None else 0)
            return depth, self._max_entries

    def get_health_metrics(self):
        """Return (depth, max_entries, queued_bytes, max_queued_bytes) for health reporting.

        Depth and max_entries are the entry-budget view (the in-flight entry
        counts toward depth, matching the entry budget in _admit_locked).
        queued_bytes is the byte-budget view: ALL retained payload bytes, the
        queued FIFO plus the in-flight entry (retained until its PUBACK), since
        the budget caps retained memory, not just the FIFO. Both views include
        the in-flight entry and are read under the same lock so the pair is
        consistent.
        """
        with self._lock:
            depth = len(self._queue) + (1 if self._in_flight is not None else 0)
            return (
                depth,
                self._max_entries,
                self._queued_bytes,
                self._max_queued_bytes,
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


class InterCore:
    """Container exposing the three explicit communication lanes."""

    def __init__(self, outbound_max=16, event_max=4, outbound_max_bytes=DEFAULT_MAX_OUTBOUND_QUEUED_BYTES):
        self.outbound_queue = OutboundQueue(outbound_max, outbound_max_bytes)
        self.event_queue = InterCoreEventQueue(event_max)
        self.state_mailboxes = StateMailboxes()
