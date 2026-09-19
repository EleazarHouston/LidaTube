"""HTTP routes and Socket.IO events, exercised through the real app that LidaTube.py builds."""

import importlib
import io
import os
import sys
import threading
from unittest.mock import Mock

import pytest

import web


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


def test_home_route_returns_html(lidatube_module):
    client = lidatube_module.app.test_client()
    response = client.get("/")

    assert response.status_code == 200
    assert b"LidaTube" in response.data


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
    monkeypatch.setattr(web.threading, "Thread", _InlineThread)
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

    monkeypatch.setattr(web.threading, "Thread", FakeThread)
    reset_called = []
    monkeypatch.setattr(lidatube_module.data_handler.queue, "reset", lambda: reset_called.append(1))

    lidatube_module.socketio.test_client(lidatube_module.app).emit("reset_ytdlp")

    assert len(started) == 1 and reset_called == []
    started[0]()
    assert reset_called == [1]
