"""Audio verification: does a downloaded file's audio belong to the requested track?

Title matching cannot catch an upload titled as one song but carrying another, so after
yt-dlp finishes, the file is fingerprinted with fpcalc (Chromaprint) and looked up on
AcoustID. The verdict compares what AcoustID heard with what Lidarr asked for:

* Verify when a recording AcoustID heard with a score of at least EVIDENCE_SCORE is the
  expected recording (merged MusicBrainz ids included), has the same title (formatting set
  aside) with the requested artist credited, or shares a MusicBrainz work with it. An artist
  match is never enough on its own: a wrong song by the right artist is the commonest failure.
* Keep, without calling it verified, a file whose only evidence is the same title
  ("title_only": soundtracks credit performers on AcoustID and the composer in Lidarr) or a
  similar one ("weak"): "Love Me" for "Love Me Tender", one misspelt word, or the same
  catalogue number and movement written in another language or edition.
* Reject when none of those recordings matches and the evidence is strong (one heard by at
  least STRONG_SOURCES sources), or when every one of them has a title sharing no word with
  the request (the mislabelled-upload case, often heard by a single source).
* Otherwise keep the file: "no_match" when AcoustID has no confident result, "weak" when
  thin evidence names a related title, "flagged" when titles match only across scripts (and
  the artist matches).

A recording that is the requested song may still be the wrong take. When every such recording
is live and a studio recording was requested, the file is rejected, since a live take must never
stand in for the original. Other variants the request did not name (instrumental, remix,
karaoke, clean...) are kept as "variant" for review.

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

import _matcher

EVIDENCE_SCORE = 0.8
# A file this much shorter than Lidarr's length is a broken download, not a candidate to judge.
TRUNCATED_FRACTION = 0.75
TRUNCATED_MIN_SECONDS = 20
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
        return self.status not in ("rejected", "truncated")


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
    """(duration in whole seconds, compressed fingerprint) of an audio file, via Chromaprint's fpcalc.

    fpcalc 1.5.1 exits non-zero ("Error decoding audio frame (End of file)") on any file shorter
    than the sample length while still printing a valid fingerprint, so its exit status is not
    trusted: the result counts when it carries both a fingerprint and a duration.
    """
    try:
        done = subprocess.run([fpcalc, "-json", "-ignore-errors", "-length", str(length), path], capture_output=True,
                              text=True, timeout=FPCALC_TIMEOUT_SECONDS)
    except FileNotFoundError:
        raise VerificationUnavailable("fpcalc is not installed (Chromaprint's libchromaprint-tools)") from None
    except subprocess.TimeoutExpired:
        raise VerificationUnavailable(f"fpcalc timed out after {FPCALC_TIMEOUT_SECONDS}s") from None
    try:
        result = json.loads(done.stdout or "")
        duration, fingerprint_text = int(result["duration"]), result["fingerprint"]
    except (ValueError, KeyError, TypeError):
        duration, fingerprint_text = 0, None
    if duration <= 0 or not fingerprint_text:
        raise VerificationUnavailable(f"fpcalc returned no fingerprint: {(done.stderr or '').strip()[:200]}")
    return duration, fingerprint_text


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

    def verify(self, path, track, album=None):
        album = album or {}
        duration, fingerprint_text = fingerprint(path, fpcalc=self.fpcalc)
        expected_seconds = (track.get("duration_ms") or 0) / 1000.0
        if expected_seconds and duration < expected_seconds * TRUNCATED_FRACTION \
                and expected_seconds - duration > TRUNCATED_MIN_SECONDS:
            return Verdict("truncated", f"file is {duration}s, Lidarr expects {expected_seconds:.0f}s")
        response = self.acoustid.lookup(duration, fingerprint_text)
        expected_live = _matcher._request_is_live(track.get("track_title", ""), album.get("album_name"),
                                                  album.get("album_secondary_types"))
        return decide(track.get("track_title", ""), track.get("recording_id"), response, self._recording_info,
                      expected_artist=track.get("artist", ""), expected_live=expected_live)


# --- Titles -------------------------------------------------------------------------------

_CATALOGUE = re.compile(
    r"\b(?:bwv|op|opus|k|kv|rv|hob|sc|p|t|s|woo|anh|r)\.?\s*\d+[a-z]?(?:\s*(?:no|nr)\.?\s*\d+)?\b", re.I)
_QUALIFIER_GROUP = re.compile(
    r"[\(\[][^\)\]]*\b(?:version|mix|remix|edit|take|tk|alt|alternate|remaster(?:ed)?|mono|stereo|demo|"
    r"instrumental|excerpt|orch|arr|feat|ft|featuring|clean|explicit|acoustic|a ?cappella|acapella|karaoke|unplugged|"
    r"live|radio)\b[^\)\]]*[\)\]]", re.I)
# A trailing " - Live" or " - Radio Edit" names the take, like a bracketed qualifier.
_QUALIFIER_SUFFIX = re.compile(
    r"\s+-\s+[^-]*\b(?:live|remix|mix|edit|version|instrumental|acoustic|demo|clean|karaoke|remaster(?:ed)?|"
    r"a ?cappella|acapella|unplugged)\b[^-]*$", re.I)
_FROM_SOURCE = re.compile(
    r"^(?P<cue>.*?)\s*[\(\[]?\s*\bfrom\s+[\"'“‘]?(?P<source>[^\"'”’\)\]]+)[\"'”’]?\s*[\)\]]?(?P<rest>.*)$",
    re.I)
# Words that differ between formats of one title ("Main Theme From X" vs "X: Main Theme").
_FILLER = {"the", "a", "an", "of", "from", "and", "in", "no", "nr", "op", "opus", "de", "la", "le", "der", "die",
           "das", "to", "for", "main", "title", "titles", "theme", "themes", "end", "credits", "credit"}
_ROMAN = {"i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8", "ix": "9", "x": "10"}


_NUMBER_WORDS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8",
                 "nine": "9", "ten": "10", "eleven": "11", "twelve": "12"}
# What a contraction leaves behind once the apostrophe is gone: "You'll" -> "you ll".
_CONTRACTIONS = {"ll": "will", "re": "are", "ve": "have"}


def _fold(text):
    text = unidecode(text or "").lower().replace("&", " and ")
    text = _QUALIFIER_GROUP.sub(" ", text)
    text = _QUALIFIER_SUFFIX.sub(" ", text)
    text = _CATALOGUE.sub(" ", text)
    words = re.findall(r"[a-z0-9]+", text)
    return " ".join(_CONTRACTIONS.get(word, _NUMBER_WORDS.get(word, word)) for word in words)


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
    # Spacing differs between spellings of one title: "Burnout" and "Burn Out".
    forms |= {form.replace(" ", "") for form in forms}
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


def _one_word_apart(expected, got):
    """Titles of three or more words that differ by one misspelt word ("Fir" for "Fit")."""
    a = [word for word in _fold(expected).split() if word not in _FILLER]
    b = [word for word in _fold(got).split() if word not in _FILLER]
    if len(a) != len(b) or len(a) < 3:
        return False
    differing = [(x, y) for x, y in zip(a, b) if x != y]
    return len(differing) == 1 and min(map(len, differing[0])) >= 3 and fuzz.ratio(*differing[0]) >= 66


# --- Catalogued classical works -----------------------------------------------------------

_WORK_NUMBER = re.compile(
    r"\b(bwv|rv|hwv|kv|k|hob\.?\s*[ivx]+\s*:?|op|opus|s|d)\.?\s*(\d+[a-z]?)(?:\s*,?\s*(?:no|nr|n)\.?\s*(\d+))?", re.I)
_ROMAN_NUMERALS = {numeral: value for value, numeral in enumerate(
    "i ii iii iv v vi vii viii ix x xi xii xiii xiv xv xvi xvii xviii xix xx xxi xxii xxiii xxiv xxv xxvi xxvii xxviii xxix xxx".split(), 1)}
# Words every movement shares; they cannot tell one movement from another.
_GENERIC_MOVEMENT_WORDS = set("""allegro allegretto adagio andante andantino largo larghetto lento presto prestissimo vivace
    moderato grave assai non troppo molto poco piu ma con e ed di da il la in sehr menuet menuetto minuet minuetto rondo
    scherzo finale aria chorus choral chorale coro recit recitativo prelude fugue fuga gigue giga sarabande courante allemande
    gavotte bourree trio part act major minor maggiore minore majeur mineur dur moll variation variations variatio tempo
    movement mvt flat sharp""".split())
# Lidarr titles often begin the movement with the track index glued on: "30.Variation 29".
_GLUED_TRACK_INDEX = re.compile(r"^\s*\d+\.(?=[a-z])")


def _work_numbers(title):
    text = unidecode(title or "").lower().replace("№", " no ")
    keys = set()
    for kind, number, sub in _WORK_NUMBER.findall(text):
        kind = re.sub(r"[\s.:]", "", kind)
        kind = {"opus": "op", "kv": "k"}.get(kind, kind)
        if kind in ("s", "d") and not re.search(rf"\b{kind}\.\s*\d", text):
            continue
        keys.add((kind, number.lstrip("0"), sub))
    return keys


def _movement(title, keys_text):
    """The part of a catalogued title naming the movement: after its last colon, or after its number."""
    text = unidecode(title or "").lower()
    if ":" in text:
        movement = text.rsplit(":", 1)[1]
    else:
        matches = list(_WORK_NUMBER.finditer(text))
        movement = text[matches[-1].end():] if matches else ""
    return _GLUED_TRACK_INDEX.sub("", movement)


def _movement_numbers(movement):
    numbers = set()
    for word in re.findall(r"[a-z0-9]+", movement):
        if word.isdigit():
            numbers.add(int(word))
        elif word in _ROMAN_NUMERALS:
            numbers.add(_ROMAN_NUMERALS[word])
    return numbers


def _movement_words(movement):
    return {word for word in re.findall(r"[a-z]+", movement)
            if len(word) >= 3 and word not in _GENERIC_MOVEMENT_WORDS and word not in _FILLER and word not in _ROMAN_NUMERALS}


def _same_catalogued_movement(expected, got):
    """True when both titles carry the same catalogue number and compatible movements.

    Translations and editions write one movement differently ("Passion selon saint Jean,
    BWV 245" / "St. John Passion, BWV 245"), so the catalogue number identifies the work. The
    movement must then agree: numbers that differ ("Variation 8" / "Variatio 7", "III." / "I.")
    mean another movement, and without numbers the movements must share a distinctive word,
    since "Allegro" alone names a movement in nearly every work.
    """
    a, b = _work_numbers(expected), _work_numbers(got)
    shared = {(kind, number) for kind, number, _ in a} & {(kind, number) for kind, number, _ in b}
    if not shared:
        return False
    for kind, number in shared:
        subs_a = {sub for k, n, sub in a if (k, n) == (kind, number) and sub}
        subs_b = {sub for k, n, sub in b if (k, n) == (kind, number) and sub}
        if subs_a and subs_b and not subs_a & subs_b:
            return False
    move_a, move_b = _movement(expected, a).strip(), _movement(got, b).strip()
    if not move_a or not move_b:
        return not move_a and not move_b
    numbers_a, numbers_b = _movement_numbers(move_a), _movement_numbers(move_b)
    # "Op. 25: No. 8" names the piece in its movement, "op. 25 no. 8" in its catalogue number.
    numbers_a = numbers_a or {int(sub) for kind, number, sub in a if (kind, number) in shared and sub}
    numbers_b = numbers_b or {int(sub) for kind, number, sub in b if (kind, number) in shared and sub}
    if numbers_a and numbers_b:
        return bool(numbers_a & numbers_b)
    words_a, words_b = _movement_words(move_a), _movement_words(move_b)
    return any(_near(word, words_b) for word in words_a)


def _non_latin(text):
    return any(char.isalpha() and "LATIN" not in unicodedata.name(char, "") for char in text or "")


def _artist_names(artists):
    """Artist names from AcoustID's [{"id", "name"}] entries, or from plain strings."""
    for artist in artists or []:
        name = artist.get("name") if isinstance(artist, dict) else artist
        if isinstance(name, str) and name.strip():
            yield name


def _artist_key(name):
    folded = _fold(name)
    return folded[4:] if folded.startswith("the ") else folded


def _artist_credited(expected_artist, artists):
    expected = _artist_key(expected_artist)
    return bool(expected) and any(fuzz.ratio(expected, _artist_key(name)) >= 85 for name in _artist_names(artists))


# --- Verdict ------------------------------------------------------------------------------

def _candidates(response):
    for result in response.get("results") or []:
        score = result.get("score") or 0
        if score < EVIDENCE_SCORE:
            continue
        for recording in result.get("recordings") or []:
            if recording.get("id") and recording.get("title"):
                yield score, recording


_VARIANT_MARKER = re.compile(
    r"\b(instrumental|karaoke|remix|mix|acoustic|demo|clean|radio edit|a ?cappella|acapella|unplugged)\b")


def _qualifier_text(title):
    """Text in a title's brackets or after its last " - ": where a variant is named, not the song."""
    text = unidecode(title or "").lower()
    groups = re.findall(r"[\(\[]([^\)\]]*)[\)\]]", text)
    if " - " in text:
        groups.append(text.rsplit(" - ", 1)[1])
    return " ".join(groups)


def _variant_markers(title):
    return set(_VARIANT_MARKER.findall(_qualifier_text(title)))


def _variant_verdict(expected_title, expected_live, matching, top_titles, status, reason):
    """Judge recordings that are the requested song: a variant the request did not ask for counts
    only when every one of them is that variant, since AcoustID groups a remix with its original."""
    if not expected_live and all(_matcher._is_live_title(r["title"]) for r in matching):
        return Verdict("rejected", "AcoustID heard a live recording of a studio request", top_titles)
    wanted = _variant_markers(expected_title)
    if all(_variant_markers(r["title"]) - wanted for r in matching):
        variants = sorted(set().union(*(_variant_markers(r["title"]) - wanted for r in matching)))
        return Verdict("variant", f"AcoustID heard a variant ({', '.join(variants)})", top_titles)
    return Verdict(status, reason, top_titles)


def decide(expected_title, expected_recording_id, response, recording_info, expected_artist="", expected_live=None):
    """Judge one AcoustID lookup response against the track Lidarr expects.

    recording_info(recording_id) returns (current_id, work_ids) from MusicBrainz and may raise
    VerificationUnavailable; it is only called when ids and titles cannot settle the case.
    expected_live says whether a live recording was requested; None judges from the title.
    """
    if expected_live is None:
        expected_live = _matcher._is_live_title(expected_title)
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

    # Strongest evidence first. A matching title alone cannot prove the recording, since another
    # recording may share it: it counts as verified only with the artist credited or a shared work.
    same, other_script, similar = [], [], []
    for recording in recordings:
        if _title_forms(expected_title) & _title_forms(recording["title"]):
            (other_script if _non_latin(expected_title) != _non_latin(recording["title"]) else same).append(recording)
        elif _non_latin(expected_title) == _non_latin(recording["title"]) and (
                same_title(expected_title, recording["title"]) or _one_word_apart(expected_title, recording["title"])
                or _same_catalogued_movement(expected_title, recording["title"])):
            similar.append(recording)
    credited = [r for r in same if _artist_credited(expected_artist, r.get("artists"))]
    if credited:
        return _variant_verdict(expected_title, expected_live, credited, top_titles, "verified", "same title and artist")
    credited = [r for r in other_script if _artist_credited(expected_artist, r.get("artists"))]
    if credited:
        return _variant_verdict(expected_title, expected_live, credited, top_titles, "flagged",
                                "same title in another script, same artist")

    if expected_recording_id:
        current_id, expected_works = recording_info(expected_recording_id)
        same_work = []
        for recording in recordings[:MAX_WORK_LOOKUPS]:
            if recording["id"] == current_id:
                return Verdict("verified", "expected recording (merged id)", top_titles)
            if expected_works:
                _, works = recording_info(recording["id"])
                if expected_works & works:
                    same_work.append(recording)
        if same_work:
            return _variant_verdict(expected_title, expected_live, same_work, top_titles, "verified", "same work")

    if same:
        return _variant_verdict(expected_title, expected_live, same, top_titles, "title_only",
                                "same title, but no artist credit or shared work confirms the recording")
    if similar:
        return _variant_verdict(expected_title, expected_live, similar, top_titles, "weak",
                                "only a similar title matches")

    if any((recording.get("sources") or 0) >= STRONG_SOURCES for recording in recordings):
        return Verdict("rejected", "AcoustID heard a different recording", top_titles)
    expected_words = title_tokens(expected_title)
    if all(not (expected_words & title_tokens(recording["title"])) for recording in recordings):
        return Verdict("rejected", "AcoustID heard an unrelated title", top_titles)
    return Verdict("weak", "thin AcoustID evidence names a related title", top_titles)
