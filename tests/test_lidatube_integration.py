import importlib
import os
import sys
import threading
from unittest.mock import Mock, patch

import pytest


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
    cfg.CONFIG_FOLDER = "config"
    handler.config = cfg

    # LidarrClient mock
    handler.lidarr_client = Mock()

    return handler


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

    monkeypatch.setattr(lidatube_module, "YTMusic", FakeYTMusic)
    monkeypatch.setattr(
        handler,
        "_yt_search",
        lambda query_text: [{"title": "Artist - Track One", "link": "https://example.com/v"}],
    )

    req_album = {
        "artist": "Artist",
        "album_name": "Album",
        "missing_tracks": [
            {"artist": "Artist", "track_title": "Track One", "link": "", "title_of_link": ""},
        ],
    }

    handler._get_song_links_secondary(req_album, artist="Artist", cleaned_artist="artist")

    assert req_album["missing_tracks"][0]["link"] == "https://example.com/v"
    assert req_album["missing_tracks"][0]["title_of_link"] == "Artist - Track One"


def test_home_route_returns_html(lidatube_module):
    client = lidatube_module.app.test_client()
    response = client.get("/")

    assert response.status_code == 200
    assert b"LidaTube" in response.data


def test_get_song_links_closes_ytmusic_client(lidatube_module, monkeypatch):
    handler = build_data_handler(lidatube_module)
    emit_mock = Mock()
    monkeypatch.setattr(lidatube_module.socketio, "emit", emit_mock)

    class FakeYTMusic:
        def __init__(self):
            self.close_called = 0

        def search(self, query, filter, limit):
            return []

        def close(self):
            self.close_called += 1

    fake_client = FakeYTMusic()
    monkeypatch.setattr(lidatube_module, "YTMusic", lambda: fake_client)

    req_album = {
        "artist": "Artist",
        "album_name": "Album",
        "missing_tracks": [
            {"artist": "Artist", "track_title": "Track One", "link": "", "title_of_link": ""},
        ],
    }

    handler._get_song_links(req_album, artist="Artist", cleaned_artist="artist")

    assert fake_client.close_called == 1


def test_get_song_links_secondary_closes_ytmusic_client(lidatube_module, monkeypatch):
    handler = build_data_handler(lidatube_module)
    emit_mock = Mock()
    monkeypatch.setattr(lidatube_module.socketio, "emit", emit_mock)
    monkeypatch.setattr(handler, "_yt_search", lambda query_text: [])

    class FakeYTMusic:
        def __init__(self):
            self.close_called = 0

        def search(self, query, filter, limit):
            return []

        def close(self):
            self.close_called += 1

    fake_client = FakeYTMusic()
    monkeypatch.setattr(lidatube_module, "YTMusic", lambda: fake_client)

    req_album = {
        "artist": "Artist",
        "album_name": "Album",
        "missing_tracks": [
            {"artist": "Artist", "track_title": "Track One", "link": "", "title_of_link": ""},
        ],
    }

    handler._get_song_links_secondary(req_album, artist="Artist", cleaned_artist="artist")

    assert fake_client.close_called == 1


def test_get_album_links_closes_ytmusic_client(lidatube_module, monkeypatch):
    handler = build_data_handler(lidatube_module)
    emit_mock = Mock()
    monkeypatch.setattr(lidatube_module.socketio, "emit", emit_mock)
    monkeypatch.setattr(lidatube_module.fuzz, "ratio", lambda _left, _right: 100)

    class FakeYTMusic:
        def __init__(self):
            self.close_called = 0

        def search(self, query, filter, limit):
            return [{"browseId": "album-1"}]

        def get_album(self, browse_id):
            assert browse_id == "album-1"
            return {"tracks": [{"title": "Track One", "videoId": "vid-1"}]}

        def close(self):
            self.close_called += 1

    fake_client = FakeYTMusic()
    monkeypatch.setattr(lidatube_module, "YTMusic", lambda: fake_client)
    monkeypatch.setattr(lidatube_module._matcher, "album_matcher", lambda *_args, **_kwargs: {"browseId": "album-1"})

    req_album = {
        "artist": "Artist",
        "album_name": "Album",
        "missing_tracks": [
            {"artist": "Artist", "track_title": "Track One", "link": "", "title_of_link": ""},
        ],
        "status": "",
    }

    handler._get_album_links(
        req_album,
        artist="Artist",
        album_name="Album",
        cleaned_artist="artist",
        cleaned_album="album",
        query_text="Artist - Album",
    )

    assert fake_client.close_called == 1
    assert req_album["missing_tracks"][0]["link"] == "https://www.youtube.com/watch?v=vid-1"


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
