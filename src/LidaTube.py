import json
import logging
import os
import threading
import time
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO
import concurrent.futures
import _general
from config import AppConfig
from lidarr_client import LidarrClient
from downloader import Downloader
from store import Store
from fd_governor import FdGovernor
from link_search import LinkSearcher
from lidarr_scan import LidarrScanner


class DataHandler:
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
        self.fd = FdGovernor(self.general_logger)
        self.fd.apply_safety_limits(self.config)

        # Download state
        self.ytdlp_items = []
        self.ytdlp_futures = []
        self.ytdlp_status = "idle"
        self.ytdlp_stop_event = threading.Event()
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
        self.searcher = LinkSearcher(
            self.config, self.store, self.fd, self.ytdlp_stop_event, self.general_logger,
            on_status_change=self._emit_ytdlp_update,
        )
        self.scanner = LidarrScanner(
            self.config, self.lidarr_client, self.fd, socketio.emit, self.general_logger,
            download_stop_event=self.ytdlp_stop_event, on_album_scanned=self._enqueue_streamed_album,
        )

        self.scanner.load_cache()
        self.general_logger.warning(
            "Thread limits in use: downloads=%s, lidarr_scan=%s, ytmusic_parallel=%s",
            self.config.thread_limit,
            self.config.lidarr_scan_thread_limit,
            self.searcher.parallel,
        )

        self._auto_resume_if_available()
        thread = threading.Thread(target=self.schedule_checker, name="Schedule_Thread")
        thread.daemon = True
        thread.start()

    # --- SocketIO connection ---

    def connect(self):
        self.scanner.emit_update()
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

                    fetch_thread = threading.Thread(target=self.scanner.fetch_wanted_albums, name="Lidarr_Fetch_Thread")
                    fetch_thread.daemon = True
                    fetch_thread.start()

                    fetch_thread.join()
                    self.streaming_mode = False

                    if not self.scanner.items:
                        self.general_logger.warning("No Missing Albums")
                    self.general_logger.warning("Big sleep for 1 Hour")
                    time.sleep(3600)
                    self.general_logger.warning(f"Checking every 10 minutes as not in a sync time window: {self.config.sync_schedule}")
                else:
                    time.sleep(600)

        except Exception as e:
            self.general_logger.error(f"Error in Scheduler: {e}")
            self.general_logger.error("Scheduler Stopped")

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

    # --- Download queue ---

    def _auto_resume_if_available(self):
        # Crash recovery only: a session the user stopped stays stopped until they resume it.
        if getattr(self.config, "auto_resume", True):
            self.resume_ytdlp(emit=False, include_user_stopped=False)

    @staticmethod
    def _yield_to_event_loop():
        """Let the gevent worker heartbeat during long DB work; gunicorn SIGKILLs it after its timeout."""
        socketio.sleep(0)

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

    def resume_ytdlp(self, emit=True, include_user_stopped=True):
        if self.ytdlp_in_progress_flag:
            return False
        session = self.store.resumable_session(include_user_stopped=include_user_stopped)
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
                if isinstance(index, int) and 0 <= index < len(self.scanner.items)
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
                    self.lidarr_client.rescan_library()

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

    def _enqueue_streamed_album(self, req_album):
        """During a scheduled sync, queue each album for download as soon as its scan completes."""
        if not self.streaming_mode:
            return
        req_album["status"] = "Queued"
        if self.current_session_id is None:
            self.current_session_id = self.store.start_session(requested_count=0)
            self._start_queue_thread(self.current_session_id)
        self.store.enqueue_items(self.current_session_id, [req_album])
        self.store.increment_session_requested_count(
            self.current_session_id, len(req_album.get("missing_tracks", []))
        )

    def _wait_for_album_scan_data(self, req_album):
        if req_album.get("scan_ready", True):
            return True

        req_album["status"] = "Waiting for refresh data"
        self._emit_ytdlp_update()

        if not req_album.get("scan_in_progress", False):
            self.general_logger.warning(f'Prioritizing scan for queued album: {req_album["artist"]} - {req_album["album_name"]}')
            self.scanner.scan_album_tracks(req_album)
            if req_album.get("scan_ready", False):
                return True

        while not self.ytdlp_stop_event.is_set():
            if req_album.get("scan_ready", False):
                return True
            if self.scanner.status != "busy":
                return req_album.get("scan_ready", False)
            self.ytdlp_stop_event.wait(0.5)

        return False

    def find_link_and_download(self, req_album):
        try:
            if not self._wait_for_album_scan_data(req_album):
                req_album["status"] = "Download Stopped" if self.ytdlp_stop_event.is_set() else "Refresh data unavailable"
                return

            self.searcher.find_links(req_album, self.current_session_id)
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
                self.lidarr_client.import_album(req_album)

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
            # Persist the stop before unwinding: if the worker is killed before the queue
            # thread finishes, the session must still read as stopped and not auto-resume.
            session_id = self.current_session_id or self.queue_progress.get("session_id")
            if session_id is not None and getattr(self, "store", None) is not None:
                self.store.finish_session(session_id, "stopped")
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
                self.store.clear_queue(session_id, on_chunk=self._yield_to_event_loop)
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


@app.route("/api/lidarr")
def api_lidarr():
    query = request.args.get("q", "").strip().lower()
    # ids_only powers selection across the virtualized list without loading rows:
    # plain -> every filtered album (select all); checked_only -> the albums the
    # server currently has selected, which is the default download set.
    if request.args.get("ids_only"):
        checked_only = bool(request.args.get("checked_only"))
        indices = data_handler.scanner.filtered_indices(query, checked_only=checked_only)
        return jsonify({"ids": indices, "total": len(indices)})
    indices = data_handler.scanner.filtered_indices(query)
    page = _page_args()
    if page is None:
        return jsonify({"error": "limit must be 1-200 and offset must be non-negative"}), 400
    limit, offset = page
    items = []
    for index in indices[offset:offset + limit]:
        item = data_handler.scanner.items[index]
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
    thread = threading.Thread(target=data_handler.scanner.fetch_wanted_albums, name="Lidarr_Thread")
    thread.daemon = True
    thread.start()


@socketio.on("stop_lidarr")
def stop_lidarr():
    data_handler.scanner.stop_event.set()


@socketio.on("reset_lidarr")
def reset_lidarr():
    data_handler.scanner.reset()


@socketio.on("stop_ytdlp")
def stop_ytdlp():
    data_handler.stop_ytdlp()


@socketio.on("reset_ytdlp")
def reset_ytdlp():
    # Clearing a large queue takes minutes of DB work; keep it off the gevent worker so the
    # UI stays responsive and gunicorn does not kill the worker for missing its heartbeat.
    thread = threading.Thread(target=data_handler.reset_ytdlp, name="Reset_Thread", daemon=True)
    thread.start()


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
