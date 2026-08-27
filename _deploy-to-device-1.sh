#!/usr/bin/env bash
# flash-deploy-pico.sh - Build, flash, provision, deploy, and monitor Pico firmware
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

set -Eeuo pipefail

readonly SCRIPT_NAME="${0##*/}"
readonly REPO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly DEFAULT_FIRMWARE_FILE="/home/worker/Documents/code/pico-bin/pico-w/RPI_PICO_W-20260406-v1.28.0.uf2"
readonly DEFAULT_RELEASE_DIR="$(dirname "$REPO_DIR")/sensor-releases"

DEVICE="auto"
RELEASE_DIR="${RELEASE_DIR:-$DEFAULT_RELEASE_DIR}"
UF2_FILE="$DEFAULT_FIRMWARE_FILE"
UF2_FILE_SET=false
TEMP_DIR=""

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
Usage:
  ${SCRIPT_NAME} [options] [firmware.uf2]

Builds and verifies a clean application release, erases and flashes MicroPython
onto one Pico in BOOTSEL mode, verifies the UF2, provisions config.json and
config-secrets.json, deploys the application, and enters the MicroPython REPL.

If no firmware file is specified, defaults to:
  ${DEFAULT_FIRMWARE_FILE}

Options:
  -d, --device DEVICE   mpremote device selector. Default: auto
                        Examples: /dev/ttyACM0, id:SERIAL, auto
  -r, --release-dir DIR Release output directory.
                        Default: ${DEFAULT_RELEASE_DIR}
  -h, --help            Show this help.

WARNING: This permanently erases the device's entire flash, including the
MicroPython filesystem and all files stored on it. Local config.json and
config-secrets.json are then provisioned onto the device.

After deployment, the script enters the MicroPython REPL. At the >>> prompt,
run:
  import main

to start the application while keeping debug output attached to the terminal.
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

# Separate root-level files from package directories for proper deployment
mapfile -t ROOT_FILES < <(
    PYTHONDONTWRITEBYTECODE=1 python3 - "$REPO_DIR/release.py" <<'PY'
import importlib.util
import pathlib
import sys

release_path = pathlib.Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("sensor_release", release_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

# Root-level .py files only (no packages with /)
for filename in sorted(module.REQUIRED_FILES | module.OPTIONAL_FILES):
    if filename.endswith(".py") and "/" not in filename:
        print(filename)
PY
)

mapfile -t PACKAGE_DIR_FILES < <(
    PYTHONDONTWRITEBYTECODE=1 python3 - "$REPO_DIR/release.py" <<'PY'
import importlib.util
import pathlib
import sys

release_path = pathlib.Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("sensor_release", release_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

# Package files (containing /) - these will be copied with -r from their parent dirs
for filename in sorted(module.REQUIRED_PACKAGES):
    if filename.endswith(".py"):
        print(filename)
PY
)

((${#ROOT_FILES[@]} > 0 || ${#PACKAGE_DIR_FILES[@]} > 0)) || fail "release.py defines no deployable application files"

# Build root-level file list
ROOT_FILE_PATHS=()
for filename in "${ROOT_FILES[@]}"; do
    staged="$STAGING_DIR/$filename"
    if [[ -f "$staged" ]]; then
        ROOT_FILE_PATHS+=("$staged")
    else
        # Check if required
        if PYTHONDONTWRITEBYTECODE=1 python3 - "$REPO_DIR/release.py" "$filename" <<'PY'
import importlib.util
import pathlib
import sys

release_path = pathlib.Path(sys.argv[1])
filename = sys.argv[2]
spec = importlib.util.spec_from_file_location("sensor_release", release_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
raise SystemExit(0 if filename in module.REQUIRED_FILES else 1)
PY
        then
            fail "Required application file missing from release artifact: $filename"
        fi
    fi
done

# Track top-level package roots that need -r copy
# Extract only the first path component (e.g., "devices" from "devices/device.py")
# This prevents double-copying subpackages like "devices/system_information"
declare -A PACKAGE_ROOTS
for filename in "${PACKAGE_DIR_FILES[@]}"; do
    staged="$STAGING_DIR/$filename"
    if [[ -f "$staged" ]]; then
        # Extract only the first path component as the package root
        package_root="${filename%%/*}"
        PACKAGE_ROOTS["$package_root"]=1
    else
        # Check if required
        if PYTHONDONTWRITEBYTECODE=1 python3 - "$REPO_DIR/release.py" "$filename" <<'PY'
import importlib.util
import pathlib
import sys

release_path = pathlib.Path(sys.argv[1])
filename = sys.argv[2]
spec = importlib.util.spec_from_file_location("sensor_release", release_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
raise SystemExit(0 if filename in module.REQUIRED_PACKAGES else 1)
PY
        then
            fail "Required application file missing from release artifact: $filename"
        fi
    fi
done

# Add config files to root files
ROOT_FILE_PATHS+=("$REPO_DIR/config.json" "$REPO_DIR/config-secrets.json")
((${#ROOT_FILE_PATHS[@]} > 0)) || fail "No root-level files selected for deployment"

# Build package root list
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

log "Checking for a Pico in BOOTSEL mode"
printf 'Connect exactly one Pico in BOOTSEL mode before continuing.\n'
sudo picotool info >/dev/null

log "Erasing entire flash"
# sudo picotool erase -a
sudo picotool erase -r 0x10000000 0x10200000

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

# Verify all package roots exist before deployment
for dir in "${PACKAGE_ROOT_PATHS[@]}"; do
    [[ -d "$dir" ]] || fail "Package root missing from release artifact: ${dir#$STAGING_DIR/}"
done

log "Deploying ${#ROOT_FILE_PATHS[@]} root files and ${#PACKAGE_ROOT_PATHS[@]} package roots with mpremote to '$DEVICE'"
log "After deployment, run 'import main' at the >>> prompt to start the application."
log "Use Ctrl-C to stop device output."
printf '\n================================================================\n\n'

# Copy root-level files to remote root
mpremote connect "$DEVICE" \
    fs cp "${ROOT_FILE_PATHS[@]}" : +

# Recursively copy package roots to the remote root.
# mpremote preserves the local directory name.
# mpremote creates the package directory from the local directory name.
if ((${#PACKAGE_ROOT_PATHS[@]} > 0)); then
    for dir in "${PACKAGE_ROOT_PATHS[@]}"; do
        mpremote connect "$DEVICE" \
            fs cp -r "$dir" : +
    done
fi

# List remote filesystem to verify deployment
mpremote connect "$DEVICE" \
    fs ls : +

# Reset device before starting the application
mpremote connect "$DEVICE" \
    soft-reset \
    exec "import main; main.main()"
