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
