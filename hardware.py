# hardware.py - Runtime hardware detection for Pico W / Pico 2 W
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import os

# Canonical hardware type identifiers
HARDWARE_TYPE_PICO_W = "pico_w"
HARDWARE_TYPE_PICO_2_W = "pico_2_w"

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


def detect_hardware():
    """
    Detect the hardware type at runtime.

    Returns a dict with:
        hardware_type: canonical hardware type ("pico_w" or "pico_2_w")
        machine: raw machine string from os.uname().machine
        minimum_free_heap_bytes: board-specific minimum free-heap reserve

    Raises:
        RuntimeError: if the hardware is not supported
    """
    try:
        machine_name = os.uname().machine
    except MemoryError:
        raise
    except Exception:
        raise RuntimeError("Unable to read machine identifier")

    # Classify the hardware based on the machine string
    if machine_name in _PICO_W_MACHINE_PATTERNS:
        return {
            "hardware_type": HARDWARE_TYPE_PICO_W,
            "machine": machine_name,
            "minimum_free_heap_bytes": PICO_W_MIN_FREE_HEAP_BYTES,
        }

    if machine_name in _PICO_2_W_MACHINE_PATTERNS:
        return {
            "hardware_type": HARDWARE_TYPE_PICO_2_W,
            "machine": machine_name,
            "minimum_free_heap_bytes": PICO_2_W_MIN_FREE_HEAP_BYTES,
        }

    # Unsupported hardware - fail startup explicitly
    raise RuntimeError("Unsupported hardware: {}".format(machine_name))


def is_supported_hardware():
    """
    Check if the current hardware is supported.

    Returns True if hardware is Pico W or Pico 2 W, False otherwise.
    """
    try:
        detect_hardware()
        return True
    except RuntimeError:
        return False


def get_minimum_free_heap(hardware_type):
    """
    Get the minimum free-heap reserve for a given hardware type.

    Args:
        hardware_type: canonical hardware type ("pico_w" or "pico_2_w")

    Returns:
        minimum free-heap reserve in bytes

    Raises:
        ValueError: if hardware_type is not recognized
    """
    if hardware_type == HARDWARE_TYPE_PICO_W:
        return PICO_W_MIN_FREE_HEAP_BYTES
    elif hardware_type == HARDWARE_TYPE_PICO_2_W:
        return PICO_2_W_MIN_FREE_HEAP_BYTES
    else:
        raise ValueError("Unknown hardware type: {}".format(hardware_type))
