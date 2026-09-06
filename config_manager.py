# config_manager.py - Core 0 configuration persistence and change policy
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Core 0-owned configuration manager: persistence, ACTIVE-vs-PERSISTED
state, change classification, file transactions, and boot recovery.

config.py remains the single source of truth for schema validation and
per-core splitting. State invariant: an active snapshot exists <=> ACTIVE
differs from PERSISTED (a reboot is required to make the committed config
the running one); ``reboot_required`` is derived from the snapshot, never
maintained separately. Core 0 orchestrates the HOT_RELOADED runtime
apply/ack and tells this manager to commit or roll back.
"""

import errno
import json
import os

from config import ConfigError, load_config, validate_config

CLASSIFICATION_UNCHANGED = "UNCHANGED"
CLASSIFICATION_HOT_RELOADED = "HOT_RELOADED"
CLASSIFICATION_REBOOT_REQUIRED = "REBOOT_REQUIRED"

CHANGE_POLICY_HOT_RELOADED = "HOT_RELOADED"
CHANGE_POLICY_REBOOT_REQUIRED = "REBOOT_REQUIRED"

# Explicit top-level change policy: every classifiable key appears exactly
# once (config_schema_version is validated before classification, not
# classifiable). Single source of truth for classification -- a required
# key without a policy fails the coverage test rather than defaulting
# silently.
_CHANGE_POLICY = {
    "source": CHANGE_POLICY_REBOOT_REQUIRED,
    "read_loop_sec": CHANGE_POLICY_HOT_RELOADED,
    "health_interval_sec": CHANGE_POLICY_HOT_RELOADED,
    "datetime_sync_interval_min": CHANGE_POLICY_HOT_RELOADED,
    "device_initialization_attempts": CHANGE_POLICY_REBOOT_REQUIRED,
    "device_initialization_retry_delay_ms": CHANGE_POLICY_REBOOT_REQUIRED,
    "device_read_failure_threshold": CHANGE_POLICY_REBOOT_REQUIRED,
    "network_snapshot_interval_sec": CHANGE_POLICY_HOT_RELOADED,
    # Consumed only by the startup probes: no steady-state probe reads it,
    # so HOT_RELOADED would be a false promise.
    "network_probe_timeout_sec": CHANGE_POLICY_REBOOT_REQUIRED,
    "mqtt_broker_ip_address": CHANGE_POLICY_REBOOT_REQUIRED,
    "mqtt_keepalive_sec": CHANGE_POLICY_REBOOT_REQUIRED,
    "mqtt_command_poll_ms": CHANGE_POLICY_HOT_RELOADED,
    "mqtt_outbound_publish_delay_ms": CHANGE_POLICY_HOT_RELOADED,
    "mqtt_broker_response_timeout_sec": CHANGE_POLICY_REBOOT_REQUIRED,
    "mqtt_topic_telemetry": CHANGE_POLICY_REBOOT_REQUIRED,
    "mqtt_topic_log": CHANGE_POLICY_REBOOT_REQUIRED,
    "mqtt_topic_command": CHANGE_POLICY_REBOOT_REQUIRED,
    "mqtt_topic_command_response": CHANGE_POLICY_REBOOT_REQUIRED,
    "mqtt_topic_info_request": CHANGE_POLICY_REBOOT_REQUIRED,
    "mqtt_topic_info_response": CHANGE_POLICY_REBOOT_REQUIRED,
    "mqtt_topic_network_probe": CHANGE_POLICY_REBOOT_REQUIRED,
    "mqtt_topic_health": CHANGE_POLICY_REBOOT_REQUIRED,
    "wifi_reconnect_delays_sec": CHANGE_POLICY_REBOOT_REQUIRED,
    "mqtt_reconnect_delays_sec": CHANGE_POLICY_REBOOT_REQUIRED,
    "devices": CHANGE_POLICY_REBOOT_REQUIRED,
}


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
            "change_policy": _CHANGE_POLICY["devices"],
            "change_type": change_type,
            "device_id": device_id,
            "device_type": device_type,
        })
    if not changes and old_devices != new_devices:
        # The lists differ only in a way no id attributes (ordering).
        changes.append({
            "setting": "devices",
            "change_policy": _CHANGE_POLICY["devices"],
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
                "change_policy": _CHANGE_POLICY[key],
                "original_value": persisted[key],
                "new_value": candidate[key],
            })
    return changes


def _classify_change(active, candidate):
    """Any active-to-candidate difference under reboot policy => REBOOT_REQUIRED; else HOT_RELOADED."""
    for key, policy in _CHANGE_POLICY.items():
        if active[key] != candidate[key] and policy == CHANGE_POLICY_REBOOT_REQUIRED:
            return CLASSIFICATION_REBOOT_REQUIRED
    return CLASSIFICATION_HOT_RELOADED


class ConfigManager:

    def __init__(self, config_path="config.json"):
        self._config_path = config_path
        # Serialized ACTIVE configuration, present only while ACTIVE differs
        # from the committed config.json (a reboot is pending); None is the
        # normal state.
        self._active_snapshot = None
        # One write transaction at a time: True from begin_write() until
        # commit_hot_reload()/rollback_hot_reload() (HOT_RELOADED only).
        self._transaction_active = False

    @property
    def reboot_required(self):
        # Derived, never maintained: a snapshot exists <=> reboot required.
        return self._active_snapshot is not None

    @property
    def transaction_active(self):
        return self._transaction_active

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
        """Boot recovery: settle the committed config before anything else runs.

        A valid .old is authoritative (a promotion interrupted before its
        commit point), then a valid config.json, then a valid .tmp; an
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
        """Validate, classify, and (for a changed candidate) atomically
        promote a write-config candidate.

        Returns classification, the change summary (PERSISTED-before vs
        candidate), reboot_required, and whether the transaction awaits
        runtime application (HOT_RELOADED only). The first REBOOT_REQUIRED
        transition serializes the ACTIVE snapshot before any file is
        modified, kept unchanged by later writes; HOT_RELOADED keeps the
        previous config in .old until commit/rollback. MemoryError
        propagates to the fail-fast boundary; other failures restore
        pre-write state and leave the artifacts for boot recovery."""
        if self._transaction_active:
            raise ConfigError(
                "A configuration write transaction is already in progress",
                code="transaction_in_progress",
            )

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
                "pending": False,
            }

        # Resolve ACTIVE: no snapshot means the committed config is what is
        # running; otherwise deserialize the compact snapshot.
        active = (
            persisted
            if self._active_snapshot is None
            else json.loads(self._active_snapshot)
        )
        classification = _classify_change(active, candidate)

        # First reboot-pending transition: keep a serialized ACTIVE copy
        # before the destructive promotion (a failed allocation aborts
        # before any file is touched).
        snapshot_created_here = False
        if (
            classification == CLASSIFICATION_REBOOT_REQUIRED
            and self._active_snapshot is None
        ):
            self._active_snapshot = json.dumps(persisted)
            snapshot_created_here = True

        self._transaction_active = True
        try:
            self._write_candidate_tmp(candidate)
            # Read the candidate back from flash and validate it: an
            # incomplete or corrupted write aborts BEFORE promotion.
            load_config(self._tmp_path())
            os.rename(self._config_path, self._old_path())
            os.rename(self._tmp_path(), self._config_path)
            os.sync()
        except MemoryError:
            # The heap is exhausted: restore pre-write state with no
            # allocation, then let the fail-fast boundary handle the reset.
            self._transaction_active = False
            if snapshot_created_here:
                self._active_snapshot = None
            raise
        except Exception:
            # A non-MemoryError failure (I/O, corrupted read-back): restore
            # pre-write state, release the transient .tmp, and propagate.
            self._transaction_active = False
            if snapshot_created_here:
                self._active_snapshot = None
            try:
                self._restore_committed()
            except OSError:
                # A failed restoration still leaves every recovery artifact
                # in place for boot recovery.
                pass
            raise

        if classification == CLASSIFICATION_REBOOT_REQUIRED:
            # The running firmware keeps its active values until the next
            # reboot: the previous committed config is no longer needed.
            self._remove_if_exists(self._old_path())
            os.sync()
            self._transaction_active = False
            # HOT_RELOADED instead keeps .old until the caller applies the
            # change and commits, or rolls back.

        return {
            "classification": classification,
            "changes": changes,
            "reboot_required": self.reboot_required,
            "pending": classification == CLASSIFICATION_HOT_RELOADED,
        }

    def commit_hot_reload(self):
        """Runtime application succeeded: the candidate is now ACTIVE and
        PERSISTED -- any pending reboot is cancelled and the steady state is
        one valid config.json."""
        self._active_snapshot = None
        self._remove_if_exists(self._old_path())
        os.sync()
        self._transaction_active = False

    def rollback_hot_reload(self):
        """Runtime application failed: restore the previous committed
        configuration (no snapshot handling: a HOT transaction never creates
        one)."""
        os.rename(self._old_path(), self._config_path)
        os.sync()
        self._transaction_active = False

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
        """Fully write the candidate to .tmp (closed) -- a crash after this leaves a promotable artifact.

        json.dump() streams the serialization straight into the file object
        (MicroPython's dump writes through the stream, no pre-built string),
        so a configuration-sized allocation never coexists with the rest of
        the write-config peak (inbound frame + parsed graph + candidate +
        active config). A mid-write failure leaves a partial .tmp, which is
        already the tolerated artifact: the read-back re-validation before
        promotion is what decides, never the write itself."""
        with open(self._tmp_path(), "w") as handle:
            json.dump(candidate, handle)
