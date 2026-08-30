# test_device_events.py - Device diagnostic log events (Core 1)
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Host-side tests for the device event contract in core1._handle_device_result.

A read failure, a reinitialization outcome, and a suppression decision each
produce exactly the documented log event; a successful read produces telemetry
and no log event. Device identity (device_id, device) is structured data on a
single generic event -- never baked into the event name.
"""

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from config import split_config  # noqa: E402
from intercore import (  # noqa: E402
    InterCore,
    KIND_LOG,
    KIND_TELEMETRY,
    RETENTION_PRIORITY_INFO,
    RETENTION_PRIORITY_WARN,
)
import observability as obs  # noqa: E402
from test_reinit_suppression import (  # noqa: E402
    BOOT_TICKS_MS,
    FakeTime,
    _install_fakes,
    _reload_core1_under_fakes,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]


class _Env:
    """Real core1 (under MicroPython fakes) + real inter-core bus."""

    def __init__(self):
        self._fake_time = FakeTime()
        self._saved_modules = {
            name: sys.modules.get(name) for name in ("time", "machine", "os")
        }
        _install_fakes(self._fake_time)
        self.core1 = _reload_core1_under_fakes()

        raw = json.loads((ROOT / "config.json").read_text())
        _core0, core1_config, _bus = split_config(raw)
        self.config = core1_config
        self.bus = InterCore(outbound_max=16, event_max=4)
        self.uptime_state = self.core1.create_uptime_state(BOOT_TICKS_MS)

    def handle(self, result):
        self.core1._handle_device_result(
            self.bus, self.config, self.uptime_state, result
        )

    def queued(self):
        """The admitted entries, decoded, in queue order."""
        return [
            {
                "kind": entry["kind"],
                "priority": entry["retention_priority"],
                "message": json.loads(entry["payload_bytes"].decode("utf-8")),
            }
            for entry in self.bus.outbound_queue._queue
        ]

    def close(self):
        for name, module in self._saved_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


@pytest.fixture
def env():
    e = _Env()
    yield e
    e.close()


def test_read_failure_is_one_generic_event_with_structured_identity(env):
    result = {
        "status": "read_failed",
        "device_id": "dev-1",
        "device": "temperature",
        "error": OSError("ETIMEDOUT"),
        "consecutive_read_failures": 2,
    }
    env.handle(result)

    entries = env.queued()
    assert len(entries) == 1
    entry = entries[0]
    assert entry["kind"] == KIND_LOG
    assert entry["priority"] == RETENTION_PRIORITY_WARN

    message = entry["message"]
    assert message["message_type"] == "log"
    assert isinstance(message["uptime_ms"], int)

    payload = message["payload"]
    assert payload["level"] == obs.LEVEL_WARNING
    assert payload["event"] == obs.EVENT_DEVICE_READ_FAILED
    assert payload["reason_code"] == obs.REASON_DEVICE_READ_EXCEPTION
    # Exception text stays human-readable data; the reason code is canonical.
    assert payload["data"]["error"] == "ETIMEDOUT"
    assert payload["data"]["device_id"] == "dev-1"
    assert payload["data"]["device"] == "temperature"
    assert payload["data"]["consecutive_read_failures"] == 2
    # No module field; no raw exception object in the payload.
    assert "module" not in payload


def test_two_device_types_share_the_same_event_name(env):
    """One generic device vocabulary: the driver type is data, never the event."""
    env.handle({
        "status": "read_failed",
        "device_id": "dev-1",
        "device": "temperature",
        "error": OSError("sensor offline"),
        "consecutive_read_failures": 1,
    })
    env.handle({
        "status": "read_failed",
        "device_id": "dev-2",
        "device": "pressure",
        "error": ValueError("math domain error"),
        "consecutive_read_failures": 1,
    })

    entries = env.queued()
    assert len(entries) == 2
    events = {e["message"]["payload"]["event"] for e in entries}
    assert events == {obs.EVENT_DEVICE_READ_FAILED}
    devices = {e["message"]["payload"]["data"]["device"] for e in entries}
    assert devices == {"temperature", "pressure"}
    # The device name is never embedded in the event string.
    for entry in entries:
        event = entry["message"]["payload"]["event"]
        assert "temperature" not in event
        assert "pressure" not in event


def test_successful_read_emits_telemetry_and_no_log_event(env):
    env.handle({
        "status": "telemetry",
        "device_id": "dev-1",
        "device": "temperature",
        "sensor_type": "temperature",
        "name": "celsius",
        "telemetry": {"value": 21},
    })

    entries = env.queued()
    assert len(entries) == 1
    assert entries[0]["kind"] == KIND_TELEMETRY
    assert entries[0]["message"]["message_type"] == "telemetry"
    # The measurement is the telemetry message; no diagnostic log for it.
    assert not any(e["kind"] == KIND_LOG for e in entries)


def test_reinitialization_completed_shape(env):
    env.handle({
        "status": "reinitialized",
        "device_id": "dev-1",
        "device": "temperature",
        "reinitialization_attempts_used": 2,
    })

    entries = env.queued()
    assert len(entries) == 1
    assert entries[0]["kind"] == KIND_LOG
    assert entries[0]["priority"] == RETENTION_PRIORITY_INFO

    payload = entries[0]["message"]["payload"]
    assert payload["level"] == obs.LEVEL_INFO
    assert payload["event"] == obs.EVENT_DEVICE_REINITIALIZATION_COMPLETED
    assert payload["reason_code"] == obs.REASON_NONE
    assert payload["data"]["reinitialization_attempts_used"] == 2


def test_reinitialization_failed_shape(env):
    env.handle({
        "status": "reinitialization_failed",
        "device_id": "dev-1",
        "device": "temperature",
        "error": RuntimeError("simulated initialization failure"),
        "log_failure_warning": True,
    })

    entries = env.queued()
    assert len(entries) == 1
    assert entries[0]["kind"] == KIND_LOG
    assert entries[0]["priority"] == RETENTION_PRIORITY_WARN

    payload = entries[0]["message"]["payload"]
    assert payload["level"] == obs.LEVEL_WARNING
    assert payload["event"] == obs.EVENT_DEVICE_REINITIALIZATION_FAILED
    assert payload["reason_code"] == obs.REASON_DEVICE_REINITIALIZATION_FAILED
    assert payload["data"]["error"] == "simulated initialization failure"


def test_reinitialization_failure_suppression_is_respected(env):
    """device_manager flags which failures should warn; a suppressed one
    produces no wire event at all."""
    env.handle({
        "status": "reinitialization_failed",
        "device_id": "dev-1",
        "device": "temperature",
        "error": RuntimeError("simulated initialization failure"),
        "log_failure_warning": False,
    })

    assert env.queued() == []
