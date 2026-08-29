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

    def _next_output(self):
        """Compute and advance one LED step under the lock; return None if contended."""
        if not self._lock.acquire(False):
            return None

        try:
            connecting = self._connecting
            if connecting:
                output = 1 if self._connecting_on else 0
                self._connecting_on = not self._connecting_on
            elif self._telemetry_ticks_remaining > 0:
                output = 1
            else:
                output = 0

            if self._telemetry_ticks_remaining > 0:
                self._telemetry_ticks_remaining -= 1

            return output
        finally:
            self._lock.release()

    def _tick(self, timer):
        """Timer callback; the only code path that writes the physical LED."""
        output = self._next_output()
        if output is None:
            return

        if output != self._output:
            self._output = output
            self._led.value(output)
