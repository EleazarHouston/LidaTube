"""
requeue_albums.py — Put albums back on a running LidaTube's download queue.

Albums come from a file, from --album, or both (--album then narrows the file). A file
line is either a Lidarr album id or a JSON object naming one, with an optional track id:
{"album": <Lidarr album id>, "tid": <Lidarr track id>, "cat": <category>}. album_id and
track_id are read too. Lines with a "cat" other than --cat are skipped. With track ids,
only those tracks are wanted, and only while Lidarr still lacks a file for them.

Albums are added to the running session, or else to the one waiting to be resumed;
nothing is started. Re-running a file is safe: albums already queued are not repeated.

Usage (inside the container):
    python3 src/requeue_albums.py lost.jsonl --album 41749      # one album first
    python3 src/requeue_albums.py lost.jsonl [--front] [--url http://127.0.0.1:5000]

Exit status: 0 when every album was accepted, 1 when some lines or albums could not be
used, 2 when a request failed.
"""

import argparse
import json
import sys

import requests

ALBUM_FIELDS = ("album", "album_id", "albumId")
TRACK_FIELDS = ("tid", "track_id", "trackId")
# The server accepts no more than this in one request.
MAX_BATCH = 500


def _integer(entry, fields):
    for field in fields:
        value = entry.get(field)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def read_albums(lines, category=None):
    """Returns ({album id: set of track ids}, skipped line count, invalid line count)."""
    albums, skipped, invalid = {}, 0, 0
    for line in lines:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            entry = None
        if isinstance(entry, int) and not isinstance(entry, bool):
            entry = {"album": entry}
        album_id = _integer(entry, ALBUM_FIELDS) if isinstance(entry, dict) else None
        if album_id is None:
            invalid += 1
            continue
        if category is not None and entry.get("cat", category) != category:
            skipped += 1
            continue
        tracks = albums.setdefault(album_id, set())
        track_id = _integer(entry, TRACK_FIELDS)
        if track_id is not None:
            tracks.add(track_id)
    return albums, skipped, invalid


def _request_entry(album_id, tracks):
    return {"album_id": album_id, "track_ids": sorted(tracks)} if tracks else {"album_id": album_id}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Put albums back on LidaTube's download queue.")
    parser.add_argument("file", nargs="?", help="Album ids, or JSONL naming an album (and track) per line")
    parser.add_argument("--album", type=int, action="append", default=[], help="Only this Lidarr album id (repeatable)")
    parser.add_argument("--cat", default="no trace", help="Category to take from lines that have one (default: %(default)s)")
    parser.add_argument("--any-cat", action="store_true", help="Take lines of every category")
    parser.add_argument("--all-missing", action="store_true", help="Want every missing track, not only the listed ones")
    parser.add_argument("--front", action="store_true", help="Queue ahead of everything pending (default: behind it)")
    parser.add_argument("--limit", type=int, help="Take only the first N albums")
    parser.add_argument("--batch", type=int, default=200, help="Albums per request, at most 500 (default: %(default)s)")
    parser.add_argument("--dry-run", action="store_true", help="Count what would be queued without sending it")
    parser.add_argument("--url", default="http://127.0.0.1:5000", help="LidaTube base URL (default: %(default)s)")
    parser.add_argument("--timeout", type=float, default=600.0, help="Request timeout in seconds (default: %(default)s)")
    args = parser.parse_args(argv)
    if not args.file and not args.album:
        parser.error("give a file, --album, or both")
    if not 1 <= args.batch <= MAX_BATCH:
        parser.error(f"--batch must be between 1 and {MAX_BATCH}")

    skipped = invalid = 0
    if args.file:
        with open(args.file, encoding="utf-8") as handle:
            albums, skipped, invalid = read_albums(handle, None if args.any_cat else args.cat)
        if args.album:
            absent = [album_id for album_id in args.album if album_id not in albums]
            if absent:
                print(f"Not in the file (or filtered out): {absent}", file=sys.stderr)
                invalid += len(absent)
            albums = {album_id: albums[album_id] for album_id in args.album if album_id in albums}
    else:
        albums = {album_id: set() for album_id in args.album}
    if args.all_missing:
        albums = {album_id: set() for album_id in albums}
    entries = [_request_entry(album_id, tracks) for album_id, tracks in albums.items()][:args.limit]

    if args.dry_run:
        tracks = sum(len(entry.get("track_ids", ())) for entry in entries)
        print(json.dumps({"albums": len(entries), "tracks": tracks, "skipped": skipped, "invalid": invalid}))
        return 1 if invalid else 0

    batches = [entries[start:start + args.batch] for start in range(0, len(entries), args.batch)]
    if args.front:
        batches.reverse()  # each batch goes ahead of the previous one, so this keeps the file's order
    totals = {"session_id": None, "added": 0, "updated": 0, "already_queued": 0, "in_progress": 0, "failed": []}
    for number, batch in enumerate(batches, start=1):
        try:
            response = requests.post(
                f"{args.url.rstrip('/')}/api/queue/requeue",
                json={"albums": batch, "front": args.front},
                timeout=args.timeout,
            )
            payload = response.json()
        except (requests.RequestException, ValueError) as error:
            print(f"Batch {number} of {len(batches)} failed: {error}", file=sys.stderr)
            print(json.dumps(totals))
            return 2
        if response.status_code != 200:
            print(f"Batch {number} of {len(batches)} failed ({response.status_code}): "
                  f"{payload.get('error', payload)}", file=sys.stderr)
            print(json.dumps(totals))
            return 2
        totals["session_id"] = payload["session_id"] or totals["session_id"]
        for key in ("added", "updated", "already_queued", "in_progress"):
            totals[key] += payload[key]
        totals["failed"] += payload["failed"]
        print(f"Batch {number} of {len(batches)} sent", file=sys.stderr)
    print(json.dumps(totals))
    return 1 if invalid or totals["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
