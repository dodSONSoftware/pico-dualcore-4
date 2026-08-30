# version.py - Firmware and schema versions
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

FIRMWARE_VERSION = "0.5.0"
CONFIG_SCHEMA_VERSION = 9
MESSAGE_SCHEMA_VERSION = 3

# Build identity: the short commit SHA this build was released from,
# generated into build_info.py by release.py at release time (host-side git;
# the Pico never runs git). The dev fallback "unknown" keeps startup working
# from a plain source tree; a MemoryError still propagates.
try:
    from build_info import FIRMWARE_BUILD_COMMIT
except MemoryError:
    raise
except Exception:
    FIRMWARE_BUILD_COMMIT = "unknown"
