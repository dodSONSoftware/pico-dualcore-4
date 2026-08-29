# test_memory_error_propagation.py - MemoryError must never be swallowed by generic handlers
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Host-side regression tests for the MemoryError propagation contract.

Heap exhaustion is a fatal condition, not a failed optional diagnostic: a
generic ``except Exception`` handler on an allocation-heavy path (message
serialization/queuing, snapshot collection) must re-raise ``MemoryError``
instead of swallowing it, so the core stops with a diagnosable error rather
than continuing to allocate on top of an exhausted heap or discarding a
message as if serialization had merely failed. Ordinary errors (malformed
data, a failed serialization) must still be swallowed and reported by the
handler.

Covers the fixed paths:
- core1._try_queue_startup_log
- core1._try_queue_health_message
- core1._collect_system_information_full
- core0.Core0._publish_core0_command_response
"""

import importlib
import json
import pathlib
import sys
import time as _real_time
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

ROOT = pathlib.Path(__file__).resolve().parents[1]

from config import split_config  # noqa: E402


def _core1():
    """The core1 module, importable on the host.

    The core1 chain imports MicroPython-only modules, so core1 cannot be
    imported at collection time. If an earlier test already imported/reloaded
    it under its fakes, reuse that module object (the functions tested here do
    not depend on which fakes are bound).
    """
    if "core1" in sys.modules:
        return sys.modules["core1"]
    if "machine" not in sys.modules:
        sys.modules["machine"] = MagicMock()
    return importlib.import_module("core1")


class _MockOutboundQueue:
    """Records put_with_kind() calls; never reached when serialization raises."""

    def __init__(self):
        self.put_with_kind_calls = []

    def put_with_kind(self, kind, payload_bytes, retention_priority):
        self.put_with_kind_calls.append((kind, payload_bytes, retention_priority))
        return True


class _MockInterCore:
    def __init__(self):
        self.outbound_queue = _MockOutboundQueue()


class _ExhaustedSystemInformation:
    """Every get_<section>() raises MemoryError, in any section order."""

    def __getattr__(self, name):
        if name.startswith("get_"):
            def _raise():
                raise MemoryError
            return _raise
        raise AttributeError(name)


def test_startup_log_queueing_propagates_memoryerror(monkeypatch):
    """A MemoryError from serialization must escape, not return False."""
    core1 = _core1()

    def _exhaust(*args, **kwargs):
        raise MemoryError

    monkeypatch.setattr(core1, "serialize_and_validate_message", _exhaust)
    intercore = _MockInterCore()

    with pytest.raises(MemoryError):
        core1._try_queue_startup_log(intercore, {"message_type": "log"}, 0)

    assert intercore.outbound_queue.put_with_kind_calls == []


def test_health_queueing_propagates_memoryerror(monkeypatch):
    """A MemoryError from serialization must escape, not return False."""
    core1 = _core1()

    def _exhaust(*args, **kwargs):
        raise MemoryError

    monkeypatch.setattr(core1, "serialize_and_validate_message", _exhaust)
    intercore = _MockInterCore()

    with pytest.raises(MemoryError):
        core1._try_queue_health_message(intercore, {"message_type": "health"})

    assert intercore.outbound_queue.put_with_kind_calls == []


def test_health_queueing_still_swallows_ordinary_errors(monkeypatch):
    """Non-fatal serialization failures are still reported and return False."""
    core1 = _core1()

    def _fail(*args, **kwargs):
        raise RuntimeError("malformed message")

    monkeypatch.setattr(core1, "serialize_and_validate_message", _fail)
    intercore = _MockInterCore()

    assert core1._try_queue_health_message(intercore, {"message_type": "health"}) is False
    assert intercore.outbound_queue.put_with_kind_calls == []


def test_collect_system_information_propagates_memoryerror():
    """A MemoryError collecting a section must escape, not become {"error": ...}."""
    core1 = _core1()

    with pytest.raises(MemoryError):
        core1._collect_system_information_full(_ExhaustedSystemInformation())


def test_collect_system_information_still_reports_ordinary_section_errors():
    """A failed section still yields an error entry and the loop continues."""
    core1 = _core1()

    class _FlakySystemInformation:
        def get_network(self):
            raise RuntimeError("flaky")

        def get_memory(self):
            return {"ok": True}

        def get_runtime(self):
            return {"ok": True}

    result = core1._collect_system_information_full(_FlakySystemInformation())
    assert "error" in result["network"]
    assert result["memory"] == {"ok": True}


# --- Core 0 -----------------------------------------------------------------
#
# The MicroPython stand-ins below must be installed inside the test (not at
# collection time): later-collected modules import the real wifi/mqtt/time
# modules at collection, and mocked entries in sys.modules would shadow them.
# The pattern mirrors tests/test_core0_recovery.py.


class _FakeTime:
    def __init__(self):
        self.now_ms = 0

    def ticks_ms(self):
        return self.now_ms

    def ticks_diff(self, now, prev):
        return now - prev

    def ticks_add(self, base, delta):
        return base + delta

    def sleep_ms(self, ms):
        self.now_ms += ms

    def __getattr__(self, name):
        return getattr(_real_time, name)


_FAKE_TIME = _FakeTime()


def _make_core0():
    """Build a fresh Core0 with faked wifi/mqtt/LED and a controllable clock."""
    sys.modules["time"] = _FAKE_TIME
    sys.modules["machine"] = MagicMock()
    debug_mock = MagicMock()
    debug_mock.DEBUG = False
    sys.modules["debug"] = debug_mock
    sys.modules["wifi"] = MagicMock()
    sys.modules["mqtt"] = MagicMock()
    importlib.reload(importlib.import_module("uptime"))
    core0_mod = importlib.import_module("core0")
    importlib.reload(core0_mod)

    config = json.loads((ROOT / "config.json").read_text())
    core0_config, _core1_config, _bus_config = split_config(config)

    instance = core0_mod.Core0(
        _MockInterCore(),
        core0_config,
        {"wifi_ssid": "test-ssid", "wifi_password": "test-password"},
        "test-runtime",
        0,
        MagicMock(),
    )
    return core0_mod, instance


def test_core0_command_response_propagates_memoryerror(monkeypatch):
    """A MemoryError serializing a command response must escape.

    Before the fix the handler was ``except (MessageTooLargeError, Exception)``
    -- effectively ``except Exception`` -- so a MemoryError was swallowed and
    the response silently discarded as if serialization had merely failed, and
    the caller's ``except MemoryError: raise`` clause (e.g. _perform_reboot)
    could never fire.
    """
    core0_mod, instance = _make_core0()

    def _exhaust(*args, **kwargs):
        raise MemoryError

    monkeypatch.setattr(core0_mod, "serialize_and_validate_message", _exhaust)

    with pytest.raises(MemoryError):
        instance._publish_core0_command_response("req-1", "reboot", True)


def test_core0_command_response_still_swallows_ordinary_errors(monkeypatch):
    """A non-fatal serialization failure still reports and returns quietly."""
    core0_mod, instance = _make_core0()

    def _fail(*args, **kwargs):
        raise RuntimeError("malformed response")

    monkeypatch.setattr(core0_mod, "serialize_and_validate_message", _fail)

    # No exception, no publish: the response is discarded and reported.
    instance._publish_core0_command_response("req-1", "reboot", True)
    instance._mqtt.publish_qos1.assert_not_called()
