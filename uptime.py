# uptime.py - Accumulated boot-relative uptime
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT
"""Accumulated boot-relative uptime.

time.ticks_diff is only guaranteed correct between ticks less than half a tick period apart, so each core accumulates deltas between consecutive samples; every individual diff compares two recent ticks, which stays correct across a tick-counter wrap."""

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
