# hardware.py - Runtime hardware detection for Pico W / Pico 2 W
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import os

from observability import (
    BOOT_REASON_POWER_ON,
    BOOT_REASON_SOFT_RESET,
    BOOT_REASON_UNKNOWN,
    BOOT_REASON_WATCHDOG_RECOVERY,
)

# Canonical hardware type identifiers
HARDWARE_TYPE_PICO_W = "pico_w"
HARDWARE_TYPE_PICO_2_W = "pico_2_w"
HARDWARE_TYPE_UNKNOWN = "unknown"

# Board-specific minimum free-heap reserves (bytes)
PICO_W_MIN_FREE_HEAP_BYTES = 64 * 1024   # 65,536 bytes
PICO_2_W_MIN_FREE_HEAP_BYTES = 128 * 1024  # 131,072 bytes

# Canonical reset-cause values (stable firmware-facing strings; the raw
# machine.reset_cause() integer is a MicroPython implementation detail and
# is never published)
RESET_CAUSE_POWER_ON = "power_on_reset"
RESET_CAUSE_HARD = "hard_reset"
RESET_CAUSE_WATCHDOG = "watchdog_reset"
RESET_CAUSE_DEEP_SLEEP = "deep_sleep_reset"
RESET_CAUSE_SOFT = "soft_reset"
RESET_CAUSE_UNKNOWN = "unknown"

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
    """
    Classify a machine string into its canonical board result.

    This is the single source of truth for mapping a machine string to a
    hardware type and the board-specific minimum free-heap reserve.

    Returns a dict with:
        hardware_type: canonical type ("pico_w", "pico_2_w", or "unknown")
        minimum_free_heap_bytes: board reserve, or None when unknown
    """
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


def read_last_reset_cause():
    """
    Read the most recent reset cause and translate it to a canonical string.

    The reset cause describes how the current boot began. Call this once at
    startup (main) and preserve the value for the lifetime of the runtime;
    do not re-read it from health generation or system-information reads.

    Returns one of the RESET_CAUSE_* constants, or RESET_CAUSE_UNKNOWN when
    the cause cannot be determined. A read failure is an ordinary diagnostic
    failure and never prevents boot; a MemoryError still propagates.
    """
    try:
        import machine
        cause = machine.reset_cause()
    except MemoryError:
        raise
    except Exception:
        return RESET_CAUSE_UNKNOWN

    try:
        if cause == machine.PWRON_RESET:
            return RESET_CAUSE_POWER_ON
        if cause == machine.HARD_RESET:
            return RESET_CAUSE_HARD
        if cause == machine.WDT_RESET:
            return RESET_CAUSE_WATCHDOG
        if cause == machine.DEEPSLEEP_RESET:
            return RESET_CAUSE_DEEP_SLEEP
        if cause == machine.SOFT_RESET:
            return RESET_CAUSE_SOFT
    except MemoryError:
        raise
    except Exception:
        return RESET_CAUSE_UNKNOWN

    return RESET_CAUSE_UNKNOWN


def derive_boot_reason(last_reset_cause):
    """
    Map a canonical reset cause to its boot reason.

    The reset cause (RESET_CAUSE_*) is the raw fact; the boot reason is the
    semantic face reported to operators. This is the single mapping; the
    boot-reason vocabulary itself lives in observability.py. Read once at
    startup (main), never persisted, never re-derived elsewhere.

    Mapping:
        power_on_reset   -> power_on
        watchdog_reset   -> watchdog_recovery
        soft_reset       -> soft_reset
        hard_reset       -> unknown
        deep_sleep_reset -> unknown
        unknown          -> unknown

    No "explicit_reboot" value exists: a soft reset from a reboot command is
    only reported as such when persisted evidence exists, and the firmware
    writes no such evidence, so it degrades to the same value as any soft
    reset.

    Args:
        last_reset_cause: a RESET_CAUSE_* constant (or any value)

    Returns:
        a BOOT_REASON_* constant (unknown for anything unrecognized)
    """
    if last_reset_cause == RESET_CAUSE_POWER_ON:
        return BOOT_REASON_POWER_ON
    if last_reset_cause == RESET_CAUSE_WATCHDOG:
        return BOOT_REASON_WATCHDOG_RECOVERY
    if last_reset_cause == RESET_CAUSE_SOFT:
        return BOOT_REASON_SOFT_RESET
    return BOOT_REASON_UNKNOWN
