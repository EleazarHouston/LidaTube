import threading
from unittest.mock import Mock, patch
import pytest

from lidarr_client import LidarrClient


class FakeResponse:
    def __init__(self, status_code, payload, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload

    def close(self):
        pass


@pytest.fixture
def config():
    cfg = Mock()
    cfg.lidarr_address = "http://lidarr.test"
    cfg.lidarr_api_key = "test-api-key"
    cfg.lidarr_api_timeout = 30
    return cfg


@pytest.fixture
def client(config):
    return LidarrClient(config, Mock())


# --- get_wanted_albums ---


def test_get_wanted_albums_hits_correct_endpoint(client):
    with patch.object(client.session, "get", return_value=FakeResponse(200, {"records": []})) as mock_get:
        client.get_wanted_albums(page=1)
    url = mock_get.call_args[0][0]
    assert "http://lidarr.test" in url
    assert "wanted/missing" in url


def test_get_wanted_albums_sends_page_param(client):
    with patch.object(client.session, "get", return_value=FakeResponse(200, {"records": []})) as mock_get:
        client.get_wanted_albums(page=3, page_size=500)
    params = mock_get.call_args[1]["params"]
    assert params["page"] == 3
    assert params["pageSize"] == 500


def test_get_wanted_albums_includes_api_key(client):
    with patch.object(client.session, "get", return_value=FakeResponse(200, {"records": []})) as mock_get:
        client.get_wanted_albums(page=1)
    params = mock_get.call_args[1]["params"]
    assert params["apikey"] == "test-api-key"


def test_get_wanted_albums_returns_response(client):
    fake = FakeResponse(200, {"records": [{"id": 1}]})
    with patch.object(client.session, "get", return_value=fake):
        response = client.get_wanted_albums(page=1)
    assert response.status_code == 200
    assert response.json()["records"][0]["id"] == 1


# --- get_tracks_for_album ---


def test_get_tracks_for_album_hits_correct_endpoint(client):
    with patch.object(client.session, "get", return_value=FakeResponse(200, [])) as mock_get:
        client.get_tracks_for_album(album_id=42)
    url = mock_get.call_args[0][0]
    assert "/api/v1/track" in url


def test_get_tracks_for_album_sends_album_id(client):
    with patch.object(client.session, "get", return_value=FakeResponse(200, [])) as mock_get:
        client.get_tracks_for_album(album_id=42)
    params = mock_get.call_args[1]["params"]
    assert params["albumId"] == 42


def test_get_tracks_for_album_returns_response(client):
    tracks = [{"id": 1, "title": "Track A"}]
    with patch.object(client.session, "get", return_value=FakeResponse(200, tracks)):
        response = client.get_tracks_for_album(album_id=1)
    assert response.json() == tracks


# --- get_root_folders ---


def test_get_root_folders_returns_paths(client):
    payload = [{"path": "/music"}, {"path": "/music2"}]
    with patch.object(client.session, "get", return_value=FakeResponse(200, payload)):
        folders = client.get_root_folders()
    assert folders == ["/music", "/music2"]


def test_get_root_folders_returns_empty_list_on_error(client):
    with patch.object(client.session, "get", return_value=FakeResponse(500, None, "Server Error")):
        folders = client.get_root_folders()
    assert folders == []


# --- trigger_library_scan ---


def test_trigger_library_scan_posts_rescan_command(client):
    with patch.object(client.session, "post", return_value=FakeResponse(201, {})) as mock_post:
        client.trigger_library_scan(["/music"])
    posted = mock_post.call_args[1]["json"]
    assert posted["name"] == "RescanFolders"
    assert posted["folders"] == ["/music"]


def test_trigger_library_scan_returns_response(client):
    with patch.object(client.session, "post", return_value=FakeResponse(201, {})):
        response = client.trigger_library_scan(["/music"])
    assert response.status_code == 201


def test_trigger_library_scan_uses_api_key_header(client):
    with patch.object(client.session, "post", return_value=FakeResponse(201, {})) as mock_post:
        client.trigger_library_scan(["/music"])
    headers = mock_post.call_args[1]["headers"]
    assert headers["X-Api-Key"] == "test-api-key"


# --- import_song ---


def test_import_song_posts_to_manualimport(client):
    req_album = {"album_full_path": "/music/Album", "artist_id": 1, "album_id": 10, "album_release_id": 100}
    song = {"track_id": 42, "track_title": "My Song"}
    with patch.object(client.session, "post", return_value=FakeResponse(202, {})) as mock_post:
        client.import_song(req_album, song, "My Song.mp3")
    url = mock_post.call_args[0][0]
    assert "manualimport" in url


def test_import_song_sends_correct_ids(client):
    req_album = {"album_full_path": "/music/Album", "artist_id": 1, "album_id": 10, "album_release_id": 100}
    song = {"track_id": 42, "track_title": "My Song"}
    with patch.object(client.session, "post", return_value=FakeResponse(202, {})) as mock_post:
        client.import_song(req_album, song, "My Song.mp3")
    payload = mock_post.call_args[1]["json"][0]
    assert payload["id"] == 42
    assert payload["artistId"] == 1
    assert payload["albumId"] == 10
    assert payload["albumReleaseId"] == 100


def test_import_song_returns_response(client):
    req_album = {"album_full_path": "/music/Album", "artist_id": 1, "album_id": 10, "album_release_id": 100}
    song = {"track_id": 42, "track_title": "My Song"}
    with patch.object(client.session, "post", return_value=FakeResponse(202, {})):
        response = client.import_song(req_album, song, "My Song.mp3")
    assert response.status_code == 202


# --- thread-local sessions ---


def test_different_threads_get_different_sessions(client):
    """Each thread must have its own session so connections don't share a pool."""
    sessions = {}
    barrier = threading.Barrier(2)

    def capture():
        sessions["thread"] = client.session
        barrier.wait()  # hold the session alive until main has captured its own

    t = threading.Thread(target=capture)
    t.start()
    sessions["main"] = client.session
    barrier.wait()
    t.join()

    assert sessions["main"] is not sessions["thread"]
