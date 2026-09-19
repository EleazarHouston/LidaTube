"""Open-file-descriptor governance for LidaTube's worker pools.

Every download holds yt-dlp, ffmpeg and HTTP sockets open at once, and a large
Lidarr scan opens one connection per album worker. Near the process's open-file
limit new sockets fail with EMFILE, so worker pools are sized to the limit at
startup and fd-hungry work pauses while usage stays high.
"""

import logging
import os
import resource
import threading
import time


class FdGovernor:
    PRESSURE_RATIO = 0.85
    BACKOFF_SECONDS = 10
    MAX_WAIT_SECONDS = 30

    def __init__(self, logger=None):
        self.logger = logger or logging.getLogger(__name__)
        self.fd_limit = self._read_fd_limit()
        self.exhaustion_event = threading.Event()
        self._backoff_lock = threading.Lock()
        self._last_pressure_log = 0.0

    @staticmethod
    def _read_fd_limit():
        try:
            soft_limit, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
            if soft_limit and soft_limit != resource.RLIM_INFINITY:
                return int(soft_limit)
        except Exception:
            pass
        return None

    def recommended_workers(self, configured_workers, estimated_fd_per_worker):
        try:
            configured = max(1, int(configured_workers))
        except (TypeError, ValueError):
            configured = 1

        if self.fd_limit is None:
            return configured

        reserved_fds = max(128, int(self.fd_limit * 0.35))
        available_budget = max(64, self.fd_limit - reserved_fds)
        recommended = max(1, available_budget // estimated_fd_per_worker)
        return min(configured, recommended)

    def apply_safety_limits(self, config):
        """Clamp the download and Lidarr scan pools in config to what the fd limit supports."""
        original_download_workers = config.thread_limit
        original_scan_workers = config.lidarr_scan_thread_limit

        config.thread_limit = self.recommended_workers(original_download_workers, estimated_fd_per_worker=160)
        config.lidarr_scan_thread_limit = self.recommended_workers(original_scan_workers, estimated_fd_per_worker=24)

        if config.thread_limit < max(1, int(original_download_workers)):
            self.logger.warning(
                "Clamped thread_limit from %s to %s based on open-file limit %s",
                original_download_workers,
                config.thread_limit,
                self.fd_limit,
            )
        if config.lidarr_scan_thread_limit < max(1, int(original_scan_workers)):
            self.logger.warning(
                "Clamped lidarr_scan_thread_limit from %s to %s based on open-file limit %s",
                original_scan_workers,
                config.lidarr_scan_thread_limit,
                self.fd_limit,
            )

    def open_fd_count(self):
        try:
            return len(os.listdir("/proc/self/fd"))
        except Exception:
            return None

    def is_pressure_high(self):
        if not self.fd_limit:
            return False
        open_count = self.open_fd_count()
        if open_count is None:
            return False
        ratio = open_count / self.fd_limit
        if ratio >= self.PRESSURE_RATIO:
            now = time.monotonic()
            if now - self._last_pressure_log >= 5:
                self._last_pressure_log = now
                self.logger.warning(
                    "FD usage high (%s/%s, %.0f%%)",
                    open_count,
                    self.fd_limit,
                    ratio * 100,
                )
            return True
        return False

    def signal_exhaustion(self):
        """Hold a back-off window during which wait_if_pressure blocks; no-op if one is running."""
        if not self._backoff_lock.acquire(blocking=False):
            return
        try:
            if self.exhaustion_event.is_set():
                return
            self.logger.warning(f"FD exhaustion detected — backing off for {self.BACKOFF_SECONDS} seconds")
            self.exhaustion_event.set()
            time.sleep(self.BACKOFF_SECONDS)
            self.exhaustion_event.clear()
            self.logger.warning("FD back-off cleared — resuming")
        finally:
            self._backoff_lock.release()

    def signal_exhaustion_in_background(self):
        threading.Thread(target=self.signal_exhaustion, daemon=True).start()

    def wait_if_pressure(self):
        """Block while an FD back-off is active (up to MAX_WAIT_SECONDS), starting one if usage is high."""
        if self.is_pressure_high():
            self.signal_exhaustion_in_background()
        if self.exhaustion_event.is_set():
            self.logger.warning(f"Waiting for FD pressure to clear (up to {self.MAX_WAIT_SECONDS}s)")
            deadline = time.monotonic() + self.MAX_WAIT_SECONDS
            while self.exhaustion_event.is_set() and time.monotonic() < deadline:
                time.sleep(0.5)
            self.logger.warning("Resuming after FD wait")
