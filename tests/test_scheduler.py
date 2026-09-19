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
