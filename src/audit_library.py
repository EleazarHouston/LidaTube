"""
audit_library.py — Retroactive library scanner.

Walks the download folder, compares each file's actual audio duration against
what Lidarr says it should be, and reports suspected bad matches.

Usage:
    python audit_library.py \\
        --lidarr-url http://192.168.1.2:8686 \\
        --api-key YOUR_KEY \\
        [--download-folder downloads] \\
        [--tolerance 15] \\
        [--output audit_results.csv] \\
        [--delete-confirmed]
"""

import argparse
import csv
import os
import sys

import requests
from mutagen.flac import FLAC
from mutagen.id3 import ID3
from mutagen.mp3 import MP3
from thefuzz import fuzz

SUPPORTED_EXTENSIONS = {".mp3", ".flac"}


# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------

def get_audio_duration_seconds(path):
    """Return audio duration in seconds, or None if unreadable/unsupported."""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".mp3":
            return MP3(path).info.length
        if ext == ".flac":
            return FLAC(path).info.length
    except Exception:
        pass
    return None


def read_tags(path):
    """Read ID3/FLAC tags and return a dict, or None on failure."""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".mp3":
            tags = ID3(path)
            return {
                "artist": _id3_text(tags, "TPE1"),
                "album": _id3_text(tags, "TALB"),
                "title": _id3_text(tags, "TIT2"),
                "track_number": _parse_track_number(_id3_text(tags, "TRCK", "0")),
            }
        if ext == ".flac":
            audio = FLAC(path)
            t = audio.tags or {}
            return {
                "artist": (t.get("artist") or [""])[0],
                "album": (t.get("album") or [""])[0],
                "title": (t.get("title") or [""])[0],
                "track_number": _parse_track_number((t.get("tracknumber") or ["0"])[0]),
            }
    except Exception:
        pass
    return None


def _id3_text(tags, key, default=""):
    frame = tags.get(key)
    if frame is None:
        return default
    if hasattr(frame, "text") and frame.text:
        return str(frame.text[0])
    return default


def _parse_track_number(raw):
    try:
        return int(str(raw).split("/")[0].strip())
    except (ValueError, AttributeError):
        return 0


# ---------------------------------------------------------------------------
# Lidarr lookup helpers
# ---------------------------------------------------------------------------

def find_track_in_lidarr(tracks, track_number, title):
    """Find a track from a Lidarr track list by number (preferred) then title."""
    if track_number:
        for t in tracks:
            if t.get("trackNumber") == track_number:
                return t

    # Fall back to fuzzy title match
    best_score = 0
    best = None
    for t in tracks:
        score = fuzz.ratio((title or "").lower(), t.get("title", "").lower())
        if score > best_score:
            best_score = score
            best = t

    if best_score >= 80:
        return best
    return None


def _get_json(session, url, params, timeout=120):
    resp = session.get(url, params=params, timeout=timeout)
    if resp.status_code == 200:
        return resp.json()
    return None


def _find_artist_id(session, lidarr_url, api_key, artist_name):
    data = _get_json(session, f"{lidarr_url}/api/v1/artist", {"apikey": api_key}, timeout=240)
    if not data:
        return None
    cleaned = artist_name.lower()
    for a in data:
        if a.get("artistName", "").lower() == cleaned:
            return a["id"]
    # Fuzzy fallback
    best_score, best_id = 0, None
    for a in data:
        score = fuzz.ratio(cleaned, a.get("artistName", "").lower())
        if score > best_score:
            best_score, best_id = score, a["id"]
    return best_id if best_score >= 80 else None


def _find_album_id(session, lidarr_url, api_key, artist_id, album_name):
    data = _get_json(session, f"{lidarr_url}/api/v1/album", {"apikey": api_key, "artistId": artist_id}, timeout=240)
    if not data:
        return None
    cleaned = album_name.lower()
    best_score, best_id = 0, None
    for a in data:
        score = fuzz.ratio(cleaned, a.get("title", "").lower())
        if score > best_score:
            best_score, best_id = score, a["id"]
    return best_id if best_score >= 80 else None


def _get_tracks(session, lidarr_url, api_key, album_id):
    return _get_json(session, f"{lidarr_url}/api/v1/track", {"apikey": api_key, "albumId": album_id}, timeout=240) or []


# ---------------------------------------------------------------------------
# Per-file audit
# ---------------------------------------------------------------------------

def audit_file(path, session, lidarr_url, api_key, tolerance=15, _cache=None):
    """
    Audit a single audio file against Lidarr expected duration.

    Returns a result dict, or None if the file should be skipped
    (unreadable tags, unknown duration on either side, track not found in Lidarr).
    """
    if _cache is None:
        _cache = {}

    tags = read_tags(path)
    if not tags:
        return None

    actual_seconds = get_audio_duration_seconds(path)
    if actual_seconds is None:
        return None

    artist_name = tags.get("artist", "")
    album_name = tags.get("album", "")
    track_number = tags.get("track_number")
    title = tags.get("title", "")

    cache_key = (artist_name.lower(), album_name.lower())
    if cache_key not in _cache:
        artist_id = _find_artist_id(session, lidarr_url, api_key, artist_name)
        if artist_id is None:
            _cache[cache_key] = []
        else:
            album_id = _find_album_id(session, lidarr_url, api_key, artist_id, album_name)
            if album_id is None:
                _cache[cache_key] = []
            else:
                _cache[cache_key] = _get_tracks(session, lidarr_url, api_key, album_id)

    tracks = _cache[cache_key]
    track = find_track_in_lidarr(tracks, track_number, title)
    if track is None:
        return None

    expected_ms = track.get("duration", 0)
    if not expected_ms:
        return None

    expected_seconds = expected_ms / 1000.0
    delta = abs(actual_seconds - expected_seconds)
    verdict = "SUSPECT" if delta > tolerance else "OK"

    return {
        "path": path,
        "artist": artist_name,
        "album": album_name,
        "title": title,
        "track_number": track_number,
        "actual_s": round(actual_seconds, 1),
        "expected_s": round(expected_seconds, 1),
        "delta_s": round(delta, 1),
        "verdict": verdict,
    }


# ---------------------------------------------------------------------------
# Walk and report
# ---------------------------------------------------------------------------

def walk_library(download_folder):
    for root, _, files in os.walk(download_folder):
        for fname in files:
            if os.path.splitext(fname)[1].lower() in SUPPORTED_EXTENSIONS:
                yield os.path.join(root, fname)


FIELDNAMES = ["path", "artist", "album", "title", "track_number", "actual_s", "expected_s", "delta_s", "verdict"]


def _load_existing_results(output_path):
    """Read an existing CSV and return (already_done_paths, suspects_so_far, ok_count)."""
    already_done = set()
    suspects = []
    ok_count = 0
    if not (output_path and os.path.exists(output_path)):
        return already_done, suspects, ok_count
    with open(output_path, newline="") as f:
        for row in csv.DictReader(f):
            already_done.add(row["path"])
            if row["verdict"] == "SUSPECT":
                suspects.append({
                    **row,
                    "actual_s": float(row["actual_s"]),
                    "expected_s": float(row["expected_s"]),
                    "delta_s": float(row["delta_s"]),
                    "track_number": int(row["track_number"]),
                })
            elif row["verdict"] == "OK":
                ok_count += 1
    return already_done, suspects, ok_count


def run_audit(lidarr_url, api_key, download_folder, tolerance, output_path, delete_confirmed):
    session = requests.Session()
    cache = {}
    skipped = 0

    already_done, suspects, ok_count = _load_existing_results(output_path)
    if already_done:
        print(f"Resuming: {len(already_done)} files already processed, {len(suspects)} suspects found so far.")

    files = list(walk_library(download_folder))
    total = len(files)
    remaining = [p for p in files if p not in already_done]
    start_index = len(already_done)
    print(f"Scanning {len(remaining)} files ({start_index} already done, {total} total) in '{download_folder}'...")

    csv_file = None
    csv_writer = None
    if output_path:
        is_new = not already_done
        csv_file = open(output_path, "w" if is_new else "a", newline="")
        csv_writer = csv.DictWriter(csv_file, fieldnames=FIELDNAMES)
        if is_new:
            csv_writer.writeheader()
            csv_file.flush()

    try:
        for i, path in enumerate(remaining, start_index + 1):
            if i % 50 == 0 or i == total:
                print(f"  {i}/{total} scanned, {len(suspects)} suspects so far...", flush=True)

            result = audit_file(path, session, lidarr_url, api_key, tolerance=tolerance, _cache=cache)
            if result is None:
                skipped += 1
            else:
                if csv_writer:
                    csv_writer.writerow(result)
                    csv_file.flush()
                if result["verdict"] == "SUSPECT":
                    suspects.append(result)
                else:
                    ok_count += 1
    finally:
        if csv_file:
            csv_file.close()

    suspects.sort(key=lambda r: r["delta_s"], reverse=True)

    print(f"\nResults: {len(suspects)} SUSPECT, {ok_count} OK, {skipped} skipped (no Lidarr match or unknown duration)")
    if output_path:
        print(f"Full results in {output_path}")
    print()

    if suspects:
        print(f"{'Delta':>8}  {'Expected':>10}  {'Actual':>8}  Path")
        print("-" * 80)
        for r in suspects:
            print(f"{r['delta_s']:>7.1f}s  {r['expected_s']:>8.1f}s  {r['actual_s']:>6.1f}s  {r['path']}")

    if delete_confirmed and suspects:
        # Extra safety: only offer deletion for files >60s off regardless of --tolerance
        deletable = [r for r in suspects if r["delta_s"] > 60]
        if not deletable:
            print("\nNo files exceed the 60s safety threshold for deletion.")
        else:
            print(f"\n{len(deletable)} file(s) exceed 60s delta and are candidates for deletion:")
            for r in deletable:
                print(f"  [{r['delta_s']:.0f}s off] {r['path']}")
            answer = input(f"Delete these {len(deletable)} file(s)? [y/N] ").strip().lower()
            if answer == "y":
                for r in deletable:
                    os.remove(r["path"])
                    print(f"  Deleted: {r['path']}")
            else:
                print("Deletion cancelled.")

    return suspects


def main():
    parser = argparse.ArgumentParser(description="Audit music library for duration mismatches against Lidarr.")
    parser.add_argument("--lidarr-url", required=True)
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--download-folder", default="downloads")
    parser.add_argument("--tolerance", type=int, default=15, help="Seconds of allowed deviation (default: 15)")
    parser.add_argument("--output", help="Write CSV report to this path")
    parser.add_argument("--delete-confirmed", action="store_true", help="Offer to delete files with delta > 60s")
    args = parser.parse_args()

    run_audit(
        lidarr_url=args.lidarr_url,
        api_key=args.api_key,
        download_folder=args.download_folder,
        tolerance=args.tolerance,
        output_path=args.output,
        delete_confirmed=args.delete_confirmed,
    )


if __name__ == "__main__":
    main()
