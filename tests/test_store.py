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
