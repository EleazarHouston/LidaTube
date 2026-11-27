"""Putting chosen albums back on the download queue, from the API and the command line."""

import json
import threading
from unittest.mock import Mock

import pytest

import requeue_albums
from download_queue import DownloadQueue
from fd_governor import FdGovernor
from lidarr_scan import LidarrScanner
from store import Store


class Response:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload
        self.text = ""

    def json(self):
        return self._payload

    def close(self):
        pass


def _cached(album_id, **changes):
    album = {
        "artist_id": 5, "artist_path": "/music/Artist", "artist": "Artist", "album_name": f"Album {album_id}",
        "album_folder": f"Album {album_id} (2001)", "album_full_path": f"/music/Artist/Album {album_id} (2001)",
        "album_year": 2001, "album_id": album_id, "album_release_id": 900 + album_id, "album_genres": "Pop",
        "album_secondary_types": [], "artist_genres": "Pop", "track_count": 3, "missing_count": 1,
        "missing_tracks": [{"track_id": 99, "link": ""}], "checked": True, "scan_ready": True,
        "scan_in_progress": False, "scan_state": "complete", "scan_error": None, "status": "Download Complete",
    }
    album.update(changes)
    return album


def _lidarr_track(track_id, has_file=False):
    return {"id": track_id, "title": f"Track {track_id}", "trackNumber": "1", "absoluteTrackNumber": 1,
            "duration": 1000, "hasFile": has_file, "foreignRecordingId": None}


@pytest.fixture
def queue(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = Mock()
    config.thread_limit = 1
    config.batch_size = 10
    client = Mock()
    fd = FdGovernor(Mock())
    fd.fd_limit = None
    scanner = LidarrScanner(config, client, fd, Mock(), Mock())
    scanner.items = [_cached(1), _cached(2)]
    store = Store(tmp_path / "lidatube.db")
    queue = DownloadQueue(config, store, scanner, Mock(), Mock(), client, threading.Event(), Mock(), Mock())
    yield queue
    store.close()


def _items(queue):
    rows = queue.store._fetchall("SELECT * FROM queue_items ORDER BY position")
    return [dict(row, album=json.loads(row["album_json"])) for row in rows]


def _session(queue, session_id):
    return queue.store._fetchone("SELECT * FROM sessions WHERE id = ?", (session_id,))


def test_a_requeued_album_is_queued_to_have_its_tracks_read_again(queue):
    result = queue.requeue_albums({1: [11, 12]})

    (item,) = _items(queue)
    assert result == {"session_id": item["session_id"], "added": 1, "updated": 0, "already_queued": 0,
                      "in_progress": 0, "failed": []}
    assert (item["status"], item["attempts"], item["album_id"]) == ("pending", 0, 1)
    album = item["album"]
    assert (album["scan_ready"], album["missing_tracks"], album["only_track_ids"]) == (False, [], [11, 12])
    assert (album["album_folder"], album["artist_path"], album["album_release_id"]) == ("Album 1 (2001)", "/music/Artist", 901)


def test_requeueing_without_track_ids_wants_every_missing_track(queue):
    queue.requeue_albums({1: None})

    assert "only_track_ids" not in _items(queue)[0]["album"]


def test_with_no_session_a_stopped_one_is_made_and_nothing_starts(queue):
    result = queue.requeue_albums({1: None})

    assert _session(queue, result["session_id"])["status"] == "stopped"
    assert queue.in_progress is False
    assert queue.store.resumable_session(include_user_stopped=True)["id"] == result["session_id"]
    assert queue.store.resumable_session() is None, "a restart must not start it unasked"


def test_albums_join_the_stopped_session_at_the_back_or_the_front(queue):
    session_id = queue.store.start_session(status="stopped")
    queue.store.enqueue_items(session_id, [_cached(7)])

    queue.requeue_albums({1: None})
    queue.requeue_albums({2: None}, front=True)

    assert [(item["session_id"], item["album_id"]) for item in _items(queue)] == [
        (session_id, 2), (session_id, 7), (session_id, 1)]


def test_a_pending_album_that_already_covers_the_tracks_is_left_alone_or_moved_up(queue):
    session_id = queue.store.start_session(status="stopped")
    queue.store.enqueue_items(session_id, [_cached(7), _cached(1)])
    before = _items(queue)[1]["album"]

    back = queue.requeue_albums({1: [99]})
    assert (back["added"], back["already_queued"]) == (0, 1)
    assert [item["album_id"] for item in _items(queue)] == [7, 1]

    queue.requeue_albums({1: [99]}, front=True)
    assert [item["album_id"] for item in _items(queue)] == [1, 7]
    assert _items(queue)[0]["album"] == before


def test_a_pending_album_missing_some_requested_tracks_is_widened_to_include_them(queue):
    session_id = queue.store.start_session(status="stopped")
    queue.store.enqueue_items(session_id, [_cached(1)])

    result = queue.requeue_albums({1: [11]})

    (item,) = _items(queue)
    assert result["updated"] == 1
    assert item["album"]["only_track_ids"] == [11, 99]
    assert item["album"]["scan_ready"] is False


def test_requeueing_twice_adds_nothing_the_second_time(queue):
    queue.requeue_albums({1: [11], 2: None})
    result = queue.requeue_albums({1: [11], 2: None})

    assert (result["added"], result["already_queued"], len(_items(queue))) == (0, 2, 2)


def test_an_album_being_downloaded_right_now_is_not_queued_twice(queue):
    session_id = queue.store.start_session()
    queue.store.enqueue_items(session_id, [_cached(1)])
    queue.store.mark_queue_item(_items(queue)[0]["id"], "in_progress")
    queue.in_progress, queue.current_session_id = True, session_id

    result = queue.requeue_albums({1: None})

    assert (result["in_progress"], result["added"], len(_items(queue))) == (1, 0, 1)


def test_a_finished_album_is_queued_again(queue):
    session_id = queue.store.start_session(status="stopped")
    queue.store.enqueue_items(session_id, [_cached(1), _cached(7)])
    queue.store.mark_queue_item(_items(queue)[0]["id"], "done")

    assert queue.requeue_albums({1: None})["added"] == 1


def test_an_album_outside_the_scan_cache_is_read_from_lidarr(queue):
    queue.lidarr_client.get_album.return_value = Response(200, {
        "id": 3, "artistId": 8, "title": "New: Album", "releaseDate": "1999-01-01T00:00:00Z",
        "genres": ["Funk"], "secondaryTypes": ["Live"], "releases": [{"id": 31}],
    })
    queue.lidarr_client.get_artist.return_value = Response(200, {
        "id": 8, "artistName": "Other", "path": "/music/Other", "genres": ["Soul"],
    })

    queue.requeue_albums({3: None})

    album = _items(queue)[0]["album"]
    assert (album["artist"], album["artist_path"], album["album_year"], album["album_release_id"]) == (
        "Other", "/music/Other", 1999, 31)
    assert album["album_folder"] == f'{album["album_name"]} (1999)'
    assert (album["album_secondary_types"], album["artist_genres"]) == (["Live"], "Soul")


def test_albums_lidarr_cannot_describe_are_reported_and_the_rest_queued(queue):
    queue.lidarr_client.get_album.return_value = Response(404)

    result = queue.requeue_albums({1: None, 404: None})

    assert (result["added"], result["failed"]) == (1, [404])


def test_nothing_to_queue_creates_no_session(queue):
    queue.lidarr_client.get_album.return_value = Response(404)

    result = queue.requeue_albums({404: None})

    assert result["session_id"] is None
    assert queue.store.count_sessions() == 0


def test_the_track_scan_keeps_only_the_wanted_missing_tracks(queue):
    queue.lidarr_client.get_tracks_for_album.return_value = Response(
        200, [_lidarr_track(11), _lidarr_track(12), _lidarr_track(13, has_file=True)])
    queue.scanner.on_album_scanned = Mock()
    queue.requeue_albums({1: [11, 13]})
    album = _items(queue)[0]["album"]

    assert queue._wait_for_album_scan_data(album) is True

    assert [t["track_id"] for t in album["missing_tracks"]] == [11]
    assert (album["track_count"], album["missing_count"]) == (3, 1)
    # Reading a queued album's tracks must not queue it a second time during a scheduled sync.
    queue.scanner.on_album_scanned.assert_not_called()


# --- command line ---

LOST = [
    {"tid": 11, "album": 1, "title": "a", "cat": "no trace"},
    {"tid": 12, "album": 1, "title": "b", "cat": "no trace"},
    {"tid": 21, "album": 2, "title": "c", "cat": "staged: None"},
    {"tid": 31, "album": 3, "title": "d", "cat": "no trace"},
]


@pytest.fixture
def lost(tmp_path):
    path = tmp_path / "lost.jsonl"
    path.write_text("".join(json.dumps(line) + "\n" for line in LOST), encoding="utf-8")
    return str(path)


@pytest.fixture
def posts(monkeypatch):
    calls = []

    def fake_post(url, json, timeout):
        calls.append((url, json))
        count = len(json["albums"])
        return Response(200, {"session_id": 10, "added": count, "updated": 0, "already_queued": 0,
                              "in_progress": 0, "failed": []})

    monkeypatch.setattr(requeue_albums.requests, "post", fake_post)
    return calls


def test_cli_requeues_only_the_lost_tracks_grouped_by_album(lost, posts, capsys):
    assert requeue_albums.main([lost, "--url", "http://host:5000/"]) == 0

    assert posts == [("http://host:5000/api/queue/requeue", {
        "albums": [{"album_id": 1, "track_ids": [11, 12]}, {"album_id": 3, "track_ids": [31]}], "front": False})]
    assert json.loads(capsys.readouterr().out)["added"] == 2


def test_cli_can_take_one_album_from_the_file(lost, posts):
    assert requeue_albums.main([lost, "--album", "3", "--front"]) == 0

    assert posts[0][1] == {"albums": [{"album_id": 3, "track_ids": [31]}], "front": True}


def test_cli_accepts_album_ids_without_a_file(posts):
    assert requeue_albums.main(["--album", "41749", "--album", "5"]) == 0

    assert posts[0][1]["albums"] == [{"album_id": 41749}, {"album_id": 5}]


def test_cli_batches_and_keeps_file_order_when_queueing_at_the_front(lost, posts):
    requeue_albums.main([lost, "--front", "--batch", "1"])

    # Each batch goes ahead of the one before, so the last albums are sent first.
    assert [body["albums"][0]["album_id"] for _, body in posts] == [3, 1]


def test_cli_dry_run_posts_nothing(lost, posts, capsys):
    assert requeue_albums.main([lost, "--dry-run"]) == 0

    assert posts == []
    assert json.loads(capsys.readouterr().out) == {"albums": 2, "tracks": 3, "skipped": 1, "invalid": 0}


def test_cli_options_widen_the_selection(lost, posts):
    requeue_albums.main([lost, "--any-cat", "--all-missing", "--limit", "2"])

    assert posts[0][1]["albums"] == [{"album_id": 1}, {"album_id": 2}]


def test_cli_reads_a_plain_list_of_album_ids(tmp_path, posts):
    path = tmp_path / "ids.txt"
    path.write_text("41749\n\nnonsense\n12603\n", encoding="utf-8")

    assert requeue_albums.main([str(path)]) == 1

    assert posts[0][1]["albums"] == [{"album_id": 41749}, {"album_id": 12603}]


def test_cli_exit_status_reports_failures(lost, monkeypatch):
    monkeypatch.setattr(requeue_albums.requests, "post", lambda *a, **k: Response(200, {
        "session_id": 1, "added": 1, "updated": 0, "already_queued": 0, "in_progress": 0, "failed": [3]}))
    assert requeue_albums.main([lost]) == 1
    monkeypatch.setattr(requeue_albums.requests, "post", lambda *a, **k: Response(500, {"error": "boom"}))
    assert requeue_albums.main([lost]) == 2


def test_cli_needs_something_to_queue():
    with pytest.raises(SystemExit):
        requeue_albums.main([])
