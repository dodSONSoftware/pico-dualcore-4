# test_config_manager.py - Core 0 configuration manager contract
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""
Host-side tests for the Core 0 configuration manager: boot recovery of the
transaction artifacts, the atomic promotion with .tmp read-back validation,
and the in-boot reboot-required flag (no live apply: every committed change
is pending a reboot). The manager is exercised on real temporary files,
exactly like the flash VFS it will drive on hardware.
"""

import copy
import errno
import json
import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from config import ConfigError
from config_manager import (
    CLASSIFICATION_REBOOT_REQUIRED,
    CLASSIFICATION_UNCHANGED,
    ConfigManager,
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


def _changed_candidate():
    config = _base_config()
    config["read_loop_sec"] = 40
    return config


def _source_candidate():
    config = _base_config()
    config["source"] = "Other-Pico"
    return config


# ---------------------------------------------------------------------------
# Boot recovery: priority order and steady state
# ---------------------------------------------------------------------------


def test_recovery_valid_old_wins_over_uncommitted_candidate(config_dir):
    """A valid .old means the promotion never reached its commit point
    (.old is deleted only after the promotion succeeds): the previous
    committed config is restored even when the promoted config.json is
    itself valid."""
    old = _base_config()
    (config_dir / "config.json").write_text(json.dumps(_changed_candidate()))
    (config_dir / "config.json.old").write_text(json.dumps(old))

    manager = ConfigManager(str(config_dir / "config.json"))
    config = manager.recover()

    assert config == old
    assert manager.read_persisted() == old
    assert _names(config_dir) == ["config.json"]


def test_recovery_keeps_completed_write(config_dir):
    """A finished write reached its commit point (.old removed and synced):
    the candidate is committed and survives reboot."""
    manager = ConfigManager(str(config_dir / "config.json"))
    manager.begin_write(_source_candidate())

    assert not (config_dir / "config.json.old").exists()

    fresh = ConfigManager(str(config_dir / "config.json"))
    config = fresh.recover()

    assert config == _source_candidate()
    # A fresh instance (the reboot) starts without a pending reboot: the
    # in-boot flag cleared with RAM, and the committed change is now the
    # booted configuration.
    assert fresh.reboot_required is False
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
    (config_dir / "config.json.tmp").write_text(json.dumps(_changed_candidate()))

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
    (config_dir / "config.json.tmp").write_text(json.dumps(_changed_candidate()))

    manager = ConfigManager(str(config_dir / "config.json"))
    config = manager.recover()

    assert config == _base_config()
    assert _names(config_dir) == ["config.json"]


def test_recovery_promotes_valid_tmp_when_normal_and_old_are_invalid(config_dir):
    (config_dir / "config.json").write_text("corrupted")
    (config_dir / "config.json.old").write_text("{ broken")
    (config_dir / "config.json.tmp").write_text(json.dumps(_changed_candidate()))

    manager = ConfigManager(str(config_dir / "config.json"))
    config = manager.recover()

    assert config == _changed_candidate()
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


def test_path_exists_maps_enoent_to_absent(config_dir, monkeypatch):
    """A missing path (ENOENT) is the only failure that means 'absent'."""

    def stat_enoent(path):
        raise OSError(errno.ENOENT, "No such file or directory")

    monkeypatch.setattr(os, "stat", stat_enoent)
    assert ConfigManager._path_exists(str(config_dir / "config.json")) is False


def test_path_exists_propagates_a_real_storage_failure(config_dir, monkeypatch):
    """An I/O fault (any non-ENOENT OSError) is not a missing file: it must
    propagate with its error context instead of letting recovery select a
    different artifact on a false premise."""

    def stat_eio(path):
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(os, "stat", stat_eio)
    with pytest.raises(OSError) as exc_info:
        ConfigManager._path_exists(str(config_dir / "config.json"))
    assert exc_info.value.args[0] == errno.EIO


# ---------------------------------------------------------------------------
# UNCHANGED: no write, reboot state preserved
# ---------------------------------------------------------------------------


def test_unchanged_candidate_writes_nothing(config_dir):
    manager = ConfigManager(str(config_dir / "config.json"))
    before = (config_dir / "config.json").read_text()

    result = manager.begin_write(_base_config())

    assert result["classification"] == CLASSIFICATION_UNCHANGED
    assert result["changes"] == []
    assert result["reboot_required"] is False
    assert (config_dir / "config.json").read_text() == before
    assert _names(config_dir) == ["config.json"]


def test_unchanged_preserves_pending_reboot_state(config_dir):
    manager = ConfigManager(str(config_dir / "config.json"))
    manager.begin_write(_source_candidate())  # reboot now pending
    assert manager.reboot_required is True

    persisted = manager.read_persisted()
    result = manager.begin_write(persisted)

    assert result["classification"] == CLASSIFICATION_UNCHANGED
    assert result["reboot_required"] is True
    assert manager.reboot_required is True


# ---------------------------------------------------------------------------
# Persistence write: streamed, no configuration-sized string allocation
# ---------------------------------------------------------------------------


def test_persistence_write_never_builds_a_full_serialization_string(config_dir, monkeypatch):
    """begin_write() streams the candidate to .tmp (json.dump into the file
    object): a configuration-sized string must never be allocated alongside
    the write-config peak (inbound frame + parsed graph + candidate + active
    config). Booby-trapping json.dumps proves the persistence path does not
    depend on it, and the streamed bytes still round-trip through the
    read-back re-validation before promotion."""

    def _boom(*_args, **_kwargs):
        raise AssertionError("persistence path built a full serialization string")

    monkeypatch.setattr(json, "dumps", _boom)
    manager = ConfigManager(str(config_dir / "config.json"))

    result = manager.begin_write(_changed_candidate())

    assert result["classification"] == CLASSIFICATION_REBOOT_REQUIRED
    assert json.loads((config_dir / "config.json").read_text()) == _changed_candidate()


# ---------------------------------------------------------------------------
# REBOOT_REQUIRED: in-boot flag lifecycle
# ---------------------------------------------------------------------------


def test_first_changed_write_sets_reboot_required(config_dir):
    manager = ConfigManager(str(config_dir / "config.json"))

    result = manager.begin_write(_source_candidate())

    assert result["classification"] == CLASSIFICATION_REBOOT_REQUIRED
    assert result["reboot_required"] is True
    # The committed config is the candidate; .old deleted; steady state
    assert manager.read_persisted() == _source_candidate()
    assert _names(config_dir) == ["config.json"]


def test_second_changed_write_keeps_reboot_pending(config_dir):
    manager = ConfigManager(str(config_dir / "config.json"))
    manager.begin_write(_source_candidate())

    second = _base_config()
    second["mqtt_keepalive_sec"] = 45
    result = manager.begin_write(second)

    assert result["classification"] == CLASSIFICATION_REBOOT_REQUIRED
    assert result["reboot_required"] is True
    assert manager.reboot_required is True
    assert manager.read_persisted() == second
    assert _names(config_dir) == ["config.json"]


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
        manager.begin_write(_source_candidate())

    # Nothing was promoted: the committed config is untouched
    assert manager.read_persisted() == _base_config()
    assert manager.reboot_required is False
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
        manager.begin_write(_source_candidate())

    # Pre-write state restored in place: the committed config is the
    # original, no .old/.tmp left behind, no reboot left pending.
    assert manager.read_persisted() == _base_config()
    assert _names(config_dir) == ["config.json"]
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
        manager.begin_write(_source_candidate())

    # config.json is missing, but the previous committed config survives in
    # .old: exactly the state boot recovery settles.
    assert not (config_dir / "config.json").exists()
    assert (config_dir / "config.json.old").exists()

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
        "original_value": 20,
        "new_value": 40,
    }
    assert by_setting["mqtt_command_poll_ms"]["new_value"] == 250
    assert by_setting["wifi_reconnect_delays_sec"] == {
        "setting": "wifi_reconnect_delays_sec",
        "original_value": _base_config()["wifi_reconnect_delays_sec"],
        "new_value": [1, 2],
    }
    # Any changed key classifies as REBOOT_REQUIRED
    assert result["classification"] == CLASSIFICATION_REBOOT_REQUIRED


def test_change_summary_reports_devices_compact_and_sorted(config_dir):
    manager = ConfigManager(str(config_dir / "config.json"))
    candidate = _base_config()
    original_device = candidate["devices"][0]

    modified = copy.deepcopy(original_device)
    modified["config"]["sea_level_pressure_pa"] = 101000
    added = {
        "id": "aaAddedDevice",
        "device_type": "bme280",
        "config": {"i2c_bus": 0, "sea_level_pressure_pa": 101325},
    }
    candidate["devices"] = [added, modified]  # original removed, both differ

    result = manager.begin_write(candidate)

    devices_changes = [c for c in result["changes"] if c["setting"] == "devices"]
    assert [c["device_id"] for c in devices_changes] == ["aaAddedDevice", original_device["id"]]
    assert devices_changes[0]["change_type"] == "ADDED"
    assert devices_changes[0]["device_type"] == "bme280"
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
        "device_type": "bme280",
        "config": {"i2c_bus": 0, "sea_level_pressure_pa": 101325},
    }]  # the original device is removed, a replacement added

    result = manager.begin_write(candidate)

    removed = [c for c in result["changes"]
               if c["setting"] == "devices" and c["change_type"] == "REMOVED"]
    assert removed == [{
        "setting": "devices",
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
        "device_type": "bme280",
        "config": {"i2c_bus": 0, "sea_level_pressure_pa": 101325},
    }
    two_devices["devices"] = list(_base_config()["devices"]) + [second]
    manager.begin_write(two_devices)  # committed: two devices

    reordered = _base_config()
    reordered["devices"] = [second] + list(_base_config()["devices"])
    result = manager.begin_write(reordered)

    devices_changes = [c for c in result["changes"] if c["setting"] == "devices"]
    assert devices_changes == [{
        "setting": "devices",
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
            "original_value": 20,
            "new_value": 30,
        }
    ]


# ---------------------------------------------------------------------------
# RAM: no duplicate permanent full configuration in the normal state
# ---------------------------------------------------------------------------


def test_normal_state_retains_no_full_config(config_dir):
    # The only state is the path and the in-boot reboot flag: the manager
    # retains no full configuration of its own.
    manager = ConfigManager(str(config_dir / "config.json"))
    assert set(manager.__dict__) == {"_config_path", "_reboot_required"}
    assert manager.reboot_required is False
