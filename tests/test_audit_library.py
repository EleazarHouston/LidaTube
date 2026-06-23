import json
import sys
import os
import pytest
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import audit_library


# ---------------------------------------------------------------------------
# get_audio_duration_seconds
# ---------------------------------------------------------------------------

def test_get_audio_duration_returns_none_for_unknown_extension(tmp_path):
    f = tmp_path / "song.wav"
    f.write_bytes(b"")
    assert audit_library.get_audio_duration_seconds(str(f)) is None


def test_get_audio_duration_mp3(tmp_path):
    f = tmp_path / "song.mp3"
    f.write_bytes(b"")
    mock_audio = MagicMock()
    mock_audio.info.length = 214.5
    with patch("audit_library.MP3", return_value=mock_audio):
        result = audit_library.get_audio_duration_seconds(str(f))
    assert result == pytest.approx(214.5)


def test_get_audio_duration_flac(tmp_path):
    f = tmp_path / "song.flac"
    f.write_bytes(b"")
    mock_audio = MagicMock()
    mock_audio.info.length = 305.0
    with patch("audit_library.FLAC", return_value=mock_audio):
        result = audit_library.get_audio_duration_seconds(str(f))
    assert result == pytest.approx(305.0)


def test_get_audio_duration_returns_none_on_mutagen_error(tmp_path):
    f = tmp_path / "song.mp3"
    f.write_bytes(b"")
    with patch("audit_library.MP3", side_effect=Exception("corrupt")):
        result = audit_library.get_audio_duration_seconds(str(f))
    assert result is None


# ---------------------------------------------------------------------------
# read_tags
# ---------------------------------------------------------------------------

def test_read_tags_mp3_returns_expected_fields(tmp_path):
    f = tmp_path / "song.mp3"
    f.write_bytes(b"")
    mock_tags = {
        "TPE1": MagicMock(text=["Drake"]),
        "TALB": MagicMock(text=["Scorpion"]),
        "TIT2": MagicMock(text=["God's Plan"]),
        "TRCK": MagicMock(text=["1"]),
    }
    with patch("audit_library.ID3", return_value=mock_tags):
        tags = audit_library.read_tags(str(f))
    assert tags["artist"] == "Drake"
    assert tags["album"] == "Scorpion"
    assert tags["title"] == "God's Plan"
    assert tags["track_number"] == 1


def test_read_tags_flac_returns_expected_fields(tmp_path):
    f = tmp_path / "song.flac"
    f.write_bytes(b"")
    mock_audio = MagicMock()
    mock_audio.tags = {
        "artist": ["Glass Animals"],
        "album": ["Dreamland"],
        "title": ["Heat Waves"],
        "tracknumber": ["8"],
    }
    with patch("audit_library.FLAC", return_value=mock_audio):
        tags = audit_library.read_tags(str(f))
    assert tags["artist"] == "Glass Animals"
    assert tags["album"] == "Dreamland"
    assert tags["title"] == "Heat Waves"
    assert tags["track_number"] == 8


def test_read_tags_returns_none_on_error(tmp_path):
    f = tmp_path / "song.mp3"
    f.write_bytes(b"")
    with patch("audit_library.ID3", side_effect=Exception("no tags")):
        assert audit_library.read_tags(str(f)) is None


# ---------------------------------------------------------------------------
# find_track_in_lidarr
# ---------------------------------------------------------------------------

SAMPLE_TRACKS = [
    {"trackNumber": 1, "title": "God's Plan", "duration": 199000},
    {"trackNumber": 2, "title": "Nonstop", "duration": 239000},
    {"trackNumber": 3, "title": "In My Feelings", "duration": 218000},
]


def test_find_track_matches_by_track_number():
    result = audit_library.find_track_in_lidarr(SAMPLE_TRACKS, track_number=2, title="Nonstop")
    assert result is not None
    assert result["title"] == "Nonstop"
    assert result["duration"] == 239000


def test_find_track_falls_back_to_title_when_number_missing():
    result = audit_library.find_track_in_lidarr(SAMPLE_TRACKS, track_number=None, title="In My Feelings")
    assert result is not None
    assert result["trackNumber"] == 3


def test_find_track_returns_none_when_no_match():
    result = audit_library.find_track_in_lidarr(SAMPLE_TRACKS, track_number=99, title="Nonexistent Song")
    assert result is None


def test_find_track_number_takes_priority_over_title():
    # Track number 1 = "God's Plan", but title says "Nonstop" — number wins
    result = audit_library.find_track_in_lidarr(SAMPLE_TRACKS, track_number=1, title="Nonstop")
    assert result["title"] == "God's Plan"


# ---------------------------------------------------------------------------
# audit_file
# ---------------------------------------------------------------------------

def _make_mock_session(track_list, album_list, artist_list):
    """Build a requests.Session mock that serves Lidarr API responses."""
    def fake_get(url, params=None, timeout=None):
        resp = MagicMock()
        resp.status_code = 200
        if "artist" in url and "album" not in url and "track" not in url:
            resp.json.return_value = artist_list
        elif "album" in url:
            resp.json.return_value = album_list
        elif "track" in url:
            resp.json.return_value = track_list
        return resp
    session = MagicMock()
    session.get.side_effect = fake_get
    return session


def test_audit_file_flags_suspect_when_delta_exceeds_tolerance(tmp_path):
    f = tmp_path / "Drake - Scorpion - 01 - God's Plan.mp3"
    f.write_bytes(b"")

    tags = {"artist": "Drake", "album": "Scorpion", "title": "God's Plan", "track_number": 1}
    track_list = [{"trackNumber": 1, "title": "God's Plan", "duration": 199000}]
    album_list = [{"id": 1, "title": "Scorpion"}]
    artist_list = [{"id": 1, "artistName": "Drake"}]

    session = _make_mock_session(track_list, album_list, artist_list)

    with patch("audit_library.get_audio_duration_seconds", return_value=357.0), \
         patch("audit_library.read_tags", return_value=tags):
        result = audit_library.audit_file(str(f), session, "http://lidarr.test", "key", tolerance=15)

    assert result is not None
    assert result["verdict"] == "SUSPECT"
    assert result["delta_s"] == pytest.approx(158.0, abs=0.1)


def test_audit_file_not_flagged_when_delta_within_tolerance(tmp_path):
    f = tmp_path / "Drake - Scorpion - 01 - God's Plan.mp3"
    f.write_bytes(b"")

    tags = {"artist": "Drake", "album": "Scorpion", "title": "God's Plan", "track_number": 1}
    track_list = [{"trackNumber": 1, "title": "God's Plan", "duration": 199000}]
    album_list = [{"id": 1, "title": "Scorpion"}]
    artist_list = [{"id": 1, "artistName": "Drake"}]

    session = _make_mock_session(track_list, album_list, artist_list)

    with patch("audit_library.get_audio_duration_seconds", return_value=201.0), \
         patch("audit_library.read_tags", return_value=tags):
        result = audit_library.audit_file(str(f), session, "http://lidarr.test", "key", tolerance=15)

    assert result is not None
    assert result["verdict"] == "OK"


def test_audit_file_skips_when_lidarr_duration_unknown(tmp_path):
    f = tmp_path / "song.mp3"
    f.write_bytes(b"")

    tags = {"artist": "Drake", "album": "Scorpion", "title": "God's Plan", "track_number": 1}
    track_list = [{"trackNumber": 1, "title": "God's Plan", "duration": 0}]
    album_list = [{"id": 1, "title": "Scorpion"}]
    artist_list = [{"id": 1, "artistName": "Drake"}]

    session = _make_mock_session(track_list, album_list, artist_list)

    with patch("audit_library.get_audio_duration_seconds", return_value=357.0), \
         patch("audit_library.read_tags", return_value=tags):
        result = audit_library.audit_file(str(f), session, "http://lidarr.test", "key", tolerance=15)

    assert result is None


def test_audit_file_skips_when_actual_duration_unknown(tmp_path):
    f = tmp_path / "song.mp3"
    f.write_bytes(b"")

    tags = {"artist": "Drake", "album": "Scorpion", "title": "God's Plan", "track_number": 1}
    track_list = [{"trackNumber": 1, "title": "God's Plan", "duration": 199000}]
    album_list = [{"id": 1, "title": "Scorpion"}]
    artist_list = [{"id": 1, "artistName": "Drake"}]

    session = _make_mock_session(track_list, album_list, artist_list)

    with patch("audit_library.get_audio_duration_seconds", return_value=None), \
         patch("audit_library.read_tags", return_value=tags):
        result = audit_library.audit_file(str(f), session, "http://lidarr.test", "key", tolerance=15)

    assert result is None


def test_audit_file_skips_when_tags_unreadable(tmp_path):
    f = tmp_path / "song.mp3"
    f.write_bytes(b"")

    with patch("audit_library.get_audio_duration_seconds", return_value=200.0), \
         patch("audit_library.read_tags", return_value=None):
        result = audit_library.audit_file(str(f), MagicMock(), "http://lidarr.test", "key", tolerance=15)

    assert result is None


# ---------------------------------------------------------------------------
# Regression tests
# ---------------------------------------------------------------------------

def test_drake_regression_flagged_as_suspect(tmp_path):
    # A different Drake song was grabbed — durations are significantly different.
    f = tmp_path / "song.mp3"
    f.write_bytes(b"")

    tags = {"artist": "Drake", "album": "Scorpion", "title": "God's Plan", "track_number": 1}
    # Expected: 3:19 (199s). Actual file: different Drake song, ~3:38 (218s)
    track_list = [{"trackNumber": 1, "title": "God's Plan", "duration": 199000}]
    album_list = [{"id": 1, "title": "Scorpion"}]
    artist_list = [{"id": 1, "artistName": "Drake"}]

    session = _make_mock_session(track_list, album_list, artist_list)

    with patch("audit_library.get_audio_duration_seconds", return_value=218.0), \
         patch("audit_library.read_tags", return_value=tags):
        result = audit_library.audit_file(str(f), session, "http://lidarr.test", "key", tolerance=15)

    assert result is not None
    assert result["verdict"] == "SUSPECT"


def test_glass_animals_regression_flagged_as_suspect(tmp_path):
    # Wrong video grabbed for Heat Waves — non-Glass-Animals content, different duration.
    f = tmp_path / "song.mp3"
    f.write_bytes(b"")

    tags = {"artist": "Glass Animals", "album": "Dreamland", "title": "Heat Waves", "track_number": 8}
    # Expected: 3:58 (238s). Actual file: wrong content, ~2:02 (122s)
    track_list = [{"trackNumber": 8, "title": "Heat Waves", "duration": 238000}]
    album_list = [{"id": 1, "title": "Dreamland"}]
    artist_list = [{"id": 1, "artistName": "Glass Animals"}]

    session = _make_mock_session(track_list, album_list, artist_list)

    with patch("audit_library.get_audio_duration_seconds", return_value=122.0), \
         patch("audit_library.read_tags", return_value=tags):
        result = audit_library.audit_file(str(f), session, "http://lidarr.test", "key", tolerance=15)

    assert result is not None
    assert result["verdict"] == "SUSPECT"


# ---------------------------------------------------------------------------
# _load_existing_results / reprocess_above
# ---------------------------------------------------------------------------

def _write_csv(path, rows):
    import csv as _csv
    with open(path, "w", newline="") as f:
        writer = _csv.DictWriter(f, fieldnames=audit_library.FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def _make_row(path, delta, verdict):
    return {
        "path": path, "artist": "A", "album": "B", "title": "T",
        "track_number": "1", "actual_s": str(200 + delta), "expected_s": "200.0",
        "delta_s": str(delta), "verdict": verdict,
    }


def test_load_existing_results_default_only_loads_suspects(tmp_path):
    csv_path = str(tmp_path / "results.csv")
    _write_csv(csv_path, [
        _make_row("/a.mp3", 20, "SUSPECT"),
        _make_row("/b.mp3", 12, "OK"),
    ])
    _, suspects, ok_count = audit_library._load_existing_results(csv_path)
    assert len(suspects) == 1 and suspects[0]["path"] == "/a.mp3"
    assert ok_count == 1


def test_load_existing_results_reprocess_promotes_ok_rows_above_threshold(tmp_path):
    csv_path = str(tmp_path / "results.csv")
    _write_csv(csv_path, [
        _make_row("/a.mp3", 20, "SUSPECT"),
        _make_row("/b.mp3", 12, "OK"),   # delta 12 > reprocess_above=10 → promoted
        _make_row("/c.mp3",  8, "OK"),   # delta 8 <= 10 → stays OK
    ])
    _, suspects, ok_count = audit_library._load_existing_results(csv_path, reprocess_above=10)
    paths = {s["path"] for s in suspects}
    assert "/a.mp3" in paths
    assert "/b.mp3" in paths
    assert "/c.mp3" not in paths
    assert ok_count == 1


def test_load_existing_results_reprocess_none_unchanged(tmp_path):
    csv_path = str(tmp_path / "results.csv")
    _write_csv(csv_path, [_make_row("/b.mp3", 12, "OK")])
    _, suspects, ok_count = audit_library._load_existing_results(csv_path, reprocess_above=None)
    assert suspects == []
    assert ok_count == 1


# ---------------------------------------------------------------------------
# Cache persistence
# ---------------------------------------------------------------------------

def _make_sample_cache():
    return {
        "__albums__": {1: {"scorpion": 10}, 2: {"dreamland": 20}},
        ("drake", "scorpion"): [
            {"trackNumber": 1, "title": "God's Plan", "duration": 199000, "extra_field": "ignored"},
        ],
        ("glass animals", "dreamland"): [
            {"trackNumber": 8, "title": "Heat Waves", "duration": 238000},
        ],
    }


def test_save_and_load_lidarr_cache_roundtrip(tmp_path):
    cache_path = str(tmp_path / "lidarr_cache.json")
    cache = _make_sample_cache()
    audit_library._save_lidarr_cache(cache, cache_path)
    assert os.path.exists(cache_path)

    loaded = audit_library._load_lidarr_cache(cache_path)
    assert loaded["__albums__"] == {1: {"scorpion": 10}, 2: {"dreamland": 20}}
    assert ("drake", "scorpion") in loaded
    assert loaded[("drake", "scorpion")][0]["title"] == "God's Plan"
    assert loaded[("drake", "scorpion")][0]["duration"] == 199000
    assert ("glass animals", "dreamland") in loaded


def test_save_lidarr_cache_slims_extra_fields(tmp_path):
    cache_path = str(tmp_path / "lidarr_cache.json")
    audit_library._save_lidarr_cache(_make_sample_cache(), cache_path)
    with open(cache_path) as f:
        raw = json.load(f)
    # extra_field should have been stripped
    saved_track = list(raw["tracks"].values())[0][0]
    assert "extra_field" not in saved_track
    assert "title" in saved_track


def test_load_lidarr_cache_returns_empty_for_missing_file():
    result = audit_library._load_lidarr_cache("/nonexistent/path/lidarr_cache.json")
    assert result == {}


def test_load_lidarr_cache_returns_empty_for_corrupt_file(tmp_path):
    cache_path = tmp_path / "lidarr_cache.json"
    cache_path.write_text("not valid json {{")
    result = audit_library._load_lidarr_cache(str(cache_path))
    assert result == {}


def test_save_lidarr_cache_noop_when_path_is_none():
    cache = _make_sample_cache()
    # Should not raise
    audit_library._save_lidarr_cache(cache, None)
