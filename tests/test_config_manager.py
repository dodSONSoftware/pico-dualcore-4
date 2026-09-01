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
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from config import ConfigError, _REQUIRED_KEYS
from config_manager import (
    CLASSIFICATION_HOT_RELOADED,
    CLASSIFICATION_REBOOT_REQUIRED,
    CLASSIFICATION_UNCHANGED,
    ConfigManager,
    _CHANGE_POLICY,
)


ROOT = pathlib.Path(__file__).resolve().parents[1]


def _base_config():
    return json.loads((ROOT / "config.json").read_text())


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


# ---------------------------------------------------------------------------
# Boot recovery: priority order and steady state
# ---------------------------------------------------------------------------


def test_recovery_valid_config_wins_and_cleans_stale_artifacts(config_dir):
    (config_dir / "config.json.old").write_text(json.dumps(_base_config()))
    (config_dir / "config.json.tmp").write_text(json.dumps(_hot_candidate()))

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
