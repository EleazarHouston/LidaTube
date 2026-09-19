import logging
import tempfile
import yt_dlp
import _general
from _backoff import BackoffPolicy, call_with_backoff

RATE_LIMIT_BACKOFF_SECONDS = [60, 180, 600]
RATE_LIMIT_POLICY = BackoffPolicy(
    "Rate-limited by YouTube",
    tuple(RATE_LIMIT_BACKOFF_SECONDS),
    lambda error: _general.is_rate_limit_error(error) or _general.is_empty_file_error(error),
)
CONSECUTIVE_UNAVAILABLE_THRESHOLD = 5
UNAVAILABLE_BACKOFF_SECONDS = 60


class Downloader:
    def __init__(self, config, stop_event, logger=None):
        self.config = config
        self.stop_event = stop_event
        self.logger = logger or logging.getLogger(__name__)
        self._consecutive_unavailable = 0

    def download(self, link, file_name):
        """Download audio from link. Returns True on success, False on failure."""
        if self.stop_event.is_set():
            return False

        def on_retry(policy, attempt, delay, error):
            self.logger.warning(f"Rate limit detected on attempt {attempt}: {error}")
            self.logger.warning(
                f"{policy.name} — waiting {delay}s before retry {attempt}/{len(policy.delays)}: {link}"
            )

        try:
            call_with_backoff(
                lambda: self._download_once(link, file_name),
                (RATE_LIMIT_POLICY,),
                self.stop_event,
                on_retry=on_retry,
            )
        except Exception as e:
            if self.stop_event.is_set():
                return False
            self.logger.error(f"Error downloading song: {link}. Error: {e}")
            if _general.is_unavailable_error(e):
                self._consecutive_unavailable += 1
                if self._consecutive_unavailable >= CONSECUTIVE_UNAVAILABLE_THRESHOLD:
                    self.logger.warning(
                        f"Back off after {self._consecutive_unavailable} in a row."
                    )
                    self._consecutive_unavailable = 0
                    self.stop_event.wait(UNAVAILABLE_BACKOFF_SECONDS)
            return False
        self.logger.warning(f"DL Complete: {link}")
        self._consecutive_unavailable = 0
        return True

    def _download_once(self, link, file_name):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            ydl_opts = self._get_ydl_opts(file_name, temp_dir)
            with yt_dlp.YoutubeDL(ydl_opts) as downloader:
                downloader.download([link])

    def _get_ydl_opts(self, file_name, temp_dir):
        opts = {
            "logger": self.logger,
            "ffmpeg_location": "/usr/bin/ffmpeg",
            "format": "bestaudio/best",
            "socket_timeout": 30,
            "outtmpl": f"{file_name}.%(ext)s",
            "paths": {"home": self.config.download_folder, "temp": temp_dir},
            "quiet": False,
            "progress_hooks": [self._progress_hook],
            "writethumbnail": True,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": self.config.preferred_codec,
                    "preferredquality": "0",
                },
                {"key": "EmbedThumbnail"},
                {"key": "FFmpegMetadata"},
            ],
        }
        if self.config.cookies_path:
            self.logger.warning(f"Using cookies file: {self.config.cookies_path}")
            opts["cookiefile"] = self.config.cookies_path
        else:
            self.logger.warning("No cookies file configured")
        return opts

    def _progress_hook(self, d):
        if self.stop_event.is_set():
            raise Exception("Cancelled")
        if d["status"] == "finished":
            self.logger.warning("Download complete")
        elif d["status"] == "downloading":
            self.logger.warning(f'Downloaded {d["_percent_str"]} of {d["_total_bytes_str"]} at {d["_speed_str"]}')
