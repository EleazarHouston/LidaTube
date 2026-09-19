"""Link search: finds a YouTube link for each missing track of an album.

Searches run in stages, cheapest and most precise first: the whole album on
YouTube Music, then each track on YouTube Music, then a wider YouTube Music
search with a plain YouTube search behind it. Saved manual overrides win over
all of them. Transient failures (network errors, YouTube soft-blocks) back off
and retry, and every outcome is recorded in the store with its match trace.
"""

import logging
import threading

import youtubesearchpython
import yt_dlp
from thefuzz import fuzz
from ytmusicapi import YTMusic

import _general
import _matcher
import library_suspicion
from _backoff import BackoffPolicy, call_with_backoff


class LinkSearcher:
    _SEARCH_RETRY_DELAYS = (5, 10, 20, 40)  # back-off between link searches after network errors
    # YouTube soft-blocks last from minutes to hours; while a search waits here it holds the
    # YouTube Music semaphore, which pauses searching instead of burning through the queue.
    _YOUTUBE_BLOCK_RETRY_DELAYS = (60, 300, 900, 1800)

    def __init__(self, config, store, fd_governor, stop_event, logger=None, on_status_change=None):
        self.config = config
        self.store = store
        self.fd = fd_governor
        self.stop_event = stop_event
        self.logger = logger or logging.getLogger(__name__)
        self.on_status_change = on_status_change or (lambda: None)
        self.parallel = max(1, min(2, int(config.thread_limit)))
        self._ytmusic_semaphore = threading.Semaphore(self.parallel)

    def _set_track_link(self, track, link, title, matched_via=None):
        track["link"] = link
        track["title_of_link"] = title
        if matched_via:
            track["_matched_via"] = matched_via

    def _set_track_link_from_video_id(self, track, video_id, title, matched_via=None):
        self._set_track_link(track, f"https://www.youtube.com/watch?v={video_id}", title, matched_via)

    def _record_link_results(self, req_album, session_id, search_error=None):
        """Persist final search outcomes after all matching stages have run.

        Unlinked tracks are recorded as "error" rather than "no_match" when the search
        itself failed, so an outage is not mistaken for YouTube lacking the track.
        """
        if not session_id:
            return
        for track in req_album.get("missing_tracks", []):
            if track.get("_track_result_id"):
                continue
            if track.get("link"):
                outcome = "matched"
            else:
                outcome = "error" if search_error is not None else "no_match"
            suspicion, _ = library_suspicion.score_track({
                "duration_delta_s": None,
                "expected_known": bool(track.get("duration_ms")),
                "is_grab": False,
                "official_art_track": False,
                "provenance_bad_version": False,
                "lidarr_matched": outcome == "matched",
            })
            accepted_scores = [entry.get("score") for entry in track.get("_match_trace", []) if entry.get("rejected_by") == "accepted" and entry.get("score") is not None]
            if accepted_scores:
                suspicion = max(suspicion, int(round(100 - max(accepted_scores))))
            result_id = self.store.record_track_result(
                session_id=session_id,
                artist=track.get("artist", req_album.get("artist")),
                album=req_album.get("album_name"),
                track_title=track.get("track_title"),
                track_number=track.get("track_number"),
                track_id=track.get("track_id"),
                duration_ms=track.get("duration_ms", 0),
                outcome=outcome,
                link=track.get("link") or None,
                title_of_link=track.get("title_of_link") or None,
                matched_via=track.get("_matched_via"),
                suspicion=track.get("suspicion", suspicion),
            )
            if outcome == "no_match":
                self.store.record_evaluations(result_id, track.get("_match_trace", []))
            track["_track_result_id"] = result_id
            track.pop("_match_trace", None)

    def _apply_saved_overrides(self, req_album):
        for track in req_album.get("missing_tracks", []):
            if track.get("link"):
                continue
            override = self.store.get_override(track.get("track_id"))
            if override:
                self._set_track_link(track, override["forced_url"], "Manual override", "yt")

    @staticmethod
    def _search_genres(req_album):
        """Album genres with the artist's genres as a fallback; Lidarr often leaves album genres empty."""
        return ", ".join(value for value in (req_album.get("album_genres"), req_album.get("artist_genres")) if value)

    def _count_found_links(self, req_album):
        return sum(1 for x in req_album["missing_tracks"] if x["link"] != "")

    def _apply_album_track_links(self, req_album, album_details):
        for track in album_details["tracks"]:
            if self.stop_event.is_set():
                return True
            for missing_track in req_album["missing_tracks"]:
                missing_track_title = _general.string_cleaner(missing_track["track_title"])
                song_title = _general.string_cleaner(track["title"])
                if fuzz.ratio(song_title, missing_track_title) > 90:
                    candidate_seconds = track.get("durationSeconds") or 0
                    if not _matcher._duration_ok(missing_track["duration_ms"], candidate_seconds, self.config.duration_tolerance_seconds):
                        continue
                    self._set_track_link_from_video_id(missing_track, track["videoId"], track["title"])
                    break
        return False

    def _close_ytmusic_client(self, ytmusic):
        if ytmusic is None:
            return
        try:
            session = getattr(ytmusic, "_session", None)
            if session is not None:
                session.close()
            else:
                self.logger.warning("YTMusic client has no _session attribute — session may not have been closed")
        except Exception as e:
            self.logger.warning(f"Error closing YTMusic client: {e}")

    def find_links(self, req_album, session_id=None):
        """Find a YouTube link for each missing track and record every outcome under session_id."""
        ytmusic = None
        semaphore_acquired = False
        search_error = None
        try:
            self.logger.warning(f'Searching for: {req_album["artist"]} - {req_album["album_name"]}')

            self.fd.wait_if_pressure()
            while not self.stop_event.is_set():
                if self._ytmusic_semaphore.acquire(timeout=0.5):
                    semaphore_acquired = True
                    break
            if not semaphore_acquired:
                return
            ytmusic = YTMusic()
            self._apply_saved_overrides(req_album)
            self._search_links_with_backoff(req_album, ytmusic)

        except Exception as e:
            search_error = e
            self.logger.error(f"Error in Link Finder: {e}")
            if _general.is_resource_exhaustion_error(e):
                self.fd.signal_exhaustion_in_background()
        finally:
            self._record_link_results(req_album, session_id, search_error=search_error)
            self._close_ytmusic_client(ytmusic)
            if semaphore_acquired:
                self._ytmusic_semaphore.release()

    def _search_backoff_policies(self):
        """Transient search failures and their retry schedules; add a policy to back off on more."""
        return (
            BackoffPolicy("Network error", tuple(self._SEARCH_RETRY_DELAYS), _general.is_network_error),
            BackoffPolicy("YouTube block", tuple(self._YOUTUBE_BLOCK_RETRY_DELAYS), _general.is_youtube_block_error),
        )

    def _search_links_with_backoff(self, req_album, ytmusic):
        """Run all search stages, backing off and retrying on transient failures.

        A container can start searching before its VPN's DNS is ready, and YouTube can
        soft-block the VPN's exit IP for hours; without a retry every album searched in
        those windows is recorded as a permanent miss.
        """
        def on_retry(policy, attempt, delay, error):
            self.logger.warning(
                f'{policy.name} searching {req_album["artist"]} - {req_album["album_name"]}, '
                f"retrying in {delay}s ({attempt}/{len(policy.delays)}): {error}"
            )
            for track in req_album.get("missing_tracks", []):
                track.pop("_match_trace", None)

        call_with_backoff(
            lambda: self._search_links(req_album, ytmusic),
            self._search_backoff_policies(),
            self.stop_event,
            on_retry=on_retry,
        )

    def _search_links(self, req_album, ytmusic):
        artist = req_album["artist"]
        album_name = req_album["album_name"]
        query_text = f"{artist} - {album_name}"
        cleaned_artist = _general.string_cleaner(artist).lower()
        cleaned_album = _general.string_cleaner(album_name).lower()

        if req_album["track_count"] == req_album["missing_count"]:
            self._get_album_links(req_album, artist, album_name, cleaned_artist, cleaned_album, query_text, ytmusic)

        number_of_links = self._count_found_links(req_album)
        if number_of_links == len(req_album["missing_tracks"]):
            req_album["status"] = "All Tracks Found"
            self.on_status_change()
            self.logger.warning(f'Links found for all tracks of: {req_album["artist"]} - {req_album["album_name"]}')
            return

        req_album["status"] = "Searching"
        self.on_status_change()
        continue_with_secondary_search = self._get_song_links(req_album, artist, cleaned_artist, ytmusic)

        number_of_links = self._count_found_links(req_album)
        if number_of_links == len(req_album["missing_tracks"]):
            req_album["status"] = "All Tracks Found"
            self.on_status_change()
            self.logger.warning(f'Links found for all Tracks of: {req_album["artist"]} - {req_album["album_name"]}')
        elif not continue_with_secondary_search:
            self.logger.warning(f'Skipping secondary search due to resource exhaustion: {req_album["artist"]} - {req_album["album_name"]}')
        else:
            self.logger.warning(f'Not all tracks found, searching again: {req_album["artist"]} - {req_album["album_name"]}')
            self._get_song_links_secondary(req_album, artist, cleaned_artist, ytmusic)

    def _get_album_links(self, req_album, artist, album_name, cleaned_artist, cleaned_album, query_text, ytmusic):
        try:
            self.logger.warning(f'Searching for Whole Album: {req_album["artist"]} - {req_album["album_name"]}')
            search_results = ytmusic.search(query=query_text, filter="albums", limit=10)
            self.logger.warning(f'Album search returned {len(search_results)} result(s) for: {query_text}')
            album_match = _matcher.album_matcher(self.config.minimum_match_ratio, artist, album_name, cleaned_artist, cleaned_album, search_results,
                                                 album_secondary_types=req_album.get("album_secondary_types"))

            if album_match:
                self.logger.warning(f'Album match found: {album_match.get("title", album_match.get("browseId"))}')
                req_album["status"] = "Album Found"
                album_details = ytmusic.get_album(album_match["browseId"])
                if self._apply_album_track_links(req_album, album_details):
                    return
            elif self.config.fallback_to_top_result:
                if search_results:
                    self.logger.warning(f'No match — falling back to top result: {search_results[0].get("title", search_results[0].get("browseId"))}')
                    req_album["status"] = "Album Found"
                    album_details = ytmusic.get_album(search_results[0]["browseId"])
                    if self._apply_album_track_links(req_album, album_details):
                        return
                else:
                    self.logger.warning(f'No search results for album: {req_album["artist"]} - {req_album["album_name"]}')
            else:
                self.logger.warning(f'No matching album for: {req_album["artist"]} - {req_album["album_name"]}')

        except Exception as e:
            self.logger.error(f"Error in Album Search: {e}")
            raise

    def _get_song_links(self, req_album, artist, cleaned_artist, ytmusic):
        """Primary song-by-song search. Returns True unless FD exhaustion occurred."""
        try:
            self.logger.warning(f'Searching for individual Tracks: {req_album["artist"]} - {req_album["album_name"]}')
            for missing_track in req_album["missing_tracks"]:
                if self.stop_event.is_set():
                    return True
                if missing_track["link"] == "":
                    override = self.store.get_override(missing_track.get("track_id"))
                    if override:
                        self._set_track_link(missing_track, override["forced_url"], "Manual override", "yt")
                        continue
                    song_title = missing_track["track_title"]
                    cleaned_song_title = _general.string_cleaner(song_title).lower()
                    query_text = f'{missing_track["artist"]} - {song_title}'
                    search_results = ytmusic.search(query=query_text, filter="songs", limit=5)
                    trace = missing_track.setdefault("_match_trace", [])
                    song_match = _matcher.song_matcher(self.config.minimum_match_ratio, artist, cleaned_artist, song_title, cleaned_song_title, search_results,
                                                       expected_duration_ms=missing_track["duration_ms"], duration_tolerance_seconds=self.config.duration_tolerance_seconds, trace=trace,
                                                       album_name=req_album.get("album_name"), album_secondary_types=req_album.get("album_secondary_types"),
                                                       extended_duration_tolerance_seconds=self.config.extended_duration_tolerance_seconds,
                                                       album_genres=self._search_genres(req_album))
                    if song_match:
                        self.logger.warning(f'Track matched: "{song_title}" -> "{song_match["title"]}"')
                        self._set_track_link_from_video_id(missing_track, song_match["videoId"], song_match["title"], "ytmusic")
                    elif self.config.fallback_to_top_result and search_results:
                        self.logger.warning(f'No match — falling back to top result for: "{song_title}" -> "{search_results[0]["title"]}"')
                        self._set_track_link_from_video_id(missing_track, search_results[0]["videoId"], search_results[0]["title"])
                    else:
                        self.logger.warning(f'No match found for track: "{song_title}"')

        except Exception as e:
            self.logger.error(f"Error in Song Search: {e}")
            raise
        return True

    def _get_song_links_secondary(self, req_album, artist, cleaned_artist, ytmusic):
        try:
            self.logger.warning(f'Secondary search for: {req_album["artist"]} - {req_album["album_name"]} (mode: {self.config.secondary_search})')
            for missing_track in req_album["missing_tracks"]:
                if self.stop_event.is_set():
                    return
                if missing_track["link"] == "":
                    song_title = missing_track["track_title"]
                    cleaned_song_title = _general.string_cleaner(song_title).lower()
                    query_text = f'{missing_track["artist"]} - {song_title}'
                    search_results = ytmusic.search(query=query_text, filter="songs", limit=20)
                    trace = missing_track.setdefault("_match_trace", [])
                    song_match = _matcher.song_matcher(self.config.minimum_match_ratio, artist, cleaned_artist, song_title, cleaned_song_title, search_results,
                                                       expected_duration_ms=missing_track["duration_ms"], duration_tolerance_seconds=self.config.duration_tolerance_seconds, trace=trace,
                                                       album_name=req_album.get("album_name"), album_secondary_types=req_album.get("album_secondary_types"),
                                                       extended_duration_tolerance_seconds=self.config.extended_duration_tolerance_seconds,
                                                       album_genres=self._search_genres(req_album))
                    if song_match:
                        self.logger.warning(f'Secondary YTMusic match: "{song_title}" -> "{song_match["title"]}"')
                        self._set_track_link_from_video_id(missing_track, song_match["videoId"], song_match["title"], "ytmusic_secondary")
                    elif self.config.fallback_to_top_result and search_results:
                        self.logger.warning(f'Secondary fallback to top result: "{song_title}" -> "{search_results[0]["title"]}"')
                        self._set_track_link_from_video_id(missing_track, search_results[0]["videoId"], search_results[0]["title"])
                    else:
                        yt_results = self._yt_search(query_text)
                        song_match = _matcher.song_matcher_yt(self.config.minimum_match_ratio, artist, query_text, yt_results,
                                                              expected_duration_ms=missing_track["duration_ms"], duration_tolerance_seconds=self.config.duration_tolerance_seconds, trace=trace,
                                                              album_name=req_album.get("album_name"), album_secondary_types=req_album.get("album_secondary_types"),
                                                              extended_duration_tolerance_seconds=self.config.extended_duration_tolerance_seconds,
                                                              album_genres=self._search_genres(req_album))
                        if song_match:
                            if self.config.secondary_search == "YTS":
                                self.logger.warning(f'YTS match: "{song_title}" -> "{song_match["title"]}"')
                                self._set_track_link(missing_track, song_match["link"], song_match["title"], "yt")
                            elif self.config.secondary_search == "YTDLP":
                                self.logger.warning(f'YTDLP match: "{song_title}" -> "{song_match["title"]}"')
                                self._set_track_link(missing_track, song_match["webpage_url"], song_match["title"], "yt")
                        else:
                            self.logger.warning(f'No match found in secondary search for: "{song_title}"')

            found = self._count_found_links(req_album)
            self.logger.warning(f'Found {found} of the missing {len(req_album["missing_tracks"])} tracks: {req_album["artist"]} - {req_album["album_name"]}')

        except Exception as e:
            self.logger.error(f"Error in Secondary Search: {e}")
            raise

    def _yt_search(self, query_text):
        try:
            if self.config.secondary_search == "YTDLP":
                ydl_opts = {"default_search": "ytsearch10", "quiet": True}
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    search = ydl.extract_info(query_text, download=False)
                    search_results = search.get("entries", [])
            elif self.config.secondary_search == "YTS":
                try:
                    videos_search = youtubesearchpython.VideosSearch(query_text, limit=10)
                    search_results = videos_search.result()["result"]
                except Exception as e:
                    self.logger.warning(f"youtube-search-python failed, falling back to yt-dlp search: {e}")
                    search_results = self._ytdlp_flat_search(query_text)
            else:
                return []

            if self.stop_event.is_set():
                return []
            return search_results

        except Exception as e:
            self.logger.error(f"Error in YouTube Search: {e}")
            raise

    def _ytdlp_flat_search(self, query_text):
        """Search YouTube via yt-dlp without resolving each video, shaped like YTS results (with "link")."""
        ydl_opts = {"default_search": "ytsearch10", "quiet": True, "extract_flat": "in_playlist"}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            entries = ydl.extract_info(query_text, download=False).get("entries") or []
        for entry in entries:
            link = entry.get("webpage_url") or entry.get("url") or ""
            if not link.startswith("http") and entry.get("id"):
                link = f"https://www.youtube.com/watch?v={entry['id']}"
            entry["link"] = link
        return entries
