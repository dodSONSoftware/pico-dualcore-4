# test_led_manager.py - LEDManager behavior tests
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import importlib.util
import pathlib
import sys
import types


ROOT = pathlib.Path(__file__).resolve().parents[1]


class FakePin:
    OUT = 1

    def __init__(self, name, mode):
        self.name = name
        self.mode = mode
        self.state = 0

    def off(self):
        self.state = 0

    def value(self, value=None):
        if value is None:
            return self.state
        self.state = value


class FakeTimer:
    PERIODIC = 1

    def __init__(self, timer_id):
        self.timer_id = timer_id
        self.callback = None

    def init(self, period, mode, callback, hard=False):
        self.period = period
        self.mode = mode
        self.callback = callback
        self.hard = hard


def _load_led_manager():
    fake_machine = types.SimpleNamespace(Pin=FakePin, Timer=FakeTimer)
    previous = sys.modules.get("machine")
    sys.modules["machine"] = fake_machine
    try:
        spec = importlib.util.spec_from_file_location(
            "led_manager_under_test", ROOT / "led_manager.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if previous is None:
            del sys.modules["machine"]
        else:
            sys.modules["machine"] = previous


def test_connecting_flashes_until_cleared():
    module = _load_led_manager()
    manager = module.LEDManager()

    manager.set_connecting(True)
    manager._tick(None)
    assert manager._led.state == 1
    manager._tick(None)
    assert manager._led.state == 0

    manager.set_connecting(False)
    manager._tick(None)
    assert manager._led.state == 0


def test_telemetry_pulse_stays_on_for_one_second():
    module = _load_led_manager()
    manager = module.LEDManager()

    manager.telemetry_sent()
    for _ in range(module._TELEMETRY_PULSE_TICKS):
        manager._tick(None)
        assert manager._led.state == 1

    manager._tick(None)
    assert manager._led.state == 0


def test_new_telemetry_restarts_active_pulse():
    module = _load_led_manager()
    manager = module.LEDManager()

    manager.telemetry_sent()
    for _ in range(5):
        manager._tick(None)

    manager.telemetry_sent()
    for _ in range(module._TELEMETRY_PULSE_TICKS):
        manager._tick(None)
        assert manager._led.state == 1

    manager._tick(None)
    assert manager._led.state == 0
