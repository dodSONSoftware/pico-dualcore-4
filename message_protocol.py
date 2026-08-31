# message_protocol.py - Message value and timestamp helpers
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import math
import time


def format_utc_epoch_ms(epoch_ms):
    if epoch_ms is None:
        return None
    tm = time.gmtime(epoch_ms // 1000)
    return "{:04d}-{:02d}-{:02d}T{:02d}:{:02d}:{:02d}Z".format(
        tm[0], tm[1], tm[2], tm[3], tm[4], tm[5]
    )


def is_json_safe(value):
    if value is None or isinstance(value, (str, int, bool)):
        return True
    if isinstance(value, float):
        # Numeric finite check (NaN and +/-inf rejected, as required for
        # strict JSON) -- no per-value string conversion on the hot path.
        return math.isfinite(value)
    if isinstance(value, (list, tuple)):
        for item in value:
            if not is_json_safe(item):
                return False
        return True
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or not is_json_safe(item):
                return False
        return True
    return False
