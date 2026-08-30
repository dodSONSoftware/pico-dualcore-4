# test_config_registry.py - Change-policy registry, patch validation,
# staged persistence, boot recovery, and committed-state bookkeeping
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Host-side tests for the configuration layer that backs runtime
configuration management:

- the single authoritative change-policy registry (exactly one policy per
  valid key, owner partitions cover the writable keys);
- ``validate_config_patch`` stable rejection codes;
- the SHA-256 checksum of the exact committed bytes;
- staged persistence (stage -> commit -> read-back) and the recovery-safe
  commit sequence (a failed commit leaves the previous file intact);
- boot recovery (valid primary wins; a valid backup is promoted; both
  invalid fails startup; a stale ``.tmp`` is never auto-promoted);
- ``ConfigState`` committed view and the ``reboot_required`` /
  ``pending_restart_keys`` bookkeeping.
"""

import copy
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import config as cfg  # noqa: E402
import observability as obs  # noqa: E402
from version import CONFIG_SCHEMA_VERSION  # noqa: E402


ROOT = pathlib.Path(__file__).resolve().parents[1]


def _base_config():
    return json.loads((ROOT / "config.json").read_text())


def _write(path, value):
    with open(str(path), "w") as handle:
        json.dump(value, handle)
    return str(path)


# ---------------------------------------------------------------------------
# Change-policy registry
# ---------------------------------------------------------------------------

def test_registry_covers_exactly_the_valid_key_set():
    base = _base_config()
    # Every valid top-level key has exactly one policy, and the policy map's
    # key set is exactly the valid key set (no extra, no missing).
    assert set(cfg.CONFIG_CHANGE_POLICIES) == set(base)
    for key, policy in cfg.CONFIG_CHANGE_POLICIES.items():
        assert policy in (
            cfg.CHANGE_DYNAMIC,
            cfg.CHANGE_RECONFIGURE,
            cfg.CHANGE_RESTART_REQUIRED,
            cfg.CHANGE_READ_ONLY,
        ), key


def test_registry_read_only_is_schema_and_generation_only():
    read_only = {
        key for key, policy in cfg.CONFIG_CHANGE_POLICIES.items()
        if policy == cfg.CHANGE_READ_ONLY
    }
    assert read_only == {"config_schema_version", "config_generation"}


def test_registry_owner_partitions_cover_the_writable_keys():
    dynamic = set(cfg.CORE0_DYNAMIC_KEYS) | set(cfg.CORE1_DYNAMIC_KEYS)
    reconfigure = set(cfg.CORE0_MQTT_RECONFIGURE_KEYS) | set(cfg.CORE1_RECONFIGURE_KEYS)
    writable = {
        key for key, policy in cfg.CONFIG_CHANGE_POLICIES.items()
        if policy in (cfg.CHANGE_DYNAMIC, cfg.CHANGE_RECONFIGURE)
    }
    # DYNAMIC and RECONFIGURE partitions are disjoint and together cover the
    # writable non-restart keys.
    assert dynamic & reconfigure == set()
    assert dynamic | reconfigure == writable
    # Restart-required keys are persisted only; they belong to no owner.
    restart = {
        key for key, policy in cfg.CONFIG_CHANGE_POLICIES.items()
        if policy == cfg.CHANGE_RESTART_REQUIRED
    }
    assert restart == {"max_intercore_event_entries"}
    assert (dynamic | reconfigure) & restart == set()


# ---------------------------------------------------------------------------
# validate_config_patch stable codes
# ---------------------------------------------------------------------------

def test_patch_unknown_key_is_invalid_config_key():
    with pytest.raises(cfg.ConfigPatchError) as exc:
        cfg.validate_config_patch({"bogus_key": 1})
    assert exc.value.code == obs.REASON_CONFIG_INVALID_KEY


def test_patch_read_only_keys_are_read_only_config_key():
    for key in ("config_schema_version", "config_generation"):
        with pytest.raises(cfg.ConfigPatchError) as exc:
            cfg.validate_config_patch({key: 1})
        assert exc.value.code == obs.REASON_CONFIG_READ_ONLY_KEY


def test_patch_invalid_value_is_invalid_config_value():
    with pytest.raises(cfg.ConfigPatchError) as exc:
        cfg.validate_config_patch({"health_interval_sec": -5})
    assert exc.value.code == obs.REASON_CONFIG_INVALID_VALUE


def test_patch_empty_and_non_object_rejected():
    for bad in ({}, "nope", 5, None, []):
        with pytest.raises(cfg.ConfigPatchError):
            cfg.validate_config_patch(bad)


def test_patch_valid_returns_sorted_keys():
    keys = cfg.validate_config_patch({
        "source": "a",
        "read_loop_sec": 2,
        "health_interval_sec": 30,
    })
    assert keys == sorted(["source", "read_loop_sec", "health_interval_sec"])


def test_patch_is_atomic_on_any_invalid_key():
    # A patch mixing a valid key and an invalid one is rejected wholesale
    # (no partial application is possible at this layer).
    with pytest.raises(cfg.ConfigPatchError) as exc:
        cfg.validate_config_patch({"source": "ok", "bogus_key": 1})
    assert exc.value.code == obs.REASON_CONFIG_INVALID_KEY


# ---------------------------------------------------------------------------
# Checksum
# ---------------------------------------------------------------------------

def test_checksum_is_sha256_of_the_exact_bytes():
    data = json.dumps({"a": 1, "b": [1, 2, 3]}).encode("utf-8")
    import hashlib
    expected = hashlib.sha256(data).hexdigest()
    assert cfg.config_checksum(data) == expected


# ---------------------------------------------------------------------------
# Staged persistence
# ---------------------------------------------------------------------------

def test_stage_then_commit_round_trip(tmp_path):
    base = _base_config()
    primary = tmp_path / "config.json"
    primary.write_text(json.dumps(base))
    staged = tmp_path / "config.json.tmp"

    candidate = dict(base)
    candidate["source"] = "staged-node"
    candidate["config_generation"] = 1

    staged_checksum = cfg.stage_config_candidate(
        candidate, str(primary), str(staged))
    assert staged.exists()
    # Staging alone does not touch the active file.
    assert json.loads(primary.read_text())["source"] == base["source"]

    new_checksum = cfg.commit_config_file(str(primary), str(staged))
    # The active file is now the candidate; the staged file is gone.
    committed = json.loads(primary.read_text())
    assert committed["source"] == "staged-node"
    assert committed["config_generation"] == 1
    assert not staged.exists()
    assert new_checksum == staged_checksum


def test_commit_failure_leaves_previous_file_intact(tmp_path):
    base = _base_config()
    primary = tmp_path / "config.json"
    primary.write_text(json.dumps(base))
    staged = tmp_path / "config.json.tmp"

    # A candidate that fails validation must not be staged (and therefore
    # cannot be committed).
    bad = dict(base)
    bad["read_loop_sec"] = -1
    with pytest.raises(cfg.ConfigError):
        cfg.stage_config_candidate(bad, str(primary), str(staged))
    assert not staged.exists()
    # The active file is untouched.
    assert json.loads(primary.read_text()) == base


def test_staged_read_back_mismatch_is_rejected(tmp_path, monkeypatch):
    base = _base_config()
    candidate = dict(base)
    candidate["source"] = "mismatch-node"
    primary = tmp_path / "config.json"
    primary.write_text(json.dumps(base))
    staged = tmp_path / "config.json.tmp"

    # Force the read-back to decode a *different* (but valid) document than
    # the bytes that were written, so the checksum comparison fails.
    real_read = cfg._read_config_file

    def _tampered(path):
        data = real_read(path)
        tampered = dict(candidate)
        tampered["source"] = "different"
        return json.dumps(tampered).encode("utf-8")

    monkeypatch.setattr(cfg, "_read_config_file", _tampered)
    with pytest.raises(cfg.ConfigError):
        cfg.stage_config_candidate(candidate, str(primary), str(staged))
    # The staged file is removed on verification failure.
    assert not staged.exists()
    # The active file is untouched.
    assert json.loads(primary.read_text()) == base


def test_stage_rejects_invalid_combination_atomically(tmp_path):
    base = _base_config()
    primary = tmp_path / "config.json"
    primary.write_text(json.dumps(base))
    staged = tmp_path / "config.json.tmp"

    # Individually valid, but the merged candidate breaks a cross-field rule
    # (diagnostics interval out of range).
    candidate = dict(base)
    candidate["network_diagnostics_interval_sec"] = 30
    with pytest.raises(cfg.ConfigError):
        cfg.stage_config_candidate(candidate, str(primary), str(staged))
    assert not staged.exists()


# ---------------------------------------------------------------------------
# Boot recovery
# ---------------------------------------------------------------------------

def test_recovery_valid_primary_wins_and_cleans_stale(tmp_path):
    base = _base_config()
    primary = tmp_path / "config.json"
    primary.write_text(json.dumps(base))
    (tmp_path / "config.json.tmp").write_text(json.dumps(base))
    (tmp_path / "config.json.bak").write_text(json.dumps(base))

    config, checksum, info = cfg.recover_config_file(str(primary))
    assert config == base
    assert info is None
    assert not (tmp_path / "config.json.tmp").exists()
    assert not (tmp_path / "config.json.bak").exists()
    assert checksum == cfg.config_checksum(json.dumps(base).encode("utf-8"))


def test_recovery_promotes_valid_backup(tmp_path):
    base = _base_config()
    primary = tmp_path / "config.json"
    primary.write_text("{ this is not valid json")
    backup = tmp_path / "config.json.bak"
    backup.write_text(json.dumps(base))

    config, checksum, info = cfg.recover_config_file(str(primary))
    assert config == base
    assert info == {"reason": "configuration_primary_invalid"}
    # The backup was renamed into place.
    assert json.loads(primary.read_text()) == base
    assert not backup.exists()
    assert checksum == cfg.config_checksum(json.dumps(base).encode("utf-8"))


def test_recovery_both_invalid_fails_startup(tmp_path):
    primary = tmp_path / "config.json"
    primary.write_text("{ this is not valid json")
    (tmp_path / "config.json.bak").write_text("{ also not valid json")

    with pytest.raises(cfg.ConfigError):
        cfg.recover_config_file(str(primary))


def test_recovery_never_promotes_stale_tmp(tmp_path):
    base = _base_config()
    primary = tmp_path / "config.json"
    primary.write_text(json.dumps(base))
    # A stale staged file (from a killed transaction) with a *different*
    # value must never be promoted over the valid primary.
    stale = dict(base)
    stale["read_loop_sec"] = 9999
    (tmp_path / "config.json.tmp").write_text(json.dumps(stale))

    config, _checksum, info = cfg.recover_config_file(str(primary))
    assert config == base
    assert info is None
    assert not (tmp_path / "config.json.tmp").exists()


def test_recovery_missing_primary_and_backup_fails(tmp_path):
    primary = tmp_path / "config.json"
    with pytest.raises(cfg.ConfigError):
        cfg.recover_config_file(str(primary))


# ---------------------------------------------------------------------------
# ConfigState: committed view + reboot_required bookkeeping
# ---------------------------------------------------------------------------

def test_config_state_boot_view_is_not_reboot_required():
    base = _base_config()
    state = cfg.ConfigState(base, "abc123")
    view = state.snapshot()
    assert view["config_schema_version"] == CONFIG_SCHEMA_VERSION
    assert view["config_generation"] == base["config_generation"]
    assert view["reboot_required"] is False
    assert view["pending_restart_keys"] == []
    assert view["config_checksum_sha256"] == "abc123"


def test_config_state_commit_bumps_and_sets_reboot_required():
    base = _base_config()
    state = cfg.ConfigState(base, "abc123")
    boot_value = base["max_intercore_event_entries"]

    candidate = copy.deepcopy(base)
    candidate["config_generation"] = base["config_generation"] + 1
    candidate["max_intercore_event_entries"] = boot_value + 4
    state.commit(candidate, "def456")

    view = state.snapshot()
    assert view["config_generation"] == base["config_generation"] + 1
    assert view["config_checksum_sha256"] == "def456"
    assert view["reboot_required"] is True
    assert view["pending_restart_keys"] == ["max_intercore_event_entries"]
    # The committed configuration is now authoritative.
    assert state.committed_config()["max_intercore_event_entries"] == boot_value + 4


def test_config_state_revert_clears_reboot_required():
    base = _base_config()
    state = cfg.ConfigState(base, "abc123")
    boot_value = base["max_intercore_event_entries"]

    changed = copy.deepcopy(base)
    changed["config_generation"] = base["config_generation"] + 1
    changed["max_intercore_event_entries"] = boot_value + 4
    state.commit(changed, "def456")
    assert state.snapshot()["reboot_required"] is True

    reverted = copy.deepcopy(changed)
    reverted["config_generation"] = base["config_generation"] + 2
    reverted["max_intercore_event_entries"] = boot_value
    state.commit(reverted, "ghi789")
    view = state.snapshot()
    assert view["reboot_required"] is False
    assert view["pending_restart_keys"] == []
