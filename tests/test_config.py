import json
import os
import pytest
from unittest.mock import Mock

from config import AppConfig


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return AppConfig(Mock())


# --- Defaults ---


def test_defaults_applied(cfg):
    assert cfg.lidarr_address == "http://192.168.1.2:8686"
    assert cfg.lidarr_api_key == ""
    assert cfg.lidarr_api_timeout == 120.0
    assert cfg.thread_limit == 1
    assert cfg.sleep_interval == 0
    assert cfg.fallback_to_top_result is False
    assert cfg.library_scan_on_completion is True
    assert cfg.sync_schedule == []
    assert cfg.minimum_match_ratio == 90
    assert cfg.secondary_search == "YTS"
    assert cfg.preferred_codec == "mp3"
    assert cfg.attempt_lidarr_import is False
    assert cfg.lidarr_scan_thread_limit == 16


# --- File persistence ---


def test_save_writes_all_keys(cfg, tmp_path):
    cfg.save()
    with open(cfg.settings_file) as f:
        data = json.load(f)
    for key in AppConfig.DEFAULTS:
        assert key in data


def test_load_from_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    os.makedirs("config")
    with open(os.path.join("config", AppConfig.SETTINGS_FILENAME), "w") as f:
        json.dump({"lidarr_address": "http://custom:9090", "lidarr_api_key": "secret"}, f)
    cfg = AppConfig(Mock())
    assert cfg.lidarr_address == "http://custom:9090"
    assert cfg.lidarr_api_key == "secret"
    assert cfg.thread_limit == 1  # default still applied


# --- Env vars ---


def test_env_var_overrides_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    os.makedirs("config")
    with open(os.path.join("config", AppConfig.SETTINGS_FILENAME), "w") as f:
        json.dump({"lidarr_address": "http://from-file:8686"}, f)
    monkeypatch.setenv("lidarr_address", "http://from-env:9000")
    cfg = AppConfig(Mock())
    assert cfg.lidarr_address == "http://from-env:9000"


def test_env_bool_true(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("fallback_to_top_result", "true")
    cfg = AppConfig(Mock())
    assert cfg.fallback_to_top_result is True


def test_env_bool_false(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("fallback_to_top_result", "false")
    cfg = AppConfig(Mock())
    assert cfg.fallback_to_top_result is False


def test_env_int_conversion(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("thread_limit", "4")
    cfg = AppConfig(Mock())
    assert cfg.thread_limit == 4


def test_env_float_conversion(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("lidarr_api_timeout", "60.5")
    cfg = AppConfig(Mock())
    assert cfg.lidarr_api_timeout == 60.5


def test_env_sync_schedule_parsed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("sync_schedule", "2,14,22")
    cfg = AppConfig(Mock())
    assert cfg.sync_schedule == [2, 14, 22]


# --- parse_sync_schedule ---


def test_parse_sync_schedule_empty_string():
    assert AppConfig.parse_sync_schedule("") == []


def test_parse_sync_schedule_valid():
    assert AppConfig.parse_sync_schedule("2,14,22") == [2, 14, 22]


def test_parse_sync_schedule_deduplicates_and_sorts():
    assert AppConfig.parse_sync_schedule("5,5,3") == [3, 5]


def test_parse_sync_schedule_strips_non_digits_from_negative():
    # re.sub strips the minus sign, so "-1" becomes 1
    result = AppConfig.parse_sync_schedule("-1,12")
    assert 1 in result
    assert 12 in result


def test_parse_sync_schedule_clamps_over_23_to_zero():
    result = AppConfig.parse_sync_schedule("25,6")
    assert 6 in result
    assert 0 in result


# --- cookies_path ---


def test_cookies_path_none_when_no_file(cfg):
    assert cfg.cookies_path is None


def test_cookies_path_set_when_file_exists(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    os.makedirs("config")
    (tmp_path / "config" / "cookies.txt").write_text("cookie data")
    cfg = AppConfig(Mock())
    assert cfg.cookies_path is not None
    assert cfg.cookies_path.endswith("cookies.txt")


# --- folder constants ---


def test_download_folder_default(cfg):
    assert cfg.download_folder == "downloads"
