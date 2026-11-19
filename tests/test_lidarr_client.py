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
    cfg.lidarr_download_path = ""
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


def _candidate(**over):
    c = {
        "path": "/media/downloads/lidatube/Artist/Album (2020)/track.mp3",
        "artist": {"id": 5}, "album": {"id": 10}, "albumReleaseId": 100,
        "quality": {"quality": {"id": 8, "name": "MP3-320"}},
        "tracks": [{"id": 42, "title": "My Song"}],
    }
    c.update(over)
    return c


def test_scan_import_candidates_hits_manualimport_with_folder(client):
    with patch.object(client.session, "get", return_value=FakeResponse(200, [])) as mock_get:
        client.scan_import_candidates("/media/downloads/lidatube/Artist/Album (2020)")
    assert "manualimport" in mock_get.call_args[0][0]
    assert mock_get.call_args[1]["params"]["folder"] == "/media/downloads/lidatube/Artist/Album (2020)"


def test_import_candidates_posts_manualimport_command_with_move(client):
    with patch.object(client.session, "post", return_value=FakeResponse(201, {})) as mock_post:
        response, count = client.import_candidates([_candidate()], import_mode="move")
    assert count == 1 and response.status_code == 201
    assert "command" in mock_post.call_args[0][0]
    body = mock_post.call_args[1]["json"]
    assert body["name"] == "ManualImport" and body["importMode"] == "move"
    f = body["files"][0]
    assert f["trackIds"] == [42]
    assert f["quality"] == {"quality": {"id": 8, "name": "MP3-320"}}
    assert f["artistId"] == 5 and f["albumId"] == 10 and f["albumReleaseId"] == 100


def test_import_candidates_skips_unmatched_candidates(client):
    # A candidate Lidarr couldn't match (no tracks) must not be imported.
    with patch.object(client.session, "post", return_value=FakeResponse(201, {})) as mock_post:
        response, count = client.import_candidates([_candidate(tracks=[])])
    assert count == 0 and response is None
    mock_post.assert_not_called()


# --- thread-local sessions ---


# --- import_album / rescan_library ---


_ALBUM = {
    "artist": "Artist",
    "album_name": "Album",
    "artist_path": "/music/Artist/",
    "album_folder": "Album (2024)",
    "album_full_path": "/music/Artist/Album (2024)",
}


@pytest.mark.parametrize("download_path, scanned_folder", [
    ("/staging", "/staging/Artist/Album (2024)"),
    ("", "/music/Artist/Album (2024)"),
])
def test_import_album_imports_the_staging_or_in_place_folder(client, download_path, scanned_folder):
    client.config.lidarr_download_path = download_path
    scan = FakeResponse(200, [{"path": "x"}])
    command = FakeResponse(201, {})
    scan.close, command.close = Mock(), Mock()
    with patch.object(client, "scan_import_candidates", return_value=scan) as scan_mock, \
            patch.object(client, "import_candidates", return_value=(command, 1)) as import_mock:
        client.import_album(dict(_ALBUM))

    scan_mock.assert_called_once_with(scanned_folder)
    import_mock.assert_called_once_with([{"path": "x"}], import_mode="move")
    scan.close.assert_called_once_with()
    command.close.assert_called_once_with()


def test_import_album_does_not_import_when_the_folder_scan_fails(client):
    scan = FakeResponse(500, None, text="boom")
    scan.close = Mock()
    with patch.object(client, "scan_import_candidates", return_value=scan), \
            patch.object(client, "import_candidates") as import_mock:
        client.import_album(dict(_ALBUM))

    import_mock.assert_not_called()
    scan.close.assert_called_once_with()


def test_import_album_logs_instead_of_raising_on_lidarr_errors(client):
    with patch.object(client, "scan_import_candidates", side_effect=ConnectionError("down")):
        client.import_album(dict(_ALBUM))
    client.logger.error.assert_called_once()


def test_rescan_library_rescans_every_root_folder(client):
    response = FakeResponse(201, {})
    response.close = Mock()
    with patch.object(client, "get_root_folders", return_value=["/music", "/audiobooks"]), \
            patch.object(client, "trigger_library_scan", return_value=response) as scan_mock:
        client.rescan_library()

    scan_mock.assert_called_once_with(["/music", "/audiobooks"])
    response.close.assert_called_once_with()


def test_rescan_library_skips_the_rescan_without_root_folders(client):
    with patch.object(client, "get_root_folders", return_value=[]), \
            patch.object(client, "trigger_library_scan") as scan_mock:
        client.rescan_library()
    scan_mock.assert_not_called()


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


def test_same_thread_reuses_session(client):
    first = client.session
    second = client.session

    assert first is second


def test_get_root_folders_uses_api_key_header(client):
    with patch.object(client.session, "get", return_value=FakeResponse(200, [])) as mock_get:
        client.get_root_folders()

    headers = mock_get.call_args[1]["headers"]
    assert headers["X-Api-Key"] == "test-api-key"


def test_get_recording_id_reads_the_tracks_musicbrainz_recording(client):
    track = {"id": 7, "foreignRecordingId": "0bd8fe74-0000-0000-0000-000000000001"}
    with patch.object(client.session, "get", return_value=FakeResponse(200, track)) as mock_get:
        assert client.get_recording_id(7) == "0bd8fe74-0000-0000-0000-000000000001"
    assert mock_get.call_args[0][0].endswith("/api/v1/track/7")


@pytest.mark.parametrize("response", [FakeResponse(404, {}), FakeResponse(200, {"id": 7})])
def test_get_recording_id_is_none_when_lidarr_cannot_say(client, response):
    with patch.object(client.session, "get", return_value=response):
        assert client.get_recording_id(7) is None
    with patch.object(client.session, "get", side_effect=ConnectionError("down")):
        assert client.get_recording_id(7) is None


def test_import_album_imports_only_the_named_files_when_asked(client):
    client.config.lidarr_download_path = "/staging"
    scanned = [{"path": "/staging/Artist/Album (2024)/Artist - Album - 01 - Verified.mp3"},
               {"path": "/staging/Artist/Album (2024)/Artist - Album - 02 - Unverified leftover.mp3"}]
    with patch.object(client, "scan_import_candidates", return_value=FakeResponse(200, scanned)), \
            patch.object(client, "import_candidates", return_value=(FakeResponse(201, {}), 1)) as import_mock:
        client.import_album(dict(_ALBUM), only_files=["Artist - Album - 01 - Verified.mp3"])
    import_mock.assert_called_once_with([scanned[0]], import_mode="move")


def test_import_album_with_no_named_file_in_the_folder_imports_nothing(client):
    scanned = [{"path": "/music/Artist/Album (2024)/leftover.mp3"}]
    with patch.object(client, "scan_import_candidates", return_value=FakeResponse(200, scanned)), \
            patch.object(client, "import_candidates") as import_mock:
        client.import_album(dict(_ALBUM), only_files=["missing.mp3"])
    import_mock.assert_not_called()
