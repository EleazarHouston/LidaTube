"""The persisted download queue: runs download sessions over albums selected in Lidarr.

Selections are written to the store and processed in bounded batches, so a session
survives worker restarts and never holds the whole selection in memory. Each album
is searched (LinkSearcher), downloaded and tagged (Downloader), then optionally
imported into Lidarr. Stop keeps pending albums for Resume; Reset deletes them.
"""

import concurrent.futures
import functools
import json
import logging
import os
import threading

import _general


class DownloadQueue:
    def __init__(self, config, store, scanner, searcher, downloader, lidarr_client, stop_event, emit,
                 logger=None, yield_to_event_loop=None, gate=None):
        """yield_to_event_loop() lets a cooperative (gevent) worker run other greenlets during long loops."""
        self.config = config
        self.store = store
        self.scanner = scanner
        self.searcher = searcher
        self.downloader = downloader
        self.lidarr_client = lidarr_client
        self.stop_event = stop_event
        self.emit = emit
        self.logger = logger or logging.getLogger(__name__)
        self.yield_to_event_loop = yield_to_event_loop or (lambda: None)
        # Audio verification between yt-dlp and Lidarr (see verification_gate); None or disabled skips it.
        self.gate = gate

        self.current_session_id = None
        # While a scheduled sync is scanning Lidarr, albums join the running session as they are scanned.
        self.streaming_mode = False
        self.items = []
        self.futures = []
        self.status = "idle"
        self.in_progress = False
        self._reset_session_ids = set()
        self.index = 0
        self.percent_completion = 0
        self.batch_number = 0
        self.progress = {
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

    def emit_update(self):
        # The current batch is bounded; omit its per-track arrays from frequent updates.
        items = [
            {
                "artist": item.get("artist", ""),
                "album_name": item.get("album_name", ""),
                "status": item.get("status", ""),
            }
            for item in self.items
        ]
        self.emit(
            "ytdlp_update",
            {
                "status": self.status,
                "data": items,
                "percent_completion": self.percent_completion,
                "queue": dict(self.progress),
            },
        )

    def _refresh_progress(self, session_id=None):
        session_id = session_id or self.current_session_id
        if session_id is None:
            return self.progress
        counts = self.store.queue_counts(session_id)
        results = self.store.get_session_result_counts(session_id)
        self.progress = {
            "session_id": session_id,
            **counts,
            "batch": self.batch_number,
            "matched": results["matched_count"],
            "failed": results["failed_count"],
        }
        completed = counts["done"] + counts["error"]
        self.percent_completion = 100 * completed / counts["total"] if counts["total"] else 0
        return self.progress

    def snapshot(self):
        """Persisted queue counts for the running session, or else the resumable one."""
        session_id = self.current_session_id
        if session_id is None:
            resumable = self.store.resumable_session()
            session_id = resumable["id"] if resumable else None
        if session_id is not None:
            self._refresh_progress(session_id)
        return dict(self.progress)

    def auto_resume(self):
        # Crash recovery only: a session the user stopped stays stopped until they resume it.
        if getattr(self.config, "auto_resume", True):
            self.resume(emit=False, include_user_stopped=False)

    def start(self, session_id):
        """Run session_id on the queue thread; False if a session is already running."""
        if self.in_progress:
            return False
        previous_session_id, previous_status = self.current_session_id, self.status
        self.current_session_id = session_id
        self.stop_event.clear()
        self.status = "running"
        self.in_progress = True
        thread = threading.Thread(
            target=self.run,
            args=(session_id,),
            name="Queue_Thread",
            daemon=True,
        )
        try:
            thread.start()
        except Exception:
            # Without a thread nothing is running; don't leave the UI and API reporting a live session.
            self.current_session_id, self.status = previous_session_id, previous_status
            self.in_progress = False
            raise
        return True

    def resume(self, emit=True, include_user_stopped=True):
        if self.in_progress:
            return False
        session = self.store.resumable_session(include_user_stopped=include_user_stopped)
        if session is None:
            return False
        self.batch_number = 0
        self.index = 0
        self.items = []
        self.store.resume_session(session["id"])
        started = self.start(session["id"])
        if emit:
            self._refresh_progress(session["id"])
            self.emit_update()
        return started

    def add_albums(self, data):
        session_id = None
        created_session = False
        added = 0
        try:
            selected = {
                int(index) for index in data
                if isinstance(index, int) and 0 <= index < len(self.scanner.items)
            }
            if not selected:
                raise ValueError("No valid albums were selected")

            self.stop_event.clear()
            if self.in_progress and self.current_session_id is not None:
                session_id = self.current_session_id
            else:
                session_id = self.store.start_session(requested_count=0)
                self.current_session_id = session_id
                created_session = True
                self.batch_number = 0
                self.index = 0
                self.items = []
                self.percent_completion = 0

            chunk = []
            chunk_tracks = 0
            for index, item in enumerate(self.scanner.items):
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
                    self.yield_to_event_loop()

            if chunk:
                added += self.store.enqueue_items(session_id, chunk)
                self.store.increment_session_requested_count(session_id, chunk_tracks)

            self._refresh_progress(session_id)
            self.logger.warning(
                f"Added {added} album(s) to persisted download queue for session {session_id}"
            )

            if created_session:
                self.start(session_id)

        except Exception as e:
            self.logger.error(str(e))
            if created_session and session_id is not None:
                self.store.finish_session(session_id, "failed")
                if self.current_session_id == session_id:
                    self.current_session_id = None
            self.emit("new_toast_msg", {"title": "Error adding new items", "message": str(e)})

        finally:
            self.emit_update()
            if added:
                self.emit(
                    "new_toast_msg",
                    {"title": "Download Queue Updated", "message": f"Added {added} album(s) to queue"},
                )

    def run(self, session_id=None):
        session_id = session_id or self.current_session_id
        try:
            if session_id is None:
                self.status = "complete"
                self.in_progress = False
                return

            self.current_session_id = session_id
            self.store.resume_session(session_id)
            self.status = "running"
            self.futures = []
            batch_size = max(1, int(getattr(self.config, "batch_size", 200)))
            self.logger.warning(
                f"Master queue started: session={session_id}, batch_size={batch_size}, "
                f"thread_limit={self.config.thread_limit}"
            )

            # Rolling queue: each download slot takes the next pending album as soon as it frees,
            # so one long album holds only its own slot, and reordering the queue takes effect at
            # the next free slot. The UI shows a window of the last batch_size albums.
            slots = max(1, int(self.config.thread_limit))
            self.items = []
            active = {}
            with concurrent.futures.ThreadPoolExecutor(max_workers=slots) as executor:
                while True:
                    claimed = [] if self.stop_event.is_set() else self._claim(session_id, slots - len(active))
                    for queue_item_id, req_album in claimed:
                        self._show(req_album, max(batch_size, slots), active)
                        future = executor.submit(self._process_item, queue_item_id, req_album)
                        active[future] = (queue_item_id, req_album)
                    self.futures = list(active)
                    if claimed:
                        self._refresh_progress(session_id)
                        self.emit_update()
                    if not active:
                        if self.stop_event.is_set():
                            break
                        if self.streaming_mode:
                            self.stop_event.wait(0.25)
                            continue
                        break
                    finished, _ = concurrent.futures.wait(
                        active, timeout=0.25, return_when=concurrent.futures.FIRST_COMPLETED
                    )
                    for future in finished:
                        queue_item_id, _ = active.pop(future)
                        try:
                            future.result()
                        except concurrent.futures.CancelledError:
                            self.store.mark_queue_item(queue_item_id, "pending")
                    if finished:
                        self._refresh_progress(session_id)
                        self.emit_update()
                        # Yield between albums so the gevent worker can service heartbeats.
                        if not self.stop_event.wait(0.05):
                            self.yield_to_event_loop()
                self.futures = []

            if session_id in self._reset_session_ids:
                self.status = "idle"
            elif self.stop_event.is_set():
                self.status = "stopped"
                self.logger.warning("Downloading Stopped")
            else:
                self.status = "complete"
                self.logger.warning("Downloading Finished")
                if self.config.library_scan_on_completion:
                    self.lidarr_client.rescan_library()

        except Exception as e:
            self.logger.error(f"Error in Master Queue: {e}")
            self.status = "failed"
            self.emit("new_toast_msg", {"title": "Error in Master Queue", "message": str(e)})

        finally:
            self.in_progress = False
            self._reset_session_ids.discard(session_id)
            if session_id is not None and self.current_session_id == session_id:
                self._refresh_progress(session_id)
                counts = self.store.get_session_result_counts(session_id)
                self.store.finish_session(session_id, self.status, **counts)
                self.current_session_id = None
            self.emit_update()
            self.emit("new_toast_msg", {"title": "End of Session", "message": f"Downloading {self.status.capitalize()}"})

    def _claim(self, session_id, free_slots):
        """Take up to free_slots pending albums from the front of the queue, marking them in progress."""
        if free_slots <= 0:
            return []
        claimed = []
        for row in self.store.next_batch(session_id, free_slots):
            try:
                req_album = json.loads(row["album_json"])
            except (TypeError, ValueError):
                self.store.mark_queue_item(row["id"], "error")
                continue
            req_album["_queue_item_id"] = row["id"]
            req_album["_attempts"] = row.get("attempts", 0)
            claimed.append((row["id"], req_album))
        if claimed:
            self.batch_number += 1
            self.store.mark_queue_items([queue_item_id for queue_item_id, _ in claimed], "in_progress")
        return claimed

    def _show(self, req_album, window, active):
        """Add an album to the UI's queue view, dropping the oldest finished ones beyond window."""
        self.items.append(req_album)
        running = {id(album) for _, album in active.values()} | {id(req_album)}
        while len(self.items) > window:
            oldest_finished = next((album for album in self.items if id(album) not in running), None)
            if oldest_finished is None:
                break
            self.items.remove(oldest_finished)

    def _process_item(self, queue_item_id, req_album):
        queue_status = "error"
        retry = False
        try:
            self.download_album(req_album)
            if req_album.pop("_search_blocked", False):
                # A YouTube block is not this album's fault: leave it queued instead of
                # consuming it, so the session picks it up once searching works again.
                self.logger.warning(
                    f'Keeping {req_album["artist"]} - {req_album["album_name"]} queued: search was blocked'
                )
                queue_status = "pending"
            elif self.stop_event.is_set():
                queue_status = "pending"
            elif req_album.get("status") == "Download Error":
                queue_status = "error"
            elif req_album.get("status") == "Refresh data unavailable" or req_album.get("_failed_tracks"):
                retry = True
            else:
                queue_status = "done"
        except Exception as e:
            req_album["status"] = "Download Error"
            self.logger.error(f"Unhandled queue item error: {e}")
        finally:
            if retry:
                self._retry_later(queue_item_id, req_album)
            else:
                self.store.mark_queue_item(queue_item_id, queue_status)
            self._refresh_progress(self.current_session_id)
            self.emit_update()

    def _retry_later(self, queue_item_id, req_album):
        """Send an album with unfinished tracks to the back of the queue, or give up on it as an error."""
        attempts = req_album.get("_attempts", 0) + 1
        limit = max(1, int(self.config.download_attempts_per_album))
        status = "pending" if attempts < limit else "error"
        failed = req_album.get("_failed_tracks")
        # Only the tracks still wanted go round again; the others may already be in the library.
        album = _album_for_retry(req_album, failed) if failed else None
        self.store.retry_queue_item(queue_item_id, status, album=album)
        wanted = f"{len(failed)} track(s)" if failed else "the track list"
        action = "will retry" if status == "pending" else "giving up on"
        self.logger.warning(
            f'{req_album["artist"]} - {req_album["album_name"]}: {action} {wanted} '
            f"after attempt {attempts} of {limit}"
        )

    def begin_streaming(self):
        """Take albums from a scheduled Lidarr scan as they are scanned, starting a session if none is running."""
        self.streaming_mode = True
        self.stop_event.clear()
        try:
            if not self.in_progress:
                self.items = []
                self.percent_completion = 0
                self.index = 0
                self.current_session_id = self.store.start_session(requested_count=0)
                self.start(self.current_session_id)
        except Exception:
            # Left on, streaming mode keeps the next session waiting for albums that never arrive.
            self.streaming_mode = False
            raise

    def end_streaming(self):
        self.streaming_mode = False

    def enqueue_scanned_album(self, req_album):
        """During a scheduled sync, queue each album for download as soon as its scan completes."""
        if not self.streaming_mode:
            return
        req_album["status"] = "Queued"
        if self.current_session_id is None:
            self.current_session_id = self.store.start_session(requested_count=0)
            self.start(self.current_session_id)
        self.store.enqueue_items(self.current_session_id, [req_album])
        self.store.increment_session_requested_count(
            self.current_session_id, len(req_album.get("missing_tracks", []))
        )

    def _wait_for_album_scan_data(self, req_album):
        if req_album.get("scan_ready", True):
            return True

        req_album["status"] = "Waiting for refresh data"
        self.emit_update()

        if not req_album.get("scan_in_progress", False):
            self.logger.warning(f'Prioritizing scan for queued album: {req_album["artist"]} - {req_album["album_name"]}')
            self.scanner.scan_album_tracks(req_album)
            if req_album.get("scan_ready", False):
                return True

        while not self.stop_event.is_set():
            if req_album.get("scan_ready", False):
                return True
            if self.scanner.status != "busy":
                return req_album.get("scan_ready", False)
            self.stop_event.wait(0.5)

        return False

    def download_album(self, req_album):
        try:
            if not self._wait_for_album_scan_data(req_album):
                req_album["status"] = "Download Stopped" if self.stop_event.is_set() else "Refresh data unavailable"
                return

            self.searcher.find_links(req_album, self.current_session_id)
            if self.stop_event.is_set():
                req_album["status"] = "Download Stopped"
                return

            req_album["status"] = "Starting Download"
            artist_str = os.path.basename(req_album["artist_path"].rstrip("/"))
            album_name = req_album["album_name"]
            folder_with_year = req_album["album_folder"]
            grabbed_count = existing_count = error_count = held_count = 0
            verified_files = []
            song_links = [x for x in req_album["missing_tracks"] if x["link"] != ""]
            total_req = len(song_links)
            self.logger.warning(f"Valid link count of {total_req} for: {artist_str} - {album_name}")

            # Tracks whose search or download failed; the queue brings the album back for them.
            failed_tracks = [x for x in req_album["missing_tracks"] if x["link"] == "" and x.get("_search_failed")]
            req_album["_failed_tracks"] = failed_tracks
            for song in song_links:
                if self.stop_event.is_set():
                    break

                outcome, verified_file = self._download_song(song, req_album, artist_str, album_name, folder_with_year)
                if verified_file:
                    verified_files.append(verified_file)
                if outcome == "grabbed":
                    grabbed_count += 1
                elif outcome == "existing":
                    existing_count += 1
                elif outcome == "held":
                    held_count += 1
                else:
                    error_count += 1
                if (outcome == "error" and not self.stop_event.is_set()) or (
                    outcome == "rejected" and not song.get("link") and song.get("_search_failed")
                ):
                    failed_tracks.append(song)
                if outcome in ("grabbed", "held"):
                    self.stop_event.wait(self.config.sleep_interval)
                if self.stop_event.is_set():
                    break

                song_processed_count = grabbed_count + error_count + existing_count + held_count
                req_album["status"] = f"Processed: {song_processed_count} of {total_req}"
                self.emit_update()

            # Files already staged count too: their earlier import may never have landed.
            if self.config.attempt_lidarr_import and grabbed_count + existing_count > 0 and not self.stop_event.is_set():
                if self.gate is not None and self.gate.enabled:
                    if verified_files:
                        self.lidarr_client.import_album(req_album, only_files=verified_files)
                else:
                    self.lidarr_client.import_album(req_album)

            if self.stop_event.is_set():
                req_album["status"] = "Download Stopped"
            elif total_req < req_album["missing_count"]:
                req_album["status"] = "Album Incomplete"
            elif grabbed_count + existing_count == total_req:
                req_album["status"] = "Download Complete"
            elif error_count == total_req:
                req_album["status"] = "Download Failed"
            elif held_count == total_req:
                req_album["status"] = "Held for Verification"
            else:
                req_album["status"] = "Partially Complete"

            self.logger.warning(
                f'Download summary for {artist_str} - {album_name}: '
                f'grabbed={grabbed_count}, existing={existing_count}, held={held_count}, errors={error_count}, '
                f'total={total_req} | status: {req_album["status"]}'
            )

        except Exception as e:
            self.logger.error(f"Error Downloading: {e}")
            req_album["status"] = "Download Error"

        finally:
            self.index += 1
            self.emit_update()

    def _download_song(self, song, req_album, artist_str, album_name, folder_with_year):
        """Download one track. Returns (outcome, verified file name or None).

        outcome is "grabbed", "existing", "held" for verification, "rejected" or "error". With
        verification on, a file already at the track's path is checked like a fresh download,
        and a file AcoustID rejects is replaced by searching that track again (its rejected
        video now excluded), up to verification_attempts_per_track tries.
        """
        verifying = self.gate is not None and self.gate.enabled
        attempts = max(1, int(self.config.verification_attempts_per_track)) if verifying else 1
        for attempt in range(1, attempts + 1):
            title = song["title_of_link"]
            title_str = _general.convert_to_lidarr_format(title)
            track_number = str(song["absolute_track_number"]).zfill(2)
            file_name = os.path.join(artist_str, folder_with_year, f"{artist_str} - {album_name} - {track_number} - {title_str}")
            full_file_path_with_ext = os.path.join(self.config.download_folder, f"{file_name}.{self.config.preferred_codec}")

            existed = os.path.exists(full_file_path_with_ext)
            if existed:
                self.logger.warning(f"File Already Exists: {artist_str} - {title_str}")
                self._record_download(song, True)
                if not verifying:
                    return "existing", None
            else:
                self.logger.warning(f"Starting Download of: {title}")
                on_failure = functools.partial(self._reject_unplayable, song)
                if not self.downloader.download(song["link"], file_name, on_failure=on_failure):
                    # A download cut short by Stop says nothing about the track.
                    if not self.stop_event.is_set():
                        self._record_download(song, False)
                    return "error", None
                self._record_download(song, True)
                _general.add_metadata(self.logger, song, req_album, full_file_path_with_ext)
                if not verifying:
                    return "grabbed", None

            verdict = self.gate.check(full_file_path_with_ext, song, req_album)
            if verdict == "accepted":
                return ("existing" if existed else "grabbed"), os.path.basename(full_file_path_with_ext)
            if verdict == "held":
                return "held", None
            if attempt == attempts or self.stop_event.is_set() or not self._search_track_again(song, req_album):
                return "rejected", None
        return "rejected", None

    def _record_download(self, song, downloaded):
        result_id = song.get("_track_result_id")
        if result_id:
            self.store.set_download_result(result_id, downloaded)

    def _reject_unplayable(self, song, error):
        """Rule out a video that can never be downloaded, so the retry searches for another."""
        video_id = _general.youtube_video_id(song.get("link"))
        if video_id and song.get("track_id") is not None and _general.is_permanent_video_error(error):
            self.store.reject_video(song["track_id"], video_id, "unplayable", "download", detail=str(error)[:300])

    def _search_track_again(self, song, req_album):
        """Search one track again after its download was rejected; True when a new link was found."""
        song["link"], song["title_of_link"] = "", ""
        song.pop("_track_result_id", None)
        song.pop("_match_trace", None)
        song.pop("_search_failed", None)
        single_track = dict(req_album)
        single_track["missing_tracks"] = [song]
        single_track["missing_count"] = 1
        # More tracks than missing ones keeps the search off the whole-album stage.
        single_track["track_count"] = max(int(req_album.get("track_count") or 0), 1) + 1
        single_track["_album_search_done"] = True
        self.searcher.find_links(single_track, self.current_session_id)
        return bool(song.get("link"))

    def stop(self):
        try:
            self.stop_event.set()
            # Persist the stop before unwinding: if the worker is killed before the queue
            # thread finishes, the session must still read as stopped and not auto-resume.
            session_id = self.current_session_id or self.progress.get("session_id")
            if session_id is not None:
                self.store.finish_session(session_id, "stopped")
            for future in self.futures:
                if not future.done():
                    future.cancel()
            for item in self.items:
                if item.get("status") not in (
                    "Download Complete", "Album Incomplete", "Download Failed", "Partially Complete"
                ):
                    item["status"] = "Download Stopped"
        except Exception as e:
            self.logger.error(f"Error Stopping yt_dlp: {e}")
        finally:
            self.status = "stopped"
            self.emit_update()

    def reset(self):
        session_id = self.current_session_id or self.progress.get("session_id")
        try:
            self.stop_event.set()
            for future in self.futures:
                if not future.done():
                    future.cancel()
            if session_id is not None:
                self._reset_session_ids.add(session_id)
                self.store.clear_queue(session_id, on_chunk=self.yield_to_event_loop)
                counts = self.store.get_session_result_counts(session_id)
                self.store.finish_session(session_id, "reset", **counts)
            self.current_session_id = None
            self.futures = []
            self.items = []
            self.status = "idle"
            self.in_progress = False
            self.index = 0
            self.percent_completion = 0
            self.batch_number = 0
            self.progress = {
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
            self.logger.error(f"Error Stopping yt_dlp: {e}")
            self.emit("new_toast_msg", {"title": "Download Reset Error", "message": str(e)})
        else:
            self.logger.warning("Reset Complete")
            self.emit("new_toast_msg", {"title": "Downloads Reset", "message": "Download queue cleared"})
        finally:
            self.emit_update()


def _album_for_retry(req_album, tracks):
    """req_album as it should be queued again: only these tracks, to be searched afresh."""
    def fresh(track):
        kept = {key: value for key, value in track.items() if not key.startswith("_")}
        return dict(kept, link="", title_of_link="")

    album = {key: value for key, value in req_album.items() if not key.startswith("_")}
    album.update(missing_tracks=[fresh(track) for track in tracks], missing_count=len(tracks), status="Queued")
    return album
