import json
from unittest.mock import Mock

import pytest

import data_handler as data_handler_module
import scheduler
from data_handler import DataHandler


@pytest.fixture
def handler(tmp_path, monkeypatch):
    """A real DataHandler rooted in tmp_path, without the scheduler's background loop."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(scheduler.SyncScheduler, "start", lambda self: None)
    handler = DataHandler(emit=Mock(), yield_to_event_loop=Mock())
    yield handler
    handler.store.close()


def test_components_share_one_download_stop_event_and_report_to_the_queue(handler):
    stop_event = handler.queue.stop_event
    assert handler.searcher.stop_event is stop_event
    assert handler.downloader.stop_event is stop_event
    assert handler.scanner.download_stop_event is stop_event
    assert handler.scanner.on_album_scanned == handler.queue.enqueue_scanned_album
    assert handler.searcher.on_status_change == handler.queue.emit_update


def test_startup_restores_the_cached_lidarr_scan(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(scheduler.SyncScheduler, "start", lambda self: None)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "lidarr_cache.json").write_text(json.dumps({
        "lidarr_items": [{"artist": "A", "album_name": "B", "scan_ready": True}],
    }))

    handler = DataHandler(emit=Mock())
    try:
        assert [item["album_name"] for item in handler.scanner.items] == ["B"]
        assert handler.scanner.status == "complete"
    finally:
        handler.store.close()


def test_connect_emits_updates_and_increments_client_counter(handler, monkeypatch):
    monkeypatch.setattr(handler.scanner, "emit_update", Mock())
    handler.queue.status = "running"
    handler.queue.items = [{"album_name": "A"}]
    handler.queue.percent_completion = 25

    handler.connect()

    handler.scanner.emit_update.assert_called_once()
    handler.emit.assert_any_call(
        "ytdlp_update",
        {
            "status": "running",
            "data": [{"artist": "", "album_name": "A", "status": ""}],
            "percent_completion": 25,
            "queue": handler.queue.progress,
        },
    )
    assert handler.clients_connected_counter == 1


def test_disconnect_clamps_counter_to_zero(handler):
    handler.disconnect()
    assert handler.clients_connected_counter == 0

    handler.clients_connected_counter = 1
    handler.disconnect()
    assert handler.clients_connected_counter == 0


def test_load_settings_emits_current_config(handler):
    handler.config.lidarr_address = "http://lidarr.local"
    handler.config.lidarr_api_key = "abc123"
    handler.config.sleep_interval = 1.5
    handler.config.sync_schedule = [1, 14]
    handler.config.minimum_match_ratio = 92

    handler.load_settings()

    handler.emit.assert_called_once_with(
        "settings_loaded",
        {
            "lidarr_address": "http://lidarr.local",
            "lidarr_api_key": "abc123",
            "sleep_interval": 1.5,
            "sync_schedule": [1, 14],
            "minimum_match_ratio": 92,
        },
    )


def test_update_settings_parses_sync_schedule_and_saves(handler, monkeypatch):
    parse_mock = Mock(return_value=[3, 9])
    monkeypatch.setattr(data_handler_module.AppConfig, "parse_sync_schedule", parse_mock)
    save_mock = Mock()
    monkeypatch.setattr(handler.config, "save", save_mock)

    handler.update_settings(
        {
            "lidarr_address": "http://new-lidarr",
            "lidarr_api_key": "new-key",
            "sleep_interval": "0.75",
            "minimum_match_ratio": "88",
            "sync_schedule": "3,9",
        }
    )

    assert handler.config.lidarr_address == "http://new-lidarr"
    assert handler.config.lidarr_api_key == "new-key"
    assert handler.config.sleep_interval == 0.75
    assert handler.config.minimum_match_ratio == 88.0
    assert handler.config.sync_schedule == [3, 9]
    parse_mock.assert_called_once_with("3,9")
    save_mock.assert_called_once()


def test_update_settings_logs_error_on_bad_payload(handler, monkeypatch):
    monkeypatch.setattr(handler, "logger", Mock())

    handler.update_settings({"lidarr_address": "http://missing-keys"})

    handler.logger.error.assert_called_once()
    assert handler.emit.call_args.args[1]["title"] == "Settings Error"
