"""Lidarr scanning: which albums are wanted, and which of their tracks are missing.

A scan pages through Lidarr's wanted/missing albums while a thread pool fetches each
album's track list. Scan progress and per-album scan state are kept consistent and
checkpointed to a JSON cache so a restart does not have to rescan ~100k albums, and a
truncated or failed scan is reported as incomplete rather than as a finished list.
"""

import concurrent.futures
import json
import logging
import os
import threading
import time
from datetime import datetime

import _general


class LidarrScanner:
    _ARTIST_RETRY_WAIT = 10  # seconds per attempt; override in tests
    CACHE_SCHEMA_VERSION = 2
    CACHE_CHECKPOINT_PAGES = 5
    MAX_PENDING_MULTIPLIER = 4
    STATUSES = {"idle", "busy", "complete", "stopped", "error"}
    ALBUM_SCAN_STATES = {"pending", "scanning", "complete", "error"}

    def __init__(self, config, lidarr_client, fd_governor, emit, logger=None,
                 download_stop_event=None, on_album_scanned=None):
        """on_album_scanned(album) runs after each album's tracks are scanned successfully.

        download_stop_event also releases scans waiting on an album that another worker
        (such as the download queue prioritising it) is already scanning.
        """
        self.config = config
        self.lidarr_client = lidarr_client
        self.fd = fd_governor
        self.emit = emit
        self.logger = logger or logging.getLogger(__name__)
        self.download_stop_event = download_stop_event or threading.Event()
        self.on_album_scanned = on_album_scanned

        self.items = []
        self.futures = []
        self.status = "idle"
        self.stop_event = threading.Event()
        self.scan_guard = threading.Lock()
        self.progress = self._default_progress()
        self.started_at = None
        self.completed_at = None
        self.scan_error = None

    def filtered_indices(self, query, checked_only=False):
        """Positional indices of albums that still have missing/pending tracks, filtered by query."""
        indices = []
        for index, item in enumerate(self.items):
            if item.get("missing_count", 0) <= 0 and item.get("scan_ready", False):
                continue
            if checked_only and not item.get("checked", False):
                continue
            label = f"{item.get('artist', '')} {item.get('album_name', '')}".lower()
            if query and query not in label:
                continue
            indices.append(index)
        return indices

    # --- Scan state ---

    def _default_progress(self):
        return {
            "phase": "Idle",
            "pages_scanned": 0,
            "albums_discovered": 0,
            "albums_processed": 0,
            "albums_total": 0,
            "percent": 0,
        }

    def _set_progress(self, **kwargs):
        self.progress.update(kwargs)

    def _set_state(self, status, *, phase=None, error=None, **progress):
        """Update global scan state and its persisted metadata as one transition."""
        if status not in self.STATUSES:
            raise ValueError(f"Invalid Lidarr scan status: {status}")

        now = datetime.now().astimezone().isoformat()
        previous_status = self.status
        self.status = status
        if status == "busy" and previous_status != "busy":
            self.started_at = now
            self.completed_at = None
            self.scan_error = None
        elif status in {"complete", "stopped", "error"}:
            self.completed_at = now
            self.scan_error = str(error) if error else None
        elif status == "idle":
            self.started_at = None
            self.completed_at = None
            self.scan_error = None

        if phase is not None:
            progress["phase"] = phase
        self._set_progress(**progress)

    def _set_album_scan_state(self, req_album, state, error=None):
        """Maintain the explicit album state and legacy flags as one invariant."""
        if state not in self.ALBUM_SCAN_STATES:
            raise ValueError(f"Invalid album scan state: {state}")
        req_album["scan_state"] = state
        req_album["scan_in_progress"] = state == "scanning"
        req_album["scan_ready"] = state == "complete"
        req_album["scan_error"] = str(error) if error else None

    def _drain_futures(self, future_map, *, wait_for_one=False):
        """Consume completed futures and release their result/album references."""
        if not future_map:
            return 0, 0

        futures = tuple(future_map)
        if wait_for_one:
            done, _ = concurrent.futures.wait(futures, return_when=concurrent.futures.FIRST_COMPLETED)
        else:
            done = {future for future in futures if future.done()}

        failed = 0
        for future in done:
            req_album = future_map.pop(future)
            try:
                future.result()
            except Exception as exc:
                self._set_album_scan_state(req_album, "error", exc)
                self.logger.error(f'Error Getting Missing Tracks for {req_album["artist"]} - {req_album["album_name"]}: {exc}')
            if req_album.get("scan_state") == "error":
                failed += 1

        self.futures = list(future_map)
        return len(done), failed

    def emit_update(self):
        # Only send albums that need attention: unscanned (still processing) or have missing tracks.
        # Fully-downloaded albums (missing_count=0, scan_ready=True) are omitted to keep the
        # payload small — serialising 90k+ items on every connect blocks the gevent event loop.
        # Each item carries its original index so the client can reference self.items correctly.
        slim_items = []
        for i, item in enumerate(self.items):
            if item.get("missing_count", 0) > 0 or not item.get("scan_ready", False):
                slim = {k: v for k, v in item.items() if k != "missing_tracks"}
                slim["index"] = i
                slim_items.append(slim)
        self.emit("lidarr_update", {
            "status": self.status,
            "data": slim_items,
            "scan_progress": self.progress,
            "total_count": len(self.items),
        })

    def _emit_progress(self):
        """Emit only scan progress stats — no data array. Use during tight loops to avoid O(n²) socket traffic."""
        self.emit("lidarr_update", {"status": self.status, "data": None, "scan_progress": self.progress})

    # --- Cache ---

    def _cache_path(self):
        return os.path.join(self.config.CONFIG_FOLDER, "lidarr_cache.json")

    def save_cache(self, *, status=None, error=None):
        try:
            cache_path = self._cache_path()
            tmp_path = cache_path + ".tmp"
            persisted_status = status or self.status
            if persisted_status == "busy":
                # A checkpoint is incomplete if the process exits before the final write.
                persisted_status = "error"
                if error is None:
                    error = "Lidarr scan was interrupted before completion"
            scan_state = {
                "status": persisted_status,
                "complete": persisted_status == "complete",
                "last_successful_page": self.progress.get("pages_scanned", 0),
                "scan_started_at": self.started_at,
                "scan_completed_at": self.completed_at,
                "error": str(error) if error else self.scan_error,
            }
            with open(tmp_path, "w") as f:
                json.dump(
                    {
                        "schema_version": self.CACHE_SCHEMA_VERSION,
                        "lidarr_items": self.items,
                        "lidarr_scan_progress": self.progress,
                        "lidarr_scan_state": scan_state,
                    },
                    f,
                )
            os.replace(tmp_path, cache_path)
            self.logger.warning(f"Saved {len(self.items)} albums to Lidarr cache")
        except Exception as e:
            self.logger.error(f"Error saving Lidarr cache: {e}")

    def load_cache(self):
        try:
            cache_path = self._cache_path()
            if os.path.exists(cache_path):
                with open(cache_path, "r") as f:
                    cache = json.load(f)
                self.items = cache.get("lidarr_items", [])
                cached_progress = cache.get("lidarr_scan_progress", {})
                cached_state = cache.get("lidarr_scan_state")
                if isinstance(cached_state, dict):
                    cached_status = cached_state.get("status", "error")
                    if cached_status not in self.STATUSES or cached_status == "busy":
                        cached_status = "error"
                        cached_progress["phase"] = "Interrupted cached scan"
                    self.status = cached_status
                    self.started_at = cached_state.get("scan_started_at")
                    self.completed_at = cached_state.get("scan_completed_at")
                    self.scan_error = cached_state.get("error")
                else:
                    # Version 1 caches predate explicit state. Preserve their historical
                    # complete-cache behavior while all newly written caches carry state.
                    self.status = "complete"
                    cached_progress["phase"] = "Complete (cached)"
                self.progress.update(cached_progress)
                for item in self.items:
                    state = item.get("scan_state")
                    if state == "scanning" or state not in self.ALBUM_SCAN_STATES:
                        state = "complete" if item.get("scan_ready", False) else "pending"
                    self._set_album_scan_state(item, state, item.get("scan_error"))
                self.logger.warning(f"Loaded {len(self.items)} albums from Lidarr cache")
        except Exception as e:
            self.logger.error(f"Error loading Lidarr cache: {e}")

    def clear_cache(self):
        removed = 0
        cache_path = self._cache_path()
        for path in (cache_path, cache_path + ".tmp"):
            if os.path.exists(path):
                os.remove(path)
                removed += 1
        self.logger.warning(f"Cleared {removed} Lidarr cache file(s)")
        return removed

    # --- Scanning ---

    def fetch_wanted_albums(self):
        try:
            self.logger.warning("Accessing Lidarr API")
            self._set_state("busy")
            self.stop_event.clear()
            self.items = []
            self._set_progress(
                phase="Fetching wanted albums",
                pages_scanned=0,
                albums_discovered=0,
                albums_processed=0,
                albums_total=0,
                percent=0,
            )
            self.emit_update()

            self.logger.warning("Pre-fetching artists from Lidarr")
            self._set_progress(phase="Fetching artists")
            self.emit_update()
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
                        self.logger.warning(f"Artist fetch attempt {attempt + 1}/{artist_max_retries} failed: {e} — retrying in {wait}s")
                        self.emit("new_toast_msg", {"title": f"Artist fetch retry {attempt + 1}/{artist_max_retries}", "message": str(e)})
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
            self.logger.warning(f"Fetched {len(artist_lookup)} artists")

            page = 1
            page_size = 1000
            scan_worker_count = max(1, int(self.config.lidarr_scan_thread_limit))
            self.logger.warning(f"Fetching wanted albums (pageSize={page_size}) and missing tracks with {scan_worker_count} worker(s)")

            future_map = {}
            total_albums = 0
            albums_processed = 0
            album_scan_errors = 0
            max_pending = scan_worker_count * self.MAX_PENDING_MULTIPLIER
            wanted_fetch_incomplete = False
            last_wanted_error = None

            with concurrent.futures.ThreadPoolExecutor(max_workers=scan_worker_count) as executor:
                while True:
                    if self.stop_event.is_set():
                        break

                    # Retry a failed page before giving up: Lidarr returns 500s when it is
                    # busy (e.g. a concurrent library rescan), and abandoning the loop there
                    # silently truncates the wanted list.
                    wanted_missing_albums = None
                    for attempt in range(3):
                        response = self.lidarr_client.get_wanted_albums(page, page_size)
                        try:
                            if response.status_code == 200:
                                wanted_missing_albums = response.json()
                                break
                            self.logger.error(f"Lidarr Wanted API Error Code: {response.status_code} (page {page}, attempt {attempt + 1}/3)")
                            self.logger.error(f"Lidarr Wanted API Error Text: {response.text}")
                            last_wanted_error = f"{response.status_code}: {response.text[:200]}"
                        finally:
                            response.close()
                        if attempt < 2:
                            self.stop_event.wait(2 ** attempt)

                    if wanted_missing_albums is None:
                        # Do not present a truncated list as a finished scan.
                        wanted_fetch_incomplete = True
                        self.logger.error(
                            f"Aborting wanted-album fetch at page {page}: Lidarr did not return a page after 3 attempts. "
                            f"The album list is INCOMPLETE ({len(self.items)} albums fetched so far)."
                        )
                        self.emit("new_toast_msg", {
                            "title": "Lidarr fetch incomplete",
                            "message": f"Lidarr errored on page {page}; only {len(self.items)} albums were fetched. Refresh again to get the rest.",
                        })
                        break

                    if not wanted_missing_albums["records"]:
                        break
                    albums_on_page = wanted_missing_albums["records"]
                    del wanted_missing_albums

                    for album in albums_on_page:
                        if self.stop_event.is_set():
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
                            "album_secondary_types": list(album.get("secondaryTypes") or []),
                            "artist_genres": ", ".join(artist.get("genres") or []),
                            "track_count": 0,
                            "missing_count": 0,
                            "missing_tracks": [],
                            "checked": True,
                            "scan_ready": False,
                            "scan_in_progress": False,
                            "scan_state": "pending",
                            "scan_error": None,
                            "status": "",
                        }
                        self.items.append(new_item)
                        while len(future_map) >= max_pending and not self.stop_event.is_set():
                            processed, failed = self._drain_futures(future_map, wait_for_one=True)
                            albums_processed += processed
                            album_scan_errors += failed
                        if self.stop_event.is_set():
                            break
                        future = executor.submit(self.scan_album_tracks, new_item)
                        future_map[future] = new_item
                        self.futures = list(future_map)
                    del albums_on_page

                    # Drain completed track futures while next page is being fetched
                    processed, failed = self._drain_futures(future_map)
                    albums_processed += processed
                    album_scan_errors += failed
                    discovered = len(self.items)
                    self._set_progress(
                        phase="Fetching albums & tracks",
                        pages_scanned=page,
                        albums_discovered=discovered,
                        albums_processed=albums_processed,
                        albums_total=discovered,
                        percent=int(albums_processed / discovered * 100) if discovered else 0,
                    )
                    self.emit_update()
                    if page % self.CACHE_CHECKPOINT_PAGES == 0:
                        self.save_cache()
                    page += 1

                self.items.sort(key=lambda x: (x["artist"], x["album_name"]))
                total_albums = len(self.items)
                self._set_progress(
                    phase="Fetching missing tracks",
                    albums_total=total_albums,
                    albums_processed=albums_processed,
                    percent=int(albums_processed / total_albums * 100) if total_albums else 100,
                )
                self.emit_update()

                while future_map:
                    if self.stop_event.is_set():
                        break
                    processed, failed = self._drain_futures(future_map, wait_for_one=True)
                    albums_processed += processed
                    album_scan_errors += failed
                    percent = int((albums_processed / total_albums) * 100) if total_albums else 100
                    self._set_progress(phase="Fetching missing tracks", albums_processed=albums_processed, percent=percent)
                    self._emit_progress()
                self.emit_update()

            self.futures = []
            if self.stop_event.is_set():
                self._set_state("stopped", phase="Stopped")
            elif wanted_fetch_incomplete:
                # The list is truncated — say so instead of reporting a finished scan.
                self._set_state(
                    "error",
                    phase=f"Incomplete — Lidarr error on page {page} ({total_albums} albums fetched)",
                    error=last_wanted_error,
                    albums_processed=albums_processed, albums_total=total_albums, percent=100,
                )
                self.logger.error(f"Wanted-album fetch incomplete; last Lidarr error: {last_wanted_error}")
                self.save_cache()
            elif album_scan_errors:
                message = f"{album_scan_errors} album track scan(s) failed"
                self._set_state(
                    "error",
                    phase=f"Incomplete — {message}",
                    error=message,
                    albums_processed=albums_processed,
                    albums_total=total_albums,
                    percent=100,
                )
                self.save_cache()
            else:
                self._set_state("complete", phase="Complete", albums_processed=total_albums, albums_total=total_albums, percent=100)
                self.save_cache()

        except Exception as e:
            self.logger.error(f"Error Getting Missing Albums: {e}")
            self._set_state("error", phase="Error", error=e)
            self.emit("new_toast_msg", {"title": "Error Getting Missing Albums", "message": str(e)})

        finally:
            self.emit_update()

    def scan_album_tracks(self, req_album):
        while True:
            with self.scan_guard:
                if req_album.get("scan_ready", False):
                    return True
                if not req_album.get("scan_in_progress", False):
                    self._set_album_scan_state(req_album, "scanning")
                    break
            if self.download_stop_event.is_set() or self.stop_event.is_set():
                self._set_album_scan_state(req_album, "pending")
                return False
            time.sleep(0.1)

        self.logger.warning(f'Reading Missing Track list of {req_album["artist"]} - {req_album["album_name"]} from Lidarr API')
        last_error = None

        for attempt in range(3):
            req_album["scan_attempts"] = attempt + 1
            try:
                req_album["missing_tracks"] = []
                req_album["track_count"] = 0
                req_album["missing_count"] = 0
                self.fd.wait_if_pressure()
                if self.stop_event.is_set():
                    self._set_album_scan_state(req_album, "pending")
                    return False

                response = self.lidarr_client.get_tracks_for_album(req_album["album_id"])
                try:
                    if response.status_code == 200:
                        tracks = response.json()
                        track_count = len(tracks)
                        for track in tracks:
                            if self.stop_event.is_set():
                                del tracks
                                self._set_album_scan_state(req_album, "pending")
                                return False
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
                        last_error = RuntimeError(
                            f"Lidarr Track API error {response.status_code}: {response.text[:200]}"
                        )
                        self.logger.error(req_album["album_name"])
                        self.logger.error(f"Lidarr Track API Error Code: {response.status_code}")
                        self.logger.error(f"Lidarr Track API Error Text: {response.text}")
                finally:
                    response.close()
                break

            except Exception as e:
                last_error = e
                if _general.is_resource_exhaustion_error(e) and attempt < 2:
                    self.logger.warning(f'FD exhaustion on track fetch attempt {attempt + 1}, backing off: {req_album["album_name"]}')
                    self.fd.signal_exhaustion_in_background()
                    continue
                break

        if last_error is not None:
            self.logger.error(req_album["album_name"])
            self.logger.error(f"Error Getting Missing Tracks: {last_error}")
            self.emit("new_toast_msg", {"title": "Error Getting Missing Tracks", "message": str(last_error)})
            self._set_album_scan_state(req_album, "error", last_error)
            return False

        self._set_album_scan_state(req_album, "complete")
        self.logger.warning(
            f'Track scan complete: {req_album["artist"]} - {req_album["album_name"]} '
            f'({req_album["missing_count"]} missing of {req_album["track_count"]} tracks)'
        )

        if self.on_album_scanned is not None:
            self.on_album_scanned(req_album)

        return True

    def reset(self):
        cache_cleanup_error = None
        cache_removed_count = 0
        try:
            self.stop_event.set()
            for future in self.futures:
                if not future.done():
                    future.cancel()
            self.futures = []
            self.items = []
            self._set_state("idle")
            self.progress = self._default_progress()
            cache_removed_count = self.clear_cache()
            self.logger.warning("Lidarr reset complete")
        except Exception as e:
            cache_cleanup_error = str(e)
            self.logger.error(f"Lidarr reset failed: {e}")
        finally:
            self.emit_update()
            if cache_cleanup_error:
                self.emit("new_toast_msg", {"title": "Lidarr Reset Error", "message": cache_cleanup_error})
            else:
                if cache_removed_count:
                    msg = f"Reset complete. Cleared {cache_removed_count} cache file(s)."
                else:
                    msg = "Reset complete. No cache files were present."
                self.emit("new_toast_msg", {"title": "Lidarr Reset", "message": msg})

    def _interruptible_sleep(self, seconds):
        """Sleep for up to `seconds`, returning early if the lidarr stop event fires."""
        self.stop_event.wait(timeout=seconds)
