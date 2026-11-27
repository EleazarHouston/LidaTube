"""When downloads fail across the board the queue pauses instead of using up every album's attempts."""

from unittest.mock import Mock

import pytest

import _general
from download_health import DownloadHealth


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def build_health(threshold=3):
    config = Mock()
    config.download_failure_threshold = threshold
    clock = Clock()
    return DownloadHealth(config, Mock(), clock=clock), clock


BROKEN = Exception("ERROR: [youtube] abc: Requested format is not available")


def test_a_run_of_failures_with_no_success_pauses_downloading():
    health, _ = build_health()
    for _ in range(2):
        health.record_failure(BROKEN)
    assert (health.failing, health.pause_remaining()) == (False, 0)

    health.record_failure(BROKEN)

    assert health.failing is True
    assert health.pause_remaining() == DownloadHealth.PAUSE_SECONDS[0]


def test_a_success_breaks_the_run():
    health, _ = build_health()
    for _ in range(2):
        health.record_failure(BROKEN)
    health.record_success()
    for _ in range(2):
        health.record_failure(BROKEN)

    assert health.failing is False


def test_failures_already_in_flight_do_not_lengthen_the_pause():
    health, _ = build_health()
    for _ in range(10):
        health.record_failure(BROKEN)

    assert health.pause_remaining() == DownloadHealth.PAUSE_SECONDS[0]


def test_each_failed_probe_waits_longer_up_to_the_last_step():
    health, clock = build_health()
    for _ in range(3):
        health.record_failure(BROKEN)

    pauses = []
    for _ in range(len(DownloadHealth.PAUSE_SECONDS) + 1):
        pauses.append(health.pause_remaining())
        clock.now += health.pause_remaining()
        assert health.failing is True and health.pause_remaining() == 0
        health.record_failure(BROKEN)

    assert pauses == [*DownloadHealth.PAUSE_SECONDS, DownloadHealth.PAUSE_SECONDS[-1]]


def test_a_successful_probe_ends_the_pause_and_restarts_the_schedule():
    health, clock = build_health()
    for _ in range(3):
        health.record_failure(BROKEN)
    clock.now += health.pause_remaining()

    health.record_success()

    assert (health.failing, health.pause_remaining()) == (False, 0)
    for _ in range(3):
        health.record_failure(BROKEN)
    assert health.pause_remaining() == DownloadHealth.PAUSE_SECONDS[0]


@pytest.mark.parametrize("message", [
    "Playback on other websites has been disabled by the video owner",
    "Private video. Sign in if you've been granted access",
    "Sign in to confirm your age",
    "The uploader has not made this video available in your country",
    "Join this channel to get access to members-only content",
    "ERROR: [youtube] abc: Video unavailable",
    "Incomplete YouTube ID None. URL https://www.youtube.com/watch?v=None looks truncated.",
])
def test_failures_that_belong_to_one_video_never_pause_downloading(message):
    health, _ = build_health()
    for _ in range(10):
        health.record_failure(Exception(message))

    assert health.failing is False


@pytest.mark.parametrize("message", [
    "Requested format is not available",
    "Video unavailable. This content isn't available, try again later",
    "Sign in to confirm you're not a bot",
    "HTTP Error 403: Forbidden",
    "[Errno 23] Too many open files in system",
])
def test_failures_that_would_hit_any_video_count(message):
    assert _general.is_video_specific_error(Exception(message)) is False


def test_a_threshold_of_zero_never_pauses():
    health, _ = build_health(threshold=0)
    for _ in range(100):
        health.record_failure(BROKEN)

    assert (health.failing, health.pause_remaining()) == (False, 0)
