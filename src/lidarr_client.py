import logging
import os
import threading
import requests
from requests.adapters import HTTPAdapter


class LidarrClient:
    def __init__(self, config, logger=None):
        self.config = config
        self.logger = logger or logging.getLogger(__name__)
        self._local = threading.local()

    @property
    def session(self):
        """Return a thread-local session so each worker thread has its own connection pool."""
        if not hasattr(self._local, "session"):
            session = requests.Session()
            adapter = HTTPAdapter(pool_connections=4, pool_maxsize=8, pool_block=True)
            session.mount("http://", adapter)
            session.mount("https://", adapter)
            self._local.session = session
        return self._local.session

    def get_wanted_albums(self, page, page_size=2000):
        endpoint = f"{self.config.lidarr_address}/api/v1/wanted/missing?includeArtist=true"
        params = {
            "apikey": self.config.lidarr_api_key,
            "page": page,
            "pageSize": page_size,
        }
        return self.session.get(endpoint, params=params, timeout=self.config.lidarr_api_timeout)

    def get_tracks_for_album(self, album_id):
        endpoint = f"{self.config.lidarr_address}/api/v1/track"
        params = {"apikey": self.config.lidarr_api_key, "albumId": album_id}
        return self.session.get(endpoint, params=params, timeout=self.config.lidarr_api_timeout)

    def get_root_folders(self):
        endpoint = f"{self.config.lidarr_address}/api/v1/rootfolder"
        headers = {"X-Api-Key": self.config.lidarr_api_key}
        response = self.session.get(endpoint, headers=headers, timeout=self.config.lidarr_api_timeout)
        try:
            if response.status_code == 200:
                folders = [folder["path"] for folder in response.json()]
                self.logger.warning(f"Lidarr root folders: {folders}")
                return folders
            self.logger.error(f"Failed to get root folders: HTTP {response.status_code}")
            return []
        finally:
            response.close()

    def trigger_library_scan(self, folders):
        self.logger.warning(f"Triggering Lidarr library scan for folders: {folders}")
        endpoint = f"{self.config.lidarr_address}/api/v1/command"
        headers = {"X-Api-Key": self.config.lidarr_api_key, "Content-Type": "application/json"}
        data = {"name": "RescanFolders", "folders": folders}
        return self.session.post(endpoint, json=data, headers=headers, timeout=self.config.lidarr_api_timeout)

    def import_song(self, req_album, song, filename):
        self.logger.warning(f'Importing song via Lidarr: {req_album.get("artist", "?")} - {song["track_title"]} ({filename})')
        endpoint = f"{self.config.lidarr_address}/api/v1/manualimport"
        headers = {"X-Api-Key": self.config.lidarr_api_key, "Content-Type": "application/json"}
        full_file_path = os.path.join(req_album["album_full_path"], filename)
        data = {
            "id": song["track_id"],
            "path": full_file_path,
            "name": song["track_title"],
            "artistId": req_album["artist_id"],
            "albumId": req_album["album_id"],
            "albumReleaseId": req_album["album_release_id"],
            "quality": {},
            "releaseGroup": "",
            "indexerFlags": 0,
            "downloadId": "",
            "additionalFile": False,
            "replaceExistingFiles": False,
            "disableReleaseSwitching": False,
            "rejections": [],
        }
        return self.session.post(endpoint, json=[data], headers=headers, timeout=self.config.lidarr_api_timeout)
