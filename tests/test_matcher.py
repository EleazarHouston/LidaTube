import pytest

import _matcher


def test_remove_album_keywords_removes_known_keywords():
    text = "my album deluxe remastered edition"
    result = _matcher.remove_album_keywords(text)
    assert "deluxe" not in result
    assert "remastered" not in result


def test_remove_song_keywords_removes_known_keywords():
    text = "my track live official version"
    result = _matcher.remove_song_keywords(text)
    assert "live" not in result
    assert "official" not in result
    assert "version" not in result


def test_album_matcher_returns_best_album_match():
    search_results = [
        {
            "type": "Album",
            "title": "Exact Match Album",
            "artists": [{"name": "Various"}, {"name": "Exact Artist"}],
            "browseId": "exact",
        },
        {
            "type": "Album",
            "title": "Different Album",
            "artists": [{"name": "Various"}, {"name": "Different Artist"}],
            "browseId": "other",
        },
    ]

    match = _matcher.album_matcher(
        minimum_match_ratio=80,
        artist="Exact Artist",
        album_name="Exact Match Album",
        cleaned_artist="exact artist",
        cleaned_album="exact match album",
        search_results=search_results,
    )

    assert match is not None
    assert match["browseId"] == "exact"


def test_album_matcher_returns_none_when_nothing_meets_threshold():
    search_results = [
        {
            "type": "Album",
            "title": "Unrelated Record",
            "artists": [{"name": "Various"}, {"name": "Another Artist"}],
            "browseId": "nope",
        }
    ]

    match = _matcher.album_matcher(
        minimum_match_ratio=95,
        artist="Target Artist",
        album_name="Target Album",
        cleaned_artist="target artist",
        cleaned_album="target album",
        search_results=search_results,
    )

    assert match is None


def test_song_matcher_returns_best_song_match():
    search_results = [
        {
            "resultType": "song",
            "title": "Track One (Official Audio)",
            "videoId": "best",
            "artists": [{"name": "Target Artist"}],
        },
        {
            "resultType": "song",
            "title": "Different Song",
            "videoId": "other",
            "artists": [{"name": "Different Artist"}],
        },
    ]

    match = _matcher.song_matcher(
        minimum_match_ratio=75,
        artist="Target Artist",
        cleaned_artist="target artist",
        song_title="Track One",
        cleaned_song_title="track one",
        search_results=search_results,
    )

    assert match is not None
    assert match["videoId"] == "best"


def test_song_matcher_returns_none_for_non_song_results():
    search_results = [
        {
            "resultType": "album",
            "title": "Track One",
            "videoId": "ignored",
            "artists": [{"name": "Target Artist"}],
        }
    ]

    match = _matcher.song_matcher(
        minimum_match_ratio=10,
        artist="Target Artist",
        cleaned_artist="target artist",
        song_title="Track One",
        cleaned_song_title="track one",
        search_results=search_results,
    )

    assert match is None


def test_song_matcher_yt_returns_best_title_match():
    search_results = [
        {"title": "Target Artist - Track Two", "link": "https://a"},
        {"title": "Target Artist - Track One", "link": "https://b"},
    ]

    match = _matcher.song_matcher_yt(
        minimum_match_ratio=70,
        artist="Target Artist",
        query_text="Target Artist - Track One",
        search_results=search_results,
    )

    assert match is not None
    assert match["link"] == "https://b"


def test_song_matcher_yt_returns_none_for_empty_results():
    assert _matcher.song_matcher_yt(50, "Some Artist", "any query", []) is None


def test_song_matcher_yt_regression_uses_combined_rating_not_raw_title(monkeypatch):
    # Regression for a prior bug where best_match_rating tracked title_similarity.
    # In that case, a high raw-title score from an earlier item could block a later
    # item with a better combined score.
    artist = "Test Artist"
    query_text = "Test Artist - query"
    search_results = [
        {"title": "Test Artist - first", "link": "https://first"},
        {"title": "Test Artist - second", "link": "https://second"},
    ]

    monkeypatch.setattr(_matcher._general, "string_cleaner", lambda value: f"clean:{value}")
    monkeypatch.setattr(_matcher, "remove_song_keywords", lambda value: f"trim:{value}")

    score_map = {
        ("Test Artist - query", "Test Artist - first"): 99,
        ("clean:Test Artist - query", "clean:Test Artist - first"): 30,
        ("trim:clean:Test Artist - query", "trim:clean:Test Artist - first"): 30,
        ("Test Artist - query", "Test Artist - second"): 80,
        ("clean:Test Artist - query", "clean:Test Artist - second"): 90,
        ("trim:clean:Test Artist - query", "trim:clean:Test Artist - second"): 90,
    }

    def fake_ratio(left, right):
        return score_map[(left, right)]

    monkeypatch.setattr(_matcher.fuzz, "ratio", fake_ratio)

    match = _matcher.song_matcher_yt(
        minimum_match_ratio=50,
        artist=artist,
        query_text=query_text,
        search_results=search_results,
    )

    assert match is not None
    assert match["link"] == "https://second"


# --- song_matcher_yt: artist gate ---

def test_song_matcher_yt_rejects_result_when_artist_absent_from_title():
    search_results = [
        {"title": "Heat Waves - Piano Cover", "link": "https://wrong"},
        {"title": "Glass Animals - Heat Waves", "link": "https://right"},
    ]

    match = _matcher.song_matcher_yt(
        minimum_match_ratio=50,
        artist="Glass Animals",
        query_text="Glass Animals - Heat Waves",
        search_results=search_results,
    )

    assert match is not None
    assert match["link"] == "https://right"


def test_song_matcher_yt_returns_none_when_all_results_fail_artist_gate():
    search_results = [
        {"title": "Heat Waves - Piano Cover", "link": "https://a"},
        {"title": "Heat Waves (Slowed)", "link": "https://b"},
    ]

    match = _matcher.song_matcher_yt(
        minimum_match_ratio=50,
        artist="Glass Animals",
        query_text="Glass Animals - Heat Waves",
        search_results=search_results,
    )

    assert match is None


def test_song_matcher_yt_artist_gate_is_case_insensitive():
    # Artist comes from Lidarr in mixed case; YouTube titles may be all-lower.
    # The gate must normalize both sides before the substring check.
    search_results = [
        {"title": "glass animals - heat waves", "link": "https://lowercase_title"},
    ]

    match = _matcher.song_matcher_yt(
        minimum_match_ratio=50,
        artist="Glass Animals",
        query_text="Glass Animals - Heat Waves",
        search_results=search_results,
    )

    assert match is not None
    assert match["link"] == "https://lowercase_title"


def test_song_matcher_yt_glass_animals_regression():
    # Regression: secondary search grabbed a non-Glass-Animals video because
    # song_matcher_yt had no artist gate — only title text was scored.
    search_results = [
        {"title": "Heat Waves (Emotional Lo-fi Remix)", "link": "https://wrong_1"},
        {"title": "Heat Waves Cover - Acoustic", "link": "https://wrong_2"},
        {"title": "Glass Animals - Heat Waves (Official Video)", "link": "https://correct"},
    ]

    match = _matcher.song_matcher_yt(
        minimum_match_ratio=50,
        artist="Glass Animals",
        query_text="Glass Animals - Heat Waves",
        search_results=search_results,
    )

    assert match is not None
    assert match["link"] == "https://correct"


# --- duration helpers ---

def test_duration_ok_within_tolerance():
    assert _matcher._duration_ok(expected_ms=214000, candidate_seconds=220, tolerance_seconds=15)


def test_duration_ok_outside_tolerance():
    assert not _matcher._duration_ok(expected_ms=214000, candidate_seconds=260, tolerance_seconds=15)


def test_duration_ok_exactly_at_tolerance_boundary():
    # 214s expected, 229s candidate → delta == 15 → should pass (<=, not <)
    assert _matcher._duration_ok(expected_ms=214000, candidate_seconds=229, tolerance_seconds=15)


def test_duration_ok_skips_gate_when_expected_unknown():
    # expected_ms=0 means Lidarr didn't provide duration — never gate
    assert _matcher._duration_ok(expected_ms=0, candidate_seconds=500, tolerance_seconds=15)


def test_duration_ok_skips_gate_when_candidate_unknown():
    # candidate_seconds=0 means source didn't provide duration — never gate
    assert _matcher._duration_ok(expected_ms=214000, candidate_seconds=0, tolerance_seconds=15)


def test_duration_ok_catches_music_video_length():
    # Drake God's Plan: song=199s, music video=357s → should reject
    assert not _matcher._duration_ok(expected_ms=199000, candidate_seconds=357, tolerance_seconds=15)


def test_parse_duration_string_mm_ss():
    assert _matcher._parse_duration_string("3:54") == 234


def test_parse_duration_string_hh_mm_ss():
    assert _matcher._parse_duration_string("1:03:21") == 3801


def test_parse_duration_string_single_digit_seconds():
    assert _matcher._parse_duration_string("4:09") == 249


def test_parse_duration_string_empty_string():
    assert _matcher._parse_duration_string("") == 0


def test_parse_duration_string_none():
    assert _matcher._parse_duration_string(None) == 0


def test_parse_duration_string_invalid():
    assert _matcher._parse_duration_string("abc") == 0


# --- song_matcher: duration gate ---

def test_song_matcher_rejects_candidate_with_wrong_duration():
    # Two Drake songs with the same artist but very different durations.
    # Only the one matching expected duration should be returned.
    search_results = [
        {
            "resultType": "song",
            "title": "God's Plan",
            "videoId": "wrong_duration",
            "artists": [{"name": "Drake"}],
            "duration_seconds": 357,  # music video length
        },
        {
            "resultType": "song",
            "title": "God's Plan",
            "videoId": "correct",
            "artists": [{"name": "Drake"}],
            "duration_seconds": 199,  # actual song length
        },
    ]

    match = _matcher.song_matcher(
        minimum_match_ratio=70,
        artist="Drake",
        cleaned_artist="drake",
        song_title="God's Plan",
        cleaned_song_title="gods plan",
        search_results=search_results,
        expected_duration_ms=199000,
        duration_tolerance_seconds=15,
    )

    assert match is not None
    assert match["videoId"] == "correct"


def test_song_matcher_passes_when_lidarr_duration_unknown():
    # expected_duration_ms=0 — Lidarr didn't supply it; all candidates should still be considered
    search_results = [
        {
            "resultType": "song",
            "title": "God's Plan",
            "videoId": "vid",
            "artists": [{"name": "Drake"}],
            "duration_seconds": 199,
        },
    ]

    match = _matcher.song_matcher(
        minimum_match_ratio=70,
        artist="Drake",
        cleaned_artist="drake",
        song_title="God's Plan",
        cleaned_song_title="gods plan",
        search_results=search_results,
        expected_duration_ms=0,
        duration_tolerance_seconds=15,
    )

    assert match is not None


def test_song_matcher_passes_when_candidate_duration_absent():
    # Candidate has no duration_seconds field — should not be rejected
    search_results = [
        {
            "resultType": "song",
            "title": "God's Plan",
            "videoId": "vid",
            "artists": [{"name": "Drake"}],
        },
    ]

    match = _matcher.song_matcher(
        minimum_match_ratio=70,
        artist="Drake",
        cleaned_artist="drake",
        song_title="God's Plan",
        cleaned_song_title="gods plan",
        search_results=search_results,
        expected_duration_ms=199000,
        duration_tolerance_seconds=15,
    )

    assert match is not None


def test_song_matcher_drake_regression():
    # Regression: a different Drake song was grabbed instead of the intended one.
    # Same artist passes text scoring; duration disambiguates.
    search_results = [
        {
            "resultType": "song",
            "title": "In My Feelings",
            "videoId": "wrong_song",
            "artists": [{"name": "Drake"}],
            "duration_seconds": 218,
        },
        {
            "resultType": "song",
            "title": "God's Plan",
            "videoId": "correct",
            "artists": [{"name": "Drake"}],
            "duration_seconds": 199,
        },
    ]

    match = _matcher.song_matcher(
        minimum_match_ratio=50,
        artist="Drake",
        cleaned_artist="drake",
        song_title="God's Plan",
        cleaned_song_title="gods plan",
        search_results=search_results,
        expected_duration_ms=199000,
        duration_tolerance_seconds=15,
    )

    assert match is not None
    assert match["videoId"] == "correct"


# --- song_matcher_yt: duration gate ---

def test_song_matcher_yt_rejects_music_video_via_duration():
    # YTS returns the 5:57 music video first; song is 3:19.
    # Duration filter must reject the video.
    search_results = [
        {"title": "Drake - God's Plan", "link": "https://music_video", "duration": "5:57"},
        {"title": "Drake - God's Plan", "link": "https://song", "duration": "3:19"},
    ]

    match = _matcher.song_matcher_yt(
        minimum_match_ratio=70,
        artist="Drake",
        query_text="Drake - God's Plan",
        search_results=search_results,
        expected_duration_ms=199000,
        duration_tolerance_seconds=15,
    )

    assert match is not None
    assert match["link"] == "https://song"


def test_song_matcher_yt_accepts_ytdlp_int_duration():
    # YTDLP returns duration as int seconds (not string)
    search_results = [
        {"title": "Drake - God's Plan", "webpage_url": "https://correct", "duration": 199},
    ]

    match = _matcher.song_matcher_yt(
        minimum_match_ratio=70,
        artist="Drake",
        query_text="Drake - God's Plan",
        search_results=search_results,
        expected_duration_ms=199000,
        duration_tolerance_seconds=15,
    )

    assert match is not None


def test_song_matcher_yt_passes_when_no_duration_in_result():
    # Result has no duration field — should not be rejected
    search_results = [
        {"title": "Drake - God's Plan", "link": "https://nodur"},
    ]

    match = _matcher.song_matcher_yt(
        minimum_match_ratio=70,
        artist="Drake",
        query_text="Drake - God's Plan",
        search_results=search_results,
        expected_duration_ms=199000,
        duration_tolerance_seconds=15,
    )

    assert match is not None


def test_album_matcher_skips_non_album_result_types():
    search_results = [
        {
            "type": "song",
            "title": "Exact Match Album",
            "artists": [{"name": "Various"}, {"name": "Target Artist"}],
            "browseId": "ignored",
        },
        {
            "type": "Album",
            "title": "Exact Match Album",
            "artists": [{"name": "Various"}, {"name": "Target Artist"}],
            "browseId": "expected",
        },
    ]

    match = _matcher.album_matcher(
        minimum_match_ratio=70,
        artist="Target Artist",
        album_name="Exact Match Album",
        cleaned_artist="target artist",
        cleaned_album="exact match album",
        search_results=search_results,
    )

    assert match is not None
    assert match["browseId"] == "expected"


# --- minimum_match_ratio scale normalization ---

def test_normalize_min_ratio_treats_fraction_as_percentage():
    assert _matcher._normalize_min_ratio(0.85) == 85
    assert _matcher._normalize_min_ratio(0.9) == 90


def test_normalize_min_ratio_leaves_percentage_scale_unchanged():
    assert _matcher._normalize_min_ratio(90) == 90
    assert _matcher._normalize_min_ratio(50) == 50


def test_normalize_min_ratio_one_is_full_scale():
    assert _matcher._normalize_min_ratio(1) == 100


def test_song_matcher_yt_fraction_min_ratio_rejects_poor_match():
    # 0.85 must behave like 85 (not "any score above 0.85"), so a weak title
    # match is rejected instead of grabbed as best-of-garbage.
    search_results = [
        {"title": "Target Artist - Completely Unrelated Filler Words Here", "link": "https://weak"},
    ]
    match = _matcher.song_matcher_yt(
        minimum_match_ratio=0.85,
        artist="Target Artist",
        query_text="Target Artist - Heat Waves",
        search_results=search_results,
    )
    assert match is None


# --- asymmetric version gate (karaoke / instrumental / cover) ---

def test_song_matcher_rejects_instrumental_when_not_requested():
    search_results = [
        {
            "resultType": "song",
            "title": "A Past Embrace (Instrumental)",
            "videoId": "instr",
            "artists": [{"name": "156/Silence"}],
            "duration_seconds": 200,
        },
    ]
    match = _matcher.song_matcher(
        minimum_match_ratio=70,
        artist="156/Silence",
        cleaned_artist="156silence",
        song_title="A Past Embrace",
        cleaned_song_title="a past embrace",
        search_results=search_results,
        expected_duration_ms=200000,
    )
    assert match is None


def test_song_matcher_prefers_studio_over_instrumental():
    search_results = [
        {
            "resultType": "song",
            "title": "A Past Embrace (Instrumental)",
            "videoId": "instr",
            "artists": [{"name": "156/Silence"}],
            "duration_seconds": 200,
        },
        {
            "resultType": "song",
            "title": "A Past Embrace",
            "videoId": "studio",
            "artists": [{"name": "156/Silence"}],
            "duration_seconds": 200,
        },
    ]
    match = _matcher.song_matcher(
        minimum_match_ratio=70,
        artist="156/Silence",
        cleaned_artist="156silence",
        song_title="A Past Embrace",
        cleaned_song_title="a past embrace",
        search_results=search_results,
        expected_duration_ms=200000,
    )
    assert match is not None
    assert match["videoId"] == "studio"


# --- "clean" (censored) version handling ---

def test_song_matcher_prefers_explicit_over_clean():
    # Astronaut in the Ocean regression: the clean cut was grabbed over the original.
    search_results = [
        {"resultType": "song", "title": "Astronaut in the Ocean (Clean)", "videoId": "clean",
         "artists": [{"name": "Masked Wolf"}], "duration_seconds": 132},
        {"resultType": "song", "title": "Astronaut in the Ocean", "videoId": "explicit",
         "artists": [{"name": "Masked Wolf"}], "duration_seconds": 132},
    ]
    match = _matcher.song_matcher(
        minimum_match_ratio=70, artist="Masked Wolf", cleaned_artist="masked wolf",
        song_title="Astronaut in the Ocean", cleaned_song_title="astronaut in the ocean",
        search_results=search_results, expected_duration_ms=132000,
    )
    assert match is not None
    assert match["videoId"] == "explicit"


def test_song_matcher_rejects_clean_when_explicit_requested_and_only_clean_present():
    search_results = [
        {"resultType": "song", "title": "Astronaut in the Ocean (Clean Version)", "videoId": "clean",
         "artists": [{"name": "Masked Wolf"}], "duration_seconds": 132},
    ]
    match = _matcher.song_matcher(
        minimum_match_ratio=70, artist="Masked Wolf", cleaned_artist="masked wolf",
        song_title="Astronaut in the Ocean", cleaned_song_title="astronaut in the ocean",
        search_results=search_results, expected_duration_ms=132000,
    )
    assert match is None


def test_song_matcher_accepts_clean_when_request_wants_clean():
    search_results = [
        {"resultType": "song", "title": "Astronaut in the Ocean (Clean)", "videoId": "clean",
         "artists": [{"name": "Masked Wolf"}], "duration_seconds": 132},
    ]
    match = _matcher.song_matcher(
        minimum_match_ratio=70, artist="Masked Wolf", cleaned_artist="masked wolf",
        song_title="Astronaut in the Ocean (Clean)", cleaned_song_title="astronaut in the ocean clean",
        search_results=search_results, expected_duration_ms=132000,
    )
    assert match is not None
    assert match["videoId"] == "clean"


def test_clean_marker_does_not_misfire_on_songs_named_clean():
    # "clean" as part of a real title must NOT be treated as the censored-version marker.
    assert not _matcher._version_mismatch("Come Clean", "Come Clean")
    assert not _matcher._version_mismatch("Mr. Clean", "Mr. Clean")
    assert not _matcher._version_mismatch("Clean", "Clean")
    # but the qualifier form is caught, asymmetrically
    assert _matcher._version_mismatch("Astronaut in the Ocean", "Astronaut in the Ocean (Clean)")
    assert _matcher._version_mismatch("Song", "Song [Clean]")
    assert not _matcher._version_mismatch("Song (Clean)", "Song (Clean)")


def test_song_matcher_accepts_instrumental_when_requested():
    # If Lidarr's track IS the instrumental, the marker is in the request too — allow it.
    search_results = [
        {
            "resultType": "song",
            "title": "A Past Embrace (Instrumental)",
            "videoId": "instr",
            "artists": [{"name": "156/Silence"}],
            "duration_seconds": 200,
        },
    ]
    match = _matcher.song_matcher(
        minimum_match_ratio=70,
        artist="156/Silence",
        cleaned_artist="156silence",
        song_title="A Past Embrace (Instrumental)",
        cleaned_song_title="a past embrace instrumental",
        search_results=search_results,
        expected_duration_ms=200000,
    )
    assert match is not None
    assert match["videoId"] == "instr"


def test_song_matcher_rejects_vocal_when_instrumental_requested():
    # Seen: "FIRE (INSTRUMENTAL)" -> "Fire". Symmetric gate: don't grab the
    # plain vocal when Lidarr's track explicitly wants the instrumental.
    search_results = [
        {
            "resultType": "song",
            "title": "Fire",
            "videoId": "vocal",
            "artists": [{"name": "Some Artist"}],
            "duration_seconds": 180,
        },
    ]
    match = _matcher.song_matcher(
        minimum_match_ratio=70,
        artist="Some Artist",
        cleaned_artist="some artist",
        song_title="FIRE (INSTRUMENTAL)",
        cleaned_song_title="fire instrumental",
        search_results=search_results,
        expected_duration_ms=180000,
    )
    assert match is None


@pytest.mark.parametrize("song_title, bad_title", [
    ("The Embassy Waltz", "The Embassy Waltz (Instrumental)"),        # seen
    ("I'm the Man", "I'm The Man (Instrumental)"),                     # seen (Anthrax)
    ("1 Thing (album version)", "1 Thing (Instrumental)"),            # seen (Amerie)
    ("Changes (instrumental)", "Keep Ya Head Up"),                    # seen: wrong song + version
    ("Trapped (instrumental mix)", "My Block (Nitty Remix)"),        # seen: wrong song + version
])
def test_song_matcher_seen_bad_matches_now_rejected(song_title, bad_title):
    # Each of these was actually downloaded from a YTMusic "song" result.
    # The only candidate is the wrong one, so the desired outcome is None
    # (no download) rather than the bad grab.
    search_results = [
        {
            "resultType": "song",
            "title": bad_title,
            "videoId": "bad",
            "artists": [{"name": "Requested Artist"}],
            "duration_seconds": 200,
        },
    ]
    match = _matcher.song_matcher(
        minimum_match_ratio=85,
        artist="Requested Artist",
        cleaned_artist="requested artist",
        song_title=song_title,
        cleaned_song_title=song_title.lower(),
        search_results=search_results,
        expected_duration_ms=200000,
    )
    assert match is None


def test_song_matcher_yt_rejects_karaoke_when_not_requested():
    search_results = [
        {"title": "Good Enough (Originally Performed by Anita Baker) (Karaoke Version)", "link": "https://kar"},
    ]
    match = _matcher.song_matcher_yt(
        minimum_match_ratio=50,
        artist="Anita Baker",
        query_text="Anita Baker - Good Enough",
        search_results=search_results,
    )
    assert match is None


# --- song_matcher_yt: artist may be in the uploader/channel, not the title ---

def test_song_matcher_yt_accepts_artist_in_uploader_channel():
    search_results = [
        {"title": "Heat Waves", "uploader": "Glass Animals - Topic", "link": "https://topic"},
    ]
    match = _matcher.song_matcher_yt(
        minimum_match_ratio=50,
        artist="Glass Animals",
        query_text="Glass Animals - Heat Waves",
        search_results=search_results,
    )
    assert match is not None
    assert match["link"] == "https://topic"


def test_song_matcher_yt_accepts_artist_in_dict_channel():
    # YTS returns channel as a dict {"name": ...}
    search_results = [
        {"title": "Heat Waves", "channel": {"name": "Glass Animals"}, "link": "https://yts"},
    ]
    match = _matcher.song_matcher_yt(
        minimum_match_ratio=50,
        artist="Glass Animals",
        query_text="Glass Animals - Heat Waves",
        search_results=search_results,
    )
    assert match is not None
    assert match["link"] == "https://yts"


# --- channel-as-artist fallback (Diggy Diggy Hole regression) ---

def test_song_matcher_yt_channel_fallback_recovers_artist_in_channel():
    # Real case: the Yogscast original is titled "♪ Diggy Diggy Hole" on channel
    # "The Yogscast"; the query's artist prefix has nothing to match in the title, so
    # the normal pass under-scores it. The channel fallback (high threshold) recovers it.
    search_results = [
        {"title": "♪ Diggy Diggy Hole", "link": "https://correct", "duration": "4:09",
         "channel": {"name": "The Yogscast"}},
        {"title": "Diggy Diggy Hole", "link": "https://cover", "duration": "5:16",
         "channel": {"name": "Wind Rose"}},
    ]
    match = _matcher.song_matcher_yt(
        minimum_match_ratio=85, artist="The Yogscast",
        query_text="The Yogscast - Diggy Diggy Hole", search_results=search_results,
        expected_duration_ms=246000, duration_tolerance_seconds=15,
    )
    assert match is not None
    assert match["link"] == "https://correct"


def test_song_matcher_yt_channel_fallback_still_requires_title_match():
    # A different song on the right channel must NOT be accepted just because the
    # channel matches the artist — the title still has to match.
    search_results = [
        {"title": "Some Other Yogscast Song", "link": "https://wrong", "duration": "4:00",
         "channel": {"name": "The Yogscast"}},
    ]
    match = _matcher.song_matcher_yt(
        minimum_match_ratio=85, artist="The Yogscast",
        query_text="The Yogscast - Diggy Diggy Hole", search_results=search_results,
        expected_duration_ms=246000, duration_tolerance_seconds=15,
    )
    assert match is None


def test_song_matcher_yt_channel_fallback_skips_unrelated_channel():
    # Channel doesn't match the artist -> fallback must not credit it.
    search_results = [
        {"title": "Diggy Diggy Hole", "link": "https://cover", "duration": "4:06",
         "channel": {"name": "Megaraptor"}},
    ]
    match = _matcher.song_matcher_yt(
        minimum_match_ratio=85, artist="The Yogscast",
        query_text="The Yogscast - Diggy Diggy Hole", search_results=search_results,
        expected_duration_ms=246000, duration_tolerance_seconds=15,
    )
    assert match is None


def test_song_matcher_breaks_early_on_perfect_score(monkeypatch):
    calls = []

    def fake_ratio(left, right):
        calls.append((left, right))
        return 100

    monkeypatch.setattr(_matcher.fuzz, "ratio", fake_ratio)
    search_results = [
        {
            "resultType": "song",
            "title": "Track One",
            "videoId": "first",
            "artists": [{"name": "Target Artist"}],
        },
        {
            "resultType": "song",
            "title": "Track Two",
            "videoId": "second",
            "artists": [{"name": "Target Artist"}],
        },
    ]

    match = _matcher.song_matcher(
        minimum_match_ratio=50,
        artist="Target Artist",
        cleaned_artist="target artist",
        song_title="Track One",
        cleaned_song_title="track one",
        search_results=search_results,
    )

    assert match is not None
    assert match["videoId"] == "first"
    assert len(calls) == 4


def test_song_matcher_trace_records_duration_and_version_rejections():
    trace = []
    match = _matcher.song_matcher(
        minimum_match_ratio=70,
        artist="Artist",
        cleaned_artist="artist",
        song_title="Target Song",
        cleaned_song_title="target song",
        search_results=[
            {"resultType": "song", "title": "Target Song (Instrumental)", "videoId": "version", "artists": [{"name": "Artist"}], "duration_seconds": 200},
            {"resultType": "song", "title": "Target Song", "videoId": "duration", "artists": [{"name": "Artist"}], "duration_seconds": 400},
        ],
        expected_duration_ms=200000,
        trace=trace,
    )

    assert match is None
    assert [item["rejected_by"] for item in trace] == ["version_gate", "duration_gate"]
    assert trace[0]["candidate_url"] == "https://www.youtube.com/watch?v=version"


def test_song_matcher_yt_trace_records_artist_and_threshold_rejections():
    trace = []
    match = _matcher.song_matcher_yt(
        minimum_match_ratio=95,
        artist="Target Artist",
        query_text="Target Artist - Target Song",
        search_results=[
            {"title": "Target Song", "link": "https://example.test/no-artist"},
            {"title": "Target Artist - Different Completely Unrelated Recording", "link": "https://example.test/weak"},
        ],
        trace=trace,
    )

    assert match is None
    assert [item["rejected_by"] for item in trace] == ["artist_gate", "below_threshold"]


def test_song_matcher_trace_marks_the_selected_candidate_accepted():
    trace = []
    match = _matcher.song_matcher(
        minimum_match_ratio=70,
        artist="Artist",
        cleaned_artist="artist",
        song_title="Target Song",
        cleaned_song_title="target song",
        search_results=[
            {"resultType": "song", "title": "Target Song", "videoId": "accepted", "artists": [{"name": "Artist"}], "duration_seconds": 200},
        ],
        expected_duration_ms=200000,
        trace=trace,
    )

    assert match is not None
    assert trace == [{
        "source": "ytmusic",
        "candidate_title": "Target Song",
        "candidate_url": "https://www.youtube.com/watch?v=accepted",
        "candidate_duration_s": 200,
        "score": 100.0,
        "rejected_by": "accepted",
    }]
