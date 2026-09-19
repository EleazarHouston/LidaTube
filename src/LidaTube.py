import logging
import os
import threading
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO
from config import AppConfig
from lidarr_client import LidarrClient
from downloader import Downloader
from store import Store
from fd_governor import FdGovernor
from link_search import LinkSearcher
from lidarr_scan import LidarrScanner
from download_queue import DownloadQueue
from scheduler import SyncScheduler


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
        self.fd = FdGovernor(self.general_logger)
        self.fd.apply_safety_limits(self.config)
        self.clients_connected_counter = 0

        # Components. One stop event halts every part of a download session: search, download and queue.
        download_stop_event = threading.Event()
        self.lidarr_client = LidarrClient(self.config, self.general_logger)
        self.downloader = Downloader(self.config, download_stop_event, self.general_logger)
        self.searcher = LinkSearcher(self.config, self.store, self.fd, download_stop_event, self.general_logger)
        self.scanner = LidarrScanner(
            self.config, self.lidarr_client, self.fd, socketio.emit, self.general_logger,
            download_stop_event=download_stop_event,
        )
        self.queue = DownloadQueue(
            self.config, self.store, self.scanner, self.searcher, self.downloader, self.lidarr_client,
            download_stop_event, socketio.emit, self.general_logger,
            yield_to_event_loop=lambda: socketio.sleep(0),
        )
        self.searcher.on_status_change = self.queue.emit_update
        self.scanner.on_album_scanned = self.queue.enqueue_scanned_album

        self.scanner.load_cache()
        self.general_logger.warning(
            "Thread limits in use: downloads=%s, lidarr_scan=%s, ytmusic_parallel=%s",
            self.config.thread_limit,
            self.config.lidarr_scan_thread_limit,
            self.searcher.parallel,
        )

        self.queue.auto_resume()
        self.scheduler = SyncScheduler(self.config, self.scanner, self.queue, self.general_logger)
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
    return jsonify(data_handler.queue.snapshot())


@app.route("/api/session/resume", methods=["POST"])
def api_session_resume():
    if data_handler.queue.in_progress:
        return jsonify({"error": "A download session is already running"}), 409
    if not data_handler.queue.resume():
        return jsonify({"error": "No resumable download session exists"}), 404
    return jsonify(data_handler.queue.snapshot()), 202


@app.route("/api/session/stop", methods=["POST"])
def api_session_stop():
    if not data_handler.queue.in_progress:
        return jsonify({"error": "No download session is running"}), 409
    data_handler.queue.stop()
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
    data_handler.queue.stop()


@socketio.on("reset_ytdlp")
def reset_ytdlp():
    # Clearing a large queue takes minutes of DB work; keep it off the gevent worker so the
    # UI stays responsive and gunicorn does not kill the worker for missing its heartbeat.
    thread = threading.Thread(target=data_handler.queue.reset, name="Reset_Thread", daemon=True)
    thread.start()


@socketio.on("add_to_download_list")
def add_to_download_list(data):
    data_handler.queue.add_albums(data)


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
