# test_wifi_config.py - Wi-Fi secrets (config-secrets.json) validation tests
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from config import (  # noqa: E402
    MAX_WIFI_PASSWORD_BYTES,
    MAX_WIFI_SSID_BYTES,
    WifiConfigError,
    load_wifi_config,
)


def _write(tmp_path, value, name="config-secrets.json"):
    path = tmp_path / name
    path.write_text(json.dumps(value))
    return str(path)


def test_load_wifi_config_accepts_valid_secrets(tmp_path):
    path = _write(tmp_path, {"wifi_ssid": "Lab", "wifi_password": "secret"})

    assert load_wifi_config(path) == {"wifi_ssid": "Lab", "wifi_password": "secret"}


def test_load_wifi_config_allows_empty_password(tmp_path):
    """An open network has no passphrase: the empty string stays legal —
    only the size and content gates are new."""
    path = _write(tmp_path, {"wifi_ssid": "Open", "wifi_password": ""})

    assert load_wifi_config(path) == {"wifi_ssid": "Open", "wifi_password": ""}


def test_load_wifi_config_rejects_unknown_key(tmp_path):
    path = _write(
        tmp_path, {"wifi_ssid": "Lab", "wifi_password": "x", "extra": 1}
    )

    with pytest.raises(WifiConfigError) as exc:
        load_wifi_config(path)
    assert "extra" in str(exc.value)


def test_load_wifi_config_rejects_empty_ssid(tmp_path):
    path = _write(tmp_path, {"wifi_ssid": "", "wifi_password": "x"})

    with pytest.raises(WifiConfigError):
        load_wifi_config(path)


def test_load_wifi_config_rejects_missing_password(tmp_path):
    path = _write(tmp_path, {"wifi_ssid": "Lab"})

    with pytest.raises(WifiConfigError):
        load_wifi_config(path)


def test_load_wifi_config_accepts_ssid_at_byte_bound(tmp_path):
    ssid = "s" * MAX_WIFI_SSID_BYTES  # 32 bytes, the IEEE 802.11 SSID limit
    path = _write(tmp_path, {"wifi_ssid": ssid, "wifi_password": "x"})

    assert load_wifi_config(path)["wifi_ssid"] == ssid


def test_load_wifi_config_rejects_ssid_over_byte_bound(tmp_path):
    path = _write(
        tmp_path, {"wifi_ssid": "s" * (MAX_WIFI_SSID_BYTES + 1), "wifi_password": "x"}
    )

    with pytest.raises(WifiConfigError) as exc:
        load_wifi_config(path)
    assert "wifi_ssid" in str(exc.value)


def test_load_wifi_config_measures_ssid_in_utf8_bytes(tmp_path):
    """A multibyte SSID inside the character count but over the byte bound
    is rejected: the bound is bytes, not Python characters."""
    # 17 characters, 51 UTF-8 bytes (> 32).
    ssid = "é" * 17
    path = _write(tmp_path, {"wifi_ssid": ssid, "wifi_password": "x"})

    with pytest.raises(WifiConfigError):
        load_wifi_config(path)


def test_load_wifi_config_accepts_password_at_byte_bound(tmp_path):
    password = "p" * MAX_WIFI_PASSWORD_BYTES
    path = _write(tmp_path, {"wifi_ssid": "Lab", "wifi_password": password})

    assert load_wifi_config(path)["wifi_password"] == password


def test_load_wifi_config_rejects_password_over_byte_bound(tmp_path):
    path = _write(
        tmp_path,
        {"wifi_ssid": "Lab", "wifi_password": "p" * (MAX_WIFI_PASSWORD_BYTES + 1)},
    )

    with pytest.raises(WifiConfigError) as exc:
        load_wifi_config(path)
    assert "wifi_password" in str(exc.value)


def test_load_wifi_config_rejects_nul_in_ssid(tmp_path):
    path = _write(tmp_path, {"wifi_ssid": "La\x00b", "wifi_password": "x"})

    with pytest.raises(WifiConfigError):
        load_wifi_config(path)


def test_load_wifi_config_rejects_nul_in_password(tmp_path):
    path = _write(tmp_path, {"wifi_ssid": "Lab", "wifi_password": "s\x00ecret"})

    with pytest.raises(WifiConfigError):
        load_wifi_config(path)
