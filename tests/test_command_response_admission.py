# test_command_response_admission.py - Tests for the command response channel
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Host-side tests for the Core 1 command response admission contract.

The outbound queue distinguishes a transient rejection (False: heap pressure, retry later) from a permanent one (ValueError: the message can never be admitted). The command channel must honor that distinction: a permanently rejected response is answered with a small error response for the same command whose code states the actual cause -- "response_too_large" for an oversized response, "response_invalid" for a validation or serialization failure -- so a response can never permanently stall the command channel behind it."""

import gc
import json
import pathlib
import sys
from unittest.mock import MagicMock, patch

import pytest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.modules.setdefault("machine", MagicMock())

import core1  # noqa: E402
import intercore as intercore_module  # noqa: E402
from intercore import (  # noqa: E402
    InterCore,
    KIND_COMMAND_RESPONSE,
    KIND_TELEMETRY,
    RETENTION_PRIORITY_CRITICAL,
    OutboundMessageTooLargeError,
)
from message_serializer import MAX_OUTBOUND_MESSAGE_BYTES  # noqa: E402


# Sentinels for "permanently reject" in a ScriptedQueue outcome list: the
# real queue raises OutboundMessageTooLargeError (a ValueError subclass) for
# an oversized message and a plain ValueError for a validation or
# serialization failure; a MemoryError is raised when the serializer's own
# gc + eviction recovery exhausts (the pool cannot form the run the
# serialized form needs, now).
RAISE = object()
RAISE_TOO_LARGE = object()
RAISE_MEMORY = object()


class ScriptedQueue:
    """An outbound queue that returns scripted admission outcomes in order.

    Outcomes: True (admit), False (transient rejection), RAISE (permanent rejection: ValueError), or RAISE_TOO_LARGE (OutboundMessageTooLargeError)."""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.admitted = []

    def put(self, kind, message, retention_priority):
        outcome = self._outcomes.pop(0)
        if outcome is RAISE:
            raise ValueError("Message validation failed: unsupported value at data")
        if outcome is RAISE_TOO_LARGE:
            raise OutboundMessageTooLargeError("Message too large: exceeds the per-message limit")
        if outcome is RAISE_MEMORY:
            raise MemoryError("memory allocation failed, allocating 2360 bytes")
        if outcome:
            self.admitted.append((kind, message, retention_priority))
        return outcome


class FakeInterCore:
    def __init__(self, queue):
        self.outbound_queue = queue


def _success_response():
    return {
        "kind": KIND_COMMAND_RESPONSE,
        "message": {
            "message_type": "command_response",
            "uptime_ms": 1234,
            "timestamp": None,
            "payload": {
                "command_id": "details-001",
                "command": "get-details",
                "targeted": True,
                "success": True,
                "data": {"memory": {"free_heap_bytes": 123}},
            },
        },
    }


def _full_get_details_response():
    """A get-details success response carrying the full nine-section
    snapshot -- the shape whose ~2.4 KiB serialized form hit the Pico W's
    fragmented pool in 0.4.91."""
    return {
        "kind": KIND_COMMAND_RESPONSE,
        "message": {
            "message_type": "command_response",
            "uptime_ms": 1234,
            "timestamp": None,
            "payload": {
                "command_id": "details-001",
                "command": "get-details",
                "targeted": True,
                "success": True,
                "data": {
                    "network": {"wifi_connected": True, "wifi_rssi_dbm": -60},
                    "memory": {"free_heap_bytes": 57000},
                    "runtime": {"uptime_ms": 1234},
                    "devices": {"configured": 3, "active": 1},
                    "cpu": {"frequency_hz": 133000000, "temperature_c": 41.2},
                    "machine": "Raspberry Pi Pico W",
                    "communications": {"mqtt_connected": True},
                    "queues": {"outbound_queue_depth": 0},
                    "device_status": [
                        {
                            "id": "bme280",
                            "status": "initialization_failed",
                            "failure_reason": (
                                "BME280 not found at candidates [118, 119]: "
                                "BME280 read failed at register 0xD0: [Errno 5] EIO"
                            ),
                        }
                    ],
                },
            },
        },
    }


def test_transient_rejection_keeps_the_same_response_pending():
    queue = ScriptedQueue([False, True])
    intercore = FakeInterCore(queue)
    response = _success_response()

    with patch.object(core1, "_message_time", return_value=(1234, None)):
        pending = core1._admit_or_substitute_command_response(
            intercore, object(), response
        )

    # Transient rejection: the original response stays pending, nothing admitted.
    assert pending is response
    assert queue.admitted == []

    with patch.object(core1, "_message_time", return_value=(1234, None)):
        pending = core1._admit_or_substitute_command_response(
            intercore, object(), pending
        )

    assert pending is None
    assert len(queue.admitted) == 1
    kind, message, priority = queue.admitted[0]
    assert kind == KIND_COMMAND_RESPONSE
    assert message["payload"]["success"] is True
    assert priority == RETENTION_PRIORITY_CRITICAL


def test_critical_vs_critical_rejection_keeps_both_responses_owned(monkeypatch):
    """End-to-end with the real queue (the get-details regression): with
    response A already admitted, the transient rejection of a second
    CRITICAL response keeps A in the queue (never evicted) and B pending
    for Core 1 to retry; once the heap recovers, B is admitted behind A.

    At no point does either response exist in a state where neither Core 1
    nor the queue owns it."""
    intercore = InterCore(64 * 1024)
    queue = intercore.outbound_queue

    response_a = _success_response()
    assert (
        queue.put(response_a["kind"], response_a["message"], RETENTION_PRIORITY_CRITICAL)
        is True
    )

    response_b = _success_response()
    response_b["message"]["payload"]["command_id"] = "details-002"

    # Unrecoverable heap pressure for the admission attempt that follows.
    monkeypatch.setattr(gc, "mem_free", lambda: 0, raising=False)
    with patch.object(core1, "_message_time", return_value=(1234, None)):
        pending = core1._admit_or_substitute_command_response(
            intercore, object(), response_b
        )

    # Transient rejection: B stays pending for retry (no substitute was
    # built), and A is still the only queued entry.
    assert pending is response_b
    status = queue.status()
    assert status["pending"] == 1
    assert status["messages_evicted"] == 0
    assert status["messages_rejected"] == 1

    # The heap recovers: the pending response is admitted on the retry.
    monkeypatch.setattr(gc, "mem_free", lambda: 256 * 1024, raising=False)
    with patch.object(core1, "_message_time", return_value=(1234, None)):
        assert core1._admit_or_substitute_command_response(intercore, object(), pending) is None
    status = queue.status()
    assert status["pending"] == 2
    assert status["messages_evicted"] == 0

    # A is still in the queue, admitted first, with B behind it.
    first = queue.take()
    assert json.loads(first["payload_bytes"])["payload"]["command_id"] == "details-001"
    queue.complete_in_flight(first)
    second = queue.take()
    assert json.loads(second["payload_bytes"])["payload"]["command_id"] == "details-002"
    queue.complete_in_flight(second)


def test_oversized_rejection_is_answered_with_response_too_large():
    queue = ScriptedQueue([RAISE_TOO_LARGE, True])
    intercore = FakeInterCore(queue)
    response = _success_response()

    with patch.object(core1, "_message_time", return_value=(1234, None)):
        pending = core1._admit_or_substitute_command_response(
            intercore, object(), response
        )

    # The channel moved on: nothing stays pending.
    assert pending is None
    assert len(queue.admitted) == 1
    kind, message, priority = queue.admitted[0]
    assert kind == KIND_COMMAND_RESPONSE
    assert priority == RETENTION_PRIORITY_CRITICAL

    payload = message["payload"]
    assert payload["success"] is False
    assert payload["error"]["code"] == "response_too_large"
    assert "data" not in payload
    # The command identity is preserved: the caller's command is still answered.
    assert payload["command_id"] == "details-001"
    assert payload["command"] == "get-details"
    assert payload["targeted"] is True

    # The substitute is small by construction: far under the per-message ceiling.
    assert len(json.dumps(message).encode("utf-8")) <= 512


def test_validation_failure_rejection_is_answered_with_response_invalid():
    """A permanent rejection that is not a size problem must be reported as
    such: a serialization/validation defect in the response must not be
    misreported to the command sender as a size limit."""
    queue = ScriptedQueue([RAISE, True])
    intercore = FakeInterCore(queue)
    response = _success_response()

    with patch.object(core1, "_message_time", return_value=(1234, None)):
        pending = core1._admit_or_substitute_command_response(
            intercore, object(), response
        )

    # The channel moved on: nothing stays pending.
    assert pending is None
    assert len(queue.admitted) == 1
    kind, message, priority = queue.admitted[0]
    assert kind == KIND_COMMAND_RESPONSE
    assert priority == RETENTION_PRIORITY_CRITICAL

    payload = message["payload"]
    assert payload["success"] is False
    assert payload["error"]["code"] == "response_invalid"
    assert "data" not in payload
    # The command identity is preserved, and the code is not the size code.
    assert payload["command_id"] == "details-001"
    assert payload["command"] == "get-details"
    assert payload["targeted"] is True

    # The substitute is small by construction: far under the per-message ceiling.
    assert len(json.dumps(message).encode("utf-8")) <= 512


def test_substitute_that_is_transiently_rejected_stays_pending():
    queue = ScriptedQueue([RAISE_TOO_LARGE, False, True])
    intercore = FakeInterCore(queue)
    response = _success_response()

    with patch.object(core1, "_message_time", return_value=(1234, None)):
        pending = core1._admit_or_substitute_command_response(
            intercore, object(), response
        )

    # The original is gone; the (small) substitute is what stays pending.
    assert pending is not response
    assert pending["message"]["payload"]["error"]["code"] == "response_too_large"

    with patch.object(core1, "_message_time", return_value=(1234, None)):
        assert core1._admit_or_substitute_command_response(
            intercore, object(), pending
        ) is None


def test_oversized_telemetry_sample_is_discarded_not_retried():
    """put() raises for a permanently rejected sample: telemetry discards it
    with a warning instead of crashing Core 1 (a current sample is not a
    retryable record)."""
    queue = ScriptedQueue([RAISE])
    intercore = FakeInterCore(queue)
    result = {
        "status": "telemetry",
        "device_id": "dev-1",
        "device": "bme280",
        "telemetry": {"blob": "x" * (MAX_OUTBOUND_MESSAGE_BYTES + 1)},
    }

    with patch.object(core1, "_message_time", return_value=(1234, None)):
        core1._handle_device_result(intercore, object(), result)

    assert queue.admitted == []


def test_oversized_get_details_response_does_not_stall_the_channel(monkeypatch):
    """End-to-end with the real queue: a get-details response beyond the
    per-message ceiling is permanently rejected and answered with the small
    error response; the channel is left with exactly one admitted entry."""
    intercore = InterCore(64 * 1024)
    queue = intercore.outbound_queue

    # A get-details data section large enough to push the whole response
    # past MAX_OUTBOUND_MESSAGE_BYTES.
    big_data = {"blob": "x" * (MAX_OUTBOUND_MESSAGE_BYTES + 4096)}
    monkeypatch.setattr(
        core1, "_collect_system_information_full", lambda system_information: big_data
    )

    event = {
        "command_id": "details-001",
        "command": core1.COMMAND_GET_DETAILS,
        "payload": {},
        "targeted": True,
    }

    class EventQueue:
        def __init__(self, event):
            self._event = event

        def take(self):
            taken = self._event
            self._event = None
            return taken

    intercore.event_queue = EventQueue(event)

    with patch.object(core1, "_message_time", return_value=(1234, None)):
        response = core1._process_intercore_event(intercore, object(), object())
        assert response is not None

        pending = core1._admit_or_substitute_command_response(
            intercore, object(), response
        )

    assert pending is None
    assert queue.get_depth() == 1
    assert queue.status()["oversized_rejected"] == 1

    entry = queue.take()
    wire = json.loads(entry["payload_bytes"])
    assert wire["payload"]["success"] is False
    assert wire["payload"]["error"]["code"] == "response_too_large"
    assert wire["payload"]["command_id"] == "details-001"
    assert wire["payload"]["command"] == "get-details"
    queue.complete_in_flight(entry)


def test_queue_raises_the_size_type_only_for_size_failures():
    """The queue's own boundary contract: OutboundMessageTooLargeError is raised only for the per-message ceiling, on both admission paths.

    It is a ValueError subclass (existing permanent-rejection handlers keep working), and a validation failure is a ValueError that is NOT a size failure -- so the two causes never get collapsed back together."""
    intercore = InterCore(64 * 1024)
    queue = intercore.outbound_queue
    big = {"blob": "x" * (MAX_OUTBOUND_MESSAGE_BYTES + 1)}

    with pytest.raises(OutboundMessageTooLargeError):
        queue.put(KIND_TELEMETRY, big, 40)
    # A size failure is still a ValueError for existing handlers.
    with pytest.raises(ValueError):
        queue.put(KIND_TELEMETRY, big, 40)

    # A validation failure is a permanent ValueError, not a size failure.
    try:
        queue.put(KIND_TELEMETRY, {"bad": float("nan")}, 40)
    except OutboundMessageTooLargeError:
        pytest.fail("a validation failure was reported as a size failure")
    except ValueError:
        pass
    else:
        pytest.fail("a validation failure was not permanently rejected")

    # The pre-serialized path enforces the same ceiling, same type.
    with pytest.raises(OutboundMessageTooLargeError):
        queue.put_with_kind(KIND_TELEMETRY, b"x" * (MAX_OUTBOUND_MESSAGE_BYTES + 1), 40)
    with pytest.raises(ValueError):
        queue.put_with_kind(KIND_TELEMETRY, b"x" * (MAX_OUTBOUND_MESSAGE_BYTES + 1), 40)


def test_persistent_memory_error_answers_get_details_with_the_bounded_snapshot():
    """The 0.4.91 Pico W board regression (scripted): the full snapshot's
    serialization MemoryError persists after the queue's own gc + eviction
    recovery (an empty queue has nothing eligible to discard), and the
    command is answered with the snapshot minus the device_status section
    instead of the MemoryError killing Core 1's worker thread."""
    queue = ScriptedQueue([RAISE_MEMORY, True])
    intercore = FakeInterCore(queue)
    response = _full_get_details_response()

    with patch.object(core1, "_message_time", return_value=(1234, None)):
        pending = core1._admit_or_substitute_command_response(
            intercore, object(), response
        )

    # The channel moved on: nothing stays pending, one bounded form admitted.
    assert pending is None
    assert len(queue.admitted) == 1
    kind, message, priority = queue.admitted[0]
    assert kind == KIND_COMMAND_RESPONSE
    assert priority == RETENTION_PRIORITY_CRITICAL

    payload = message["payload"]
    # The command identity is preserved, and the response is still a success.
    assert payload["success"] is True
    assert payload["command_id"] == "details-001"
    assert payload["command"] == "get-details"
    assert payload["targeted"] is True
    data = payload["data"]
    # device_status (the unbounded failure_reason section) is dropped and
    # named; the rest of the snapshot is still carried.
    assert data["omitted_sections"] == ["device_status"]
    assert "device_status" not in data
    assert "devices" in data
    assert data["memory"] == {"free_heap_bytes": 57000}


def test_memory_error_drops_the_sections_in_order_until_one_is_admitted():
    queue = ScriptedQueue([RAISE_MEMORY, RAISE_MEMORY, True])
    intercore = FakeInterCore(queue)
    response = _full_get_details_response()

    with patch.object(core1, "_message_time", return_value=(1234, None)):
        pending = core1._admit_or_substitute_command_response(
            intercore, object(), response
        )

    assert pending is None
    assert len(queue.admitted) == 1
    data = queue.admitted[0][1]["payload"]["data"]
    # device_status first (the unbounded failure_reason strings), then
    # devices: the marker accumulates what the response no longer carries.
    assert data["omitted_sections"] == ["device_status", "devices"]
    assert "device_status" not in data
    assert "devices" not in data
    # The fixed-size sections survive all the drops.
    assert data["memory"] == {"free_heap_bytes": 57000}
    assert "queues" in data


def test_memory_error_on_every_form_is_answered_with_the_error_response():
    """When even the last bounded form cannot serialize, the small error
    substitute answers the command -- the channel still moves on. (Only a
    MemoryError on the substitute's own admission propagates.)"""
    queue = ScriptedQueue([RAISE_MEMORY, RAISE_MEMORY, RAISE_MEMORY, True])
    intercore = FakeInterCore(queue)
    response = _full_get_details_response()

    with patch.object(core1, "_message_time", return_value=(1234, None)):
        pending = core1._admit_or_substitute_command_response(
            intercore, object(), response
        )

    assert pending is None
    assert len(queue.admitted) == 1
    payload = queue.admitted[0][1]["payload"]
    assert payload["success"] is False
    assert payload["error"]["code"] == "response_invalid"
    assert "data" not in payload
    assert payload["command_id"] == "details-001"
    assert payload["command"] == "get-details"


def test_transient_rejection_of_the_bounded_form_stays_pending():
    queue = ScriptedQueue([RAISE_MEMORY, False, True])
    intercore = FakeInterCore(queue)
    response = _full_get_details_response()

    with patch.object(core1, "_message_time", return_value=(1234, None)):
        pending = core1._admit_or_substitute_command_response(
            intercore, object(), response
        )

    # The bounded form (not the full snapshot) is what stays pending.
    assert pending is not response
    assert pending["message"]["payload"]["data"]["omitted_sections"] == ["device_status"]

    with patch.object(core1, "_message_time", return_value=(1234, None)):
        assert core1._admit_or_substitute_command_response(
            intercore, object(), pending
        ) is None
    assert len(queue.admitted) == 1


def test_memory_error_on_a_failure_response_is_answered_with_the_error_response():
    """The bounded get-details form exists only for a successful get-details
    (the one response with a full snapshot in its data): a MemoryError on
    any other response takes the small error substitute."""
    queue = ScriptedQueue([RAISE_MEMORY, True])
    intercore = FakeInterCore(queue)
    response = {
        "kind": KIND_COMMAND_RESPONSE,
        "message": {
            "message_type": "command_response",
            "uptime_ms": 1234,
            "timestamp": None,
            "payload": {
                "command_id": "details-001",
                "command": "get-details",
                "targeted": True,
                "success": False,
                "error": {
                    "code": "system_information_unavailable",
                    "message": "System information is unavailable",
                },
            },
        },
    }

    with patch.object(core1, "_message_time", return_value=(1234, None)):
        pending = core1._admit_or_substitute_command_response(
            intercore, object(), response
        )

    assert pending is None
    assert len(queue.admitted) == 1
    payload = queue.admitted[0][1]["payload"]
    assert payload["success"] is False
    assert payload["error"]["code"] == "response_invalid"
    assert payload["command_id"] == "details-001"


def test_memory_error_fallback_end_to_end_with_the_real_queue(monkeypatch):
    """The 0.4.91 Pico W board regression, end to end with the real queue:
    the full get-details snapshot's serialization MemoryErrors (a fragmented
    pool cannot form the run) and the queue's own gc + eviction recovery has
    an empty queue to draw on, so the MemoryError reaches Core 1's admission
    -- and the command is answered with the bounded snapshot instead of the
    worker thread dying."""
    intercore = InterCore(64 * 1024)
    queue = intercore.outbound_queue
    monkeypatch.setattr(gc, "mem_free", lambda: 256 * 1024, raising=False)

    real_serialize = intercore_module.serialize_and_validate_message

    def flaky_serialize(message):
        # The pool has a run for everything but the full snapshot: the
        # device_status section (the unbounded failure_reason strings) is
        # what does not fit. The omitted_sections marker names the section
        # without a colon, so it does not trip this check.
        if '"device_status":' in json.dumps(message):
            raise MemoryError("memory allocation failed, allocating 2360 bytes")
        return real_serialize(message)

    monkeypatch.setattr(
        intercore_module, "serialize_and_validate_message", flaky_serialize
    )

    monkeypatch.setattr(
        core1,
        "_collect_system_information_full",
        lambda system_information: _full_get_details_response()["message"]["payload"]["data"],
    )

    event = {
        "command_id": "details-001",
        "command": core1.COMMAND_GET_DETAILS,
        "payload": {},
        "targeted": True,
    }

    class EventQueue:
        def __init__(self, event):
            self._event = event

        def take(self):
            taken = self._event
            self._event = None
            return taken

    intercore.event_queue = EventQueue(event)

    with patch.object(core1, "_message_time", return_value=(1234, None)):
        response = core1._process_intercore_event(intercore, object(), object())
        assert response is not None

        pending = core1._admit_or_substitute_command_response(
            intercore, object(), response
        )

    # The command was answered (not lost): exactly one bounded entry.
    assert pending is None
    assert queue.get_depth() == 1

    entry = queue.take()
    wire = json.loads(entry["payload_bytes"])
    payload = wire["payload"]
    assert payload["success"] is True
    assert payload["command_id"] == "details-001"
    assert payload["command"] == "get-details"
    assert "device_status" not in payload["data"]
    assert payload["data"]["omitted_sections"] == ["device_status"]
    assert "devices" in payload["data"]
    queue.complete_in_flight(entry)
