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
    same = {"status": "ok", "results": [{"score": 0.95, "recordings": [{"id": "cand", "title": "Song", "sources": 30}]}]}
    assert decide("Song", "exp", same, down).status == "verified"


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
    assert calls[0][0] == ["fpcalc", "-json", "-length", "120", "/tmp/song.mp3"]
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
