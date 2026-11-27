"""Whether downloads are working at all, so a broken downloader cannot use up the queue.

One video failing says something about that video. A run of failures with no success
between them says something about LidaTube, the network or the disk: carrying on would
only mark album after album as failed. While that lasts the queue pauses, then probes
with one album at a time until a download succeeds.
"""

import logging
import threading
import time

import _general
from _backoff import SharedCooldown


class DownloadHealth:
    # Pause before each probe, one step longer after every probe that fails.
    PAUSE_SECONDS = (60, 300, 900, 1800, 3600)

    def __init__(self, config, logger=None, clock=time.monotonic):
        self.config = config
        self.logger = logger or logging.getLogger(__name__)
        self._clock = clock
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        """Forget past failures; downloads count as working until shown otherwise."""
        with self._lock:
            self._failures = 0
            self._cooldown = SharedCooldown(self.PAUSE_SECONDS, clock=self._clock)

    @property
    def failing(self):
        """True once download_failure_threshold downloads in a row have failed (0 turns this off)."""
        threshold = int(self.config.download_failure_threshold)
        return threshold > 0 and self._failures >= threshold

    def pause_remaining(self):
        """Seconds before the next probe; 0 when downloads work or a probe is due."""
        return self._cooldown.remaining() if self.failing else 0

    def record_success(self):
        was_failing = self.failing
        self.reset()
        if was_failing:
            self.logger.warning("Downloads are working again")

    def record_failure(self, error):
        if _general.is_video_specific_error(error):
            return
        with self._lock:
            self._failures += 1
            # Downloads already running when the pause began must not lengthen it.
            if not self.failing or self._cooldown.remaining() > 0:
                return
            delay = self._cooldown.escalate()
        self.logger.error(
            f"The last {self._failures} downloads failed, so downloading pauses for {delay}s. "
            f"Latest error: {error}"
        )
