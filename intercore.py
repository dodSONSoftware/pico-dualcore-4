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


class OutboundQueue:
    """Core 1 -> Core 0 queue for MQTT-bound messages only.

    Hard ownership rule:
    Once put() succeeds, the message bytes are immutable. The queue stores
    pre-serialized, UTF-8 encoded payload bytes.

    Retention rule:
    Lower numeric retention priorities are more important. When capacity is
    full, the oldest entry in the least-important queued priority class is
    evicted only when the incoming entry is at least as important. An in-flight
    QoS 1 entry consumes capacity but is never an eviction candidate.
    """

    def __init__(self, max_entries):
        if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries <= 0:
            raise ValueError("max_entries must be a positive integer")
        self._max_entries = max_entries
        self._queue = []
        self._in_flight = None
        self._lock = _thread.allocate_lock()
        self._high_watermark = 0
        self._messages_evicted = 0
        self._telemetry_evicted = 0
        self._messages_rejected = 0
        self._serialization_rejected = 0
        self._oversized_rejected = 0

    def _evict_oldest_by_priority_locked(self, retention_priority):
        for index, entry in enumerate(self._queue):
            if entry["retention_priority"] == retention_priority:
                evicted = self._queue.pop(index)
                self._messages_evicted += 1
                if evicted["kind"] == KIND_TELEMETRY:
                    self._telemetry_evicted += 1
                return True
        return False

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
            occupied = len(self._queue) + (1 if self._in_flight is not None else 0)
            if occupied >= self._max_entries:
                if not self._queue:
                    self._messages_rejected += 1
                    return False

                worst_priority = max(entry["retention_priority"] for entry in self._queue)
                if retention_priority > worst_priority:
                    self._messages_rejected += 1
                    return False

                if not self._evict_oldest_by_priority_locked(worst_priority):
                    self._messages_rejected += 1
                    return False

            entry = {
                "kind": kind,
                "retention_priority": retention_priority,
                "payload_bytes": payload_bytes,
            }
            self._queue.append(entry)

            depth = len(self._queue)
            if depth > self._high_watermark:
                self._high_watermark = depth
            return True

    def put_with_kind(self, kind, payload_bytes, retention_priority):
        """Admit one MQTT-bound message with a specific kind (e.g., health, log).

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

        with self._lock:
            occupied = len(self._queue) + (1 if self._in_flight is not None else 0)
            if occupied >= self._max_entries:
                if not self._queue:
                    self._messages_rejected += 1
                    return False

                worst_priority = max(entry["retention_priority"] for entry in self._queue)
                if retention_priority > worst_priority:
                    self._messages_rejected += 1
                    return False

                if not self._evict_oldest_by_priority_locked(worst_priority):
                    self._messages_rejected += 1
                    return False

            entry = {
                "kind": kind,
                "retention_priority": retention_priority,
                "payload_bytes": payload_bytes,
            }
            self._queue.append(entry)

            depth = len(self._queue)
            if depth > self._high_watermark:
                self._high_watermark = depth
            return True

    def take(self):
        """Return the current in-flight entry or move one queued entry into it."""
        with self._lock:
            if self._in_flight is not None:
                return self._in_flight
            if not self._queue:
                return None
            self._in_flight = self._queue.pop(0)
            return self._in_flight

    def complete_in_flight(self, entry):
        with self._lock:
            if self._in_flight is entry:
                self._in_flight = None
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

    def __init__(self, outbound_max=16, event_max=4):
        self.outbound_queue = OutboundQueue(outbound_max)
        self.event_queue = InterCoreEventQueue(event_max)
        self.state_mailboxes = StateMailboxes()
