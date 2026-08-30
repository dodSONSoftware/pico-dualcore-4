# test_boot_reason.py - Reset-cause to boot-reason mapping
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import pathlib
import sys
from unittest.mock import MagicMock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

sys.modules.setdefault("machine", MagicMock())

import hardware  # noqa: E402
import observability as obs  # noqa: E402


def test_reset_cause_to_boot_reason_mapping():
    """The documented mapping, exactly:

    power_on_reset   -> power_on
    watchdog_reset   -> watchdog_recovery
    soft_reset       -> soft_reset
    hard_reset       -> unknown
    deep_sleep_reset -> unknown
    unknown          -> unknown
    """
    assert hardware.derive_boot_reason(hardware.RESET_CAUSE_POWER_ON) == "power_on"
    assert hardware.derive_boot_reason(hardware.RESET_CAUSE_WATCHDOG) == "watchdog_recovery"
    assert hardware.derive_boot_reason(hardware.RESET_CAUSE_SOFT) == "soft_reset"
    assert hardware.derive_boot_reason(hardware.RESET_CAUSE_HARD) == "unknown"
    assert hardware.derive_boot_reason(hardware.RESET_CAUSE_DEEP_SLEEP) == "unknown"
    assert hardware.derive_boot_reason(hardware.RESET_CAUSE_UNKNOWN) == "unknown"


def test_unrecognized_reset_cause_degrades_to_unknown():
    assert hardware.derive_boot_reason(None) == "unknown"
    assert hardware.derive_boot_reason("something_else") == "unknown"
    assert hardware.derive_boot_reason("explicit_reboot") == "unknown"


def test_boot_reason_values_are_the_observability_vocabulary():
    """The mapping returns the observability constants, not a second copy of
    the strings."""
    values = {
        hardware.derive_boot_reason(cause)
        for cause in (
            hardware.RESET_CAUSE_POWER_ON,
            hardware.RESET_CAUSE_WATCHDOG,
            hardware.RESET_CAUSE_SOFT,
            hardware.RESET_CAUSE_HARD,
            hardware.RESET_CAUSE_DEEP_SLEEP,
            hardware.RESET_CAUSE_UNKNOWN,
        )
    }
    assert values == {
        obs.BOOT_REASON_POWER_ON,
        obs.BOOT_REASON_WATCHDOG_RECOVERY,
        obs.BOOT_REASON_SOFT_RESET,
        obs.BOOT_REASON_UNKNOWN,
    }


def test_no_persisted_explicit_reboot_boot_reason():
    """A soft reset from a reboot command is only "explicit" with persisted
    evidence; the firmware writes no such evidence, so no boot reason named
    after it may exist."""
    boot_reason_values = {
        obs.BOOT_REASON_POWER_ON,
        obs.BOOT_REASON_WATCHDOG_RECOVERY,
        obs.BOOT_REASON_SOFT_RESET,
        obs.BOOT_REASON_UNKNOWN,
    }
    assert "explicit_reboot" not in boot_reason_values
    # The reboot command still has its reason-code face for command logs.
    assert obs.REASON_EXPLICIT_REBOOT_COMMAND == "explicit_reboot_command"


def test_boot_reason_is_read_only_derivation_not_recomputed():
    """derive_boot_reason is a pure mapping over the captured reset cause --
    no re-reading of machine state (the reset cause is captured once by
    main and rides the hardware snapshot)."""
    first = hardware.derive_boot_reason(hardware.RESET_CAUSE_POWER_ON)
    second = hardware.derive_boot_reason(hardware.RESET_CAUSE_POWER_ON)
    assert first == second == "power_on"
    # Calling it with an unknown cause never fabricates an evidence value.
    assert hardware.derive_boot_reason(hardware.RESET_CAUSE_UNKNOWN) == "unknown"
