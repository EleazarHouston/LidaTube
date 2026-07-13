"""
library_suspicion.py — Score every library file for how likely it is to be a
wrong / bad match, combining multiple independent signals:

  * duration delta vs Lidarr's expected track length (wrong-song / wrong-length)
  * embedded YouTube provenance (TXXX:purl / description / comment) — an official
    "Provided to YouTube by <label>" upload is trustworthy; a random upload is not
  * strong version markers in the provenance text (karaoke / instrumental / …),
    which catches same-duration instrumentals that the duration check alone misses

Every file gets a row and a 0-100 suspicion score (higher = more dubious) with a
list of reasons, written to CSV sorted worst-first for spot checking.

Usage:
    python library_suspicion.py \\
        --lidarr-url http://host.docker.internal:8686 \\
        --api-key YOUR_KEY \\
        --download-folder /lidatube/downloads \\
        --output suspicion.csv \\
        --cache-file lidarr_cache.json
"""

import argparse
import csv
import os

import requests
from mutagen.flac import FLAC
from mutagen.id3 import ID3

import audit_library

# Version *phrases* scanned inside free-text descriptions. Phrases (not bare
# words) are required because descriptions frequently embed the song's lyrics,
# where "instrumental break" or "walk to karaoke" appear innocently. A phrase
# like "official karaoke" or "(instrumental" only shows up when the upload
# really is that version.
_VERSION_PHRASES = [
    "karaoke version",
    "karaoke lyric",
    "official karaoke",
    "(karaoke",
    "karaoke)",
    "instrumental version",
    "(instrumental",
    "instrumental)",
    "-instrumental",
    "instrumental -",
    "instrumental audio",
    "instrumental track",
    "instrumental mix",
    "instrumental remake",
    "backing track",
    "made famous by",
    "originally performed by",
    "nightcore",
    "8d audio",
    "sped up",
]

# Phrases in the instrumental family — suppressed when the request itself asks
# for an instrumental (including abbreviations like "(inst.)" or "strumentale").
_INSTRUMENTAL_PHRASES = {
    "instrumental version", "(instrumental", "instrumental)", "-instrumental",
    "instrumental -", "instrumental audio", "instrumental track",
    "instrumental mix", "instrumental remake",
}
_INSTRUMENTAL_REQUEST_HINTS = (
    "instrumental", "(inst.", "(inst)", " inst.", "-inst", "strumentale", "instrumentale",
)


# ---------------------------------------------------------------------------
# Provenance extraction
# ---------------------------------------------------------------------------

def _first_frame(tags, predicate):
    for key in tags.keys():
        if predicate(key):
            return str(tags[key])
    return ""


def read_provenance(path):
    """Extract YouTube provenance left by yt-dlp that survives Lidarr re-tagging.

    Returns {'purl': str, 'description': str, 'comment': str}; blanks if absent.
    """
    prov = {"purl": "", "description": "", "comment": ""}
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".mp3":
            tags = ID3(path)

            def txxx(name):
                return _first_frame(tags, lambda k: k.startswith("TXXX:") and k[5:].lower() == name)

            prov["purl"] = txxx("purl")
            prov["description"] = txxx("description") or txxx("synopsis")
            prov["comment"] = _first_frame(tags, lambda k: k.startswith("COMM")) or txxx("comment")
        elif ext == ".flac":
            tags = FLAC(path).tags or {}
            lowered = {k.lower(): v for k, v in dict(tags).items()}

            def vc(name):
                vals = lowered.get(name)
                return vals[0] if vals else ""

            prov["purl"] = vc("purl")
            prov["description"] = vc("description") or vc("synopsis")
            prov["comment"] = vc("comment")
    except Exception:
        pass
    return prov


def is_youtube_grab(prov):
    """True if the file carries YouTube provenance (i.e. a LidaTube download)."""
    if "youtube" in prov.get("purl", "").lower():
        return True
    if "youtube" in prov.get("comment", "").lower():
        return True
    return prov.get("description", "").lower().startswith("provided to youtube by")


def is_official_art_track(prov):
    """True for auto-generated 'Provided to YouTube by <label>' uploads (trustworthy)."""
    return prov.get("description", "").lower().startswith("provided to youtube by")


# Only the primary metadata line matters — "<Title> (Instrumental) · <Artist>" or
# "The official karaoke lyric video for …" sit at the very start. Scanning further
# hits album tracklists (another track's "(instrumental)") and artist-bio prose
# ("made famous by …"), which are false positives.
_DESC_SCAN_CHARS = 220


def _request_wants_instrumental(expected_title):
    return any(hint in expected_title for hint in _INSTRUMENTAL_REQUEST_HINTS)


def provenance_has_strong_marker(prov, expected_title):
    """True if a version phrase appears in the provenance but not the request."""
    expected = (expected_title or "").lower()
    desc = prov.get("description", "")[:_DESC_SCAN_CHARS]
    haystack = (desc + " " + prov.get("comment", "")).lower()
    wants_instrumental = _request_wants_instrumental(expected)
    for phrase in _VERSION_PHRASES:
        if phrase not in haystack or phrase in expected:
            continue
        if phrase in _INSTRUMENTAL_PHRASES and wants_instrumental:
            continue
        return True
    return False


# ---------------------------------------------------------------------------
# Scoring (pure)
# ---------------------------------------------------------------------------

def score_track(signals):
    """Return (score, reasons) from a signals dict. Score clamped to 0-100.

    signals keys:
      duration_delta_s   float | None  — abs(actual - expected), None if unknown
      expected_known     bool          — Lidarr gave an expected duration
      is_grab            bool          — file has YouTube provenance
      official_art_track bool          — trustworthy 'Provided to YouTube by' upload
      provenance_bad_version bool      — karaoke/instrumental/... named in provenance
      lidarr_matched     bool          — the track resolved to a Lidarr track
    """
    score = 0
    reasons = []

    delta = signals.get("duration_delta_s")
    if delta is None:
        if not signals.get("expected_known"):
            score += 10
            reasons.append("no Lidarr duration to compare")
    elif delta > 60:
        score += 55
        reasons.append(f"duration off by {delta:.0f}s (>60s)")
    elif delta > 30:
        score += 40
        reasons.append(f"duration off by {delta:.0f}s")
    elif delta > 15:
        score += 25
        reasons.append(f"duration off by {delta:.0f}s")
    elif delta > 8:
        score += 12
        reasons.append(f"duration off by {delta:.0f}s")

    if signals.get("provenance_bad_version"):
        score += 45
        reasons.append("provenance names a karaoke/instrumental version")

    if signals.get("is_grab"):
        if signals.get("official_art_track"):
            score -= 25
            reasons.append("official 'Provided to YouTube by' upload")
        else:
            score += 15
            reasons.append("non-official YouTube upload")
    else:
        reasons.append("no YouTube provenance (external source)")

    if not signals.get("lidarr_matched"):
        score += 10
        reasons.append("no matching Lidarr track")

    score = max(0, min(100, score))
    return score, reasons


# ---------------------------------------------------------------------------
# Per-file assessment
# ---------------------------------------------------------------------------

def assess_file(path, session, lidarr_url, api_key, tolerance=8, _cache=None):
    """Assess one file and return a row dict (always returns a row)."""
    if _cache is None:
        _cache = {}

    tags = audit_library.read_tags(path) or {}
    actual_seconds = audit_library.get_audio_duration_seconds(path)
    prov = read_provenance(path)

    artist_name = tags.get("artist", "")
    album_name = tags.get("album", "")
    title = tags.get("title", "")
    track_number = tags.get("track_number")

    track = None
    if artist_name or album_name:
        tracks = audit_library.lookup_lidarr_tracks(artist_name, album_name, session, lidarr_url, api_key, _cache)
        track = audit_library.find_track_in_lidarr(tracks, track_number, title)

    expected_ms = track.get("duration", 0) if track else 0
    expected_seconds = expected_ms / 1000.0 if expected_ms else None
    delta = None
    if actual_seconds is not None and expected_seconds:
        delta = abs(actual_seconds - expected_seconds)

    signals = {
        "duration_delta_s": delta,
        "expected_known": bool(expected_seconds),
        "is_grab": is_youtube_grab(prov),
        "official_art_track": is_official_art_track(prov),
        "provenance_bad_version": provenance_has_strong_marker(prov, title),
        "lidarr_matched": track is not None,
    }
    score, reasons = score_track(signals)

    return {
        "score": score,
        "path": path,
        "artist": artist_name,
        "album": album_name,
        "title": title,
        "actual_s": round(actual_seconds, 1) if actual_seconds is not None else "",
        "expected_s": round(expected_seconds, 1) if expected_seconds else "",
        "delta_s": round(delta, 1) if delta is not None else "",
        "is_grab": signals["is_grab"],
        "official": signals["official_art_track"],
        "purl": prov.get("purl", ""),
        "reasons": "; ".join(reasons),
    }


FIELDNAMES = ["score", "path", "artist", "album", "title", "actual_s", "expected_s",
              "delta_s", "is_grab", "official", "purl", "reasons"]


def run(lidarr_url, api_key, download_folder, output_path, cache_file=None, min_score=0):
    session = requests.Session()
    cache = audit_library._load_lidarr_cache(cache_file)
    try:
        cache["__artists__"] = audit_library._preload_artists(session, lidarr_url, api_key)
        print(f"  {len(cache['__artists__'])} artists loaded.")
    except Exception as e:
        print(f"  Warning: could not pre-load artists ({e}), will fetch on demand.")
        cache["__artists__"] = {}

    files = list(audit_library.walk_library(download_folder))
    total = len(files)
    print(f"Scoring {total} files in '{download_folder}'...")

    rows = []
    for i, path in enumerate(files, 1):
        if i % 100 == 0 or i == total:
            print(f"  {i}/{total} scored...", flush=True)
        try:
            row = assess_file(path, session, lidarr_url, api_key, _cache=cache)
        except Exception as e:
            print(f"  Warning: failed on {path}: {e}")
            continue
        if row["score"] >= min_score:
            rows.append(row)

    rows.sort(key=lambda r: r["score"], reverse=True)
    audit_library._save_lidarr_cache(cache, cache_file)

    if output_path:
        with open(output_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            writer.writeheader()
            writer.writerows(rows)
        print(f"\nWrote {len(rows)} rows (sorted worst-first) to {output_path}")

    buckets = {"80+": 0, "50-79": 0, "20-49": 0, "<20": 0}
    for r in rows:
        s = r["score"]
        buckets["80+" if s >= 80 else "50-79" if s >= 50 else "20-49" if s >= 20 else "<20"] += 1
    print(f"Score distribution: {buckets}")
    return rows


def main():
    parser = argparse.ArgumentParser(description="Score library files for suspected bad matches.")
    parser.add_argument("--lidarr-url", required=True)
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--download-folder", default="downloads")
    parser.add_argument("--output", default="suspicion.csv", help="CSV report path")
    parser.add_argument("--cache-file", default="lidarr_cache.json",
                        help="Reuse the audit's Lidarr album/track cache")
    parser.add_argument("--min-score", type=int, default=0,
                        help="Only write rows at or above this score (default: 0 = everything)")
    args = parser.parse_args()

    run(
        lidarr_url=args.lidarr_url,
        api_key=args.api_key,
        download_folder=args.download_folder,
        output_path=args.output,
        cache_file=args.cache_file,
        min_score=args.min_score,
    )


if __name__ == "__main__":
    main()
