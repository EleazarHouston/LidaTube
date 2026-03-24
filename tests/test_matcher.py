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
        query_text="Target Artist - Track One",
        search_results=search_results,
    )

    assert match is not None
    assert match["link"] == "https://b"


def test_song_matcher_yt_returns_none_for_empty_results():
    assert _matcher.song_matcher_yt(50, "any query", []) is None


def test_song_matcher_yt_regression_uses_combined_rating_not_raw_title(monkeypatch):
    # Regression for a prior bug where best_match_rating tracked title_similarity.
    # In that case, a high raw-title score from an earlier item could block a later
    # item with a better combined score.
    query_text = "query"
    search_results = [
        {"title": "first", "link": "https://first"},
        {"title": "second", "link": "https://second"},
    ]

    monkeypatch.setattr(_matcher._general, "string_cleaner", lambda value: f"clean:{value}")
    monkeypatch.setattr(_matcher, "remove_song_keywords", lambda value: f"trim:{value}")

    score_map = {
        ("query", "first"): 99,
        ("clean:query", "clean:first"): 30,
        ("trim:clean:query", "trim:clean:first"): 30,
        ("query", "second"): 80,
        ("clean:query", "clean:second"): 90,
        ("trim:clean:query", "trim:clean:second"): 90,
    }

    def fake_ratio(left, right):
        return score_map[(left, right)]

    monkeypatch.setattr(_matcher.fuzz, "ratio", fake_ratio)

    match = _matcher.song_matcher_yt(
        minimum_match_ratio=50,
        query_text=query_text,
        search_results=search_results,
    )

    assert match is not None
    assert match["link"] == "https://second"
