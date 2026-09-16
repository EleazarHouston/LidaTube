import pytest

from store import Store


def test_store_round_trip_sessions_tracks_evaluations_and_overrides(tmp_path):
    store = Store(tmp_path / "lidatube.db")

    session_id = store.start_session(requested_count=2)
    matched_id = store.record_track_result(
        session_id=session_id,
        artist="Artist",
        album="Album",
        track_title="Matched Track",
        track_number=1,
        track_id=101,
        duration_ms=180000,
        outcome="matched",
        link="https://youtube.test/watch?v=matched",
        title_of_link="Artist - Matched Track",
        matched_via="ytmusic",
        suspicion=4,
    )
    no_match_id = store.record_track_result(
        session_id=session_id,
        artist="Artist",
        album="Album",
        track_title="Missing Track",
        track_number=2,
        track_id=102,
        duration_ms=200000,
        outcome="no_match",
        suspicion=91,
    )
    store.record_evaluations(no_match_id, [
        {
            "source": "ytmusic",
            "candidate_title": "Wrong version",
            "candidate_url": "https://youtube.test/watch?v=wrong",
            "candidate_duration_s": 201,
            "score": 88.5,
            "rejected_by": "version_gate",
            "detail": "candidate is instrumental",
        }
    ])
    store.set_override(102, "https://youtube.test/watch?v=forced", "known good upload")
    store.finish_session(session_id, status="complete", matched_count=1, failed_count=1)

    sessions = store.list_sessions()
    assert sessions == [
        {
            "id": session_id,
            "started_at": sessions[0]["started_at"],
            "ended_at": sessions[0]["ended_at"],
            "status": "complete",
            "requested_count": 2,
            "matched_count": 1,
            "failed_count": 1,
        }
    ]

    tracks = store.get_session_tracks(session_id)
    assert [track["id"] for track in tracks] == [matched_id, no_match_id]
    assert tracks[1]["outcome"] == "no_match"

    assert store.get_evaluations(no_match_id) == [
        {
            "id": store.get_evaluations(no_match_id)[0]["id"],
            "track_result_id": no_match_id,
            "source": "ytmusic",
            "candidate_title": "Wrong version",
            "candidate_url": "https://youtube.test/watch?v=wrong",
            "candidate_duration_s": 201.0,
            "score": 88.5,
            "rejected_by": "version_gate",
            "detail": "candidate is instrumental",
        }
    ]
    assert store.get_override(102)["forced_url"] == "https://youtube.test/watch?v=forced"
    assert store.list_overrides()[0]["track_id"] == 102

    store.close()


def test_store_paginates_no_matches_by_descending_suspicion(tmp_path):
    store = Store(tmp_path / "lidatube.db")
    session_id = store.start_session()
    for track_id, suspicion in ((1, 30), (2, 90), (3, 60)):
        store.record_track_result(
            session_id=session_id,
            artist="Artist",
            album="Album",
            track_title=f"Track {track_id}",
            track_number=track_id,
            track_id=track_id,
            duration_ms=0,
            outcome="no_match",
            suspicion=suspicion,
        )

    assert [row["track_id"] for row in store.list_no_match(limit=2, offset=0)] == [2, 3]
    assert [row["track_id"] for row in store.list_no_match(limit=2, offset=2)] == [1]
    assert [row["track_id"] for row in store.list_no_match(order_by_suspicion=False)] == [1, 2, 3]

    store.close()


def test_store_initializes_empty_database_and_marks_open_sessions_interrupted(tmp_path):
    db_path = tmp_path / "nested" / "lidatube.db"
    store = Store(db_path)
    assert db_path.exists()
    assert store.list_sessions() == []

    session_id = store.start_session(status="running")
    store.close()

    reopened = Store(db_path)
    assert reopened.list_sessions()[0]["id"] == session_id
    assert reopened.list_sessions()[0]["status"] == "interrupted"
    reopened.close()


def test_persisted_queue_round_trip_order_status_and_counts(tmp_path):
    store = Store(tmp_path / "lidatube.db")
    session_id = store.start_session(requested_count=3)
    albums = [
        {"artist": "Artist", "artist_id": 7, "album_name": f"Album {index}", "album_id": index}
        for index in (30, 10, 20)
    ]

    assert store.enqueue_items(session_id, albums[:2]) == 2
    assert store.enqueue_items(session_id, albums[2:]) == 1
    batch = store.next_batch(session_id, 2)
    assert [row["position"] for row in batch] == [0, 1]
    assert [row["album_id"] for row in batch] == [30, 10]

    store.mark_queue_item(batch[0]["id"], "done")
    store.mark_queue_item(batch[1]["id"], "error")
    remaining = store.next_batch(session_id, 10)
    assert [row["album_id"] for row in remaining] == [20]
    store.mark_queue_items([remaining[0]["id"]], "in_progress")

    assert store.queue_counts(session_id) == {
        "pending": 0,
        "in_progress": 1,
        "done": 1,
        "error": 1,
        "total": 3,
    }
    store.close()


def test_reopen_finds_resumable_session_and_resets_in_progress(tmp_path):
    db_path = tmp_path / "lidatube.db"
    store = Store(db_path)
    older_id = store.start_session()
    store.enqueue_items(older_id, [{"album_id": 1}])
    store.finish_session(older_id, "stopped")

    session_id = store.start_session()
    store.enqueue_items(session_id, [{"album_id": 2}, {"album_id": 3}])
    first = store.next_batch(session_id, 1)[0]
    store.mark_queue_item(first["id"], "in_progress")
    store.close()

    reopened = Store(db_path)
    resumable = reopened.resumable_session()
    assert resumable["id"] == session_id
    assert resumable["status"] == "interrupted"
    assert reopened.queue_counts(session_id)["in_progress"] == 1

    assert reopened.resume_session(session_id) is True
    assert reopened.queue_counts(session_id) == {
        "pending": 2,
        "in_progress": 0,
        "done": 0,
        "error": 0,
        "total": 2,
    }
    assert reopened.list_sessions()[0]["status"] == "running"
    reopened.close()


def test_clear_queue_removes_only_requested_session(tmp_path):
    store = Store(tmp_path / "lidatube.db")
    first_id = store.start_session()
    second_id = store.start_session()
    store.enqueue_items(first_id, [{"album_id": 1}, {"album_id": 2}])
    store.enqueue_items(second_id, [{"album_id": 3}])

    assert store.clear_queue(first_id) == 2
    assert store.queue_counts(first_id)["total"] == 0
    assert store.queue_counts(second_id)["total"] == 1
    store.close()



def test_store_adopts_unversioned_database_without_losing_data(tmp_path):
    import sqlite3

    import store as store_module

    path = tmp_path / "legacy.db"
    store = Store(path)
    session_id = store.start_session()
    store.set_override(42, "https://youtube.test/keep", "keep me")
    store.close()
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 0")
    for _ in range(2):
        store = Store(path)
        assert store._connection.execute("PRAGMA user_version").fetchone()[0] == len(store_module._MIGRATIONS)
        assert store.list_sessions()[0]["id"] == session_id
        assert store.list_sessions()[0]["status"] == "interrupted"
        assert store.get_override(42)["note"] == "keep me"
        store.close()


def test_store_rejects_newer_schema_without_changing_session_state(tmp_path):
    import sqlite3
    import pytest

    path = tmp_path / "future.db"
    store = Store(path)
    store.start_session()
    store.close()
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 999")
    with pytest.raises(RuntimeError, match="newer than supported"):
        Store(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 999
        assert connection.execute("SELECT status FROM sessions").fetchone()[0] == "running"


def test_store_rolls_back_failed_migration_and_can_retry(tmp_path, monkeypatch):
    import sqlite3
    import pytest
    import store as store_module

    path = tmp_path / "migration.db"
    Store(path).close()
    migrations = store_module._MIGRATIONS
    monkeypatch.setattr(store_module, "_MIGRATIONS", migrations + (
        "CREATE TABLE example (id INTEGER); INSERT INTO missing_table VALUES (1);",
    ))
    with pytest.raises(sqlite3.OperationalError):
        Store(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == len(migrations)
        assert connection.execute("SELECT name FROM sqlite_master WHERE name = 'example'").fetchone() is None
    monkeypatch.setattr(store_module, "_MIGRATIONS", migrations + ("CREATE TABLE example (id INTEGER);",))
    store = Store(path)
    assert store._connection.execute("PRAGMA user_version").fetchone()[0] == len(migrations) + 1
    store.close()


def test_resumable_session_skips_user_stopped_sessions_by_default(tmp_path):
    store = Store(tmp_path / "lidatube.db")
    session_id = store.start_session(requested_count=1)
    store.enqueue_items(session_id, [{"artist": "A", "album_name": "B", "missing_tracks": []}])
    store.finish_session(session_id, "stopped")

    assert store.resumable_session() is None
    resumable = store.resumable_session(include_user_stopped=True)
    assert resumable is not None and resumable["id"] == session_id
    store.close()


@pytest.mark.parametrize("status", ["running", "interrupted", "failed"])
def test_resumable_session_still_offers_crashed_sessions(tmp_path, status):
    store = Store(tmp_path / "lidatube.db")
    session_id = store.start_session(requested_count=1)
    store.enqueue_items(session_id, [{"artist": "A", "album_name": "B", "missing_tracks": []}])
    store.finish_session(session_id, status)

    assert store.resumable_session()["id"] == session_id
    store.close()


def test_stopped_status_survives_a_restart(tmp_path):
    path = tmp_path / "lidatube.db"
    store = Store(path)
    session_id = store.start_session(requested_count=1)
    store.enqueue_items(session_id, [{"artist": "A", "album_name": "B", "missing_tracks": []}])
    store.finish_session(session_id, "stopped")
    store.close()

    restarted = Store(path)
    assert restarted.list_sessions(10, 0)[0]["status"] == "stopped"
    assert restarted.resumable_session() is None
    restarted.close()


def test_clear_queue_deletes_in_chunks_and_yields_between_them(tmp_path):
    store = Store(tmp_path / "lidatube.db")
    session_id = store.start_session(requested_count=5)
    store.enqueue_items(session_id, [{"artist": "A", "album_name": f"Album {n}", "missing_tracks": []} for n in range(5)])
    yields = []

    removed = store.clear_queue(session_id, chunk_size=2, on_chunk=lambda: yields.append(1))

    assert removed == 5
    assert store.queue_counts(session_id)["total"] == 0
    assert len(yields) >= 2
    store.close()


def test_clear_queue_commits_each_chunk(tmp_path):
    import sqlite3

    path = tmp_path / "lidatube.db"
    store = Store(path)
    session_id = store.start_session(requested_count=4)
    store.enqueue_items(session_id, [{"artist": "A", "album_name": f"Album {n}", "missing_tracks": []} for n in range(4)])

    observed = []

    def observe():
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as other:
            observed.append(other.execute("SELECT COUNT(*) FROM queue_items").fetchone()[0])

    store.clear_queue(session_id, chunk_size=2, on_chunk=observe)

    assert observed and min(observed) < 4
    store.close()


def test_track_results_are_indexed_by_session(tmp_path):
    store = Store(tmp_path / "lidatube.db")
    indexes = {row[0] for row in store._connection.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_track_results_session_id" in indexes
    plan = " ".join(str(part) for row in store._connection.execute(
        "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM track_results WHERE session_id = 1") for part in row)
    assert "idx_track_results_session_id" in plan
    store.close()
