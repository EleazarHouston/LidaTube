import threading

import pytest

from _backoff import BackoffPolicy, SharedCooldown, call_with_backoff


class RecordingStop:
    """A stop event whose wait() records delays instead of sleeping."""

    def __init__(self, stop_after=None):
        self.waits = []
        self.stop_after = stop_after

    def wait(self, seconds):
        self.waits.append(seconds)
        return self.stop_after is not None and len(self.waits) >= self.stop_after


class Flaky(Exception):
    pass


class Blocked(Exception):
    pass


FLAKY = BackoffPolicy("flaky", (1, 2, 3), lambda error: isinstance(error, Flaky))
BLOCKED = BackoffPolicy("blocked", (10, 20), lambda error: isinstance(error, Blocked))


def _failing(*errors, result="ok"):
    remaining = list(errors)
    calls = []

    def operation():
        calls.append(1)
        if remaining:
            raise remaining.pop(0)
        return result

    return operation, calls


def test_returns_immediately_when_the_operation_succeeds():
    stop = RecordingStop()
    operation, calls = _failing()
    assert call_with_backoff(operation, (FLAKY,), stop) == "ok"
    assert calls == [1] and stop.waits == []


def test_waits_each_delay_in_order_then_succeeds():
    stop = RecordingStop()
    operation, calls = _failing(Flaky(), Flaky())
    assert call_with_backoff(operation, (FLAKY,), stop) == "ok"
    assert stop.waits == [1, 2] and len(calls) == 3


def test_reraises_an_unmatched_error_without_waiting():
    stop = RecordingStop()
    operation, calls = _failing(ValueError("not transient"))
    with pytest.raises(ValueError):
        call_with_backoff(operation, (FLAKY, BLOCKED), stop)
    assert stop.waits == [] and len(calls) == 1


def test_reraises_after_the_schedule_is_exhausted():
    stop = RecordingStop()
    operation, calls = _failing(*[Flaky() for _ in range(10)])
    with pytest.raises(Flaky):
        call_with_backoff(operation, (FLAKY,), stop)
    assert stop.waits == [1, 2, 3] and len(calls) == 4


def test_each_policy_keeps_its_own_attempt_count():
    stop = RecordingStop()
    operation, calls = _failing(Flaky(), Blocked(), Flaky(), Blocked())
    assert call_with_backoff(operation, (FLAKY, BLOCKED), stop) == "ok"
    assert stop.waits == [1, 10, 2, 20]


def test_first_matching_policy_wins():
    stop = RecordingStop()
    greedy = BackoffPolicy("greedy", (99,), lambda error: True)
    operation, _ = _failing(Flaky())
    call_with_backoff(operation, (FLAKY, greedy), stop)
    assert stop.waits == [1]


def test_stops_retrying_when_the_stop_event_fires_during_a_wait():
    stop = RecordingStop(stop_after=1)
    operation, calls = _failing(Flaky(), Flaky())
    with pytest.raises(Flaky):
        call_with_backoff(operation, (FLAKY,), stop)
    assert stop.waits == [1] and len(calls) == 1


def test_on_retry_receives_policy_attempt_delay_and_error():
    stop = RecordingStop()
    seen = []
    first = Flaky("first")
    operation, _ = _failing(first)
    call_with_backoff(operation, (FLAKY,), stop, on_retry=lambda *args: seen.append(args))
    assert seen == [(FLAKY, 1, 1, first)]


def test_works_with_a_real_threading_event():
    stop = threading.Event()
    operation, calls = _failing(Flaky())
    fast = BackoffPolicy("fast", (0,), lambda error: isinstance(error, Flaky))
    assert call_with_backoff(operation, (fast,), stop) == "ok"
    assert len(calls) == 2


# --- SharedCooldown: one pause every worker observes, instead of each burning its own schedule ---

class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _cooldown(delays=(60, 300, 900)):
    clock = FakeClock()
    return SharedCooldown(delays, clock=clock), clock


def test_a_fresh_cooldown_is_not_active():
    cooldown, _ = _cooldown()
    assert cooldown.remaining() == 0
    assert cooldown.wait(RecordingStop()) is True


def test_escalate_walks_the_schedule_and_holds_at_the_last_step():
    cooldown, clock = _cooldown()
    assert [cooldown.escalate() for _ in range(5)] == [60, 300, 900, 900, 900]


def test_escalate_sets_a_deadline_other_workers_can_see():
    cooldown, clock = _cooldown()
    cooldown.escalate()
    assert cooldown.remaining() == 60
    clock.advance(25)
    assert cooldown.remaining() == 35
    clock.advance(100)
    assert cooldown.remaining() == 0


def test_escalate_never_shortens_a_longer_running_cooldown():
    cooldown, clock = _cooldown(delays=(300, 60))
    cooldown.escalate()
    cooldown.escalate()
    assert cooldown.remaining() == 300


def test_clear_ends_the_cooldown_and_resets_the_schedule():
    cooldown, _ = _cooldown()
    cooldown.escalate()
    cooldown.clear()
    assert cooldown.remaining() == 0
    assert cooldown.escalate() == 60


def test_wait_blocks_until_the_deadline_passes():
    cooldown, clock = _cooldown(delays=(10,))
    cooldown.escalate()
    stop = RecordingStop()

    def tick(seconds):
        clock.advance(seconds)
        return False

    stop.wait = tick
    assert cooldown.wait(stop) is True
    assert cooldown.remaining() == 0


def test_wait_gives_up_when_the_stop_event_fires():
    cooldown, _ = _cooldown()
    cooldown.escalate()
    assert cooldown.wait(RecordingStop(stop_after=1)) is False
