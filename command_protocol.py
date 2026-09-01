# command_protocol.py - Command protocol constants and validation helpers
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Command protocol constants and pure validation helpers (the Core 0 command boundary).

The firmware owns one strict command protocol boundary before any command
executes. This module holds only the protocol constants and the pure helpers
that validate against them -- no classes, no state, no generic schema
framework. Firmware policy (the command-ID debounce cache, response queues,
command dispatch) stays in the owning core modules.
"""

COMMAND_REBOOT = "reboot"
COMMAND_GET_DETAILS = "get-details"
COMMAND_READ_CONFIG = "read-config"
COMMAND_WRITE_CONFIG = "write-config"

MAX_COMMAND_ID_LENGTH = 128
MAX_COMMAND_LENGTH = 32
MAX_TARGET_LENGTH = 128

# The configured source is spliced into every Core 0 outbound envelope, so it
# is a protocol-scale identity (matched against a bounded target), not an
# open-ended string: a multi-kilobyte identity would push even a tiny envelope
# past the outbound wire ceiling.
MAX_SOURCE_LENGTH = 64

BROADCAST_TARGET = "*"

# A version-3 command has exactly these top-level fields; any other key
# violates the command contract (an unknown field).
COMMAND_ENVELOPE_KEYS = frozenset((
    "message_type",
    "message_schema_version",
    "target",
    "command_id",
    "command",
    "payload",
))

# Command ownership: Core 0-owned commands are executed on Core 0 (reboot,
# read-config / write-config with the configuration manager); Core 1-owned
# commands are dispatched to Core 1 as validated bounded events. Only
# get-details crosses; the configuration hot-reload handshake (the
# config-update event for read_loop_sec / health_interval_sec) is internal
# control traffic, not an external command.
CORE0_OWNED_COMMANDS = (
    COMMAND_REBOOT,
    COMMAND_READ_CONFIG,
    COMMAND_WRITE_CONFIG,
)
CORE1_OWNED_COMMANDS = (COMMAND_GET_DETAILS,)
SUPPORTED_COMMANDS = CORE0_OWNED_COMMANDS + CORE1_OWNED_COMMANDS

# Broadcast policy: every supported command accepts the * broadcast target
# except write-config, which must never apply fleet-wide.
BROADCAST_EXCLUDED_COMMANDS = frozenset((COMMAND_WRITE_CONFIG,))


def is_supported_command(command):
    """True when command is one of the supported command names."""
    return command in SUPPORTED_COMMANDS


def is_core1_owned_command(command):
    """True when command execution belongs to Core 1 (a dispatched bounded event)."""
    return command in CORE1_OWNED_COMMANDS


def is_broadcast_allowed(command):
    """True when the * broadcast target is valid for this command."""
    return command not in BROADCAST_EXCLUDED_COMMANDS


def is_bounded_command_id(value):
    """A command_id is a non-empty string within the protocol length bound."""
    return isinstance(value, str) and 0 < len(value) <= MAX_COMMAND_ID_LENGTH


def is_bounded_command(value):
    """A command name is a non-empty string within the protocol length bound."""
    return isinstance(value, str) and 0 < len(value) <= MAX_COMMAND_LENGTH


def is_bounded_target(value):
    """A target is a non-empty string within the protocol length bound, before any matching."""
    return isinstance(value, str) and 0 < len(value) <= MAX_TARGET_LENGTH


def unknown_field_names(keys, allowed):
    """The keys outside the allowed set, sorted alphabetically (empty when every key is known)."""
    return sorted(key for key in keys if key not in allowed)
