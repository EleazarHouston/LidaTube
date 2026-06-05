# Plan: Duration Validation & Matching Accuracy

> **Ephemeral — do not commit.** This file is working notes only.

---

## 1. Problem Statement

Two classes of bad match exist in the library:

| Class | Example | Root Cause |
|-------|---------|------------|
| **Wrong song, right artist** | A different Drake song grabbed instead of the intended one | Artist always scores 100 (same artist), title fuzzy score is not tight enough to distinguish between songs by the same prolific artist |
| **Wrong song, wrong artist** | Glass Animals track grabbed a non-Glass-Animals video | `song_matcher_yt` (secondary/fallback path) performs **no artist check** — it fuzzy-matches the combined `"Artist - Title"` string against the video title as a single blob |

Both failures would be caught by a duration check. The Glass Animals failure is also independently fixable by adding an artist gate to `song_matcher_yt`.

---

## 2. Code Locations

| File | Role |
|------|------|
| `src/_matcher.py` | All three matchers: `song_matcher`, `album_matcher`, `song_matcher_yt` |
| `src/LidaTube.py` | `get_missing_tracks_for_album` (track dict construction), `_get_song_links`, `_get_song_links_secondary`, `_get_album_links`, `_apply_album_track_links` |
| `src/lidarr_client.py` | `get_tracks_for_album` — issues the `/api/v1/track?albumId=` request |
| `src/config.py` | `AppConfig` — `DEFAULTS`, `_ENV_CONVERTERS`, `save()` |
| `src/_general.py` | `add_metadata` — already imports `mutagen` (useful for scanner) |
| `tests/test_matcher.py` | Existing matcher tests; extend here |
| `tests/test_lidarr_client.py` | Existing client tests; extend here |
| `tests/test_general.py` | Existing general tests |

---

## 3. Prerequisite Verification (do first, before any code)

### 3a. Confirm Lidarr `/api/v1/track` returns `duration`

Make a one-off call from the terminal:

```bash
curl -s "http://<LIDARR_HOST>/api/v1/track?albumId=<SOME_ID>&apikey=<KEY>" | python3 -m json.tool | grep -i "duration\|title" | head -30
```

**Expected:** Each track object contains a `duration` field (integer milliseconds, e.g. `214000` for a 3:34 track).

**If absent:** Lidarr may expose it on the album release level instead. Fall back to querying `/api/v1/album?albumId=X` and extracting per-track durations from the nested release data. Pause and confirm with user before proceeding.

### 3b. YTMusic `duration_seconds` — CONFIRMED ✅

Each result has `duration_seconds` (int seconds, e.g. `199`) and `duration` (string `"3:19"`).
Use `duration_seconds` — already an integer, no parsing needed.

### 3c. YTS `duration` — CONFIRMED ✅

Returns `duration` as `"M:SS"` string (e.g. `"5:57"`). No integer equivalent.
Requires `_parse_duration_string()` helper.
Notable: first result for "Drake - God's Plan" was the 5:57 music video (357s), not the 3:19 song.
Duration filter would correctly reject it against Lidarr's expected ~199s.

### 3d. YTDLP `duration` — CONFIRMED ✅

Returns `duration` as integer seconds (e.g. `357` or `199`). No parsing needed.

---

## 4. Going-Forward Fix: Part 1 — Fix `song_matcher_yt` Artist Gate

**File:** `src/_matcher.py`

### Current bug (lines 139–172)

`song_matcher_yt` receives:
- `query_text` = `"Glass Animals - Heat Waves"`
- A list of YouTube search results, each with a `"title"` field

It fuzzy-matches `query_text` against `result["title"]` as a single string. A video titled `"Heat Waves - Emotional Piano Cover"` would score well because the title substring is present. No check that the artist name appears in the video title (or anywhere else).

### Fix

Add an **artist hard-gate**: extract the artist portion from `query_text` (everything before ` - `), and require that the artist name appears as a case-insensitive substring in the video title. If it doesn't, the result is skipped entirely before scoring.

```python
def song_matcher_yt(minimum_match_ratio, artist, query_text, search_results):
    # artist is now passed explicitly (was not a parameter before)
    ...
    cleaned_artist = _general.string_cleaner(artist).lower()
    for item in search_results:
        title = item.get("title", "")
        # Hard gate: artist must appear in the video title
        if cleaned_artist not in _general.string_cleaner(title).lower():
            continue
        # ... existing scoring logic unchanged
```

**Callers to update:** `LidaTube.py:1070` — pass `artist` as the new first argument.

### Tests to write (`tests/test_matcher.py`)

1. `test_song_matcher_yt_rejects_result_when_artist_absent_from_title` — result with correct title words but missing artist name is rejected even if text score would pass.
2. `test_song_matcher_yt_accepts_result_when_artist_present_in_title` — result containing artist name scores normally.
3. `test_song_matcher_yt_artist_gate_is_case_insensitive` — "glass animals" matches "Glass Animals - Heat Waves".
4. `test_song_matcher_yt_glass_animals_regression` — concrete regression: searching for Glass Animals / Heat Waves, a non-Glass-Animals result is rejected, the correct one is returned.

---

## 5. Going-Forward Fix: Part 2 — Duration Validation

### 5a. Pull `duration_ms` from Lidarr API

**File:** `src/LidaTube.py`, function `get_missing_tracks_for_album` (~line 448)

Current track dict construction:
```python
new_item = {
    "artist": req_album["artist"],
    "track_title": track["title"],
    "track_number": track["trackNumber"],
    "absolute_track_number": track["absoluteTrackNumber"],
    "track_id": track["id"],
    "link": "",
    "title_of_link": "",
}
```

Add:
```python
    "duration_ms": track.get("duration", 0),   # milliseconds from Lidarr; 0 = unknown
```

### 5b. Add `duration_tolerance_seconds` to config

**File:** `src/config.py`

In `DEFAULTS`:
```python
"duration_tolerance_seconds": 15,
```

In `_ENV_CONVERTERS`:
```python
"duration_tolerance_seconds": int,
```

This is the maximum allowable difference (in seconds) between the expected duration from Lidarr and the actual YouTube video duration. Default 15 s covers legitimate variation (fade-ins, silence at end) without being so wide that it misses wrong songs. User can widen it (e.g. 30 s) for albums with unlisted interludes or hidden tracks.

When `duration_ms` is 0 (Lidarr didn't provide it), duration validation is **skipped** for that track — the matcher falls back to text-only matching as today. This prevents regressions for Lidarr setups that don't return duration.

### 5c. Duration comparison helper

**File:** `src/_matcher.py` (or `src/_general.py` — prefer `_matcher.py` since it's already the matching module)

```python
def _duration_ok(expected_ms, candidate_seconds, tolerance_seconds):
    """Return True if candidate duration is within tolerance of expected, or if expected is unknown."""
    if not expected_ms:
        return True   # unknown — don't gate
    expected_seconds = expected_ms / 1000.0
    return abs(candidate_seconds - expected_seconds) <= tolerance_seconds
```

### 5d. Apply to `song_matcher`

**File:** `src/_matcher.py`, `song_matcher` (line 101)

New signature:
```python
def song_matcher(minimum_match_ratio, artist, cleaned_artist, song_title, cleaned_song_title,
                 search_results, item_wanted_type="song",
                 expected_duration_ms=0, duration_tolerance_seconds=15):
```

Inside the loop, after the `resultType` filter and before scoring:
```python
candidate_seconds = item.get("duration_seconds") or 0
if candidate_seconds and not _duration_ok(expected_duration_ms, candidate_seconds, duration_tolerance_seconds):
    continue   # duration mismatch — skip this candidate
```

No change to the scoring logic or threshold. Duration is a hard gate, not a scoring component — a wrong-length song should never win regardless of how good the text looks.

**Callers to update:** `LidaTube.py:1035` and `LidaTube.py:1061` — pass `expected_duration_ms=missing_track["duration_ms"]` and `duration_tolerance_seconds=self.config.duration_tolerance_seconds`.

### 5e. Apply to `song_matcher_yt`

YTS duration is a `"M:SS"` or `"H:MM:SS"` string. Add a parser:

```python
def _parse_duration_string(duration_str):
    """Parse 'M:SS' or 'H:MM:SS' to total seconds. Returns 0 on failure."""
    if not duration_str:
        return 0
    try:
        parts = [int(p) for p in str(duration_str).strip().split(":")]
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
    except (ValueError, AttributeError):
        pass
    return 0
```

New `song_matcher_yt` signature:
```python
def song_matcher_yt(minimum_match_ratio, artist, query_text, search_results,
                    expected_duration_ms=0, duration_tolerance_seconds=15):
```

Inside the loop (after the artist gate, before scoring):
```python
# YTS: duration is string "M:SS"; YTDLP: duration is int seconds
raw_duration = item.get("duration", 0)
if isinstance(raw_duration, str):
    candidate_seconds = _parse_duration_string(raw_duration)
else:
    candidate_seconds = int(raw_duration or 0)

if candidate_seconds and not _duration_ok(expected_duration_ms, candidate_seconds, duration_tolerance_seconds):
    continue
```

**Callers to update:** `LidaTube.py:1070` — pass artist, `expected_duration_ms`, and `duration_tolerance_seconds`.

### 5f. Apply to `album_matcher`

Album-level duration matching is coarser — the YTMusic album result doesn't expose per-track durations directly from the search, only total album duration. The full per-track duration is available only after calling `ytmusic.get_album()`. 

**Decision:** Do **not** add duration filtering to `album_matcher` at the search stage. Instead, apply it during `_apply_album_track_links` when the full album details (including per-track `durationSeconds`) are available. This is covered in section 5g.

### 5g. Apply during album track linking

**File:** `src/LidaTube.py`, `_apply_album_track_links` — find this function and locate where it pairs YTMusic tracks to `missing_tracks`.

When pairing each YTMusic track to a Lidarr track, check duration:
```python
yt_duration_seconds = yt_track.get("durationSeconds") or 0
expected_ms = lidarr_track["duration_ms"]
if not _matcher._duration_ok(expected_ms, yt_duration_seconds, self.config.duration_tolerance_seconds):
    # Don't assign this link — treat as no match found for this track
    continue
```

### Tests to write (`tests/test_matcher.py`)

1. `test_duration_ok_within_tolerance` — 214s expected, 220s actual, tol=15 → True.
2. `test_duration_ok_outside_tolerance` — 214s expected, 260s actual, tol=15 → False.
3. `test_duration_ok_skips_when_expected_unknown` — expected_ms=0 → always True.
4. `test_duration_ok_skips_when_candidate_unknown` — candidate_seconds=0 → True (can't gate on absent data).
5. `test_parse_duration_string_mm_ss` — `"3:54"` → 234.
6. `test_parse_duration_string_hh_mm_ss` — `"1:03:21"` → 3801.
7. `test_parse_duration_string_invalid` — `""`, `None`, `"abc"` → 0.
8. `test_song_matcher_rejects_duration_mismatch` — two candidates, same artist/title text, different durations; only the duration-matched one is returned.
9. `test_song_matcher_passes_when_duration_unknown_on_candidate` — candidate has no `duration_seconds`; should not be rejected.
10. `test_song_matcher_passes_when_lidarr_duration_unknown` — `expected_duration_ms=0`; duration gate does not fire.
11. `test_song_matcher_yt_rejects_duration_mismatch_yts_string` — YTS-style `"M:SS"` string is parsed and compared.
12. `test_song_matcher_yt_rejects_duration_mismatch_ytdlp_int` — YTDLP-style integer seconds is compared directly.
13. `test_song_matcher_drake_regression` — concrete: two Drake song candidates with similar titles but different durations; expected duration matches only one → correct one is returned.

---

## 6. Retroactive Fix: Library Audit Scanner

### 6a. Script: `audit_library.py`

Location: repo root (or `src/` — root is simpler to run directly).

The scanner is a **standalone CLI script**, not integrated into the Flask app. It requires `mutagen` (already a dependency) and `requests`.

### 6b. Logic

```
for each audio file under DOWNLOAD_FOLDER:
    1. Read actual duration via mutagen (works for mp3 and flac)
    2. Read ID3/FLAC tags to get: artist, album, track_title, track_number
    3. Parse filename as fallback if tags are missing
    4. Query Lidarr:
        a. GET /api/v1/artist?term={artist_name} → get artist_id
        b. GET /api/v1/album?artistId={artist_id} → find album by name → get album_id
        c. GET /api/v1/track?albumId={album_id} → find track by number/title → get expected duration_ms
    5. Compare:
        delta_seconds = |actual_duration - (expected_duration_ms / 1000)|
    6. If delta_seconds > THRESHOLD → flag as suspect
```

Results printed as a sorted table (worst offenders first) and optionally written to `audit_results.csv`.

### 6c. CLI interface

```
python audit_library.py \
    --lidarr-url http://192.168.1.2:8686 \
    --api-key YOUR_KEY \
    --tolerance 15 \
    [--output audit_results.csv] \
    [--delete-confirmed]   # only files flagged AND where delta > 60s (extra safety margin)
```

- `--tolerance` defaults to 15 (same as matching tolerance).
- `--delete-confirmed` is gated at 60 s delta minimum regardless of `--tolerance`; always prompts `"Delete N files? [y/N]"` before acting.
- Without `--delete-confirmed`, the script is read-only.

### 6d. Getting actual duration with mutagen

```python
from mutagen.mp3 import MP3
from mutagen.flac import FLAC

def get_audio_duration_seconds(path):
    ext = path.lower().rsplit(".", 1)[-1]
    try:
        if ext == "mp3":
            return MP3(path).info.length
        if ext == "flac":
            return FLAC(path).info.length
    except Exception:
        pass
    return None   # unknown — skip this file
```

### 6e. Lidarr lookup strategy

**Problem:** We know artist name, album name, track title, and track number from the file's tags. We need to map these back to a Lidarr `duration_ms`. The Lidarr API doesn't have a direct "find track by name" endpoint — we must chain: artist → album → tracks.

**Optimisation:** Cache Lidarr responses in memory keyed by album_id so we only fetch each album's track list once per run.

**Fallback:** If Lidarr returns no `duration` (field is 0 or absent), skip that file — don't flag it as suspect on absent data.

**Artist lookup:** The existing `/api/v1/artist` endpoint returns all artists; filter client-side by normalized name. No dedicated search endpoint needed for our use case since the library size is bounded.

### 6f. Output format (CSV + terminal)

```
path, artist, album, track_title, track_num, actual_s, expected_s, delta_s, verdict
/downloads/Drake/Scorpion/..., Drake, Scorpion, God's Plan, 1, 198.4, 278.0, 79.6, SUSPECT
/downloads/Glass Animals/Dreamland/..., Glass Animals, Dreamland, Heat Waves, 8, 122.1, 237.0, 114.9, SUSPECT
```

Terminal output is sorted by `delta_s` descending so worst mismatches are shown first. Files with `delta_s <= tolerance` are shown only with `-v`/`--verbose`.

### 6g. Tests to write (`tests/test_audit_library.py`)

1. `test_get_audio_duration_returns_none_for_unknown_extension` — `.wav` or corrupted file.
2. `test_get_audio_duration_mp3` — mock `mutagen.mp3.MP3`; verify `info.length` is returned.
3. `test_get_audio_duration_flac` — same for FLAC.
4. `test_find_track_in_lidarr_matches_by_track_number` — given mocked track list, find by number.
5. `test_find_track_in_lidarr_falls_back_to_title_match` — no number match, fuzzy title match.
6. `test_audit_flags_file_over_threshold` — delta > tolerance → verdict == SUSPECT.
7. `test_audit_skips_file_when_lidarr_duration_unknown` — `duration_ms == 0` → not flagged.
8. `test_audit_skips_file_when_actual_duration_unknown` — mutagen returns None → not flagged.
9. `test_drake_regression` — delta between two Drake songs (e.g. 80 s) flags as SUSPECT.
10. `test_glass_animals_regression` — delta over 60 s flags as SUSPECT.

---

## 7. Config / Settings UI

The new `duration_tolerance_seconds` setting should be exposed in the Flask settings page so the user can adjust it without editing files. Check `templates/` for the settings form and add the field alongside `minimum_match_ratio`.

---

## 8. Implementation Order

```
Step 0 — Verify prerequisites (terminal API calls — 3a, 3b, 3c, 3d above)
         PAUSE if Lidarr doesn't return duration — ask user before continuing
         PAUSE if YTMusic duration field name differs from expected

Step 1 — Tests for song_matcher_yt artist gate (Part 1 tests)
Step 2 — Implement artist gate in song_matcher_yt + update caller in LidaTube.py
Step 3 — Run tests, confirm pass

Step 4 — Tests for duration helpers (_duration_ok, _parse_duration_string)
Step 5 — Implement helpers in _matcher.py
Step 6 — Add duration_ms to track dict in LidaTube.py (get_missing_tracks_for_album)
Step 7 — Add duration_tolerance_seconds to config.py
Step 8 — Tests for song_matcher with duration gate
Step 9 — Implement duration gate in song_matcher
Step 10 — Tests for song_matcher_yt with duration gate
Step 11 — Implement duration gate in song_matcher_yt (builds on artist gate from Step 2)
Step 12 — Implement duration gate in _apply_album_track_links (album path)
Step 13 — Update all callers in LidaTube.py to pass new parameters
Step 14 — Run full test suite

Step 15 — Write tests for audit_library.py scanner
Step 16 — Implement audit_library.py
Step 17 — Run scanner against real library (requires Lidarr creds) — confirm Drake + Glass Animals flagged
Step 18 — Settings UI for duration_tolerance_seconds
Step 19 — Full regression run
```

---

## 9. Edge Cases and Gotchas

| Case | Handling |
|------|----------|
| Lidarr returns `duration == 0` for a track | Skip duration gate (treat as unknown) |
| YTMusic result has no `duration_seconds` field | Skip duration gate for that candidate |
| Short intros/outros track (< 60 s) | Tolerance of 15 s is proportionally large — consider a minimum-length floor (e.g. if expected < 30 s, skip gate). Add to `_duration_ok` as special case. |
| Multi-part tracks (e.g. "Shine On You Crazy Diamond Pts. 1-5") | Often much longer on YouTube than what Lidarr lists per-part. Consider widening tolerance or setting `duration_ms=0` as opt-out. |
| `fallback_to_top_result=True` | The fallback path in `_get_song_links` assigns the top result without going through a matcher at all — duration gate doesn't apply. This is a known bypass; document in code but do not change behaviour here (the flag exists for a reason). |
| YTDLP duration is already int seconds | No parsing needed; handle the `isinstance(raw_duration, str)` branch carefully. |
| Songs legitimately close in duration | Two Drake songs of similar length with similar names — only the artist gate (Part 1) + title scoring offers defence here. Might still fail. Note in code. |
| Scanner: artist name in tags slightly different from Lidarr | Normalise both with `string_cleaner` before matching. |
| Scanner: track imported under a different album (release variant) | The scanner will not find the track in Lidarr and will skip it. Log as "not found in Lidarr" separately from "flagged suspect". |
| `--delete-confirmed` safety | Always require delta > 60 s (4× default tolerance) AND interactive `y/N` prompt. Never delete based on tolerance alone. |
