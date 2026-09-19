"""Shared retry-with-back-off for transient failures.

A BackoffPolicy pairs a classifier with a delay schedule. To make a new failure mode
back off, add a classifier (usually in _general) and a policy wherever the operation
runs; call_with_backoff handles waiting, attempt counting, stop requests and re-raising.
"""

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
