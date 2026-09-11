# config_manager.py - Core 0 configuration persistence
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Core 0-owned configuration manager: persistence, file transactions,
and boot recovery.

config.py remains the single source of truth for schema validation and
per-core splitting. There is no live apply: every committed change is
pending a reboot (the running firmware keeps its boot values until then),
and ``reboot_required`` is a plain in-boot flag set by the first committed
change of the boot.
"""

import errno
import json
import os

from config import ConfigError, load_config, validate_config

CLASSIFICATION_UNCHANGED = "UNCHANGED"
CLASSIFICATION_REBOOT_REQUIRED = "REBOOT_REQUIRED"


def _device_changes(old_devices, new_devices):
    """Compact whole-device change entries (ADDED / REMOVED / MODIFIED),
    sorted by id; whole-device entries keep the summary small, and a
    difference no single id accounts for (ordering) is one list-level
    MODIFIED entry -- never the arrays themselves."""
    old_by_id = {device["id"]: device for device in old_devices}
    new_by_id = {device["id"]: device for device in new_devices}
    changes = []
    for device_id in sorted(set(old_by_id) | set(new_by_id)):
        old_device = old_by_id.get(device_id)
        new_device = new_by_id.get(device_id)
        if old_device is None:
            change_type = "ADDED"
            device_type = new_device["device_type"]
        elif new_device is None:
            change_type = "REMOVED"
            device_type = old_device["device_type"]
        elif old_device != new_device:
            change_type = "MODIFIED"
            device_type = new_device["device_type"]
        else:
            continue
        changes.append({
            "setting": "devices",
            "change_type": change_type,
            "device_id": device_id,
            "device_type": device_type,
        })
    if not changes and old_devices != new_devices:
        # The lists differ only in a way no id attributes (ordering).
        changes.append({
            "setting": "devices",
            "change_type": "MODIFIED",
        })
    return changes


def _changes_summary(persisted, candidate):
    """Describe what this write changed (PERSISTED-before vs candidate),
    deterministically sorted; config_schema_version is never reported (a
    different version is invalid, not a change)."""
    changes = []
    for key in sorted(persisted):
        if key == "config_schema_version":
            continue
        if key == "devices":
            changes.extend(_device_changes(persisted["devices"], candidate["devices"]))
            continue
        if persisted[key] != candidate[key]:
            changes.append({
                "setting": key,
                "original_value": persisted[key],
                "new_value": candidate[key],
            })
    return changes


class ConfigManager:

    def __init__(self, config_path="config.json"):
        self._config_path = config_path
        # Set by the first committed change of this boot; the running
        # firmware keeps its boot values until the reboot.
        self._reboot_required = False

    @property
    def reboot_required(self):
        return self._reboot_required

    def _tmp_path(self):
        return self._config_path + ".tmp"

    def _old_path(self):
        return self._config_path + ".old"

    @staticmethod
    def _path_exists(path):
        try:
            os.stat(path)
            return True
        except MemoryError:
            raise
        except OSError as err:
            # Only ENOENT means "absent". Any other OSError (a flash/LittleFS
            # I/O fault, corruption, ...) is a real storage failure that the
            # recovery decision must not mistake for a missing file: propagate
            # it with its error context intact.
            if err.args and err.args[0] == errno.ENOENT:
                return False
            raise

    def _remove_if_exists(self, path):
        if self._path_exists(path):
            os.remove(path)

    def recover(self):
        """Boot recovery: settle the committed config before anything else
        runs. A valid .old is authoritative (a promotion interrupted before
        its commit point), then a valid config.json, then a valid .tmp; an
        invalid .old is released. On success the steady state is exactly one
        valid config.json; else startup fails clearly."""
        config_path = self._config_path
        old_path = self._old_path()
        tmp_path = self._tmp_path()

        if self._path_exists(old_path):
            try:
                config = load_config(old_path)
            except MemoryError:
                raise
            except ConfigError:
                config = None
            if config is not None:
                if self._path_exists(config_path):
                    os.remove(config_path)
                os.rename(old_path, config_path)
                self._remove_if_exists(tmp_path)
                os.sync()
                return config

        if self._path_exists(config_path):
            try:
                config = load_config(config_path)
            except MemoryError:
                raise
            except ConfigError:
                config = None
            if config is not None:
                self._remove_if_exists(old_path)
                self._remove_if_exists(tmp_path)
                os.sync()
                return config

        if self._path_exists(tmp_path):
            try:
                config = load_config(tmp_path)
            except MemoryError:
                raise
            except ConfigError:
                config = None
            if config is not None:
                os.rename(tmp_path, config_path)
                # The .old that reached this branch is invalid by
                # definition: release it so the steady state is one file.
                self._remove_if_exists(old_path)
                os.sync()
                return config

        raise ConfigError(
            "No valid configuration found: {} and its .old/.tmp recovery "
            "artifacts are all missing or invalid".format(config_path),
            code="unreadable_file",
        )

    def read_persisted(self):
        """The validated committed configuration (read-config); no copy is retained."""
        return load_config(self._config_path)

    def begin_write(self, candidate):
        """Validate and (for a changed candidate) atomically promote a
        write-config candidate. Returns the classification and the change
        summary (PERSISTED-before vs candidate). UNCHANGED writes perform no
        filesystem modification and preserve the current reboot state; a
        changed candidate commits and is pending a reboot -- no live apply on
        either core. MemoryError propagates to the fail-fast boundary; other
        failures restore pre-write state and leave the artifacts for boot
        recovery."""
        validate_config(candidate)
        persisted = load_config(self._config_path)
        changes = _changes_summary(persisted, candidate)

        if candidate == persisted:
            # Semantically identical to what is already committed: no
            # filesystem write and the current reboot state is preserved.
            return {
                "classification": CLASSIFICATION_UNCHANGED,
                "changes": changes,
                "reboot_required": self.reboot_required,
            }

        try:
            self._write_candidate_tmp(candidate)
            # Read the candidate back from flash and validate it: an
            # incomplete or corrupted write aborts BEFORE promotion.
            load_config(self._tmp_path())
            os.rename(self._config_path, self._old_path())
            os.rename(self._tmp_path(), self._config_path)
            os.sync()
        except MemoryError:
            # The heap is exhausted: every recovery artifact is already in
            # place; let the fail-fast boundary handle the reset.
            raise
        except Exception:
            # A non-MemoryError failure (I/O, corrupted read-back): restore
            # pre-write state, release the transient .tmp, and propagate.
            try:
                self._restore_committed()
            except OSError:
                # A failed restoration still leaves every recovery artifact
                # in place for boot recovery.
                pass
            raise

        # The running firmware keeps its boot values until the next reboot:
        # the previous committed config is no longer needed.
        self._remove_if_exists(self._old_path())
        os.sync()
        self._reboot_required = True
        return {
            "classification": CLASSIFICATION_REBOOT_REQUIRED,
            "changes": changes,
            "reboot_required": True,
        }

    def _restore_committed(self):
        """Restore the pre-write committed config after a failed promotion:
        if the first rename moved config.json into .old, put it back before
        releasing the failed candidate, so config.json is never missing when
        the caller regains control."""
        if self._path_exists(self._old_path()):
            self._remove_if_exists(self._config_path)
            os.rename(self._old_path(), self._config_path)
        self._remove_if_exists(self._tmp_path())
        os.sync()

    def _write_candidate_tmp(self, candidate):
        """Fully write the candidate to .tmp (closed) -- a crash after this
        leaves a promotable artifact.

        json.dump() streams the serialization straight into the file object
        (MicroPython's dump writes through the stream, no pre-built string),
        so a configuration-sized allocation never coexists with the rest of
        the write-config peak (inbound frame + parsed graph + candidate +
        active config). A mid-write failure leaves a partial .tmp -- the
        tolerated artifact: the read-back re-validation before promotion is
        what decides, never the write itself."""
        with open(self._tmp_path(), "w") as handle:
            json.dump(candidate, handle)
