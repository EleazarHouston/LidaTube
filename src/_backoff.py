"""Shared retry-with-back-off for transient failures.

A BackoffPolicy pairs a classifier with a delay schedule. To make a new failure mode
back off, add a classifier (usually in _general) and a policy wherever the operation
runs; call_with_backoff handles waiting, attempt counting, stop requests and re-raising.
"""

import threading
import time
from dataclasses import dataclass
from typing import Callable, Tuple


@dataclass(frozen=True)
class BackoffPolicy:
    name: str
    delays: Tuple[float, ...]
    matches: Callable[[BaseException], bool]


def matching_policy(error, policies):
    """Return the first policy whose classifier matches the error, or None."""
    for policy in policies:
        if policy.matches(error):
            return policy
    return None


def call_with_backoff(operation, policies, stop_event, on_retry=None):
    """Run operation(), retrying after each policy's delays when its classifier matches.

    Each policy keeps its own attempt count, so a network blip does not consume the
    schedule for a YouTube block. The last error is re-raised when no policy matches,
    the matching policy's schedule is exhausted, or stop_event is set while waiting.
    on_retry(policy, attempt, delay, error) runs before each wait (attempt is 1-based).
    """
    attempts = {}
    while True:
        try:
            return operation()
        except Exception as error:
            policy = matching_policy(error, policies)
            if policy is None:
                raise
            attempt = attempts.get(policy.name, 0)
            if attempt >= len(policy.delays):
                raise
            attempts[policy.name] = attempt + 1
            delay = policy.delays[attempt]
            if on_retry is not None:
                on_retry(policy, attempt + 1, delay, error)
            if stop_event.wait(delay):
                raise


class SharedCooldown:
    """One pause that every worker observes after a shared failure.

    A per-worker schedule is wrong when the cause is shared: during a YouTube soft-block each
    album would wait out its own escalating schedule, so the few parallel search slots sit full
    of sleeping work. Here the first failure sets a deadline, repeats extend it one step further
    along the schedule, and a clean pass clears it.
    """

    WAIT_SLICE_SECONDS = 5.0

    def __init__(self, delays, clock=time.monotonic):
        self.delays = tuple(delays)
        self._clock = clock
        self._lock = threading.Lock()
        self._deadline = 0.0
        self._level = 0

    def remaining(self):
        """Seconds left before work may resume; 0 when the cooldown is not active."""
        with self._lock:
            return max(0.0, self._deadline - self._clock())

    def escalate(self):
        """Extend the cooldown by the next step of the schedule and return that delay."""
        with self._lock:
            if not self.delays:
                return 0
            delay = self.delays[min(self._level, len(self.delays) - 1)]
            self._level += 1
            self._deadline = max(self._deadline, self._clock() + delay)
            return delay

    def clear(self):
        """Drop the cooldown and start the schedule over; call this after a clean pass."""
        with self._lock:
            self._deadline = 0.0
            self._level = 0

    def wait(self, stop_event):
        """Block until the cooldown expires. False if stop_event fired first."""
        while True:
            remaining = self.remaining()
            if remaining <= 0:
                return True
            if stop_event.wait(min(remaining, self.WAIT_SLICE_SECONDS)):
                return False
