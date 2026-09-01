# test_core0_heartbeat_watchdog.py - Tests for the Core 0 Core-1 liveness watchdog
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Host-side regression tests for the Core 0 stale-heartbeat watchdog.

Core 1 is the producer of the core_1_activity_ms stamp; a dead Core 1 cannot report itself (it no longer builds the health message that would carry core_1_inactive), so Core 0 is the independent consumer: an established stamp that goes stale beyond the timeout resets the MCU.

These tests pin the boundary semantics:

* no stamp (Core 1 not started) is never a reset trigger;
* a fresh stamp is never a reset trigger;
* a stamp exactly at the timeout IS a reset trigger (a live Core 1 cannot be that late, given its 5-second refresh deadline and 20 ms loop);
* a stale stamp resets, both when driven directly and inside the real run() loop;
* a stale stamp also resets *during* a failed network-recovery wait: the reset fires inside the first backoff sleep, before it even completes.
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


class LoopStop(Exception):
    """Raised by FakeTime to end an infinite Core 0 loop deterministically."""


class FakeTime:
    """Controllable stand-in for MicroPython's time module.

    sleep_ms advances the clock by exactly the requested amount so loop steps are deterministic; stop_after_ms (when set) ends the loop."""

    def __init__(self):
        self.now_ms = 0
        self.stop_after_ms = None

    def ticks_ms(self):
        return self.now_ms

    def ticks_diff(self, now, prev):
        return now - prev

    def ticks_add(self, base, delta):
        return base + delta

    def sleep_ms(self, ms):
        self.now_ms += ms
        if self.stop_after_ms is not None and self.now_ms >= self.stop_after_ms:
            raise LoopStop()

    def sleep(self, secs):
        self.sleep_ms(int(secs * 1000))

    def __getattr__(self, name):
        # Anything not explicitly faked falls through to the real time
        # module so host tooling keeps working.
        return getattr(_real_time, name)


_FAKE_TIME = FakeTime()


class _MachineReset(Exception):
    """Stand-in for machine.reset(): on hardware it never returns."""


class ResettingMachine:
    """Records reset() calls and, like real hardware, never returns."""

    def __init__(self):
        self.reset_calls = 0

    def reset(self):
        self.reset_calls += 1
        raise _MachineReset()


_DEBUG_MOCK = MagicMock()
_DEBUG_MOCK.DEBUG = False


def _install_mocks(machine):
    sys.modules["time"] = _FAKE_TIME
    sys.modules["machine"] = machine
    sys.modules["debug"] = _DEBUG_MOCK
    sys.modules["wifi"] = MagicMock()
    sys.modules["mqtt"] = MagicMock()


# NOTE: the MicroPython stand-ins above must NOT be installed at collection
# time (see tests/test_core0_recovery.py for the rationale); they are
# installed inside the fixture, which reloads core0 under them.
from config import split_config  # noqa: E402
from config_manager import ConfigManager  # noqa: E402
from intercore import InterCore  # noqa: E402


def _load_core0_config():
    config = json.loads((ROOT / "config.json").read_text())
    core0_config, _core1_config= split_config(config)
    return core0_config


class FakeLed:
    def set_connecting(self, value):
        pass

    def telemetry_sent(self):
        pass


class FakeWifi:
    """A steady-state connected Wi-Fi link; the watchdog is network-agnostic."""

    def __init__(self):
        self.connected = True

    def is_connected(self):
        return self.connected

    def connect(self):
        return True

    def snapshot(self, mqtt_connected):
        return {
            "ssid": "test-ssid",
            "ip_address": "192.168.1.100",
            "rssi": -50,
            "wifi_connect_count": 1,
        }


class FailingWifi:
    """A link that never comes up: connect() always fails, counting attempts."""

    def __init__(self):
        self.connect_calls = 0

    def is_connected(self):
        return False

    def connect(self):
        self.connect_calls += 1
        return False

    def snapshot(self, mqtt_connected):
        return {
            "ssid": "test-ssid",
            "ip_address": None,
            "rssi": None,
            "wifi_connect_count": 0,
        }


class FakeMqtt:
    """A steady-state connected MQTT session; enough surface for run()."""

    def __init__(self):
        self.connected = True

    def is_connected(self):
        return self.connected

    def connect(self):
        return True

    def mark_disconnected(self):
        self.connected = False

    def status(self):
        return {"connected": self.connected, "connect_count": 1, "disconnect_count": 0}

    def publish_qos1(self, topic, message):
        pass

    def check_msg(self):
        pass

    def ping_due(self):
        return False


class FailingMqtt:
    """A broker that never answers: connect() always fails, counting attempts."""

    def __init__(self):
        self.connect_calls = 0

    def is_connected(self):
        return False

    def connect(self):
        self.connect_calls += 1
        return False

    def mark_disconnected(self):
        pass

    def status(self):
        return {"connected": False, "connect_count": 0, "disconnect_count": 0}


@pytest.fixture
def env():
    """A fresh Core0 under faked machine/time/wifi/mqtt and a controllable clock."""
    _FAKE_TIME.now_ms = 0
    _FAKE_TIME.stop_after_ms = None
    machine = ResettingMachine()
    _install_mocks(machine)
    # core0 binds time from sys.modules at import time (as does uptime);
    # reload in dependency order so the fakes are authoritative.
    importlib.reload(importlib.import_module("uptime"))
    core0_mod = importlib.import_module("core0")
    importlib.reload(core0_mod)

    instance = core0_mod.Core0(
        InterCore(minimum_free_heap_bytes=65536),
        _load_core0_config(),
        {"wifi_ssid": "test-ssid", "wifi_password": "test-password"},
        "test-runtime",
        0,
        FakeLed(),
            ConfigManager("config.json"),
    )
    instance._wifi = FakeWifi()
    instance._mqtt = FakeMqtt()
    return {"instance": instance, "machine": machine, "core0_mod": core0_mod}


def test_no_reset_before_core1_has_started(env):
    """A mailbox with no stamp means Core 1 has not started: no reset, ever."""
    instance, machine = env["instance"], env["machine"]

    assert instance._intercore.state_mailboxes.get_core_1_activity_ms() is None
    # Even far past the timeout, absence of a stamp is not evidence of death.
    _FAKE_TIME.now_ms = env["core0_mod"]._CORE_1_HEARTBEAT_STALE_TIMEOUT_MS * 10
    instance._watch_core_1_heartbeat()

    assert machine.reset_calls == 0


def test_fresh_heartbeat_does_not_reset(env):
    """A just-registered stamp must not trip the watchdog."""
    instance, machine = env["instance"], env["machine"]
    instance._intercore.state_mailboxes.set_core_1_activity_ms(_FAKE_TIME.now_ms)

    instance._watch_core_1_heartbeat()

    assert machine.reset_calls == 0


def test_heartbeat_just_inside_timeout_does_not_reset(env):
    """One millisecond under the timeout is still 'possibly alive'."""
    instance, machine = env["instance"], env["machine"]
    timeout_ms = env["core0_mod"]._CORE_1_HEARTBEAT_STALE_TIMEOUT_MS
    instance._intercore.state_mailboxes.set_core_1_activity_ms(_FAKE_TIME.now_ms - (timeout_ms - 1))

    instance._watch_core_1_heartbeat()

    assert machine.reset_calls == 0


def test_heartbeat_exactly_at_timeout_resets(env):
    """At the timeout the stamp is stale: a live Core 1 cannot be that late."""
    instance, machine = env["instance"], env["machine"]
    timeout_ms = env["core0_mod"]._CORE_1_HEARTBEAT_STALE_TIMEOUT_MS
    instance._intercore.state_mailboxes.set_core_1_activity_ms(_FAKE_TIME.now_ms - timeout_ms)

    with pytest.raises(_MachineReset):
        instance._watch_core_1_heartbeat()

    assert machine.reset_calls == 1


def test_stale_heartbeat_resets(env):
    """A long-silent Core 1 resets the board, exactly once per check."""
    instance, machine = env["instance"], env["machine"]
    timeout_ms = env["core0_mod"]._CORE_1_HEARTBEAT_STALE_TIMEOUT_MS
    instance._intercore.state_mailboxes.set_core_1_activity_ms(
        _FAKE_TIME.now_ms - (timeout_ms + 60000)
    )

    with pytest.raises(_MachineReset):
        instance._watch_core_1_heartbeat()

    assert machine.reset_calls == 1


def test_run_loop_resets_on_stale_heartbeat(env):
    """The check is wired into run(): a stale stamp resets before any work."""
    instance, machine = env["instance"], env["machine"]
    timeout_ms = env["core0_mod"]._CORE_1_HEARTBEAT_STALE_TIMEOUT_MS
    instance._intercore.state_mailboxes.set_core_1_activity_ms(
        _FAKE_TIME.now_ms - (timeout_ms + 60000)
    )

    with pytest.raises(_MachineReset):
        instance.run()

    assert machine.reset_calls == 1


def test_zero_delay_sleeps_nothing_and_services_nothing(env):
    """A configured zero delay is an immediate retry: no 100 ms slice, no watchdog check.

    A stale stamp is armed so any servicing would reset; a zero-delay wait that services nothing leaves the machine untouched and the clock unadvanced."""
    instance, machine = env["instance"], env["machine"]
    timeout_ms = env["core0_mod"]._CORE_1_HEARTBEAT_STALE_TIMEOUT_MS
    instance._intercore.state_mailboxes.set_core_1_activity_ms(-timeout_ms)
    _FAKE_TIME.now_ms = 0

    instance._sleep_and_service(0)

    assert _FAKE_TIME.now_ms == 0
    assert machine.reset_calls == 0


def test_positive_delay_still_services_each_slice(env):
    """The zero-delay fix must not have removed servicing from real waits.

    With a stale stamp armed, the first 100 ms slice of any positive delay resets."""
    instance, machine = env["instance"], env["machine"]
    timeout_ms = env["core0_mod"]._CORE_1_HEARTBEAT_STALE_TIMEOUT_MS
    instance._intercore.state_mailboxes.set_core_1_activity_ms(-timeout_ms)

    with pytest.raises(_MachineReset):
        instance._sleep_and_service(1)

    assert machine.reset_calls == 1


def test_run_loop_stays_up_with_fresh_heartbeat(env):
    """A live Core 1 stamp keeps the run loop running across iterations."""
    instance, machine = env["instance"], env["machine"]
    instance._intercore.state_mailboxes.set_core_1_activity_ms(_FAKE_TIME.now_ms)
    # Three 10 ms loop iterations; the stamp ages by at most 30 ms, far
    # inside the timeout.
    _FAKE_TIME.stop_after_ms = 30

    with pytest.raises(LoopStop):
        instance.run()

    assert machine.reset_calls == 0


def test_stale_heartbeat_resets_during_network_recovery(env):
    """The watchdog fires during a recovery wait, not after recovery ends.

    The first backoff sleep (40 s, the configured maximum) outlasts the 30 s staleness timeout: a watchdog blind to these waits would only reset once the whole sequence finished. A serviced wait must reset at the 30 s mark -- inside the first backoff sleep, before it has even completed."""
    instance, machine = env["instance"], env["machine"]
    timeout_ms = env["core0_mod"]._CORE_1_HEARTBEAT_STALE_TIMEOUT_MS
    max_backoff_ms = env["instance"]._config["wifi_reconnect_delays_sec"][-1] * 1000

    # Core 1 has already started and registered its liveness stamp.
    instance._intercore.state_mailboxes.set_core_1_activity_ms(_FAKE_TIME.now_ms)

    # The network is down and stays down: recovery enters
    # establish_network() and its connect loops keep failing.
    failing_wifi = FailingWifi()
    failing_mqtt = FailingMqtt()
    instance._wifi = failing_wifi
    instance._mqtt = failing_mqtt

    with pytest.raises(_MachineReset):
        instance.run()

    assert machine.reset_calls == 1
    # The reset fired when the stamp hit the timeout, inside the first
    # backoff sleep -- the clock advanced past the timeout but not past
    # the end of the sleep that contained it.
    assert _FAKE_TIME.now_ms >= timeout_ms
    assert _FAKE_TIME.now_ms < max_backoff_ms
    # And we never got as far as the second recovery attempt.
    assert failing_wifi.connect_calls == 1
    assert failing_mqtt.connect_calls == 0
