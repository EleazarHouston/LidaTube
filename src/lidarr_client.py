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

    def get_artists_page(self, page, page_size=1000):
        endpoint = f"{self.config.lidarr_address}/api/v1/artist"
        params = {
            "apikey": self.config.lidarr_api_key,
            "page": page,
            "pageSize": page_size,
        }
        # This endpoint returns all artists in one shot regardless of pageSize;
        # large libraries need more time than the standard per-request timeout.
        timeout = max(self.config.lidarr_api_timeout, 600)
        return self.session.get(endpoint, params=params, timeout=timeout)

    def get_wanted_albums(self, page, page_size=1000):
        endpoint = f"{self.config.lidarr_address}/api/v1/wanted/missing"
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

    def scan_import_candidates(self, folder):
        """GET Lidarr's parsed import candidates for a folder (it detects quality + tracks)."""
        endpoint = f"{self.config.lidarr_address}/api/v1/manualimport"
        headers = {"X-Api-Key": self.config.lidarr_api_key}
        params = {"folder": folder, "filterExistingFiles": "false"}
        return self.session.get(endpoint, params=params, headers=headers, timeout=self.config.lidarr_api_timeout)

    @staticmethod
    def _candidate_to_file(candidate):
        """Map a manualimport candidate to a ManualImport command file entry, or None."""
        tracks = candidate.get("tracks") or []
        artist = candidate.get("artist") or {}
        album = candidate.get("album") or {}
        if not tracks or not artist.get("id") or not album.get("id"):
            return None
        return {
            "path": candidate.get("path"),
            "artistId": artist["id"],
            "albumId": album["id"],
            "albumReleaseId": candidate.get("albumReleaseId"),
            "quality": candidate.get("quality"),
            "trackIds": [t["id"] for t in tracks],
            "disableReleaseSwitching": False,
            "indexerFlags": 0,
            "additionalFile": False,
            "replaceExistingFiles": False,
        }

    def import_candidates(self, candidates, import_mode="move"):
        """POST a ManualImport command for scanned candidates. Returns (response, file_count).

        This is the flow Lidarr's own UI uses: the candidate carries the detected
        quality and matched track ids, which a hand-built payload lacks (and which
        caused imports to be accepted but silently import nothing).
        """
        files = [f for f in (self._candidate_to_file(c) for c in candidates) if f]
        if not files:
            return None, 0
        endpoint = f"{self.config.lidarr_address}/api/v1/command"
        headers = {"X-Api-Key": self.config.lidarr_api_key, "Content-Type": "application/json"}
        data = {"name": "ManualImport", "importMode": import_mode, "files": files}
        return self.session.post(endpoint, json=data, headers=headers, timeout=self.config.lidarr_api_timeout), len(files)

    def import_album(self, req_album):
        """Import a downloaded album into the library via Lidarr's manual import.

        Scans the album's folder (staging when lidarr_download_path is set, else the
        in-place library folder), then posts a ManualImport 'move' command using
        Lidarr's own parsed candidates.
        """
        if self.config.lidarr_download_path:
            artist_str = os.path.basename(req_album["artist_path"].rstrip("/"))
            folder = os.path.join(self.config.lidarr_download_path, artist_str, req_album["album_folder"])
        else:
            folder = req_album["album_full_path"]
        scan = None
        response = None
        try:
            scan = self.scan_import_candidates(folder)
            if scan.status_code != 200:
                self.logger.error(f"Import scan failed ({scan.status_code}) for {folder}")
                return
            response, count = self.import_candidates(scan.json(), import_mode="move")
            if count == 0:
                self.logger.warning(f"No importable files found in {folder}")
            elif response.status_code in (200, 201):
                self.logger.warning(
                    f'Import queued for {count} file(s): {req_album["artist"]} - {req_album["album_name"]}'
                )
            else:
                self.logger.error(f"Import command failed ({response.status_code}): {response.text}")
        except Exception as e:
            self.logger.error(f"Error importing album via Lidarr: {e}")
        finally:
            if response is not None:
                response.close()
            if scan is not None:
                scan.close()

    def rescan_library(self):
        """Ask Lidarr to rescan every root folder so it picks up newly written files."""
        response = None
        try:
            root_folders = self.get_root_folders()
            if not root_folders:
                self.logger.warning("No Lidarr root folders found")
                return
            response = self.trigger_library_scan(root_folders)
            if response.status_code != 201:
                self.logger.warning("Failed to start lidarr library scan")
            else:
                self.logger.warning("Lidarr library scan started")
        except Exception as e:
            self.logger.error(f"Lidarr library scan failed: {e}")
        finally:
            if response is not None:
                response.close()
