# hardware.py - Runtime hardware detection for Pico W / Pico 2 W
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import os

# Canonical hardware type identifiers
HARDWARE_TYPE_PICO_W = "pico_w"
HARDWARE_TYPE_PICO_2_W = "pico_2_w"
HARDWARE_TYPE_UNKNOWN = "unknown"

# Board-specific minimum free-heap reserves (bytes)
PICO_W_MIN_FREE_HEAP_BYTES = 64 * 1024   # 65,536 bytes
PICO_2_W_MIN_FREE_HEAP_BYTES = 128 * 1024  # 131,072 bytes

# Machine string patterns for supported boards
# These are the actual strings returned by os.uname().machine on supported boards
_PICO_W_MACHINE_PATTERNS = (
    "Raspberry Pi Pico W with RP2040",
    "RPI_PICO_W with RP2040",
)

_PICO_2_W_MACHINE_PATTERNS = (
    "Raspberry Pi Pico 2 W with RP2350",
    "RPI_PICO2_W with RP2350",
)


def classify_machine(machine_name):
    """Classify a machine string into {hardware_type, minimum_free_heap_bytes} -- the single source of truth for the mapping."""
    if machine_name in _PICO_W_MACHINE_PATTERNS:
        return {
            "hardware_type": HARDWARE_TYPE_PICO_W,
            "minimum_free_heap_bytes": PICO_W_MIN_FREE_HEAP_BYTES,
        }
    if machine_name in _PICO_2_W_MACHINE_PATTERNS:
        return {
            "hardware_type": HARDWARE_TYPE_PICO_2_W,
            "minimum_free_heap_bytes": PICO_2_W_MIN_FREE_HEAP_BYTES,
        }
    return {
        "hardware_type": HARDWARE_TYPE_UNKNOWN,
        "minimum_free_heap_bytes": None,
    }


def detect_hardware():
    """Detect the hardware type at runtime; returns {hardware_type, machine, minimum_free_heap_bytes}. Raises RuntimeError on unsupported hardware."""
    try:
        machine_name = os.uname().machine
    except MemoryError:
        raise
    except Exception:
        raise RuntimeError("Unable to read machine identifier")

    # Classify via the shared policy, then fail startup on unknown hardware.
    result = classify_machine(machine_name)
    if result["hardware_type"] == HARDWARE_TYPE_UNKNOWN:
        raise RuntimeError("Unsupported hardware: {}".format(machine_name))

    return {
        "hardware_type": result["hardware_type"],
        "machine": machine_name,
        "minimum_free_heap_bytes": result["minimum_free_heap_bytes"],
    }


def is_supported_hardware():
    """Check if the current hardware is supported (Pico W or Pico 2 W)."""
    try:
        detect_hardware()
        return True
    except RuntimeError:
        return False


def get_minimum_free_heap(hardware_type):
    """Get the minimum free-heap reserve (bytes) for a canonical hardware type. Raises ValueError if unrecognized."""
    if hardware_type == HARDWARE_TYPE_PICO_W:
        return PICO_W_MIN_FREE_HEAP_BYTES
    elif hardware_type == HARDWARE_TYPE_PICO_2_W:
        return PICO_2_W_MIN_FREE_HEAP_BYTES
    else:
        raise ValueError("Unknown hardware type: {}".format(hardware_type))
