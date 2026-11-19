"""
reject_videos.py — Bulk-reject YouTube videos per track in a running LidaTube.

Each line of the JSONL file is {"track_id": <Lidarr track id>, "video_id": <id or URL>,
"reason": <text>}. Re-running a file is safe: videos already rejected are counted as
duplicates and keep their first reason. The file goes to the app's import API rather than
straight into the database, because opening the database marks any running session as
interrupted.

Usage (inside the container):
    python3 src/reject_videos.py rejects.jsonl [--url http://127.0.0.1:5000] [--source bulk]

Exit status: 0 when every line was accepted, 1 when some lines were invalid, 2 when the
import failed.
"""

import argparse
import json
import sys

import requests


def main(argv=None):
    parser = argparse.ArgumentParser(description="Reject YouTube videos per track from a JSONL file.")
    parser.add_argument("file", help="JSONL file of {track_id, video_id, reason} lines")
    parser.add_argument("--url", default="http://127.0.0.1:5000", help="LidaTube base URL (default: %(default)s)")
    parser.add_argument("--source", default="import", help="Recorded as each rejection's source (default: %(default)s)")
    parser.add_argument("--timeout", type=float, default=120.0, help="Request timeout in seconds (default: %(default)s)")
    args = parser.parse_args(argv)

    with open(args.file, "rb") as handle:
        body = handle.read()
    try:
        response = requests.post(
            f"{args.url.rstrip('/')}/api/rejected_videos/import",
            data=body,
            headers={"Content-Type": "application/x-ndjson"},
            timeout=args.timeout,
            params={"source": args.source},
        )
        payload = response.json()
    except (requests.RequestException, ValueError) as error:
        print(f"Import failed: {error}", file=sys.stderr)
        return 2
    if response.status_code != 200:
        print(f"Import failed ({response.status_code}): {payload.get('error', payload)}", file=sys.stderr)
        return 2
    print(json.dumps(payload))
    return 1 if payload.get("invalid") else 0


if __name__ == "__main__":
    sys.exit(main())
