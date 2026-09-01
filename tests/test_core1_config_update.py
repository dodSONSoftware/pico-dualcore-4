# test_core1_config_update.py - Core 1 config-update (hot-reload) contract
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""
Host-side tests for Core 1's half of the HOT_RELOADED write-config
handshake, over the dedicated config-update lane. A configuration reload is a
NEW scheduling boundary: each changed interval is re-anchored from the reload
instant (next = now + interval), Core 1's own config copy is refreshed (so the
health activity threshold, which reads read_loop_sec, uses the new value), the
request is acknowledged with exactly one result on the lane, and a failed apply
restores the prior values and reports a bounded failure. No command response is
owed on this path (it is internal control traffic, not a user command).
"""

import pathlib
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.modules.setdefault("machine", MagicMock())

import core1


class FakeTime:
    """Monotonic stand-in for MicroPython's time module."""

    def __init__(self, now_ms=0):
        self.now_ms = now_ms

    def ticks_ms(self):
        return self.now_ms

    def ticks_diff(self, now, prev):
        return now - prev

    def ticks_add(self, base, delta):
        return base + delta


class FakeLane:
    """The config-update request/result lane for Core 1 tests: holds one
    pending request and records every result Core 1 posts."""

    def __init__(self, request=None):
        self._request = request
        self.results = []

    def post_request(self, request):
        self._request = request

    def take_request(self):
        request = self._request
        self._request = None
        return request

    def post_result(self, result):
        self.results.append(result)

    def take_result_for(self, generation):
        return None


class _Intercore:
    def __init__(self, lane):
        self.config_update_lane = lane


# --- _regrid_next_boundary (still used to align the boot-anchor init) --------


@pytest.fixture
def fake_time(monkeypatch):
    fake = FakeTime(now_ms=0)
    monkeypatch.setattr(core1, "time", fake)
    return fake


def test_regrid_first_boundary_when_now_is_before_it(fake_time):
    assert core1._regrid_next_boundary(0, 1000, 2000) == 2000


def test_regrid_skips_boundaries_that_already_passed(fake_time):
    assert core1._regrid_next_boundary(0, 2000, 2000) == 4000
    assert core1._regrid_next_boundary(0, 10_000, 3000) == 12_000


def test_regrid_never_returns_a_boundary_at_or_before_now(fake_time):
    for now in (0, 1, 1999, 2000, 2001, 4000, 1_000_000):
        next_ms = core1._regrid_next_boundary(0, now, 2000)
        assert next_ms > now
        assert next_ms % 2000 == 0


# --- _apply_config_update: re-anchor + refresh the config copy --------------


def _config():
    return {"read_loop_sec": 20, "health_interval_sec": 60}


def _schedulers():
    return {
        "anchor_ms": 0,
        "read_loop_ms": 20_000,
        "health_interval_ms": 60_000,
        "next_read_ms": 20_000,
        "next_health_ms": 60_000,
    }


def test_apply_reanchors_read_from_the_reload_instant(fake_time):
    fake_time.now_ms = 95_000
    config = _config()
    schedulers = _schedulers()

    core1._apply_config_update({"read_loop_sec": 10}, config, schedulers)

    assert config["read_loop_sec"] == 10
    assert schedulers["read_loop_ms"] == 10_000
    assert schedulers["next_read_ms"] == 105_000  # now (95s) + new interval (10s)
    # health untouched
    assert config["health_interval_sec"] == 60
    assert schedulers["health_interval_ms"] == 60_000


def test_apply_reanchors_health_from_the_reload_instant(fake_time):
    fake_time.now_ms = 95_000
    config = _config()
    schedulers = _schedulers()

    core1._apply_config_update({"health_interval_sec": 30}, config, schedulers)

    assert config["health_interval_sec"] == 30
    assert schedulers["health_interval_ms"] == 30_000
    assert schedulers["next_health_ms"] == 125_000  # now (95s) + new interval (30s)
    # read untouched
    assert config["read_loop_sec"] == 20
    assert schedulers["read_loop_ms"] == 20_000


def test_apply_grow_and_shrink_both_start_one_interval_after_now(fake_time):
    # A shortened interval does not burst an earlier boundary; a lengthened one
    # does not fire sooner than a new interval either -- both start at now + n.
    fake_time.now_ms = 95_000
    config = _config()
    schedulers = _schedulers()
    core1._apply_config_update({"read_loop_sec": 5}, config, schedulers)
    assert schedulers["next_read_ms"] == 100_000

    fake_time.now_ms = 95_000
    config = _config()
    schedulers = _schedulers()
    core1._apply_config_update({"read_loop_sec": 120}, config, schedulers)
    assert schedulers["next_read_ms"] == 215_000


def test_apply_touches_only_the_keys_it_carries(fake_time):
    fake_time.now_ms = 7000
    config = _config()
    schedulers = _schedulers()

    core1._apply_config_update({"read_loop_sec": 10}, config, schedulers)

    assert config["read_loop_sec"] == 10
    assert schedulers["read_loop_ms"] == 10_000
    assert schedulers["next_read_ms"] == 17_000
    # health untouched
    assert config["health_interval_sec"] == 60
    assert schedulers["health_interval_ms"] == 60_000
    assert schedulers["next_health_ms"] == 60_000


def test_apply_updates_config_copy_for_the_health_threshold(fake_time):
    """The health activity threshold reads config["read_loop_sec"]; a reload
    must refresh that copy so subsequent health reports use the new value."""
    fake_time.now_ms = 0
    config = {"read_loop_sec": 30, "health_interval_sec": 60}
    schedulers = _schedulers()
    assert max(config["read_loop_sec"] * 3 * 1000, 60_000) == 90_000

    core1._apply_config_update({"read_loop_sec": 100}, config, schedulers)

    assert max(config["read_loop_sec"] * 3 * 1000, 60_000) == 300_000


# --- _process_config_update: lane apply + acknowledgement --------------------


def test_request_applies_and_acks_success(fake_time):
    lane = FakeLane(request={"generation": 7,
                             "read_loop_sec": 10, "health_interval_sec": 30})
    intercore = _Intercore(lane)
    config = _config()
    schedulers = _schedulers()

    core1._process_config_update(intercore, config, schedulers)

    assert config["read_loop_sec"] == 10
    assert config["health_interval_sec"] == 30
    assert schedulers["read_loop_ms"] == 10_000
    assert schedulers["health_interval_ms"] == 30_000
    assert schedulers["next_read_ms"] == 10_000  # now (0) + new interval
    assert lane.results == [{"generation": 7, "success": True}]
    assert lane.take_request() is None  # the request was consumed


def test_request_ack_carrying_single_key_only(fake_time):
    lane = FakeLane(request={"generation": 2, "read_loop_sec": 40})
    intercore = _Intercore(lane)
    config = _config()
    schedulers = _schedulers()

    core1._process_config_update(intercore, config, schedulers)

    assert config["read_loop_sec"] == 40
    assert config["health_interval_sec"] == 60
    assert lane.results == [{"generation": 2, "success": True}]


def test_no_request_leaves_state_and_results_untouched():
    lane = FakeLane()
    intercore = _Intercore(lane)
    config = _config()
    schedulers = _schedulers()
    original = dict(schedulers)

    core1._process_config_update(intercore, config, schedulers)

    assert config == _config()
    assert schedulers == original
    assert lane.results == []


def test_apply_failure_restores_prior_values_and_reports_failure(monkeypatch):
    """An unexpected apply failure leaves Core 1 unchanged and posts a bounded
    failure, so Core 0 can roll the transaction back."""
    def _boom(config_update, config, schedulers):
        raise RuntimeError("apply failed")

    monkeypatch.setattr(core1, "_apply_config_update", _boom)
    lane = FakeLane(request={"generation": 3, "read_loop_sec": 10})
    intercore = _Intercore(lane)
    config = _config()
    schedulers = _schedulers()
    original = dict(schedulers)

    core1._process_config_update(intercore, config, schedulers)

    # Restored: neither the config copy nor the schedulers changed.
    assert config == _config()
    assert schedulers == original
    assert lane.results == [
        {"generation": 3, "success": False, "code": "core1_apply_failed"}
    ]


def test_config_update_does_not_touch_the_command_event_queue(fake_time):
    """Config-update traffic is on its own lane and never on the user-command
    event queue, so the get-details path is never reached for it."""
    lane = FakeLane(request={"generation": 1, "read_loop_sec": 10})
    intercore = _Intercore(lane)
    intercore.event_queue = MagicMock()
    config = _config()
    schedulers = _schedulers()

    core1._process_config_update(intercore, config, schedulers)

    intercore.event_queue.take.assert_not_called()
