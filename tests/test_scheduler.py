from unittest.mock import Mock

import pytest

import scheduler
from scheduler import SyncScheduler


class _StopScheduler(BaseException):
    """Escapes the scheduler's infinite loop without going through its error handling."""


class _InlineThread:
    def __init__(self, target=None, name=None, daemon=None, **kwargs):
        self.target = target

    def start(self):
        self.target()

    def join(self, timeout=None):
        pass


def _run_once(monkeypatch, hour, sync_schedule=(3,)):
    calls = []
    queue = Mock()
    queue.begin_streaming.side_effect = lambda: calls.append("begin_streaming")
    queue.end_streaming.side_effect = lambda: calls.append("end_streaming")
    scanner = Mock()
    scanner.items = []
    scanner.fetch_wanted_albums.side_effect = lambda: calls.append("fetch")
    config = Mock()
    config.sync_schedule = list(sync_schedule)

    def fake_sleep(seconds):
        calls.append(("sleep", seconds))
        raise _StopScheduler

    monkeypatch.setattr(scheduler.time, "localtime", lambda: Mock(tm_hour=hour))
    monkeypatch.setattr(scheduler.time, "sleep", fake_sleep)
    monkeypatch.setattr(scheduler.threading, "Thread", _InlineThread)
    with pytest.raises(_StopScheduler):
        SyncScheduler(config, scanner, queue, Mock()).run()
    return calls


def test_in_a_sync_window_streams_a_lidarr_fetch_into_the_queue_then_waits_an_hour(monkeypatch):
    assert _run_once(monkeypatch, hour=3) == ["begin_streaming", "fetch", "end_streaming", ("sleep", 3600)]


def test_outside_a_sync_window_checks_again_in_ten_minutes(monkeypatch):
    assert _run_once(monkeypatch, hour=4) == [("sleep", 600)]


def test_start_runs_the_loop_on_a_daemon_thread(monkeypatch):
    threads = []

    class FakeThread:
        def __init__(self, target=None, name=None, daemon=None):
            threads.append((target, name, daemon))

        def start(self):
            pass

    monkeypatch.setattr(scheduler.threading, "Thread", FakeThread)
    sync = SyncScheduler(Mock(), Mock(), Mock(), Mock())

    sync.start()

    assert threads == [(sync.run, "Schedule_Thread", True)]


def _run_until_second_sleep(monkeypatch, queue, scanner, thread_class=_InlineThread):
    """Run the loop until its second sleep, returning the calls and error logs seen on the way."""
    calls = []
    config = Mock()
    config.sync_schedule = [3]
    logger = Mock()

    def fake_sleep(seconds):
        calls.append(("sleep", seconds))
        if len([c for c in calls if isinstance(c, tuple)]) == 2:
            raise _StopScheduler

    monkeypatch.setattr(scheduler.time, "localtime", lambda: Mock(tm_hour=3))
    monkeypatch.setattr(scheduler.time, "sleep", fake_sleep)
    monkeypatch.setattr(scheduler.threading, "Thread", thread_class)
    with pytest.raises(_StopScheduler):
        SyncScheduler(config, scanner, queue, logger).run()
    return calls, logger.error


def test_an_error_in_one_sync_does_not_stop_later_syncs(monkeypatch):
    queue = Mock()
    scanner = Mock()
    scanner.items = []
    queue.begin_streaming.side_effect = [RuntimeError("database is locked"), None]

    calls, errors = _run_until_second_sleep(monkeypatch, queue, scanner)

    assert calls == [("sleep", 600), ("sleep", 3600)]
    assert queue.begin_streaming.call_count == 2
    scanner.fetch_wanted_albums.assert_called_once_with()
    errors.assert_called_once()


def test_streaming_ends_even_when_the_fetch_thread_cannot_start(monkeypatch):
    """Under fd exhaustion Thread.start() raises; the queue must not keep waiting for streamed albums."""
    class UnstartableThread(_InlineThread):
        def start(self):
            raise RuntimeError("can't start new thread")

    queue = Mock()
    scanner = Mock()
    scanner.items = []

    calls, _ = _run_until_second_sleep(monkeypatch, queue, scanner, thread_class=UnstartableThread)

    assert queue.end_streaming.call_count == 2
    assert calls == [("sleep", 600), ("sleep", 600)]


def test_streaming_ends_even_when_begin_streaming_fails(monkeypatch):
    """begin_streaming can fail after switching streaming on (database locked); the sync must still end it."""
    queue = Mock()
    scanner = Mock()
    scanner.items = []
    queue.begin_streaming.side_effect = [RuntimeError("database is locked"), None]

    _run_until_second_sleep(monkeypatch, queue, scanner)

    assert queue.end_streaming.call_count == 2
