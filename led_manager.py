# led_manager.py - Core 0 onboard LED manager
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import _thread
import machine


LED_TICK_MS = 50
TELEMETRY_PULSE_MS = 1000
_TELEMETRY_PULSE_TICKS = TELEMETRY_PULSE_MS // LED_TICK_MS


class LEDManager:
    """Own the onboard LED and provide non-blocking status indications."""

    def __init__(self):
        self._led = machine.Pin("LED", machine.Pin.OUT)
        self._lock = _thread.allocate_lock()
        self._connecting = False
        self._telemetry_ticks_remaining = 0
        self._connecting_on = False
        self._output = 0
        self._led.off()

        self._timer = machine.Timer(-1)
        self._timer.init(
            period=LED_TICK_MS,
            mode=machine.Timer.PERIODIC,
            callback=self._tick,
            hard=False,
        )

    def set_connecting(self, connecting):
        """Flash continuously while Wi-Fi/MQTT connectivity is being established."""
        with self._lock:
            connecting = bool(connecting)
            if connecting and not self._connecting:
                self._connecting_on = True
            self._connecting = connecting

    def telemetry_sent(self):
        """Request a one-second LED pulse after successful telemetry publication."""
        with self._lock:
            self._telemetry_ticks_remaining = _TELEMETRY_PULSE_TICKS

    def _snapshot_and_advance(self):
        if not self._lock.acquire(False):
            return None

        try:
            connecting = self._connecting
            connecting_on = self._connecting_on
            telemetry_active = self._telemetry_ticks_remaining > 0

            if connecting:
                self._connecting_on = not self._connecting_on

            if self._telemetry_ticks_remaining > 0:
                self._telemetry_ticks_remaining -= 1

            return connecting, connecting_on, telemetry_active
        finally:
            self._lock.release()

    def _tick(self, timer):
        """Timer callback; the only code path that writes the physical LED."""
        state = self._snapshot_and_advance()
        if state is None:
            return

        connecting, connecting_on, telemetry_active = state

        if connecting:
            output = 1 if connecting_on else 0
        elif telemetry_active:
            output = 1
        else:
            output = 0

        if output != self._output:
            self._output = output
            self._led.value(output)
