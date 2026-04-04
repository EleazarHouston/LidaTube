import logging
import tempfile
import yt_dlp
import _general

RATE_LIMIT_BACKOFF_SECONDS = [60, 180, 600]
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
        for attempt in range(len(RATE_LIMIT_BACKOFF_SECONDS) + 1):
            if attempt > 0:
                backoff = RATE_LIMIT_BACKOFF_SECONDS[attempt - 1]
                self.logger.warning(
                    f"Rate-limited by YouTube — waiting {backoff}s before retry "
                    f"{attempt}/{len(RATE_LIMIT_BACKOFF_SECONDS)}: {link}"
                )
                if self.stop_event.wait(backoff):
                    return False
            try:
                with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
                    ydl_opts = self._get_ydl_opts(file_name, temp_dir)
                    with yt_dlp.YoutubeDL(ydl_opts) as downloader:
                        downloader.download([link])
                self.logger.warning(f"DL Complete: {link}")
                self._consecutive_unavailable = 0
                return True
            except Exception as e:
                if _general.is_rate_limit_error(e) and attempt < len(RATE_LIMIT_BACKOFF_SECONDS):
                    self.logger.warning(f"Rate limit detected on attempt {attempt + 1}: {e}")
                    continue
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
        return False  # all retries exhausted

    def _get_ydl_opts(self, file_name, temp_dir):
        opts = {
            "logger": self.logger,
            "ffmpeg_location": "/usr/bin/ffmpeg",
            "format": "bestaudio",
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
