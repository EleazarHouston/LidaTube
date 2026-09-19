"""HTTP API routes and Socket.IO event handlers, delegating to a DataHandler's components."""

import os
import threading

from flask import jsonify, render_template, request


def _page_args():
    try:
        limit = int(request.args.get("limit", 100))
        offset = int(request.args.get("offset", 0))
    except (TypeError, ValueError):
        return None
    if not 1 <= limit <= 200 or offset < 0:
        return None
    return limit, offset


def register_routes(app, socketio, data_handler):
    @app.route("/")
    def home():
        return render_template("base.html")

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
