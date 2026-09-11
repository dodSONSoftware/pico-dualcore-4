# test_command_admission_capacity.py - Core 0 command admission vs response capacity
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Host-side tests for the Core 0 command-admission / response-capacity contract.

A command is only admitted if it can reserve a response slot: admitting a
command that then cannot acknowledge itself would let it execute (write-config
promotes config.json, reboot arms _pending_reboot, get-details posts a Core 1
event) while its acknowledgement is dropped by a full response queue -- after
which the sender's retry hits the command-ID debounce cache and is silently
lost. The command ran, the answer never arrived, and no retry can recover it.

So a full response queue refuses the admission outright: the command is not
executed, its command_id is not claimed, and no response is queued; the same
command can be redelivered once capacity frees up.
"""

import pathlib
import sys
from unittest.mock import MagicMock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.modules.setdefault("machine", MagicMock())

from test_read_write_config_commands import (  # noqa: E402
    _command,
    _committed,
    _full_config,
    _send,
    make_core0,
)


def _max_pending():
    # core0 is imported (with wifi/mqtt mocked) by the make_core0 fixture; read
    # the constant from the live module rather than importing it at collection
    # time (which would drag in MicroPython's `network`).
    return sys.modules["core0"]._MAX_PENDING_CORE0_RESPONSES


def _dummy_response(tag):
    return {"command_id": "dummy-{}".format(tag), "success": True, "targeted": True}


# --- full queue: admission is refused before execution / claim ---------------


def test_full_queue_refuses_write_config_without_executing_or_claiming(make_core0, tmp_path):
    """The P1 case: a full response queue must refuse write-config so it never
    executes (no config.json promotion) and never claims the command_id, which
    is what would turn a later retry into a silent debounce drop."""
    core0 = make_core0()
    for i in range(_max_pending()):
        core0._pending_core0_responses.append(_dummy_response(i))

    candidate = _full_config()
    candidate["source"] = "Other-Pico"  # a REBOOT_REQUIRED change that would commit
    _send(core0, _command("write-config", "adm-cap-1", payload={"config": candidate}))

    # Not executed: config.json is unchanged.
    assert _committed(tmp_path)["source"] != "Other-Pico"
    # Not claimed: the command_id is absent from the debounce cache.
    assert "adm-cap-1" not in core0._recent_command_ids
    # No response queued: the queue is unchanged (still exactly full).
    assert len(core0._pending_core0_responses) == _max_pending()
    # No side effects armed either.
    assert core0._pending_reboot is None


def test_full_queue_refuses_read_config_without_claiming(make_core0):
    core0 = make_core0()
    for i in range(_max_pending()):
        core0._pending_core0_responses.append(_dummy_response(i))

    _send(core0, _command("read-config", "adm-cap-2"))

    assert "adm-cap-2" not in core0._recent_command_ids
    assert len(core0._pending_core0_responses) == _max_pending()
    assert not any(r.get("command_id") == "adm-cap-2"
                   for r in core0._pending_core0_responses)


def test_full_queue_refuses_reboot_without_arming(make_core0):
    core0 = make_core0()
    for i in range(_max_pending()):
        core0._pending_core0_responses.append(_dummy_response(i))

    _send(core0, _command("reboot", "adm-cap-3"))

    # A refused reboot must not arm the reset, nor claim the ID.
    assert core0._pending_reboot is None
    assert "adm-cap-3" not in core0._recent_command_ids


def test_full_queue_refuses_get_details_without_posting_event(make_core0):
    core0 = make_core0()
    for i in range(_max_pending()):
        core0._pending_core0_responses.append(_dummy_response(i))

    _send(core0, _command("get-details", "adm-cap-4"))

    assert core0._intercore.event_queue.events == []
    assert "adm-cap-4" not in core0._recent_command_ids


# --- retry after capacity frees up -------------------------------------------


def test_same_command_redelivered_after_capacity_frees_is_accepted(make_core0, tmp_path):
    """The refused command must be re-admitable: once the queue drains, the
    same command_id (which was never claimed) executes and is answered."""
    core0 = make_core0()
    for i in range(_max_pending()):
        core0._pending_core0_responses.append(_dummy_response(i))

    candidate = _full_config()
    candidate["source"] = "Other-Pico"
    _send(core0, _command("write-config", "adm-retry", payload={"config": candidate}))
    assert "adm-retry" not in core0._recent_command_ids
    assert _committed(tmp_path)["source"] != "Other-Pico"

    # Capacity frees up; the same command_id is now accepted and executes.
    core0._pending_core0_responses.clear()
    _send(core0, _command("write-config", "adm-retry", payload={"config": candidate}))

    assert "adm-retry" in core0._recent_command_ids
    assert _committed(tmp_path)["source"] == "Other-Pico"


# --- boundary: one free slot still admits ------------------------------------


def test_one_free_slot_still_admits_command(make_core0):
    """With one slot free (queue at MAX-1), admission proceeds and the command's
    own response uses that slot, filling the queue to capacity."""
    core0 = make_core0()
    for i in range(_max_pending() - 1):
        core0._pending_core0_responses.append(_dummy_response(i))

    _send(core0, _command("read-config", "adm-bound"))

    assert "adm-bound" in core0._recent_command_ids
    assert len(core0._pending_core0_responses) == _max_pending()
    assert any(r.get("command_id") == "adm-bound"
               for r in core0._pending_core0_responses)
