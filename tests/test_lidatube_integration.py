import importlib
import io
import json
import os
import sys
import threading
import time
from unittest.mock import Mock, patch

import pytest
import _matcher


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

    handler.lidarr_items = []
    handler.lidarr_futures = []
    handler.lidarr_status = "idle"
    handler.lidarr_stop_event = threading.Event()
    handler.lidarr_scan_guard = threading.Lock()
    handler.lidarr_scan_progress = {
        "phase": "Idle",
        "pages_scanned": 0,
        "albums_discovered": 0,
        "albums_processed": 0,
        "albums_total": 0,
        "percent": 0,
    }

    handler.ytdlp_items = []
    handler.ytdlp_futures = []
    handler.ytdlp_status = "idle"
    handler.ytdlp_stop_event = threading.Event()
    handler.fd_exhaustion_event = threading.Event()
    handler._ytmusic_semaphore = threading.Semaphore(2)
    handler.ytdlp_in_progress_flag = False
    handler.streaming_mode = False
    handler.clients_connected_counter = 0
    handler.index = 0
    handler.percent_completion = 0

    # Config mock
    cfg = Mock()
    cfg.lidarr_address = "http://lidarr.test"
    cfg.lidarr_api_key = "api-key"
    cfg.lidarr_api_timeout = 30
    cfg.minimum_match_ratio = 80
    cfg.fallback_to_top_result = False
    cfg.secondary_search = "YTS"
    cfg.thread_limit = 1
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
    handler.store.start_session.return_value = 1
    handler.store.get_session_result_counts.return_value = {"matched_count": 0, "failed_count": 0}
    handler.current_session_id = None

    # LidarrClient mock
    handler.lidarr_client = Mock()
    handler.lidarr_client.get_artists_page.return_value = FakeResponse(200, [])
    handler.downloader = Mock()

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

        assert sessions["total"] == 1
        assert tracks["items"][0]["id"] == result_id
        assert no_match["items"][0]["suspicion"] == 88
        assert evaluations["items"][0]["rejected_by"] == "version_gate"

    def test_override_endpoints_validate_and_round_trip(self, app_client):
        client, _ = app_client
        assert client.post("/api/override", json={"track_id": 1}).status_code == 400
        response = client.post("/api/override", json={"track_id": 17, "forced_url": "https://youtube.test/watch?v=forced", "note": "known good"})
        assert response.status_code == 201
        assert client.get("/api/overrides?limit=10&offset=0").get_json()["items"][0]["track_id"] == 17
        assert client.delete("/api/override/17").status_code == 200
        assert client.get("/api/overrides?limit=10&offset=0").get_json()["items"] == []

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


def test_get_wanted_albums_from_lidarr_populates_missing_tracks(lidatube_module, monkeypatch):
    handler = build_data_handler(lidatube_module)
    emit_mock = Mock()
    monkeypatch.setattr(lidatube_module.socketio, "emit", emit_mock)

    page_one_records = [
        {
            "id": 200,
            "title": "Zulu/Album?",
            "releaseDate": "2025-01-02T00:00:00Z",
            "genres": ["Metal"],
            "artistId": 20,
            "artist": {"path": "/music/Zulu", "artistName": "Zulu"},
            "releases": [{"id": 2000}],
        },
        {
            "id": 100,
            "title": "Alpha:Album*",
            "releaseDate": "2024-05-01T00:00:00Z",
            "genres": ["Rock"],
            "artistId": 10,
            "artist": {"path": "/music/Alpha", "artistName": "Alpha"},
            "releases": [{"id": 1000}],
        },
    ]

    tracks_by_album = {
        100: [
            {"title": "Song A", "trackNumber": 1, "absoluteTrackNumber": 1, "id": 10001, "hasFile": False},
            {"title": "Song B", "trackNumber": 2, "absoluteTrackNumber": 2, "id": 10002, "hasFile": True},
        ],
        200: [
            {"title": "Song Z", "trackNumber": 1, "absoluteTrackNumber": 1, "id": 20001, "hasFile": False},
        ],
    }

    def fake_get_wanted(page, page_size=2000):
        records = page_one_records if page == 1 else []
        return FakeResponse(200, {"records": records})

    def fake_get_tracks(album_id):
        return FakeResponse(200, tracks_by_album[album_id])

    handler.lidarr_client.get_artists_page.return_value = FakeResponse(200, [
        {"id": 10, "artistName": "Alpha", "path": "/music/Alpha"},
        {"id": 20, "artistName": "Zulu", "path": "/music/Zulu"},
    ])
    handler.lidarr_client.get_wanted_albums.side_effect = fake_get_wanted
    handler.lidarr_client.get_tracks_for_album.side_effect = fake_get_tracks

    handler.get_wanted_albums_from_lidarr()

    assert handler.lidarr_status == "complete"
    assert [item["artist"] for item in handler.lidarr_items] == ["Alpha", "Zulu"]

    alpha_album = handler.lidarr_items[0]
    zulu_album = handler.lidarr_items[1]

    assert alpha_album["album_name"] == "Alpha-Album-"
    assert alpha_album["track_count"] == 2
    assert alpha_album["missing_count"] == 1
    assert alpha_album["missing_tracks"][0]["track_title"] == "Song A"

    assert zulu_album["album_name"] == "Zulu+Album!"
    assert zulu_album["track_count"] == 1
    assert zulu_album["missing_count"] == 1
    assert zulu_album["missing_tracks"][0]["track_title"] == "Song Z"

    assert emit_mock.call_args_list[-1].args[0] == "lidarr_update"
    assert emit_mock.call_args_list[-1].args[1]["status"] == "complete"


def test_get_song_links_secondary_uses_yt_search_fallback(lidatube_module, monkeypatch):
    handler = build_data_handler(lidatube_module)

    class FakeYTMusic:
        def search(self, query, filter, limit):
            return []

    monkeypatch.setattr(
        handler,
        "_yt_search",
        lambda query_text: [{"title": "Artist - Track One", "link": "https://example.com/v"}],
    )

    req_album = {
        "artist": "Artist",
        "album_name": "Album",
        "missing_tracks": [
            {"artist": "Artist", "track_title": "Track One", "link": "", "title_of_link": "", "duration_ms": 0},
        ],
    }

    handler._get_song_links_secondary(req_album, artist="Artist", cleaned_artist="artist", ytmusic=FakeYTMusic())

    assert req_album["missing_tracks"][0]["link"] == "https://example.com/v"
    assert req_album["missing_tracks"][0]["title_of_link"] == "Artist - Track One"


def test_home_route_returns_html(lidatube_module):
    client = lidatube_module.app.test_client()
    response = client.get("/")

    assert response.status_code == 200
    assert b"LidaTube" in response.data


def test_link_finder_closes_ytmusic_client(lidatube_module, monkeypatch):
    """_link_finder must close the single shared YTMusic session it creates."""
    handler = build_data_handler(lidatube_module)
    monkeypatch.setattr(lidatube_module.socketio, "emit", Mock())

    close_called = []

    class FakeSession:
        def close(self):
            close_called.append(1)

    class FakeYTMusic:
        _session = FakeSession()

        def search(self, query, filter, limit):
            return []

    monkeypatch.setattr(lidatube_module, "YTMusic", FakeYTMusic)
    monkeypatch.setattr(handler, "_yt_search", lambda query_text: [])

    req_album = {
        "artist": "Artist",
        "album_name": "Album",
        "track_count": 1,
        "missing_count": 1,
        "missing_tracks": [
            {"artist": "Artist", "track_title": "Track One", "link": "", "title_of_link": ""},
        ],
        "status": "",
    }

    handler._link_finder(req_album)

    assert len(close_called) == 1


def test_streaming_mode_adds_scan_ready_album_to_ytdlp_items(lidatube_module, monkeypatch):
    """When streaming_mode=True, scan_ready albums are auto-added to ytdlp_items."""
    handler = build_data_handler(lidatube_module)
    handler.streaming_mode = True
    monkeypatch.setattr(lidatube_module.socketio, "emit", Mock())

    album = {
        "artist": "Test Artist",
        "album_name": "Test Album",
        "album_id": 42,
        "missing_tracks": [],
        "track_count": 0,
        "missing_count": 0,
        "scan_ready": False,
        "scan_in_progress": False,
        "status": "",
    }
    handler.lidarr_client.get_tracks_for_album.return_value = FakeResponse(
        200,
        [{"title": "Track 1", "trackNumber": 1, "absoluteTrackNumber": 1, "id": 1, "hasFile": False}],
    )

    handler.get_missing_tracks_for_album(album)

    assert album["scan_ready"] is True
    assert album in handler.ytdlp_items
    assert album["status"] == "Queued"


def test_streaming_mode_off_does_not_add_to_ytdlp_items(lidatube_module, monkeypatch):
    """When streaming_mode=False, scan_ready albums are NOT added to ytdlp_items."""
    handler = build_data_handler(lidatube_module)
    handler.streaming_mode = False
    monkeypatch.setattr(lidatube_module.socketio, "emit", Mock())

    album = {
        "artist": "Test Artist",
        "album_name": "Test Album",
        "album_id": 42,
        "missing_tracks": [],
        "track_count": 0,
        "missing_count": 0,
        "scan_ready": False,
        "scan_in_progress": False,
        "status": "",
    }
    handler.lidarr_client.get_tracks_for_album.return_value = FakeResponse(200, [])

    handler.get_missing_tracks_for_album(album)

    assert album["scan_ready"] is True
    assert len(handler.ytdlp_items) == 0


def test_master_queue_exits_when_empty_and_not_streaming(lidatube_module, monkeypatch):
    """master_queue exits immediately when queue is empty and not in streaming mode."""
    handler = build_data_handler(lidatube_module)
    handler.streaming_mode = False
    handler.lidarr_status = "complete"
    handler.ytdlp_in_progress_flag = True
    handler.index = 0
    monkeypatch.setattr(lidatube_module.socketio, "emit", Mock())
    monkeypatch.setattr(handler.config, "library_scan_on_completion", False)

    handler.master_queue()

    assert handler.ytdlp_status == "complete"
    assert handler.ytdlp_in_progress_flag is False


def test_master_queue_waits_when_streaming_and_fetch_busy(lidatube_module, monkeypatch):
    """master_queue stays alive while streaming_mode=True and lidarr is busy, then exits once fetch is done."""
    handler = build_data_handler(lidatube_module)
    handler.streaming_mode = True
    handler.lidarr_status = "busy"
    handler.ytdlp_in_progress_flag = True
    handler.index = 0
    monkeypatch.setattr(lidatube_module.socketio, "emit", Mock())
    monkeypatch.setattr(handler.config, "library_scan_on_completion", False)

    def finish_fetch():
        time.sleep(0.3)
        handler.lidarr_status = "complete"

    t = threading.Thread(target=finish_fetch)
    t.start()

    handler.master_queue()
    t.join()

    assert handler.ytdlp_status == "complete"
    assert handler.ytdlp_in_progress_flag is False


def test_master_queue_processes_item_added_during_streaming(lidatube_module, monkeypatch):
    """Items added to ytdlp_items during streaming are processed by the running master_queue."""
    handler = build_data_handler(lidatube_module)
    handler.streaming_mode = True
    handler.lidarr_status = "busy"
    handler.ytdlp_in_progress_flag = True
    handler.index = 0
    emit_mock = Mock()
    monkeypatch.setattr(lidatube_module.socketio, "emit", emit_mock)
    monkeypatch.setattr(handler.config, "library_scan_on_completion", False)

    processed = []

    def fake_find_link_and_download(req_album):
        processed.append(req_album["album_name"])
        handler.index += 1

    monkeypatch.setattr(handler, "find_link_and_download", fake_find_link_and_download)

    album = {"album_name": "Late Album", "status": "Queued", "scan_ready": True}

    def add_item_then_finish():
        time.sleep(0.1)
        handler.ytdlp_items.append(album)
        time.sleep(0.2)
        handler.lidarr_status = "complete"

    t = threading.Thread(target=add_item_then_finish)
    t.start()

    handler.master_queue()
    t.join()

    assert "Late Album" in processed
    assert handler.ytdlp_status == "complete"


def test_emit_lidarr_update_strips_missing_tracks(lidatube_module, monkeypatch):
    """lidarr_update socket event must not include missing_tracks (large, not needed by UI)."""
    handler = build_data_handler(lidatube_module)
    emitted = {}
    monkeypatch.setattr(lidatube_module.socketio, "emit", lambda event, data: emitted.update({event: data}))

    handler.lidarr_items = [
        {
            "artist": "A",
            "album_name": "B",
            "checked": True,
            "scan_ready": True,
            "track_count": 2,
            "missing_count": 1,
            "missing_tracks": [{"track_title": "secret", "link": ""}],
        }
    ]
    handler._emit_lidarr_update()

    assert "lidarr_update" in emitted
    item = emitted["lidarr_update"]["data"][0]
    assert "missing_tracks" not in item


def test_emit_lidarr_update_filters_complete_albums(lidatube_module, monkeypatch):
    """Albums with missing_count=0 and scan_ready=True are excluded from the emit to keep payload small."""
    handler = build_data_handler(lidatube_module)
    emitted = {}
    monkeypatch.setattr(lidatube_module.socketio, "emit", lambda event, data: emitted.update({event: data}))

    handler.lidarr_items = [
        {"artist": "A", "album_name": "complete", "scan_ready": True, "missing_count": 0, "missing_tracks": []},
        {"artist": "B", "album_name": "has_missing", "scan_ready": True, "missing_count": 2, "missing_tracks": []},
        {"artist": "C", "album_name": "still_scanning", "scan_ready": False, "missing_count": 0, "missing_tracks": []},
    ]
    handler._emit_lidarr_update()

    data = emitted["lidarr_update"]["data"]
    names = [item["album_name"] for item in data]
    assert "complete" not in names
    assert "has_missing" in names
    assert "still_scanning" in names


def test_emit_lidarr_update_includes_index_and_total_count(lidatube_module, monkeypatch):
    """Each emitted item carries its original lidarr_items index; total_count reflects full list size."""
    handler = build_data_handler(lidatube_module)
    emitted = {}
    monkeypatch.setattr(lidatube_module.socketio, "emit", lambda event, data: emitted.update({event: data}))

    handler.lidarr_items = [
        {"artist": "A", "album_name": "complete", "scan_ready": True, "missing_count": 0, "missing_tracks": []},
        {"artist": "B", "album_name": "has_missing", "scan_ready": True, "missing_count": 1, "missing_tracks": []},
    ]
    handler._emit_lidarr_update()

    payload = emitted["lidarr_update"]
    assert payload["total_count"] == 2
    assert len(payload["data"]) == 1
    assert payload["data"][0]["index"] == 1


def test_save_lidarr_cache_is_atomic(lidatube_module, monkeypatch, tmp_path):
    """Cache write uses a .tmp file then renames to prevent corruption on kill."""
    handler = build_data_handler(lidatube_module)
    handler.config.CONFIG_FOLDER = str(tmp_path)
    handler.lidarr_items = [{"artist": "X", "album_name": "Y", "missing_tracks": []}]

    rename_calls = []
    real_replace = lidatube_module.os.replace
    monkeypatch.setattr(lidatube_module.os, "replace", lambda src, dst: rename_calls.append((src, dst)) or real_replace(src, dst))

    handler._save_lidarr_cache()

    assert len(rename_calls) == 1
    src, dst = rename_calls[0]
    assert src.endswith(".tmp")
    assert not src.endswith(".tmp") or dst == src[: -len(".tmp")]


def test_cache_saved_after_each_page(lidatube_module, monkeypatch):
    """Cache is saved after each page so partial scans survive a restart."""
    handler = build_data_handler(lidatube_module)
    emit_mock = Mock()
    monkeypatch.setattr(lidatube_module.socketio, "emit", emit_mock)

    save_calls = []
    monkeypatch.setattr(handler, "_save_lidarr_cache", lambda: save_calls.append(1))

    page_one_records = [
        {
            "id": 1,
            "title": "Album A",
            "releaseDate": "2024-01-01T00:00:00Z",
            "genres": [],
            "artistId": 1,
            "artist": {"path": "/music/A", "artistName": "Artist A"},
            "releases": [{"id": 10}],
        }
    ]

    def fake_get_wanted(page, page_size=2000):
        return FakeResponse(200, {"records": page_one_records if page == 1 else []})

    handler.lidarr_client.get_artists_page.return_value = FakeResponse(200, [
        {"id": 1, "artistName": "Artist A", "path": "/music/A"},
    ])
    handler.lidarr_client.get_wanted_albums.side_effect = fake_get_wanted
    handler.lidarr_client.get_tracks_for_album.return_value = FakeResponse(200, [])

    handler.get_wanted_albums_from_lidarr()

    # At least one save during scan (per page) plus the final save on completion
    assert len(save_calls) >= 2


def test_missing_tracks_preserved_after_download(lidatube_module, monkeypatch):
    """missing_tracks must NOT be cleared after download — clearing it corrupts the cache
    for the next session (album re-queued with no tracks = silent no-op download)."""
    handler = build_data_handler(lidatube_module)
    handler.ytdlp_in_progress_flag = False
    handler.index = 0
    handler.streaming_mode = False
    monkeypatch.setattr(lidatube_module.socketio, "emit", Mock())
    monkeypatch.setattr(handler.config, "library_scan_on_completion", False)

    original_tracks = [
        {"artist": "A", "track_title": "T1", "track_number": 1, "absolute_track_number": 1,
         "track_id": 1, "link": "", "title_of_link": ""},
    ]
    req_album = {
        "artist": "A",
        "album_name": "B",
        "album_id": 1,
        "artist_path": "/music/A",
        "album_folder": "B (2024)",
        "track_count": 1,
        "missing_count": 1,
        "missing_tracks": original_tracks,
        "scan_ready": True,
        "status": "",
        "checked": True,
    }
    handler.lidarr_items = [req_album]
    handler.ytdlp_items = [req_album]

    monkeypatch.setattr(handler, "_link_finder", lambda album: None)

    handler.find_link_and_download(req_album)

    assert req_album["missing_tracks"] is original_tracks


def test_response_closed_on_non_200_track_fetch(lidatube_module, monkeypatch):
    """Response must be closed even when Lidarr returns a non-200 status (prevents FD leak)."""
    handler = build_data_handler(lidatube_module)
    handler.streaming_mode = False
    monkeypatch.setattr(lidatube_module.socketio, "emit", Mock())

    close_called = []
    error_response = FakeResponse(500, None, "Server Error")
    error_response.close = lambda: close_called.append(1)

    handler.lidarr_client.get_tracks_for_album.return_value = error_response

    album = {
        "artist": "A", "album_name": "B", "album_id": 1,
        "missing_tracks": [], "track_count": 0, "missing_count": 0,
        "scan_ready": False, "scan_in_progress": False, "status": "",
    }
    handler.get_missing_tracks_for_album(album)

    assert len(close_called) >= 1


def test_link_finder_does_not_retry_secondary_after_emfile(lidatube_module, monkeypatch):
    handler = build_data_handler(lidatube_module)
    emit_mock = Mock()
    monkeypatch.setattr(lidatube_module.socketio, "emit", emit_mock)

    class FailingYTMusic:
        def search(self, query, filter, limit):
            raise OSError(24, "No file descriptors available")

    monkeypatch.setattr(lidatube_module, "YTMusic", FailingYTMusic)
    secondary_search_mock = Mock()
    monkeypatch.setattr(handler, "_get_song_links_secondary", secondary_search_mock)

    req_album = {
        "artist": "Andy Grammer",
        "album_name": "The Art of Joy",
        "track_count": 2,
        "missing_count": 1,
        "missing_tracks": [
            {"artist": "Andy Grammer", "track_title": "The Wrong Party", "link": "", "title_of_link": ""},
        ],
    }

    handler._link_finder(req_album)

    secondary_search_mock.assert_not_called()


def test_close_ytmusic_client_closes_underlying_session(lidatube_module):
    """_close_ytmusic_client must close the requests.Session inside _session."""
    handler = build_data_handler(lidatube_module)

    closed = []

    class FakeSession:
        def close(self):
            closed.append(1)

    class FakeYTMusic:
        _session = FakeSession()

    handler._close_ytmusic_client(FakeYTMusic())
    assert len(closed) == 1


def test_close_ytmusic_client_handles_none(lidatube_module):
    """_close_ytmusic_client must not raise when passed None."""
    handler = build_data_handler(lidatube_module)
    handler._close_ytmusic_client(None)  # should not raise


def test_ytmusic_session_closed_after_album_search(lidatube_module, monkeypatch):
    """The YTMusic session must be closed by _link_finder after album search."""
    handler = build_data_handler(lidatube_module)
    monkeypatch.setattr(lidatube_module.socketio, "emit", Mock())

    closed = []

    class FakeYTMusic:
        class _session:
            @staticmethod
            def close():
                closed.append(1)

        def search(self, query, filter, limit):
            return []

    monkeypatch.setattr(lidatube_module, "YTMusic", FakeYTMusic)

    req_album = {
        "artist": "X", "album_name": "Y", "track_count": 1, "missing_count": 1,
        "missing_tracks": [{"link": "", "track_title": "T", "artist": "X", "title_of_link": ""}],
        "status": "",
    }
    handler._link_finder(req_album)

    assert len(closed) == 1


def test_record_link_results_persists_no_match_trace(lidatube_module, tmp_path):
    from store import Store

    handler = build_data_handler(lidatube_module)
    handler.store = Store(tmp_path / "lidatube.db")
    handler.current_session_id = handler.store.start_session(requested_count=1)
    album = {
        "artist": "Artist",
        "album_name": "Album",
        "missing_tracks": [{
            "artist": "Artist",
            "track_title": "Missing Track",
            "track_number": 1,
            "track_id": 42,
            "duration_ms": 180000,
            "link": "",
            "title_of_link": "",
            "_match_trace": [{
                "source": "ytmusic",
                "candidate_title": "Wrong Version",
                "candidate_url": "https://example.test/wrong",
                "candidate_duration_s": 180,
                "score": 91,
                "rejected_by": "version_gate",
            }],
        }],
    }

    handler._record_link_results(album)

    tracks = handler.store.get_session_tracks(handler.current_session_id)
    assert tracks[0]["outcome"] == "no_match"
    assert handler.store.get_evaluations(tracks[0]["id"])[0]["rejected_by"] == "version_gate"
    assert "_match_trace" not in album["missing_tracks"][0]
    handler.store.close()


def test_connect_emits_updates_and_increments_client_counter(lidatube_module, monkeypatch):
    handler = build_data_handler(lidatube_module)
    emit_mock = Mock()
    monkeypatch.setattr(lidatube_module.socketio, "emit", emit_mock)
    monkeypatch.setattr(handler, "_emit_lidarr_update", Mock())

    handler.ytdlp_status = "running"
    handler.ytdlp_items = [{"album_name": "A"}]
    handler.percent_completion = 25

    handler.connect()

    handler._emit_lidarr_update.assert_called_once()
    emit_mock.assert_any_call(
        "ytdlp_update",
        {"status": "running", "data": [{"album_name": "A"}], "percent_completion": 25},
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


def test_load_lidarr_cache_restores_cached_state(lidatube_module, tmp_path):
    handler = build_data_handler(lidatube_module)
    handler.config.CONFIG_FOLDER = str(tmp_path)

    cache_payload = {
        "lidarr_items": [{"artist": "A", "album_name": "B"}],
        "lidarr_scan_progress": {"phase": "Fetching", "albums_processed": 5, "albums_total": 10, "percent": 50},
    }
    (tmp_path / "lidarr_cache.json").write_text(json.dumps(cache_payload))

    handler._load_lidarr_cache()

    assert handler.lidarr_status == "complete"
    assert handler.lidarr_items == [{"artist": "A", "album_name": "B"}]
    assert handler.lidarr_scan_progress["phase"] == "Complete (cached)"
    assert handler.lidarr_scan_progress["albums_processed"] == 5


def test_load_lidarr_cache_logs_error_on_invalid_json(lidatube_module, tmp_path):
    handler = build_data_handler(lidatube_module)
    handler.config.CONFIG_FOLDER = str(tmp_path)
    (tmp_path / "lidarr_cache.json").write_text("{not-json")

    handler._load_lidarr_cache()

    handler.general_logger.error.assert_called_once()


def test_get_wanted_albums_handles_non_200_and_emits_toast(lidatube_module, monkeypatch):
    handler = build_data_handler(lidatube_module)
    emit_mock = Mock()
    monkeypatch.setattr(lidatube_module.socketio, "emit", emit_mock)

    close_called = []
    response = FakeResponse(503, {"records": []}, "Service unavailable")
    response.close = lambda: close_called.append(1)
    handler.lidarr_client.get_wanted_albums.return_value = response

    handler.get_wanted_albums_from_lidarr()

    assert close_called == [1]
    assert any(call.args[0] == "new_toast_msg" for call in emit_mock.call_args_list)
    assert handler.lidarr_status == "complete"


def test_artist_prefetch_retries_then_succeeds(lidatube_module, monkeypatch):
    """Artist fetch retries on failure and succeeds before exhausting attempts."""
    handler = build_data_handler(lidatube_module)
    handler._ARTIST_RETRY_WAIT = 0
    monkeypatch.setattr(lidatube_module.socketio, "emit", Mock())

    attempts = []

    def fake_get_artists(page, page_size=1000):
        attempts.append(page)
        if len(attempts) < 2:
            raise ConnectionError("timeout")
        return FakeResponse(200, [{"id": 1, "artistName": "Artist A", "path": "/music/A"}])

    handler.lidarr_client.get_artists_page.side_effect = fake_get_artists
    handler.lidarr_client.get_wanted_albums.return_value = FakeResponse(200, {"records": []})

    handler.get_wanted_albums_from_lidarr()

    assert handler.lidarr_status == "complete"
    assert len(attempts) == 2
    assert len(handler.lidarr_items) == 0


def test_artist_prefetch_exhausts_retries_sets_error(lidatube_module, monkeypatch):
    """Artist fetch sets error status after all retry attempts fail."""
    handler = build_data_handler(lidatube_module)
    handler._ARTIST_RETRY_WAIT = 0
    emit_mock = Mock()
    monkeypatch.setattr(lidatube_module.socketio, "emit", emit_mock)

    handler.lidarr_client.get_artists_page.side_effect = ConnectionError("timeout")

    handler.get_wanted_albums_from_lidarr()

    assert handler.lidarr_status == "error"
    assert handler.lidarr_client.get_artists_page.call_count == 3


def test_artist_prefetch_failure_sets_error_status(lidatube_module, monkeypatch):
    """Non-200 from get_artists_page aborts the scan and sets status to error."""
    handler = build_data_handler(lidatube_module)
    handler._ARTIST_RETRY_WAIT = 0
    emit_mock = Mock()
    monkeypatch.setattr(lidatube_module.socketio, "emit", emit_mock)

    handler.lidarr_client.get_artists_page.return_value = FakeResponse(503, None, "Service unavailable")

    handler.get_wanted_albums_from_lidarr()

    assert handler.lidarr_status == "error"
    assert handler.lidarr_items == []
    assert any(call.args[0] == "new_toast_msg" for call in emit_mock.call_args_list)


def test_artist_prefetch_paginated_response(lidatube_module, monkeypatch):
    """Artist endpoint returning paginated records object is handled correctly."""
    handler = build_data_handler(lidatube_module)
    monkeypatch.setattr(lidatube_module.socketio, "emit", Mock())

    pages = {
        1: {"records": [{"id": 1, "artistName": "Artist A", "path": "/music/A"}]},
        2: {"records": []},
    }
    handler.lidarr_client.get_artists_page.side_effect = lambda page, page_size=1000: FakeResponse(200, pages[page])
    handler.lidarr_client.get_wanted_albums.return_value = FakeResponse(200, {"records": []})

    handler.get_wanted_albums_from_lidarr()

    assert handler.lidarr_status == "complete"
    handler.lidarr_client.get_artists_page.assert_called_with(2)


def test_artist_prefetch_flat_array_response(lidatube_module, monkeypatch):
    """Artist endpoint returning a flat array (non-paginated Lidarr builds) is handled correctly."""
    handler = build_data_handler(lidatube_module)
    monkeypatch.setattr(lidatube_module.socketio, "emit", Mock())

    handler.lidarr_client.get_artists_page.return_value = FakeResponse(200, [
        {"id": 5, "artistName": "Flat Artist", "path": "/music/flat"},
    ])
    handler.lidarr_client.get_wanted_albums.return_value = FakeResponse(200, {"records": [
        {
            "id": 99,
            "title": "Flat Album",
            "releaseDate": "2020-01-01T00:00:00Z",
            "genres": [],
            "artistId": 5,
            "releases": [{"id": 999}],
        }
    ]})
    handler.lidarr_client.get_tracks_for_album.return_value = FakeResponse(200, [])

    def fake_get_wanted(page, page_size=1000):
        return FakeResponse(200, {"records": [
            {"id": 99, "title": "Flat Album", "releaseDate": "2020-01-01T00:00:00Z",
             "genres": [], "artistId": 5, "releases": [{"id": 999}]}
        ] if page == 1 else []})

    handler.lidarr_client.get_wanted_albums.side_effect = fake_get_wanted

    handler.get_wanted_albums_from_lidarr()

    # Flat array: get_artists_page should only be called once (no further pages)
    assert handler.lidarr_client.get_artists_page.call_count == 1
    assert handler.lidarr_items[0]["artist"] == "Flat Artist"
    assert handler.lidarr_items[0]["artist_path"] == "/music/flat"


def test_get_missing_tracks_retries_after_fd_exhaustion(lidatube_module, monkeypatch):
    handler = build_data_handler(lidatube_module)
    monkeypatch.setattr(lidatube_module.socketio, "emit", Mock())

    started = []

    class FakeThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            started.append(1)

    monkeypatch.setattr(lidatube_module.threading, "Thread", FakeThread)

    call_count = {"count": 0}

    def fake_get_tracks(album_id):
        call_count["count"] += 1
        if call_count["count"] == 1:
            raise OSError(24, "Too many open files")
        return FakeResponse(
            200,
            [{"title": "Track 1", "trackNumber": 1, "absoluteTrackNumber": 1, "id": 1, "hasFile": False}],
        )

    handler.lidarr_client.get_tracks_for_album.side_effect = fake_get_tracks

    album = {
        "artist": "Retry Artist",
        "album_name": "Retry Album",
        "album_id": 10,
        "missing_tracks": [],
        "track_count": 0,
        "missing_count": 0,
        "scan_ready": False,
        "scan_in_progress": False,
        "status": "",
    }

    handler.get_missing_tracks_for_album(album)

    assert call_count["count"] == 2
    assert started == [1]
    assert album["scan_ready"] is True
    assert album["missing_count"] == 1


def test_wait_for_album_scan_data_returns_false_when_not_busy(lidatube_module, monkeypatch):
    handler = build_data_handler(lidatube_module)
    monkeypatch.setattr(handler, "_emit_ytdlp_update", Mock())
    handler.lidarr_status = "complete"

    req_album = {"scan_ready": False, "scan_in_progress": True, "status": ""}

    result = handler._wait_for_album_scan_data(req_album)

    assert result is False
    assert req_album["status"] == "Waiting for refresh data"


def test_find_link_and_download_marks_album_incomplete_when_links_missing(lidatube_module, monkeypatch):
    handler = build_data_handler(lidatube_module)
    monkeypatch.setattr(lidatube_module.socketio, "emit", Mock())
    monkeypatch.setattr(handler, "_wait_for_album_scan_data", lambda _: True)
    monkeypatch.setattr(handler, "_link_finder", lambda _: None)
    monkeypatch.setattr(lidatube_module.os.path, "exists", lambda _: True)

    req_album = {
        "artist": "Artist",
        "album_name": "Album",
        "artist_path": "/music/Artist",
        "album_folder": "Album (2024)",
        "missing_count": 2,
        "missing_tracks": [
            {
                "artist": "Artist",
                "track_title": "Track One",
                "track_number": 1,
                "absolute_track_number": 1,
                "track_id": 1,
                "link": "https://example.com/1",
                "title_of_link": "Track One",
            },
            {
                "artist": "Artist",
                "track_title": "Track Two",
                "track_number": 2,
                "absolute_track_number": 2,
                "track_id": 2,
                "link": "",
                "title_of_link": "",
            },
        ],
        "status": "",
    }
    handler.ytdlp_items = [req_album]

    handler.find_link_and_download(req_album)

    assert req_album["status"] == "Album Incomplete"


def test_find_link_and_download_marks_download_failed_when_all_fail(lidatube_module, monkeypatch):
    handler = build_data_handler(lidatube_module)
    monkeypatch.setattr(lidatube_module.socketio, "emit", Mock())
    monkeypatch.setattr(handler, "_wait_for_album_scan_data", lambda _: True)
    monkeypatch.setattr(handler, "_link_finder", lambda _: None)
    monkeypatch.setattr(lidatube_module.os.path, "exists", lambda _: False)
    handler.downloader.download.return_value = False

    req_album = {
        "artist": "Artist",
        "album_name": "Album",
        "artist_path": "/music/Artist",
        "album_folder": "Album (2024)",
        "missing_count": 1,
        "missing_tracks": [
            {
                "artist": "Artist",
                "track_title": "Track One",
                "track_number": 1,
                "absolute_track_number": 1,
                "track_id": 1,
                "link": "https://example.com/1",
                "title_of_link": "Track One",
            }
        ],
        "status": "",
    }
    handler.ytdlp_items = [req_album]

    handler.find_link_and_download(req_album)

    assert req_album["status"] == "Download Failed"


def test_stop_ytdlp_cancels_pending_futures_and_marks_unprocessed(lidatube_module, monkeypatch):
    handler = build_data_handler(lidatube_module)
    monkeypatch.setattr(handler, "_emit_ytdlp_update", Mock())

    class FakeFuture:
        def __init__(self, done_state):
            self._done_state = done_state
            self.cancel_called = False

        def done(self):
            return self._done_state

        def cancel(self):
            self.cancel_called = True

    pending = FakeFuture(False)
    finished = FakeFuture(True)
    handler.ytdlp_futures = [pending, finished]
    handler.ytdlp_items = [{"status": "Done"}, {"status": "Queued"}]
    handler.index = 1

    handler.stop_ytdlp()

    assert pending.cancel_called is True
    assert finished.cancel_called is False
    assert handler.ytdlp_items[1]["status"] == "Download Stopped"
    assert handler.ytdlp_status == "stopped"


def test_reset_ytdlp_clears_queue_and_completion(lidatube_module, monkeypatch):
    handler = build_data_handler(lidatube_module)
    monkeypatch.setattr(lidatube_module.socketio, "emit", Mock())
    monkeypatch.setattr(handler, "_emit_ytdlp_update", Mock())

    class FakeFuture:
        def __init__(self):
            self.cancel_called = False

        def done(self):
            return False

        def cancel(self):
            self.cancel_called = True

    future = FakeFuture()
    handler.ytdlp_futures = [future]
    handler.ytdlp_items = [{"status": "Queued"}]
    handler.percent_completion = 42

    handler.reset_ytdlp()

    assert future.cancel_called is True
    assert handler.ytdlp_items == []
    assert handler.ytdlp_status == "idle"
    assert handler.index == 0
    assert handler.percent_completion == 0


def test_reset_lidarr_clears_cache_and_restores_idle_state(lidatube_module, monkeypatch, tmp_path):
    handler = build_data_handler(lidatube_module)
    emit_mock = Mock()
    monkeypatch.setattr(lidatube_module.socketio, "emit", emit_mock)
    monkeypatch.setattr(handler, "_emit_lidarr_update", Mock())

    cache_path = tmp_path / "lidarr_cache.json"
    cache_path.write_text(json.dumps({"lidarr_items": [{"artist": "A"}]}))
    handler.config.CONFIG_FOLDER = str(tmp_path)

    class FakeFuture:
        def __init__(self, done_state=False):
            self._done_state = done_state
            self.cancel_called = False

        def done(self):
            return self._done_state

        def cancel(self):
            self.cancel_called = True

    pending = FakeFuture(done_state=False)
    completed = FakeFuture(done_state=True)
    handler.lidarr_futures = [pending, completed]
    handler.lidarr_items = [{"artist": "Artist", "album_name": "Album", "missing_tracks": []}]
    handler.lidarr_status = "busy"
    handler.lidarr_scan_progress = {
        "phase": "Fetching missing tracks",
        "pages_scanned": 4,
        "albums_discovered": 10,
        "albums_processed": 8,
        "albums_total": 10,
        "percent": 80,
    }

    handler.reset_lidarr()

    assert handler.lidarr_stop_event.is_set() is True
    assert pending.cancel_called is True
    assert completed.cancel_called is False
    assert handler.lidarr_futures == []
    assert handler.lidarr_items == []
    assert handler.lidarr_status == "idle"
    assert handler.lidarr_scan_progress == {
        "phase": "Idle",
        "pages_scanned": 0,
        "albums_discovered": 0,
        "albums_processed": 0,
        "albums_total": 0,
        "percent": 0,
    }
    assert cache_path.exists() is False
    handler._emit_lidarr_update.assert_called_once()
    assert any(call.args[0] == "new_toast_msg" and call.args[1]["title"] == "Lidarr Reset" for call in emit_mock.call_args_list)


def test_apply_album_track_links_returns_true_when_stop_is_set(lidatube_module):
    handler = build_data_handler(lidatube_module)
    handler.ytdlp_stop_event.set()

    req_album = {
        "missing_tracks": [
            {"track_title": "Track A", "link": "", "title_of_link": ""},
        ]
    }
    album_details = {"tracks": [{"title": "Track A", "videoId": "abc"}]}

    should_stop = handler._apply_album_track_links(req_album, album_details)

    assert should_stop is True
    assert req_album["missing_tracks"][0]["link"] == ""


def test_get_album_links_falls_back_to_top_result(lidatube_module, monkeypatch):
    handler = build_data_handler(lidatube_module)
    handler.config.fallback_to_top_result = True
    monkeypatch.setattr(_matcher, "album_matcher", lambda *args, **kwargs: None)

    apply_mock = Mock(return_value=False)
    monkeypatch.setattr(handler, "_apply_album_track_links", apply_mock)

    class FakeYTMusic:
        def search(self, query, filter, limit):
            return [{"title": "Fallback Album", "browseId": "album-1"}]

        def get_album(self, browse_id):
            assert browse_id == "album-1"
            return {"tracks": [{"title": "Track A", "videoId": "vid-1"}]}

    req_album = {"artist": "A", "album_name": "B", "status": "", "missing_tracks": []}

    handler._get_album_links(req_album, "A", "B", "a", "b", "A - B", FakeYTMusic())

    assert req_album["status"] == "Album Found"
    apply_mock.assert_called_once_with(req_album, {"tracks": [{"title": "Track A", "videoId": "vid-1"}]})


def test_get_song_links_uses_fallback_to_top_result(lidatube_module, monkeypatch):
    handler = build_data_handler(lidatube_module)
    handler.config.fallback_to_top_result = True
    monkeypatch.setattr(_matcher, "song_matcher", lambda *args, **kwargs: None)

    class FakeYTMusic:
        def search(self, query, filter, limit):
            return [{"title": "Fallback Song", "videoId": "vid-123"}]

    req_album = {
        "artist": "Artist",
        "album_name": "Album",
        "missing_tracks": [
            {"artist": "Artist", "track_title": "Track One", "link": "", "title_of_link": "", "duration_ms": 0},
        ],
    }

    handler._get_song_links(req_album, "Artist", "artist", FakeYTMusic())

    track = req_album["missing_tracks"][0]
    assert track["link"] == "https://www.youtube.com/watch?v=vid-123"
    assert track["title_of_link"] == "Fallback Song"


def test_get_song_links_secondary_ytdlp_mode_uses_webpage_url(lidatube_module, monkeypatch):
    handler = build_data_handler(lidatube_module)
    handler.config.secondary_search = "YTDLP"
    monkeypatch.setattr(_matcher, "song_matcher", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        _matcher,
        "song_matcher_yt",
        lambda *args, **kwargs: {"title": "YT Match", "webpage_url": "https://yt.example/watch?v=1", "link": "unused"},
    )
    monkeypatch.setattr(handler, "_yt_search", lambda _: [{"title": "YT Match", "webpage_url": "https://yt.example/watch?v=1"}])

    class FakeYTMusic:
        def search(self, query, filter, limit):
            return []

    req_album = {
        "artist": "Artist",
        "album_name": "Album",
        "missing_tracks": [
            {"artist": "Artist", "track_title": "Track One", "link": "", "title_of_link": "", "duration_ms": 0},
        ],
    }

    handler._get_song_links_secondary(req_album, "Artist", "artist", FakeYTMusic())

    track = req_album["missing_tracks"][0]
    assert track["link"] == "https://yt.example/watch?v=1"
    assert track["title_of_link"] == "YT Match"


def test_yt_search_returns_empty_list_for_unknown_secondary_mode(lidatube_module):
    handler = build_data_handler(lidatube_module)
    handler.config.secondary_search = "UNKNOWN"

    assert handler._yt_search("artist - song") == []


def test_yt_search_ytdlp_returns_empty_when_stop_requested(lidatube_module, monkeypatch):
    handler = build_data_handler(lidatube_module)
    handler.config.secondary_search = "YTDLP"
    handler.ytdlp_stop_event.set()

    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def extract_info(self, query_text, download=False):
            return {"entries": [{"webpage_url": "https://yt.example/watch?v=1"}]}

    monkeypatch.setattr(lidatube_module.yt_dlp, "YoutubeDL", FakeYDL)

    assert handler._yt_search("artist - song") == []


def test_update_settings_socket_route_forwards_payload(lidatube_module, monkeypatch):
    update_mock = Mock()
    monkeypatch.setattr(lidatube_module.data_handler, "update_settings", update_mock)
    payload = {"minimum_match_ratio": "90"}

    lidatube_module.update_settings(payload)

    update_mock.assert_called_once_with(payload)


def test_add_to_download_list_socket_route_forwards_payload(lidatube_module, monkeypatch):
    add_mock = Mock()
    monkeypatch.setattr(lidatube_module.data_handler, "add_items_to_download", add_mock)
    payload = [0, 2, 5]

    lidatube_module.add_to_download_list(payload)


def _make_track(n):
    return {
        "artist": "Artist",
        "track_title": f"Track {n}",
        "track_number": n,
        "absolute_track_number": n,
        "track_id": n,
        "link": f"https://example.com/{n}",
        "title_of_link": f"Track {n}",
    }


def _make_req_album(tracks):
    return {
        "artist": "Artist",
        "album_name": "Album",
        "artist_path": "/music/Artist",
        "album_folder": "Album (2024)",
        "track_count": len(tracks),
        "missing_count": len(tracks),
        "missing_tracks": tracks,
        "status": "",
    }


def test_find_link_and_download_status_is_stopped_when_download_cancelled(lidatube_module, monkeypatch):
    """When stop event is set by a cancelled download, status should be 'Download Stopped'
    and subsequent tracks should not be attempted."""
    handler = build_data_handler(lidatube_module)
    monkeypatch.setattr(lidatube_module.socketio, "emit", Mock())
    monkeypatch.setattr(handler, "_wait_for_album_scan_data", lambda _: True)
    monkeypatch.setattr(handler, "_link_finder", lambda _: None)
    monkeypatch.setattr(lidatube_module.os.path, "exists", lambda _: False)

    def cancel_on_first_download(*args, **kwargs):
        handler.ytdlp_stop_event.set()
        return False

    handler.downloader.download.side_effect = cancel_on_first_download

    req_album = _make_req_album([_make_track(1), _make_track(2)])
    handler.ytdlp_items = [req_album]

    handler.find_link_and_download(req_album)

    assert handler.downloader.download.call_count == 1, "Should stop after first cancelled download"
    assert req_album["status"] == "Download Stopped"


def test_find_link_and_download_status_is_stopped_when_stop_set_after_link_finder(lidatube_module, monkeypatch):
    """When stop event is set after _link_finder, status should be 'Download Stopped'."""
    handler = build_data_handler(lidatube_module)
    monkeypatch.setattr(lidatube_module.socketio, "emit", Mock())
    monkeypatch.setattr(handler, "_wait_for_album_scan_data", lambda _: True)

    def link_finder_then_stop(_):
        handler.ytdlp_stop_event.set()

    monkeypatch.setattr(handler, "_link_finder", link_finder_then_stop)

    req_album = _make_req_album([_make_track(1)])
    handler.ytdlp_items = [req_album]

    handler.find_link_and_download(req_album)

    assert handler.downloader.download.call_count == 0, "Should not download if stop set after link finder"
    assert req_album["status"] == "Download Stopped"


def test_link_finder_returns_without_api_call_if_stop_set_before_semaphore(lidatube_module, monkeypatch):
    """_link_finder should exit without making API calls when stop event is set and semaphore is unavailable."""
    handler = build_data_handler(lidatube_module)
    # Make semaphore impossible to acquire
    handler._ytmusic_semaphore = threading.Semaphore(0)
    handler.ytdlp_stop_event.set()

    ytmusic_created = []

    class FakeYTMusic:
        def __init__(self):
            ytmusic_created.append(1)

    monkeypatch.setattr(lidatube_module, "YTMusic", FakeYTMusic)

    req_album = _make_req_album([_make_track(1)])
    handler._link_finder(req_album)

    assert len(ytmusic_created) == 0, "YTMusic should not be created when stop is set"


def test_link_finder_exits_when_stop_set_while_waiting_for_semaphore(lidatube_module, monkeypatch):
    """_link_finder should exit when stop event is set while waiting for a full semaphore."""
    handler = build_data_handler(lidatube_module)
    # Semaphore has 0 permits — blocks immediately
    handler._ytmusic_semaphore = threading.Semaphore(0)

    ytmusic_created = []

    class FakeYTMusic:
        def __init__(self):
            ytmusic_created.append(1)

    monkeypatch.setattr(lidatube_module, "YTMusic", FakeYTMusic)

    def set_stop_after_delay():
        time.sleep(0.3)
        handler.ytdlp_stop_event.set()

    t = threading.Thread(target=set_stop_after_delay)
    t.start()

    req_album = _make_req_album([_make_track(1)])
    handler._link_finder(req_album)
    t.join()

    assert len(ytmusic_created) == 0, "YTMusic should not be created when stop is set during semaphore wait"
