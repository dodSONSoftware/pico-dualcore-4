# test_command_response_admission.py - Tests for the command response channel
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Host-side tests for the Core 1 command response admission contract.

The outbound queue distinguishes a transient rejection (False: heap pressure, retry later) from a permanent one (ValueError: the message can never be admitted). The command channel must honor that distinction: a permanently rejected response is answered with a small error response for the same command whose code states the actual cause -- "response_too_large" for an oversized response, "response_invalid" for a validation or serialization failure -- so a response can never permanently stall the command channel behind it."""

import json
import pathlib
import sys
from unittest.mock import MagicMock, patch

import pytest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.modules.setdefault("machine", MagicMock())

import core1  # noqa: E402
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
# serialization failure.
RAISE = object()
RAISE_TOO_LARGE = object()


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
        "device": "system-information",
        "sensor_type": "information",
        "telemetry": {"blob": "x" * (MAX_OUTBOUND_MESSAGE_BYTES + 1)},
    }

    with patch.object(core1, "_message_time", return_value=(1234, None)):
        core1._handle_device_result(intercore, {}, object(), result)

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
