import json
import logging
import os
import resource
import threading
import time
from datetime import datetime
import youtubesearchpython
from ytmusicapi import YTMusic
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO
import yt_dlp
import concurrent.futures
from thefuzz import fuzz
import _matcher
import _general
import library_suspicion
from config import AppConfig
from lidarr_client import LidarrClient
from downloader import Downloader
from store import Store


class DataHandler:
    _ARTIST_RETRY_WAIT = 10  # seconds per attempt; override in tests

    def __init__(self):
        logging.basicConfig(level=logging.WARNING, format="%(message)s")
        self.general_logger = logging.getLogger()

        app_name_text = os.path.basename(__file__).replace(".py", "")
        release_version = os.environ.get("RELEASE_VERSION", "unknown")
        self.general_logger.warning(f"{'*' * 50}\n")
        self.general_logger.warning(f"{app_name_text} Version: {release_version}\n")
        self.general_logger.warning(f"{'*' * 50}")

        # Configuration
        self.config = AppConfig(self.general_logger)
        self.config.save()
        self.store = Store(os.path.join(self.config.CONFIG_FOLDER, "lidatube.db"))
        self.current_session_id = None
        self._fd_limit = self._get_fd_limit()
        self._fd_pressure_ratio = 0.85
        self._fd_backoff_lock = threading.Lock()
        self._last_fd_pressure_log = 0.0
        self._apply_fd_safety_limits()

        # Lidarr state
        self.lidarr_items = []
        self.lidarr_futures = []
        self.lidarr_status = "idle"
        self.lidarr_stop_event = threading.Event()
        self.lidarr_scan_guard = threading.Lock()
        self.lidarr_scan_progress = self._default_lidarr_scan_progress()

        # Download state
        self.ytdlp_items = []
        self.ytdlp_futures = []
        self.ytdlp_status = "idle"
        self.ytdlp_stop_event = threading.Event()
        self.fd_exhaustion_event = threading.Event()
        ytmusic_parallel = max(1, min(2, int(self.config.thread_limit)))
        self._ytmusic_semaphore = threading.Semaphore(ytmusic_parallel)
        self.ytdlp_in_progress_flag = False
        self._reset_session_ids = set()
        self.index = 0
        self.percent_completion = 0
        self.batch_number = 0
        self.queue_progress = {
            "session_id": None,
            "pending": 0,
            "in_progress": 0,
            "done": 0,
            "error": 0,
            "total": 0,
            "batch": 0,
            "matched": 0,
            "failed": 0,
        }

        self.streaming_mode = False
        self.clients_connected_counter = 0

        # Sub-components
        self.lidarr_client = LidarrClient(self.config, self.general_logger)
        self.downloader = Downloader(self.config, self.ytdlp_stop_event, self.general_logger)

        self._load_lidarr_cache()
        self.general_logger.warning(
            "Thread limits in use: downloads=%s, lidarr_scan=%s, ytmusic_parallel=%s",
            self.config.thread_limit,
            self.config.lidarr_scan_thread_limit,
            ytmusic_parallel,
        )

        self._auto_resume_if_available()
        thread = threading.Thread(target=self.schedule_checker, name="Schedule_Thread")
        thread.daemon = True
        thread.start()

    # --- SocketIO connection ---

    def connect(self):
        self._emit_lidarr_update()
        self.queue_status()
        self._emit_ytdlp_update()
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
        socketio.emit("settings_loaded", data)

    def update_settings(self, data):
        try:
            self.config.lidarr_address = data["lidarr_address"]
            self.config.lidarr_api_key = data["lidarr_api_key"]
            self.config.sleep_interval = float(data["sleep_interval"])
            self.config.minimum_match_ratio = float(data["minimum_match_ratio"])
            self.config.sync_schedule = AppConfig.parse_sync_schedule(data["sync_schedule"])
            self.config.save()
            socketio.emit("new_toast_msg", {"title": "Settings", "message": "Settings saved successfully"})
        except Exception as e:
            self.general_logger.error(f"Failed to update settings: {e}")
            socketio.emit("new_toast_msg", {"title": "Settings Error", "message": str(e)})

    # --- Scheduler ---

    def schedule_checker(self):
        try:
            while True:
                current_hour = time.localtime().tm_hour
                within_time_window = any(t == current_hour for t in self.config.sync_schedule)

                if within_time_window:
                    self.general_logger.warning(f"Time to Start - as in a time window: {self.config.sync_schedule}")
                    self.streaming_mode = True
                    self.ytdlp_stop_event.clear()

                    if not self.ytdlp_in_progress_flag:
                        self.ytdlp_items = []
                        self.percent_completion = 0
                        self.index = 0
                        self.current_session_id = self.store.start_session(requested_count=0)
                        self._start_queue_thread(self.current_session_id)

                    fetch_thread = threading.Thread(target=self.get_wanted_albums_from_lidarr, name="Lidarr_Fetch_Thread")
                    fetch_thread.daemon = True
                    fetch_thread.start()

                    fetch_thread.join()
                    self.streaming_mode = False

                    if not self.lidarr_items:
                        self.general_logger.warning("No Missing Albums")
                    self.general_logger.warning("Big sleep for 1 Hour")
                    time.sleep(3600)
                    self.general_logger.warning(f"Checking every 10 minutes as not in a sync time window: {self.config.sync_schedule}")
                else:
                    time.sleep(600)

        except Exception as e:
            self.general_logger.error(f"Error in Scheduler: {e}")
            self.general_logger.error("Scheduler Stopped")

    # --- Lidarr scan state helpers ---

    def _default_lidarr_scan_progress(self):
        return {
            "phase": "Idle",
            "pages_scanned": 0,
            "albums_discovered": 0,
            "albums_processed": 0,
            "albums_total": 0,
            "percent": 0,
        }

    def _set_lidarr_scan_progress(self, **kwargs):
        self.lidarr_scan_progress.update(kwargs)

    def _emit_lidarr_update(self):
        # Only send albums that need attention: unscanned (still processing) or have missing tracks.
        # Fully-downloaded albums (missing_count=0, scan_ready=True) are omitted to keep the
        # payload small — serialising 90k+ items on every connect blocks the gevent event loop.
        # Each item carries its original index so the client can reference self.lidarr_items correctly.
        slim_items = []
        for i, item in enumerate(self.lidarr_items):
            if item.get("missing_count", 0) > 0 or not item.get("scan_ready", False):
                slim = {k: v for k, v in item.items() if k != "missing_tracks"}
                slim["index"] = i
                slim_items.append(slim)
        socketio.emit("lidarr_update", {
            "status": self.lidarr_status,
            "data": slim_items,
            "scan_progress": self.lidarr_scan_progress,
            "total_count": len(self.lidarr_items),
        })

    def _emit_lidarr_progress(self):
        """Emit only scan progress stats — no data array. Use during tight loops to avoid O(n²) socket traffic."""
        socketio.emit("lidarr_update", {"status": self.lidarr_status, "data": None, "scan_progress": self.lidarr_scan_progress})

    def _emit_ytdlp_update(self):
        # The current batch is bounded; omit its per-track arrays from frequent updates.
        items = [
            {
                "artist": item.get("artist", ""),
                "album_name": item.get("album_name", ""),
                "status": item.get("status", ""),
            }
            for item in self.ytdlp_items
        ]
        socketio.emit(
            "ytdlp_update",
            {
                "status": self.ytdlp_status,
                "data": items,
                "percent_completion": self.percent_completion,
                "queue": dict(self.queue_progress),
            },
        )

    def _refresh_queue_progress(self, session_id=None):
        session_id = session_id or self.current_session_id
        if session_id is None:
            return self.queue_progress
        counts = self.store.queue_counts(session_id)
        results = self.store.get_session_result_counts(session_id)
        self.queue_progress = {
            "session_id": session_id,
            **counts,
            "batch": self.batch_number,
            "matched": results["matched_count"],
            "failed": results["failed_count"],
        }
        completed = counts["done"] + counts["error"]
        self.percent_completion = 100 * completed / counts["total"] if counts["total"] else 0
        return self.queue_progress

    def queue_status(self):
        session_id = self.current_session_id
        if session_id is None:
            resumable = self.store.resumable_session()
            session_id = resumable["id"] if resumable else None
        if session_id is not None:
            self._refresh_queue_progress(session_id)
        return dict(self.queue_progress)

    # --- Lidarr cache ---

    def _lidarr_cache_path(self):
        return os.path.join(self.config.CONFIG_FOLDER, "lidarr_cache.json")

    def _save_lidarr_cache(self):
        try:
            import json
            cache_path = self._lidarr_cache_path()
            tmp_path = cache_path + ".tmp"
            with open(tmp_path, "w") as f:
                json.dump({"lidarr_items": self.lidarr_items, "lidarr_scan_progress": self.lidarr_scan_progress}, f)
            os.replace(tmp_path, cache_path)
            self.general_logger.warning(f"Saved {len(self.lidarr_items)} albums to Lidarr cache")
        except Exception as e:
            self.general_logger.error(f"Error saving Lidarr cache: {e}")

    def _load_lidarr_cache(self):
        try:
            import json
            cache_path = self._lidarr_cache_path()
            if os.path.exists(cache_path):
                with open(cache_path, "r") as f:
                    cache = json.load(f)
                self.lidarr_items = cache.get("lidarr_items", [])
                cached_progress = cache.get("lidarr_scan_progress", {})
                cached_progress["phase"] = "Complete (cached)"
                self.lidarr_scan_progress.update(cached_progress)
                self.lidarr_status = "complete"
                self.general_logger.warning(f"Loaded {len(self.lidarr_items)} albums from Lidarr cache")
        except Exception as e:
            self.general_logger.error(f"Error loading Lidarr cache: {e}")

    def _clear_lidarr_cache(self):
        removed = 0
        cache_path = self._lidarr_cache_path()
        for path in (cache_path, cache_path + ".tmp"):
            if os.path.exists(path):
                os.remove(path)
                removed += 1
        self.general_logger.warning(f"Cleared {removed} Lidarr cache file(s)")
        return removed

    # --- Lidarr wanted albums ---

    def get_wanted_albums_from_lidarr(self):
        try:
            self.general_logger.warning("Accessing Lidarr API")
            self.lidarr_status = "busy"
            self.lidarr_stop_event.clear()
            self.lidarr_items = []
            self._set_lidarr_scan_progress(
                phase="Fetching wanted albums",
                pages_scanned=0,
                albums_discovered=0,
                albums_processed=0,
                albums_total=0,
                percent=0,
            )
            self._emit_lidarr_update()

            self.general_logger.warning("Pre-fetching artists from Lidarr")
            self._set_lidarr_scan_progress(phase="Fetching artists")
            self._emit_lidarr_update()
            artist_lookup = {}
            artist_page = 1
            artist_max_retries = 3
            while True:
                last_error = None
                for attempt in range(artist_max_retries):
                    try:
                        response = self.lidarr_client.get_artists_page(artist_page)
                        try:
                            if response.status_code != 200:
                                raise RuntimeError(f"Lidarr artist API error {response.status_code}: {response.text}")
                            data = response.json()
                        finally:
                            response.close()
                        last_error = None
                        break
                    except Exception as e:
                        last_error = e
                        wait = self._ARTIST_RETRY_WAIT * (attempt + 1)
                        self.general_logger.warning(f"Artist fetch attempt {attempt + 1}/{artist_max_retries} failed: {e} — retrying in {wait}s")
                        socketio.emit("new_toast_msg", {"title": f"Artist fetch retry {attempt + 1}/{artist_max_retries}", "message": str(e)})
                        self._interruptible_sleep(wait)
                if last_error:
                    raise last_error
                # Lidarr may return a flat array or a paginated object depending on version
                if isinstance(data, list):
                    for a in data:
                        artist_lookup[a["id"]] = a
                    break
                records = data.get("records", [])
                if not records:
                    break
                for a in records:
                    artist_lookup[a["id"]] = a
                artist_page += 1
            self.general_logger.warning(f"Fetched {len(artist_lookup)} artists")

            page = 1
            page_size = 1000
            scan_worker_count = max(1, int(self.config.lidarr_scan_thread_limit))
            self.general_logger.warning(f"Fetching wanted albums (pageSize={page_size}) and missing tracks with {scan_worker_count} worker(s)")

            future_map = {}
            total_albums = 0
            albums_processed = 0
            undrained_futures = set()

            with concurrent.futures.ThreadPoolExecutor(max_workers=scan_worker_count) as executor:
                while True:
                    if self.lidarr_stop_event.is_set():
                        break

                    response = self.lidarr_client.get_wanted_albums(page, page_size)
                    try:
                        if response.status_code != 200:
                            self.general_logger.error(f"Lidarr Wanted API Error Code: {response.status_code}")
                            self.general_logger.error(f"Lidarr Wanted API Error Text: {response.text}")
                            socketio.emit("new_toast_msg", {"title": f"Lidarr API Error: {response.status_code}", "message": response.text})
                            break
                        wanted_missing_albums = response.json()
                    finally:
                        response.close()

                    if not wanted_missing_albums["records"]:
                        break
                    albums_on_page = wanted_missing_albums["records"]
                    del wanted_missing_albums

                    for album in albums_on_page:
                        if self.lidarr_stop_event.is_set():
                            break
                        artist = artist_lookup.get(album["artistId"], {})
                        parsed_date = datetime.fromisoformat(album["releaseDate"].replace("Z", "+00:00"))
                        album_year = parsed_date.year
                        album_name = _general.convert_to_lidarr_format(album["title"])
                        album_folder = f"{album_name} ({album_year})"
                        album_full_path = os.path.join(artist.get("path", ""), album_folder)
                        album_release_id = album["releases"][0]["id"]
                        new_item = {
                            "artist_id": album["artistId"],
                            "artist_path": artist.get("path", ""),
                            "artist": artist.get("artistName", ""),
                            "album_name": album_name,
                            "album_folder": album_folder,
                            "album_full_path": album_full_path,
                            "album_year": album_year,
                            "album_id": album["id"],
                            "album_release_id": album_release_id,
                            "album_genres": ", ".join(album["genres"]),
                            "track_count": 0,
                            "missing_count": 0,
                            "missing_tracks": [],
                            "checked": True,
                            "scan_ready": False,
                            "scan_in_progress": False,
                            "status": "",
                        }
                        self.lidarr_items.append(new_item)
                        future = executor.submit(self.get_missing_tracks_for_album, new_item)
                        future_map[future] = new_item
                        undrained_futures.add(future)
                    del albums_on_page

                    # Drain completed track futures while next page is being fetched
                    newly_done = [f for f in undrained_futures if f.done()]
                    for f in newly_done:
                        undrained_futures.discard(f)
                        req_album = future_map[f]
                        try:
                            f.result()
                        except Exception as e:
                            self.general_logger.error(f'Error Getting Missing Tracks for {req_album["artist"]} - {req_album["album_name"]}: {e}')
                        albums_processed += 1

                    self.lidarr_futures = list(future_map.keys())
                    discovered = len(self.lidarr_items)
                    self._set_lidarr_scan_progress(
                        phase="Fetching albums & tracks",
                        pages_scanned=page,
                        albums_discovered=discovered,
                        albums_processed=albums_processed,
                        albums_total=discovered,
                        percent=int(albums_processed / discovered * 100) if discovered else 0,
                    )
                    self._emit_lidarr_update()
                    self._save_lidarr_cache()
                    page += 1

                self.lidarr_items.sort(key=lambda x: (x["artist"], x["album_name"]))
                total_albums = len(self.lidarr_items)
                self._set_lidarr_scan_progress(
                    phase="Fetching missing tracks",
                    albums_total=total_albums,
                    albums_processed=albums_processed,
                    percent=int(albums_processed / total_albums * 100) if total_albums else 100,
                )
                self._emit_lidarr_update()

                for future in concurrent.futures.as_completed(undrained_futures):
                    if self.lidarr_stop_event.is_set():
                        break
                    req_album = future_map[future]
                    try:
                        future.result()
                    except Exception as e:
                        self.general_logger.error(f'Error Getting Missing Tracks for {req_album["artist"]} - {req_album["album_name"]}: {e}')
                    albums_processed += 1
                    percent = int((albums_processed / total_albums) * 100) if total_albums else 100
                    self._set_lidarr_scan_progress(phase="Fetching missing tracks", albums_processed=albums_processed, percent=percent)
                    self._emit_lidarr_progress()
                self._emit_lidarr_update()

            self.lidarr_status = "stopped" if self.lidarr_stop_event.is_set() else "complete"
            if self.lidarr_status == "complete":
                self._set_lidarr_scan_progress(phase="Complete", albums_processed=total_albums, albums_total=total_albums, percent=100)
                self._save_lidarr_cache()
            elif self.lidarr_status == "stopped":
                self._set_lidarr_scan_progress(phase="Stopped")

        except Exception as e:
            self.general_logger.error(f"Error Getting Missing Albums: {e}")
            self.lidarr_status = "error"
            self._set_lidarr_scan_progress(phase="Error")
            socketio.emit("new_toast_msg", {"title": "Error Getting Missing Albums", "message": str(e)})

        finally:
            self._emit_lidarr_update()

    def get_missing_tracks_for_album(self, req_album):
        while True:
            with self.lidarr_scan_guard:
                if req_album.get("scan_ready", False):
                    return
                if not req_album.get("scan_in_progress", False):
                    req_album["scan_in_progress"] = True
                    break
            if self.ytdlp_stop_event.is_set() or self.lidarr_stop_event.is_set():
                return
            time.sleep(0.1)

        self.general_logger.warning(f'Reading Missing Track list of {req_album["artist"]} - {req_album["album_name"]} from Lidarr API')
        last_error = None

        for attempt in range(3):
            try:
                req_album["missing_tracks"] = []
                req_album["track_count"] = 0
                req_album["missing_count"] = 0
                self._wait_if_fd_pressure()
                if self.lidarr_stop_event.is_set():
                    return

                response = self.lidarr_client.get_tracks_for_album(req_album["album_id"])
                try:
                    if response.status_code == 200:
                        tracks = response.json()
                        track_count = len(tracks)
                        for track in tracks:
                            if self.lidarr_stop_event.is_set():
                                del tracks
                                return
                            if not track.get("hasFile", False):
                                new_item = {
                                    "artist": req_album["artist"],
                                    "track_title": track["title"],
                                    "track_number": track["trackNumber"],
                                    "absolute_track_number": track["absoluteTrackNumber"],
                                    "track_id": track["id"],
                                    "duration_ms": track.get("duration", 0),
                                    "link": "",
                                    "title_of_link": "",
                                }
                                req_album["missing_tracks"].append(new_item)
                        del tracks
                        req_album["track_count"] = track_count
                        req_album["missing_count"] = len(req_album["missing_tracks"])
                        last_error = None
                    else:
                        self.general_logger.error(req_album["album_name"])
                        self.general_logger.error(f"Lidarr Track API Error Code: {response.status_code}")
                        self.general_logger.error(f"Lidarr Track API Error Text: {response.text}")
                finally:
                    response.close()
                break

            except Exception as e:
                last_error = e
                if _general.is_resource_exhaustion_error(e) and attempt < 2:
                    self.general_logger.warning(f'FD exhaustion on track fetch attempt {attempt + 1}, backing off: {req_album["album_name"]}')
                    threading.Thread(target=self._signal_fd_exhaustion, daemon=True).start()
                    continue
                break

        if last_error is not None:
            self.general_logger.error(req_album["album_name"])
            self.general_logger.error(f"Error Getting Missing Tracks: {last_error}")
            socketio.emit("new_toast_msg", {"title": "Error Getting Missing Tracks", "message": str(last_error)})

        req_album["scan_in_progress"] = False
        req_album["scan_ready"] = True
        self.general_logger.warning(
            f'Track scan complete: {req_album["artist"]} - {req_album["album_name"]} '
            f'({req_album["missing_count"]} missing of {req_album["track_count"]} tracks)'
        )

        if self.streaming_mode:
            req_album["status"] = "Queued"
            if self.current_session_id is None:
                self.current_session_id = self.store.start_session(requested_count=0)
                self._start_queue_thread(self.current_session_id)
            self.store.enqueue_items(self.current_session_id, [req_album])
            self.store.increment_session_requested_count(
                self.current_session_id, len(req_album.get("missing_tracks", []))
            )

    # --- Lidarr actions ---

    def attempt_lidarr_song_import(self, req_album, song, filename):
        response = None
        try:
            self.general_logger.warning("Attempting import of song via Lidarr API")
            response = self.lidarr_client.import_song(req_album, song, filename)
            if response.status_code == 202:
                self.general_logger.warning("Song import initiated")
            else:
                self.general_logger.error(f"Import Attempt - Failed to initiate song import: {response.status_code}")
                self.general_logger.error(f"Import Attempt - Error message: {response.text}")
        except Exception as e:
            self.general_logger.error(f"Error occurred while attempting import of song: {e}")
        finally:
            if response is not None:
                response.close()

    def import_album(self, req_album):
        """Import a downloaded album into the library via Lidarr's manual import.

        Scans the album's folder (staging when lidarr_download_path is set, else the
        in-place library folder), then posts a ManualImport 'move' command using
        Lidarr's own parsed candidates.
        """
        if self.config.lidarr_download_path:
            artist_str = os.path.basename(req_album["artist_path"].rstrip("/"))
            folder = os.path.join(self.config.lidarr_download_path, artist_str, req_album["album_folder"])
        else:
            folder = req_album["album_full_path"]
        try:
            scan = self.lidarr_client.scan_import_candidates(folder)
            if scan.status_code != 200:
                self.general_logger.error(f"Import scan failed ({scan.status_code}) for {folder}")
                return
            response, count = self.lidarr_client.import_candidates(scan.json(), import_mode="move")
            if count == 0:
                self.general_logger.warning(f"No importable files found in {folder}")
            elif response.status_code in (200, 201):
                self.general_logger.warning(
                    f'Import queued for {count} file(s): {req_album["artist"]} - {req_album["album_name"]}'
                )
            else:
                self.general_logger.error(f"Import command failed ({response.status_code}): {response.text}")
        except Exception as e:
            self.general_logger.error(f"Error importing album via Lidarr: {e}")

    def trigger_lidarr_scan(self):
        response = None
        try:
            root_folders = self.lidarr_client.get_root_folders()
            if not root_folders:
                self.general_logger.warning("No Lidarr root folders found")
                return
            response = self.lidarr_client.trigger_library_scan(root_folders)
            if response.status_code != 201:
                self.general_logger.warning("Failed to start lidarr library scan")
            else:
                self.general_logger.warning("Lidarr library scan started")
        except Exception as e:
            self.general_logger.error(f"Lidarr library scan failed: {e}")
        finally:
            if response is not None:
                response.close()

    def reset_lidarr(self):
        cache_cleanup_error = None
        cache_removed_count = 0
        try:
            self.lidarr_stop_event.set()
            for future in self.lidarr_futures:
                if not future.done():
                    future.cancel()
            self.lidarr_futures = []
            self.lidarr_items = []
            self.lidarr_status = "idle"
            self.lidarr_scan_progress = self._default_lidarr_scan_progress()
            cache_removed_count = self._clear_lidarr_cache()
            self.general_logger.warning("Lidarr reset complete")
        except Exception as e:
            cache_cleanup_error = str(e)
            self.general_logger.error(f"Lidarr reset failed: {e}")
        finally:
            self._emit_lidarr_update()
            if cache_cleanup_error:
                socketio.emit("new_toast_msg", {"title": "Lidarr Reset Error", "message": cache_cleanup_error})
            else:
                if cache_removed_count:
                    msg = f"Reset complete. Cleared {cache_removed_count} cache file(s)."
                else:
                    msg = "Reset complete. No cache files were present."
                socketio.emit("new_toast_msg", {"title": "Lidarr Reset", "message": msg})

    # --- Download queue ---

    def _auto_resume_if_available(self):
        if getattr(self.config, "auto_resume", True):
            self.resume_ytdlp(emit=False)

    def _start_queue_thread(self, session_id):
        if self.ytdlp_in_progress_flag:
            return False
        self.current_session_id = session_id
        self.ytdlp_stop_event.clear()
        self.ytdlp_status = "running"
        self.ytdlp_in_progress_flag = True
        thread = threading.Thread(
            target=self.master_queue,
            args=(session_id,),
            name="Queue_Thread",
            daemon=True,
        )
        thread.start()
        return True

    def resume_ytdlp(self, emit=True):
        if self.ytdlp_in_progress_flag:
            return False
        session = self.store.resumable_session()
        if session is None:
            return False
        self.batch_number = 0
        self.index = 0
        self.ytdlp_items = []
        self.store.resume_session(session["id"])
        started = self._start_queue_thread(session["id"])
        if emit:
            self._refresh_queue_progress(session["id"])
            self._emit_ytdlp_update()
        return started

    def add_items_to_download(self, data):
        session_id = None
        created_session = False
        added = 0
        try:
            selected = {
                int(index) for index in data
                if isinstance(index, int) and 0 <= index < len(self.lidarr_items)
            }
            if not selected:
                raise ValueError("No valid albums were selected")

            self.ytdlp_stop_event.clear()
            if self.ytdlp_in_progress_flag and self.current_session_id is not None:
                session_id = self.current_session_id
            else:
                session_id = self.store.start_session(requested_count=0)
                self.current_session_id = session_id
                created_session = True
                self.batch_number = 0
                self.index = 0
                self.ytdlp_items = []
                self.percent_completion = 0

            chunk = []
            chunk_tracks = 0
            for index, item in enumerate(self.lidarr_items):
                item["checked"] = index in selected
                if not item["checked"]:
                    continue
                item["status"] = (
                    "Queued" if item.get("scan_ready", True)
                    else "Waiting for refresh data"
                )
                chunk.append(item)
                chunk_tracks += len(item.get("missing_tracks", []))
                if len(chunk) >= 500:
                    added += self.store.enqueue_items(session_id, chunk)
                    self.store.increment_session_requested_count(session_id, chunk_tracks)
                    chunk = []
                    chunk_tracks = 0
                    socketio.sleep(0)

            if chunk:
                added += self.store.enqueue_items(session_id, chunk)
                self.store.increment_session_requested_count(session_id, chunk_tracks)

            self._refresh_queue_progress(session_id)
            self.general_logger.warning(
                f"Added {added} album(s) to persisted download queue for session {session_id}"
            )

            if created_session:
                self._start_queue_thread(session_id)

        except Exception as e:
            self.general_logger.error(str(e))
            if created_session and session_id is not None:
                self.store.finish_session(session_id, "failed")
                if self.current_session_id == session_id:
                    self.current_session_id = None
            socketio.emit("new_toast_msg", {"title": "Error adding new items", "message": str(e)})

        finally:
            self._emit_ytdlp_update()
            if added:
                socketio.emit(
                    "new_toast_msg",
                    {"title": "Download Queue Updated", "message": f"Added {added} album(s) to queue"},
                )

    def master_queue(self, session_id=None):
        session_id = session_id or self.current_session_id
        try:
            if session_id is None:
                self.ytdlp_status = "complete"
                self.ytdlp_in_progress_flag = False
                return

            self.current_session_id = session_id
            self.store.resume_session(session_id)
            self.ytdlp_status = "running"
            self.ytdlp_futures = []
            batch_size = max(1, int(getattr(self.config, "batch_size", 200)))
            self.general_logger.warning(
                f"Master queue started: session={session_id}, batch_size={batch_size}, "
                f"thread_limit={self.config.thread_limit}"
            )

            while not self.ytdlp_stop_event.is_set():
                batch_rows = self.store.next_batch(session_id, batch_size)
                if not batch_rows:
                    if self.streaming_mode:
                        self.ytdlp_stop_event.wait(0.25)
                        continue
                    break

                self.batch_number += 1
                self.index = 0
                self.ytdlp_items = []
                work_items = []
                for row in batch_rows:
                    try:
                        req_album = json.loads(row["album_json"])
                    except (TypeError, ValueError):
                        self.store.mark_queue_item(row["id"], "error")
                        continue
                    req_album["_queue_item_id"] = row["id"]
                    self.ytdlp_items.append(req_album)
                    work_items.append((row["id"], req_album))

                self.store.mark_queue_items(
                    [queue_item_id for queue_item_id, _ in work_items], "in_progress"
                )
                self._refresh_queue_progress(session_id)
                self._emit_ytdlp_update()

                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=self.config.thread_limit
                ) as executor:
                    future_items = {
                        executor.submit(self._process_queue_item, queue_item_id, req_album): queue_item_id
                        for queue_item_id, req_album in work_items
                    }
                    self.ytdlp_futures = list(future_items)
                    for future in concurrent.futures.as_completed(future_items):
                        queue_item_id = future_items[future]
                        try:
                            future.result()
                        except concurrent.futures.CancelledError:
                            self.store.mark_queue_item(queue_item_id, "pending")

                self._refresh_queue_progress(session_id)
                self._emit_ytdlp_update()
                # Yield between bounded batches so the gevent worker can service heartbeats.
                if not self.ytdlp_stop_event.wait(0.05):
                    socketio.sleep(0)

            if session_id in self._reset_session_ids:
                self.ytdlp_status = "idle"
            elif self.ytdlp_stop_event.is_set():
                self.ytdlp_status = "stopped"
                self.general_logger.warning("Downloading Stopped")
            else:
                self.ytdlp_status = "complete"
                self.general_logger.warning("Downloading Finished")
                if self.config.library_scan_on_completion:
                    self.trigger_lidarr_scan()

        except Exception as e:
            self.general_logger.error(f"Error in Master Queue: {e}")
            self.ytdlp_status = "failed"
            socketio.emit("new_toast_msg", {"title": "Error in Master Queue", "message": str(e)})

        finally:
            self.ytdlp_in_progress_flag = False
            self._reset_session_ids.discard(session_id)
            if session_id is not None and self.current_session_id == session_id:
                self._refresh_queue_progress(session_id)
                counts = self.store.get_session_result_counts(session_id)
                self.store.finish_session(session_id, self.ytdlp_status, **counts)
                self.current_session_id = None
            self._emit_ytdlp_update()
            socketio.emit("new_toast_msg", {"title": "End of Session", "message": f"Downloading {self.ytdlp_status.capitalize()}"})

    def _process_queue_item(self, queue_item_id, req_album):
        queue_status = "error"
        try:
            self.find_link_and_download(req_album)
            if self.ytdlp_stop_event.is_set():
                queue_status = "pending"
            elif req_album.get("status") in ("Download Error", "Refresh data unavailable"):
                queue_status = "error"
            else:
                queue_status = "done"
        except Exception as e:
            req_album["status"] = "Download Error"
            self.general_logger.error(f"Unhandled queue item error: {e}")
        finally:
            self.store.mark_queue_item(queue_item_id, queue_status)
            self._refresh_queue_progress(self.current_session_id)
            self._emit_ytdlp_update()

    def _wait_for_album_scan_data(self, req_album):
        if req_album.get("scan_ready", True):
            return True

        req_album["status"] = "Waiting for refresh data"
        self._emit_ytdlp_update()

        if not req_album.get("scan_in_progress", False):
            self.general_logger.warning(f'Prioritizing scan for queued album: {req_album["artist"]} - {req_album["album_name"]}')
            self.get_missing_tracks_for_album(req_album)
            if req_album.get("scan_ready", False):
                return True

        while not self.ytdlp_stop_event.is_set():
            if req_album.get("scan_ready", False):
                return True
            if self.lidarr_status != "busy":
                return req_album.get("scan_ready", False)
            self.ytdlp_stop_event.wait(0.5)

        return False

    def find_link_and_download(self, req_album):
        try:
            if not self._wait_for_album_scan_data(req_album):
                req_album["status"] = "Download Stopped" if self.ytdlp_stop_event.is_set() else "Refresh data unavailable"
                return

            self._link_finder(req_album)
            if self.ytdlp_stop_event.is_set():
                req_album["status"] = "Download Stopped"
                return

            req_album["status"] = "Starting Download"
            artist_str = os.path.basename(req_album["artist_path"].rstrip("/"))
            album_name = req_album["album_name"]
            folder_with_year = req_album["album_folder"]
            grabbed_count = existing_count = error_count = 0
            song_links = [x for x in req_album["missing_tracks"] if x["link"] != ""]
            total_req = len(song_links)
            self.general_logger.warning(f"Valid link count of {total_req} for: {artist_str} - {album_name}")

            for song in song_links:
                if self.ytdlp_stop_event.is_set():
                    break

                title = song["title_of_link"]
                link = song["link"]
                self.general_logger.warning(f"Starting Download of: {title}")
                title_str = _general.convert_to_lidarr_format(title)
                track_number = str(song["absolute_track_number"]).zfill(2)
                file_name = os.path.join(artist_str, folder_with_year, f"{artist_str} - {album_name} - {track_number} - {title_str}")
                full_file_path_with_ext = os.path.join(self.config.download_folder, f"{file_name}.{self.config.preferred_codec}")

                if os.path.exists(full_file_path_with_ext):
                    existing_count += 1
                    self.general_logger.warning(f"File Already Exists: {artist_str} - {title_str}")
                else:
                    success = self.downloader.download(link, file_name)
                    if success:
                        _general.add_metadata(self.general_logger, song, req_album, full_file_path_with_ext)
                        grabbed_count += 1
                        self.ytdlp_stop_event.wait(self.config.sleep_interval)
                        if self.ytdlp_stop_event.is_set():
                            break
                    else:
                        error_count += 1
                        if self.ytdlp_stop_event.is_set():
                            break

                song_processed_count = grabbed_count + error_count + existing_count
                req_album["status"] = f"Processed: {song_processed_count} of {total_req}"
                self._emit_ytdlp_update()

            if self.config.attempt_lidarr_import and grabbed_count > 0 and not self.ytdlp_stop_event.is_set():
                self.import_album(req_album)

            if self.ytdlp_stop_event.is_set():
                req_album["status"] = "Download Stopped"
            elif total_req < req_album["missing_count"]:
                req_album["status"] = "Album Incomplete"
            elif grabbed_count + existing_count == total_req:
                req_album["status"] = "Download Complete"
            elif error_count == total_req:
                req_album["status"] = "Download Failed"
            else:
                req_album["status"] = "Partially Complete"

            self.general_logger.warning(
                f'Download summary for {artist_str} - {album_name}: '
                f'grabbed={grabbed_count}, existing={existing_count}, errors={error_count}, total={total_req} | status: {req_album["status"]}'
            )

        except Exception as e:
            self.general_logger.error(f"Error Downloading: {e}")
            req_album["status"] = "Download Error"

        finally:
            self.index += 1
            self._emit_ytdlp_update()

    def stop_ytdlp(self):
        try:
            self.ytdlp_stop_event.set()
            for future in self.ytdlp_futures:
                if not future.done():
                    future.cancel()
            for item in self.ytdlp_items:
                if item.get("status") not in (
                    "Download Complete", "Album Incomplete", "Download Failed", "Partially Complete"
                ):
                    item["status"] = "Download Stopped"
        except Exception as e:
            self.general_logger.error(f"Error Stopping yt_dlp: {e}")
        finally:
            self.ytdlp_status = "stopped"
            self._emit_ytdlp_update()

    def reset_ytdlp(self):
        session_id = self.current_session_id or self.queue_progress.get("session_id")
        try:
            self.ytdlp_stop_event.set()
            for future in self.ytdlp_futures:
                if not future.done():
                    future.cancel()
            if session_id is not None:
                self._reset_session_ids.add(session_id)
                self.store.clear_queue(session_id)
                counts = self.store.get_session_result_counts(session_id)
                self.store.finish_session(session_id, "reset", **counts)
            self.current_session_id = None
            self.ytdlp_futures = []
            self.ytdlp_items = []
            self.ytdlp_status = "idle"
            self.ytdlp_in_progress_flag = False
            self.index = 0
            self.percent_completion = 0
            self.batch_number = 0
            self.queue_progress = {
                "session_id": None,
                "pending": 0,
                "in_progress": 0,
                "done": 0,
                "error": 0,
                "total": 0,
                "batch": 0,
                "matched": 0,
                "failed": 0,
            }
        except Exception as e:
            self.general_logger.error(f"Error Stopping yt_dlp: {e}")
            socketio.emit("new_toast_msg", {"title": "Download Reset Error", "message": str(e)})
        else:
            self.general_logger.warning("Reset Complete")
            socketio.emit("new_toast_msg", {"title": "Downloads Reset", "message": "Download queue cleared"})
        finally:
            self._emit_ytdlp_update()

    # --- FD management ---

    def _get_fd_limit(self):
        try:
            soft_limit, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
            if soft_limit and soft_limit != resource.RLIM_INFINITY:
                return int(soft_limit)
        except Exception:
            pass
        return None

    def _recommended_workers_from_fd_limit(self, configured_workers, estimated_fd_per_worker):
        try:
            configured = max(1, int(configured_workers))
        except (TypeError, ValueError):
            configured = 1

        if self._fd_limit is None:
            return configured

        reserved_fds = max(128, int(self._fd_limit * 0.35))
        available_budget = max(64, self._fd_limit - reserved_fds)
        recommended = max(1, available_budget // estimated_fd_per_worker)
        return min(configured, recommended)

    def _apply_fd_safety_limits(self):
        original_download_workers = self.config.thread_limit
        original_scan_workers = self.config.lidarr_scan_thread_limit

        self.config.thread_limit = self._recommended_workers_from_fd_limit(
            original_download_workers, estimated_fd_per_worker=160
        )
        self.config.lidarr_scan_thread_limit = self._recommended_workers_from_fd_limit(
            original_scan_workers, estimated_fd_per_worker=24
        )

        if self.config.thread_limit < max(1, int(original_download_workers)):
            self.general_logger.warning(
                "Clamped thread_limit from %s to %s based on open-file limit %s",
                original_download_workers,
                self.config.thread_limit,
                self._fd_limit,
            )
        if self.config.lidarr_scan_thread_limit < max(1, int(original_scan_workers)):
            self.general_logger.warning(
                "Clamped lidarr_scan_thread_limit from %s to %s based on open-file limit %s",
                original_scan_workers,
                self.config.lidarr_scan_thread_limit,
                self._fd_limit,
            )

    def _open_fd_count(self):
        try:
            return len(os.listdir("/proc/self/fd"))
        except Exception:
            return None

    def _is_fd_pressure_high(self):
        fd_limit = getattr(self, "_fd_limit", None)
        if not fd_limit:
            return False
        open_count = self._open_fd_count()
        if open_count is None:
            return False
        ratio = open_count / fd_limit
        if ratio >= getattr(self, "_fd_pressure_ratio", 0.85):
            now = time.monotonic()
            last_log = getattr(self, "_last_fd_pressure_log", 0.0)
            if now - last_log >= 5:
                self._last_fd_pressure_log = now
                self.general_logger.warning(
                    "FD usage high (%s/%s, %.0f%%)",
                    open_count,
                    fd_limit,
                    ratio * 100,
                )
            return True
        return False

    def _signal_fd_exhaustion(self):
        lock = getattr(self, "_fd_backoff_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._fd_backoff_lock = lock
        if not lock.acquire(blocking=False):
            return
        try:
            if self.fd_exhaustion_event.is_set():
                return
            self.general_logger.warning("FD exhaustion detected — backing off for 10 seconds")
            self.fd_exhaustion_event.set()
            time.sleep(10)
            self.fd_exhaustion_event.clear()
            self.general_logger.warning("FD back-off cleared — resuming")
        finally:
            lock.release()

    def _interruptible_sleep(self, seconds):
        """Sleep for up to `seconds`, returning early if the lidarr stop event fires."""
        self.lidarr_stop_event.wait(timeout=seconds)

    def _wait_if_fd_pressure(self):
        """Block while FD back-off is active, then proceed."""
        if self._is_fd_pressure_high():
            threading.Thread(target=self._signal_fd_exhaustion, daemon=True).start()
        if self.fd_exhaustion_event.is_set():
            self.general_logger.warning("Waiting for FD pressure to clear (up to 30s)")
            deadline = time.monotonic() + 30
            while self.fd_exhaustion_event.is_set() and time.monotonic() < deadline:
                time.sleep(0.5)
            self.general_logger.warning("Resuming after FD wait")

    # --- Link search helpers ---

    def _set_track_link(self, track, link, title, matched_via=None):
        track["link"] = link
        track["title_of_link"] = title
        if matched_via:
            track["_matched_via"] = matched_via

    def _set_track_link_from_video_id(self, track, video_id, title, matched_via=None):
        self._set_track_link(track, f"https://www.youtube.com/watch?v={video_id}", title, matched_via)

    def _record_link_results(self, req_album):
        """Persist final search outcomes after all matching stages have run."""
        session_id = getattr(self, "current_session_id", None)
        store = getattr(self, "store", None)
        if not session_id or store is None:
            return
        for track in req_album.get("missing_tracks", []):
            if track.get("_track_result_id"):
                continue
            outcome = "matched" if track.get("link") else "no_match"
            suspicion, _ = library_suspicion.score_track({
                "duration_delta_s": None,
                "expected_known": bool(track.get("duration_ms")),
                "is_grab": False,
                "official_art_track": False,
                "provenance_bad_version": False,
                "lidarr_matched": outcome == "matched",
            })
            accepted_scores = [entry.get("score") for entry in track.get("_match_trace", []) if entry.get("rejected_by") == "accepted" and entry.get("score") is not None]
            if accepted_scores:
                suspicion = max(suspicion, int(round(100 - max(accepted_scores))))
            result_id = store.record_track_result(
                session_id=session_id,
                artist=track.get("artist", req_album.get("artist")),
                album=req_album.get("album_name"),
                track_title=track.get("track_title"),
                track_number=track.get("track_number"),
                track_id=track.get("track_id"),
                duration_ms=track.get("duration_ms", 0),
                outcome=outcome,
                link=track.get("link") or None,
                title_of_link=track.get("title_of_link") or None,
                matched_via=track.get("_matched_via"),
                suspicion=track.get("suspicion", suspicion),
            )
            if outcome == "no_match":
                store.record_evaluations(result_id, track.get("_match_trace", []))
            track["_track_result_id"] = result_id
            track.pop("_match_trace", None)

    def _apply_saved_overrides(self, req_album):
        store = getattr(self, "store", None)
        if store is None:
            return
        for track in req_album.get("missing_tracks", []):
            if track.get("link"):
                continue
            override = store.get_override(track.get("track_id"))
            if override:
                self._set_track_link(track, override["forced_url"], "Manual override", "yt")

    def _count_found_links(self, req_album):
        return sum(1 for x in req_album["missing_tracks"] if x["link"] != "")

    def _apply_album_track_links(self, req_album, album_details):
        for track in album_details["tracks"]:
            if self.ytdlp_stop_event.is_set():
                return True
            for missing_track in req_album["missing_tracks"]:
                missing_track_title = _general.string_cleaner(missing_track["track_title"])
                song_title = _general.string_cleaner(track["title"])
                if fuzz.ratio(song_title, missing_track_title) > 90:
                    candidate_seconds = track.get("durationSeconds") or 0
                    if not _matcher._duration_ok(missing_track["duration_ms"], candidate_seconds, self.config.duration_tolerance_seconds):
                        continue
                    self._set_track_link_from_video_id(missing_track, track["videoId"], track["title"])
                    break
        return False

    def _close_ytmusic_client(self, ytmusic):
        if ytmusic is None:
            return
        try:
            session = getattr(ytmusic, "_session", None)
            if session is not None:
                session.close()
            else:
                self.general_logger.warning("YTMusic client has no _session attribute — session may not have been closed")
        except Exception as e:
            self.general_logger.warning(f"Error closing YTMusic client: {e}")

    def _link_finder(self, req_album):
        ytmusic = None
        semaphore_acquired = False
        try:
            self.general_logger.warning(f'Searching for: {req_album["artist"]} - {req_album["album_name"]}')
            artist = req_album["artist"]
            album_name = req_album["album_name"]
            number_tracks_in_album = req_album["track_count"]
            number_of_missing_tracks = req_album["missing_count"]
            query_text = f"{artist} - {album_name}"
            cleaned_artist = _general.string_cleaner(artist).lower()
            cleaned_album = _general.string_cleaner(album_name).lower()

            self._wait_if_fd_pressure()
            while not self.ytdlp_stop_event.is_set():
                if self._ytmusic_semaphore.acquire(timeout=0.5):
                    semaphore_acquired = True
                    break
            if not semaphore_acquired:
                return
            ytmusic = YTMusic()
            self._apply_saved_overrides(req_album)

            if number_tracks_in_album == number_of_missing_tracks:
                self._get_album_links(req_album, artist, album_name, cleaned_artist, cleaned_album, query_text, ytmusic)

            number_of_links = self._count_found_links(req_album)
            if number_of_links == len(req_album["missing_tracks"]):
                req_album["status"] = "All Tracks Found"
                self._emit_ytdlp_update()
                self.general_logger.warning(f'Links found for all tracks of: {req_album["artist"]} - {req_album["album_name"]}')
            else:
                req_album["status"] = "Searching"
                self._emit_ytdlp_update()
                continue_with_secondary_search = self._get_song_links(req_album, artist, cleaned_artist, ytmusic)

                number_of_links = self._count_found_links(req_album)
                if number_of_links == len(req_album["missing_tracks"]):
                    req_album["status"] = "All Tracks Found"
                    self._emit_ytdlp_update()
                    self.general_logger.warning(f'Links found for all Tracks of: {req_album["artist"]} - {req_album["album_name"]}')
                elif not continue_with_secondary_search:
                    self.general_logger.warning(f'Skipping secondary search due to resource exhaustion: {req_album["artist"]} - {req_album["album_name"]}')
                else:
                    self.general_logger.warning(f'Not all tracks found, searching again: {req_album["artist"]} - {req_album["album_name"]}')
                    self._get_song_links_secondary(req_album, artist, cleaned_artist, ytmusic)

        except Exception as e:
            self.general_logger.error(f"Error in Link Finder: {e}")
            if _general.is_resource_exhaustion_error(e):
                threading.Thread(target=self._signal_fd_exhaustion, daemon=True).start()
        finally:
            self._record_link_results(req_album)
            self._close_ytmusic_client(ytmusic)
            if semaphore_acquired:
                self._ytmusic_semaphore.release()

    def _get_album_links(self, req_album, artist, album_name, cleaned_artist, cleaned_album, query_text, ytmusic):
        try:
            self.general_logger.warning(f'Searching for Whole Album: {req_album["artist"]} - {req_album["album_name"]}')
            search_results = ytmusic.search(query=query_text, filter="albums", limit=10)
            self.general_logger.warning(f'Album search returned {len(search_results)} result(s) for: {query_text}')
            album_match = _matcher.album_matcher(self.config.minimum_match_ratio, artist, album_name, cleaned_artist, cleaned_album, search_results)

            if album_match:
                self.general_logger.warning(f'Album match found: {album_match.get("title", album_match.get("browseId"))}')
                req_album["status"] = "Album Found"
                album_details = ytmusic.get_album(album_match["browseId"])
                if self._apply_album_track_links(req_album, album_details):
                    return
            elif self.config.fallback_to_top_result:
                if search_results:
                    self.general_logger.warning(f'No match — falling back to top result: {search_results[0].get("title", search_results[0].get("browseId"))}')
                    req_album["status"] = "Album Found"
                    album_details = ytmusic.get_album(search_results[0]["browseId"])
                    if self._apply_album_track_links(req_album, album_details):
                        return
                else:
                    self.general_logger.warning(f'No search results for album: {req_album["artist"]} - {req_album["album_name"]}')
            else:
                self.general_logger.warning(f'No matching album for: {req_album["artist"]} - {req_album["album_name"]}')

        except Exception as e:
            self.general_logger.error(f"Error in Album Search: {e}")
            raise

    def _get_song_links(self, req_album, artist, cleaned_artist, ytmusic):
        """Primary song-by-song search. Returns True unless FD exhaustion occurred."""
        try:
            self.general_logger.warning(f'Searching for individual Tracks: {req_album["artist"]} - {req_album["album_name"]}')
            for missing_track in req_album["missing_tracks"]:
                if self.ytdlp_stop_event.is_set():
                    return True
                if missing_track["link"] == "":
                    override = self.store.get_override(missing_track.get("track_id")) if getattr(self, "store", None) else None
                    if override:
                        self._set_track_link(missing_track, override["forced_url"], "Manual override", "yt")
                        continue
                    song_title = missing_track["track_title"]
                    cleaned_song_title = _general.string_cleaner(song_title).lower()
                    query_text = f'{missing_track["artist"]} - {song_title}'
                    search_results = ytmusic.search(query=query_text, filter="songs", limit=5)
                    trace = missing_track.setdefault("_match_trace", [])
                    song_match = _matcher.song_matcher(self.config.minimum_match_ratio, artist, cleaned_artist, song_title, cleaned_song_title, search_results,
                                                       expected_duration_ms=missing_track["duration_ms"], duration_tolerance_seconds=self.config.duration_tolerance_seconds, trace=trace)
                    if song_match:
                        self.general_logger.warning(f'Track matched: "{song_title}" -> "{song_match["title"]}"')
                        self._set_track_link_from_video_id(missing_track, song_match["videoId"], song_match["title"], "ytmusic")
                    elif self.config.fallback_to_top_result and search_results:
                        self.general_logger.warning(f'No match — falling back to top result for: "{song_title}" -> "{search_results[0]["title"]}"')
                        self._set_track_link_from_video_id(missing_track, search_results[0]["videoId"], search_results[0]["title"])
                    else:
                        self.general_logger.warning(f'No match found for track: "{song_title}"')

        except Exception as e:
            self.general_logger.error(f"Error in Song Search: {e}")
            raise
        return True

    def _get_song_links_secondary(self, req_album, artist, cleaned_artist, ytmusic):
        try:
            self.general_logger.warning(f'Secondary search for: {req_album["artist"]} - {req_album["album_name"]} (mode: {self.config.secondary_search})')
            for missing_track in req_album["missing_tracks"]:
                if self.ytdlp_stop_event.is_set():
                    return
                if missing_track["link"] == "":
                    song_title = missing_track["track_title"]
                    cleaned_song_title = _general.string_cleaner(song_title).lower()
                    query_text = f'{missing_track["artist"]} - {song_title}'
                    search_results = ytmusic.search(query=query_text, filter="songs", limit=20)
                    trace = missing_track.setdefault("_match_trace", [])
                    song_match = _matcher.song_matcher(self.config.minimum_match_ratio, artist, cleaned_artist, song_title, cleaned_song_title, search_results,
                                                       expected_duration_ms=missing_track["duration_ms"], duration_tolerance_seconds=self.config.duration_tolerance_seconds, trace=trace)
                    if song_match:
                        self.general_logger.warning(f'Secondary YTMusic match: "{song_title}" -> "{song_match["title"]}"')
                        self._set_track_link_from_video_id(missing_track, song_match["videoId"], song_match["title"], "ytmusic_secondary")
                    elif self.config.fallback_to_top_result and search_results:
                        self.general_logger.warning(f'Secondary fallback to top result: "{song_title}" -> "{search_results[0]["title"]}"')
                        self._set_track_link_from_video_id(missing_track, search_results[0]["videoId"], search_results[0]["title"])
                    else:
                        yt_results = self._yt_search(query_text)
                        song_match = _matcher.song_matcher_yt(self.config.minimum_match_ratio, artist, query_text, yt_results,
                                                              expected_duration_ms=missing_track["duration_ms"], duration_tolerance_seconds=self.config.duration_tolerance_seconds, trace=trace)
                        if song_match:
                            if self.config.secondary_search == "YTS":
                                self.general_logger.warning(f'YTS match: "{song_title}" -> "{song_match["title"]}"')
                                self._set_track_link(missing_track, song_match["link"], song_match["title"], "yt")
                            elif self.config.secondary_search == "YTDLP":
                                self.general_logger.warning(f'YTDLP match: "{song_title}" -> "{song_match["title"]}"')
                                self._set_track_link(missing_track, song_match["webpage_url"], song_match["title"], "yt")
                        else:
                            self.general_logger.warning(f'No match found in secondary search for: "{song_title}"')

            found = self._count_found_links(req_album)
            self.general_logger.warning(f'Found {found} of the missing {len(req_album["missing_tracks"])} tracks: {req_album["artist"]} - {req_album["album_name"]}')

        except Exception as e:
            self.general_logger.error(f"Error in Secondary Search: {e}")
            raise

    def _yt_search(self, query_text):
        try:
            if self.config.secondary_search == "YTDLP":
                ydl_opts = {"default_search": "ytsearch10", "quiet": True}
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    search = ydl.extract_info(query_text, download=False)
                    search_results = search.get("entries", [])
            elif self.config.secondary_search == "YTS":
                videos_search = youtubesearchpython.VideosSearch(query_text, limit=10)
                search_results = videos_search.result()["result"]
            else:
                return []

            if self.ytdlp_stop_event.is_set():
                return []
            return search_results

        except Exception as e:
            self.general_logger.error(f"Error in YouTube Search: {e}")
            return []


app = Flask(__name__)
app.secret_key = "secret_key"
socketio = SocketIO(app)
data_handler = DataHandler()


@app.route("/")
def home():
    return render_template("base.html")


def _page_args():
    try:
        limit = int(request.args.get("limit", 100))
        offset = int(request.args.get("offset", 0))
    except (TypeError, ValueError):
        return None
    if not 1 <= limit <= 200 or offset < 0:
        return None
    return limit, offset


def _filtered_lidarr_indices(query):
    """Positional indices of albums that still have missing/pending tracks, filtered by query."""
    indices = []
    for index, item in enumerate(data_handler.lidarr_items):
        if item.get("missing_count", 0) <= 0 and item.get("scan_ready", False):
            continue
        label = f"{item.get('artist', '')} {item.get('album_name', '')}".lower()
        if query and query not in label:
            continue
        indices.append(index)
    return indices


@app.route("/api/lidarr")
def api_lidarr():
    query = request.args.get("q", "").strip().lower()
    indices = _filtered_lidarr_indices(query)
    # ids_only powers "select all" across the virtualized list without loading rows.
    if request.args.get("ids_only"):
        return jsonify({"ids": indices, "total": len(indices)})
    page = _page_args()
    if page is None:
        return jsonify({"error": "limit must be 1-200 and offset must be non-negative"}), 400
    limit, offset = page
    items = []
    for index in indices[offset:offset + limit]:
        item = data_handler.lidarr_items[index]
        slim = {key: value for key, value in item.items() if key != "missing_tracks"}
        slim["index"] = index
        items.append(slim)
    return jsonify({"items": items, "total": len(indices)})


@app.route("/api/sessions")
def api_sessions():
    page = _page_args()
    if page is None:
        return jsonify({"error": "limit must be 1-200 and offset must be non-negative"}), 400
    limit, offset = page
    return jsonify({"items": data_handler.store.list_sessions(limit, offset), "total": data_handler.store.count_sessions()})


@app.route("/api/queue/status")
def api_queue_status():
    return jsonify(data_handler.queue_status())


@app.route("/api/session/resume", methods=["POST"])
def api_session_resume():
    if data_handler.ytdlp_in_progress_flag:
        return jsonify({"error": "A download session is already running"}), 409
    if not data_handler.resume_ytdlp():
        return jsonify({"error": "No resumable download session exists"}), 404
    return jsonify(data_handler.queue_status()), 202


@app.route("/api/session/stop", methods=["POST"])
def api_session_stop():
    if not data_handler.ytdlp_in_progress_flag:
        return jsonify({"error": "No download session is running"}), 409
    data_handler.stop_ytdlp()
    return jsonify({"message": "Stop requested"}), 202


@app.route("/api/sessions/<int:session_id>/tracks")
def api_session_tracks(session_id):
    page = _page_args()
    if page is None:
        return jsonify({"error": "limit must be 1-200 and offset must be non-negative"}), 400
    limit, offset = page
    return jsonify({"items": data_handler.store.get_session_tracks(session_id, limit, offset), "total": data_handler.store.count_session_tracks(session_id)})


@app.route("/api/no_match")
def api_no_match():
    page = _page_args()
    if page is None:
        return jsonify({"error": "limit must be 1-200 and offset must be non-negative"}), 400
    limit, offset = page
    order_by_suspicion = request.args.get("order", "suspicion") == "suspicion"
    return jsonify({"items": data_handler.store.list_no_match(order_by_suspicion, limit, offset), "total": data_handler.store.count_no_match()})


@app.route("/api/attention")
def api_attention():
    page = _page_args()
    if page is None:
        return jsonify({"error": "limit must be 1-200 and offset must be non-negative"}), 400
    limit, offset = page
    return jsonify({"items": data_handler.store.list_attention(limit, offset), "total": data_handler.store.count_attention()})


@app.route("/api/track/<int:track_result_id>/evaluations")
def api_track_evaluations(track_result_id):
    return jsonify({"items": data_handler.store.get_evaluations(track_result_id)})


@app.route("/api/overrides")
def api_overrides():
    page = _page_args()
    if page is None:
        return jsonify({"error": "limit must be 1-200 and offset must be non-negative"}), 400
    limit, offset = page
    return jsonify({"items": data_handler.store.list_overrides(limit, offset)})


@app.route("/api/override", methods=["POST"])
def api_set_override():
    data = request.get_json(silent=True) or {}
    track_id = data.get("track_id")
    forced_url = data.get("forced_url")
    if not isinstance(track_id, int) or not isinstance(forced_url, str) or not forced_url.strip():
        return jsonify({"error": "track_id and forced_url are required"}), 400
    data_handler.store.set_override(track_id, forced_url.strip(), data.get("note"))
    return jsonify({"message": "Override saved"}), 201


@app.route("/api/override/<int:track_id>", methods=["DELETE"])
def api_delete_override(track_id):
    data_handler.store.delete_override(track_id)
    return jsonify({"message": "Override deleted"})


@app.route("/cookies_status")
def cookies_status():
    exists = data_handler.config.cookies_path is not None and os.path.exists(data_handler.config.cookies_path)
    return jsonify({"exists": exists})


@app.route("/upload_cookies", methods=["POST"])
def upload_cookies():
    if "cookies_file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files["cookies_file"]
    if not f.filename:
        return jsonify({"error": "No file selected"}), 400
    cookies_path = os.path.abspath(os.path.join(data_handler.config.CONFIG_FOLDER, "cookies.txt"))
    f.save(cookies_path)
    data_handler.config.cookies_path = cookies_path
    return jsonify({"message": "Cookies file uploaded successfully"})


@app.route("/delete_cookies", methods=["DELETE"])
def delete_cookies():
    cookies_path = os.path.join(data_handler.config.CONFIG_FOLDER, "cookies.txt")
    if os.path.exists(cookies_path):
        os.remove(cookies_path)
    data_handler.config.cookies_path = None
    return jsonify({"message": "Cookies file deleted"})


@socketio.on("lidarr_get_wanted")
def lidarr():
    thread = threading.Thread(target=data_handler.get_wanted_albums_from_lidarr, name="Lidarr_Thread")
    thread.daemon = True
    thread.start()


@socketio.on("stop_lidarr")
def stop_lidarr():
    data_handler.lidarr_stop_event.set()


@socketio.on("reset_lidarr")
def reset_lidarr():
    data_handler.reset_lidarr()


@socketio.on("stop_ytdlp")
def stop_ytdlp():
    data_handler.stop_ytdlp()


@socketio.on("reset_ytdlp")
def reset_ytdlp():
    data_handler.reset_ytdlp()


@socketio.on("add_to_download_list")
def add_to_download_list(data):
    data_handler.add_items_to_download(data)


@socketio.on("connect")
def connection():
    data_handler.connect()


@socketio.on("disconnect")
def disconnect():
    data_handler.disconnect()


@socketio.on("load_settings")
def load_settings():
    data_handler.load_settings()


@socketio.on("update_settings")
def update_settings(data):
    data_handler.update_settings(data)


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000)
