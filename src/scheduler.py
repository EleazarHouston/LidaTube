"""Scheduled syncs: at each hour listed in sync_schedule, fetch Lidarr's wanted list and download it.

During a sync the download queue runs in streaming mode, so each album starts
downloading as soon as its tracks are scanned instead of after the whole scan.
"""

import logging
import threading
import time


class SyncScheduler:
    def __init__(self, config, scanner, queue, logger=None):
        self.config = config
        self.scanner = scanner
        self.queue = queue
        self.logger = logger or logging.getLogger(__name__)

    def start(self):
        threading.Thread(target=self.run, name="Schedule_Thread", daemon=True).start()

    def run(self):
        while True:
            try:
                self._run_once()
            except Exception as e:
                # One failed sync (Lidarr down, database locked, no threads left) must not end
                # scheduled syncs for the life of the process.
                self.logger.error(f"Error in Scheduler, checking again in 10 minutes: {e}")
                time.sleep(600)

    def _run_once(self):
        current_hour = time.localtime().tm_hour
        within_time_window = any(t == current_hour for t in self.config.sync_schedule)

        if within_time_window:
            self.logger.warning(f"Time to Start - as in a time window: {self.config.sync_schedule}")
            try:
                self.queue.begin_streaming()
                fetch_thread = threading.Thread(target=self.scanner.fetch_wanted_albums, name="Lidarr_Fetch_Thread")
                fetch_thread.daemon = True
                fetch_thread.start()
                fetch_thread.join()
            finally:
                # Otherwise the queue keeps waiting for albums from a scan that is not running.
                self.queue.end_streaming()

            if not self.scanner.items:
                self.logger.warning("No Missing Albums")
            self.logger.warning("Big sleep for 1 Hour")
            time.sleep(3600)
            self.logger.warning(f"Checking every 10 minutes as not in a sync time window: {self.config.sync_schedule}")
        else:
            time.sleep(600)
