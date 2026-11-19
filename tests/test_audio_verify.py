"""The audio verification gate: does a downloaded file's audio belong to the requested track?

The real-data tests read a local fixture of AcoustID lookups of hand-labelled library files,
pinning the gate's behaviour on the cases that motivated it: uploads titled as one song but
carrying another, and soundtrack or classical files whose titles differ only in formatting.
The fixture describes a private library, so it is not committed; without it those tests skip.
"""

import json
import os

import pytest

import audio_verify
from audio_verify import VerificationUnavailable, decide, same_title

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "acoustid_gate_cases.json")


@pytest.fixture(scope="module")
def gate_data():
    if not os.path.exists(FIXTURE):
        pytest.skip("local AcoustID fixture not present (tests/fixtures/acoustid_gate_cases.json)")
    with open(FIXTURE, encoding="utf-8") as handle:
        data = json.load(handle)
    works = {rec: set(ids) for rec, ids in data["works"].items()}

    def recording_info(recording_id):
        return recording_id, works.get(recording_id, set())

    verdicts = {}
    for case in data["cases"]:
        verdicts[case["track"]] = decide(case["title"], case["recording_id"], case["response"], recording_info,
                                         expected_artist=case["artist"])
    return data["cases"], verdicts


def _titles(cases, verdicts, label, rejected):
    return sorted(c["title"] for c in cases if c["label"] == label and (verdicts[c["track"]].status == "rejected") == rejected)


def test_rejects_the_known_wrong_songs(gate_data):
    cases, verdicts = gate_data
    # MusicBrainz links this Phantom Menace recording to the work "He Is the Chosen One",
    # the work AcoustID heard, so by the gate's evidence the file is the right recording.
    assert _titles(cases, verdicts, "wrong", rejected=False) == ["Qui-Gon's Mission / Obi-Wan's Warning"]
    assert len(_titles(cases, verdicts, "wrong", rejected=True)) == 16


def test_catches_a_mislabelled_upload_heard_by_a_single_source():
    # The Tammy Wynette case: one source, score 0.85, and a title sharing no word with the request.
    response = {"status": "ok", "results": [{"score": 0.847, "recordings": [
        {"id": "9c0b45c7-0000-0000-0000-000000000000", "title": "(Or) Is It Love", "sources": 1}]}]}
    verdict = decide("Soakin’ Wet", "0bd8fe74-0000-0000-0000-000000000000", response, lambda rec: (rec, set()))
    assert verdict.status == "rejected"
    assert verdict.top_titles == ["(Or) Is It Love"]


def test_accepts_formatting_noise_except_two_renamed_classical_pieces(gate_data):
    cases, verdicts = gate_data
    # "Kanon" (Pachelbel's Canon) and "Gayne Ballet: Adagio" are the same pieces under other
    # names, with no recording or work link in MusicBrainz; no title rule can bridge them.
    assert _titles(cases, verdicts, "noise", rejected=True) == [
        'Khachaturian: Gayane: Adagio (From "2001: A Space Odyssey")',
        'Pachelbel: Canon in D Major, P. 37, T. 337 (From "Ordinary People")',
    ]


def test_never_rejects_a_control_file(gate_data):
    cases, verdicts = gate_data
    controls = [c for c in cases if c["label"] == "control"]
    assert len(controls) == 389
    assert _titles(cases, verdicts, "control", rejected=True) == []


def test_unlabelled_rejections_are_genuinely_different_pieces(gate_data):
    cases, verdicts = gate_data
    assert _titles(cases, verdicts, "unlabelled", rejected=True) == [
        "Album d'un voyageur, S. 156: I. Impressions et poésies: 1. Lyon",
        "Be My Love",
        "Valse oubliée no. 4, S. 215 no. 4",
    ]


def test_no_results_or_only_low_scores_accept_as_no_match():
    assert decide("Song", "rec", {"status": "ok", "results": []}, None).status == "no_match"
    low = {"status": "ok", "results": [{"score": 0.5, "recordings": [{"id": "x", "title": "Other", "sources": 50}]}]}
    verdict = decide("Song", "rec", low, None)
    assert verdict.status == "no_match"
    assert verdict.accepted


def test_recording_id_match_needs_no_musicbrainz_lookup():
    response = {"status": "ok", "results": [{"score": 0.95, "recordings": [{"id": "rec", "title": "Different Title", "sources": 9}]}]}

    def unavailable(recording_id):
        raise AssertionError("MusicBrainz should not be asked")

    assert decide("Song", "rec", response, unavailable).status == "verified"


def test_merged_recording_id_counts_as_the_expected_recording():
    response = {"status": "ok", "results": [{"score": 0.95, "recordings": [{"id": "new-id", "title": "Other Name", "sources": 9}]}]}
    info = {"old-id": ("new-id", set()), "new-id": ("new-id", set())}
    assert decide("Song", "old-id", response, info.__getitem__).status == "verified"


def test_shared_work_accepts_a_differently_titled_recording():
    response = {"status": "ok", "results": [{"score": 0.95, "recordings": [{"id": "cand", "title": "Kanon", "sources": 139}]}]}
    info = {"exp": ("exp", {"work-canon"}), "cand": ("cand", {"work-canon"})}
    assert decide("Canon in D", "exp", response, info.__getitem__).status == "verified"


def test_musicbrainz_outage_is_raised_only_when_work_evidence_is_needed():
    response = {"status": "ok", "results": [{"score": 0.95, "recordings": [{"id": "cand", "title": "Other Song", "sources": 30}]}]}

    def down(recording_id):
        raise VerificationUnavailable("MusicBrainz unreachable")

    with pytest.raises(VerificationUnavailable):
        decide("Song", "exp", response, down)
    same_artist = {"status": "ok", "results": [{"score": 0.95, "recordings": [
        {"id": "cand", "title": "Song", "sources": 30, "artists": [{"id": "a1", "name": "Artist"}]}]}]}
    assert decide("Song", "exp", same_artist, down, expected_artist="Artist").status == "verified"
    title_alone = {"status": "ok", "results": [{"score": 0.95, "recordings": [{"id": "cand", "title": "Song", "sources": 30}]}]}
    with pytest.raises(VerificationUnavailable):
        decide("Song", "exp", title_alone, down)


def test_weak_unrelated_evidence_is_kept_when_titles_share_words():
    # One source naming a related title is not enough to throw a file away.
    response = {"status": "ok", "results": [{"score": 1.0, "recordings": [
        {"id": "cand", "title": "Children’s Album, op. 39: No. 24. In Church", "sources": 1}]}]}
    verdict = decide("Children’s Album, op. 39: No. 24, Chorale", "exp", response, lambda rec: (rec, set()))
    assert verdict.status == "weak"
    assert verdict.accepted


def test_title_that_differs_only_by_script_is_accepted_but_flagged_when_the_artist_matches():
    response = {"status": "ok", "results": [{"score": 0.95, "recordings": [
        {"id": "cand", "title": "Москва", "sources": 20, "artists": ["Чайф"]}]}]}
    flagged = decide("Moskva", "exp", response, lambda rec: (rec, set()), expected_artist="Chaif")
    assert flagged.status == "flagged"
    rejected = decide("Moskva", "exp", response, lambda rec: (rec, set()), expected_artist="Someone Else")
    assert rejected.status == "rejected"


def test_an_artist_match_alone_never_accepts():
    response = {"status": "ok", "results": [{"score": 0.99, "recordings": [
        {"id": "cand", "title": "Marijuana, Malt Liquor", "sources": 40, "artists": ["Afroman"]}]}]}
    assert decide("Thunderfucc", "exp", response, lambda rec: (rec, set()), expected_artist="Afroman").status == "rejected"


@pytest.mark.parametrize("expected, got", [
    ('Theme From "Jaws"', "Jaws Theme"),
    ('Main Theme From "Schindler\'s List"', "Schindler's List: Main Theme"),
    ("Steiner: Casablanca Suite", "Casablanca Suite"),
    ("Symphony No. 9 in D Major; II. Im Tempo eines gemächlichen Ländlers. Etwas täppisch und sehr derb",
     "Symphony no. 9: II. Im Tempo eines gemächlichen Ländlers"),
    ("Song (feat. Someone)", "Song"),
    ("Song (Remastered 2009)", "Song"),
    ("Take the “A” Train (1952 version)", 'Take the "A" Train'),
    ("Café", "Cafe"),
])
def test_same_title_ignores_formatting(expected, got):
    assert same_title(expected, got)


@pytest.mark.parametrize("expected, got", [
    ("Soakin’ Wet", "(Or) Is It Love"),
    ("Be My Love", "Ellie My Love"),
    ("Valse oubliée no. 4, S. 215 no. 4", "Valse oubliée no. 1, S 215"),
    ("Love Me Tender", "Love"),
    ("Lost Love", "Love"),
    ("When you wish upon a star", "When the Swallows Come Back to Capistrano"),
])
def test_same_title_keeps_different_songs_apart(expected, got):
    assert not same_title(expected, got)


def test_module_documents_its_thresholds():
    assert (audio_verify.EVIDENCE_SCORE, audio_verify.STRONG_SOURCES) == (0.8, 3)


# --- Service clients ----------------------------------------------------------------------

class FakeHttp:
    def __init__(self, status_code=200, payload=None, error=None):
        self.status_code, self._payload, self.error = status_code, payload, error
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return self

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def test_fingerprint_runs_fpcalc_and_reads_its_json(monkeypatch):
    calls = []

    class Done:
        returncode, stdout, stderr = 0, '{"duration": 181.4, "fingerprint": "AQADtJGS"}', ""

    monkeypatch.setattr(audio_verify.subprocess, "run", lambda args, **kwargs: calls.append((args, kwargs)) or Done())
    assert audio_verify.fingerprint("/tmp/song.mp3") == (181, "AQADtJGS")
    assert calls[0][0] == ["fpcalc", "-json", "-ignore-errors", "-length", "120", "/tmp/song.mp3"]
    assert calls[0][1]["timeout"] > 0


def test_fingerprint_failures_are_unavailable_not_verdicts(monkeypatch):
    def missing(args, **kwargs):
        raise FileNotFoundError("fpcalc")

    monkeypatch.setattr(audio_verify.subprocess, "run", missing)
    with pytest.raises(VerificationUnavailable, match="fpcalc is not installed"):
        audio_verify.fingerprint("/tmp/song.mp3")

    class Failed:
        returncode, stdout, stderr = 2, "", "ERROR: Could not decode"

    monkeypatch.setattr(audio_verify.subprocess, "run", lambda args, **kwargs: Failed())
    with pytest.raises(VerificationUnavailable, match="Could not decode"):
        audio_verify.fingerprint("/tmp/song.mp3")


def test_acoustid_lookup_posts_fingerprint_and_never_leaks_the_key(monkeypatch):
    ok = FakeHttp(payload={"status": "ok", "results": []})
    monkeypatch.setattr(audio_verify.requests, "post", ok)
    client = audio_verify.AcoustIdClient("SECRETKEY", min_interval=0)
    assert client.lookup(181, "AQADtJGS") == {"status": "ok", "results": []}
    url, kwargs = ok.calls[0]
    assert url == "https://api.acoustid.org/v2/lookup"
    assert kwargs["data"] == {"client": "SECRETKEY", "meta": "recordings sources", "duration": 181,
                              "fingerprint": "AQADtJGS", "format": "json"}

    for fake in (FakeHttp(error=audio_verify.requests.ConnectionError("https://api.acoustid.org/?client=SECRETKEY")),
                 FakeHttp(status_code=503, payload={}),
                 FakeHttp(payload={"status": "error", "error": {"message": "invalid API key SECRETKEY"}}),
                 FakeHttp(payload=ValueError("not json"))):
        monkeypatch.setattr(audio_verify.requests, "post", fake)
        with pytest.raises(VerificationUnavailable) as raised:
            client.lookup(181, "AQADtJGS")
        assert "SECRETKEY" not in str(raised.value)


def test_rate_limiter_spaces_calls(monkeypatch):
    now = [100.0]
    sleeps = []
    monkeypatch.setattr(audio_verify.time, "monotonic", lambda: now[0])

    def fake_sleep(seconds):
        sleeps.append(round(seconds, 3))
        now[0] += seconds

    monkeypatch.setattr(audio_verify.time, "sleep", fake_sleep)
    limiter = audio_verify.RateLimiter(1 / 3)
    for _ in range(3):
        limiter.wait()
    assert sleeps == [0.333, 0.333]


def test_musicbrainz_recording_info_reads_works_follows_merges_and_caches(monkeypatch):
    fake = FakeHttp(payload={"id": "new-id", "relations": [
        {"type": "performance", "work": {"id": "w1", "title": "Canon"}}, {"type": "other"}]})
    monkeypatch.setattr(audio_verify.requests, "get", fake)
    client = audio_verify.MusicBrainzClient("LidaTube/test ( https://example.test )", min_interval=0)
    assert client.recording_info("old-id") == ("new-id", {"w1"})
    assert client.recording_info("old-id") == ("new-id", {"w1"})
    assert len(fake.calls) == 1
    url, kwargs = fake.calls[0]
    assert url == "https://musicbrainz.org/ws/2/recording/old-id"
    assert kwargs["params"] == {"inc": "work-rels", "fmt": "json"}
    assert kwargs["headers"]["User-Agent"] == "LidaTube/test ( https://example.test )"


def test_musicbrainz_unknown_recording_has_no_works_and_outages_are_unavailable(monkeypatch):
    monkeypatch.setattr(audio_verify.requests, "get", FakeHttp(status_code=404, payload={}))
    client = audio_verify.MusicBrainzClient("ua", min_interval=0, retry_delays=())
    assert client.recording_info("gone") == ("gone", set())
    monkeypatch.setattr(audio_verify.requests, "get", FakeHttp(status_code=503, payload={}))
    with pytest.raises(VerificationUnavailable):
        client.recording_info("busy")


def test_musicbrainz_retries_rate_limit_responses(monkeypatch):
    responses = [FakeHttp(status_code=503, payload={}), FakeHttp(payload={"id": "r", "relations": []})]
    monkeypatch.setattr(audio_verify.requests, "get", lambda url, **kwargs: responses.pop(0))
    monkeypatch.setattr(audio_verify.time, "sleep", lambda seconds: None)
    client = audio_verify.MusicBrainzClient("ua", min_interval=0, retry_delays=(0.01,))
    assert client.recording_info("r") == ("r", set())


def test_verifier_fingerprints_looks_up_and_decides(monkeypatch):
    monkeypatch.setattr(audio_verify, "fingerprint", lambda path, **kwargs: (180, "FP"))
    acoustid = Mock_lookup({"status": "ok", "results": [{"score": 0.97, "recordings": [
        {"id": "other", "title": "(Or) Is It Love", "sources": 1}]}]})
    musicbrainz = type("NoWorks", (), {"recording_info": staticmethod(lambda recording_id: (recording_id, set()))})()
    verifier = audio_verify.Verifier(acoustid, musicbrainz=musicbrainz)
    track = {"track_title": "Soakin’ Wet", "recording_id": "exp", "artist": "Tammy Wynette"}
    verdict = verifier.verify("/tmp/song.mp3", track)
    assert verdict.status == "rejected"
    assert acoustid.calls == [(180, "FP")]


def test_verifier_without_musicbrainz_cannot_judge_works_and_holds_the_file(monkeypatch):
    monkeypatch.setattr(audio_verify, "fingerprint", lambda path, **kwargs: (180, "FP"))
    acoustid = Mock_lookup({"status": "ok", "results": [{"score": 0.97, "recordings": [
        {"id": "other", "title": "Kanon", "sources": 139}]}]})
    verifier = audio_verify.Verifier(acoustid, musicbrainz=None)
    with pytest.raises(VerificationUnavailable):
        verifier.verify("/tmp/song.mp3", {"track_title": "Canon in D", "recording_id": "exp", "artist": "Pachelbel"})


class Mock_lookup:
    def __init__(self, response):
        self.response, self.calls = response, []

    def lookup(self, duration, fingerprint):
        self.calls.append((duration, fingerprint))
        return self.response


# --- Variants of the requested song -------------------------------------------------------

def _heard(*titles, sources=20):
    return {"status": "ok", "results": [{"score": 0.95, "recordings": [
        {"id": f"cand{index}", "title": title, "sources": sources, "artists": [{"id": "a1", "name": "Artist"}]}
        for index, title in enumerate(titles)]}]}


def _no_works(recording_id):
    return recording_id, set()


@pytest.mark.parametrize("heard", ["Song (Instrumental)", "Song (Karaoke Version)", "Song (Remix)", "Song - Radio Edit",
                                   "Song (Clean)", "Song (Acoustic)", "Song [Demo]", "Song (A Cappella)", "Song (Dance Mix)"])
def test_a_variant_of_the_requested_song_is_kept_but_flagged(heard):
    verdict = decide("Song", "exp", _heard(heard), _no_works, expected_artist="Artist")
    assert verdict.status == "variant"
    assert verdict.accepted


@pytest.mark.parametrize("heard", ["Song (Live)", "Song (Live at Carnegie Hall)", "Song - Live"])
def test_a_live_recording_heard_for_a_studio_request_is_rejected(heard):
    verdict = decide("Song", "exp", _heard(heard), _no_works, expected_artist="Artist")
    assert verdict.status == "rejected"
    assert "live" in verdict.reason


def test_a_live_recording_is_right_for_a_live_request():
    assert decide("Song", "exp", _heard("Song (Live)"), _no_works, expected_artist="Artist", expected_live=True).status == "verified"
    assert decide("Song (Live)", "exp", _heard("Song (Live at Wembley)"), _no_works, expected_artist="Artist").status == "verified"


def test_no_flag_when_any_matching_recording_is_the_plain_song():
    # AcoustID often groups a remix's entry with the original's; one plain title is enough.
    assert decide("Song", "exp", _heard("Song (Remix)", "Song"), _no_works, expected_artist="Artist").status == "verified"
    assert decide("Song", "exp", _heard("Song (Live)", "Song"), _no_works, expected_artist="Artist").status == "verified"


def test_no_flag_when_the_request_names_the_same_variant():
    assert decide("Song (Remix)", "exp", _heard("Song (Remix)"), _no_works, expected_artist="Artist").status == "verified"
    assert decide("Song (Instrumental)", "exp", _heard("Song - Instrumental"), _no_works, expected_artist="Artist").status == "verified"


@pytest.mark.parametrize("title", ["Clean", "Live Forever", "Remix to Ignition", "Live and Let Die"])
def test_variant_words_that_are_part_of_the_song_title_are_not_variants(title):
    assert decide(title, "exp", _heard(title), _no_works, expected_artist="Artist").status == "verified"


def test_the_expected_recording_itself_is_never_a_variant():
    response = {"status": "ok", "results": [{"score": 0.95, "recordings": [{"id": "exp", "title": "Song (Live)", "sources": 9}]}]}
    assert decide("Song", "exp", response, _no_works).status == "verified"


def test_verifier_tells_the_verdict_whether_the_album_is_live(monkeypatch):
    monkeypatch.setattr(audio_verify, "fingerprint", lambda path, **kwargs: (180, "FP"))
    verifier = audio_verify.Verifier(Mock_lookup(_heard("Song (Live)")), musicbrainz=None)
    track = {"track_title": "Song", "recording_id": "exp", "artist": "Artist"}
    assert verifier.verify("/tmp/x.mp3", track, {"album_name": "Live at Leeds"}).status == "verified"
    assert verifier.verify("/tmp/x.mp3", track, {"album_name": "Album", "album_secondary_types": ["Live"]}).status == "verified"
    assert verifier.verify("/tmp/x.mp3", track, {"album_name": "Studio Album"}).status == "rejected"


def test_fingerprint_of_a_track_shorter_than_the_sample_length_is_used_despite_fpcalc_exit_code(monkeypatch):
    # fpcalc 1.5.1 exits 3 ("Error decoding audio frame (End of file)") on any file shorter than
    # -length, while printing a valid fingerprint; about 9% of library tracks are under 2 minutes.
    class ShortTrack:
        returncode, stdout, stderr = 3, '{"duration": 57.2, "fingerprint": "AQADtShort"}', "ERROR: Error decoding audio frame (End of file)"

    monkeypatch.setattr(audio_verify.subprocess, "run", lambda args, **kwargs: ShortTrack())
    assert audio_verify.fingerprint("/tmp/short.mp3") == (57, "AQADtShort")


@pytest.mark.parametrize("stdout", ["", '{"duration": 57.2}', '{"fingerprint": "AQAD"}', '{"duration": 0, "fingerprint": "AQAD"}'])
def test_fingerprint_without_both_fingerprint_and_duration_is_unavailable(monkeypatch, stdout):
    class Broken:
        returncode, stderr = 3, "ERROR: Error decoding audio frame"

    Broken.stdout = stdout
    monkeypatch.setattr(audio_verify.subprocess, "run", lambda args, **kwargs: Broken())
    with pytest.raises(VerificationUnavailable):
        audio_verify.fingerprint("/tmp/broken.mp3")


@pytest.mark.parametrize("seconds, expected_ms, status", [
    (57, 57000, "verified"),     # a short track is judged normally
    (100, 180000, "truncated"),  # far shorter than Lidarr's length: a broken download
    (170, 180000, "verified"),   # within normal tolerance
    (40, 0, "verified"),         # Lidarr has no length to compare
])
def test_verifier_treats_a_file_far_shorter_than_expected_as_truncated(monkeypatch, seconds, expected_ms, status):
    monkeypatch.setattr(audio_verify, "fingerprint", lambda path, **kwargs: (seconds, "FP"))
    acoustid = Mock_lookup({"status": "ok", "results": [{"score": 0.97, "recordings": [{"id": "exp", "title": "Song", "sources": 9}]}]})
    verifier = audio_verify.Verifier(acoustid, musicbrainz=None)
    verdict = verifier.verify("/tmp/x.mp3", {"track_title": "Song", "recording_id": "exp", "artist": "A", "duration_ms": expected_ms})
    assert verdict.status == status
    if status == "truncated":
        assert not verdict.accepted
        assert acoustid.calls == []


# --- Evidence ladder ----------------------------------------------------------------------

def _recording(title, artists=(), rec_id="cand", sources=20):
    return {"id": rec_id, "title": title, "sources": sources, "artists": [{"id": f"a{i}", "name": n} for i, n in enumerate(artists)]}


def _one(*recordings, score=0.95):
    return {"status": "ok", "results": [{"score": score, "recordings": list(recordings)}]}


def test_same_title_by_the_requested_artist_is_verified_without_musicbrainz():
    def unavailable(recording_id):
        raise AssertionError("MusicBrainz should not be asked")

    assert decide("Song", "exp", _one(_recording("Song", ["The Artist"])), unavailable, expected_artist="Artist").status == "verified"


def test_same_title_without_artist_or_work_is_kept_but_not_called_verified():
    # Soundtracks are credited to performers on AcoustID and to the composer in Lidarr, so
    # a matching title alone cannot be refused, but neither does it prove the recording.
    verdict = decide("Theme From \"Jaws\"", "exp", _one(_recording("Jaws Theme", ["Boston Pops"])), _no_works,
                     expected_artist="John Williams")
    assert verdict.status == "title_only"
    assert verdict.accepted


def test_shared_work_is_verified_even_when_titles_differ_and_artists_do_not_match():
    info = {"exp": ("exp", {"w1"}), "cand": ("cand", {"w1"})}
    assert decide("Canon in D", "exp", _one(_recording("Kanon", ["Orchestra"])), info.__getitem__,
                  expected_artist="Pachelbel").status == "verified"


def test_a_longer_title_containing_a_shorter_one_is_only_weak_evidence():
    # "Love Me" is a different song from "Love Me Tender", by the same artist.
    verdict = decide("Love Me Tender", "exp", _one(_recording("Love Me", ["Elvis Presley"])), _no_works,
                     expected_artist="Elvis Presley")
    assert verdict.status == "weak"
    assert verdict.status != "verified"


def test_artist_credits_are_read_in_acoustids_object_shape_and_plain_strings():
    assert audio_verify._artist_credited("M83", [{"id": "x", "name": "M83"}])
    assert audio_verify._artist_credited("M83", ["M83"])
    assert audio_verify._artist_credited("The Bangles", [{"name": "Bangles"}])
    assert not audio_verify._artist_credited("M83", [{"id": "x"}, None, {"name": "Someone Else"}])


def test_cross_script_title_with_acoustids_artist_objects_is_flagged_not_crashed():
    response = _one(_recording("Москва", ["Чайф"]))
    assert decide("Moskva", "exp", response, _no_works, expected_artist="Chaif").status == "flagged"


# --- Titles written differently (from a 10k-file library scan) ----------------------------

def _heard_one(title, sources=30, artists=("Someone Else",)):
    return {"status": "ok", "results": [{"score": 0.97, "recordings": [
        {"id": "cand", "title": title, "sources": sources, "artists": [{"name": a} for a in artists]}]}]}


@pytest.mark.parametrize("expected, heard", [
    ("Burnout", "Burn Out"),
    ("Two O’Clock in the Morning", "2 O'Clock in the Morning"),
    ("I Hope We Meet (On the Road Some Day)", "I Hope We Meet (On the Road Someday)"),
    ("You Will Never Find", "You'll Never Find"),
])
def test_spacing_numerals_and_contractions_are_the_same_title(expected, heard):
    assert same_title(expected, heard)


@pytest.mark.parametrize("expected, heard", [
    ("Joshua Fir The Battle Of Jericho", "Joshua Fit The Battle Of Jericho"),
    ("Aaj Koi Baat Ho Gayi", "Aaj Koi Baat Ho Gaye"),
    ("It Tears Me Ail to Pieces", "It Tears Me All to Pieces"),
])
def test_one_misspelt_word_in_a_longer_title_is_kept_as_similar(expected, heard):
    verdict = decide(expected, "exp", _heard_one(heard), _no_works)
    assert verdict.status == "weak"


@pytest.mark.parametrize("expected, heard", [
    ("Goldberg Variations BWV 988: 22.Variation 21 Canon at the Seventh", "Goldberg Variations, BWV 988: Variation 21. Canon on the Seventh"),
    ("Goldberg Variations BWV 988: 14.Variation 13", "Goldberg Variations, BWV 988: XIV. Variatio 13 a 2 clav."),
    ("Fünf Lieder, op. 105: I. Wie Melodien zieht es mir", "Five Songs, Op. 105 No. 1: Wie Melodien zieht es mir"),
    ("Passion selon saint Jean, BWV 245: Chorus \"Sei gergrubet, lieber judenkonig\"", "St. John Passion, BWV 245: Part II. Chorus \"Sei gegrüßet, lieber Jüdenkönig\""),
    ("Mass in B Minor, BWV 232: XX. Osanna [da capo]", "Mass in B minor BWV 232: Osanna in excelsis (Chorus)"),
    ("Partita no. 2 for Violin Solo in D Minor, BWV 1004: V. Chaconne", "Partita No.2 in D minor, BWV 1004: V. Ciaccona"),
    ("12 Etudes, Op. 25: No. 8 in D-Flat Major", "Etude, op. 25 no. 8 in D-flat major \"Sixths\""),
    ("Symphony no. 1 in B-flat major, op. 38, “Frühling”: Allegro animato e grazioso", "Symphony No.1 in B-flat major, Op. 38 - IV. Allegro animato e grazioso"),
])
def test_same_catalogue_number_and_movement_is_kept_as_similar(expected, heard):
    assert decide(expected, "exp", _heard_one(heard), _no_works).status == "weak"


@pytest.mark.parametrize("expected, heard", [
    ("Goldberg Variations BWV 988: 9.Variation 8", "Goldberg Variations, BWV 988: Variatio 7. a 1 ô vero 2 Clav."),
    ("Concerto in F minor “L’inverno”, op. 8 no. 4, RV 297: III. Allegro", "Concerto in F minor “L’inverno”, op. 8 no. 4, RV 297: I. Allegro non molto"),
    ("Concerto no. 5 in F minor, BWV 1056: I. [...]", "Keyboard Concerto in F minor, BWV 1056: II. Adagio"),
    ("Acht Klavierstücke op. 76: 1. Capriccio fis-moll", "8 Klavierstücke, op. 76: No. 2, Capriccio in B minor"),
    ("Sonata, BWV 1005 in C: Adagio", "Sonata no. 3 in C major, BWV 1005: IV. Allegro assai"),
    ("Concerto Grosso in D major, op. 6 no. 5, HWV 323: IV. Largo", "Concerto Grosso in D, Op. 6 No. 5: Allegro"),
    ("The Well-Tempered Clavier, Book I: No. 1 in C major, BWV 846: Prelude", "Fugue no. 1 en do majeur, BWV 846"),
    # Lidarr's "30." is the track index glued to the title, not the variation's number.
    ("Goldberg Variations BWV 988: 30.Variation 29", "Aria with 30 Variations \"Goldberg Variations\", BWV 988: Var. 30. Quodlibet"),
    ("12 Etudes, Op. 25: No. 9 in G-Flat Major", "Etude, op. 25 no. 8 in D-flat major \"Sixths\""),
    ("String Quartet No. 2 in A minor, Op. 51/2: II. Andante moderato", "String quartet in A minor, op. 51 no. 2: I. Allegro non troppo"),
])
def test_same_catalogue_number_with_another_movement_is_still_rejected(expected, heard):
    assert decide(expected, "exp", _heard_one(heard), _no_works).status == "rejected"


@pytest.mark.parametrize("expected, heard", [("Still Up", "Set Him Up"), ("Long Gun", "Long Way"), ("AGUA", "AGUARDIENTE"),
                                             ("Days of Desperation", "Days Are Blood")])
def test_different_songs_with_shared_words_are_still_rejected(expected, heard):
    assert decide(expected, "exp", _heard_one(heard), _no_works).status == "rejected"


@pytest.mark.parametrize("expected, heard", [
    ("St. Louis Blues", "Saint Louis Blues"),
    ("Pale Dry Boogie, Pt 1", "Pale Dry Boogie, Part 1"),
    ("15. O Weh, Sie Binden Ihn Mit Strick Und Ketten!", "O weh, sie binden ihn mit Strick und Ketten!"),
])
def test_abbreviations_and_a_leading_track_index_are_the_same_title(expected, heard):
    assert same_title(expected, heard)


@pytest.mark.parametrize("expected, heard", [
    ("Tumhen Dillagi Bhool Jaani Padegi", "Tumhen Dillagi Bhool Jani Paregee"),
    ("Aag Daman Main Lag Jayegi", "Aag Daman Mein Lag Jai"),
    ("Data Saab De Dware", "Data sahab de daware"),
    ("Children’s Corner: Golliwog’s Cake-Walk", "Children's Corner: VI. Golliwoggs Cakewalk"),
    ("Mass in B Minor, BWV 232: X. Credo in unum Deum", "B Minor Mass: Credo: Chorus. Credo in unum Deum"),
    ("Orchestral Suite no. 3 in D major, BWV 1068: II. Air", "Orchestral Suite no. 3 in D major: Air on a G String"),
    ('Orlando, HWV 31: Act II. "Verdi allori" (Medoro)', 'Orlando: Act Two VIII. "Verdi allori" (Medoro)'),
])
def test_transliterations_and_one_sided_movement_numbers_are_kept_as_similar(expected, heard):
    assert decide(expected, "exp", _heard_one(heard), _no_works).status == "weak"


def test_a_bracketed_composer_name_is_not_part_of_the_title():
    response = _heard_one("Abendempfindung (Wolfgang Amadeus Mozart)", artists=("Elly Ameling",))
    assert decide("Abendempfindung KV523", "exp", response, _no_works, expected_artist="Wolfgang Amadeus Mozart").status == "title_only"


def test_placeholder_titles_from_acoustid_are_no_evidence():
    response = _heard_one("Track 12", sources=50)
    assert decide("Cantata, BWV 130: IV. Recitativo", "exp", response, _no_works).status == "no_match"


@pytest.mark.parametrize("expected, heard", [
    ("There Was a Kid (Part 3)", "There Was a Kid, Part 2"),
    ("Up, Up and Down", "Up, Up, and Away"),
    ("Symphony no. 5 in C minor, op. 67: III. Allegro Scherzo", "Symphony No. 4, Op. 60 - III. Allegro Vivace"),
    ("Precious Lord,Take My Hand", "Precious Lord, Hold My Hand"),
    ("Data Saab De Dware", "Data Sahab Ki Nagri"),
])
def test_similar_looking_different_titles_stay_rejected(expected, heard):
    assert decide(expected, "exp", _heard_one(heard), _no_works).status == "rejected"
