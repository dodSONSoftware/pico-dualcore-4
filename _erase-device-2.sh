#!/usr/bin/env bash

# erase-prepare-pico.sh - Erase, flash, verify, and prepare a Pico for deployment
#
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

set -Eeuo pipefail

readonly SCRIPT_NAME="${0##*/}"
readonly DEFAULT_FIRMWARE_FILE="/home/worker/Documents/code/pico-bin/pico-2-w/RPI_PICO2_W-20260406-v1.28.0.uf2"

DEVICE="auto"
UF2_FILE="$DEFAULT_FIRMWARE_FILE"
UF2_FILE_SET=false

# -----------------------------------------------------------------------------
# Acquire administrator privileges
# -----------------------------------------------------------------------------

printf '\nEnter the administrator password if prompted.\n'
sudo -v

# -----------------------------------------------------------------------------

log() {
    printf '\n==> %s\n' "$*"
}

fail() {
    printf '\nError: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<USAGE
Usage:
  ${SCRIPT_NAME} [options] [firmware.uf2]

Completely erases a Raspberry Pi Pico, installs MicroPython from a UF2 file,
verifies the flashed firmware, reboots the device, and confirms that the
MicroPython USB serial interface is available.

If no firmware file is specified, defaults to:
  ${DEFAULT_FIRMWARE_FILE}

Options:
  -d, --device DEVICE   mpremote device selector.
                        Default: auto
                        Examples:
                          /dev/ttyACM0
                          id:SERIAL
                          auto

  -h, --help            Show this help.

WARNING:
  This permanently erases the Pico's entire flash, including:

    - MicroPython firmware
    - MicroPython filesystem
    - main.py
    - boot.py
    - configuration files
    - application files
    - all other files stored on the device

The Pico must be connected in BOOTSEL mode before running this script.

After successful completion, the Pico will contain a clean MicroPython
installation and will be ready for application deployment with mpremote,
VS Code, Thonny, or another MicroPython deployment tool.
USAGE
}

wait_for_micropython() {
    local timeout_sec="${1:-15}"
    local deadline=$((SECONDS + timeout_sec))

    log "Waiting for MicroPython USB serial device"

    while (( SECONDS < deadline )); do
        if mpremote connect "$DEVICE" resume eval "0" >/dev/null 2>&1; then
            log "MicroPython USB serial device is ready"
            return 0
        fi

        sleep 1
    done

    printf '\nDetected serial devices:\n\n' >&2
    mpremote connect list >&2 || true

    fail "MicroPython USB serial device did not become ready within ${timeout_sec} seconds."
}

# -----------------------------------------------------------------------------
# Parse arguments
# -----------------------------------------------------------------------------

while (($#)); do
    case "$1" in
        -d|--device)
            (($# >= 2)) || fail "--device requires a value"
            DEVICE="$2"
            shift 2
            ;;

        -h|--help)
            usage
            exit 0
            ;;

        --)
            shift
            break
            ;;

        -*)
            fail "Unknown option: $1"
            ;;

        *)
            if $UF2_FILE_SET; then
                fail "Only one firmware UF2 file may be specified"
            fi

            UF2_FILE="$1"
            UF2_FILE_SET=true
            shift
            ;;
    esac
done

if (($#)); then
    if $UF2_FILE_SET || (($# != 1)); then
        fail "Only one firmware UF2 file may be specified"
    fi

    UF2_FILE="$1"
fi

# -----------------------------------------------------------------------------
# Validate environment
# -----------------------------------------------------------------------------

for command in mpremote picotool sudo; do
    command -v "$command" >/dev/null 2>&1 \
        || fail "Required command not found: $command"
done

[[ -f "$UF2_FILE" ]] \
    || fail "UF2 file not found: $UF2_FILE"

[[ -r "$UF2_FILE" ]] \
    || fail "UF2 file is not readable: $UF2_FILE"

case "${UF2_FILE,,}" in
    *.uf2)
        ;;
    *)
        fail "Firmware file must have a .uf2 extension: $UF2_FILE"
        ;;
esac

# -----------------------------------------------------------------------------
# Inspect firmware
# -----------------------------------------------------------------------------

log "Inspecting UF2 firmware"
picotool info "$UF2_FILE"

# -----------------------------------------------------------------------------
# Validate device
# -----------------------------------------------------------------------------

log "Checking for Pico in BOOTSEL mode"

if ! sudo picotool info >/dev/null 2>&1; then
    fail "No Pico detected in BOOTSEL mode"
fi

# -----------------------------------------------------------------------------
# Erase device
# -----------------------------------------------------------------------------

log "Erasing entire Pico flash"
sudo picotool erase --range 0x10000000 0x10400000

# -----------------------------------------------------------------------------
# Flash MicroPython
# -----------------------------------------------------------------------------

log "Writing MicroPython firmware"
sudo picotool load --ignore-partitions "$UF2_FILE"

# -----------------------------------------------------------------------------
# Verify firmware
# -----------------------------------------------------------------------------

log "Verifying flash against UF2"
sudo picotool verify "$UF2_FILE"

# -----------------------------------------------------------------------------
# Reboot device
# -----------------------------------------------------------------------------

log "Rebooting Pico into MicroPython"
sudo picotool reboot -a

# -----------------------------------------------------------------------------
# Confirm MicroPython
# -----------------------------------------------------------------------------

wait_for_micropython 15

# -----------------------------------------------------------------------------
# Complete
# -----------------------------------------------------------------------------

log "Pico preparation complete"

printf '\n'
printf 'Firmware: %s\n' "$UF2_FILE"
printf 'Device:   %s\n' "$DEVICE"
printf '\n'
printf 'The Pico now contains a clean MicroPython installation and is ready\n'
printf 'for application deployment.\n'
