# uptime.py - Accumulated boot-relative uptime
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT
"""Accumulated boot-relative uptime.

MicroPython's ``time.ticks_diff`` is only guaranteed correct when the two
samples are less than half a tick period apart. Firmware therefore never
diffs the original boot tick against the current tick: each core accumulates
deltas between consecutive samples into a running total. Every individual
``ticks_diff`` compares two recent ticks, so uptime stays correct and
monotonically increasing across a tick-counter wrap on a long-running
device.
"""

import time


def create_uptime_state(boot_ticks_ms):
    """Create the state used to accumulate uptime from boot_ticks_ms."""
    return {
        "last_ticks_ms": boot_ticks_ms,
        "accumulated_ms": 0,
    }


def current_uptime_ms(state):
    """Return uptime since boot and advance the state by one recent delta."""
    now_ms = time.ticks_ms()
    state["accumulated_ms"] += time.ticks_diff(now_ms, state["last_ticks_ms"])
    state["last_ticks_ms"] = now_ms
    return state["accumulated_ms"]
