import json
import threading
import time
from unittest.mock import Mock

import pytest

import download_queue
from download_queue import DownloadQueue
from store import Store


def build_queue(yield_to_event_loop=None):
    config = Mock()
    config.thread_limit = 1
    config.batch_size = 2
    config.auto_resume = True
    config.download_folder = "/downloads"
    config.preferred_codec = "mp3"
    config.sleep_interval = 0
    config.attempt_lidarr_import = False
    config.library_scan_on_completion = False
    store = Mock()
    store.enqueue_items.side_effect = lambda session_id, items: len(items)
    store.start_session.return_value = 1
    store.resumable_session.return_value = None
    store.resume_session.return_value = True
    store.next_batch.return_value = []
    store.queue_counts.return_value = {"pending": 0, "in_progress": 0, "done": 0, "error": 0, "total": 0}
    store.get_override.return_value = None
    store.get_session_result_counts.return_value = {"matched_count": 0, "failed_count": 0}
    scanner = Mock()
    scanner.items = []
    scanner.status = "idle"
    return DownloadQueue(
        config, store, scanner, searcher=Mock(), downloader=Mock(), lidarr_client=Mock(),
        stop_event=threading.Event(), emit=Mock(), logger=Mock(),
        yield_to_event_loop=yield_to_event_loop or Mock(),
    )


def test_streaming_mode_persists_scan_ready_album(monkeypatch):
    """Streaming albums enter the durable session without growing the memory queue."""
    queue = build_queue()
    queue.streaming_mode = True
    queue.current_session_id = 1
    queue.in_progress = True

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
    album["missing_tracks"] = [{"track_title": "Track 1", "track_id": 1}]

    queue.enqueue_scanned_album(album)

    assert queue.items == []
    queue.store.enqueue_items.assert_called_once_with(1, [album])
    queue.store.increment_session_requested_count.assert_called_once_with(1, 1)
    assert album["status"] == "Queued"


def test_streaming_mode_off_does_not_queue_scanned_albums(monkeypatch):
    """Outside a scheduled sync, scanned albums wait for the user to select them."""
    queue = build_queue()
    queue.streaming_mode = False

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
    queue.enqueue_scanned_album(album)

    queue.store.enqueue_items.assert_not_called()
    assert album["status"] == ""


def test_run_exits_when_empty_and_not_streaming(monkeypatch):
    """run exits immediately when queue is empty and not in streaming mode."""
    queue = build_queue()
    queue.streaming_mode = False
    queue.scanner.status = "complete"
    queue.in_progress = True
    queue.index = 0
    monkeypatch.setattr(queue.config, "library_scan_on_completion", False)

    queue.run()

    assert queue.status == "complete"
    assert queue.in_progress is False


def test_run_waits_when_streaming_and_fetch_busy(monkeypatch):
    """run stays alive while streaming_mode=True and lidarr is busy, then exits once fetch is done."""
    queue = build_queue()
    queue.streaming_mode = True
    queue.scanner.status = "busy"
    queue.in_progress = True
    queue.index = 0
    monkeypatch.setattr(queue.config, "library_scan_on_completion", False)

    def finish_fetch():
        time.sleep(0.3)
        queue.scanner.status = "complete"
        queue.streaming_mode = False

    t = threading.Thread(target=finish_fetch)
    t.start()

    queue.run()
    t.join()

    assert queue.status == "complete"
    assert queue.in_progress is False


def test_run_processes_persisted_item_added_during_streaming(monkeypatch):
    """The running queue polls persisted albums added by the streaming scanner."""
    queue = build_queue()
    queue.streaming_mode = True
    queue.scanner.status = "busy"
    queue.in_progress = True
    queue.current_session_id = 1
    queue.index = 0
    emit_mock = Mock()
    queue.emit = emit_mock
    monkeypatch.setattr(queue.config, "library_scan_on_completion", False)

    processed = []

    def fake_download_album(req_album):
        processed.append(req_album["album_name"])
        queue.index += 1

    monkeypatch.setattr(queue, "download_album", fake_download_album)

    album = {"album_name": "Late Album", "status": "Queued", "scan_ready": True}
    persisted_rows = []

    def next_batch(session_id, limit):
        if persisted_rows:
            return [persisted_rows.pop(0)]
        return []

    queue.store.next_batch.side_effect = next_batch

    def add_item_then_finish():
        time.sleep(0.1)
        persisted_rows.append({"id": 10, "album_json": json.dumps(album)})
        time.sleep(0.2)
        queue.scanner.status = "complete"
        queue.streaming_mode = False

    t = threading.Thread(target=add_item_then_finish)
    t.start()

    queue.run()
    t.join()

    assert "Late Album" in processed
    assert queue.status == "complete"


def test_add_albums_persists_selected_albums_without_growing_memory_queue(monkeypatch
):
    queue = build_queue()
    start_mock = Mock(return_value=True)
    monkeypatch.setattr(queue, "start", start_mock)
    queue.scanner.items = [
        {
            "artist": "Artist",
            "album_name": f"Album {index}",
            "scan_ready": True,
            "missing_tracks": [{"track_id": track_id} for track_id in range(index + 1)],
        }
        for index in range(3)
    ]

    queue.add_albums([2, 0])

    assert queue.items == []
    queued = queue.store.enqueue_items.call_args.args[1]
    assert [item["album_name"] for item in queued] == ["Album 0", "Album 2"]
    queue.store.increment_session_requested_count.assert_called_once_with(1, 4)
    assert [item["checked"] for item in queue.scanner.items] == [True, False, True]
    start_mock.assert_called_once_with(1)


def test_persisted_batches_resume_after_simulated_restart_without_drops(monkeypatch, tmp_path
):
    class SimulatedWorkerCrash(BaseException):
        pass

    db_path = tmp_path / "restart.db"
    session_id = None
    processed = []
    observed_batch_sizes = []
    sleep_mock = Mock()

    first = build_queue(yield_to_event_loop=sleep_mock)
    first.store = Store(db_path)
    first.config.batch_size = 2
    first.current_session_id = first.store.start_session(requested_count=5)
    session_id = first.current_session_id
    first.store.enqueue_items(
        session_id,
        [
            {"album_id": album_id, "album_name": f"Album {album_id}", "scan_ready": True}
            for album_id in range(5)
        ],
    )

    def process_first(req_album):
        observed_batch_sizes.append(len(first.items))
        processed.append(req_album["album_id"])
        req_album["status"] = "Download Complete"

    monkeypatch.setattr(first, "download_album", process_first)
    real_next_batch = first.store.next_batch
    batch_calls = 0

    def crash_after_one_batch(requested_session_id, limit):
        nonlocal batch_calls
        batch_calls += 1
        if batch_calls == 1:
            return real_next_batch(requested_session_id, limit)
        raise SimulatedWorkerCrash()

    monkeypatch.setattr(first.store, "next_batch", crash_after_one_batch)
    with pytest.raises(SimulatedWorkerCrash):
        first.run(session_id)
    first.store.close()

    reopened = Store(db_path)
    assert reopened.resumable_session()["id"] == session_id
    assert reopened.queue_counts(session_id) == {
        "pending": 3,
        "in_progress": 0,
        "done": 2,
        "error": 0,
        "total": 5,
    }

    resumed = build_queue(yield_to_event_loop=sleep_mock)
    resumed.store = reopened
    resumed.config.batch_size = 2

    def process_remainder(req_album):
        observed_batch_sizes.append(len(resumed.items))
        processed.append(req_album["album_id"])
        req_album["status"] = "Download Complete"

    monkeypatch.setattr(resumed, "download_album", process_remainder)
    resumed.run(session_id)

    assert sorted(processed) == list(range(5))
    assert len(processed) == len(set(processed))
    assert max(observed_batch_sizes) <= 2
    assert reopened.queue_counts(session_id)["done"] == 5
    assert reopened.list_sessions()[0]["status"] == "complete"
    assert sleep_mock.called
    reopened.close()


def test_missing_tracks_preserved_after_download(monkeypatch):
    """missing_tracks must NOT be cleared after download — clearing it corrupts the cache
    for the next session (album re-queued with no tracks = silent no-op download)."""
    queue = build_queue()
    queue.in_progress = False
    queue.index = 0
    queue.streaming_mode = False
    monkeypatch.setattr(queue.config, "library_scan_on_completion", False)

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
    queue.scanner.items = [req_album]
    queue.items = [req_album]

    monkeypatch.setattr(queue.searcher, "find_links", lambda album, session_id=None: None)

    queue.download_album(req_album)

    assert req_album["missing_tracks"] is original_tracks


def test_wait_for_album_scan_data_returns_false_when_not_busy(monkeypatch):
    queue = build_queue()
    monkeypatch.setattr(queue, "emit_update", Mock())
    queue.scanner.status = "complete"

    req_album = {"scan_ready": False, "scan_in_progress": True, "status": ""}

    result = queue._wait_for_album_scan_data(req_album)

    assert result is False
    assert req_album["status"] == "Waiting for refresh data"


def test_download_album_marks_album_incomplete_when_links_missing(monkeypatch):
    queue = build_queue()
    monkeypatch.setattr(queue, "_wait_for_album_scan_data", lambda _: True)
    monkeypatch.setattr(queue.searcher, "find_links", lambda _, session_id=None: None)
    monkeypatch.setattr(download_queue.os.path, "exists", lambda _: True)

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
    queue.items = [req_album]

    queue.download_album(req_album)

    assert req_album["status"] == "Album Incomplete"


def test_download_album_marks_download_failed_when_all_fail(monkeypatch):
    queue = build_queue()
    monkeypatch.setattr(queue, "_wait_for_album_scan_data", lambda _: True)
    monkeypatch.setattr(queue.searcher, "find_links", lambda _, session_id=None: None)
    monkeypatch.setattr(download_queue.os.path, "exists", lambda _: False)
    queue.downloader.download.return_value = False

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
    queue.items = [req_album]

    queue.download_album(req_album)

    assert req_album["status"] == "Download Failed"


def test_stop_cancels_pending_futures_and_marks_unprocessed(monkeypatch):
    queue = build_queue()
    monkeypatch.setattr(queue, "emit_update", Mock())

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
    queue.futures = [pending, finished]
    queue.items = [{"status": "Done"}, {"status": "Queued"}]
    queue.index = 1

    queue.stop()

    assert pending.cancel_called is True
    assert finished.cancel_called is False
    assert queue.items[1]["status"] == "Download Stopped"
    assert queue.status == "stopped"


def test_reset_clears_queue_and_completion(monkeypatch):
    queue = build_queue()
    monkeypatch.setattr(queue, "emit_update", Mock())

    class FakeFuture:
        def __init__(self):
            self.cancel_called = False

        def done(self):
            return False

        def cancel(self):
            self.cancel_called = True

    future = FakeFuture()
    queue.futures = [future]
    queue.items = [{"status": "Queued"}]
    queue.percent_completion = 42
    queue.current_session_id = 8
    queue.progress["session_id"] = 8

    queue.reset()

    assert future.cancel_called is True
    assert queue.items == []
    assert queue.status == "idle"
    assert queue.index == 0
    assert queue.percent_completion == 0
    clear_call = queue.store.clear_queue.call_args
    assert clear_call.args == (8,)
    assert callable(clear_call.kwargs["on_chunk"])
    queue.store.finish_session.assert_called_once_with(8, "reset", matched_count=0, failed_count=0)


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


def test_download_album_status_is_stopped_when_download_cancelled(monkeypatch):
    """When stop event is set by a cancelled download, status should be 'Download Stopped'
    and subsequent tracks should not be attempted."""
    queue = build_queue()
    monkeypatch.setattr(queue, "_wait_for_album_scan_data", lambda _: True)
    monkeypatch.setattr(queue.searcher, "find_links", lambda _, session_id=None: None)
    monkeypatch.setattr(download_queue.os.path, "exists", lambda _: False)

    def cancel_on_first_download(*args, **kwargs):
        queue.stop_event.set()
        return False

    queue.downloader.download.side_effect = cancel_on_first_download

    req_album = _make_req_album([_make_track(1), _make_track(2)])
    queue.items = [req_album]

    queue.download_album(req_album)

    assert queue.downloader.download.call_count == 1, "Should stop after first cancelled download"
    assert req_album["status"] == "Download Stopped"


def test_download_album_status_is_stopped_when_stop_set_after_link_finder(monkeypatch):
    """When stop event is set after link search, status should be 'Download Stopped'."""
    queue = build_queue()
    monkeypatch.setattr(queue, "_wait_for_album_scan_data", lambda _: True)

    def link_finder_then_stop(_, session_id=None):
        queue.stop_event.set()

    monkeypatch.setattr(queue.searcher, "find_links", link_finder_then_stop)

    req_album = _make_req_album([_make_track(1)])
    queue.items = [req_album]

    queue.download_album(req_album)

    assert queue.downloader.download.call_count == 0, "Should not download if stop set after link finder"
    assert req_album["status"] == "Download Stopped"


def test_stop_persists_the_stop_so_a_crash_does_not_auto_resume(monkeypatch, tmp_path):
    """A worker killed after the user pressed Stop must not resume on restart (POLA)."""
    queue = build_queue()
    store = Store(tmp_path / "lidatube.db")
    queue.store = store
    session_id = store.start_session(requested_count=1)
    store.enqueue_items(session_id, [{"artist": "A", "album_name": "B", "missing_tracks": []}])
    queue.current_session_id = session_id
    queue.in_progress = True

    queue.stop()

    assert store.list_sessions(10, 0)[0]["status"] == "stopped"

    # Simulate the SIGKILL + restart: a fresh queue auto-resumes only crashed work.
    restarted = build_queue()
    restarted.store = Store(tmp_path / "lidatube.db")
    restarted.config.auto_resume = True
    started = []
    monkeypatch.setattr(restarted, "start", lambda sid: started.append(sid) or True)

    restarted.auto_resume()
    assert started == []

    # The user can still resume explicitly.
    assert restarted.resume(emit=False) is True
    assert started == [session_id]
    restarted.store.close()
    store.close()


def test_reset_lets_the_event_loop_run_while_clearing_the_queue(monkeypatch):
    """The queue delete must not block the gevent worker: gunicorn SIGKILLs it after 300s."""
    queue = build_queue()
    slept = []
    queue.yield_to_event_loop = lambda *args: slept.append(args)
    queue.current_session_id = 7
    queue.store.clear_queue.return_value = 3

    queue.reset()

    kwargs = queue.store.clear_queue.call_args.kwargs
    assert callable(kwargs.get("on_chunk"))
    kwargs["on_chunk"]()
    assert slept, "on_chunk must yield to the event loop"


def test_start_runs_one_session_thread_at_a_time(monkeypatch):
    started = []

    class FakeThread:
        def __init__(self, target=None, args=(), name=None, daemon=None):
            self.target, self.args = target, args

        def start(self):
            started.append((self.target, self.args))

    monkeypatch.setattr(download_queue.threading, "Thread", FakeThread)
    queue = build_queue()
    queue.stop_event.set()

    assert queue.start(5) is True
    assert queue.start(6) is False

    assert started == [(queue.run, (5,))]
    assert (queue.current_session_id, queue.status, queue.in_progress) == (5, "running", True)
    assert not queue.stop_event.is_set()


@pytest.mark.parametrize("attempt_import, grabbed, imported", [(True, True, True), (True, False, False), (False, True, False)])
def test_download_album_imports_grabbed_tracks_through_lidarr_when_enabled(monkeypatch, attempt_import, grabbed, imported):
    queue = build_queue()
    queue.config.attempt_lidarr_import = attempt_import
    monkeypatch.setattr(queue, "_wait_for_album_scan_data", lambda _: True)
    monkeypatch.setattr(download_queue.os.path, "exists", lambda _: False)
    monkeypatch.setattr(download_queue._general, "add_metadata", Mock())
    queue.downloader.download.return_value = grabbed
    req_album = _make_req_album([_make_track(1)])

    queue.download_album(req_album)

    assert queue.lidarr_client.import_album.called is imported


@pytest.mark.parametrize("scan_on_completion", [True, False])
def test_run_rescans_the_lidarr_library_when_a_session_completes(scan_on_completion):
    queue = build_queue()
    queue.config.library_scan_on_completion = scan_on_completion

    queue.run(1)

    assert queue.status == "complete"
    assert queue.lidarr_client.rescan_library.called is scan_on_completion


@pytest.mark.parametrize("queue_running", [False, True])
def test_begin_streaming_joins_the_running_session_or_starts_one(monkeypatch, queue_running):
    queue = build_queue()
    queue.in_progress = queue_running
    queue.stop_event.set()
    start = Mock()
    monkeypatch.setattr(queue, "start", start)

    queue.begin_streaming()

    assert queue.streaming_mode is True
    assert not queue.stop_event.is_set()
    if queue_running:
        queue.store.start_session.assert_not_called()
        start.assert_not_called()
    else:
        queue.store.start_session.assert_called_once_with(requested_count=0)
        start.assert_called_once_with(1)

    queue.end_streaming()
    assert queue.streaming_mode is False
