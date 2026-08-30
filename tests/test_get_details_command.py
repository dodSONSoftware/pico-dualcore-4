# test_get_details_command.py - Tests for the get-details command
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import pathlib
import sys
from unittest.mock import MagicMock, patch


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.modules.setdefault("machine", MagicMock())

import core1
from intercore import KIND_COMMAND_RESPONSE
from system_information import SYSTEM_INFORMATION_SECTIONS


class EventQueue:
    def __init__(self, event):
        self._event = event

    def take(self):
        event = self._event
        self._event = None
        return event


class InterCore:
    def __init__(self, event):
        self.event_queue = EventQueue(event)


class FullSystemInformation:
    def __getattr__(self, name):
        if not name.startswith("get_"):
            raise AttributeError(name)
        section = name[4:]
        if section not in SYSTEM_INFORMATION_SECTIONS:
            raise AttributeError(name)
        return lambda: {"section": section}


def _event(payload=None):
    return {
        "command_id": "details-001",
        "command": "get-details",
        "payload": {} if payload is None else payload,
        "targeted": True,
    }


def test_get_details_returns_every_system_information_section():
    intercore = InterCore(_event())

    with patch.object(core1, "_message_time", return_value=(1234, "2026-08-30T21:00:00.000Z")):
        response = core1._process_intercore_event(
            intercore,
            object(),
            FullSystemInformation(),
        )

    assert response["kind"] == KIND_COMMAND_RESPONSE
    message = response["message"]
    assert message["message_type"] == "command_response"
    assert message["payload"]["command_id"] == "details-001"
    assert message["payload"]["command"] == "get-details"
    assert message["payload"]["success"] is True

    data = message["payload"]["data"]
    assert tuple(data) == SYSTEM_INFORMATION_SECTIONS
    for section in SYSTEM_INFORMATION_SECTIONS:
        assert data[section] == {"section": section}


def test_configured_include_does_not_restrict_get_details():
    """The system-information device include list limits scheduled telemetry
    reads only; get-details must still return every configured section."""
    from devices.system_information.system_information_device import (
        SystemInformationDevice,
    )

    system_information = FullSystemInformation()
    device = SystemInformationDevice(system_information)
    device.initialize({"include": ["network", "memory", "runtime"]})

    # The configured device read honors the include subset...
    assert tuple(device.read()) == ("network", "memory", "runtime")

    # ...but the command path must ignore it and return the full snapshot.
    intercore = InterCore(_event())
    with patch.object(core1, "_message_time", return_value=(1234, None)):
        response = core1._process_intercore_event(
            intercore,
            object(),
            system_information,
        )

    data = response["message"]["payload"]["data"]
    assert tuple(data) == SYSTEM_INFORMATION_SECTIONS


def test_get_details_rejects_non_empty_payload():
    intercore = InterCore(_event({"include": ["memory"]}))

    with patch.object(core1, "_message_time", return_value=(1234, None)):
        response = core1._process_intercore_event(
            intercore,
            object(),
            FullSystemInformation(),
        )

    payload = response["message"]["payload"]
    assert payload["success"] is False
    assert payload["error"] == {
        "code": "invalid_payload",
        "message": "get-details payload must be {}",
    }
