#!/usr/bin/env bash
# _deploy_to_pico.sh - Build, flash, provision, deploy, and monitor Pico firmware
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

set -Eeuo pipefail

readonly SCRIPT_NAME="${0##*/}"
readonly REPO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly DEFAULT_RELEASE_DIR="$(dirname "$REPO_DIR")/sensor-releases"
readonly PICO_W_FIRMWARE_FILE="/home/worker/Documents/code/pico-bin/pico-w/RPI_PICO_W-20260406-v1.28.0.uf2"
readonly PICO_2_W_FIRMWARE_FILE="/home/worker/Documents/code/pico-bin/pico-2-w/RPI_PICO2_W-20260406-v1.28.0.uf2"

DEVICE="auto"
RELEASE_DIR="${RELEASE_DIR:-$DEFAULT_RELEASE_DIR}"
TEMP_DIR=""
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

cleanup() {
    if [[ -n "$TEMP_DIR" && -d "$TEMP_DIR" ]]; then
        rm -rf -- "$TEMP_DIR"
    fi
}
trap cleanup EXIT

usage() {
    cat <<USAGE
${SCRIPT_NAME} builds and deploys the sensor firmware to a Raspberry Pi Pico W
or Pico 2 W. The target board is mandatory and determines the default
MicroPython UF2 image and the full-flash erase range.

Usage:
  ${SCRIPT_NAME} <1|2> [options] [firmware.uf2]

Targets:
  1                     Deploy to a Raspberry Pi Pico W
  2                     Deploy to a Raspberry Pi Pico 2 W

Options:
  -d, --device DEVICE   mpremote device selector. Default: auto
                        Examples: /dev/ttyACM0, id:SERIAL, auto
  -r, --release-dir DIR Release output directory.
                        Default: ${DEFAULT_RELEASE_DIR}
  -h, --help            Show this help.

The script:
  1. Requires a clean Git working tree.
  2. Builds and verifies a release artifact with release.py.
  3. Erases the selected Pico's flash while it is in BOOTSEL mode.
  4. Flashes and verifies the selected MicroPython UF2 image.
  5. Provisions config.json and config-secrets.json.
  6. Deploys the application and package directories with mpremote.
  7. Soft-resets the device and starts main.main().

Default firmware images:
  Pico W:   ${PICO_W_FIRMWARE_FILE}
  Pico 2 W: ${PICO_2_W_FIRMWARE_FILE}

WARNING: Deployment permanently erases the selected device's entire flash,
including the MicroPython filesystem and all files stored on it.

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

release_query() {
    local action="$1"

    PYTHONDONTWRITEBYTECODE=1 python3 - "$REPO_DIR/release.py" "$action" <<'PY'
import importlib.util
import pathlib
import sys

release_path = pathlib.Path(sys.argv[1]).resolve()
action = sys.argv[2]

# release.py imports version.py from its own directory. Ensure that directory is
# importable even when this deployment script is launched from elsewhere.
sys.path.insert(0, str(release_path.parent))

spec = importlib.util.spec_from_file_location("sensor_release", release_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

if action == "root-files":
    for name in sorted(module.REQUIRED_FILES):
        if name.endswith(".py") and "/" not in name:
            print(name)
elif action == "package-files":
    for name in sorted(module.REQUIRED_PACKAGES):
        if name.endswith(".py"):
            print(name)
else:
    raise SystemExit("Unknown release metadata action: {}".format(action))
PY
}

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
            (($# >= 2)) || fail "--device requires a value"
            DEVICE="$2"
            shift 2
            ;;
        -r|--release-dir)
            (($# >= 2)) || fail "--release-dir requires a value"
            RELEASE_DIR="$2"
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

for command in git python3 tar mpremote picotool realpath sudo; do
    command -v "$command" >/dev/null 2>&1 || fail "Required command not found: $command"
done

[[ -f "$UF2_FILE" ]] || fail "UF2 file not found: $UF2_FILE"
[[ -r "$UF2_FILE" ]] || fail "UF2 file is not readable: $UF2_FILE"

case "${UF2_FILE,,}" in
    *.uf2) ;;
    *) fail "Firmware file must have a .uf2 extension: $UF2_FILE" ;;
esac

[[ -f "$REPO_DIR/release.py" ]] || fail "release.py not found in $REPO_DIR"
[[ -f "$REPO_DIR/version.py" ]] || fail "version.py not found in $REPO_DIR"
[[ -f "$REPO_DIR/config.json" ]] || fail "Provisioning requires $REPO_DIR/config.json"
[[ -f "$REPO_DIR/config-secrets.json" ]] || fail "Provisioning requires $REPO_DIR/config-secrets.json"

# -----------------------------------------------------------------------------
# Build and verify the release before modifying the device.
# -----------------------------------------------------------------------------

GIT_ROOT="$(git -C "$REPO_DIR" rev-parse --show-toplevel 2>/dev/null)" \
    || fail "$REPO_DIR is not inside a Git repository"
GIT_ROOT="$(cd "$GIT_ROOT" && pwd -P)"

[[ "$GIT_ROOT" == "$REPO_DIR" ]] \
    || fail "Expected repository root $REPO_DIR, but Git reports $GIT_ROOT"

GIT_STATUS="$(git -C "$REPO_DIR" status --porcelain --untracked-files=normal)"
if [[ -n "$GIT_STATUS" ]]; then
    printf '%s\n' "$GIT_STATUS" >&2
    fail "Git working tree is not clean. Commit/stash source changes before releasing."
fi

COMMIT_HASH="$(git -C "$REPO_DIR" rev-parse HEAD)"
log "Target: $TARGET_NAME"
log "Source commit: $COMMIT_HASH"
log "Provisioning mode: config.json and config-secrets.json WILL be overwritten on the Pico."

mkdir -p -- "$RELEASE_DIR"
RELEASE_DIR="$(cd "$RELEASE_DIR" && pwd -P)"

TEMP_DIR="$(mktemp -d)"
RELEASE_LOG="$TEMP_DIR/release.log"

log "Running release.py"
(
    cd "$REPO_DIR"
    python3 ./release.py "$RELEASE_DIR"
) | tee "$RELEASE_LOG"

ARTIFACT="$(sed -n 's/^[[:space:]]*Artifact:[[:space:]]*//p' "$RELEASE_LOG" | tail -n 1)"
MANIFEST="$(sed -n 's/^[[:space:]]*Manifest:[[:space:]]*//p' "$RELEASE_LOG" | tail -n 1)"
[[ -n "$ARTIFACT" ]] || fail "Could not determine release artifact path from release.py output"

if [[ "$ARTIFACT" != /* ]]; then
    ARTIFACT="$REPO_DIR/$ARTIFACT"
fi
ARTIFACT="$(realpath "$ARTIFACT")"
[[ -f "$ARTIFACT" ]] || fail "Release artifact not found: $ARTIFACT"

if [[ -n "$MANIFEST" && "$MANIFEST" != /* ]]; then
    MANIFEST="$REPO_DIR/$MANIFEST"
fi

POST_RELEASE_STATUS="$(git -C "$REPO_DIR" status --porcelain --untracked-files=normal)"
if [[ -n "$POST_RELEASE_STATUS" ]]; then
    printf '%s\n' "$POST_RELEASE_STATUS" >&2
    fail "release.py left the source repository dirty; refusing deployment"
fi

CURRENT_COMMIT="$(git -C "$REPO_DIR" rev-parse HEAD)"
[[ "$CURRENT_COMMIT" == "$COMMIT_HASH" ]] \
    || fail "Git HEAD changed during release generation"

log "Release artifact: $ARTIFACT"

while IFS= read -r member; do
    case "$member" in
        /*|../*|*/../*|*/..)
            fail "Unsafe path in release artifact: $member"
            ;;
    esac

    case "$member" in
        config-secrets.json|*/config-secrets.json|.env|*/.env|.env.*|*/.env.*|secret*.json|*/secret*.json|*.secret|*/*.secret)
            rm -f -- "$ARTIFACT"
            [[ -n "$MANIFEST" ]] && rm -f -- "$MANIFEST" 2>/dev/null || true
            fail "Sensitive file found in release artifact: $member"
            ;;
    esac
done < <(tar -tzf "$ARTIFACT")

STAGING_DIR="$TEMP_DIR/staging"
mkdir -p -- "$STAGING_DIR"
tar -xzf "$ARTIFACT" -C "$STAGING_DIR"

mapfile -t ROOT_FILES < <(release_query root-files)
mapfile -t PACKAGE_DIR_FILES < <(release_query package-files)

((${#ROOT_FILES[@]} > 0)) \
    || fail "Could not determine root application files from release.py"
((${#PACKAGE_DIR_FILES[@]} > 0)) \
    || fail "Could not determine package files from release.py"

ROOT_FILE_PATHS=()
MAIN_SELECTED=false
for filename in "${ROOT_FILES[@]}"; do
    staged="$STAGING_DIR/$filename"
    [[ -f "$staged" ]] \
        || fail "Required application file missing from release artifact: $filename"
    ROOT_FILE_PATHS+=("$staged")
    [[ "$filename" == "main.py" ]] && MAIN_SELECTED=true
done

$MAIN_SELECTED || fail "release.py did not select main.py for deployment"

# Track only the first path component for package deployment. This avoids
# recursively copying the same package through both a parent and subpackage.
declare -A PACKAGE_ROOTS
for filename in "${PACKAGE_DIR_FILES[@]}"; do
    staged="$STAGING_DIR/$filename"
    if [[ -f "$staged" ]]; then
        package_root="${filename%%/*}"
        PACKAGE_ROOTS["$package_root"]=1
    else
        fail "Required package file missing from release artifact: $filename"
    fi
done

# Provision local configuration rather than the release artifact's config.json.
ROOT_FILE_PATHS+=("$REPO_DIR/config.json" "$REPO_DIR/config-secrets.json")
((${#ROOT_FILE_PATHS[@]} > 0)) || fail "No root-level files selected for deployment"

PACKAGE_ROOT_PATHS=()
for dir in "${!PACKAGE_ROOTS[@]}"; do
    staged_dir="$STAGING_DIR/$dir"
    if [[ -d "$staged_dir" ]]; then
        PACKAGE_ROOT_PATHS+=("$staged_dir")
    fi
done

# -----------------------------------------------------------------------------
# Flash MicroPython.
# -----------------------------------------------------------------------------

log "Inspecting UF2 file"
picotool info "$UF2_FILE"

printf '\nEnter the administrator password if prompted.\n'
sudo -v

log "Checking for a $TARGET_NAME in BOOTSEL mode"
printf 'Connect exactly one %s in BOOTSEL mode before continuing.\n' "$TARGET_NAME"
sudo picotool info >/dev/null

log "Erasing entire flash for $TARGET_NAME"
sudo picotool erase --range 0x10000000 "$FLASH_ERASE_END"

log "Writing firmware"
sudo picotool load --ignore-partitions "$UF2_FILE"

log "Verifying flash against UF2"
sudo picotool verify "$UF2_FILE"

log "Rebooting into application mode"
sudo picotool reboot -a

wait_for_micropython 15

# -----------------------------------------------------------------------------
# Deploy immediately after the Pico becomes available, then stay attached.
# -----------------------------------------------------------------------------

for dir in "${PACKAGE_ROOT_PATHS[@]}"; do
    [[ -d "$dir" ]] || fail "Package root missing from release artifact: ${dir#$STAGING_DIR/}"
done

log "Deploying ${#ROOT_FILE_PATHS[@]} root files and ${#PACKAGE_ROOT_PATHS[@]} package roots with mpremote to '$DEVICE'"
log "Use Ctrl-C to stop device output."
printf '\n================================================================\n\n'

mpremote connect "$DEVICE" \
    fs cp "${ROOT_FILE_PATHS[@]}" : +

if ((${#PACKAGE_ROOT_PATHS[@]} > 0)); then
    for dir in "${PACKAGE_ROOT_PATHS[@]}"; do
        mpremote connect "$DEVICE" \
            fs cp -r "$dir" : +
    done
fi

mpremote connect "$DEVICE" \
    fs ls : +

mpremote connect "$DEVICE" \
    soft-reset \
    exec "import main; main.main()"
