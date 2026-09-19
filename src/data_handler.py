"""The composition root: builds LidaTube's components and serves settings to the UI.

DataHandler wires configuration, the store and the Lidarr, search, download, queue
and scheduler components together, starts crash recovery and the sync scheduler,
and handles the settings and connection events the web layer forwards to it.
"""

import logging
import os
import threading

from config import AppConfig
from download_queue import DownloadQueue
from downloader import Downloader
from fd_governor import FdGovernor
from lidarr_client import LidarrClient
from lidarr_scan import LidarrScanner
from link_search import LinkSearcher
from scheduler import SyncScheduler
from store import Store


class DataHandler:
    def __init__(self, emit, yield_to_event_loop=None):
        """emit(event, data) sends a Socket.IO event to the UI; yield_to_event_loop() lets a
        cooperative (gevent) worker run other greenlets during long loops."""
        self.emit = emit
        logging.basicConfig(level=logging.WARNING, format="%(message)s")
        self.logger = logging.getLogger()

        release_version = os.environ.get("RELEASE_VERSION", "unknown")
        self.logger.warning(f"{'*' * 50}\n")
        self.logger.warning(f"LidaTube Version: {release_version}\n")
        self.logger.warning(f"{'*' * 50}")

        # Configuration
        self.config = AppConfig(self.logger)
        self.config.save()
        self.store = Store(os.path.join(self.config.CONFIG_FOLDER, "lidatube.db"))
        self.fd = FdGovernor(self.logger)
        self.fd.apply_safety_limits(self.config)
        self.clients_connected_counter = 0

        # Components. One stop event halts every part of a download session: search, download and queue.
        download_stop_event = threading.Event()
        self.lidarr_client = LidarrClient(self.config, self.logger)
        self.downloader = Downloader(self.config, download_stop_event, self.logger)
        self.searcher = LinkSearcher(self.config, self.store, self.fd, download_stop_event, self.logger)
        self.scanner = LidarrScanner(
            self.config, self.lidarr_client, self.fd, self.emit, self.logger,
            download_stop_event=download_stop_event,
        )
        self.queue = DownloadQueue(
            self.config, self.store, self.scanner, self.searcher, self.downloader, self.lidarr_client,
            download_stop_event, self.emit, self.logger,
            yield_to_event_loop=yield_to_event_loop,
        )
        self.searcher.on_status_change = self.queue.emit_update
        self.scanner.on_album_scanned = self.queue.enqueue_scanned_album

        self.scanner.load_cache()
        self.logger.warning(
            "Thread limits in use: downloads=%s, lidarr_scan=%s, ytmusic_parallel=%s",
            self.config.thread_limit,
            self.config.lidarr_scan_thread_limit,
            self.searcher.parallel,
        )

        self.queue.auto_resume()
        self.scheduler = SyncScheduler(self.config, self.scanner, self.queue, self.logger)
        self.scheduler.start()

    # --- SocketIO connection ---

    def connect(self):
        self.scanner.emit_update()
        self.queue.snapshot()
        self.queue.emit_update()
        self.clients_connected_counter += 1

    def disconnect(self):
        self.clients_connected_counter = max(0, self.clients_connected_counter - 1)

    # --- Settings ---

    def load_settings(self):
        data = {
            "lidarr_address": self.config.lidarr_address,
            "lidarr_api_key": self.config.lidarr_api_key,
            "sleep_interval": self.config.sleep_interval,
            "sync_schedule": self.config.sync_schedule,
            "minimum_match_ratio": self.config.minimum_match_ratio,
        }
        self.emit("settings_loaded", data)

    def update_settings(self, data):
        try:
            self.config.lidarr_address = data["lidarr_address"]
            self.config.lidarr_api_key = data["lidarr_api_key"]
            self.config.sleep_interval = float(data["sleep_interval"])
            self.config.minimum_match_ratio = float(data["minimum_match_ratio"])
            self.config.sync_schedule = AppConfig.parse_sync_schedule(data["sync_schedule"])
            self.config.save()
            self.emit("new_toast_msg", {"title": "Settings", "message": "Settings saved successfully"})
        except Exception as e:
            self.logger.error(f"Failed to update settings: {e}")
            self.emit("new_toast_msg", {"title": "Settings Error", "message": str(e)})
