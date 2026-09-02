# debug.py - Debug switch
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

DEBUG = False

# Temporary validation gate for the heap-reserve queue instrumentation in
# intercore.py: one [DEBUG] line per meaningful queue event (admit, reject,
# evict, memory-pressure entry, gc.collect() before/after, backlog drained).
# Production default is False; enable it only during heap-reserve queue
# validation, then remove it together with the instrumentation.
DEBUG_QUEUE_MEMORY = False
