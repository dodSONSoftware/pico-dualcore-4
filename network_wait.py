# network_wait.py - Core 0's sliced network waits
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import time


def sleep_sliced(delay_sec, service):
    """Sleep delay_sec in 100 ms slices, calling service() before each
    slice. Slicing is the invariant: Core 0's long network waits (Wi-Fi
    backoffs, the MQTT retry backoff, the startup-verification retry delay)
    must keep the service hook firing -- the Core 1 heartbeat watchdog and
    the hardware watchdog feed -- so a stale heartbeat or a wedged Core 0
    surfaces during the wait, not after it. A zero or negative delay sleeps
    no slice and services nothing: zero is a configured immediate retry, and
    a slice floor would make it wait anyway."""
    if delay_sec <= 0:
        return

    for _ in range(int(delay_sec * 10)):
        service()
        time.sleep_ms(100)
