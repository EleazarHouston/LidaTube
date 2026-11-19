"""The verification gate between yt-dlp and Lidarr: keep, reject or hold each download.

A download is fingerprinted and judged (see audio_verify) before Lidarr is asked to import
it. A rejected file is deleted and its video is never offered for that track again. A file
that cannot be judged because AcoustID or MusicBrainz is unreachable is held outside the
download folder, so Lidarr never imports it unverified, and is checked again later.
"""

import logging
import os
import shutil
import threading
from datetime import datetime, timedelta, timezone

import _general
from audio_verify import AcoustIdClient, MusicBrainzClient, VerificationUnavailable, Verifier

USER_AGENT = "LidaTube/{} ( https://github.com/EleazarHouston/LidaTube )"
HELD_FOLDER = "pending_verification"
# A kept file whose audio was not confirmed surfaces on the Override list for review.
SUSPICION = {"verified": None, "title_only": 20, "no_match": 75, "variant": 60, "weak": 50, "flagged": 50, "pending": 50, "rejected": 90, "truncated": None}
RETRY_POLL_SECONDS = 60


def default_verifier_factory(api_key):
    version = os.environ.get("RELEASE_VERSION", "unknown")
    return Verifier(AcoustIdClient(api_key), MusicBrainzClient(USER_AGENT.format(version)))


class VerificationGate:
    def __init__(self, config, store, lidarr_client, logger=None, verifier_factory=None, clock=None):
        self.config = config
        self.store = store
        self.lidarr_client = lidarr_client
        self.logger = logger or logging.getLogger(__name__)
        self.verifier_factory = verifier_factory or default_verifier_factory
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._verifier = None
        self._verifier_key = None
        self._lock = threading.Lock()

    @property
    def enabled(self):
        return bool(self.config.audio_verification and self.config.acoustid_api_key)

    def _current_verifier(self):
        # Rebuilt when the key changes in the settings; the clients' rate limits live inside it.
        with self._lock:
            if self._verifier is None or self._verifier_key != self.config.acoustid_api_key:
                self._verifier = self.verifier_factory(self.config.acoustid_api_key)
                self._verifier_key = self.config.acoustid_api_key
            return self._verifier

    def _judge(self, path, track, album):
        try:
            return self._current_verifier().verify(path, track, album), None
        except VerificationUnavailable as error:
            return None, str(error)
        except Exception as error:
            # An unexpected failure is no licence to import an unverified file.
            return None, f"verification failed: {type(error).__name__}: {error}"

    def check(self, path, track, album):
        """Judge a downloaded file: "accepted" (left in place), "rejected" (deleted) or "held"."""
        if not track.get("recording_id") and track.get("track_id") is not None:
            # Albums queued before tracks carried their recording id; the id and its works
            # are what keep renamed soundtrack and classical recordings from being rejected.
            track["recording_id"] = self.lidarr_client.get_recording_id(track["track_id"])
        verdict, error = self._judge(path, track, album)
        if verdict is None:
            self._hold(path, track, album, error)
            return "held"
        if verdict.accepted:
            self._record(track, verdict.status)
            if verdict.status != "verified":
                self.logger.warning(f'Kept "{track.get("track_title")}" unconfirmed ({verdict.status}): {verdict.reason}')
            return "accepted"
        self._reject(path, track, verdict)
        return "rejected"

    def _record(self, track, status):
        result_id = track.get("_track_result_id")
        if result_id:
            self.store.set_verification(result_id, status, minimum_suspicion=SUSPICION.get(status))

    def _reject(self, path, track, verdict):
        _remove(path)
        if verdict.status == "truncated":
            # The download broke, not the video: leave the video free to be downloaded again.
            self._record(track, "truncated")
            self.logger.warning(f'Discarded truncated download of "{track.get("track_title")}": {verdict.reason}')
            return
        video_id = _general.youtube_video_id(track.get("link"))
        heard = "; ".join(verdict.top_titles) or "nothing identifiable"
        if video_id and track.get("track_id") is not None:
            self.store.reject_video(track["track_id"], video_id, "acoustid_mismatch", "acoustid", detail=f"AcoustID heard: {heard}")
        self._record(track, "rejected")
        self.logger.warning(f'Rejected download of "{track.get("track_title")}": {verdict.reason} ({heard})')

    def _held_path(self, path):
        relative = os.path.relpath(os.path.abspath(path), os.path.abspath(self.config.download_folder))
        if relative.startswith(".."):
            relative = os.path.basename(path)
        return os.path.join(self.config.CONFIG_FOLDER, HELD_FOLDER, relative)

    def _next_attempt(self):
        return (self.clock() + timedelta(minutes=float(self.config.verification_retry_minutes))).isoformat()

    def _hold(self, path, track, album, error):
        held = self._held_path(path)
        os.makedirs(os.path.dirname(held), exist_ok=True)
        shutil.move(path, held)
        stored_track = {key: value for key, value in track.items() if not key.startswith("_") or key == "_track_result_id"}
        self.store.add_pending_verification(
            track_result_id=track.get("_track_result_id"), track_id=track.get("track_id"),
            video_id=_general.youtube_video_id(track.get("link")), held_path=held, final_path=path,
            track=stored_track, album=_album_summary(album), error=error, next_attempt_at=self._next_attempt(),
        )
        self._record(track, "pending")
        self.logger.warning(f'Holding "{track.get("track_title")}" out of Lidarr until it can be verified: {error}')

    def retry_due(self):
        """Judge every held file whose retry time has come; returns counts by outcome."""
        counts = {"accepted": 0, "rejected": 0, "held": 0, "missing": 0}
        if not self.enabled:
            return counts
        for pending in self.store.due_pending_verifications(self.clock().isoformat()):
            held, track = pending["held_path"], pending["track"]
            if not os.path.exists(held):
                self.store.remove_pending_verification(pending["id"])
                counts["missing"] += 1
                continue
            verdict, error = self._judge(held, track, pending["album"])
            if verdict is None:
                self.store.retry_pending_verification(pending["id"], error, self._next_attempt())
                counts["held"] += 1
            elif verdict.accepted:
                os.makedirs(os.path.dirname(pending["final_path"]), exist_ok=True)
                shutil.move(held, pending["final_path"])
                self._record(track, verdict.status)
                self.store.remove_pending_verification(pending["id"])
                counts["accepted"] += 1
                self.logger.warning(f'Verified held file "{track.get("track_title")}" ({verdict.status}); importing')
                if self.config.attempt_lidarr_import:
                    self.lidarr_client.import_album(pending["album"], only_files=[os.path.basename(pending["final_path"])])
            else:
                self._reject(held, track, verdict)
                self.store.remove_pending_verification(pending["id"])
                counts["rejected"] += 1
        return counts

    def start(self):
        """Re-verify held files in the background for the life of the process."""
        self._stop = threading.Event()
        threading.Thread(target=self.run, args=(self._stop,), daemon=True, name="verification-retry").start()

    def log_status(self):
        if not self.config.audio_verification:
            self.logger.warning("Audio verification is switched off: downloads go to Lidarr unchecked")
        elif not self.config.acoustid_api_key:
            self.logger.warning("Audio verification is off: set an AcoustID API key to check downloads before import")
        elif shutil.which("fpcalc") is None:
            self.logger.error("Audio verification is on but fpcalc (Chromaprint) is missing: every download will be held")
        else:
            self.logger.warning("Audio verification is on: downloads are checked with AcoustID before import")

    def run(self, stop_event):
        """Background loop: re-verify held files until stop_event is set."""
        while not stop_event.wait(RETRY_POLL_SECONDS):
            try:
                self.retry_due()
            except Exception as error:
                self.logger.error(f"Error re-verifying held downloads: {error}")


def _album_summary(album):
    keys = ("artist", "album_name", "artist_path", "album_folder", "album_full_path", "album_id", "album_secondary_types")
    return {key: album.get(key) for key in keys if key in album}


def _remove(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
