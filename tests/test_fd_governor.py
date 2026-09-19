import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import fd_governor
from fd_governor import FdGovernor


@pytest.fixture
def governor():
    gov = FdGovernor(Mock())
    gov.fd_limit = 1000
    return gov


def test_reads_the_soft_open_file_limit(monkeypatch):
    monkeypatch.setattr(fd_governor.resource, "getrlimit", lambda _: (4096, 8192))
    assert FdGovernor(Mock()).fd_limit == 4096


def test_unlimited_open_files_leave_worker_counts_alone(monkeypatch):
    monkeypatch.setattr(fd_governor.resource, "getrlimit", lambda _: (fd_governor.resource.RLIM_INFINITY,) * 2)
    gov = FdGovernor(Mock())
    assert gov.fd_limit is None
    assert gov.recommended_workers(64, estimated_fd_per_worker=160) == 64


@pytest.mark.parametrize("configured, per_worker, expected", [
    (8, 160, 4),     # 1000 - 350 reserved = 650 budget // 160
    (2, 160, 2),     # never raises the configured count
    (8, 1000, 1),    # always at least one worker
    ("bad", 160, 1),
])
def test_recommended_workers_fit_the_fd_budget(governor, configured, per_worker, expected):
    assert governor.recommended_workers(configured, estimated_fd_per_worker=per_worker) == expected


def test_apply_safety_limits_clamps_both_pools_and_logs(governor):
    config = SimpleNamespace(thread_limit=8, lidarr_scan_thread_limit=64)

    governor.apply_safety_limits(config)

    assert config.thread_limit == 4
    assert config.lidarr_scan_thread_limit == 27  # 650 // 24
    assert governor.logger.warning.call_count == 2


@pytest.mark.parametrize("open_count, expected", [(849, False), (850, True), (None, False)])
def test_pressure_is_high_from_85_percent_of_the_limit(governor, monkeypatch, open_count, expected):
    monkeypatch.setattr(governor, "open_fd_count", lambda: open_count)
    assert governor.is_pressure_high() is expected


def test_pressure_is_never_high_without_a_known_limit(governor, monkeypatch):
    governor.fd_limit = None
    monkeypatch.setattr(governor, "open_fd_count", lambda: 10**6)
    assert governor.is_pressure_high() is False


def test_signal_exhaustion_holds_the_backoff_then_clears_it(governor, monkeypatch):
    held_during_sleep = []
    monkeypatch.setattr(fd_governor.time, "sleep", lambda s: held_during_sleep.append((s, governor.exhaustion_event.is_set())))

    governor.signal_exhaustion()

    assert held_during_sleep == [(10, True)]
    assert not governor.exhaustion_event.is_set()


def test_signal_exhaustion_is_a_no_op_while_a_backoff_is_running(governor, monkeypatch):
    sleep = Mock()
    monkeypatch.setattr(fd_governor.time, "sleep", sleep)
    governor._backoff_lock.acquire()
    try:
        governor.signal_exhaustion()
    finally:
        governor._backoff_lock.release()
    sleep.assert_not_called()


def test_signal_exhaustion_in_background_runs_on_a_daemon_thread(governor, monkeypatch):
    threads = []

    class FakeThread:
        def __init__(self, target=None, daemon=None, **kwargs):
            threads.append((target, daemon))

        def start(self):
            pass

    monkeypatch.setattr(fd_governor.threading, "Thread", FakeThread)
    governor.signal_exhaustion_in_background()
    assert threads == [(governor.signal_exhaustion, True)]


def test_wait_if_pressure_blocks_until_the_backoff_clears(governor, monkeypatch):
    monkeypatch.setattr(governor, "is_pressure_high", lambda: False)
    governor.exhaustion_event.set()
    waits = []

    def fake_sleep(seconds):
        waits.append(seconds)
        governor.exhaustion_event.clear()

    monkeypatch.setattr(fd_governor.time, "sleep", fake_sleep)

    governor.wait_if_pressure()

    assert waits == [0.5]


def test_wait_if_pressure_starts_a_backoff_when_usage_is_high(governor, monkeypatch):
    monkeypatch.setattr(governor, "is_pressure_high", lambda: True)
    background = Mock()
    monkeypatch.setattr(governor, "signal_exhaustion_in_background", background)

    governor.wait_if_pressure()

    background.assert_called_once_with()


def test_wait_if_pressure_gives_up_after_its_deadline(governor, monkeypatch):
    monkeypatch.setattr(governor, "is_pressure_high", lambda: False)
    governor.exhaustion_event.set()
    clock = iter(range(0, 1000, 10))
    monkeypatch.setattr(fd_governor.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(fd_governor.time, "sleep", lambda s: None)

    governor.wait_if_pressure()

    assert governor.exhaustion_event.is_set()
