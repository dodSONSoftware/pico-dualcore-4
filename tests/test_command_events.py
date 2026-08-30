# test_command_events.py - Command event and error-code contract
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Host-side tests for the command observability contract.

Core 0 rejections use the canonical ``command_*`` vocabulary in both the
command response error code and the queued log event; a reboot acceptance
emits ``runtime_reboot_requested``; a Core 1 unsupported command emits
``command_rejected`` + response code ``command_unknown``. The command name is
always structured data (``data.command``), never part of the event string.
"""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import observability as obs  # noqa: E402
from version import MESSAGE_SCHEMA_VERSION  # noqa: E402
from test_core0_recovery import make_core0  # noqa: E402


def _command_doc(command="reboot", command_id="req-1", schema=MESSAGE_SCHEMA_VERSION,
                 payload={}):
    return {
        "message_type": "command",
        "target": "*",
        "command": command,
        "command_id": command_id,
        "message_schema_version": schema,
        "payload": payload,
    }


def _deliver(instance, doc):
    instance._on_mqtt_message(
        instance._config["mqtt_topic_command"], json.dumps(doc)
    )


def _last_log(instance):
    assert instance._pending_connection_logs, "expected a queued log event"
    message = instance._pending_connection_logs[-1]
    assert message["message_type"] == "log"
    return message["payload"]


def _last_response(instance):
    assert instance._pending_core0_responses, "expected a queued response"
    return instance._pending_core0_responses[-1]


def test_invalid_schema_rejection_uses_command_invalid_envelope(make_core0):
    instance = make_core0()
    _deliver(instance, _command_doc(schema=MESSAGE_SCHEMA_VERSION + 1))

    payload = _last_log(instance)
    assert payload["level"] == obs.LEVEL_WARNING
    assert payload["event"] == obs.EVENT_COMMAND_REJECTED
    assert payload["reason_code"] == obs.REASON_COMMAND_INVALID_ENVELOPE
    assert payload["data"] == {"command": "reboot", "command_id": "req-1"}

    response = _last_response(instance)
    assert response["success"] is False
    assert response["error"]["code"] == obs.REASON_COMMAND_INVALID_ENVELOPE


def test_missing_payload_rejection_uses_command_invalid_payload(make_core0):
    instance = make_core0()
    doc = _command_doc()
    del doc["payload"]
    _deliver(instance, doc)

    payload = _last_log(instance)
    assert payload["event"] == obs.EVENT_COMMAND_REJECTED
    assert payload["reason_code"] == obs.REASON_COMMAND_INVALID_PAYLOAD
    response = _last_response(instance)
    assert response["error"]["code"] == obs.REASON_COMMAND_INVALID_PAYLOAD


def test_non_object_payload_rejection_uses_command_invalid_payload(make_core0):
    instance = make_core0()
    _deliver(instance, _command_doc(payload=[]))

    payload = _last_log(instance)
    assert payload["reason_code"] == obs.REASON_COMMAND_INVALID_PAYLOAD
    response = _last_response(instance)
    assert response["error"]["code"] == obs.REASON_COMMAND_INVALID_PAYLOAD


def test_reboot_with_non_empty_payload_is_command_invalid_payload(make_core0):
    instance = make_core0()
    _deliver(instance, _command_doc(payload={"force": True}))

    payload = _last_log(instance)
    assert payload["reason_code"] == obs.REASON_COMMAND_INVALID_PAYLOAD
    response = _last_response(instance)
    assert response["error"]["code"] == obs.REASON_COMMAND_INVALID_PAYLOAD


def test_reboot_acceptance_emits_runtime_reboot_requested(make_core0):
    instance = make_core0()
    _deliver(instance, _command_doc())

    payload = _last_log(instance)
    assert payload["level"] == obs.LEVEL_INFO
    assert payload["event"] == obs.EVENT_RUNTIME_REBOOT_REQUESTED
    assert payload["reason_code"] == obs.REASON_NONE
    assert payload["data"] == {"command": "reboot", "command_id": "req-1"}
    # The acceptance is a log event; the response rides the reboot path.
    assert instance._pending_reboot is not None
    assert instance._pending_core0_responses == []


def test_duplicate_reboot_is_command_duplicate(make_core0):
    instance = make_core0()
    _deliver(instance, _command_doc())  # accepted
    assert instance._pending_reboot is not None
    _deliver(instance, _command_doc(command_id="req-2"))

    payload = _last_log(instance)
    assert payload["event"] == obs.EVENT_COMMAND_REJECTED
    assert payload["reason_code"] == obs.REASON_COMMAND_DUPLICATE
    assert payload["data"]["command_id"] == "req-2"
    response = _last_response(instance)
    assert response["error"]["code"] == obs.REASON_COMMAND_DUPLICATE


def test_event_queue_full_is_command_execution_failed(make_core0):
    """A Core 1 command that cannot even be queued is an execution failure,
    not a rejection: canonical response code, no rejection log."""
    instance = make_core0()
    instance._intercore.event_queue.put = lambda event: False

    _deliver(instance, _command_doc(command="pause"))

    response = _last_response(instance)
    assert response["success"] is False
    assert response["error"]["code"] == obs.REASON_COMMAND_EXECUTION_FAILED
    assert instance._pending_connection_logs == []


def test_command_name_is_data_never_part_of_the_event_string(make_core0):
    instance = make_core0()
    _deliver(instance, _command_doc(schema=MESSAGE_SCHEMA_VERSION + 1))

    payload = _last_log(instance)
    event = payload["event"]
    assert event == obs.EVENT_COMMAND_REJECTED
    assert "reboot" not in event
    assert "command_id" not in event
    # The identity lives in structured data instead.
    assert payload["data"]["command"] == "reboot"


# --- Core 1: unsupported command -------------------------------------------------

def test_core1_unsupported_command_rejected_log_and_command_unknown():
    from intercore import InterCore, KIND_LOG, RETENTION_PRIORITY_WARN

    from test_reinit_suppression import (
        BOOT_TICKS_MS,
        FakeTime,
        _install_fakes,
        _reload_core1_under_fakes,
    )

    fake_time = FakeTime()
    saved = {name: sys.modules.get(name) for name in ("time", "machine", "os")}
    _install_fakes(fake_time)
    try:
        core1 = _reload_core1_under_fakes()
        bus = InterCore(outbound_max=16, event_max=4)
        uptime_state = core1.create_uptime_state(BOOT_TICKS_MS)

        bus.event_queue.put({
            "command": "reboot",
            "command_id": "req-9",
            "targeted": True,
        })
        response = core1._process_intercore_event(bus, uptime_state)

        # The response carries the canonical unknown-command code.
        assert response["kind"] == "command_response"
        body = response["message"]["payload"]
        assert body["success"] is False
        assert body["error"]["code"] == obs.REASON_COMMAND_UNKNOWN
        assert body["command_id"] == "req-9"

        # And exactly one rejection log event was queued.
        entries = bus.outbound_queue._queue
        assert len(entries) == 1
        assert entries[0]["kind"] == KIND_LOG
        assert entries[0]["retention_priority"] == RETENTION_PRIORITY_WARN
        log_payload = json.loads(entries[0]["payload_bytes"].decode("utf-8"))["payload"]
        assert log_payload["level"] == obs.LEVEL_WARNING
        assert log_payload["event"] == obs.EVENT_COMMAND_REJECTED
        assert log_payload["reason_code"] == obs.REASON_COMMAND_UNKNOWN
        assert log_payload["data"] == {"command": "reboot", "command_id": "req-9"}
        # The command name is never part of the event string.
        assert "reboot" not in log_payload["event"]
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
