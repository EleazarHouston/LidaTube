"""The verification gate between yt-dlp and Lidarr: keep, reject or hold each download."""

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from audio_verify import VerificationUnavailable, Verdict
from store import Store
from verification_gate import VerificationGate

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)


class FakeVerifier:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def verify(self, path, track, album=None):
        self.calls.append((path, track.get("track_title")))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def build_gate(tmp_path, verifier, key="key", enabled=True):
    config = Mock()
    config.acoustid_api_key = key
    config.audio_verification = enabled
    config.download_folder = str(tmp_path / "downloads")
    config.CONFIG_FOLDER = str(tmp_path / "config")
    config.verification_retry_minutes = 30
    config.attempt_lidarr_import = True
    store = Store(tmp_path / "lidatube.db")
    gate = VerificationGate(config, store, lidarr_client=Mock(), logger=Mock(),
                            verifier_factory=lambda key: verifier, clock=lambda: NOW)
    return gate, store


def downloaded_file(tmp_path, name="Artist - Album - 01 - Song.mp3"):
    path = tmp_path / "downloads" / "Artist" / "Album (2024)" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"audio")
    return str(path)


def matched_track(store, track_id=7, link="https://www.youtube.com/watch?v=abcdefghijk"):
    session_id = store.start_session(requested_count=1)
    result_id = store.record_track_result(session_id=session_id, artist="Artist", album="Album", track_title="Song",
                                          track_number=1, track_id=track_id, duration_ms=180000, outcome="matched",
                                          link=link, title_of_link="Song", matched_via="yt", suspicion=0)
    return {"artist": "Artist", "track_title": "Song", "track_id": track_id, "recording_id": "rec", "link": link,
            "_track_result_id": result_id}


def verification_of(store, track):
    return store._fetchone("SELECT verification, suspicion FROM track_results WHERE id = ?", (track["_track_result_id"],))


ALBUM = {"artist": "Artist", "album_name": "Album", "artist_path": "/music/Artist", "album_folder": "Album (2024)"}


@pytest.mark.parametrize("key, enabled, expected", [("key", True, True), ("", True, False), ("key", False, False)])
def test_gate_runs_only_when_switched_on_with_a_key(tmp_path, key, enabled, expected):
    gate, _ = build_gate(tmp_path, FakeVerifier(), key=key, enabled=enabled)
    assert gate.enabled is expected


def test_verified_file_is_kept_and_recorded(tmp_path):
    gate, store = build_gate(tmp_path, FakeVerifier(Verdict("verified", "expected recording")))
    track = matched_track(store)
    path = downloaded_file(tmp_path)
    assert gate.check(path, track, ALBUM) == "accepted"
    assert os.path.exists(path)
    assert verification_of(store, track) == {"verification": "verified", "suspicion": 0}


@pytest.mark.parametrize("status, suspicion", [("no_match", 75), ("variant", 60), ("weak", 50), ("flagged", 50)])
def test_unconfirmed_files_are_kept_but_marked_suspicious(tmp_path, status, suspicion):
    gate, store = build_gate(tmp_path, FakeVerifier(Verdict(status, "why")))
    track = matched_track(store)
    assert gate.check(downloaded_file(tmp_path), track, ALBUM) == "accepted"
    assert verification_of(store, track) == {"verification": status, "suspicion": suspicion}


def test_rejected_file_is_deleted_and_its_video_rejected_for_the_track(tmp_path):
    verdict = Verdict("rejected", "AcoustID heard an unrelated title", ["(Or) Is It Love"])
    gate, store = build_gate(tmp_path, FakeVerifier(verdict))
    track = matched_track(store)
    path = downloaded_file(tmp_path)
    assert gate.check(path, track, ALBUM) == "rejected"
    assert not os.path.exists(path)
    assert store.rejected_video_ids(7) == {"abcdefghijk"}
    row = store.list_rejected_videos()[0]
    assert (row["reason"], row["source"], row["detail"]) == ("acoustid_mismatch", "acoustid", "AcoustID heard: (Or) Is It Love")
    assert verification_of(store, track)["verification"] == "rejected"


@pytest.mark.parametrize("error", [VerificationUnavailable("AcoustID unreachable (ConnectionError)"), RuntimeError("boom")])
def test_unverifiable_file_is_held_out_of_the_album_folder(tmp_path, error):
    gate, store = build_gate(tmp_path, FakeVerifier(error))
    track = matched_track(store)
    path = downloaded_file(tmp_path)
    assert gate.check(path, track, ALBUM) == "held"
    assert not os.path.exists(path)
    pending = store.due_pending_verifications((NOW + timedelta(minutes=30)).isoformat())
    assert len(pending) == 1
    held = pending[0]["held_path"]
    assert os.path.exists(held)
    assert held.startswith(str(tmp_path / "config" / "pending_verification"))
    assert pending[0]["final_path"] == path
    assert pending[0]["video_id"] == "abcdefghijk"
    assert store.due_pending_verifications((NOW + timedelta(minutes=29)).isoformat()) == []
    assert verification_of(store, track) == {"verification": "pending", "suspicion": 50}


def test_held_file_is_released_and_imported_once_verified(tmp_path):
    verifier = FakeVerifier(VerificationUnavailable("down"), Verdict("verified", "same title"))
    gate, store = build_gate(tmp_path, verifier)
    track = matched_track(store)
    path = downloaded_file(tmp_path)
    gate.check(path, track, ALBUM)

    gate.clock = lambda: NOW + timedelta(minutes=31)
    assert gate.retry_due() == {"accepted": 1, "rejected": 0, "held": 0, "missing": 0}
    assert os.path.exists(path)
    assert store.count_pending_verifications() == 0
    assert verification_of(store, track)["verification"] == "verified"
    gate.lidarr_client.import_album.assert_called_once_with(ALBUM, only_files=[os.path.basename(path)])


def test_held_file_is_deleted_when_verification_later_rejects_it(tmp_path):
    verifier = FakeVerifier(VerificationUnavailable("down"), Verdict("rejected", "different", ["Other"]))
    gate, store = build_gate(tmp_path, verifier)
    track = matched_track(store)
    path = downloaded_file(tmp_path)
    gate.check(path, track, ALBUM)
    held = store.due_pending_verifications((NOW + timedelta(days=1)).isoformat())[0]["held_path"]

    gate.clock = lambda: NOW + timedelta(minutes=31)
    assert gate.retry_due()["rejected"] == 1
    assert not os.path.exists(held) and not os.path.exists(path)
    assert store.rejected_video_ids(7) == {"abcdefghijk"}
    gate.lidarr_client.import_album.assert_not_called()


def test_held_file_stays_held_while_services_are_down(tmp_path):
    verifier = FakeVerifier(VerificationUnavailable("down"), VerificationUnavailable("still down"))
    gate, store = build_gate(tmp_path, verifier)
    track = matched_track(store)
    gate.check(downloaded_file(tmp_path), track, ALBUM)

    gate.clock = lambda: NOW + timedelta(minutes=31)
    assert gate.retry_due()["held"] == 1
    assert store.count_pending_verifications() == 1
    assert store.due_pending_verifications((NOW + timedelta(minutes=60)).isoformat()) == []
    assert store.due_pending_verifications((NOW + timedelta(minutes=62)).isoformat())[0]["attempts"] == 1
    gate.lidarr_client.import_album.assert_not_called()


def test_vanished_held_file_is_dropped_from_the_queue(tmp_path):
    gate, store = build_gate(tmp_path, FakeVerifier(VerificationUnavailable("down")))
    track = matched_track(store)
    gate.check(downloaded_file(tmp_path), track, ALBUM)
    os.remove(store.due_pending_verifications((NOW + timedelta(days=1)).isoformat())[0]["held_path"])

    gate.clock = lambda: NOW + timedelta(minutes=31)
    assert gate.retry_due()["missing"] == 1
    assert store.count_pending_verifications() == 0


def test_retry_waits_while_verification_is_switched_off(tmp_path):
    gate, store = build_gate(tmp_path, FakeVerifier(VerificationUnavailable("down")))
    gate.check(downloaded_file(tmp_path), matched_track(store), ALBUM)
    gate.config.acoustid_api_key = ""
    gate.clock = lambda: NOW + timedelta(minutes=31)
    assert gate.retry_due() == {"accepted": 0, "rejected": 0, "held": 0, "missing": 0}
    assert store.count_pending_verifications() == 1


def test_start_runs_the_retry_loop_in_a_daemon_thread(tmp_path, monkeypatch):
    import verification_gate

    gate, _ = build_gate(tmp_path, FakeVerifier())
    started = []
    monkeypatch.setattr(verification_gate.threading.Thread, "start", lambda self: started.append((self.daemon, self._target)))
    gate.start()
    assert started == [(True, gate.run)]


def test_track_queued_before_recording_ids_were_kept_asks_lidarr_for_it(tmp_path):
    verifier = FakeVerifier(Verdict("verified", "expected recording"))
    seen = []
    verifier.verify = lambda path, track, album=None: seen.append(track.get("recording_id")) or Verdict("verified", "ok")
    gate, store = build_gate(tmp_path, verifier)
    gate.lidarr_client.get_recording_id.return_value = "rec-from-lidarr"
    track = matched_track(store)
    del track["recording_id"]
    gate.check(downloaded_file(tmp_path), track, ALBUM)
    assert seen == ["rec-from-lidarr"]
    gate.lidarr_client.get_recording_id.assert_called_once_with(7)


def test_truncated_download_is_deleted_without_rejecting_its_video(tmp_path):
    gate, store = build_gate(tmp_path, FakeVerifier(Verdict("truncated", "file is 100s, Lidarr expects 180s")))
    track = matched_track(store)
    path = downloaded_file(tmp_path)
    assert gate.check(path, track, ALBUM) == "rejected"
    assert not os.path.exists(path)
    assert store.rejected_video_ids(7) == set()
    assert verification_of(store, track)["verification"] == "truncated"
