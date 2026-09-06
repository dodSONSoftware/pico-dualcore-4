# test_core0_wdt.py - Tests for the Core 0 hardware watchdog
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Host-side regression tests for the Core 0 hardware watchdog (machine.WDT).

The watchdog is the one supervision layer for Core 0 itself: Core 0's
heartbeat watchdog recovers a dead Core 1, and the hardware watchdog
recovers a Core 0 that is alive but no longer making progress. The contract
pinned here:

* arming happens exactly once, at the end of start() — after the
  deliberately unbounded startup verification, never before;
* the budget invariant: WDT_TIMEOUT_MS sits under the RP2 hardware maximum
  (8388 ms) and above every single bounded wait Core 0 performs
  (the config.py response-timeout bound and mqtt.py's PINGRESP bound),
  so a stalled link fails on its own timeout before the watchdog can fire;
* feeding comes only from Core 0's own execution: every run-loop pass and
  every 100 ms slice of a long wait (via _service_wait), nothing else;
* a build without machine.WDT degrades to no hardware supervision (a
  warning, not a reset loop), and the run loop keeps running;
* before start() the watchdog is neither armed nor fed.
"""

import importlib
import json
import pathlib
import sys
import time as _real_time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

ROOT = pathlib.Path(__file__).resolve().parents[1]


class LoopStop(Exception):
    """Raised by FakeTime to end an infinite Core 0 loop deterministically."""


class FakeTime:
    """Controllable stand-in for MicroPython's time module.

    sleep_ms advances the clock by exactly the requested amount so loop
    steps are deterministic; stop_after_ms (when set) ends the loop."""

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


class _Wdt:
    """Records arming and feeds, like machine.WDT on the rp2 port."""

    def __init__(self, timeout):
        self.timeout = timeout
        self.feed_calls = 0

    def feed(self):
        self.feed_calls += 1


class WDTMachine:
    """machine stand-in with WDT, reset, and unique_id."""

    def __init__(self):
        self.wdt = None
        self.reset_calls = 0

    def reset(self):
        self.reset_calls += 1
        raise _MachineReset()

    def unique_id(self):
        return b"\x00\x01\x02\x03\x04\x05\x06\x07"

    def WDT(self, timeout=0):
        self.wdt = _Wdt(timeout)
        return self.wdt


class BareMachine:
    """machine stand-in without WDT: a build that lacks the capability.

    Must degrade (no hardware supervision) instead of a deterministic reset
    loop — making absence fatal would reboot into the same missing
    attribute forever."""

    def __init__(self):
        self.reset_calls = 0

    def reset(self):
        self.reset_calls += 1
        raise _MachineReset()

    def unique_id(self):
        return b"\x00\x01\x02\x03\x04\x05\x06\x07"


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


class FakeMqtt:
    """Scripts successful connect/probe and echoes a UTC info_response, the
    way the broker would, so the startup UTC wait terminates."""

    def __init__(self, core0_instance):
        self.core0 = core0_instance
        self.connected = True
        self._last_info_request = None
        self._utc_deliver = True

    def is_connected(self):
        return self.connected

    def connect(self):
        self.connected = True
        return True

    def mark_disconnected(self):
        self.connected = False

    def status(self):
        return {"connected": self.connected, "connect_count": 1, "disconnect_count": 0}

    def get_next_packet_id(self):
        return 1

    def publish_qos1(self, topic, message, splice_fragment=None):
        doc = json.loads(message)
        if doc.get("message_type") == "info_request":
            self._last_info_request = doc

    def publish_qos1_with_packet_id(self, topic, message, packet_id, timeout_ms=None):
        return True

    def check_msg(self):
        if self._utc_deliver and self._last_info_request is not None:
            self._utc_deliver = False
            request = self._last_info_request
            utc_epoch_ms = 1750000000000
            response = {
                "message_type": "info_response",
                "message_schema_version": MESSAGE_SCHEMA_VERSION,
                "source": "server",
                "target": self.core0._config["source"],
                "request_type": "utc_time",
                "request_id": request["request_id"],
                "payload": {
                    "timestamp": format_utc_epoch_ms(utc_epoch_ms),
                    "utc_epoch_ms": utc_epoch_ms,
                },
            }
            self.core0._on_mqtt_message(
                self.core0._config["mqtt_topic_info_response"],
                json.dumps(response),
            )

    def ping_due(self):
        return False


# NOTE: the MicroPython stand-ins must NOT be installed at collection time
# (see tests/test_core0_recovery.py for the rationale); they are installed
# inside the fixture, which reloads core0 under them.
from config import MAX_MQTT_BROKER_RESPONSE_TIMEOUT_SEC, split_config  # noqa: E402
from config_manager import ConfigManager  # noqa: E402
from intercore import InterCore  # noqa: E402
from message_protocol import format_utc_epoch_ms  # noqa: E402
from version import MESSAGE_SCHEMA_VERSION  # noqa: E402


def _load_core0_config():
    config = json.loads((ROOT / "tests" / "fixtures" / "config.json").read_text())
    core0_config, _core1_config = split_config(config)
    return core0_config


def _install_mocks(machine):
    """Fake machine/time; the REAL mqtt module (this file's invariant test
    needs its _MAX_PINGRESP_WAIT_SEC value, and an earlier test file may have
    leaked a MagicMock into sys.modules — pop it so the import is genuine);
    a MagicMock wifi (its `import network` has no host equivalent)."""
    from unittest.mock import MagicMock

    sys.modules["time"] = _FAKE_TIME
    sys.modules["machine"] = machine
    sys.modules.pop("mqtt", None)
    _mqtt_mod = importlib.import_module("mqtt")
    sys.modules["wifi"] = MagicMock()
    return _mqtt_mod


def _build_core0(machine):
    """A fresh Core0 under the installed mocks and the controllable clock;
    the real InterCore bus (an empty queue keeps run()'s publish path a
    no-op)."""
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
        ConfigManager(str(ROOT / "tests" / "fixtures" / "config.json")),
    )
    instance._wifi = FakeWifi()
    instance._mqtt = FakeMqtt(instance)
    return instance, core0_mod


@pytest.fixture
def env():
    """A fresh Core0 under a WDT-capable fake machine and a controllable
    clock, plus the real mqtt module for the budget invariant."""
    _FAKE_TIME.now_ms = 0
    _FAKE_TIME.stop_after_ms = None
    machine = WDTMachine()
    mqtt_mod = _install_mocks(machine)
    instance, core0_mod = _build_core0(machine)
    return {
        "instance": instance,
        "machine": machine,
        "core0_mod": core0_mod,
        "mqtt_mod": mqtt_mod,
    }


def test_wdt_budget_invariant(env):
    """The watchdog budget must exceed every single bounded wait Core 0
    performs (one CONNACK/SUBACK or PUBACK exchange under the config bound,
    one PINGRESP wait under mqtt.py's bound) and stay under the RP2 hardware
    maximum — otherwise a slow broker, not a wedged Core 0, causes the
    reset, or the arm itself is rejected by the port."""
    wdt_timeout_ms = env["core0_mod"].WDT_TIMEOUT_MS
    assert wdt_timeout_ms <= 8388  # ports/rp2 machine_wdt.c WDT_TIMEOUT_MAX
    assert MAX_MQTT_BROKER_RESPONSE_TIMEOUT_SEC * 1000 < wdt_timeout_ms
    assert env["mqtt_mod"]._MAX_PINGRESP_WAIT_SEC * 1000 < wdt_timeout_ms


def test_watchdog_armed_exactly_once_at_end_of_startup(env):
    """Arming is deferred to the end of start(): the connect/verification
    loops above are deliberately unbounded, and a watchdog would reset them
    into the same waits."""
    instance, machine = env["instance"], env["machine"]

    assert instance._wdt is None
    instance.start()

    assert machine.wdt is not None
    assert machine.wdt.timeout == env["core0_mod"].WDT_TIMEOUT_MS
    assert instance._wdt is machine.wdt
    # Startup feeds nothing: the watchdog is armed, not serviced, on this
    # path (every startup wait predates arming).
    assert machine.wdt.feed_calls == 0


def test_run_loop_feeds_watchdog_on_every_pass(env):
    """Every run-loop pass feeds the watchdog: a healthy Core 0 never lets
    the 8 s budget lapse (each pass ends in a 10 ms sleep, so a fixed
    advance yields a fixed number of passes)."""
    instance, machine = env["instance"], env["machine"]
    instance.start()
    machine.wdt.feed_calls = 0  # count steady-state feeds only

    # Relative to the clock position start() left (the 5 s stabilization
    # sleep): each pass ends in a 10 ms sleep, so 250 ms is exactly 25 passes.
    _FAKE_TIME.stop_after_ms = _FAKE_TIME.now_ms + 250
    with pytest.raises(LoopStop):
        instance.run()

    # Exactly one feed per 10 ms pass.
    assert machine.wdt.feed_calls == 25
    assert machine.reset_calls == 0


def test_sliced_wait_feeds_watchdog(env):
    """Each 100 ms slice of a long wait services Core 0, which now feeds the
    watchdog: reconnect backoffs and observation windows (up to the
    MAX_RECONNECT_DELAY_SEC bound) can never lapse the budget."""
    instance, machine = env["instance"], env["machine"]
    instance._wdt = machine.WDT(timeout=env["core0_mod"].WDT_TIMEOUT_MS)

    instance._sleep_and_service(0.3)

    assert machine.wdt.feed_calls == 3
    assert machine.reset_calls == 0


def test_watchdog_degrades_when_machine_lacks_wdt():
    """A build without machine.WDT keeps the firmware running without
    hardware supervision (warning, not a reset loop)."""
    _FAKE_TIME.now_ms = 0
    _FAKE_TIME.stop_after_ms = None
    machine = BareMachine()
    _install_mocks(machine)
    instance, _core0_mod = _build_core0(machine)

    instance.start()

    assert instance._wdt is None
    _FAKE_TIME.stop_after_ms = _FAKE_TIME.now_ms + 50
    with pytest.raises(LoopStop):
        instance.run()
    assert machine.reset_calls == 0


def test_no_arming_or_feeding_before_start(env):
    """Before start() returns the watchdog is neither armed nor fed: the
    startup loops are unbounded by design, and feeding an un-armed watchdog
    would be a no-op at best."""
    instance, machine = env["instance"], env["machine"]

    _FAKE_TIME.stop_after_ms = 50
    with pytest.raises(LoopStop):
        instance.run()

    assert machine.wdt is None
    assert machine.reset_calls == 0
