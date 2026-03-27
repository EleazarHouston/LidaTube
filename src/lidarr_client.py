import logging
import os
import requests


class LidarrClient:
    def __init__(self, config, logger=None):
        self.config = config
        self.logger = logger or logging.getLogger(__name__)
        self.session = requests.Session()

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
        response = self.session.get(endpoint, headers=headers)
        if response.status_code == 200:
            return [folder["path"] for folder in response.json()]
        return []

    def trigger_library_scan(self, folders):
        endpoint = f"{self.config.lidarr_address}/api/v1/command"
        headers = {"X-Api-Key": self.config.lidarr_api_key, "Content-Type": "application/json"}
        data = {"name": "RescanFolders", "folders": folders}
        return self.session.post(endpoint, json=data, headers=headers)

    def import_song(self, req_album, song, filename):
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
        return self.session.post(endpoint, json=[data], headers=headers)
