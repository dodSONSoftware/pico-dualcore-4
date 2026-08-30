# conftest.py - Host-side shims for MicroPython-only APIs
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""
MicroPython exposes ``gc.mem_free()``/``gc.mem_alloc()``; CPython (the host that
runs these tests) does not. Provide a healthy default so admission, health, and
memory-reporting paths that read the free heap run on the host.

The default is well above the largest board reserve (Pico 2 W, 128 KiB), so the
memory-pressure path never triggers unless a test explicitly lowers the free
heap. Tests that exercise pressure / controlled-GC behavior override
``gc.mem_free`` (and ``gc.collect``) -- via monkeypatch or direct reassignment
-- to model reclaim-on-collect and eviction-frees-bytes.
"""

import gc

_HOST_FREE_HEAP = 1024 * 1024

if not hasattr(gc, "mem_free"):
    gc.mem_free = lambda: _HOST_FREE_HEAP
if not hasattr(gc, "mem_alloc"):
    gc.mem_alloc = lambda: 0
