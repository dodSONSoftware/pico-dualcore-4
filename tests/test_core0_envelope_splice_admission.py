# test_core0_envelope_splice_admission.py - Core 0 splice-time size ceiling
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Host-side tests for the wire-boundary size check on Core 0's envelope splice.

The per-message ceiling is enforced at admission against the message body, but Core 0 later splices its five envelope members (sequence, runtime_id, source, firmware_version, message_schema_version) on top of that body before the PUBLISH. A body admitted at or under MAX_OUTBOUND_MESSAGE_BYTES can therefore exceed it once spliced. The splice must check the FINAL wire length against the ceiling before the joined frame is allocated, and treat an oversized splice as a PERMANENT failure of that entry: retrying the same bytes can never succeed. Command responses (the channel must keep moving) are answered with the bounded "response_too_large" substitute; telemetry/health/log are discarded with a warning and the queue's oversized_discarded counter. Transient publish failures (a failed QoS 1 attempt, which raises a different error) still leave the entry in flight for retry, unchanged."""

import json
import pathlib
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.modules.setdefault("machine", MagicMock())

from test_outbound_publish_pacing import (  # noqa: E402
    _MACHINE,
    _queue_telemetry,
    _run_to,
    _utc_synchronized,
    make_core0,
)
from intercore import (  # noqa: E402
    KIND_COMMAND_RESPONSE,
    KIND_TELEMETRY,
    RETENTION_PRIORITY_CRITICAL,
    RETENTION_PRIORITY_TELEMETRY,
    OutboundMessageTooLargeError,
)
from message_serializer import MAX_OUTBOUND_MESSAGE_BYTES  # noqa: E402


def _telemetry_body_of_length(n):
    """A valid JSON-object body of exactly n bytes (the blob sizes it)."""
    template = {
        "message_type": "telemetry",
        "uptime_ms": 0,
        "timestamp": None,
        "payload": {"blob": "x"},
    }
    base = len(json.dumps(template).encode("utf-8"))
    blob_len = n - (base - 1)
    body = json.dumps({
        "message_type": "telemetry",
        "uptime_ms": 0,
        "timestamp": None,
        "payload": {"blob": "x" * blob_len},
    }).encode("utf-8")
    assert len(body) == n
    return body


def _command_response_body_of_length(n):
    """A valid Core 1 command-response body of exactly n bytes (the blob sizes it)."""
    def encode(blob_len):
        return json.dumps({
            "message_type": "command_response",
            "uptime_ms": 0,
            "timestamp": None,
            "payload": {
                "command_id": "drain-big-1",
                "command": "get-details",
                "targeted": True,
                "success": True,
                "data": {"blob": "x" * blob_len},
            },
        }).encode("utf-8")

    base = len(encode(0))
    body = encode(n - base)
    assert len(body) == n
    return body


# --- _publish_entry: the splice check itself -------------------------------


def test_publish_entry_refuses_a_body_oversized_only_by_the_envelope(make_core0):
    """A body one byte short of the ceiling after splicing is a permanent size
    failure: it raises the admission-path size error and publishes nothing
    (the oversized frame is never assembled)."""
    instance = make_core0(delay_ms=0)
    fragment = instance._envelope_fragment(instance._next_sequence)
    body = _telemetry_body_of_length(MAX_OUTBOUND_MESSAGE_BYTES - len(fragment))
    entry = {
        "kind": KIND_TELEMETRY,
        "retention_priority": 40,
        "payload_bytes": body,
    }

    try:
        instance._publish_entry(entry)
    except OutboundMessageTooLargeError as err:
        # The size failure stays a ValueError for existing permanent handlers.
        assert isinstance(err, ValueError)
    else:
        pytest.fail("an envelope-oversized body was published")
    # Nothing reached the wire.
    assert instance._mqtt.published == []


def test_publish_entry_admits_a_body_that_just_fits_with_the_envelope(make_core0):
    """Boundary: body + comma + fragment + closing brace == exactly
    MAX_OUTBOUND_MESSAGE_BYTES is admitted; the check is a true ceiling."""
    instance = make_core0(delay_ms=0)
    fragment = instance._envelope_fragment(instance._next_sequence)
    body = _telemetry_body_of_length(MAX_OUTBOUND_MESSAGE_BYTES - len(fragment) - 1)
    entry = {
        "kind": KIND_TELEMETRY,
        "retention_priority": 40,
        "payload_bytes": body,
    }

    instance._publish_entry(entry)

    assert len(instance._mqtt.published) == 1
    _topic, wire, _now = instance._mqtt.published[0]
    assert len(wire) == MAX_OUTBOUND_MESSAGE_BYTES
    doc = json.loads(wire)
    # The body content and the spliced envelope coexist in one object.
    assert doc["message_type"] == "telemetry"
    assert doc["payload"]["blob"]
    assert doc["sequence"] == 0
    assert doc["source"] == instance._config["source"]


# --- The run loop: discard, do not stall -----------------------------------


def test_run_loop_discards_an_envelope_oversized_entry_and_keeps_draining(make_core0):
    """A spliced-oversized entry is discarded (not held in flight, not
    retried): the queue drains, the entry behind it still publishes, and the
    discard is counted."""
    instance = make_core0(delay_ms=0)
    _utc_synchronized(instance)
    queue = instance._intercore.outbound_queue
    fragment = instance._envelope_fragment(instance._next_sequence)
    oversized = _telemetry_body_of_length(MAX_OUTBOUND_MESSAGE_BYTES - len(fragment))
    assert queue.put_with_kind(
        KIND_TELEMETRY, oversized, RETENTION_PRIORITY_TELEMETRY
    )
    _queue_telemetry(instance, 7)

    _run_to(instance, 40)

    # Exactly the entry behind the discarded one made it out.
    published = instance._mqtt.published
    assert len(published) == 1
    assert json.loads(published[0][1])["payload"]["value"] == 7
    assert queue.get_depth() == 0
    assert queue.status()["oversized_discarded"] == 1


def test_run_loop_answers_a_discarded_command_response_with_the_bounded_substitute(make_core0):
    """A command response is a promise, not a sample: when the splice pushes
    it over the ceiling, the command is still answered -- with the small
    bounded error response naming the cause -- and the channel moves on."""
    instance = make_core0(delay_ms=0)
    _utc_synchronized(instance)
    queue = instance._intercore.outbound_queue
    fragment = instance._envelope_fragment(instance._next_sequence)
    body = _command_response_body_of_length(MAX_OUTBOUND_MESSAGE_BYTES - len(fragment))
    assert queue.put_with_kind(
        KIND_COMMAND_RESPONSE, body, RETENTION_PRIORITY_CRITICAL
    )

    _run_to(instance, 40)

    # Exactly one publish: the substitute. The oversized body never went out.
    assert len(instance._mqtt.published) == 1
    topic, wire, _now = instance._mqtt.published[0]
    assert topic == instance._config["mqtt_topic_command_response"]
    payload = json.loads(wire)["payload"]
    assert payload["success"] is False
    assert payload["error"]["code"] == "response_too_large"
    # The command identity is preserved: the caller's command is still answered.
    assert payload["command_id"] == "drain-big-1"
    assert payload["command"] == "get-details"
    assert payload["targeted"] is True
    assert "data" not in payload
    # The oversized original is gone; the substitute was serviced and popped.
    assert queue.get_depth() == 0
    assert queue.status()["oversized_discarded"] == 1
    assert instance._pending_core0_responses == []


def test_discarded_response_with_unreadable_body_is_dropped_without_a_substitute(make_core0):
    """A command-response entry whose body cannot be read back has no command
    identity to answer with: the discard stands (no crash, no substitute)."""
    instance = make_core0(delay_ms=0)
    _utc_synchronized(instance)
    queue = instance._intercore.outbound_queue
    fragment = instance._envelope_fragment(instance._next_sequence)
    n = MAX_OUTBOUND_MESSAGE_BYTES - len(fragment)
    body = b"{" + b"x" * (n - 2) + b"}"  # ends in '}' (the entry check) but is not JSON
    assert queue.put_with_kind(
        KIND_COMMAND_RESPONSE, body, RETENTION_PRIORITY_CRITICAL
    )

    _run_to(instance, 40)

    assert instance._mqtt.published == []
    assert instance._pending_core0_responses == []
    assert queue.get_depth() == 0
    assert queue.status()["oversized_discarded"] == 1


# --- Core 0's own response and reboot paths --------------------------------


def test_pending_response_oversized_only_by_the_envelope_is_substituted(make_core0):
    """A pending Core 0 response whose body passes admission but overflows
    only after the splice is answered with the bounded substitute instead of
    spinning forever on the same bytes; the substitute then publishes."""
    instance = make_core0(delay_ms=0)
    fragment = instance._envelope_fragment(instance._next_sequence)

    def body_size(blob_len):
        return len(json.dumps({
            "message_type": "command_response",
            "uptime_ms": instance._uptime_ms(),
            "timestamp": instance._current_utc_timestamp(),
            "payload": {
                "command_id": "cfg-big-1",
                "command": "read-config",
                "targeted": True,
                "success": True,
                "data": {"blob": "x" * blob_len},
            },
        }).encode("utf-8"))

    # Size the data blob so the body is exactly MAX - len(fragment):
    # admitted at admission, one byte over once spliced.
    blob_len = MAX_OUTBOUND_MESSAGE_BYTES - len(fragment) - body_size(0)
    response = {
        "command_id": "cfg-big-1",
        "command": "read-config",
        "success": True,
        "targeted": True,
        "data": {"blob": "x" * blob_len},
    }
    instance._pending_core0_responses.append(response)

    # Pass 1: the splice overflows; the response is answered in place with
    # its substitute and nothing is published.
    instance._service_pending_core0_response()
    assert instance._mqtt.published == []
    head = instance._pending_core0_responses[0]
    assert head is not response
    assert head["success"] is False
    assert head["error"]["code"] == "response_too_large"
    assert head["command_id"] == "cfg-big-1"
    assert head["command"] == "read-config"
    assert head["targeted"] is True

    # Pass 2: the substitute (small by construction) publishes.
    instance._service_pending_core0_response()
    assert len(instance._mqtt.published) == 1
    payload = json.loads(instance._mqtt.published[0][1])["payload"]
    assert payload["success"] is False
    assert payload["error"]["code"] == "response_too_large"
    assert payload["command_id"] == "cfg-big-1"
    assert instance._pending_core0_responses == []


def test_reboot_ack_oversized_by_the_envelope_is_answered_and_reboot_held(make_core0):
    """A permanent failure of the reboot acknowledgement must not spin the
    loop retrying the same bytes: the command is answered with the bounded
    substitute, and the reboot stays pending (no reset without an answer)."""
    import core0 as core0_module

    instance = make_core0(delay_ms=0)

    with patch.object(core0_module, "MAX_OUTBOUND_MESSAGE_BYTES", 64):
        instance._pending_reboot = {
            "command_id": "rbig-1",
            "command": "reboot",
            "targeted": True,
        }
        published = instance._perform_reboot()

    assert published is False
    assert _MACHINE.reset_calls == 0
    assert instance._pending_reboot is not None
    # The command is still answered -- with the bounded substitute.
    assert len(instance._pending_core0_responses) == 1
    head = instance._pending_core0_responses[0]
    assert head["success"] is False
    assert head["error"]["code"] == "response_too_large"
    assert head["command_id"] == "rbig-1"
    assert instance._mqtt.published == []


def test_permanent_reboot_ack_failure_queues_exactly_one_substitute(make_core0):
    """A permanently unsendable reboot acknowledgement is answered with the
    bounded substitute exactly once: later passes must not re-attempt the
    same unsendable bytes and must not queue another identical substitute."""
    import core0 as core0_module

    instance = make_core0(delay_ms=0)

    with patch.object(core0_module, "MAX_OUTBOUND_MESSAGE_BYTES", 64):
        instance._pending_reboot = {
            "command_id": "rbig-once",
            "command": "reboot",
            "targeted": True,
        }
        assert instance._perform_reboot() is False
        # The run loop can call this twice in one pass; both must be no-ops.
        assert instance._perform_reboot() is False
        assert instance._perform_reboot() is False

    # A later pass, with the ceiling no longer in play, still neither
    # re-attempts the unsendable acknowledgement (which would reset without
    # the failure answer) nor queues a second substitute.
    assert instance._perform_reboot() is False
    assert instance._mqtt.published == []
    assert _MACHINE.reset_calls == 0
    # The caller was answered exactly once, with the bounded substitute.
    assert len(instance._pending_core0_responses) == 1
    assert instance._pending_core0_responses[0]["command_id"] == "rbig-once"
    assert instance._pending_core0_responses[0]["error"]["code"] == "response_too_large"


def test_reboot_released_once_the_substitute_is_published(make_core0):
    """Once the bounded substitute has been published (the caller was told
    the reboot failed), the held reboot is released and a later reboot
    command is admissible instead of hitting reboot_already_pending."""
    import core0 as core0_module

    instance = make_core0(delay_ms=0)

    with patch.object(core0_module, "MAX_OUTBOUND_MESSAGE_BYTES", 64):
        instance._pending_reboot = {
            "command_id": "rbig-release",
            "command": "reboot",
            "targeted": True,
        }
        assert instance._perform_reboot() is False
    # Held until the substitute is out the door...
    assert instance._pending_reboot is not None

    instance._service_pending_core0_response()
    assert len(instance._mqtt.published) == 1
    payload = json.loads(instance._mqtt.published[0][1])["payload"]
    assert payload["success"] is False
    assert payload["error"]["code"] == "response_too_large"
    assert instance._pending_core0_responses == []
    # ...then the command is done: failed, and reported.
    assert instance._pending_reboot is None

    # A later reboot command is admissible again (no pending rejection).
    instance._handle_reboot_command("rbig-again", True, {})
    assert instance._pending_reboot is not None
    assert instance._pending_core0_responses == []


def test_full_response_queue_does_not_mark_the_reboot_answered(make_core0):
    """If the response queue is full the substitute is not queued, so the
    answer is not marked given: the next pass retries the answer (not the
    unsendable bytes) once there is room, and only then is it terminal."""
    import core0 as core0_module

    instance = make_core0(delay_ms=0)
    # Fill the bounded queue so the substitute cannot be accepted.
    for i in range(4):
        assert instance._queue_core0_response({
            "command_id": "filler-{}".format(i),
            "success": True,
            "targeted": False,
            "data": {"i": i},
        })
    assert len(instance._pending_core0_responses) == 4

    with patch.object(core0_module, "MAX_OUTBOUND_MESSAGE_BYTES", 64):
        instance._pending_reboot = {
            "command_id": "rbig-full",
            "command": "reboot",
            "targeted": True,
        }
        assert instance._perform_reboot() is False
    # Queue full: nothing was added and the answer was not marked given.
    assert len(instance._pending_core0_responses) == 4
    assert instance._pending_core0_responses[0]["command_id"] == "filler-0"
    assert instance._pending_reboot.get("_permanent_failure_answer_queued") is None
    assert _MACHINE.reset_calls == 0

    # Free one slot: the next pass queues the substitute and only then is
    # the request terminal.
    instance._pending_core0_responses.pop(0)
    with patch.object(core0_module, "MAX_OUTBOUND_MESSAGE_BYTES", 64):
        assert instance._perform_reboot() is False
    assert len(instance._pending_core0_responses) == 4
    assert instance._pending_core0_responses[-1]["command_id"] == "rbig-full"
    assert instance._pending_core0_responses[-1]["error"]["code"] == "response_too_large"
    # A further pass adds nothing.
    assert instance._perform_reboot() is False
    assert len(instance._pending_core0_responses) == 4
    assert _MACHINE.reset_calls == 0
