"""Durable, small-footprint persistence for LidaTube download runs.

The application has a single gevent worker but performs download work from native
threads.  A Store owns one SQLite connection guarded by a process-wide re-entrant
lock, which keeps every operation short and safe for both callers.
"""

import os
import sqlite3
import threading
from datetime import datetime, timezone


_DB_LOCK = threading.RLock()


class Store:
    """SQLite-backed persistence for sessions, match results, and overrides."""

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

    def _fetchone(self, query, params=()):
        with _DB_LOCK:
            row = self._connection.execute(query, params).fetchone()
        return dict(row) if row is not None else None

    def _fetchall(self, query, params=()):
        with _DB_LOCK:
            rows = self._connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]
