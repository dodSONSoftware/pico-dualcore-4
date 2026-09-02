# config_manager.py - Core 0 configuration persistence and change policy
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Core 0-owned configuration manager: persistence, ACTIVE-vs-PERSISTED state, change classification, file transactions and boot recovery.

config.py remains the single source of truth for schema validation and
per-core splitting (this manager calls it, never re-implements it). This
manager owns:

- boot recovery of the committed config from the transaction artifacts;
- the ACTIVE (running firmware) vs PERSISTED (config.json) distinction and
  the derived ``reboot_required`` state;
- classification of a write-config candidate (UNCHANGED / HOT_RELOADED /
  REBOOT_REQUIRED) against the explicit policy table below;
- the atomic config.json promotion (``.tmp`` write + read-back + rename)
  and its commit/rollback for HOT_RELOADED transactions.

It does not own subsystem behavior (no MQTT, Wi-Fi, devices, or machine
control): Core 0 orchestrates the HOT_RELOADED runtime apply/ack and tells
this manager to commit or roll back.

State invariant: an active snapshot exists <=> ACTIVE differs from PERSISTED
(a reboot is required to make the committed config the running one). The
snapshot is a compact serialized copy, never a second permanent object
graph; ``reboot_required`` is derived from it, never maintained separately.
A reboot clears RAM and reloads config.json, so no flag is persisted.
"""

import json
import os

from config import ConfigError, load_config, validate_config

CLASSIFICATION_UNCHANGED = "UNCHANGED"
CLASSIFICATION_HOT_RELOADED = "HOT_RELOADED"
CLASSIFICATION_REBOOT_REQUIRED = "REBOOT_REQUIRED"

CHANGE_POLICY_HOT_RELOADED = "HOT_RELOADED"
CHANGE_POLICY_REBOOT_REQUIRED = "REBOOT_REQUIRED"

# Explicit top-level change policy: every classifiable config key appears
# exactly once (config_schema_version is a schema invariant, validated
# before classification, not classifiable). This table is the single source
# of truth for classification -- no reboot/hot decision lives anywhere else.
# A future required key without a policy fails the coverage test rather
# than defaulting silently.
_CHANGE_POLICY = {
    "source": CHANGE_POLICY_REBOOT_REQUIRED,
    "read_loop_sec": CHANGE_POLICY_HOT_RELOADED,
    "health_interval_sec": CHANGE_POLICY_HOT_RELOADED,
    "datetime_sync_interval_min": CHANGE_POLICY_HOT_RELOADED,
    "device_initialization_attempts": CHANGE_POLICY_REBOOT_REQUIRED,
    "device_initialization_retry_delay_ms": CHANGE_POLICY_REBOOT_REQUIRED,
    "device_read_failure_threshold": CHANGE_POLICY_REBOOT_REQUIRED,
    "network_snapshot_interval_sec": CHANGE_POLICY_HOT_RELOADED,
    # Only consumed by the startup verification contract's network probes;
    # no steady-state probe reads it, so a live change would have no effect
    # until the next boot -- HOT_RELOADED would be a false promise.
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
    """Compact whole-device change entries (ADDED / REMOVED / MODIFIED), sorted by id.

    Whole-device entries keep the summary small in RAM and on the wire; the
    per-field diff is what the sender already knows (it sent the candidate).
    A difference no single id accounts for (a list-order change) is one
    bounded list-level MODIFIED entry -- never the arrays themselves."""
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
    """Describe what THIS write changed: PERSISTED-before vs candidate, deterministically sorted.

    Scalar/list/string settings carry original/new values; devices carry
    compact whole-device entries. config_schema_version is never reported:
    a different version is invalid, not a change."""
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
        # from the committed config.json (a reboot is pending). None is the
        # normal state: ACTIVE == current config.json, and nothing
        # configuration-shaped is retained at all.
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
        except OSError:
            return False

    def _remove_if_exists(self, path):
        if self._path_exists(path):
            os.remove(path)

    def recover(self):
        """Boot recovery: settle the committed config before anything else runs.

        A valid .old is authoritative: its presence means a promotion was
        interrupted before its commit point, so it is restored even when the
        current config.json is valid but uncommitted. Otherwise a valid
        config.json is the committed steady state (an invalid .old is
        released); a valid .tmp is the last-resort recovery artifact; else
        startup fails clearly. On success the steady state is exactly one
        valid config.json."""
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
        """Validate, classify and (for a changed candidate) atomically promote a write-config candidate.

        Returns a result dict: classification, the change summary (PERSISTED-before
        vs candidate), the resulting reboot_required, and whether the
        transaction awaits runtime application (HOT_RELOADED only). For
        REBOOT_REQUIRED the first transition serializes the ACTIVE snapshot
        BEFORE any file is modified; the snapshot is kept unchanged by later
        writes. For HOT_RELOADED the previous config is retained in .old
        until commit_hot_reload()/rollback_hot_reload(). MemoryError
        propagates to the fail-fast boundary; any other failure leaves the
        transaction artifacts for boot recovery and restores pre-write state."""
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
        # running; otherwise deserialize the compact snapshot (released
        # again by the end of the transaction).
        active = (
            persisted
            if self._active_snapshot is None
            else json.loads(self._active_snapshot)
        )
        classification = _classify_change(active, candidate)

        # First reboot-pending transition: the committed config is also the
        # active one; keep a compact serialized copy before the destructive
        # promotion. A failed allocation aborts before any file is touched
        # (MemoryError propagates; the snapshot is still None).
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
                # A failed restoration still leaves every recovery
                # artifact in place for boot recovery; the original
                # failure below propagates unchanged.
                pass
            raise

        if classification == CLASSIFICATION_REBOOT_REQUIRED:
            # The running firmware keeps its active values until the next
            # reboot: the snapshot is the reboot state and the previous
            # committed config is no longer needed.
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
        """Runtime application succeeded: the candidate is now ACTIVE and PERSISTED.

        Any pending reboot is cancelled (the snapshot is discarded), the
        retained previous config is released, and the steady state is one
        valid config.json."""
        self._active_snapshot = None
        self._remove_if_exists(self._old_path())
        os.sync()
        self._transaction_active = False

    def rollback_hot_reload(self):
        """Runtime application failed: restore the previous committed configuration.

        The active snapshot state is exactly what it was before
        begin_write() (begin_write only ever CREATES a snapshot, for a
        REBOOT_REQUIRED classification -- never for a HOT one), so no
        snapshot handling is needed here."""
        os.rename(self._old_path(), self._config_path)
        os.sync()
        self._transaction_active = False

    def _restore_committed(self):
        """Restore the pre-write committed config after a failed promotion.

        If the first promotion rename has already moved config.json into
        .old, the previous committed config is there: put it back BEFORE
        releasing the failed candidate, so config.json is never missing
        when the caller regains control. A failure here is an OSError
        for the caller to preserve (artifacts stay for boot recovery)."""
        if self._path_exists(self._old_path()):
            self._remove_if_exists(self._config_path)
            os.rename(self._old_path(), self._config_path)
        self._remove_if_exists(self._tmp_path())
        os.sync()

    def _write_candidate_tmp(self, candidate):
        """Fully write the candidate to .tmp (closed) -- a crash after this leaves a promotable artifact."""
        text = json.dumps(candidate)
        with open(self._tmp_path(), "w") as handle:
            handle.write(text)
