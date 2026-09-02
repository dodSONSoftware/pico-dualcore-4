# test_core0_response_admission.py - Tests for the Core 0 command response channel
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Host-side tests for the Core 0 command response admission contract.

The pending Core 0 response queue is FIFO and only the head is serviced per pass. A permanent serialization failure (a response that can never be published as-is) must therefore not stay queued retrying the same bytes forever: that would block every response behind it until the queue fills and the command plane stalls. The head response is answered with a small bounded error response for the same command whose code states the actual cause -- "response_too_large" for an oversized response, "response_invalid" for a validation or serialization failure -- so a response can never permanently stall the command channel behind it. Transient publish failures (a failed QoS 1 attempt, which raises) still leave the original response pending for retry, unchanged."""

import json
import pathlib
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.modules.setdefault("machine", MagicMock())

from test_read_write_config_commands import make_core0  # noqa: E402, F401
from message_serializer import MAX_OUTBOUND_MESSAGE_BYTES  # noqa: E402


def _success_response(command_id, command, data):
    return {
        "command_id": command_id,
        "command": command,
        "success": True,
        "targeted": True,
        "data": data,
    }


def test_oversized_response_is_replaced_with_response_too_large(make_core0):
    core0 = make_core0()
    big = _success_response(
        "cfg-read-big",
        "read-config",
        {"blob": "x" * (MAX_OUTBOUND_MESSAGE_BYTES + 4096)},
    )
    core0._pending_core0_responses.append(big)

    core0._service_pending_core0_response()

    # The original is gone from the queue; a bounded substitute holds its slot.
    head = core0._pending_core0_responses[0]
    assert head is not big
    assert head["success"] is False
    assert head["error"]["code"] == "response_too_large"
    assert "data" not in head
    # The command identity is preserved: the caller's command is still answered.
    assert head["command_id"] == "cfg-read-big"
    assert head["command"] == "read-config"
    assert head["targeted"] is True
    # The substitute is small by construction: far under the per-message ceiling.
    assert len(json.dumps(head).encode("utf-8")) <= 512


def test_invalid_response_is_replaced_with_response_invalid(make_core0):
    """A permanent failure that is not a size problem must be reported as
    such: a serialization/validation defect must not be misreported to the
    command sender as a size limit."""
    core0 = make_core0()
    invalid = _success_response(
        "cfg-read-invalid",
        "read-config",
        {"blob": float("nan")},
    )
    core0._pending_core0_responses.append(invalid)

    core0._service_pending_core0_response()

    head = core0._pending_core0_responses[0]
    assert head is not invalid
    assert head["success"] is False
    assert head["error"]["code"] == "response_invalid"
    assert head["command_id"] == "cfg-read-invalid"
    assert head["command"] == "read-config"


def test_substitute_publishes_and_the_channel_moves_on(make_core0):
    """A permanently invalid response cannot block the responses behind it:
    the substitute takes the failed response's slot, publishes on the next
    pass, and the waiting response is serviced after it."""
    core0 = make_core0()
    big = _success_response(
        "cfg-read-big",
        "read-config",
        {"blob": "x" * (MAX_OUTBOUND_MESSAGE_BYTES + 4096)},
    )
    waiting = _success_response("cfg-read-2", "read-config", {"ok": True})
    core0._pending_core0_responses.extend([big, waiting])

    # Pass 1: the oversized response is replaced in place by its substitute.
    core0._service_pending_core0_response()
    assert core0._pending_core0_responses[1] is waiting
    assert core0._mqtt.publish_qos1.call_count == 0

    # Pass 2: the substitute publishes; the original oversized bytes never go on the wire.
    core0._service_pending_core0_response()
    frame = json.loads(core0._mqtt.publish_qos1.call_args_list[0][0][1])
    payload = frame["payload"]
    assert payload["success"] is False
    assert payload["error"]["code"] == "response_too_large"
    assert payload["command_id"] == "cfg-read-big"
    assert payload["command"] == "read-config"
    assert core0._pending_core0_responses == [waiting]

    # Pass 3: the response that was waiting behind it gets its turn.
    core0._service_pending_core0_response()
    assert core0._pending_core0_responses == []
    frame2 = json.loads(core0._mqtt.publish_qos1.call_args_list[1][0][1])
    assert frame2["payload"]["success"] is True
    assert frame2["payload"]["command_id"] == "cfg-read-2"

    # The substitute and the waiting response carry distinct wire sequences.
    assert frame["sequence"] != frame2["sequence"]


def test_transient_publish_failure_still_keeps_the_original_pending(make_core0):
    """Only permanent failures are substituted: a failed QoS 1 attempt
    (which raises) leaves the original response queued for retry, unchanged."""
    core0 = make_core0()
    response = _success_response("cfg-read-t", "read-config", {"ok": True})
    core0._pending_core0_responses.append(response)
    # A lost PUBACK is a socket timeout in the real client: a transport
    # failure (OSError), not a programming error.
    core0._mqtt.publish_qos1.side_effect = OSError("PUBACK timeout")

    with pytest.raises(OSError):
        core0._service_pending_core0_response()

    assert core0._pending_core0_responses == [response]
    assert "error" not in response
