# test_config_manager.py - Core 0 configuration manager contract
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""
Host-side tests for the Core 0 configuration manager: the policy table, the
ACTIVE-vs-PERSISTED snapshot lifecycle, boot recovery of the transaction
artifacts, the atomic promotion with .tmp read-back validation, and the
HOT_RELOADED commit/rollback. The manager is exercised on real temporary
files, exactly like the flash VFS it will drive on hardware.
"""

import copy
import json
import os
import pathlib
import sys

import pytest
from unittest.mock import MagicMock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from config import ConfigError, _REQUIRED_KEYS
from config_manager import (
    CLASSIFICATION_HOT_RELOADED,
    CLASSIFICATION_REBOOT_REQUIRED,
    CLASSIFICATION_UNCHANGED,
    ConfigManager,
    _CHANGE_POLICY,
    _classify_change,
)


ROOT = pathlib.Path(__file__).resolve().parents[1]


def _base_config():
    return json.loads((ROOT / "tests" / "fixtures" / "config.json").read_text())


@pytest.fixture
def config_dir(tmp_path):
    """A steady-state config.json plus a manager pointed at it."""
    (tmp_path / "config.json").write_text(json.dumps(_base_config()))
    return tmp_path


def _names(directory):
    return sorted(path.name for path in directory.iterdir())


def _hot_candidate():
    config = _base_config()
    config["read_loop_sec"] = 40
    return config


def _reboot_candidate():
    config = _base_config()
    config["source"] = "Other-Pico"
    return config


# ---------------------------------------------------------------------------
# Policy table: complete and explicit
# ---------------------------------------------------------------------------


def test_policy_table_covers_every_classifiable_key():
    """Every required key except the schema invariant has exactly one explicit policy."""
    classifiable = set(_REQUIRED_KEYS) - {"config_schema_version"}
    assert set(_CHANGE_POLICY) == classifiable
    assert "config_schema_version" not in _CHANGE_POLICY


def test_policy_table_covers_the_shipped_config():
    """No key of a real (schema-validated) config can lack a policy silently."""
    assert set(_base_config()) - set(_CHANGE_POLICY) <= {"config_schema_version"}


def test_policy_values_are_the_two_classifications():
    assert set(_CHANGE_POLICY.values()) <= {
        CLASSIFICATION_HOT_RELOADED,
        CLASSIFICATION_REBOOT_REQUIRED,
    }


def test_hot_policy_set_matches_the_live_apply_sets():
    """A key may be HOT_RELOADED only if a live runtime actually applies it.

    Classification promises the change takes effect in the running system
    (reboot_required=false); a policy that says HOT for a key no runtime
    consumes in steady state would report success for a value that only
    takes effect on the next boot. The classified-HOT set must therefore be
    exactly the two apply sets (this is how network_probe_timeout_sec
    regressed: HOT policy, but consumed only by the startup probes)."""
    # core0 binds MicroPython's machine/network at import time; the host has
    # neither, so stub them (setdefault can only add — CPython has no such
    # modules) and read core0's apply-set constants.
    sys.modules.setdefault("machine", MagicMock())
    sys.modules.setdefault("network", MagicMock())
    import core0
    hot_policy = {
        key for key, policy in _CHANGE_POLICY.items()
        if policy == CLASSIFICATION_HOT_RELOADED
    }
    assert hot_policy == (
        set(core0._HOT_APPLY_CORE0_KEYS) | set(core0._HOT_APPLY_CORE1_KEYS)
    )


def test_network_probe_timeout_sec_is_reboot_required():
    """The network probe timeout is consumed only by the startup verification
    contract's probes; no steady-state probe exists, so a live change has no
    effect on the running system and must ask for a reboot."""
    assert (
        _CHANGE_POLICY["network_probe_timeout_sec"]
        == CLASSIFICATION_REBOOT_REQUIRED
    )
    active = _base_config()
    candidate = _base_config()
    candidate["network_probe_timeout_sec"] = 99
    assert _classify_change(active, candidate) == CLASSIFICATION_REBOOT_REQUIRED


# ---------------------------------------------------------------------------
# Boot recovery: priority order and steady state
# ---------------------------------------------------------------------------


def test_recovery_valid_old_wins_over_uncommitted_candidate(config_dir):
    """A valid .old means the promotion never reached its commit point: the
    previous committed config is restored even when the promoted config.json
    is itself valid."""
    old = _base_config()
    (config_dir / "config.json").write_text(json.dumps(_hot_candidate()))
    (config_dir / "config.json.old").write_text(json.dumps(old))

    manager = ConfigManager(str(config_dir / "config.json"))
    config = manager.recover()

    assert config == old
    assert manager.read_persisted() == old
    assert _names(config_dir) == ["config.json"]


def test_recovery_restores_old_after_hot_promotion_reset(config_dir):
    """Reset after begin_write()'s promotion but before commit_hot_reload():
    config.json holds the candidate and .old the previous committed config —
    recovery must roll the unacknowledged candidate back, not commit it."""
    old = _base_config()
    (config_dir / "config.json").write_text(json.dumps(_hot_candidate()))
    (config_dir / "config.json.old").write_text(json.dumps(old))

    manager = ConfigManager(str(config_dir / "config.json"))
    config = manager.recover()

    assert config == old
    assert _names(config_dir) == ["config.json"]


def test_recovery_pending_hot_ack_rolls_back_after_reboot(config_dir):
    """A hot write whose Core 1 acknowledgement never arrived: the durable
    state is the promoted config.json with .old retained (transaction_active
    is in memory only). A fresh boot recovers the previous committed
    config."""
    manager = ConfigManager(str(config_dir / "config.json"))
    manager.begin_write(_hot_candidate())

    assert manager.transaction_active is True
    assert manager.read_persisted() == _hot_candidate()
    assert (config_dir / "config.json.old").exists()

    fresh = ConfigManager(str(config_dir / "config.json"))
    config = fresh.recover()

    assert config == _base_config()
    assert fresh.read_persisted() == _base_config()
    assert _names(config_dir) == ["config.json"]


def test_recovery_keeps_candidate_after_hot_commit(config_dir):
    """A committed hot reload (old released and synced) is the committed
    steady state: recovery must not roll it back."""
    manager = ConfigManager(str(config_dir / "config.json"))
    manager.begin_write(_hot_candidate())
    manager.commit_hot_reload()

    assert not (config_dir / "config.json.old").exists()

    fresh = ConfigManager(str(config_dir / "config.json"))
    config = fresh.recover()

    assert config == _hot_candidate()
    assert _names(config_dir) == ["config.json"]


def test_recovery_rolls_back_interrupted_reboot_required_promotion(config_dir):
    """Reset after a reboot-required promotion but before its commit removed
    .old: the previous committed config wins — begin_write had not completed
    and no success could have been reported."""
    old = _base_config()
    (config_dir / "config.json").write_text(json.dumps(_reboot_candidate()))
    (config_dir / "config.json.old").write_text(json.dumps(old))

    manager = ConfigManager(str(config_dir / "config.json"))
    config = manager.recover()

    assert config == old
    assert _names(config_dir) == ["config.json"]


def test_recovery_keeps_completed_reboot_required_write(config_dir):
    """A finished reboot-required write reached its commit point (.old
    removed and synced): the candidate is committed and survives reboot."""
    manager = ConfigManager(str(config_dir / "config.json"))
    manager.begin_write(_reboot_candidate())

    assert not (config_dir / "config.json.old").exists()

    fresh = ConfigManager(str(config_dir / "config.json"))
    config = fresh.recover()

    assert config == _reboot_candidate()
    assert _names(config_dir) == ["config.json"]


def test_recovery_uses_valid_current_when_old_is_invalid(config_dir):
    """.old wins only when it is itself valid; a corrupt .old is released
    with the stale artifacts."""
    (config_dir / "config.json.old").write_text("{ broken")

    manager = ConfigManager(str(config_dir / "config.json"))
    config = manager.recover()

    assert config == _base_config()
    assert _names(config_dir) == ["config.json"]


def test_recovery_restores_valid_old_over_invalid_current(config_dir):
    (config_dir / "config.json").write_text("not json at all")
    old = _base_config()
    (config_dir / "config.json.old").write_text(json.dumps(old))
    (config_dir / "config.json.tmp").write_text(json.dumps(_hot_candidate()))

    manager = ConfigManager(str(config_dir / "config.json"))
    config = manager.recover()

    assert config == old
    assert _names(config_dir) == ["config.json"]


def test_recovery_interrupted_between_renames_restores_old(config_dir):
    """A crash between the two promotion renames leaves config.json missing
    with both old and tmp present: the committed old config (the last
    steady state) is restored to config.json and the stale tmp removed."""
    (config_dir / "config.json").unlink()
    (config_dir / "config.json.old").write_text(json.dumps(_base_config()))
    (config_dir / "config.json.tmp").write_text(json.dumps(_hot_candidate()))

    manager = ConfigManager(str(config_dir / "config.json"))
    config = manager.recover()

    assert config == _base_config()
    assert _names(config_dir) == ["config.json"]


def test_recovery_promotes_valid_tmp_when_normal_and_old_are_invalid(config_dir):
    (config_dir / "config.json").write_text("corrupted")
    (config_dir / "config.json.old").write_text("{ broken")
    (config_dir / "config.json.tmp").write_text(json.dumps(_hot_candidate()))

    manager = ConfigManager(str(config_dir / "config.json"))
    config = manager.recover()

    assert config == _hot_candidate()
    assert _names(config_dir) == ["config.json"]


def test_recovery_fails_clearly_when_nothing_valid_exists(config_dir):
    (config_dir / "config.json").write_text("corrupted")
    (config_dir / "config.json.old").write_text("{ broken")
    (config_dir / "config.json.tmp").write_text("also broken")

    manager = ConfigManager(str(config_dir / "config.json"))
    with pytest.raises(ConfigError, match="No valid configuration"):
        manager.recover()

    # Nothing was touched: the recovery decision made no change
    assert _names(config_dir) == ["config.json", "config.json.old", "config.json.tmp"]


def test_recovery_fails_when_all_files_are_absent(tmp_path):
    manager = ConfigManager(str(tmp_path / "config.json"))
    with pytest.raises(ConfigError):
        manager.recover()


# ---------------------------------------------------------------------------
# UNCHANGED: no write, reboot state preserved
# ---------------------------------------------------------------------------


def test_unchanged_candidate_writes_nothing(config_dir):
    manager = ConfigManager(str(config_dir / "config.json"))
    before = (config_dir / "config.json").read_text()

    result = manager.begin_write(_base_config())

    assert result["classification"] == CLASSIFICATION_UNCHANGED
    assert result["pending"] is False
    assert result["changes"] == []
    assert result["reboot_required"] is False
    assert (config_dir / "config.json").read_text() == before
    assert _names(config_dir) == ["config.json"]
    assert manager.transaction_active is False


def test_unchanged_preserves_pending_reboot_state(config_dir):
    manager = ConfigManager(str(config_dir / "config.json"))
    manager.begin_write(_reboot_candidate())  # reboot now pending
    assert manager.reboot_required is True

    persisted = manager.read_persisted()
    result = manager.begin_write(persisted)

    assert result["classification"] == CLASSIFICATION_UNCHANGED
    assert result["reboot_required"] is True
    assert manager.reboot_required is True


# ---------------------------------------------------------------------------
# REBOOT_REQUIRED: snapshot lifecycle and reboot-state derivation
# ---------------------------------------------------------------------------


def test_first_reboot_write_snapshots_active_before_promotion(config_dir):
    original = _base_config()
    manager = ConfigManager(str(config_dir / "config.json"))

    result = manager.begin_write(_reboot_candidate())

    assert result["classification"] == CLASSIFICATION_REBOOT_REQUIRED
    assert result["reboot_required"] is True
    assert result["pending"] is False
    # The snapshot is the pre-change ACTIVE config (a serialized copy, not a dict)
    assert isinstance(manager._active_snapshot, str)
    assert json.loads(manager._active_snapshot) == original
    # The committed config is the candidate; steady state otherwise
    assert manager.read_persisted() == _reboot_candidate()
    assert _names(config_dir) == ["config.json"]
    assert manager.transaction_active is False


def test_second_reboot_write_keeps_the_original_snapshot(config_dir):
    original = _base_config()
    manager = ConfigManager(str(config_dir / "config.json"))
    manager.begin_write(_reboot_candidate())

    second = _base_config()
    second["mqtt_keepalive_sec"] = 45
    result = manager.begin_write(second)

    assert result["classification"] == CLASSIFICATION_REBOOT_REQUIRED
    assert result["reboot_required"] is True
    # The snapshot still holds the ORIGINAL active config, not the first write
    assert json.loads(manager._active_snapshot) == original
    assert manager.read_persisted() == second
    assert _names(config_dir) == ["config.json"]


def test_hot_write_cancels_pending_reboot_on_commit(config_dir):
    original = _base_config()
    manager = ConfigManager(str(config_dir / "config.json"))
    manager.begin_write(_reboot_candidate())
    assert manager.reboot_required is True

    # Candidate matches the ORIGINAL active config except one HOT key:
    # hot-applying it makes it the active config, cancelling the reboot.
    cancel = original
    cancel["read_loop_sec"] = 40
    result = manager.begin_write(cancel)
    assert result["classification"] == CLASSIFICATION_HOT_RELOADED
    assert result["pending"] is True
    assert (config_dir / "config.json.old").exists()

    manager.commit_hot_reload()

    assert manager.reboot_required is False
    assert manager._active_snapshot is None
    assert manager.read_persisted() == cancel
    assert _names(config_dir) == ["config.json"]


# ---------------------------------------------------------------------------
# HOT_RELOADED: commit and rollback
# ---------------------------------------------------------------------------


def test_hot_write_keeps_old_until_commit(config_dir):
    manager = ConfigManager(str(config_dir / "config.json"))

    result = manager.begin_write(_hot_candidate())

    assert result["classification"] == CLASSIFICATION_HOT_RELOADED
    assert result["pending"] is True
    assert manager.reboot_required is False
    assert manager.transaction_active is True
    # .old retains the previous committed config until commit/rollback
    assert json.loads((config_dir / "config.json.old").read_text()) == _base_config()
    assert manager.read_persisted() == _hot_candidate()


def test_hot_commit_releases_old_and_clears_state(config_dir):
    manager = ConfigManager(str(config_dir / "config.json"))
    manager.begin_write(_hot_candidate())

    manager.commit_hot_reload()

    assert manager.transaction_active is False
    assert manager.reboot_required is False
    assert _names(config_dir) == ["config.json"]


def test_hot_rollback_restores_previous_config(config_dir):
    manager = ConfigManager(str(config_dir / "config.json"))
    manager.begin_write(_hot_candidate())

    manager.rollback_hot_reload()

    assert manager.transaction_active is False
    assert manager.reboot_required is False
    assert manager.read_persisted() == _base_config()
    assert _names(config_dir) == ["config.json"]


def test_hot_rollback_restores_prior_snapshot_state(config_dir):
    """Rolling back a cancelling hot write restores the pending-reboot state exactly."""
    manager = ConfigManager(str(config_dir / "config.json"))
    manager.begin_write(_reboot_candidate())
    original = json.loads(manager._active_snapshot)

    cancel = _base_config()
    cancel["read_loop_sec"] = 40
    manager.begin_write(cancel)

    manager.rollback_hot_reload()

    # The committed config is the reboot-pending one again, and the snapshot
    # state is exactly what it was before the transaction.
    assert manager.read_persisted() == _reboot_candidate()
    assert manager.reboot_required is True
    assert json.loads(manager._active_snapshot) == original
    assert _names(config_dir) == ["config.json"]


def test_transaction_guard_refuses_overlapping_writes(config_dir):
    manager = ConfigManager(str(config_dir / "config.json"))
    manager.begin_write(_hot_candidate())  # HOT: pending, transaction active

    with pytest.raises(ConfigError) as excinfo:
        manager.begin_write(_reboot_candidate())
    assert excinfo.value.code == "transaction_in_progress"


def test_invalid_candidate_leaves_committed_config_untouched(config_dir):
    before = (config_dir / "config.json").read_text()
    manager = ConfigManager(str(config_dir / "config.json"))
    bad = _base_config()
    bad["not_a_real_key"] = 1

    with pytest.raises(ConfigError) as excinfo:
        manager.begin_write(bad)
    assert excinfo.value.code == "unknown_config_fields"
    assert excinfo.value.unknown_fields == ["not_a_real_key"]
    assert (config_dir / "config.json").read_text() == before
    assert _names(config_dir) == ["config.json"]
    assert manager.reboot_required is False


def test_corrupted_tmp_write_aborts_before_promotion(config_dir, monkeypatch):
    """A .tmp that does not read back as a valid config must never be promoted."""
    manager = ConfigManager(str(config_dir / "config.json"))

    def corrupt(candidate):
        (config_dir / "config.json.tmp").write_text("not json at all")

    monkeypatch.setattr(manager, "_write_candidate_tmp", corrupt)

    with pytest.raises(ConfigError):
        manager.begin_write(_reboot_candidate())

    # Nothing was promoted; the snapshot created for this write is withdrawn
    assert manager.read_persisted() == _base_config()
    assert manager.reboot_required is False
    assert manager.transaction_active is False
    assert _names(config_dir) == ["config.json"]


def test_failed_second_promotion_rename_restores_committed_config(config_dir, monkeypatch):
    """A second-promotion rename failure (after config.json has already
    moved to .old) must restore the pre-write config SYNCHRONOUSLY, before
    begin_write re-raises -- config.json may never be missing once the
    caller regains control."""
    manager = ConfigManager(str(config_dir / "config.json"))
    real_rename = os.rename

    def fail_promotion_rename(src, dst):
        if dst == str(config_dir / "config.json") and src.endswith("config.json.tmp"):
            raise OSError("simulated second-promotion rename failure")
        return real_rename(src, dst)

    monkeypatch.setattr(os, "rename", fail_promotion_rename)

    with pytest.raises(OSError, match="simulated second-promotion"):
        manager.begin_write(_reboot_candidate())

    # Pre-write state restored in place: the committed config is the
    # original, no .old/.tmp left behind, no reboot left pending.
    assert manager.read_persisted() == _base_config()
    assert _names(config_dir) == ["config.json"]
    assert manager.transaction_active is False
    assert manager.reboot_required is False


def test_failed_restoration_preserves_artifacts_and_propagates(config_dir, monkeypatch):
    """If the restoration itself fails, every recovery artifact is
    preserved for boot recovery and the ORIGINAL failure propagates."""
    manager = ConfigManager(str(config_dir / "config.json"))
    real_rename = os.rename

    def fail_promotion_and_restore(src, dst):
        if dst == str(config_dir / "config.json"):
            if src.endswith("config.json.tmp"):
                raise OSError("simulated second-promotion rename failure")
            raise OSError("simulated restoration failure")
        return real_rename(src, dst)

    monkeypatch.setattr(os, "rename", fail_promotion_and_restore)

    with pytest.raises(OSError, match="simulated second-promotion"):
        manager.begin_write(_reboot_candidate())

    # config.json is missing, but the previous committed config survives in
    # .old: exactly the state boot recovery settles.
    assert not (config_dir / "config.json").exists()
    assert (config_dir / "config.json.old").exists()
    assert manager.transaction_active is False

    monkeypatch.undo()
    # A fresh manager recovers this state on the next boot.
    recovered = ConfigManager(str(config_dir / "config.json")).recover()
    assert recovered == _base_config()
    assert _names(config_dir) == ["config.json"]


# ---------------------------------------------------------------------------
# Change summary
# ---------------------------------------------------------------------------


def test_change_summary_lists_scalar_and_list_changes_sorted(config_dir):
    manager = ConfigManager(str(config_dir / "config.json"))
    candidate = _base_config()
    candidate["read_loop_sec"] = 40
    candidate["mqtt_command_poll_ms"] = 250
    candidate["wifi_reconnect_delays_sec"] = [1, 2]

    result = manager.begin_write(candidate)

    by_setting = {change["setting"]: change for change in result["changes"]}
    assert [change["setting"] for change in result["changes"]] == sorted(by_setting)
    assert by_setting["read_loop_sec"] == {
        "setting": "read_loop_sec",
        "change_policy": CLASSIFICATION_HOT_RELOADED,
        "original_value": 20,
        "new_value": 40,
    }
    assert by_setting["mqtt_command_poll_ms"]["change_policy"] == CLASSIFICATION_HOT_RELOADED
    assert by_setting["wifi_reconnect_delays_sec"]["change_policy"] == CLASSIFICATION_REBOOT_REQUIRED
    assert by_setting["wifi_reconnect_delays_sec"] == {
        "setting": "wifi_reconnect_delays_sec",
        "change_policy": CLASSIFICATION_REBOOT_REQUIRED,
        "original_value": _base_config()["wifi_reconnect_delays_sec"],
        "new_value": [1, 2],
    }
    # Mixed policies with any reboot difference classify as REBOOT_REQUIRED
    assert result["classification"] == CLASSIFICATION_REBOOT_REQUIRED


def test_change_summary_reports_devices_compact_and_sorted(config_dir):
    manager = ConfigManager(str(config_dir / "config.json"))
    candidate = _base_config()
    original_device = candidate["devices"][0]

    modified = copy.deepcopy(original_device)
    modified["config"]["include"] = ["queues", "device_status", "memory"]
    added = {
        "id": "aaAddedDevice",
        "device_type": "system-information",
        "config": {"include": ["memory"]},
    }
    candidate["devices"] = [added, modified]  # original removed, both differ

    result = manager.begin_write(candidate)

    devices_changes = [c for c in result["changes"] if c["setting"] == "devices"]
    assert [c["device_id"] for c in devices_changes] == ["aaAddedDevice", original_device["id"]]
    assert devices_changes[0]["change_type"] == "ADDED"
    assert devices_changes[0]["device_type"] == "system-information"
    assert devices_changes[0]["change_policy"] == CLASSIFICATION_REBOOT_REQUIRED
    assert devices_changes[1]["change_type"] == "MODIFIED"
    assert devices_changes[1]["device_type"] == original_device["device_type"]
    assert "original_value" not in devices_changes[0]
    assert "new_value" not in devices_changes[0]


def test_change_summary_reports_removed_device(config_dir):
    manager = ConfigManager(str(config_dir / "config.json"))
    candidate = _base_config()
    original_device = candidate["devices"][0]
    candidate["devices"] = [{
        "id": "replacementDevice",
        "device_type": "system-information",
        "config": {"include": ["memory"]},
    }]  # the original device is removed, a replacement added

    result = manager.begin_write(candidate)

    removed = [c for c in result["changes"]
               if c["setting"] == "devices" and c["change_type"] == "REMOVED"]
    assert removed == [{
        "setting": "devices",
        "change_policy": CLASSIFICATION_REBOOT_REQUIRED,
        "change_type": "REMOVED",
        "device_id": original_device["id"],
        "device_type": original_device["device_type"],
    }]


def test_change_summary_order_only_device_change_is_one_bounded_entry(config_dir):
    """A device-list difference no id accounts for (ordering) is reported as a
    single bounded list-level MODIFIED entry, never the arrays themselves."""
    manager = ConfigManager(str(config_dir / "config.json"))

    two_devices = _base_config()
    second = {
        "id": "secondDevice",
        "device_type": "system-information",
        "config": {"include": ["memory"]},
    }
    two_devices["devices"] = list(_base_config()["devices"]) + [second]
    manager.begin_write(two_devices)  # committed: two devices

    reordered = _base_config()
    reordered["devices"] = [second] + list(_base_config()["devices"])
    result = manager.begin_write(reordered)

    devices_changes = [c for c in result["changes"] if c["setting"] == "devices"]
    assert devices_changes == [{
        "setting": "devices",
        "change_policy": CLASSIFICATION_REBOOT_REQUIRED,
        "change_type": "MODIFIED",
    }]
    for change in result["changes"]:
        assert "original_value" not in change
        assert "new_value" not in change
        assert "device_id" not in change


def test_unchanged_device_set_produces_no_device_entries(config_dir):
    manager = ConfigManager(str(config_dir / "config.json"))
    candidate = _base_config()
    candidate["read_loop_sec"] = 30

    result = manager.begin_write(candidate)

    assert [c for c in result["changes"] if c["setting"] == "devices"] == []
    assert result["changes"] == [
        {
            "setting": "read_loop_sec",
            "change_policy": CLASSIFICATION_HOT_RELOADED,
            "original_value": 20,
            "new_value": 30,
        }
    ]


# ---------------------------------------------------------------------------
# RAM: no duplicate permanent full configuration in the normal state
# ---------------------------------------------------------------------------


def test_normal_state_retains_no_full_config(config_dir):
    manager = ConfigManager(str(config_dir / "config.json"))
    manager.begin_write(_hot_candidate())
    manager.commit_hot_reload()

    # The only place a full configuration may live is the (serialized)
    # snapshot; in the normal state it is absent entirely.
    assert set(manager.__dict__) == {
        "_config_path",
        "_active_snapshot",
        "_transaction_active",
    }
    assert manager._active_snapshot is None
    assert manager.reboot_required is False
