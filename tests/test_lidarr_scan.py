import json
import os
import threading
from unittest.mock import Mock

import pytest

import lidarr_scan
from fd_governor import FdGovernor
from lidarr_scan import LidarrScanner


@pytest.fixture(autouse=True)
def _isolated_config_folder(tmp_path, monkeypatch):
    """The cache lives in config.CONFIG_FOLDER; keep test runs out of the repo's config/."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()


class FakeResponse:
    def __init__(self, status_code, payload, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload

    def close(self):
        pass


def build_scanner(on_album_scanned=None):
    config = Mock()
    config.lidarr_scan_thread_limit = 8
    config.CONFIG_FOLDER = "config"
    lidarr_client = Mock()
    lidarr_client.get_artists_page.return_value = FakeResponse(200, [])
    fd = FdGovernor(Mock())
    fd.fd_limit = None
    return LidarrScanner(
        config, lidarr_client, fd, emit=Mock(), logger=Mock(),
        download_stop_event=threading.Event(), on_album_scanned=on_album_scanned,
    )


def _unscanned_album():
    return {
        "artist": "Test Artist", "album_name": "Test Album", "album_id": 42,
        "missing_tracks": [], "track_count": 0, "missing_count": 0,
        "scan_ready": False, "scan_in_progress": False, "status": "",
    }


def test_fetch_wanted_albums_populates_missing_tracks(monkeypatch):
    scanner = build_scanner()
    emit_mock = Mock()
    scanner.emit = emit_mock

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
            "secondaryTypes": ["Live"],
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

    scanner.lidarr_client.get_artists_page.return_value = FakeResponse(200, [
        {"id": 10, "artistName": "Alpha", "path": "/music/Alpha", "genres": ["Classical", "Romantic"]},
        {"id": 20, "artistName": "Zulu", "path": "/music/Zulu"},
    ])
    scanner.lidarr_client.get_wanted_albums.side_effect = fake_get_wanted
    scanner.lidarr_client.get_tracks_for_album.side_effect = fake_get_tracks

    scanner.fetch_wanted_albums()

    assert scanner.status == "complete"
    assert [item["artist"] for item in scanner.items] == ["Alpha", "Zulu"]

    alpha_album = scanner.items[0]
    zulu_album = scanner.items[1]

    assert alpha_album["album_name"] == "Alpha-Album-"
    assert alpha_album["track_count"] == 2
    assert alpha_album["missing_count"] == 1
    assert alpha_album["missing_tracks"][0]["track_title"] == "Song A"
    assert alpha_album["album_secondary_types"] == ["Live"]
    assert alpha_album["artist_genres"] == "Classical, Romantic"

    assert zulu_album["album_name"] == "Zulu+Album!"
    assert zulu_album["track_count"] == 1
    assert zulu_album["missing_count"] == 1
    assert zulu_album["missing_tracks"][0]["track_title"] == "Song Z"
    assert zulu_album["album_secondary_types"] == []
    assert zulu_album["artist_genres"] == ""

    assert emit_mock.call_args_list[-1].args[0] == "lidarr_update"
    assert emit_mock.call_args_list[-1].args[1]["status"] == "complete"


def test_emit_update_strips_missing_tracks(monkeypatch):
    """lidarr_update socket event must not include missing_tracks (large, not needed by UI)."""
    scanner = build_scanner()
    emitted = {}
    scanner.emit = lambda event, data: emitted.update({event: data})

    scanner.items = [
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
    scanner.emit_update()

    assert "lidarr_update" in emitted
    item = emitted["lidarr_update"]["data"][0]
    assert "missing_tracks" not in item


def test_emit_update_filters_complete_albums(monkeypatch):
    """Albums with missing_count=0 and scan_ready=True are excluded from the emit to keep payload small."""
    scanner = build_scanner()
    emitted = {}
    scanner.emit = lambda event, data: emitted.update({event: data})

    scanner.items = [
        {"artist": "A", "album_name": "complete", "scan_ready": True, "missing_count": 0, "missing_tracks": []},
        {"artist": "B", "album_name": "has_missing", "scan_ready": True, "missing_count": 2, "missing_tracks": []},
        {"artist": "C", "album_name": "still_scanning", "scan_ready": False, "missing_count": 0, "missing_tracks": []},
    ]
    scanner.emit_update()

    data = emitted["lidarr_update"]["data"]
    names = [item["album_name"] for item in data]
    assert "complete" not in names
    assert "has_missing" in names
    assert "still_scanning" in names


def test_emit_update_includes_index_and_total_count(monkeypatch):
    """Each emitted item carries its original lidarr_items index; total_count reflects full list size."""
    scanner = build_scanner()
    emitted = {}
    scanner.emit = lambda event, data: emitted.update({event: data})

    scanner.items = [
        {"artist": "A", "album_name": "complete", "scan_ready": True, "missing_count": 0, "missing_tracks": []},
        {"artist": "B", "album_name": "has_missing", "scan_ready": True, "missing_count": 1, "missing_tracks": []},
    ]
    scanner.emit_update()

    payload = emitted["lidarr_update"]
    assert payload["total_count"] == 2
    assert len(payload["data"]) == 1
    assert payload["data"][0]["index"] == 1


def test_save_cache_is_atomic(monkeypatch, tmp_path):
    """Cache write uses a .tmp file then renames to prevent corruption on kill."""
    scanner = build_scanner()
    scanner.config.CONFIG_FOLDER = str(tmp_path)
    scanner.items = [{"artist": "X", "album_name": "Y", "missing_tracks": []}]

    rename_calls = []
    real_replace = os.replace
    monkeypatch.setattr(lidarr_scan.os, "replace", lambda src, dst: rename_calls.append((src, dst)) or real_replace(src, dst))

    scanner.save_cache()

    assert len(rename_calls) == 1
    src, dst = rename_calls[0]
    assert src.endswith(".tmp")
    assert not src.endswith(".tmp") or dst == src[: -len(".tmp")]


def test_cache_checkpointed_periodically(monkeypatch):
    """Growing caches are checkpointed periodically instead of rewritten per page."""
    scanner = build_scanner()
    emit_mock = Mock()
    scanner.emit = emit_mock

    save_calls = []
    monkeypatch.setattr(scanner, "save_cache", lambda: save_calls.append(1))

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
        return FakeResponse(200, {"records": page_one_records if page <= 6 else []})

    scanner.lidarr_client.get_artists_page.return_value = FakeResponse(200, [
        {"id": 1, "artistName": "Artist A", "path": "/music/A"},
    ])
    scanner.lidarr_client.get_wanted_albums.side_effect = fake_get_wanted
    scanner.lidarr_client.get_tracks_for_album.return_value = FakeResponse(200, [])

    scanner.fetch_wanted_albums()

    # One checkpoint at page 5 plus the final completed-state write.
    assert len(save_calls) == 2


def test_response_closed_on_non_200_track_fetch(monkeypatch):
    """Response must be closed even when Lidarr returns a non-200 status (prevents FD leak)."""
    scanner = build_scanner()

    close_called = []
    error_response = FakeResponse(500, None, "Server Error")
    error_response.close = lambda: close_called.append(1)

    scanner.lidarr_client.get_tracks_for_album.return_value = error_response

    album = {
        "artist": "A", "album_name": "B", "album_id": 1,
        "missing_tracks": [], "track_count": 0, "missing_count": 0,
        "scan_ready": False, "scan_in_progress": False, "status": "",
    }
    scanner.scan_album_tracks(album)

    assert len(close_called) >= 1
    assert album["scan_state"] == "error"
    assert album["scan_ready"] is False
    assert album["scan_in_progress"] is False
    assert album["missing_count"] == 0
    assert "500" in album["scan_error"]


def test_drain_futures_releases_completed_references():
    scanner = build_scanner()
    future = Mock()
    future.done.return_value = True
    future.result.return_value = True
    album = {"scan_state": "complete"}
    future_map = {future: album}
    scanner.futures = [future]

    processed, failed = scanner._drain_futures(future_map)

    assert (processed, failed) == (1, 0)
    assert future_map == {}
    assert scanner.futures == []


def test_load_cache_restores_cached_state(tmp_path):
    scanner = build_scanner()
    scanner.config.CONFIG_FOLDER = str(tmp_path)

    cache_payload = {
        "lidarr_items": [{"artist": "A", "album_name": "B"}],
        "lidarr_scan_progress": {"phase": "Fetching", "albums_processed": 5, "albums_total": 10, "percent": 50},
    }
    (tmp_path / "lidarr_cache.json").write_text(json.dumps(cache_payload))

    scanner.load_cache()

    assert scanner.status == "complete"
    assert scanner.items[0]["artist"] == "A"
    assert scanner.items[0]["album_name"] == "B"
    assert scanner.items[0]["scan_state"] == "pending"
    assert scanner.progress["phase"] == "Complete (cached)"
    assert scanner.progress["albums_processed"] == 5


def test_incomplete_lidarr_cache_remains_incomplete_after_restart(tmp_path):
    scanner = build_scanner()
    scanner.config.CONFIG_FOLDER = str(tmp_path)
    scanner.items = [{"artist": "A", "album_name": "B", "scan_ready": False}]
    scanner._set_album_scan_state(scanner.items[0], "error", "track API failed")
    scanner._set_progress(phase="Incomplete — Lidarr error on page 2", pages_scanned=1)
    scanner._set_state("error", error="Lidarr page 2 returned 500")
    scanner.save_cache()

    payload = json.loads((tmp_path / "lidarr_cache.json").read_text())
    assert payload["schema_version"] == 2
    assert payload["lidarr_scan_state"]["complete"] is False
    assert payload["lidarr_scan_state"]["last_successful_page"] == 1

    restarted = build_scanner()
    restarted.config.CONFIG_FOLDER = str(tmp_path)
    restarted.load_cache()

    assert restarted.status == "error"
    assert restarted.progress["phase"].startswith("Incomplete")
    assert restarted.items[0]["scan_state"] == "error"
    assert restarted.items[0]["scan_ready"] is False


def test_load_cache_logs_error_on_invalid_json(tmp_path):
    scanner = build_scanner()
    scanner.config.CONFIG_FOLDER = str(tmp_path)
    (tmp_path / "lidarr_cache.json").write_text("{not-json")

    scanner.load_cache()

    scanner.logger.error.assert_called_once()


def test_get_wanted_albums_handles_non_200_and_emits_toast(monkeypatch):
    scanner = build_scanner()
    emit_mock = Mock()
    scanner.emit = emit_mock
    monkeypatch.setattr(scanner.stop_event, "wait", lambda *a, **k: False)

    close_called = []
    response = FakeResponse(503, {"records": []}, "Service unavailable")
    response.close = lambda: close_called.append(1)
    scanner.lidarr_client.get_wanted_albums.return_value = response

    scanner.fetch_wanted_albums()

    # The page is retried before giving up, and every response is closed.
    assert len(close_called) == 3
    assert any(call.args[0] == "new_toast_msg" for call in emit_mock.call_args_list)
    # A failed fetch must NOT be reported as a finished scan.
    assert scanner.status == "error"


def test_wanted_fetch_error_midway_is_not_reported_complete(monkeypatch):
    """Regression: Lidarr 500 on page N truncated the album list but reported 'Complete'.

    A busy Lidarr (concurrent rescan) returned 500 on page 26; the loop broke and the
    partial 25,000-album list was cached as a finished scan, hiding ~64k albums.
    """
    scanner = build_scanner()
    monkeypatch.setattr(scanner.stop_event, "wait", lambda *a, **k: False)
    monkeypatch.setattr(scanner, "scan_album_tracks", lambda item: None)
    monkeypatch.setattr(scanner, "save_cache", Mock())

    def album(i):
        return {"artistId": 1, "id": i, "title": f"Album {i}", "releaseDate": "2020-01-01T00:00:00Z",
                "genres": [], "releases": [{"id": 100 + i}]}

    calls = {"n": 0}

    def fake_get_wanted(page, page_size):
        calls["n"] += 1
        if page == 1:
            return FakeResponse(200, {"records": [album(1), album(2)]})
        return FakeResponse(500, {}, "Internal Server Error")  # page 2 always fails

    scanner.lidarr_client.get_wanted_albums.side_effect = fake_get_wanted
    scanner.fetch_wanted_albums()

    assert len(scanner.items) == 2          # page 1 kept
    assert scanner.status == "error"        # not "complete"
    assert "Incomplete" in scanner.progress["phase"]
    assert calls["n"] == 1 + 3                     # page 1, then page 2 retried 3x


def test_artist_prefetch_retries_then_succeeds(monkeypatch):
    """Artist fetch retries on failure and succeeds before exhausting attempts."""
    scanner = build_scanner()
    scanner._ARTIST_RETRY_WAIT = 0

    attempts = []

    def fake_get_artists(page, page_size=1000):
        attempts.append(page)
        if len(attempts) < 2:
            raise ConnectionError("timeout")
        return FakeResponse(200, [{"id": 1, "artistName": "Artist A", "path": "/music/A"}])

    scanner.lidarr_client.get_artists_page.side_effect = fake_get_artists
    scanner.lidarr_client.get_wanted_albums.return_value = FakeResponse(200, {"records": []})

    scanner.fetch_wanted_albums()

    assert scanner.status == "complete"
    assert len(attempts) == 2
    assert len(scanner.items) == 0


def test_artist_prefetch_exhausts_retries_sets_error(monkeypatch):
    """Artist fetch sets error status after all retry attempts fail."""
    scanner = build_scanner()
    scanner._ARTIST_RETRY_WAIT = 0
    emit_mock = Mock()
    scanner.emit = emit_mock

    scanner.lidarr_client.get_artists_page.side_effect = ConnectionError("timeout")

    scanner.fetch_wanted_albums()

    assert scanner.status == "error"
    assert scanner.lidarr_client.get_artists_page.call_count == 3


def test_artist_prefetch_failure_sets_error_status(monkeypatch):
    """Non-200 from get_artists_page aborts the scan and sets status to error."""
    scanner = build_scanner()
    scanner._ARTIST_RETRY_WAIT = 0
    emit_mock = Mock()
    scanner.emit = emit_mock

    scanner.lidarr_client.get_artists_page.return_value = FakeResponse(503, None, "Service unavailable")

    scanner.fetch_wanted_albums()

    assert scanner.status == "error"
    assert scanner.items == []
    assert any(call.args[0] == "new_toast_msg" for call in emit_mock.call_args_list)


def test_artist_prefetch_paginated_response(monkeypatch):
    """Artist endpoint returning paginated records object is handled correctly."""
    scanner = build_scanner()

    pages = {
        1: {"records": [{"id": 1, "artistName": "Artist A", "path": "/music/A"}]},
        2: {"records": []},
    }
    scanner.lidarr_client.get_artists_page.side_effect = lambda page, page_size=1000: FakeResponse(200, pages[page])
    scanner.lidarr_client.get_wanted_albums.return_value = FakeResponse(200, {"records": []})

    scanner.fetch_wanted_albums()

    assert scanner.status == "complete"
    scanner.lidarr_client.get_artists_page.assert_called_with(2)


def test_artist_prefetch_flat_array_response(monkeypatch):
    """Artist endpoint returning a flat array (non-paginated Lidarr builds) is handled correctly."""
    scanner = build_scanner()

    scanner.lidarr_client.get_artists_page.return_value = FakeResponse(200, [
        {"id": 5, "artistName": "Flat Artist", "path": "/music/flat"},
    ])
    scanner.lidarr_client.get_wanted_albums.return_value = FakeResponse(200, {"records": [
        {
            "id": 99,
            "title": "Flat Album",
            "releaseDate": "2020-01-01T00:00:00Z",
            "genres": [],
            "artistId": 5,
            "releases": [{"id": 999}],
        }
    ]})
    scanner.lidarr_client.get_tracks_for_album.return_value = FakeResponse(200, [])

    def fake_get_wanted(page, page_size=1000):
        return FakeResponse(200, {"records": [
            {"id": 99, "title": "Flat Album", "releaseDate": "2020-01-01T00:00:00Z",
             "genres": [], "artistId": 5, "releases": [{"id": 999}]}
        ] if page == 1 else []})

    scanner.lidarr_client.get_wanted_albums.side_effect = fake_get_wanted

    scanner.fetch_wanted_albums()

    # Flat array: get_artists_page should only be called once (no further pages)
    assert scanner.lidarr_client.get_artists_page.call_count == 1
    assert scanner.items[0]["artist"] == "Flat Artist"
    assert scanner.items[0]["artist_path"] == "/music/flat"


def test_scan_album_tracks_retries_after_fd_exhaustion(monkeypatch):
    scanner = build_scanner()

    scanner.fd.signal_exhaustion_in_background = Mock()

    call_count = {"count": 0}

    def fake_get_tracks(album_id):
        call_count["count"] += 1
        if call_count["count"] == 1:
            raise OSError(24, "Too many open files")
        return FakeResponse(
            200,
            [{"title": "Track 1", "trackNumber": 1, "absoluteTrackNumber": 1, "id": 1, "hasFile": False}],
        )

    scanner.lidarr_client.get_tracks_for_album.side_effect = fake_get_tracks

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

    scanner.scan_album_tracks(album)

    assert call_count["count"] == 2
    scanner.fd.signal_exhaustion_in_background.assert_called_once_with()
    assert album["scan_ready"] is True
    assert album["missing_count"] == 1


def test_reset_clears_cache_and_restores_idle_state(monkeypatch, tmp_path):
    scanner = build_scanner()
    emit_mock = Mock()
    scanner.emit = emit_mock
    monkeypatch.setattr(scanner, "emit_update", Mock())

    cache_path = tmp_path / "lidarr_cache.json"
    cache_path.write_text(json.dumps({"lidarr_items": [{"artist": "A"}]}))
    scanner.config.CONFIG_FOLDER = str(tmp_path)

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
    scanner.futures = [pending, completed]
    scanner.items = [{"artist": "Artist", "album_name": "Album", "missing_tracks": []}]
    scanner.status = "busy"
    scanner.progress = {
        "phase": "Fetching missing tracks",
        "pages_scanned": 4,
        "albums_discovered": 10,
        "albums_processed": 8,
        "albums_total": 10,
        "percent": 80,
    }

    scanner.reset()

    assert scanner.stop_event.is_set() is True
    assert pending.cancel_called is True
    assert completed.cancel_called is False
    assert scanner.futures == []
    assert scanner.items == []
    assert scanner.status == "idle"
    assert scanner.progress == {
        "phase": "Idle",
        "pages_scanned": 0,
        "albums_discovered": 0,
        "albums_processed": 0,
        "albums_total": 0,
        "percent": 0,
    }
    assert cache_path.exists() is False
    scanner.emit_update.assert_called_once()
    assert any(call.args[0] == "new_toast_msg" and call.args[1]["title"] == "Lidarr Reset" for call in emit_mock.call_args_list)


def test_completed_album_scan_is_reported_to_the_listener():
    scanned = []
    scanner = build_scanner(on_album_scanned=scanned.append)
    scanner.lidarr_client.get_tracks_for_album.return_value = FakeResponse(
        200, [{"title": "Track 1", "trackNumber": 1, "absoluteTrackNumber": 1, "id": 1, "hasFile": False}],
    )
    album = _unscanned_album()

    assert scanner.scan_album_tracks(album) is True

    assert scanned == [album]
    assert album["scan_ready"] is True


def test_failed_album_scan_is_not_reported_to_the_listener():
    scanned = []
    scanner = build_scanner(on_album_scanned=scanned.append)
    scanner.lidarr_client.get_tracks_for_album.return_value = FakeResponse(500, None, "Server Error")

    assert scanner.scan_album_tracks(_unscanned_album()) is False

    assert scanned == []


def test_filtered_indices_keep_albums_with_missing_or_pending_tracks_matching_the_query():
    scanner = build_scanner()
    scanner.items = [
        {"artist": "Artist", "album_name": "One", "missing_count": 1, "scan_ready": True, "checked": True},
        {"artist": "Artist", "album_name": "Complete", "missing_count": 0, "scan_ready": True, "checked": True},
        {"artist": "Other", "album_name": "Pending", "missing_count": 0, "scan_ready": False, "checked": False},
    ]

    assert scanner.filtered_indices("") == [0, 2]
    assert scanner.filtered_indices("other") == [2]
    assert scanner.filtered_indices("", checked_only=True) == [0]
