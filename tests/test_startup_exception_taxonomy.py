# test_startup_exception_taxonomy.py - MQTT exception taxonomy on the Core 0 startup and reboot paths
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Host-side regression tests for the MQTT exception taxonomy on Core 0's startup verification and reboot paths.

mqtt.py establishes the rule: MQTT_TRANSPORT_ERRORS (OSError, MQTTException) are transport/protocol failures -- link conditions. Startup, they fail the verification pass (the pass re-establishes the network and retries); on the reboot path, the reboot stays pending for a later pass. Anything else escaping the client (a bug in message handling, callback code, or state handling) is a programming failure and must reach main.py's controlled-reset boundary -- not be reclassified as a network/startup failure and retried into the same deterministic fault forever.

Before the fix the startup helpers undid that rule with broad except-Exception wrappers: _utc_send_request() and _utc_wait_response() converted programming failures into failed UTC attempts, _perform_network_probe() into failed probes (the retry loop then reconnected into the same fault), _drain_startup_mqtt_work() swallowed them while the un-removed head log stayed queued -- an infinite retry of the same failing operation -- and _perform_reboot() converted them into "reboot remains pending".

Covers:
- core0.Core0._utc_send_request: a programming failure from publish escapes (a transport failure still rolls back the armed request ID)
- core0.Core0._utc_wait_response: a programming failure from the response cycle (the callback path) escapes; a transport failure still fails the wait cleanly
- core0.Core0._perform_network_probe: a programming failure escapes; a transport failure is a failed probe
- core0.Core0._drain_startup_mqtt_work: a programming failure escapes with the head log still queued (the state a broad wrapper would have retried forever); a transport failure is still owned by _service_pending_connection_log, which holds the log for a later pass
- core0.Core0._perform_reboot: a programming failure from the acknowledgement publish escapes (no reset); a transport failure still leaves the reboot pending"""

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
from config_manager import ConfigManager  # noqa: E402


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


class _MockOutboundQueue:
    def has_in_flight(self):
        return False


class _MockInterCore:
    def __init__(self):
        self.outbound_queue = _MockOutboundQueue()


@pytest.fixture
def make_core0():
    """Build a fresh Core0 with faked wifi/mqtt/LED and a controllable clock.

    The MicroPython stand-ins are installed inside the fixture (not at collection time): later-collected modules import the real wifi/mqtt/time modules at collection, and mocked entries in sys.modules would shadow them. The pattern mirrors tests/test_memory_error_propagation.py."""
    def _make():
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
        _FAKE_TIME.now_ms = 0

        config = json.loads((ROOT / "tests" / "fixtures" / "config.json").read_text())
        core0_config, _core1_config = split_config(config)

        instance = core0_mod.Core0(
            _MockInterCore(),
            core0_config,
            {"wifi_ssid": "test-ssid", "wifi_password": "test-password"},
            "test-runtime",
            0,
            MagicMock(),
            ConfigManager(str(ROOT / "tests" / "fixtures" / "config.json")),
        )
        return core0_mod, instance

    return _make


# --- UTC request/response cycle ---------------------------------------------


def test_startup_utc_publish_programming_failure_propagates(make_core0):
    """A programming failure inside the UTC request publish must escape to the
    top-level recovery boundary, not be reported as a failed attempt that the
    retry loop would repeat into the same deterministic fault."""
    core0_mod, instance = make_core0()
    instance._mqtt.publish_qos1.side_effect = RuntimeError("state handling bug")

    with pytest.raises(RuntimeError, match="state handling bug"):
        instance._utc_send_request()


def test_startup_utc_publish_transport_failure_still_rolls_back(make_core0):
    """A transport failure is a link condition: the armed request ID is rolled
    back and the attempt simply fails (unchanged behavior)."""
    core0_mod, instance = make_core0()
    instance._mqtt.publish_qos1.side_effect = OSError("PUBACK timeout")

    instance._utc_send_request()  # must not raise

    assert instance._pending_utc_request_id is None
    assert instance._utc_snapshot is None
    assert instance._utc_should_send_request() is True


def test_startup_utc_callback_failure_propagates(make_core0):
    """A programming failure in the response cycle -- the inbound callback is
    the realistic source -- must escape, not end the wait as a failed sync.

    A failed sync would arm the self-healing retry: re-establish, resend,
    re-deliver the same frame into the same callback bug, forever."""
    core0_mod, instance = make_core0()
    instance._utc_send_request()
    assert instance._pending_utc_request_id is not None
    instance._mqtt.check_msg.side_effect = RuntimeError("callback bug")

    with pytest.raises(RuntimeError, match="callback bug"):
        instance._utc_wait_response()


def test_startup_utc_wait_transport_failure_still_fails_the_wait(make_core0):
    """A transport failure mid-wait fails the attempt cleanly: the pending
    request is cleared so the pass can re-establish and retry (unchanged)."""
    core0_mod, instance = make_core0()
    instance._utc_send_request()
    instance._mqtt.check_msg.side_effect = OSError("socket reset")

    instance._utc_wait_response()  # must not raise

    assert instance._pending_utc_request_id is None
    assert instance._utc_snapshot is None


# --- Network probe ------------------------------------------------------------


def test_startup_probe_programming_failure_propagates(make_core0):
    """A programming failure inside the probe publish must escape, not be
    reported as a failed probe the retry loop would repeat forever."""
    core0_mod, instance = make_core0()
    instance._mqtt.get_next_packet_id.return_value = 42
    instance._mqtt.publish_qos1_with_packet_id.side_effect = RuntimeError("client state bug")

    with pytest.raises(RuntimeError, match="client state bug"):
        instance._perform_network_probe()


def test_startup_probe_transport_failure_is_a_failed_probe(make_core0):
    """A transport failure (here: the not-connected guard's OSError) is a link
    condition: the probe reports False so the pass re-establishes and retries
    (unchanged)."""
    core0_mod, instance = make_core0()
    instance._mqtt.get_next_packet_id.return_value = 42
    instance._mqtt.publish_qos1_with_packet_id.side_effect = OSError("MQTT is not connected")

    assert instance._perform_network_probe() is False


# --- Startup drain -------------------------------------------------------------


def _connection_log():
    return {
        "message_type": "log",
        "payload": {
            "level": "info",
            "message": "Connected to Wi-Fi",
            "event": "wifi_connection_established",
            "module": "wifi",
            "data": {"ssid": "test-ssid"},
        },
    }


def test_startup_drain_programming_failure_propagates(make_core0, monkeypatch):
    """A programming failure servicing the head log must escape.

    The head stays queued: with the old broad wrapper the failure was
    swallowed, the head remained, and the drain loop re-attempted the same
    failing operation indefinitely with no forward progress."""
    core0_mod, instance = make_core0()

    def _fail(*args, **kwargs):
        raise RuntimeError("message handling bug")

    # Patch where the call resolves: core0's module-level import (the
    # suite's convention, matching the core1 serializer patches).
    monkeypatch.setattr(core0_mod, "serialize_and_validate_message", _fail)

    instance._pending_connection_logs.append(_connection_log())

    with pytest.raises(RuntimeError, match="message handling bug"):
        instance._drain_startup_mqtt_work()

    # The head was never removed: a broad wrapper would have retried it forever.
    assert len(instance._pending_connection_logs) == 1


def test_startup_drain_transport_failure_is_still_owned_by_the_service(make_core0):
    """A transport failure is handled by _service_pending_connection_log()
    itself (the log stays pending for a later pass): it never escapes, which
    is what lets the drain keep no broad wrapper of its own."""
    core0_mod, instance = make_core0()
    instance._pending_connection_logs.append(_connection_log())
    instance._mqtt.publish_qos1.side_effect = OSError("PUBACK timeout")

    instance._service_pending_connection_log()  # must not raise

    assert len(instance._pending_connection_logs) == 1  # held for a later pass


# --- Reboot acknowledgement ----------------------------------------------------


def test_reboot_publish_programming_failure_propagates(make_core0):
    """A programming failure publishing the reboot acknowledgement must escape
    run() to the top-level recovery boundary -- not be reported as a failed
    publish with the reboot left pending forever for the same deterministic
    fault. No board reset happens on the way out."""
    core0_mod, instance = make_core0()
    machine = core0_mod.machine
    instance._pending_reboot = {
        "command_id": "req-1",
        "command": "reboot",
        "targeted": False,
    }
    instance._mqtt.publish_qos1.side_effect = RuntimeError("state handling bug")

    with pytest.raises(RuntimeError, match="state handling bug"):
        instance._perform_reboot()

    machine.reset.assert_not_called()


def test_reboot_publish_transport_failure_still_keeps_reboot_pending(make_core0):
    """A transport failure is a link condition: the acknowledgement was never
    out the door, so the reboot remains pending for a later pass (unchanged).
    Never a reset without the published answer."""
    core0_mod, instance = make_core0()
    machine = core0_mod.machine
    instance._pending_reboot = {
        "command_id": "req-1",
        "command": "reboot",
        "targeted": False,
    }
    instance._mqtt.publish_qos1.side_effect = OSError("MQTT is not connected")

    assert instance._perform_reboot() is False

    assert instance._pending_reboot is not None  # still pending
    machine.reset.assert_not_called()
    instance._mqtt.publish_qos1.assert_called_once()
