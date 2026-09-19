import threading
import time
from unittest.mock import Mock

import pytest

import _matcher
import link_search
from fd_governor import FdGovernor
from link_search import LinkSearcher
from store import Store


def build_searcher():
    config = Mock()
    config.minimum_match_ratio = 80
    config.extended_duration_tolerance_seconds = 30
    config.fallback_to_top_result = False
    config.secondary_search = "YTS"
    config.thread_limit = 1
    store = Mock()
    store.get_override.return_value = None
    fd = FdGovernor(Mock())
    fd.fd_limit = None
    return LinkSearcher(config, store, fd, threading.Event(), Mock(), on_status_change=Mock())


def _make_track(n):
    return {
        "artist": "Artist",
        "track_title": f"Track {n}",
        "track_number": n,
        "absolute_track_number": n,
        "track_id": n,
        "link": f"https://example.com/{n}",
        "title_of_link": f"Track {n}",
    }


def _make_req_album(tracks):
    return {
        "artist": "Artist",
        "album_name": "Album",
        "artist_path": "/music/Artist",
        "album_folder": "Album (2024)",
        "track_count": len(tracks),
        "missing_count": len(tracks),
        "missing_tracks": tracks,
        "status": "",
    }


def test_get_song_links_secondary_uses_yt_search_fallback(monkeypatch):
    searcher = build_searcher()

    class FakeYTMusic:
        def search(self, query, filter, limit):
            return []

    monkeypatch.setattr(
        searcher,
        "_yt_search",
        lambda query_text: [{"title": "Artist - Track One", "link": "https://example.com/v"}],
    )

    req_album = {
        "artist": "Artist",
        "album_name": "Album",
        "missing_tracks": [
            {"artist": "Artist", "track_title": "Track One", "link": "", "title_of_link": "", "duration_ms": 0},
        ],
    }

    searcher._get_song_links_secondary(req_album, artist="Artist", cleaned_artist="artist", ytmusic=FakeYTMusic())

    assert req_album["missing_tracks"][0]["link"] == "https://example.com/v"
    assert req_album["missing_tracks"][0]["title_of_link"] == "Artist - Track One"


def test_find_links_closes_ytmusic_client(monkeypatch):
    """find_links must close the single shared YTMusic session it creates."""
    searcher = build_searcher()

    close_called = []

    class FakeSession:
        def close(self):
            close_called.append(1)

    class FakeYTMusic:
        _session = FakeSession()

        def search(self, query, filter, limit):
            return []

    monkeypatch.setattr(link_search, "YTMusic", FakeYTMusic)
    monkeypatch.setattr(searcher, "_yt_search", lambda query_text: [])

    req_album = {
        "artist": "Artist",
        "album_name": "Album",
        "track_count": 1,
        "missing_count": 1,
        "missing_tracks": [
            {"artist": "Artist", "track_title": "Track One", "link": "", "title_of_link": ""},
        ],
        "status": "",
    }

    searcher.find_links(req_album)

    assert len(close_called) == 1


def test_find_links_does_not_retry_secondary_after_emfile(monkeypatch):
    searcher = build_searcher()


    class FailingYTMusic:
        def search(self, query, filter, limit):
            raise OSError(24, "No file descriptors available")

    monkeypatch.setattr(link_search, "YTMusic", FailingYTMusic)
    secondary_search_mock = Mock()
    monkeypatch.setattr(searcher, "_get_song_links_secondary", secondary_search_mock)

    req_album = {
        "artist": "Andy Grammer",
        "album_name": "The Art of Joy",
        "track_count": 2,
        "missing_count": 1,
        "missing_tracks": [
            {"artist": "Andy Grammer", "track_title": "The Wrong Party", "link": "", "title_of_link": ""},
        ],
    }

    searcher.find_links(req_album)

    secondary_search_mock.assert_not_called()


def test_close_ytmusic_client_closes_underlying_session():
    """_close_ytmusic_client must close the requests.Session inside _session."""
    searcher = build_searcher()

    closed = []

    class FakeSession:
        def close(self):
            closed.append(1)

    class FakeYTMusic:
        _session = FakeSession()

    searcher._close_ytmusic_client(FakeYTMusic())
    assert len(closed) == 1


def test_close_ytmusic_client_handles_none():
    """_close_ytmusic_client must not raise when passed None."""
    searcher = build_searcher()
    searcher._close_ytmusic_client(None)  # should not raise


def test_ytmusic_session_closed_after_album_search(monkeypatch):
    """The YTMusic session must be closed by find_links after album search."""
    searcher = build_searcher()

    closed = []

    class FakeYTMusic:
        class _session:
            @staticmethod
            def close():
                closed.append(1)

        def search(self, query, filter, limit):
            return []

    monkeypatch.setattr(link_search, "YTMusic", FakeYTMusic)

    req_album = {
        "artist": "X", "album_name": "Y", "track_count": 1, "missing_count": 1,
        "missing_tracks": [{"link": "", "track_title": "T", "artist": "X", "title_of_link": ""}],
        "status": "",
    }
    searcher.find_links(req_album)

    assert len(closed) == 1


def test_saved_override_is_applied_before_album_matching():
    searcher = build_searcher()
    searcher.store.get_override.return_value = {"forced_url": "https://youtube.test/watch?v=forced"}
    album = {"missing_tracks": [{"track_id": 99, "link": "", "title_of_link": ""}]}

    searcher._apply_saved_overrides(album)

    assert album["missing_tracks"][0]["link"] == "https://youtube.test/watch?v=forced"
    assert album["missing_tracks"][0]["title_of_link"] == "Manual override"


def test_record_link_results_persists_no_match_trace(tmp_path):
    from store import Store

    searcher = build_searcher()
    searcher.store = Store(tmp_path / "lidatube.db")
    session_id = searcher.store.start_session(requested_count=1)
    album = {
        "artist": "Artist",
        "album_name": "Album",
        "missing_tracks": [{
            "artist": "Artist",
            "track_title": "Missing Track",
            "track_number": 1,
            "track_id": 42,
            "duration_ms": 180000,
            "link": "",
            "title_of_link": "",
            "_match_trace": [{
                "source": "ytmusic",
                "candidate_title": "Wrong Version",
                "candidate_url": "https://example.test/wrong",
                "candidate_duration_s": 180,
                "score": 91,
                "rejected_by": "version_gate",
            }],
        }],
    }

    searcher._record_link_results(album, session_id)

    tracks = searcher.store.get_session_tracks(session_id)
    assert tracks[0]["outcome"] == "no_match"
    assert searcher.store.get_evaluations(tracks[0]["id"])[0]["rejected_by"] == "version_gate"
    assert "_match_trace" not in album["missing_tracks"][0]
    searcher.store.close()


def test_apply_album_track_links_returns_true_when_stop_is_set():
    searcher = build_searcher()
    searcher.stop_event.set()

    req_album = {
        "missing_tracks": [
            {"track_title": "Track A", "link": "", "title_of_link": ""},
        ]
    }
    album_details = {"tracks": [{"title": "Track A", "videoId": "abc"}]}

    should_stop = searcher._apply_album_track_links(req_album, album_details)

    assert should_stop is True
    assert req_album["missing_tracks"][0]["link"] == ""


def test_get_album_links_falls_back_to_top_result(monkeypatch):
    searcher = build_searcher()
    searcher.config.fallback_to_top_result = True
    monkeypatch.setattr(_matcher, "album_matcher", lambda *args, **kwargs: None)

    apply_mock = Mock(return_value=False)
    monkeypatch.setattr(searcher, "_apply_album_track_links", apply_mock)

    class FakeYTMusic:
        def search(self, query, filter, limit):
            return [{"title": "Fallback Album", "browseId": "album-1"}]

        def get_album(self, browse_id):
            assert browse_id == "album-1"
            return {"tracks": [{"title": "Track A", "videoId": "vid-1"}]}

    req_album = {"artist": "A", "album_name": "B", "status": "", "missing_tracks": []}

    searcher._get_album_links(req_album, "A", "B", "a", "b", "A - B", FakeYTMusic())

    assert req_album["status"] == "Album Found"
    apply_mock.assert_called_once_with(req_album, {"tracks": [{"title": "Track A", "videoId": "vid-1"}]})


def test_get_song_links_uses_fallback_to_top_result(monkeypatch):
    searcher = build_searcher()
    searcher.config.fallback_to_top_result = True
    monkeypatch.setattr(_matcher, "song_matcher", lambda *args, **kwargs: None)

    class FakeYTMusic:
        def search(self, query, filter, limit):
            return [{"title": "Fallback Song", "videoId": "vid-123"}]

    req_album = {
        "artist": "Artist",
        "album_name": "Album",
        "missing_tracks": [
            {"artist": "Artist", "track_title": "Track One", "link": "", "title_of_link": "", "duration_ms": 0},
        ],
    }

    searcher._get_song_links(req_album, "Artist", "artist", FakeYTMusic())

    track = req_album["missing_tracks"][0]
    assert track["link"] == "https://www.youtube.com/watch?v=vid-123"
    assert track["title_of_link"] == "Fallback Song"


def test_get_song_links_secondary_ytdlp_mode_uses_webpage_url(monkeypatch):
    searcher = build_searcher()
    searcher.config.secondary_search = "YTDLP"
    monkeypatch.setattr(_matcher, "song_matcher", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        _matcher,
        "song_matcher_yt",
        lambda *args, **kwargs: {"title": "YT Match", "webpage_url": "https://yt.example/watch?v=1", "link": "unused"},
    )
    monkeypatch.setattr(searcher, "_yt_search", lambda _: [{"title": "YT Match", "webpage_url": "https://yt.example/watch?v=1"}])

    class FakeYTMusic:
        def search(self, query, filter, limit):
            return []

    req_album = {
        "artist": "Artist",
        "album_name": "Album",
        "missing_tracks": [
            {"artist": "Artist", "track_title": "Track One", "link": "", "title_of_link": "", "duration_ms": 0},
        ],
    }

    searcher._get_song_links_secondary(req_album, "Artist", "artist", FakeYTMusic())

    track = req_album["missing_tracks"][0]
    assert track["link"] == "https://yt.example/watch?v=1"
    assert track["title_of_link"] == "YT Match"


def test_yt_search_returns_empty_list_for_unknown_secondary_mode():
    searcher = build_searcher()
    searcher.config.secondary_search = "UNKNOWN"

    assert searcher._yt_search("artist - song") == []


def test_yt_search_ytdlp_returns_empty_when_stop_requested(monkeypatch):
    searcher = build_searcher()
    searcher.config.secondary_search = "YTDLP"
    searcher.stop_event.set()

    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def extract_info(self, query_text, download=False):
            return {"entries": [{"webpage_url": "https://yt.example/watch?v=1"}]}

    monkeypatch.setattr(link_search.yt_dlp, "YoutubeDL", FakeYDL)

    assert searcher._yt_search("artist - song") == []


def test_find_links_returns_without_api_call_if_stop_set_before_semaphore(monkeypatch):
    """find_links should exit without making API calls when stop event is set and semaphore is unavailable."""
    searcher = build_searcher()
    # Make semaphore impossible to acquire
    searcher._ytmusic_semaphore = threading.Semaphore(0)
    searcher.stop_event.set()

    ytmusic_created = []

    class FakeYTMusic:
        def __init__(self):
            ytmusic_created.append(1)

    monkeypatch.setattr(link_search, "YTMusic", FakeYTMusic)

    req_album = _make_req_album([_make_track(1)])
    searcher.find_links(req_album)

    assert len(ytmusic_created) == 0, "YTMusic should not be created when stop is set"


def test_find_links_exits_when_stop_set_while_waiting_for_semaphore(monkeypatch):
    """find_links should exit when stop event is set while waiting for a full semaphore."""
    searcher = build_searcher()
    # Semaphore has 0 permits — blocks immediately
    searcher._ytmusic_semaphore = threading.Semaphore(0)

    ytmusic_created = []

    class FakeYTMusic:
        def __init__(self):
            ytmusic_created.append(1)

    monkeypatch.setattr(link_search, "YTMusic", FakeYTMusic)

    def set_stop_after_delay():
        time.sleep(0.3)
        searcher.stop_event.set()

    t = threading.Thread(target=set_stop_after_delay)
    t.start()

    req_album = _make_req_album([_make_track(1)])
    searcher.find_links(req_album)
    t.join()

    assert len(ytmusic_created) == 0, "YTMusic should not be created when stop is set during semaphore wait"


def _network_retry_album():
    return {
        "artist": "Nanci Griffith",
        "album_name": "Blue Roses From the Moons",
        "track_count": 2,
        "missing_count": 1,
        "missing_tracks": [{
            "artist": "Nanci Griffith", "track_title": "Wouldn't That Be Fine", "track_number": 1,
            "track_id": 7, "duration_ms": 200000, "link": "", "title_of_link": "",
        }],
        "status": "",
    }


def test_find_links_retries_search_after_network_error(monkeypatch):
    import requests

    searcher = build_searcher()
    searcher._SEARCH_RETRY_DELAYS = (0, 0, 0)
    searcher.config.duration_tolerance_seconds = 15
    calls = []

    class FlakyYTMusic:
        def search(self, query, filter, limit):
            calls.append(query)
            if len(calls) <= 2:
                raise requests.exceptions.ConnectionError("Failed to resolve 'music.youtube.com'")
            return [{"resultType": "song", "title": "Wouldn't That Be Fine", "videoId": "vid-1",
                     "artists": [{"name": "Nanci Griffith"}], "duration_seconds": 200}]

    monkeypatch.setattr(link_search, "YTMusic", FlakyYTMusic)
    monkeypatch.setattr(searcher, "_yt_search", lambda query_text: [])
    album = _network_retry_album()

    searcher.find_links(album)

    assert len(calls) == 3
    assert album["missing_tracks"][0]["link"] == "https://www.youtube.com/watch?v=vid-1"


def test_find_links_records_error_outcome_when_network_never_recovers(monkeypatch, tmp_path):
    import requests

    searcher = build_searcher()
    searcher._SEARCH_RETRY_DELAYS = (0, 0)
    searcher.store = Store(tmp_path / "lidatube.db")
    session_id = searcher.store.start_session(requested_count=1)
    calls = []

    class OfflineYTMusic:
        def search(self, query, filter, limit):
            calls.append(query)
            raise requests.exceptions.ConnectionError("Failed to resolve 'music.youtube.com'")

    monkeypatch.setattr(link_search, "YTMusic", OfflineYTMusic)

    searcher.find_links(_network_retry_album(), session_id)

    tracks = searcher.store.get_session_tracks(session_id)
    assert len(calls) == 3
    assert [track["outcome"] for track in tracks] == ["error"]
    searcher.store.close()


def test_find_links_does_not_retry_non_network_errors(monkeypatch):
    searcher = build_searcher()
    searcher._SEARCH_RETRY_DELAYS = (0, 0, 0)
    calls = []

    class BrokenYTMusic:
        def search(self, query, filter, limit):
            calls.append(query)
            raise ValueError("unexpected response shape")

    monkeypatch.setattr(link_search, "YTMusic", BrokenYTMusic)

    searcher.find_links(_network_retry_album())

    assert len(calls) == 1


class _BrokenVideosSearch:
    def __init__(self, query, limit):
        raise TypeError('can only concatenate str (not "NoneType") to str')


def _fake_ytdlp(entries, seen_opts):
    class FakeYDL:
        def __init__(self, opts):
            seen_opts.append(opts)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, query, download=False):
            if isinstance(entries, Exception):
                raise entries
            return {"entries": entries}

    return FakeYDL


def test_yt_search_falls_back_to_flat_ytdlp_search_when_yts_fails(monkeypatch):
    searcher = build_searcher()
    searcher.config.secondary_search = "YTS"
    seen_opts = []
    monkeypatch.setattr(link_search.youtubesearchpython, "VideosSearch", _BrokenVideosSearch)
    monkeypatch.setattr(link_search.yt_dlp, "YoutubeDL", _fake_ytdlp([
        {"title": "Larry Coryell - Larry's Boogie", "url": "https://www.youtube.com/watch?v=abc", "duration": 213},
        {"title": "Larry's Boogie (Live)", "id": "xyz", "duration": 250},
    ], seen_opts))

    results = searcher._yt_search("Larry Coryell - Larry's Boogie")

    assert [item["link"] for item in results] == [
        "https://www.youtube.com/watch?v=abc",
        "https://www.youtube.com/watch?v=xyz",
    ]
    assert seen_opts[0]["extract_flat"]


def test_yt_search_propagates_error_when_yts_and_ytdlp_fallback_both_fail(monkeypatch):
    searcher = build_searcher()
    searcher.config.secondary_search = "YTS"
    monkeypatch.setattr(link_search.youtubesearchpython, "VideosSearch", _BrokenVideosSearch)
    monkeypatch.setattr(link_search.yt_dlp, "YoutubeDL", _fake_ytdlp(RuntimeError("yt-dlp offline"), []))

    with pytest.raises(RuntimeError, match="yt-dlp offline"):
        searcher._yt_search("Artist - Song")


def test_secondary_search_links_track_from_ytdlp_fallback_when_yts_fails(monkeypatch):
    searcher = build_searcher()
    searcher.config.secondary_search = "YTS"
    searcher.config.duration_tolerance_seconds = 15
    monkeypatch.setattr(link_search.youtubesearchpython, "VideosSearch", _BrokenVideosSearch)
    monkeypatch.setattr(link_search.yt_dlp, "YoutubeDL", _fake_ytdlp([
        {"title": "Larry Coryell - Larry's Boogie", "url": "https://www.youtube.com/watch?v=abc", "duration": 213},
    ], []))

    class EmptyYTMusic:
        def search(self, query, filter, limit):
            return []

    req_album = {
        "artist": "Larry Coryell",
        "album_name": "The Lion and the Ram",
        "missing_tracks": [
            {"artist": "Larry Coryell", "track_title": "Larry's Boogie", "link": "", "title_of_link": "", "duration_ms": 211000},
        ],
    }

    searcher._get_song_links_secondary(req_album, "Larry Coryell", "larry coryell", EmptyYTMusic())

    assert req_album["missing_tracks"][0]["link"] == "https://www.youtube.com/watch?v=abc"


def test_find_links_stops_retrying_when_stop_requested_during_backoff(monkeypatch, tmp_path):
    import requests

    searcher = build_searcher()
    searcher._SEARCH_RETRY_DELAYS = (30, 30)
    searcher.store = Store(tmp_path / "lidatube.db")
    session_id = searcher.store.start_session(requested_count=1)
    calls = []

    class OfflineYTMusic:
        def search(self, query, filter, limit):
            calls.append(query)
            searcher.stop_event.set()
            raise requests.exceptions.ConnectionError("Failed to resolve 'music.youtube.com'")

    monkeypatch.setattr(link_search, "YTMusic", OfflineYTMusic)
    started = time.monotonic()

    searcher.find_links(_network_retry_album(), session_id)

    assert len(calls) == 1
    assert time.monotonic() - started < 5
    assert [track["outcome"] for track in searcher.store.get_session_tracks(session_id)] == ["error"]
    searcher.store.close()


@pytest.mark.parametrize("mode", ["YTS", "YTDLP"])
def test_yt_search_preserves_successful_empty_results(monkeypatch, mode):
    searcher = build_searcher()
    searcher.config.secondary_search = mode
    monkeypatch.setattr(link_search.youtubesearchpython, "VideosSearch", _BrokenVideosSearch)
    monkeypatch.setattr(link_search.yt_dlp, "YoutubeDL", _fake_ytdlp([], []))
    assert searcher._yt_search("Artist - Song") == []


@pytest.mark.parametrize("mode", ["YTS", "YTDLP"])
@pytest.mark.parametrize("recovers", [False, True])
def test_find_links_retries_youtube_outages_and_persists_outcome(
    monkeypatch, tmp_path, mode, recovers,
):
    import requests

    searcher = build_searcher()
    searcher.config.secondary_search = mode
    searcher.config.duration_tolerance_seconds = 15
    searcher._SEARCH_RETRY_DELAYS = (0, 0)
    searcher.store = Store(tmp_path / "youtube-retry.db")
    session_id = searcher.store.start_session(requested_count=1)
    monkeypatch.setattr(link_search, "YTMusic", lambda: Mock(search=Mock(return_value=[])))
    monkeypatch.setattr(link_search.youtubesearchpython, "VideosSearch", _BrokenVideosSearch)
    calls = []

    class FlakyYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, query, download=False):
            calls.append(query)
            if not recovers or len(calls) == 1:
                raise requests.exceptions.ConnectionError("Failed to resolve youtube.com")
            return {"entries": [{
                "title": "Nanci Griffith - Wouldn't That Be Fine", "duration": 200,
                "webpage_url": "https://www.youtube.com/watch?v=recovered",
            }]}

    monkeypatch.setattr(link_search.yt_dlp, "YoutubeDL", FlakyYDL)
    try:
        searcher.find_links(_network_retry_album(), session_id)
        assert len(calls) == (2 if recovers else 3)
        tracks = searcher.store.get_session_tracks(session_id)
        assert [track["outcome"] for track in tracks] == ["matched" if recovers else "error"]
        if recovers:
            assert tracks[0]["link"] == "https://www.youtube.com/watch?v=recovered"
    finally:
        searcher.store.close()


def _live_album_request(**album_fields):
    album = {
        "artist": "Three Dog Night", "album_name": "Three Dog Night", "album_secondary_types": [],
        "missing_tracks": [{"artist": "Three Dog Night", "track_title": "One", "link": "", "title_of_link": "", "duration_ms": 180000}],
    }
    album.update(album_fields)
    return album


def test_song_search_passes_album_context_so_live_album_requests_prefer_live_recordings():
    searcher = build_searcher()
    searcher.config.duration_tolerance_seconds = 15

    class FakeYTMusic:
        def search(self, query, filter, limit):
            return [
                {"resultType": "song", "title": "One", "videoId": "studio", "artists": [{"name": "Three Dog Night"}], "duration_seconds": 180, "album": {"name": "Three Dog Night"}},
                {"resultType": "song", "title": "One (Live)", "videoId": "live", "artists": [{"name": "Three Dog Night"}], "duration_seconds": 178, "album": {"name": "Live in Concert"}},
            ]

    req_album = _live_album_request(album_name="Live in Concert")
    searcher._get_song_links(req_album, "Three Dog Night", "three dog night", FakeYTMusic())

    assert req_album["missing_tracks"][0]["link"] == "https://www.youtube.com/watch?v=live"


def test_youtube_fallback_passes_album_context_for_live_secondary_type(monkeypatch):
    searcher = build_searcher()
    searcher.config.duration_tolerance_seconds = 15

    class EmptyYTMusic:
        def search(self, query, filter, limit):
            return []

    monkeypatch.setattr(searcher, "_yt_search", lambda query_text: [
        {"title": "Three Dog Night - One (Live)", "link": "https://youtube.test/live", "duration": "3:00", "channel": {"name": "Three Dog Night"}},
    ])
    req_album = _live_album_request(album_name="Harmony Tour", album_secondary_types=["Live"])
    searcher._get_song_links_secondary(req_album, "Three Dog Night", "three dog night", EmptyYTMusic())

    assert req_album["missing_tracks"][0]["link"] == "https://youtube.test/live"


def test_song_searches_pass_extended_duration_tolerance_from_config(monkeypatch):
    searcher = build_searcher()
    searcher.config.duration_tolerance_seconds = 15
    searcher.config.extended_duration_tolerance_seconds = 30

    class FakeYTMusic:
        def search(self, query, filter, limit):
            return [{"resultType": "song", "title": "Can't Take My Eyes off You", "videoId": "original",
                     "artists": [{"name": "Frankie Valli"}], "duration_seconds": 204}]

    req_album = {
        "artist": "Frankie Valli", "album_name": "Can’t Take My Eyes Off You", "album_secondary_types": [],
        "missing_tracks": [{"artist": "Frankie Valli", "track_title": "Can’t Take My Eyes Off You", "link": "", "title_of_link": "", "duration_ms": 231000}],
    }
    searcher._get_song_links(req_album, "Frankie Valli", "frankie valli", FakeYTMusic())

    assert req_album["missing_tracks"][0]["link"] == "https://www.youtube.com/watch?v=original"


def test_song_searches_pass_album_genres_so_classical_skips_extended_duration_window():
    searcher = build_searcher()
    searcher.config.duration_tolerance_seconds = 15
    searcher.config.extended_duration_tolerance_seconds = 30

    class FakeYTMusic:
        def search(self, query, filter, limit):
            return [{"resultType": "song", "title": "Widmung", "videoId": "other-performance",
                     "artists": [{"name": "Robert Schumann"}], "duration_seconds": 242}]

    req_album = {
        "artist": "Robert Schumann", "album_name": "Piano Works", "album_secondary_types": [], "album_genres": "Classical",
        "missing_tracks": [{"artist": "Robert Schumann", "track_title": "Widmung", "link": "", "title_of_link": "", "duration_ms": 270000}],
    }
    searcher._get_song_links(req_album, "Robert Schumann", "robert schumann", FakeYTMusic())

    assert req_album["missing_tracks"][0]["link"] == ""


def test_song_searches_use_artist_genres_when_album_genres_are_missing():
    searcher = build_searcher()
    searcher.config.duration_tolerance_seconds = 15
    searcher.config.extended_duration_tolerance_seconds = 30

    class FakeYTMusic:
        def search(self, query, filter, limit):
            return [{"resultType": "song", "title": "Widmung", "videoId": "other-performance",
                     "artists": [{"name": "Robert Schumann"}], "duration_seconds": 242}]

    req_album = {
        "artist": "Robert Schumann", "album_name": "Piano Works", "album_secondary_types": [], "album_genres": "", "artist_genres": "Classical",
        "missing_tracks": [{"artist": "Robert Schumann", "track_title": "Widmung", "link": "", "title_of_link": "", "duration_ms": 270000}],
    }
    searcher._get_song_links(req_album, "Robert Schumann", "robert schumann", FakeYTMusic())

    assert req_album["missing_tracks"][0]["link"] == ""


def test_find_links_backs_off_and_retries_when_youtube_music_blocks(monkeypatch):
    import json

    searcher = build_searcher()
    searcher._SEARCH_RETRY_DELAYS = (0,)
    searcher._YOUTUBE_BLOCK_RETRY_DELAYS = (0, 0, 0)
    searcher.config.duration_tolerance_seconds = 15
    calls = []

    class BlockedThenOpenYTMusic:
        def search(self, query, filter, limit):
            calls.append(query)
            if len(calls) <= 2:
                raise json.JSONDecodeError("Expecting value", "", 0)
            return [{"resultType": "song", "title": "Wouldn't That Be Fine", "videoId": "vid-1",
                     "artists": [{"name": "Nanci Griffith"}], "duration_seconds": 200}]

    monkeypatch.setattr(link_search, "YTMusic", BlockedThenOpenYTMusic)
    monkeypatch.setattr(searcher, "_yt_search", lambda query_text: [])
    album = _network_retry_album()

    searcher.find_links(album)

    assert len(calls) == 3
    assert album["missing_tracks"][0]["link"] == "https://www.youtube.com/watch?v=vid-1"


def test_find_links_records_error_when_youtube_block_outlasts_the_schedule(monkeypatch, tmp_path):
    import json

    searcher = build_searcher()
    searcher._SEARCH_RETRY_DELAYS = (0,)
    searcher._YOUTUBE_BLOCK_RETRY_DELAYS = (0, 0)
    searcher.store = Store(tmp_path / "lidatube.db")
    session_id = searcher.store.start_session(requested_count=1)
    calls = []

    class BlockedYTMusic:
        def search(self, query, filter, limit):
            calls.append(query)
            raise json.JSONDecodeError("Expecting value", "", 0)

    monkeypatch.setattr(link_search, "YTMusic", BlockedYTMusic)

    searcher.find_links(_network_retry_album(), session_id)

    assert len(calls) == 3
    assert [track["outcome"] for track in searcher.store.get_session_tracks(session_id)] == ["error"]
    searcher.store.close()


def test_youtube_search_block_backs_off_like_youtube_music(monkeypatch):
    searcher = build_searcher()
    searcher._SEARCH_RETRY_DELAYS = (0,)
    searcher._YOUTUBE_BLOCK_RETRY_DELAYS = (0,)
    searcher.config.duration_tolerance_seconds = 15

    class EmptyYTMusic:
        def search(self, query, filter, limit):
            return []

    monkeypatch.setattr(link_search, "YTMusic", EmptyYTMusic)
    searches = []

    def blocked_then_found(query_text):
        searches.append(query_text)
        if len(searches) == 1:
            raise Exception(f'ERROR: query "{query_text}" page 1: Unable to download API page: HTTP Error 403: Forbidden')
        return [{"title": "Nanci Griffith - Wouldn't That Be Fine", "link": "https://youtube.test/found", "duration": "3:20"}]

    monkeypatch.setattr(searcher, "_yt_search", blocked_then_found)
    album = _network_retry_album()

    searcher.find_links(album)

    assert len(searches) == 2
    assert album["missing_tracks"][0]["link"] == "https://youtube.test/found"


def test_search_reports_status_changes_through_the_callback():
    searcher = build_searcher()

    class AlbumYTMusic:
        def search(self, query, filter, limit):
            return [{"title": "Album", "browseId": "album-1", "artists": [{"name": "Artist"}], "type": "Album"}]

        def get_album(self, browse_id):
            return {"tracks": [{"title": "Track 1", "videoId": "vid-1", "durationSeconds": 0}]}

    album = _make_req_album([dict(_make_track(1), link="", title_of_link="", duration_ms=0)])

    searcher._search_links(album, AlbumYTMusic())

    assert album["status"] == "All Tracks Found"
    assert album["missing_tracks"][0]["link"] == "https://www.youtube.com/watch?v=vid-1"
    searcher.on_status_change.assert_called_with()
