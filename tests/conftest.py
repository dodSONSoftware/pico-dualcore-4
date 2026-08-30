# conftest.py - Host-side test bootstrap
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Shared host-test shims for the MicroPython-only gc heap API.

The firmware targets MicroPython, where gc.mem_free() reports the current
free heap. CPython has no such API, so install a generous default before any
production code path (queue admission, health payload) measures the heap.
Tests that need a specific heap state override gc.mem_free themselves (for
example test_health.HealthEnv or the intercore FakeHeap) and restore it.
"""

import gc

if not hasattr(gc, "mem_free"):
    gc.mem_free = lambda: 256 * 1024
