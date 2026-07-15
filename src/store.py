"""Durable, small-footprint persistence for LidaTube download runs.

The application has a single gevent worker but performs download work from native
threads.  A Store owns one SQLite connection guarded by a process-wide re-entrant
lock, which keeps every operation short and safe for both callers.
"""

import os
import json
import sqlite3
import threading
from datetime import datetime, timezone


_DB_LOCK = threading.RLock()


class Store:
    """SQLite-backed persistence for sessions, queued albums, results, and overrides."""

    QUEUE_STATUSES = ("pending", "in_progress", "done", "error")

    def __init__(self, path):
        self.path = os.fspath(path)
        parent = os.path.dirname(os.path.abspath(self.path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._initialize()

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat()

    def _initialize(self):
        with _DB_LOCK:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    status TEXT NOT NULL,
                    requested_count INTEGER NOT NULL DEFAULT 0,
                    matched_count INTEGER NOT NULL DEFAULT 0,
                    failed_count INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS queue_items (
                    id INTEGER PRIMARY KEY,
                    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    position INTEGER NOT NULL,
                    artist_id INTEGER,
                    album_id INTEGER,
                    album_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending', 'in_progress', 'done', 'error')),
                    updated_at TEXT NOT NULL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_queue_items_session_position
                    ON queue_items(session_id, position);

                CREATE INDEX IF NOT EXISTS idx_queue_items_session_status_position
                    ON queue_items(session_id, status, position);

                CREATE TABLE IF NOT EXISTS track_results (
                    id INTEGER PRIMARY KEY,
                    session_id INTEGER NOT NULL REFERENCES sessions(id),
                    artist TEXT,
                    album TEXT,
                    track_title TEXT,
                    track_number INTEGER,
                    track_id INTEGER,
                    duration_ms INTEGER,
                    outcome TEXT NOT NULL CHECK(outcome IN ('matched', 'no_match', 'error')),
                    link TEXT,
                    title_of_link TEXT,
                    matched_via TEXT CHECK(matched_via IN ('ytmusic', 'ytmusic_secondary', 'yt') OR matched_via IS NULL),
                    suspicion INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS evaluations (
                    id INTEGER PRIMARY KEY,
                    track_result_id INTEGER NOT NULL REFERENCES track_results(id) ON DELETE CASCADE,
                    source TEXT,
                    candidate_title TEXT,
                    candidate_url TEXT,
                    candidate_duration_s REAL,
                    score REAL,
                    rejected_by TEXT,
                    detail TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_evaluations_track_result_id
                    ON evaluations(track_result_id);

                CREATE TABLE IF NOT EXISTS overrides (
                    track_id INTEGER PRIMARY KEY,
                    forced_url TEXT NOT NULL,
                    note TEXT,
                    created_at TEXT NOT NULL
                );
                """
            )
            # A process restart has no active worker capable of completing a run.
            self._connection.execute(
                "UPDATE sessions SET status = 'interrupted', ended_at = COALESCE(ended_at, ?) "
                "WHERE status = 'running'",
                (self._now(),),
            )
            self._connection.commit()

    def close(self):
        with _DB_LOCK:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def start_session(self, requested_count=0, status="running"):
        with _DB_LOCK:
            cursor = self._connection.execute(
                "INSERT INTO sessions (started_at, status, requested_count) VALUES (?, ?, ?)",
                (self._now(), status, requested_count),
            )
            self._connection.commit()
            return cursor.lastrowid

    def finish_session(self, session_id, status, matched_count=None, failed_count=None):
        updates = ["ended_at = ?", "status = ?"]
        values = [self._now(), status]
        if matched_count is not None:
            updates.append("matched_count = ?")
            values.append(matched_count)
        if failed_count is not None:
            updates.append("failed_count = ?")
            values.append(failed_count)
        values.append(session_id)
        with _DB_LOCK:
            self._connection.execute(
                f"UPDATE sessions SET {', '.join(updates)} WHERE id = ?", values
            )
            self._connection.commit()

    def increment_session_requested_count(self, session_id, amount):
        """Add newly enqueued tracks to a session's requested total."""
        if not amount:
            return
        with _DB_LOCK:
            self._connection.execute(
                "UPDATE sessions SET requested_count = requested_count + ? WHERE id = ?",
                (int(amount), session_id),
            )
            self._connection.commit()

    def enqueue_items(self, session_id, items):
        """Append serialized album dictionaries to a session in stable order."""
        now = self._now()
        with _DB_LOCK:
            row = self._connection.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 AS next_position "
                "FROM queue_items WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            position = row["next_position"]
            rows = []
            for item in items:
                if isinstance(item, str):
                    album_json = item
                    decoded = json.loads(item)
                else:
                    decoded = item
                    album_json = json.dumps(item, separators=(",", ":"))
                rows.append(
                    (
                        session_id,
                        position,
                        decoded.get("artist_id"),
                        decoded.get("album_id"),
                        album_json,
                        "pending",
                        now,
                    )
                )
                position += 1

            if rows:
                self._connection.executemany(
                    """
                    INSERT INTO queue_items (
                        session_id, position, artist_id, album_id, album_json,
                        status, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
                self._connection.commit()
            return len(rows)

    def next_batch(self, session_id, limit):
        """Return the next pending queue rows without changing their status."""
        limit = max(1, int(limit))
        return self._fetchall(
            """
            SELECT * FROM queue_items
            WHERE session_id = ? AND status = 'pending'
            ORDER BY position ASC LIMIT ?
            """,
            (session_id, limit),
        )

    def mark_queue_item(self, queue_item_id, status):
        if status not in self.QUEUE_STATUSES:
            raise ValueError(f"Invalid queue status: {status}")
        with _DB_LOCK:
            self._connection.execute(
                "UPDATE queue_items SET status = ?, updated_at = ? WHERE id = ?",
                (status, self._now(), queue_item_id),
            )
            self._connection.commit()

    def mark_queue_items(self, queue_item_ids, status):
        if status not in self.QUEUE_STATUSES:
            raise ValueError(f"Invalid queue status: {status}")
        ids = [int(queue_item_id) for queue_item_id in queue_item_ids]
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with _DB_LOCK:
            self._connection.execute(
                f"UPDATE queue_items SET status = ?, updated_at = ? WHERE id IN ({placeholders})",
                (status, self._now(), *ids),
            )
            self._connection.commit()

    def queue_counts(self, session_id):
        row = self._fetchone(
            """
            SELECT
                COALESCE(SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END), 0) AS pending,
                COALESCE(SUM(CASE WHEN status = 'in_progress' THEN 1 ELSE 0 END), 0) AS in_progress,
                COALESCE(SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END), 0) AS done,
                COALESCE(SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END), 0) AS error,
                COUNT(*) AS total
            FROM queue_items WHERE session_id = ?
            """,
            (session_id,),
        )
        return row or {
            "pending": 0,
            "in_progress": 0,
            "done": 0,
            "error": 0,
            "total": 0,
        }

    def resumable_session(self):
        """Return the newest unfinished session with persisted work remaining."""
        return self._fetchone(
            """
            SELECT s.* FROM sessions AS s
            WHERE s.status IN ('running', 'interrupted', 'stopped', 'failed')
              AND EXISTS (
                  SELECT 1 FROM queue_items AS q
                  WHERE q.session_id = s.id AND q.status IN ('pending', 'in_progress')
              )
            ORDER BY s.started_at DESC, s.id DESC LIMIT 1
            """
        )

    def resume_session(self, session_id):
        """Make crash-time work pending again and reopen a session."""
        now = self._now()
        with _DB_LOCK:
            self._connection.execute(
                """
                UPDATE queue_items SET status = 'pending', updated_at = ?
                WHERE session_id = ? AND status = 'in_progress'
                """,
                (now, session_id),
            )
            cursor = self._connection.execute(
                "UPDATE sessions SET status = 'running', ended_at = NULL WHERE id = ?",
                (session_id,),
            )
            self._connection.commit()
            return cursor.rowcount > 0

    def clear_queue(self, session_id):
        with _DB_LOCK:
            cursor = self._connection.execute(
                "DELETE FROM queue_items WHERE session_id = ?", (session_id,)
            )
            self._connection.commit()
            return cursor.rowcount

    def record_track_result(
        self,
        *,
        session_id,
        artist,
        album,
        track_title,
        track_number,
        track_id,
        duration_ms,
        outcome,
        link=None,
        title_of_link=None,
        matched_via=None,
        suspicion=0,
    ):
        now = self._now()
        with _DB_LOCK:
            cursor = self._connection.execute(
                """
                INSERT INTO track_results (
                    session_id, artist, album, track_title, track_number, track_id,
                    duration_ms, outcome, link, title_of_link, matched_via, suspicion,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    artist,
                    album,
                    track_title,
                    track_number,
                    track_id,
                    duration_ms,
                    outcome,
                    link,
                    title_of_link,
                    matched_via,
                    suspicion,
                    now,
                    now,
                ),
            )
            self._connection.commit()
            return cursor.lastrowid

    def record_evaluations(self, track_result_id, evaluations):
        rows = [
            (
                track_result_id,
                evaluation.get("source"),
                evaluation.get("candidate_title"),
                evaluation.get("candidate_url") or evaluation.get("videoId"),
                evaluation.get("candidate_duration_s"),
                evaluation.get("score"),
                evaluation.get("rejected_by"),
                evaluation.get("detail"),
            )
            for evaluation in evaluations
        ]
        if not rows:
            return
        with _DB_LOCK:
            self._connection.executemany(
                """
                INSERT INTO evaluations (
                    track_result_id, source, candidate_title, candidate_url,
                    candidate_duration_s, score, rejected_by, detail
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            self._connection.commit()

    def list_sessions(self, limit=100, offset=0):
        return self._fetchall(
            "SELECT * FROM sessions ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )

    def get_session_tracks(self, session_id, limit=100, offset=0):
        return self._fetchall(
            """
            SELECT * FROM track_results WHERE session_id = ?
            ORDER BY track_number ASC, id ASC LIMIT ? OFFSET ?
            """,
            (session_id, limit, offset),
        )

    def list_no_match(self, order_by_suspicion=True, limit=100, offset=0):
        order_by = "suspicion DESC, updated_at DESC, id DESC" if order_by_suspicion else "id ASC"
        return self._fetchall(
            f"SELECT * FROM track_results WHERE outcome = 'no_match' ORDER BY {order_by} LIMIT ? OFFSET ?",
            (limit, offset),
        )

    def list_attention(self, limit=100, offset=0, minimum_suspicion=25):
        return self._fetchall(
            """
            SELECT * FROM track_results
            WHERE outcome = 'no_match' OR (outcome = 'matched' AND suspicion >= ?)
            ORDER BY suspicion DESC, updated_at DESC, id DESC LIMIT ? OFFSET ?
            """,
            (minimum_suspicion, limit, offset),
        )

    def count_attention(self, minimum_suspicion=25):
        return self._fetchone(
            "SELECT COUNT(*) AS count FROM track_results WHERE outcome = 'no_match' OR (outcome = 'matched' AND suspicion >= ?)",
            (minimum_suspicion,),
        )["count"]

    def get_evaluations(self, track_result_id):
        return self._fetchall(
            "SELECT * FROM evaluations WHERE track_result_id = ? ORDER BY id ASC",
            (track_result_id,),
        )

    def set_override(self, track_id, forced_url, note=None):
        with _DB_LOCK:
            self._connection.execute(
                """
                INSERT INTO overrides (track_id, forced_url, note, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(track_id) DO UPDATE SET forced_url = excluded.forced_url, note = excluded.note
                """,
                (track_id, forced_url, note, self._now()),
            )
            self._connection.commit()

    def get_override(self, track_id):
        return self._fetchone("SELECT * FROM overrides WHERE track_id = ?", (track_id,))

    def list_overrides(self, limit=100, offset=0):
        return self._fetchall(
            "SELECT * FROM overrides ORDER BY created_at DESC, track_id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )

    def count_sessions(self):
        return self._fetchone("SELECT COUNT(*) AS count FROM sessions")["count"]

    def count_session_tracks(self, session_id):
        return self._fetchone("SELECT COUNT(*) AS count FROM track_results WHERE session_id = ?", (session_id,))["count"]

    def count_no_match(self):
        return self._fetchone("SELECT COUNT(*) AS count FROM track_results WHERE outcome = 'no_match'")["count"]

    def delete_override(self, track_id):
        with _DB_LOCK:
            self._connection.execute("DELETE FROM overrides WHERE track_id = ?", (track_id,))
            self._connection.commit()

    def get_session_result_counts(self, session_id):
        row = self._fetchone(
            """
            SELECT
                COALESCE(SUM(CASE WHEN outcome = 'matched' THEN 1 ELSE 0 END), 0) AS matched_count,
                COALESCE(SUM(CASE WHEN outcome IN ('no_match', 'error') THEN 1 ELSE 0 END), 0) AS failed_count
            FROM track_results WHERE session_id = ?
            """,
            (session_id,),
        )
        return row or {"matched_count": 0, "failed_count": 0}

    def _fetchone(self, query, params=()):
        with _DB_LOCK:
            row = self._connection.execute(query, params).fetchone()
        return dict(row) if row is not None else None

    def _fetchall(self, query, params=()):
        with _DB_LOCK:
            rows = self._connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]
