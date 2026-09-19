"""The persisted download queue: runs download sessions over albums selected in Lidarr.

Selections are written to the store and processed in bounded batches, so a session
survives worker restarts and never holds the whole selection in memory. Each album
is searched (LinkSearcher), downloaded and tagged (Downloader), then optionally
imported into Lidarr. Stop keeps pending albums for Resume; Reset deletes them.
"""

import concurrent.futures
import json
import logging
import os
import threading

import _general


class DownloadQueue:
    def __init__(self, config, store, scanner, searcher, downloader, lidarr_client, stop_event, emit,
                 logger=None, yield_to_event_loop=None):
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
        thread.start()
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

            while not self.stop_event.is_set():
                batch_rows = self.store.next_batch(session_id, batch_size)
                if not batch_rows:
                    if self.streaming_mode:
                        self.stop_event.wait(0.25)
                        continue
                    break

                self.batch_number += 1
                self.index = 0
                self.items = []
                work_items = []
                for row in batch_rows:
                    try:
                        req_album = json.loads(row["album_json"])
                    except (TypeError, ValueError):
                        self.store.mark_queue_item(row["id"], "error")
                        continue
                    req_album["_queue_item_id"] = row["id"]
                    self.items.append(req_album)
                    work_items.append((row["id"], req_album))

                self.store.mark_queue_items(
                    [queue_item_id for queue_item_id, _ in work_items], "in_progress"
                )
                self._refresh_progress(session_id)
                self.emit_update()

                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=self.config.thread_limit
                ) as executor:
                    future_items = {
                        executor.submit(self._process_item, queue_item_id, req_album): queue_item_id
                        for queue_item_id, req_album in work_items
                    }
                    self.futures = list(future_items)
                    for future in concurrent.futures.as_completed(future_items):
                        queue_item_id = future_items[future]
                        try:
                            future.result()
                        except concurrent.futures.CancelledError:
                            self.store.mark_queue_item(queue_item_id, "pending")

                self._refresh_progress(session_id)
                self.emit_update()
                # Yield between bounded batches so the gevent worker can service heartbeats.
                if not self.stop_event.wait(0.05):
                    self.yield_to_event_loop()

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

    def _process_item(self, queue_item_id, req_album):
        queue_status = "error"
        try:
            self.download_album(req_album)
            if self.stop_event.is_set():
                queue_status = "pending"
            elif req_album.get("status") in ("Download Error", "Refresh data unavailable"):
                queue_status = "error"
            else:
                queue_status = "done"
        except Exception as e:
            req_album["status"] = "Download Error"
            self.logger.error(f"Unhandled queue item error: {e}")
        finally:
            self.store.mark_queue_item(queue_item_id, queue_status)
            self._refresh_progress(self.current_session_id)
            self.emit_update()

    def begin_streaming(self):
        """Take albums from a scheduled Lidarr scan as they are scanned, starting a session if none is running."""
        self.streaming_mode = True
        self.stop_event.clear()
        if not self.in_progress:
            self.items = []
            self.percent_completion = 0
            self.index = 0
            self.current_session_id = self.store.start_session(requested_count=0)
            self.start(self.current_session_id)

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
            grabbed_count = existing_count = error_count = 0
            song_links = [x for x in req_album["missing_tracks"] if x["link"] != ""]
            total_req = len(song_links)
            self.logger.warning(f"Valid link count of {total_req} for: {artist_str} - {album_name}")

            for song in song_links:
                if self.stop_event.is_set():
                    break

                title = song["title_of_link"]
                link = song["link"]
                self.logger.warning(f"Starting Download of: {title}")
                title_str = _general.convert_to_lidarr_format(title)
                track_number = str(song["absolute_track_number"]).zfill(2)
                file_name = os.path.join(artist_str, folder_with_year, f"{artist_str} - {album_name} - {track_number} - {title_str}")
                full_file_path_with_ext = os.path.join(self.config.download_folder, f"{file_name}.{self.config.preferred_codec}")

                if os.path.exists(full_file_path_with_ext):
                    existing_count += 1
                    self.logger.warning(f"File Already Exists: {artist_str} - {title_str}")
                else:
                    success = self.downloader.download(link, file_name)
                    if success:
                        _general.add_metadata(self.logger, song, req_album, full_file_path_with_ext)
                        grabbed_count += 1
                        self.stop_event.wait(self.config.sleep_interval)
                        if self.stop_event.is_set():
                            break
                    else:
                        error_count += 1
                        if self.stop_event.is_set():
                            break

                song_processed_count = grabbed_count + error_count + existing_count
                req_album["status"] = f"Processed: {song_processed_count} of {total_req}"
                self.emit_update()

            if self.config.attempt_lidarr_import and grabbed_count > 0 and not self.stop_event.is_set():
                self.lidarr_client.import_album(req_album)

            if self.stop_event.is_set():
                req_album["status"] = "Download Stopped"
            elif total_req < req_album["missing_count"]:
                req_album["status"] = "Album Incomplete"
            elif grabbed_count + existing_count == total_req:
                req_album["status"] = "Download Complete"
            elif error_count == total_req:
                req_album["status"] = "Download Failed"
            else:
                req_album["status"] = "Partially Complete"

            self.logger.warning(
                f'Download summary for {artist_str} - {album_name}: '
                f'grabbed={grabbed_count}, existing={existing_count}, errors={error_count}, total={total_req} | status: {req_album["status"]}'
            )

        except Exception as e:
            self.logger.error(f"Error Downloading: {e}")
            req_album["status"] = "Download Error"

        finally:
            self.index += 1
            self.emit_update()

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
