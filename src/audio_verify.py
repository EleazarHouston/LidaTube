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

import json
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


# --- Services -----------------------------------------------------------------------------

FPCALC_TIMEOUT_SECONDS = 60
ACOUSTID_LOOKUP_URL = "https://api.acoustid.org/v2/lookup"
MUSICBRAINZ_RECORDING_URL = "https://musicbrainz.org/ws/2/recording/{}"


class RateLimiter:
    """Space calls at least min_interval seconds apart, across every thread that shares it."""

    def __init__(self, min_interval):
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            if now < self._next:
                time.sleep(self._next - now)
                now = self._next
            self._next = now + self.min_interval


def fingerprint(path, fpcalc="fpcalc", length=120):
    """(duration in whole seconds, compressed fingerprint) of an audio file, via Chromaprint's fpcalc."""
    try:
        done = subprocess.run([fpcalc, "-json", "-length", str(length), path], capture_output=True, text=True,
                              timeout=FPCALC_TIMEOUT_SECONDS)
    except FileNotFoundError:
        raise VerificationUnavailable("fpcalc is not installed (Chromaprint's libchromaprint-tools)") from None
    except subprocess.TimeoutExpired:
        raise VerificationUnavailable(f"fpcalc timed out after {FPCALC_TIMEOUT_SECONDS}s") from None
    if done.returncode != 0:
        raise VerificationUnavailable(f"fpcalc failed: {(done.stderr or '').strip()[:200]}")
    try:
        result = json.loads(done.stdout)
        return int(result["duration"]), result["fingerprint"]
    except (ValueError, KeyError, TypeError):
        raise VerificationUnavailable("fpcalc returned no fingerprint") from None


class AcoustIdClient:
    """AcoustID lookups, at most 3 a second as AcoustID asks. The key never appears in errors."""

    def __init__(self, api_key, timeout=15, min_interval=1 / 3):
        self._api_key = api_key
        self.timeout = timeout
        self._limiter = RateLimiter(min_interval)

    def lookup(self, duration, fingerprint_text):
        self._limiter.wait()
        try:
            response = requests.post(ACOUSTID_LOOKUP_URL, timeout=self.timeout, data={
                "client": self._api_key, "meta": "recordings sources", "duration": int(duration),
                "fingerprint": fingerprint_text, "format": "json",
            })
            body = response.json()
        except (requests.RequestException, ValueError) as error:
            # Request errors can quote the request, so report only the kind of failure.
            raise VerificationUnavailable(f"AcoustID unreachable ({type(error).__name__})") from None
        if response.status_code != 200 or body.get("status") != "ok":
            message = str((body.get("error") or {}).get("message") or f"HTTP {response.status_code}")
            raise VerificationUnavailable(f"AcoustID error: {message.replace(self._api_key, '***')[:200]}")
        return body


class MusicBrainzClient:
    """Recording lookups for current ids and works, at most 1 a second as MusicBrainz asks."""

    def __init__(self, user_agent, timeout=15, min_interval=1.1, retry_delays=(2, 5)):
        self.user_agent = user_agent
        self.timeout = timeout
        self.retry_delays = tuple(retry_delays)
        self._limiter = RateLimiter(min_interval)
        self._cache = {}
        self._cache_lock = threading.Lock()

    def recording_info(self, recording_id):
        """(current id, set of work ids); an id MusicBrainz no longer knows has no works."""
        with self._cache_lock:
            if recording_id in self._cache:
                return self._cache[recording_id]
        for delay in self.retry_delays + (None,):
            self._limiter.wait()
            try:
                response = requests.get(MUSICBRAINZ_RECORDING_URL.format(recording_id), timeout=self.timeout,
                                        params={"inc": "work-rels", "fmt": "json"},
                                        headers={"User-Agent": self.user_agent, "Accept": "application/json"})
            except requests.RequestException as error:
                raise VerificationUnavailable(f"MusicBrainz unreachable ({type(error).__name__})") from None
            if response.status_code in (429, 503) and delay is not None:
                time.sleep(delay)
                continue
            break
        if response.status_code == 404:
            info = (recording_id, set())
        elif response.status_code != 200:
            raise VerificationUnavailable(f"MusicBrainz error: HTTP {response.status_code}")
        else:
            try:
                body = response.json()
            except ValueError:
                raise VerificationUnavailable("MusicBrainz returned invalid JSON") from None
            works = {relation["work"]["id"] for relation in body.get("relations") or [] if relation.get("work")}
            info = (body.get("id") or recording_id, works)
        with self._cache_lock:
            if len(self._cache) > 50000:
                self._cache.clear()
            self._cache[recording_id] = info
        return info


class Verifier:
    """Fingerprint a file, look it up, and judge it against the track Lidarr expects."""

    def __init__(self, acoustid, musicbrainz, fpcalc="fpcalc"):
        self.acoustid = acoustid
        self.musicbrainz = musicbrainz
        self.fpcalc = fpcalc

    def _recording_info(self, recording_id):
        if self.musicbrainz is None:
            raise VerificationUnavailable("MusicBrainz lookups are not configured")
        return self.musicbrainz.recording_info(recording_id)

    def verify(self, path, track):
        duration, fingerprint_text = fingerprint(path, fpcalc=self.fpcalc)
        response = self.acoustid.lookup(duration, fingerprint_text)
        return decide(track.get("track_title", ""), track.get("recording_id"), response, self._recording_info,
                      expected_artist=track.get("artist", ""))


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
