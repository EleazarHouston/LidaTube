import logging
import tempfile
import yt_dlp


class Downloader:
    def __init__(self, config, stop_event, logger=None):
        self.config = config
        self.stop_event = stop_event
        self.logger = logger or logging.getLogger(__name__)

    def download(self, link, file_name):
        """Download audio from link. Returns True on success, False on failure."""
        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
                ydl_opts = self._get_ydl_opts(file_name, temp_dir)
                with yt_dlp.YoutubeDL(ydl_opts) as downloader:
                    downloader.download([link])
                self.logger.warning(f"DL Complete: {link}")
                return True
        except Exception as e:
            self.logger.error(f"Error downloading song: {link}. Error: {e}")
            return False

    def _get_ydl_opts(self, file_name, temp_dir):
        opts = {
            "logger": self.logger,
            "ffmpeg_location": "/usr/bin/ffmpeg",
            "format": "bestaudio",
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
            opts["cookiefile"] = self.config.cookies_path
        return opts

    def _progress_hook(self, d):
        if self.stop_event.is_set():
            raise Exception("Cancelled")
        if d["status"] == "finished":
            self.logger.warning("Download complete")
        elif d["status"] == "downloading":
            self.logger.warning(f'Downloaded {d["_percent_str"]} of {d["_total_bytes_str"]} at {d["_speed_str"]}')
