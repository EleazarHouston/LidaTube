![Build Status](https://github.com/EleazarHouston/LidaTube/actions/workflows/main.yml/badge.svg)



<img src=src/static/lidatube.png>

LidaTube is a tool for finding and fetching missing Lidarr albums via yt-dlp.

It reads Lidarr's wanted/missing list, finds each missing track on YouTube Music (falling back to YouTube), downloads the audio with yt-dlp and tags it with Lidarr's metadata. Its matcher deliberately prefers returning no match over automatically downloading an uncertain candidate, and it records why every track did or did not match, so misses can be reviewed and corrected from the web UI.

This repository is a fork of [TheWicklowWolf/LidaTube](https://github.com/TheWicklowWolf/LidaTube) focused on cleanup, bug fixes and match accuracy. Upstream project credit remains with TheWicklowWolf. Compared with upstream, this fork adds:

* a stricter matcher (live, version and duration gates) with a per-track decision record,
* a persisted download queue that survives restarts and can be stopped, resumed or reset,
* History and Override panels for reviewing results and forcing a YouTube URL for a track,
* optional staging downloads that Lidarr imports into the library,
* back-off and retry when the network drops or YouTube soft-blocks searches,
* offline tools for finding bad matches already in a library.


## Run using docker-compose

```yaml
services:
  lidatube:
    image: ghcr.io/eleazarhouston/lidatube:latest
    container_name: lidatube
    volumes:
      - /path/to/config:/lidatube/config
      - /data/media/lidatube:/lidatube/downloads
      - /etc/localtime:/etc/localtime:ro
    ports:
      - 5000:5000
    restart: unless-stopped
```

Open `http://<host>:5000`, enter the Lidarr address and API key in the settings (gear icon), then refresh the Lidarr list and select albums to download.

> **Before you point `/lidatube/downloads` at your music library, read [Where downloads go](#where-downloads-go).** By default LidaTube writes straight into whatever folder is mounted there.

Images are published to GHCR for `linux/amd64` and `linux/arm64`: `latest` tracks the main branch and each release is also tagged with its version number (for example `0.0.29`).


## Where downloads go

LidaTube always writes to `/lidatube/downloads/<artist folder>/<Album Title> (<Year>)/`, where `<artist folder>` is the last part of the artist's path in Lidarr. Each file is named `<artist folder> - <album> - <NN> - <YouTube title>.<codec>` and is tagged (title, artist, album artist, album, track number, year, genre) from Lidarr's metadata. A file that already exists is skipped, never overwritten.

There are two ways to use that folder.

**In place (the default).** Mount your Lidarr music root folder at `/lidatube/downloads`. Files land directly in the artist and album folders, which assumes Lidarr's default album folder format `{Album Title} ({Release Year})`. With `library_scan_on_completion` (on by default), Lidarr rescans its root folders when a download session completes. This is simple, but any wrong match goes straight into your library.

**Staging (recommended).** Mount a separate downloads folder at `/lidatube/downloads`, make the same folder visible to Lidarr, and set:

* `lidarr_download_path` to the path at which **Lidarr** sees that folder,
* `attempt_lidarr_import` to `True`.

After each album, LidaTube asks Lidarr to scan `<lidarr_download_path>/<artist folder>/<album folder>` and import what it recognises (a ManualImport "move", the same flow Lidarr's own UI uses). Lidarr then moves the files into the library and applies your naming scheme. For example, if Lidarr mounts `/data/downloads` as `/downloads`:

```yaml
services:
  lidatube:
    image: ghcr.io/eleazarhouston/lidatube:latest
    container_name: lidatube
    environment:
      - lidarr_download_path=/downloads/lidatube
      - attempt_lidarr_import=True
    volumes:
      - /path/to/config:/lidatube/config
      - /data/downloads/lidatube:/lidatube/downloads
      - /etc/localtime:/etc/localtime:ro
    ports:
      - 5000:5000
    restart: unless-stopped
```

Without `attempt_lidarr_import`, staged files stay in the downloads folder until you import them yourself.


## Configuration via environment variables

Certain values can be set via environment variables:

* __PUID__: The user ID to run the app with. Defaults to `1000`.
* __PGID__: The group ID to run the app with. Defaults to `1000`.
* __lidarr_address__: The URL for Lidarr. Defaults to `http://192.168.1.2:8686`.
* __lidarr_api_key__: The API key for Lidarr. Defaults to ``.
* __lidarr_api_timeout__: Timeout in seconds for Lidarr API calls. Defaults to `120`.
* __lidarr_download_path__: Where Lidarr sees LidaTube's downloads folder; setting it turns on staging (see [Where downloads go](#where-downloads-go)). Defaults to `` (download in place).
* __attempt_lidarr_import__: After each album, ask Lidarr to import its downloaded files. Defaults to `False`.
* __library_scan_on_completion__: Ask Lidarr to rescan its root folders when a download session completes. Defaults to `True`.
* __thread_limit__: Number of albums downloaded in parallel. YouTube Music searches are capped at 2 at a time either way. Defaults to `1`.
* __lidarr_scan_thread_limit__: Max worker threads used during the Lidarr refresh (fetching missing tracks per album). Defaults to `16`.
* __batch_size__: Maximum albums loaded and processed per persisted batch. Defaults to `200`.
* __auto_resume__: Resume an interrupted persisted queue when LidaTube starts. Defaults to `True`.
* __sleep_interval__: Seconds to wait after each downloaded track. Defaults to `0`.
* __sync_schedule__: Hours at which to start a sync (comma-separated, 24-hour). Defaults to ``.
* __minimum_match_ratio__: Minimum match score (0–100) a candidate needs to be accepted. Defaults to `90`.
* __duration_tolerance_seconds__: How far a candidate's length may differ from Lidarr's track length. Defaults to `8`.
* __extended_duration_tolerance_seconds__: Wider second-pass window, used only in narrow cases (see [How matching works](#how-matching-works)). Defaults to `30`.
* __fallback_to_top_result__: Use the top search result when nothing passes the matcher. This bypasses every accuracy check below. Defaults to `False`.
* __secondary_search__: How the last-resort YouTube search runs, `YTS` (youtube-search-python, falling back to a yt-dlp search if it fails) or `YTDLP`. Defaults to `YTS`.
* __preferred_codec__: Audio codec to extract. Tags are written for `mp3` and `flac`. Defaults to `mp3`.
* __ULIMIT_NOFILE__: Startup target for open file descriptors (set in `init.sh` via `ulimit -n`). Thread limits are lowered automatically if the limit is too small for them. Defaults to `8192`.

Settings are saved to `config/settings_config.json`. On startup, environment variables take priority over that file, which takes priority over the defaults. The Lidarr address and API key, sleep interval, sync schedule and minimum match ratio can also be changed in the web UI. A UI change to a setting that is also set by an environment variable lasts only until the next restart.


## Sync Schedule

Use a comma-separated list of hours to start sync (e.g. `2, 20` will initiate a sync at 2 AM and 8 PM).
> Note: There is a deadband of up to 10 minutes from the scheduled start time.

A scheduled sync refreshes the wanted list from Lidarr and queues every wanted album for download. Each album starts downloading as soon as its tracks have been scanned, without waiting for the whole refresh.


## Cookies (optional)
To utilize a cookies file with yt-dlp, follow these steps:

* Generate Cookies File: Open your web browser and use a suitable extension (e.g. cookies.txt for Firefox) to extract cookies for a user on YT.

* Save Cookies File: Upload it from the settings dialog, or save it as `cookies.txt` in the config folder.


## How matching works

For each album, LidaTube searches in stages and stops once every missing track has a link:

1. **Manual override.** A YouTube URL you forced for a track (see [Reviewing results](#reviewing-results)) is used as-is.
2. **Whole album on YouTube Music**, only when every track of the album is missing. The album must score at least `minimum_match_ratio`; its tracks are then paired with the missing tracks by title and length.
3. **Each remaining track on YouTube Music** (top 5 song results).
4. **A wider YouTube Music search** (top 20), then a plain **YouTube search** (see `secondary_search`).

Every candidate has to pass these checks:

* **Length** within `duration_tolerance_seconds` of Lidarr's track length. A missing length on either side is not a rejection.
* **Live gate.** A live recording is never accepted for a studio track. When the request is live (the title or album name says so, or Lidarr marks the album as Live), live recordings are preferred.
* **Version gate.** Karaoke, instrumentals, covers, tributes, a cappella, sped-up, slowed, nightcore and 8D uploads are rejected, as are remixes and named versions (e.g. "Solo Version") that the requested title doesn't have. A track that *is* an instrumental or a remix still matches its own version.
* **Score.** Artist credit and title similarity must reach `minimum_match_ratio`. Other non-original recordings (acoustic, demo, orchestral, re-recorded and so on) are ranked below the original but can still be accepted.

If nothing passes, a second pass allows a length difference of up to `extended_duration_tolerance_seconds` (and at most 15% of the track), but only for the same title by the named artist with no edit, version or demo qualifiers. The second pass is skipped for live requests and classical works (a Classical album or artist genre, or titles with opus/BWV/K. numbers or key signatures), where a different length usually means a different performance.

Temporary failures are retried rather than recorded as misses. Network errors are retried after 5, 10, 20 and 40 seconds. When YouTube soft-blocks searches, LidaTube backs off for 1, 5, 15 and then 30 minutes, pausing searches meanwhile. If the failure outlasts the schedule, the tracks are recorded as `error` rather than `no_match`. Downloads back off on rate limits (1, 3 and 10 minutes) and pause for a minute after five unavailable videos in a row.


## Reviewing results

Every search outcome is stored in `config/lidatube.db`: the link chosen, which stage found it, and a heuristic suspicion score. For misses, every candidate is stored with the reason it was rejected. In the web UI:

* **History** lists download sessions. Open one to see each track's outcome and suspicion score, and click a `no_match` track to see the candidates YouTube returned and why each was rejected.
* **Override** lists tracks worth a second look, sorted by suspicion score, and lets you force a YouTube URL for a track. The override is used the next time the album is searched.

Suspicion scores are for triage. A high score does not prove that a file is wrong.


## Download queue recovery

Download selections are stored in `config/lidatube.db` and processed in bounded batches. If the worker restarts mid-session, the unfinished session resumes automatically unless `auto_resume` is disabled. **Stop** halts the run and keeps pending albums for **Resume**. A stopped session stays stopped across restarts until you resume it. **Reset** deletes the persisted queue for that session and clears its progress.

LidaTube currently supports a single application process. The shipped Gunicorn configuration intentionally uses one worker; running multiple workers against the SQLite queue database is unsupported.


## Security

LidaTube has no login. Anyone who can reach port 5000 can change its settings, start or stop downloads, upload a cookies file, and read the Lidarr API key from the settings dialog. Run it only on a trusted network, or put it behind a reverse proxy that requires authentication.


## Library audit tools

Two command-line tools look for bad matches that are already in a library. Both read Lidarr's expected track lengths through its API and cache them in `--cache-file`, so a rerun is cheap:

* `audit_library.py` compares each file's actual length with Lidarr's and lists files outside `--tolerance` seconds, largest difference first. With `--output`, it writes every checked file to a CSV and resumes from it if interrupted. With `--delete-confirmed`, it offers (interactively) to delete files at least `--delete-threshold` seconds off.
* `library_suspicion.py` gives every file a 0–100 suspicion score from the length difference, the YouTube upload details yt-dlp embedded in the file (an official "Provided to YouTube by …" upload lowers the score) and version words such as karaoke or instrumental, and writes a CSV sorted worst-first.

Run them inside the container, writing results to the config volume:

```bash
docker exec -it lidatube python /lidatube/src/library_suspicion.py \
    --lidarr-url http://192.168.1.2:8686 --api-key YOUR_KEY \
    --download-folder /lidatube/downloads \
    --cache-file /lidatube/config/audit_cache.json \
    --output /lidatube/config/suspicion.csv
```

Run either tool with `--help` for all options. Neither changes files unless you use `--delete-confirmed` and confirm each deletion.


## Development

### Running tests

```bash
python -m pip install -r requirements-dev.txt
python -m pytest
```

The tests need no network access, Lidarr or YouTube.

### Running locally

```bash
cd src
RELEASE_VERSION=dev python LidaTube.py
```

This serves the UI on port 5000 and creates `config/` and `downloads/` in the working directory. In the container, Gunicorn serves `src.LidaTube:app` with the single gevent-websocket worker from `gunicorn_config.py`.

### Project layout

| Module | Responsibility |
|---|---|
| `LidaTube.py` | Entry point: creates the Flask/Socket.IO app, the `DataHandler` and the routes. |
| `web.py` | HTTP API routes and Socket.IO event handlers. |
| `data_handler.py` | Composition root: builds and wires the components, and serves settings and connection events. |
| `lidarr_scan.py` | `LidarrScanner`: fetches wanted albums and their missing tracks, tracks scan state, keeps the scan cache. |
| `download_queue.py` | `DownloadQueue`: persisted download sessions, batches, stop/resume/reset, and each album's download, tagging and import. |
| `link_search.py` | `LinkSearcher`: the search stages, retries and recorded outcomes. |
| `_matcher.py` | Candidate scoring and the live, version and duration gates. |
| `scheduler.py` | `SyncScheduler`: starts scheduled syncs. |
| `downloader.py` | yt-dlp download with rate-limit back-off. |
| `lidarr_client.py` | Lidarr API calls, including album import and library rescan. |
| `store.py` | SQLite persistence: sessions, the queue, track results, candidate evaluations and overrides. |
| `config.py` | Settings from environment variables, the settings file and defaults. |
| `fd_governor.py` | Keeps worker pools within the open-file limit and backs off when it is nearly used up. |
| `_backoff.py`, `_general.py` | Shared retry policy; naming, tagging and error-classification helpers. |
| `audit_library.py`, `library_suspicion.py` | The offline library audit tools. |

---

<img src=src/static/light.png>


<img src=src/static/dark.png>


https://github.com/EleazarHouston/LidaTube/pkgs/container/lidatube
