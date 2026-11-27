"""A download that fails is recorded as failed and its album comes round again instead of being marked done."""

import json
import threading
from unittest.mock import Mock

import pytest

import download_queue
from download_queue import DownloadQueue
from store import Store


def _track(n):
    return {"artist": "Artist", "track_title": f"Track {n}", "track_number": n, "absolute_track_number": n,
            "track_id": n, "duration_ms": 180000, "link": "", "title_of_link": ""}


def _album(name="Album", track_ids=(1, 2)):
    return {"artist": "Artist", "album_name": name, "album_id": sum(track_ids), "artist_path": "/music/Artist",
            "album_folder": f"{name} (2024)", "track_count": len(track_ids), "missing_count": len(track_ids),
            "missing_tracks": [_track(n) for n in track_ids], "status": ""}


class FakeSearcher:
    """Links every track and records it as matched, as LinkSearcher does before any download."""

    def __init__(self, store):
        self.store = store
        self.failing_search = set()

    def find_links(self, req_album, session_id=None):
        for track in req_album["missing_tracks"]:
            failed = track["track_id"] in self.failing_search
            if not failed:
                track["link"] = f"https://www.youtube.com/watch?v=video{track['track_id']:06d}"
                track["title_of_link"] = track["track_title"]
            else:
                track["_search_failed"] = True
            track["_track_result_id"] = self.store.record_track_result(
                session_id=session_id, artist="Artist", album=req_album["album_name"],
                track_title=track["track_title"], track_number=track["track_number"], track_id=track["track_id"],
                duration_ms=180000, outcome="error" if failed else "matched", link=track["link"] or None,
            )


@pytest.fixture
def queue(tmp_path, monkeypatch):
    config = Mock()
    config.thread_limit = 1
    config.batch_size = 10
    config.download_folder = str(tmp_path / "downloads")
    config.preferred_codec = "mp3"
    config.sleep_interval = 0
    config.attempt_lidarr_import = False
    config.library_scan_on_completion = False
    config.download_attempts_per_album = 3
    store = Store(tmp_path / "lidatube.db")
    scanner = Mock()
    scanner.items = []
    queue = DownloadQueue(config, store, scanner, FakeSearcher(store), Mock(), Mock(), threading.Event(), Mock(), Mock())
    monkeypatch.setattr(download_queue._general, "add_metadata", Mock())
    yield queue
    store.close()


def _enqueue(queue, *albums):
    session_id = queue.store.start_session()
    queue.store.enqueue_items(session_id, list(albums))
    queue.current_session_id = session_id
    return session_id


def _process_next(queue, session_id):
    queue_item_id, album = queue._claim(session_id, 1)[0]
    queue._process_item(queue_item_id, album)
    return queue_item_id


def _rows(queue, table):
    return queue.store._fetchall(f"SELECT * FROM {table} ORDER BY id")


def _fail(*track_ids):
    links = {f"video{n:06d}" for n in track_ids}
    return lambda link, file_name, **kwargs: link[-11:] not in links


def test_a_failed_download_is_not_left_recorded_as_matched(queue):
    session_id = _enqueue(queue, _album())
    queue.downloader.download.side_effect = _fail(2)

    _process_next(queue, session_id)

    results = _rows(queue, "track_results")
    assert [(r["track_id"], r["outcome"], r["download"]) for r in results] == [(1, "matched", "done"), (2, "error", "failed")]
    assert results[1]["link"].endswith("video000002")


def test_an_album_with_failed_downloads_is_requeued_with_only_those_tracks(queue):
    session_id = _enqueue(queue, _album(), _album("Next", (3,)))
    queue.downloader.download.side_effect = _fail(2)

    item_id = _process_next(queue, session_id)

    first, second = _rows(queue, "queue_items")
    assert (first["id"], first["status"], first["attempts"]) == (item_id, "pending", 1)
    assert first["position"] > second["position"], "a retry goes to the back of the queue"
    stored = json.loads(first["album_json"])
    assert [t["track_id"] for t in stored["missing_tracks"]] == [2]
    assert (stored["missing_count"], stored["track_count"]) == (1, 2)
    assert stored["missing_tracks"][0]["link"] == ""
    assert not any(key.startswith("_") for key in list(stored) + list(stored["missing_tracks"][0]))


def test_an_album_whose_downloads_keep_failing_ends_as_an_error_not_done(queue):
    session_id = _enqueue(queue, _album(track_ids=(1,)))
    queue.downloader.download.side_effect = _fail(1)

    for _ in range(3):
        _process_next(queue, session_id)

    (item,) = _rows(queue, "queue_items")
    assert (item["status"], item["attempts"]) == ("error", 3)
    assert queue.store.next_batch(session_id, 1) == []


def test_an_album_whose_downloads_all_succeed_is_done(queue):
    session_id = _enqueue(queue, _album())
    queue.downloader.download.return_value = True

    _process_next(queue, session_id)

    (item,) = _rows(queue, "queue_items")
    assert (item["status"], item["attempts"]) == ("done", 0)


def test_a_track_whose_search_failed_is_retried_too(queue):
    session_id = _enqueue(queue, _album())
    queue.searcher.failing_search = {1}
    queue.downloader.download.return_value = True

    _process_next(queue, session_id)

    (item,) = _rows(queue, "queue_items")
    assert item["status"] == "pending"
    assert [t["track_id"] for t in json.loads(item["album_json"])["missing_tracks"]] == [1]


def test_a_download_cut_short_by_stop_is_neither_a_failure_nor_an_attempt(queue):
    session_id = _enqueue(queue, _album(track_ids=(1,)))

    def stop_during_download(link, file_name, **kwargs):
        queue.stop_event.set()
        return False

    queue.downloader.download.side_effect = stop_during_download

    _process_next(queue, session_id)

    (item,) = _rows(queue, "queue_items")
    assert (item["status"], item["attempts"]) == ("pending", 0)
    assert _rows(queue, "track_results")[0]["outcome"] == "matched"
    assert _rows(queue, "track_results")[0]["download"] is None


def test_an_album_whose_tracks_lidarr_could_not_list_is_tried_again(queue, monkeypatch):
    album = dict(_album(), scan_ready=False)
    session_id = _enqueue(queue, album)
    monkeypatch.setattr(queue, "_wait_for_album_scan_data", lambda _: False)

    _process_next(queue, session_id)

    (item,) = _rows(queue, "queue_items")
    assert (item["status"], item["attempts"]) == ("pending", 1)
    assert json.loads(item["album_json"]) == album


def test_a_session_works_through_retries_and_completes(queue):
    session_id = _enqueue(queue, _album(), _album("Next", (3,)))
    failures = iter([False])
    queue.downloader.download.side_effect = lambda link, file_name, **kwargs: next(failures, True)
    queue.in_progress = True

    queue.run(session_id)

    assert [item["status"] for item in _rows(queue, "queue_items")] == ["done", "done"]
    assert queue.status == "complete"


def test_a_video_that_can_never_be_played_is_not_picked_for_that_track_again(queue):
    session_id = _enqueue(queue, _album(track_ids=(1,)))

    def disabled(link, file_name, on_failure=None):
        on_failure(Exception("ERROR: Playback on other websites has been disabled by the video owner"))
        return False

    queue.downloader.download.side_effect = disabled

    _process_next(queue, session_id)

    assert queue.store.rejected_video_ids(1) == {"video000001"}


def test_an_ordinary_download_error_does_not_reject_the_video(queue):
    session_id = _enqueue(queue, _album(track_ids=(1,)))

    def broken(link, file_name, on_failure=None):
        on_failure(Exception("Requested format is not available"))
        return False

    queue.downloader.download.side_effect = broken

    _process_next(queue, session_id)

    assert queue.store.rejected_video_ids(1) == set()
