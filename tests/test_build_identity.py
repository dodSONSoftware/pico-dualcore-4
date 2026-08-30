# test_build_identity.py - Build commit, generated build_info, envelope identity
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import importlib
import json
import pathlib
import sys
import tarfile
from unittest.mock import MagicMock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import release  # noqa: E402
from version import FIRMWARE_BUILD_COMMIT, FIRMWARE_VERSION, MESSAGE_SCHEMA_VERSION  # noqa: E402
from observability import (  # noqa: E402
    EVENT_WIFI_CONNECTION_ESTABLISHED,
    LEVEL_INFO,
    REASON_NONE,
)
# Shared with test_core0_recovery (its mocks are installed inside the fixture,
# so importing here at collection time is safe). _install_mocks must run
# before `import core0` in the tests below so this file passes in isolation.
from test_core0_recovery import _install_mocks, make_core0  # noqa: E402,F401


class _GitResult:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


def test_version_dev_fallback_is_unknown_on_host():
    """A source tree without build_info.py must degrade to "unknown" (and
    not block startup)."""
    import version
    assert "build_info" not in sys.modules
    assert version.FIRMWARE_BUILD_COMMIT == "unknown"
    assert FIRMWARE_BUILD_COMMIT == "unknown"
    # Version identity is separate from build identity.
    assert FIRMWARE_VERSION != FIRMWARE_BUILD_COMMIT


def test_resolve_build_commit_reads_git_head(monkeypatch, tmp_path):
    calls = {}

    def fake_run(cmd, **kwargs):
        calls["cmd"] = cmd
        calls["cwd"] = kwargs.get("cwd")
        return _GitResult(0, "abc1234def56\n")

    monkeypatch.setattr(release.subprocess, "run", fake_run)
    commit = release.resolve_build_commit(root=tmp_path)
    assert commit == "abc1234def56"
    assert calls["cmd"] == ["git", "rev-parse", "--short", "HEAD"]
    assert str(calls["cwd"]) == str(tmp_path)


def test_resolve_build_commit_degrades_to_unknown_on_any_failure(monkeypatch, tmp_path):
    def failing_run(cmd, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(release.subprocess, "run", failing_run)
    assert release.resolve_build_commit(root=tmp_path) == "unknown"

    for rc, stdout in ((1, ""), (0, ""), (0, "abc1"), (0, "zzz1234")):
        monkeypatch.setattr(
            release.subprocess, "run",
            lambda cmd, **kwargs: _GitResult(rc, stdout))
        assert release.resolve_build_commit(root=tmp_path) == "unknown"


def test_build_info_module_content_carries_the_commit():
    content = release.build_info_module_content("abc1234def56")
    assert 'FIRMWARE_BUILD_COMMIT = "abc1234def56"' in content
    assert "build_info.py" in content  # generated-file header names itself


def test_release_archive_contains_generated_build_info_and_observability(monkeypatch, tmp_path):
    commit = "abc1234def56"
    expected = release.build_info_module_content(commit)
    monkeypatch.setattr(release, "resolve_build_commit", lambda: commit)

    artifact = release.build_artifact(tmp_path)
    assert artifact.name == "sensor-firmware-{}.tar.gz".format(FIRMWARE_VERSION)

    with tarfile.open(artifact, "r:gz") as archive:
        names = archive.getnames()
        # Exactly one build_info.py, with exactly the generated content.
        assert names.count("build_info.py") == 1
        assert archive.extractfile("build_info.py").read().decode("utf-8") == expected
        # The vocabulary module ships with the release (not generated).
        assert "observability.py" in names
        assert "version.py" in names
        # No secrets path ever enters the archive.
        assert not any("config-secrets" in name for name in names)


def test_envelope_carrys_firmware_build_commit_exactly_once():
    """The spliced envelope has firmware_build_commit exactly once, and it is
    version.FIRMWARE_BUILD_COMMIT (the dev fallback on the host)."""
    # Install the MicroPython stand-ins before importing core0 so this file
    # passes in isolation, not only when another module's collection-time
    # import has already shadowed machine/time in sys.modules.
    _install_mocks()
    import core0
    import version

    class _FakeSelf:
        _runtime_id = "test-runtime"
        _config = {"source": "source-host"}

    fragment = core0.Core0._envelope_fragment(_FakeSelf(), 7)
    text = fragment.decode("utf-8")
    assert text.count("firmware_build_commit") == 1
    assert '"firmware_build_commit": "{}"'.format(version.FIRMWARE_BUILD_COMMIT) in text
    assert '"firmware_build_commit": "{}"'.format(version.FIRMWARE_VERSION) not in text
    # All six envelope members are present exactly once.
    for key in ("sequence", "runtime_id", "source", "firmware_version",
                "message_schema_version", "firmware_build_commit"):
        assert text.count('"{}"'.format(key)) == 1, key


def test_queued_connection_log_carries_no_envelope_or_identity_keys(make_core0):
    """Senders carry their complete message pre-serialized: the queued log
    message has uptime_ms/timestamp (sender-owned) and must NOT carry the
    envelope keys at the top level, nor duplicate identity inside the
    payload or data. make_core0() installs the stand-ins and imports core0
    under them; core0 must not be imported before that call."""
    instance = make_core0()
    instance._queue_connection_log(
        LEVEL_INFO,
        EVENT_WIFI_CONNECTION_ESTABLISHED,
        REASON_NONE,
        "Connected to Wi-Fi",
        {"ssid": "test-ssid", "ip_address": "192.168.1.100"},
    )

    message = instance._pending_connection_logs[0]
    assert message["message_type"] == "log"
    # Sender-owned fields, stamped at queue time.
    assert isinstance(message["uptime_ms"], int)
    assert "timestamp" in message
    # No envelope key at the top level.
    for key in ("sequence", "runtime_id", "source", "firmware_version",
                "message_schema_version", "firmware_build_commit"):
        assert key not in message, key

    payload = message["payload"]
    assert set(payload) == {"level", "event", "reason_code", "message", "data"}
    assert payload["level"] == "INFO"
    assert payload["event"] == "wifi_connection_established"
    assert payload["reason_code"] == "none"
    assert "module" not in payload
    # Identity fields never duplicated inside the payload or its data.
    for key in ("runtime_id", "uptime_ms", "firmware_version",
                "firmware_build_commit", "sequence", "message_schema_version"):
        assert key not in payload.get("data", {}), key
