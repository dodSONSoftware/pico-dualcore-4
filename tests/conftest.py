# conftest.py - Host-side test bootstrap
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Shared host-test shims for the MicroPython-only gc heap API.

CPython has no gc.mem_free()/gc.mem_alloc(), so install defaults before any production code path (queue admission, health payload, the DEBUG_QUEUE_MEMORY instrumentation) measures the heap. Tests that need a specific heap state override gc.mem_free themselves and restore it."""

import gc

if not hasattr(gc, "mem_free"):
    gc.mem_free = lambda: 256 * 1024

if not hasattr(gc, "mem_alloc"):
    gc.mem_alloc = lambda: 0
