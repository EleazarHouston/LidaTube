import json
import logging
import os
import re


class AppConfig:
    SETTINGS_FILENAME = "settings_config.json"
    CONFIG_FOLDER = "config"
    DOWNLOAD_FOLDER = "downloads"

    DEFAULTS = {
        "lidarr_address": "http://192.168.1.2:8686",
        "lidarr_api_key": "",
        "lidarr_api_timeout": 120.0,
        "thread_limit": 1,
        "sleep_interval": 0,
        "fallback_to_top_result": False,
        "library_scan_on_completion": True,
        "sync_schedule": [],
        "minimum_match_ratio": 90,
        "secondary_search": "YTS",
        "preferred_codec": "mp3",
        "attempt_lidarr_import": False,
        "lidarr_scan_thread_limit": 16,
        "duration_tolerance_seconds": 8,
        # Lidarr-namespace path that maps to LidaTube's download_folder. When set,
        # downloads go to a staging area and are imported into the library via the
        # Lidarr API instead of being written into the library in place. Empty = legacy
        # behavior (download_folder IS the library; import registers files in place).
        "lidarr_download_path": "",
    }

    _ENV_CONVERTERS = {
        "lidarr_address": str,
        "lidarr_api_key": str,
        "lidarr_api_timeout": float,
        "thread_limit": int,
        "sleep_interval": float,
        "fallback_to_top_result": lambda v: v.lower() == "true",
        "library_scan_on_completion": lambda v: v.lower() == "true",
        "minimum_match_ratio": float,
        "secondary_search": str,
        "preferred_codec": str,
        "attempt_lidarr_import": lambda v: v.lower() == "true",
        "lidarr_scan_thread_limit": int,
        "duration_tolerance_seconds": int,
        "lidarr_download_path": str,
    }

    def __init__(self, logger=None):
        self.logger = logger or logging.getLogger(__name__)
        os.makedirs(self.CONFIG_FOLDER, exist_ok=True)
        self.settings_file = os.path.join(self.CONFIG_FOLDER, self.SETTINGS_FILENAME)
        self.download_folder = self.DOWNLOAD_FOLDER

        cookies_path = os.path.join(self.CONFIG_FOLDER, "cookies.txt")
        self.cookies_path = cookies_path if os.path.exists(cookies_path) else None

        self._load()

    def _load(self):
        # Initialise all keys to sentinel so we can detect "not set"
        for key in self.DEFAULTS:
            setattr(self, key, "")

        # 1. Environment variables take highest priority
        for key, converter in self._ENV_CONVERTERS.items():
            val = os.environ.get(key, "")
            if val:
                setattr(self, key, converter(val))

        sync_val = os.environ.get("sync_schedule", "")
        if sync_val:
            self.sync_schedule = self.parse_sync_schedule(sync_val)

        # 2. Config file fills in anything still unset
        try:
            if os.path.exists(self.settings_file):
                self.logger.warning("Loading Settings via config file")
                with open(self.settings_file, "r") as f:
                    saved = json.load(f)
                for key, value in saved.items():
                    if key in self.DEFAULTS and getattr(self, key, "") == "":
                        setattr(self, key, value)
        except Exception as e:
            self.logger.error(f"Error Loading Config: {e}")

        # 3. Defaults fill in anything still unset
        for key, default in self.DEFAULTS.items():
            if getattr(self, key, "") == "":
                setattr(self, key, default)

    def save(self):
        try:
            data = {key: getattr(self, key) for key in self.DEFAULTS}
            with open(self.settings_file, "w") as f:
                json.dump(data, f, indent=4)
        except Exception as e:
            self.logger.error(f"Error Saving Config: {e}")

    @staticmethod
    def parse_sync_schedule(input_string):
        ret = []
        try:
            if input_string:
                raw = [int(re.sub(r"\D", "", t.strip())) for t in str(input_string).split(",")]
                clamped = [0 if x < 0 or x > 23 else x for x in raw]
                ret = sorted(set(clamped))
        except Exception:
            pass
        return ret
