import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mutagen.id3 import ID3, TXXX, COMM

import library_suspicion as ls


# ---------------------------------------------------------------------------
# score_track (pure)
# ---------------------------------------------------------------------------

def _signals(**over):
    base = {
        "duration_delta_s": None,
        "expected_known": True,
        "is_grab": True,
        "official_art_track": False,
        "provenance_bad_version": False,
        "lidarr_matched": True,
    }
    base.update(over)
    return base


def test_official_art_track_with_good_duration_scores_low():
    score, reasons = ls.score_track(_signals(duration_delta_s=2.0, official_art_track=True))
    assert score == 0
    assert any("official" in r.lower() for r in reasons)


def test_same_duration_instrumental_is_flagged_despite_matching_length():
    # The key case duration alone misses: instrumental with identical runtime.
    score, reasons = ls.score_track(_signals(duration_delta_s=1.0, provenance_bad_version=True))
    assert score >= 45
    assert any("karaoke/instrumental" in r for r in reasons)


def test_wrong_song_large_duration_delta_scores_high():
    score, _ = ls.score_track(_signals(duration_delta_s=130.0))
    assert score >= 55


def test_non_official_grab_adds_suspicion():
    low = ls.score_track(_signals(duration_delta_s=2.0, official_art_track=True))[0]
    high = ls.score_track(_signals(duration_delta_s=2.0, official_art_track=False))[0]
    assert high > low


def test_no_lidarr_match_adds_points():
    score, reasons = ls.score_track(_signals(duration_delta_s=None, expected_known=False, lidarr_matched=False))
    assert score >= 20
    assert any("Lidarr" in r for r in reasons)


def test_external_source_is_noted_not_penalized_as_grab():
    score, reasons = ls.score_track(_signals(duration_delta_s=2.0, is_grab=False))
    assert any("external source" in r for r in reasons)
    # No non-official-upload penalty for external files
    assert score == 0


def test_score_never_negative():
    score, _ = ls.score_track(_signals(duration_delta_s=0.0, official_art_track=True, lidarr_matched=True))
    assert score >= 0


# ---------------------------------------------------------------------------
# provenance parsing / helpers
# ---------------------------------------------------------------------------

def _write_mp3_with_frames(path, purl="", description="", comment=""):
    tags = ID3()
    if purl:
        tags.add(TXXX(encoding=3, desc="purl", text=[purl]))
    if description:
        tags.add(TXXX(encoding=3, desc="description", text=[description]))
    if comment:
        tags.add(COMM(encoding=3, lang="eng", desc="", text=[comment]))
    tags.save(path)


def test_read_provenance_extracts_purl_and_description(tmp_path):
    f = tmp_path / "song.mp3"
    f.write_bytes(b"")
    _write_mp3_with_frames(
        str(f),
        purl="https://www.youtube.com/watch?v=abc123",
        description="Provided to YouTube by Sub Pop Records\n\nBattery Kinzie",
    )
    prov = ls.read_provenance(str(f))
    assert "youtube.com/watch?v=abc123" in prov["purl"]
    assert prov["description"].lower().startswith("provided to youtube by")


def test_is_youtube_grab_true_for_purl(tmp_path):
    prov = {"purl": "https://www.youtube.com/watch?v=x", "description": "", "comment": ""}
    assert ls.is_youtube_grab(prov) is True


def test_is_youtube_grab_false_for_scene_release():
    prov = {"purl": "", "description": "", "comment": "PMEDIA"}
    assert ls.is_youtube_grab(prov) is False


def test_is_official_art_track_detects_provided_by():
    assert ls.is_official_art_track({"description": "Provided to YouTube by X", "comment": "", "purl": ""})
    assert not ls.is_official_art_track({"description": "some random uploader notes", "comment": "", "purl": ""})


def test_provenance_strong_marker_flags_karaoke_not_in_request():
    prov = {"description": "Good Enough (Karaoke Version) originally performed by Anita Baker", "comment": "", "purl": ""}
    assert ls.provenance_has_strong_marker(prov, expected_title="Good Enough")


def test_provenance_strong_marker_allows_when_request_wants_instrumental():
    prov = {"description": "Song (Instrumental)", "comment": "", "purl": ""}
    assert not ls.provenance_has_strong_marker(prov, expected_title="Song (Instrumental)")


def test_provenance_strong_marker_ignores_official_description():
    prov = {"description": "Provided to YouTube by Sub Pop Records\n\nBattery Kinzie", "comment": "", "purl": ""}
    assert not ls.provenance_has_strong_marker(prov, expected_title="Battery Kinzie")


# ---------------------------------------------------------------------------
# assess_file (integration with mocks)
# ---------------------------------------------------------------------------

def test_assess_file_flags_same_duration_instrumental(tmp_path):
    f = tmp_path / "Artist - Album - 01 - A Past Embrace.mp3"
    f.write_bytes(b"")
    _write_mp3_with_frames(
        str(f),
        purl="https://www.youtube.com/watch?v=zzz",
        description="A Past Embrace (Instrumental) - some karaoke channel instrumental",
    )
    tags = {"artist": "156/Silence", "album": "People Watching", "title": "A Past Embrace", "track_number": 1}
    track_list = [{"trackNumber": 1, "title": "A Past Embrace", "duration": 200000}]

    with patch("library_suspicion.audit_library.read_tags", return_value=tags), \
         patch("library_suspicion.audit_library.get_audio_duration_seconds", return_value=200.0), \
         patch("library_suspicion.audit_library.lookup_lidarr_tracks", return_value=track_list):
        row = ls.assess_file(str(f), MagicMock(), "http://lidarr.test", "key")

    # Duration matches (delta ~0) yet it's flagged via provenance.
    assert row["delta_s"] in (0.0, 0)
    assert row["score"] >= 45
    assert "instrumental" in row["reasons"].lower() or "karaoke" in row["reasons"].lower()


def test_assess_file_official_track_scores_low(tmp_path):
    f = tmp_path / "Artist - Album - 02 - Battery Kinzie.mp3"
    f.write_bytes(b"")
    _write_mp3_with_frames(
        str(f),
        purl="https://www.youtube.com/watch?v=sGQhRY4gLRQ",
        description="Provided to YouTube by Sub Pop Records\n\nBattery Kinzie",
    )
    tags = {"artist": "Fleet Foxes", "album": "Helplessness Blues", "title": "Battery Kinzie", "track_number": 2}
    track_list = [{"trackNumber": 2, "title": "Battery Kinzie", "duration": 168000}]

    with patch("library_suspicion.audit_library.read_tags", return_value=tags), \
         patch("library_suspicion.audit_library.get_audio_duration_seconds", return_value=168.0), \
         patch("library_suspicion.audit_library.lookup_lidarr_tracks", return_value=track_list):
        row = ls.assess_file(str(f), MagicMock(), "http://lidarr.test", "key")

    assert row["official"] is True
    assert row["score"] == 0
