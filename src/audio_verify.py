"""Audio verification: does a downloaded file's audio belong to the requested track?

Title matching cannot catch an upload titled as one song but carrying another, so after
yt-dlp finishes, the file is fingerprinted with fpcalc (Chromaprint) and looked up on
AcoustID. The verdict compares what AcoustID heard with what Lidarr asked for:

* Accept when a recording AcoustID heard with a score of at least EVIDENCE_SCORE is the
  expected recording (merged MusicBrainz ids included), shares a MusicBrainz work with it,
  or has the same title once formatting is ignored. An artist match is never enough on its
  own: a wrong song by the right artist is the commonest failure.
* Reject when none of those recordings matches and the evidence is strong (one heard by at
  least STRONG_SOURCES sources), or when every one of them has a title sharing no word with
  the request (the mislabelled-upload case, often heard by a single source).
* Otherwise keep the file: "no_match" when AcoustID has no confident result, "weak" when
  thin evidence names a related title, "flagged" when titles match only across scripts.

The thresholds were set against real lookups of hand-labelled library files: 16 of 17 wrong
songs rejected, and none of 389 correct control files.
"""

import re
import subprocess
import threading
import time
import unicodedata
from dataclasses import dataclass, field

import requests
from thefuzz import fuzz
from unidecode import unidecode

EVIDENCE_SCORE = 0.8
STRONG_SOURCES = 3
# Each distinct candidate recording costs one MusicBrainz request (1/s) when titles disagree.
MAX_WORK_LOOKUPS = 5


class VerificationUnavailable(Exception):
    """A verification service could not answer; the file must be held, not imported or deleted."""


@dataclass
class Verdict:
    status: str  # verified, flagged, weak, no_match or rejected
    reason: str
    top_titles: list = field(default_factory=list)

    @property
    def accepted(self):
        return self.status != "rejected"


# --- Titles -------------------------------------------------------------------------------

_CATALOGUE = re.compile(
    r"\b(?:bwv|op|opus|k|kv|rv|hob|sc|p|t|s|woo|anh|r)\.?\s*\d+[a-z]?(?:\s*(?:no|nr)\.?\s*\d+)?\b", re.I)
_QUALIFIER_GROUP = re.compile(
    r"[\(\[][^\)\]]*\b(?:version|mix|remix|edit|take|tk|alt|alternate|remaster(?:ed)?|mono|stereo|demo|"
    r"instrumental|excerpt|orch|arr|feat|ft|featuring)\b[^\)\]]*[\)\]]", re.I)
_FROM_SOURCE = re.compile(
    r"^(?P<cue>.*?)\s*[\(\[]?\s*\bfrom\s+[\"'“‘]?(?P<source>[^\"'”’\)\]]+)[\"'”’]?\s*[\)\]]?(?P<rest>.*)$",
    re.I)
# Words that differ between formats of one title ("Main Theme From X" vs "X: Main Theme").
_FILLER = {"the", "a", "an", "of", "from", "and", "in", "no", "nr", "op", "opus", "de", "la", "le", "der", "die",
           "das", "to", "for", "main", "title", "titles", "theme", "themes", "end", "credits", "credit"}
_ROMAN = {"i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8", "ix": "9", "x": "10"}


def _fold(text):
    text = unidecode(text or "").lower().replace("&", " and ")
    text = _QUALIFIER_GROUP.sub(" ", text)
    text = _CATALOGUE.sub(" ", text)
    return " ".join(re.findall(r"[a-z0-9]+", text))


def title_tokens(title):
    """Meaningful words of a title: folded, without qualifiers, catalogue numbers or filler."""
    words = (_ROMAN.get(word, word) for word in _fold(title).split())
    return {word for word in words if word not in _FILLER}


def _title_forms(title):
    """Equivalent spellings: whole title, the cue without its source, the cue after a prefix."""
    text = unidecode(title or "")
    forms = {_fold(text), " ".join(sorted(_fold(text).split()))}
    pairs = []
    match = _FROM_SOURCE.match(text)
    if match and match.group("cue").strip():
        forms.add(_fold(match.group("cue")))
        forms.add(_fold(match.group("cue") + " " + match.group("rest")))
        pairs.append((match.group("source"), match.group("cue")))
    if ":" in text:
        prefix, cue = text.split(":", 1)
        forms.add(_fold(cue))
        pairs.append((prefix, cue))
        inner = _FROM_SOURCE.match(cue)
        if inner and inner.group("cue").strip():
            forms.add(_fold(inner.group("cue")))
    for source, cue in pairs:
        forms.add(" ".join(sorted((_fold(source) + " " + _fold(cue)).split())))
    return {form for form in forms if form}


def _near(word, pool):
    return word in pool or (len(word) >= 5 and any(len(other) >= 5 and fuzz.ratio(word, other) >= 80 for other in pool))


def same_title(expected, got):
    """True when two titles name the same piece once formatting is set aside.

    Formatting covers source phrases ('Theme From "Jaws"' / "Jaws Theme"), composer or work
    prefixes, catalogue numbers, version qualifiers, punctuation and diacritics. Otherwise the
    shorter title's words must all appear in the longer one, and a one-word title must match
    exactly, so "Love" never stands in for "Lost Love" or "Love Me Tender".
    """
    if _title_forms(expected) & _title_forms(got):
        return True
    a, b = title_tokens(expected), title_tokens(got)
    if not a or not b:
        return False
    small, big = (a, b) if len(a) <= len(b) else (b, a)
    return all(_near(word, big) for word in small) and (len(small) >= 2 or len(big) == 1)


def _non_latin(text):
    return any(char.isalpha() and "LATIN" not in unicodedata.name(char, "") for char in text or "")


def _artist_credited(expected_artist, artists):
    expected = _fold(expected_artist)
    return bool(expected) and any(fuzz.ratio(expected, _fold(name)) >= 85 for name in artists or [])


# --- Verdict ------------------------------------------------------------------------------

def _candidates(response):
    for result in response.get("results") or []:
        score = result.get("score") or 0
        if score < EVIDENCE_SCORE:
            continue
        for recording in result.get("recordings") or []:
            if recording.get("id") and recording.get("title"):
                yield score, recording


def decide(expected_title, expected_recording_id, response, recording_info, expected_artist=""):
    """Judge one AcoustID lookup response against the track Lidarr expects.

    recording_info(recording_id) returns (current_id, work_ids) from MusicBrainz and may raise
    VerificationUnavailable; it is only called when ids and titles cannot settle the case.
    """
    candidates = sorted(_candidates(response), key=lambda item: -item[0])
    if not candidates:
        return Verdict("no_match", "AcoustID has no confident result")
    recordings = []
    for _, recording in candidates:
        if recording["id"] not in {r["id"] for r in recordings}:
            recordings.append(recording)
    top_titles = list(dict.fromkeys(r["title"] for r in recordings))[:3]

    for recording in recordings:
        if expected_recording_id and recording["id"] == expected_recording_id:
            return Verdict("verified", "expected recording", top_titles)
    for recording in recordings:
        if same_title(expected_title, recording["title"]):
            if _non_latin(expected_title) != _non_latin(recording["title"]):
                if _artist_credited(expected_artist, recording.get("artists")):
                    return Verdict("flagged", "same title in another script, same artist", top_titles)
                continue
            return Verdict("verified", "same title", top_titles)

    if expected_recording_id:
        current_id, expected_works = recording_info(expected_recording_id)
        for recording in recordings[:MAX_WORK_LOOKUPS]:
            if recording["id"] == current_id:
                return Verdict("verified", "expected recording (merged id)", top_titles)
            if expected_works:
                _, works = recording_info(recording["id"])
                if expected_works & works:
                    return Verdict("verified", "same work", top_titles)

    if any((recording.get("sources") or 0) >= STRONG_SOURCES for recording in recordings):
        return Verdict("rejected", "AcoustID heard a different recording", top_titles)
    expected_words = title_tokens(expected_title)
    if all(not (expected_words & title_tokens(recording["title"])) for recording in recordings):
        return Verdict("rejected", "AcoustID heard an unrelated title", top_titles)
    return Verdict("weak", "thin AcoustID evidence names a related title", top_titles)
