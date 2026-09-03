# debug.py - Debug switch
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

DEBUG = False

# Validation gate for the heap-reserve queue instrumentation in intercore.py:
# one [DEBUG] line per meaningful queue event (admit, reject, evict,
# memory-pressure entry, gc.collect() before/after, backlog drained).
DEBUG_QUEUE_MEMORY = False
