#!/usr/bin/env bash
# _erase_pico.sh - Erase, flash, verify, and prepare a Pico for deployment
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

set -Eeuo pipefail

readonly SCRIPT_NAME="${0##*/}"
readonly PICO_W_FIRMWARE_FILE="/home/worker/Documents/code/pico-bin/pico-w/RPI_PICO_W-20260406-v1.28.0.uf2"
readonly PICO_2_W_FIRMWARE_FILE="/home/worker/Documents/code/pico-bin/pico-2-w/RPI_PICO2_W-20260406-v1.28.0.uf2"

DEVICE="auto"
TARGET=""
TARGET_NAME=""
UF2_FILE=""
UF2_FILE_SET=false
FLASH_ERASE_END=""

log() {
    printf '\n==> %s\n' "$*"
}

fail() {
    printf '\nError: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<USAGE
${SCRIPT_NAME} completely erases a Raspberry Pi Pico W or Pico 2 W, installs
MicroPython from a UF2 image, verifies the flashed firmware, reboots the board,
and confirms that the MicroPython USB serial interface is available.

Usage:
  ${SCRIPT_NAME} <1|2> [options] [firmware.uf2]

Targets:
  1                     Prepare a Raspberry Pi Pico W
  2                     Prepare a Raspberry Pi Pico 2 W

Options:
  -d, --device DEVICE   mpremote device selector. Default: auto
                        Examples: /dev/ttyACM0, id:SERIAL, auto
  -h, --help            Show this help.

Default firmware images:
  Pico W:   ${PICO_W_FIRMWARE_FILE}
  Pico 2 W: ${PICO_2_W_FIRMWARE_FILE}

WARNING:
  This permanently erases the selected Pico's entire flash, including:

    - MicroPython firmware
    - MicroPython filesystem
    - main.py
    - boot.py
    - configuration files
    - application files
    - all other files stored on the device

The selected Pico must be connected in BOOTSEL mode before running this script.

After successful completion, the Pico will contain a clean MicroPython
installation and will be ready for application deployment with mpremote,
VS Code, Thonny, or another MicroPython deployment tool.

Examples:
  ${SCRIPT_NAME} 1
  ${SCRIPT_NAME} 2
  ${SCRIPT_NAME} 1 --device /dev/ttyACM0
  ${SCRIPT_NAME} 2 /path/to/RPI_PICO2_W-custom.uf2
USAGE
}

usage_error() {
    [[ $# -eq 0 ]] || printf 'Error: %s\n\n' "$*" >&2
    usage >&2
    exit 2
}

configure_target() {
    case "$1" in
        1)
            TARGET="1"
            TARGET_NAME="Raspberry Pi Pico W"
            UF2_FILE="$PICO_W_FIRMWARE_FILE"
            FLASH_ERASE_END="0x10200000"
            ;;
        2)
            TARGET="2"
            TARGET_NAME="Raspberry Pi Pico 2 W"
            UF2_FILE="$PICO_2_W_FIRMWARE_FILE"
            FLASH_ERASE_END="0x10400000"
            ;;
        *)
            usage_error "Target must be 1 (Pico W) or 2 (Pico 2 W)."
            ;;
    esac
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
# Parse arguments.
# -----------------------------------------------------------------------------

if (($# == 0)); then
    usage_error
fi

case "$1" in
    -h|--help|help)
        usage
        exit 0
        ;;
esac

configure_target "$1"
shift

while (($#)); do
    case "$1" in
        -d|--device)
            (($# >= 2)) || usage_error "--device requires a value"
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
            usage_error "Unknown option: $1"
            ;;
        *)
            if $UF2_FILE_SET; then
                usage_error "Only one firmware UF2 file may be specified"
            fi

            UF2_FILE="$1"
            UF2_FILE_SET=true
            shift
            ;;
    esac
done

if (($#)); then
    if $UF2_FILE_SET || (($# != 1)); then
        usage_error "Only one firmware UF2 file may be specified"
    fi

    UF2_FILE="$1"
fi

# -----------------------------------------------------------------------------
# Validate environment and firmware before touching the device.
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
# Inspect firmware and acquire administrator privileges.
# -----------------------------------------------------------------------------

log "Inspecting UF2 firmware"
picotool info "$UF2_FILE"

printf '\nEnter the administrator password if prompted.\n'
sudo -v

# -----------------------------------------------------------------------------
# Validate, erase, flash, verify, and reboot the selected Pico.
# -----------------------------------------------------------------------------

log "Checking for $TARGET_NAME in BOOTSEL mode"

if ! sudo picotool info >/dev/null 2>&1; then
    fail "No $TARGET_NAME detected in BOOTSEL mode"
fi

log "Erasing entire flash for $TARGET_NAME"
sudo picotool erase --range 0x10000000 "$FLASH_ERASE_END"

log "Writing MicroPython firmware"
sudo picotool load --ignore-partitions "$UF2_FILE"

log "Verifying flash against UF2"
sudo picotool verify "$UF2_FILE"

log "Rebooting $TARGET_NAME into MicroPython"
sudo picotool reboot -a

wait_for_micropython 15

# -----------------------------------------------------------------------------
# Complete.
# -----------------------------------------------------------------------------

log "$TARGET_NAME preparation complete"

printf '\n'
printf 'Target:   %s\n' "$TARGET_NAME"
printf 'Firmware: %s\n' "$UF2_FILE"
printf 'Device:   %s\n' "$DEVICE"
printf '\n'
printf 'The Pico now contains a clean MicroPython installation and is ready\n'
printf 'for application deployment.\n'
