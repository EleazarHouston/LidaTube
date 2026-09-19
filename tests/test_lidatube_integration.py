import importlib
import io
import json
import os
import sys
import threading
import time
from unittest.mock import Mock, patch

import pytest
from fd_governor import FdGovernor
from download_queue import DownloadQueue
from lidarr_scan import LidarrScanner
from store import Store


@pytest.fixture
def lidatube_module(tmp_path):
    old_cwd = os.getcwd()
    old_start = threading.Thread.start
    sys.modules.pop("LidaTube", None)

    try:
        os.chdir(tmp_path)
        threading.Thread.start = lambda self: None
        module = importlib.import_module("LidaTube")
        threading.Thread.start = old_start
        yield module
    finally:
        threading.Thread.start = old_start
        os.chdir(old_cwd)
        sys.modules.pop("LidaTube", None)


def build_data_handler(module):
    handler = module.DataHandler.__new__(module.DataHandler)
    handler.general_logger = Mock()

    handler.fd = FdGovernor(Mock())
    handler.fd.fd_limit = None
    handler.clients_connected_counter = 0

    # Config mock
    cfg = Mock()
    cfg.lidarr_address = "http://lidarr.test"
    cfg.lidarr_api_key = "api-key"
    cfg.lidarr_api_timeout = 30
    cfg.lidarr_download_path = "/staging"
    cfg.minimum_match_ratio = 80
    cfg.extended_duration_tolerance_seconds = 30
    cfg.fallback_to_top_result = False
    cfg.secondary_search = "YTS"
    cfg.thread_limit = 1
    cfg.batch_size = 2
    cfg.auto_resume = True
    cfg.lidarr_scan_thread_limit = 8
    cfg.download_folder = "/downloads"
    cfg.preferred_codec = "mp3"
    cfg.sleep_interval = 0
    cfg.attempt_lidarr_import = False
    cfg.library_scan_on_completion = False
    cfg.sync_schedule = []
    cfg.CONFIG_FOLDER = "config"
    cfg.save = Mock()
    handler.config = cfg
    handler.store = Mock()
    handler.store.enqueue_items.side_effect = lambda session_id, items: len(items)
    handler.store.start_session.return_value = 1
    handler.store.resumable_session.return_value = None
    handler.store.resume_session.return_value = True
    handler.store.next_batch.return_value = []
    handler.store.queue_counts.return_value = {
        "pending": 0,
        "in_progress": 0,
        "done": 0,
        "error": 0,
        "total": 0,
    }
    handler.store.get_override.return_value = None
    handler.store.get_session_result_counts.return_value = {"matched_count": 0, "failed_count": 0}

    # LidarrClient mock
    handler.lidarr_client = Mock()
    handler.lidarr_client.get_artists_page.return_value = FakeResponse(200, [])
    handler.downloader = Mock()
    handler.searcher = Mock()
    stop_event = threading.Event()
    handler.scanner = LidarrScanner(
        cfg, handler.lidarr_client, handler.fd, emit=Mock(), logger=handler.general_logger,
        download_stop_event=stop_event,
    )
    handler.queue = DownloadQueue(
        cfg, handler.store, handler.scanner, handler.searcher, handler.downloader, handler.lidarr_client,
        stop_event, emit=Mock(), logger=handler.general_logger, yield_to_event_loop=Mock(),
    )

    return handler


@pytest.fixture
def app_client(lidatube_module):
    lidatube_module.app.config["TESTING"] = True
    with lidatube_module.app.test_client() as client:
        yield client, lidatube_module


class TestPersistenceApiRoutes:
    def test_sessions_tracks_no_match_and_evaluations_are_paginated(self, app_client):
        client, module = app_client
        session_id = module.data_handler.store.start_session(requested_count=1)
        result_id = module.data_handler.store.record_track_result(
            session_id=session_id, artist="Artist", album="Album", track_title="Missing",
            track_number=1, track_id=17, duration_ms=180000, outcome="no_match", suspicion=88,
        )
        module.data_handler.store.record_evaluations(result_id, [{
            "source": "ytmusic", "candidate_title": "Wrong", "candidate_url": "https://example.test/wrong",
            "candidate_duration_s": 180, "score": 88, "rejected_by": "version_gate",
        }])

        sessions = client.get("/api/sessions?limit=1&offset=0").get_json()
        tracks = client.get(f"/api/sessions/{session_id}/tracks?limit=1&offset=0").get_json()
        no_match = client.get("/api/no_match?limit=1&offset=0&order=suspicion").get_json()
        evaluations = client.get(f"/api/track/{result_id}/evaluations").get_json()
        attention = client.get("/api/attention?limit=1&offset=0").get_json()

        assert sessions["total"] == 1
        assert tracks["items"][0]["id"] == result_id
        assert no_match["items"][0]["suspicion"] == 88
        assert evaluations["items"][0]["rejected_by"] == "version_gate"
        assert attention["items"][0]["id"] == result_id

    def test_override_endpoints_validate_and_round_trip(self, app_client):
        client, _ = app_client
        assert client.post("/api/override", json={"track_id": 1}).status_code == 400
        response = client.post("/api/override", json={"track_id": 17, "forced_url": "https://youtube.test/watch?v=forced", "note": "known good"})
        assert response.status_code == 201
        assert client.get("/api/overrides?limit=10&offset=0").get_json()["items"][0]["track_id"] == 17
        assert client.delete("/api/override/17").status_code == 200
        assert client.get("/api/overrides?limit=10&offset=0").get_json()["items"] == []

    def test_lidarr_api_pages_and_filters_attention_items(self, app_client):
        client, module = app_client
        module.data_handler.scanner.items = [
            {"artist": "Artist", "album_name": "One", "missing_count": 1, "track_count": 2, "scan_ready": True},
            {"artist": "Artist", "album_name": "Complete", "missing_count": 0, "track_count": 2, "scan_ready": True},
            {"artist": "Other", "album_name": "Two", "missing_count": 1, "track_count": 1, "scan_ready": True},
        ]
        page = client.get("/api/lidarr?limit=1&offset=0&q=artist").get_json()
        assert page["total"] == 1
        assert page["items"][0]["album_name"] == "One"
        assert page["items"][0]["index"] == 0

    def test_lidarr_api_ids_only_returns_all_matching_indices(self, app_client):
        client, module = app_client
        module.data_handler.scanner.items = [
            {"artist": "Artist", "album_name": "One", "missing_count": 1, "track_count": 2, "scan_ready": True},
            {"artist": "Artist", "album_name": "Complete", "missing_count": 0, "track_count": 2, "scan_ready": True},
            {"artist": "Other", "album_name": "Two", "missing_count": 1, "track_count": 1, "scan_ready": True},
        ]
        # ids_only must return every filtered index (for select-all), not just a page.
        data = client.get("/api/lidarr?ids_only=1").get_json()
        assert data["ids"] == [0, 2]
        assert data["total"] == 2
        assert client.get("/api/lidarr?ids_only=1&q=other").get_json()["ids"] == [2]

    def test_lidarr_api_ids_only_checked_filter_drives_default_download_set(self, app_client):
        """Regression: downloads queued only the ~100 rendered rows.

        The default download set must come from the server's `checked` state across the
        whole list, not from whichever rows the virtualized table happened to load.
        """
        client, module = app_client
        module.data_handler.scanner.items = [
            {"artist": "A", "album_name": "One", "missing_count": 1, "track_count": 2, "scan_ready": True, "checked": True},
            {"artist": "B", "album_name": "Two", "missing_count": 1, "track_count": 1, "scan_ready": True, "checked": False},
            {"artist": "C", "album_name": "Three", "missing_count": 3, "track_count": 3, "scan_ready": True, "checked": True},
        ]
        assert client.get("/api/lidarr?ids_only=1").get_json()["ids"] == [0, 1, 2]
        checked = client.get("/api/lidarr?ids_only=1&checked_only=1").get_json()
        assert checked["ids"] == [0, 2]
        assert checked["total"] == 2

    def test_queue_status_reports_persisted_counts(self, app_client):
        client, module = app_client
        session_id = module.data_handler.store.start_session(requested_count=2)
        module.data_handler.store.enqueue_items(
            session_id,
            [{"album_id": 1}, {"album_id": 2}],
        )
        first = module.data_handler.store.next_batch(session_id, 1)[0]
        module.data_handler.store.mark_queue_item(first["id"], "in_progress")

        response = client.get("/api/queue/status")

        assert response.status_code == 200
        assert response.get_json() == {
            "session_id": session_id,
            "pending": 1,
            "in_progress": 1,
            "done": 0,
            "error": 0,
            "total": 2,
            "batch": 0,
            "matched": 0,
            "failed": 0,
        }

    def test_resume_and_stop_endpoints_delegate_to_handler(self, app_client, monkeypatch):
        client, module = app_client
        monkeypatch.setattr(module.data_handler.queue, "resume", Mock(return_value=True))
        monkeypatch.setattr(module.data_handler.queue, "snapshot", Mock(return_value={"session_id": 7}))
        assert client.post("/api/session/resume").status_code == 202

        module.data_handler.queue.in_progress = True
        stop_mock = Mock()
        monkeypatch.setattr(module.data_handler.queue, "stop", stop_mock)
        assert client.post("/api/session/stop").status_code == 202
        stop_mock.assert_called_once_with()

    @pytest.mark.parametrize("path, running, resumed, status", [
        ("/api/session/resume", True, True, 409),
        ("/api/session/resume", False, False, 404),
        ("/api/session/stop", False, None, 409),
    ])
    def test_session_endpoints_refuse_conflicting_requests(self, app_client, monkeypatch, path, running, resumed, status):
        client, module = app_client
        module.data_handler.queue.in_progress = running
        monkeypatch.setattr(module.data_handler.queue, "resume", Mock(return_value=resumed))
        stop_mock = Mock()
        monkeypatch.setattr(module.data_handler.queue, "stop", stop_mock)

        assert client.post(path).status_code == status
        stop_mock.assert_not_called()

    def test_persistence_api_rejects_invalid_pagination(self, app_client):
        client, _ = app_client
        assert client.get("/api/sessions?limit=0").status_code == 400


class TestCookiesRoutes:
    def test_status_no_file(self, app_client):
        client, module = app_client
        module.data_handler.config.cookies_path = None
        resp = client.get("/cookies_status")
        assert resp.status_code == 200
        assert resp.get_json()["exists"] is False

    def test_status_file_exists(self, app_client):
        client, module = app_client
        cookies_file = os.path.join(module.data_handler.config.CONFIG_FOLDER, "cookies.txt")
        with open(cookies_file, "w") as f:
            f.write("cookie data")
        module.data_handler.config.cookies_path = os.path.abspath(cookies_file)
        resp = client.get("/cookies_status")
        assert resp.status_code == 200
        assert resp.get_json()["exists"] is True

    def test_upload_saves_as_cookies_txt(self, app_client):
        client, module = app_client
        data = {"cookies_file": (io.BytesIO(b"cookie data"), "my_exported_cookies")}
        resp = client.post("/upload_cookies", data=data, content_type="multipart/form-data")
        assert resp.status_code == 200
        expected_path = os.path.abspath(os.path.join(module.data_handler.config.CONFIG_FOLDER, "cookies.txt"))
        assert os.path.exists(expected_path)
        assert module.data_handler.config.cookies_path == expected_path

    def test_upload_any_filename_accepted(self, app_client):
        client, module = app_client
        for filename in ("cookies.txt", "my_cookies", "export.bin", "netscape_cookies.txt"):
            data = {"cookies_file": (io.BytesIO(b"cookie data"), filename)}
            resp = client.post("/upload_cookies", data=data, content_type="multipart/form-data")
            assert resp.status_code == 200, f"Upload failed for filename: {filename}"

    def test_upload_no_file_returns_400(self, app_client):
        client, module = app_client
        resp = client.post("/upload_cookies", data={}, content_type="multipart/form-data")
        assert resp.status_code == 400

    def test_delete_removes_file_and_clears_path(self, app_client):
        client, module = app_client
        cookies_file = os.path.join(module.data_handler.config.CONFIG_FOLDER, "cookies.txt")
        with open(cookies_file, "w") as f:
            f.write("cookie data")
        module.data_handler.config.cookies_path = os.path.abspath(cookies_file)
        resp = client.delete("/delete_cookies")
        assert resp.status_code == 200
        assert not os.path.exists(cookies_file)
        assert module.data_handler.config.cookies_path is None

    def test_delete_when_no_file_still_succeeds(self, app_client):
        client, module = app_client
        module.data_handler.config.cookies_path = None
        resp = client.delete("/delete_cookies")
        assert resp.status_code == 200
        assert module.data_handler.config.cookies_path is None

    def test_delete_then_reupload(self, app_client):
        client, module = app_client
        cookies_file = os.path.join(module.data_handler.config.CONFIG_FOLDER, "cookies.txt")
        with open(cookies_file, "w") as f:
            f.write("old cookies")
        module.data_handler.config.cookies_path = os.path.abspath(cookies_file)

        client.delete("/delete_cookies")
        assert module.data_handler.config.cookies_path is None

        data = {"cookies_file": (io.BytesIO(b"new cookies"), "fresh_export")}
        resp = client.post("/upload_cookies", data=data, content_type="multipart/form-data")
        assert resp.status_code == 200
        assert module.data_handler.config.cookies_path is not None
        assert os.path.exists(module.data_handler.config.cookies_path)
        with open(module.data_handler.config.cookies_path) as f:
            assert f.read() == "new cookies"

        status_resp = client.get("/cookies_status")
        assert status_resp.get_json()["exists"] is True


class FakeResponse:
    def __init__(self, status_code, payload, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload

    def close(self):
        pass


def test_home_route_returns_html(lidatube_module):
    client = lidatube_module.app.test_client()
    response = client.get("/")

    assert response.status_code == 200
    assert b"LidaTube" in response.data


def test_connect_emits_updates_and_increments_client_counter(lidatube_module, monkeypatch):
    handler = build_data_handler(lidatube_module)
    emit_mock = Mock()
    monkeypatch.setattr(lidatube_module.socketio, "emit", emit_mock)
    monkeypatch.setattr(handler.scanner, "emit_update", Mock())

    handler.queue.status = "running"
    handler.queue.items = [{"album_name": "A"}]
    handler.queue.percent_completion = 25
    handler.queue.emit = emit_mock

    handler.connect()

    handler.scanner.emit_update.assert_called_once()
    emit_mock.assert_any_call(
        "ytdlp_update",
        {
            "status": "running",
            "data": [{"artist": "", "album_name": "A", "status": ""}],
            "percent_completion": 25,
            "queue": handler.queue.progress,
        },
    )
    assert handler.clients_connected_counter == 1


def test_disconnect_clamps_counter_to_zero(lidatube_module):
    handler = build_data_handler(lidatube_module)
    handler.clients_connected_counter = 0

    handler.disconnect()
    assert handler.clients_connected_counter == 0

    handler.clients_connected_counter = 1
    handler.disconnect()
    assert handler.clients_connected_counter == 0


def test_load_settings_emits_current_config(lidatube_module, monkeypatch):
    handler = build_data_handler(lidatube_module)
    emit_mock = Mock()
    monkeypatch.setattr(lidatube_module.socketio, "emit", emit_mock)

    handler.config.lidarr_address = "http://lidarr.local"
    handler.config.lidarr_api_key = "abc123"
    handler.config.sleep_interval = 1.5
    handler.config.sync_schedule = [1, 14]
    handler.config.minimum_match_ratio = 92

    handler.load_settings()

    emit_mock.assert_called_once_with(
        "settings_loaded",
        {
            "lidarr_address": "http://lidarr.local",
            "lidarr_api_key": "abc123",
            "sleep_interval": 1.5,
            "sync_schedule": [1, 14],
            "minimum_match_ratio": 92,
        },
    )


def test_update_settings_parses_sync_schedule_and_saves(lidatube_module, monkeypatch):
    handler = build_data_handler(lidatube_module)
    monkeypatch.setattr(lidatube_module.socketio, "emit", Mock())
    parse_mock = Mock(return_value=[3, 9])
    monkeypatch.setattr(lidatube_module.AppConfig, "parse_sync_schedule", parse_mock)

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
    handler.config.save.assert_called_once()


def test_update_settings_logs_error_on_bad_payload(lidatube_module, monkeypatch):
    handler = build_data_handler(lidatube_module)
    monkeypatch.setattr(lidatube_module.socketio, "emit", Mock())

    handler.update_settings({"lidarr_address": "http://missing-keys"})

    handler.general_logger.error.assert_called_once()


class _InlineThread:
    """Runs a thread's target on start() so socket handlers can be asserted synchronously."""

    def __init__(self, target=None, args=(), name=None, daemon=None, **kwargs):
        self.target = target
        self.args = args

    def start(self):
        self.target(*self.args)

    def join(self, timeout=None):
        pass


@pytest.mark.parametrize("event, args, method", [
    ("lidarr_get_wanted", (), "scanner.fetch_wanted_albums"),
    ("reset_lidarr", (), "scanner.reset"),
    ("stop_ytdlp", (), "queue.stop"),
    ("reset_ytdlp", (), "queue.reset"),
    ("add_to_download_list", ([0, 2, 5],), "queue.add_albums"),
    ("load_settings", (), "load_settings"),
    ("update_settings", ({"minimum_match_ratio": "90"},), "update_settings"),
])
def test_socket_events_dispatch_to_the_data_handler(lidatube_module, monkeypatch, event, args, method):
    monkeypatch.setattr(lidatube_module.threading, "Thread", _InlineThread)
    handler_method = Mock()
    owner = lidatube_module.data_handler
    *path, name = method.split(".")
    for attr in path:
        owner = getattr(owner, attr)
    monkeypatch.setattr(owner, name, handler_method)
    client = lidatube_module.socketio.test_client(lidatube_module.app)

    client.emit(event, *args)

    handler_method.assert_called_once_with(*args)


def test_stop_lidarr_socket_event_signals_the_running_scan(lidatube_module):
    lidatube_module.data_handler.scanner.stop_event.clear()
    client = lidatube_module.socketio.test_client(lidatube_module.app)

    client.emit("stop_lidarr")

    assert lidatube_module.data_handler.scanner.stop_event.is_set()


def test_socket_connect_and_disconnect_track_connected_clients(lidatube_module):
    handler = lidatube_module.data_handler
    client = lidatube_module.socketio.test_client(lidatube_module.app)
    assert handler.clients_connected_counter == 1

    client.disconnect()
    assert handler.clients_connected_counter == 0


def test_reset_socket_handler_runs_off_the_event_loop(lidatube_module, monkeypatch):
    """Clearing a large queue must not run inside the gevent worker: it starved /api for ~5min."""
    started = []

    class FakeThread:
        def __init__(self, target=None, name=None, daemon=None, **kwargs):
            self.target = target
            self.daemon = daemon

        def start(self):
            started.append(self.target)

    monkeypatch.setattr(lidatube_module.threading, "Thread", FakeThread)
    reset_called = []
    monkeypatch.setattr(lidatube_module.data_handler.queue, "reset", lambda: reset_called.append(1))

    lidatube_module.reset_ytdlp()

    assert len(started) == 1 and reset_called == []
    started[0]()
    assert reset_called == [1]


# --- Characterization: scheduler, Lidarr import and rescan ---


class _StopScheduler(BaseException):
    """Escapes the scheduler's infinite loop without going through its error handling."""


def _run_scheduler_once(module, handler, monkeypatch, hour):
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        raise _StopScheduler

    monkeypatch.setattr(module.time, "localtime", lambda: Mock(tm_hour=hour))
    monkeypatch.setattr(module.time, "sleep", fake_sleep)
    monkeypatch.setattr(module.threading, "Thread", _InlineThread)
    with pytest.raises(_StopScheduler):
        handler.schedule_checker()
    return sleeps


@pytest.mark.parametrize("queue_running", [False, True])
def test_scheduler_in_sync_window_streams_a_lidarr_fetch_into_the_queue(lidatube_module, monkeypatch, queue_running):
    handler = build_data_handler(lidatube_module)
    handler.config.sync_schedule = [3]
    handler.queue.in_progress = queue_running
    handler.queue.stop_event.set()
    start_queue = Mock()
    monkeypatch.setattr(handler.queue, "start", start_queue)
    streaming_during_fetch = []
    monkeypatch.setattr(handler.scanner, "fetch_wanted_albums", lambda: streaming_during_fetch.append(handler.queue.streaming_mode))

    sleeps = _run_scheduler_once(lidatube_module, handler, monkeypatch, hour=3)

    assert streaming_during_fetch == [True]
    assert handler.queue.streaming_mode is False
    assert not handler.queue.stop_event.is_set()
    assert sleeps == [3600]
    if queue_running:
        handler.store.start_session.assert_not_called()
        start_queue.assert_not_called()
    else:
        handler.store.start_session.assert_called_once_with(requested_count=0)
        start_queue.assert_called_once_with(1)


def test_scheduler_outside_sync_window_checks_again_in_ten_minutes(lidatube_module, monkeypatch):
    handler = build_data_handler(lidatube_module)
    handler.config.sync_schedule = [3]
    fetch = Mock()
    monkeypatch.setattr(handler.scanner, "fetch_wanted_albums", fetch)

    sleeps = _run_scheduler_once(lidatube_module, handler, monkeypatch, hour=4)

    assert sleeps == [600]
    fetch.assert_not_called()
