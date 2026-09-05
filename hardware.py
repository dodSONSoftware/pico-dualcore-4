# hardware.py - Runtime hardware detection for Pico W / Pico 2 W
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import os

HARDWARE_TYPE_PICO_W = "pico_w"
HARDWARE_TYPE_PICO_2_W = "pico_2_w"
HARDWARE_TYPE_UNKNOWN = "unknown"

# Board-specific free-heap thresholds (bytes). Two distinct concepts: the
# PREFERRED reserve marks the start of memory-pressure handling (GC,
# increased willingness to reclaim low-retention queue entries — not a
# rejection wall), and the MINIMUM is the hard survival floor admission must
# protect (below it, expendable incoming traffic may be rejected). Real Pico
# W operation transiently approaches or dips below 64 KiB while constructing
# and serializing legitimate messages, so the two must not be one value.
PICO_W_PREFERRED_FREE_HEAP_BYTES = 64 * 1024   # 65,536 bytes
PICO_W_MIN_FREE_HEAP_BYTES = 48 * 1024         # 49,152 bytes
# Pico 2 W keeps its own policy: the same 16 KiB pressure band above its
# (unchanged) 128 KiB hard floor.
PICO_2_W_PREFERRED_FREE_HEAP_BYTES = 144 * 1024  # 147,456 bytes
PICO_2_W_MIN_FREE_HEAP_BYTES = 128 * 1024        # 131,072 bytes

# Machine strings returned by os.uname().machine on supported boards
_PICO_W_MACHINE_PATTERNS = (
    "Raspberry Pi Pico W with RP2040",
    "RPI_PICO_W with RP2040",
)

_PICO_2_W_MACHINE_PATTERNS = (
    "Raspberry Pi Pico 2 W with RP2350",
    "RPI_PICO2_W with RP2350",
)


def classify_machine(machine_name):
    """Classify a machine string into {hardware_type, preferred_free_heap_bytes, minimum_free_heap_bytes} -- the single source of truth for the mapping."""
    if machine_name in _PICO_W_MACHINE_PATTERNS:
        return {
            "hardware_type": HARDWARE_TYPE_PICO_W,
            "preferred_free_heap_bytes": PICO_W_PREFERRED_FREE_HEAP_BYTES,
            "minimum_free_heap_bytes": PICO_W_MIN_FREE_HEAP_BYTES,
        }
    if machine_name in _PICO_2_W_MACHINE_PATTERNS:
        return {
            "hardware_type": HARDWARE_TYPE_PICO_2_W,
            "preferred_free_heap_bytes": PICO_2_W_PREFERRED_FREE_HEAP_BYTES,
            "minimum_free_heap_bytes": PICO_2_W_MIN_FREE_HEAP_BYTES,
        }
    return {
        "hardware_type": HARDWARE_TYPE_UNKNOWN,
        "preferred_free_heap_bytes": None,
        "minimum_free_heap_bytes": None,
    }


def detect_hardware():
    """Detect the hardware type at runtime; returns {hardware_type, machine, preferred_free_heap_bytes, minimum_free_heap_bytes}. Raises RuntimeError on unsupported hardware."""
    try:
        machine_name = os.uname().machine
    except MemoryError:
        raise
    except Exception:
        raise RuntimeError("Unable to read machine identifier")

    result = classify_machine(machine_name)
    if result["hardware_type"] == HARDWARE_TYPE_UNKNOWN:
        raise RuntimeError("Unsupported hardware: {}".format(machine_name))

    return {
        "hardware_type": result["hardware_type"],
        "machine": machine_name,
        "preferred_free_heap_bytes": result["preferred_free_heap_bytes"],
        "minimum_free_heap_bytes": result["minimum_free_heap_bytes"],
    }
