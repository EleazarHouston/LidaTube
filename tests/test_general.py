from unittest.mock import Mock

import _general


def test_convert_to_lidarr_format_replaces_problematic_characters():
    source = 'Artist/Album\\Name: "Live" *Edition*?'
    assert _general.convert_to_lidarr_format(source) == "Artist+Album+Name-  Live  -Edition-!"


def test_string_cleaner_handles_string_input():
    source = "  Beyonc\u00e9 / Live: 2020?  "
    assert _general.string_cleaner(source) == "Beyonce Live 2020"


def test_string_cleaner_handles_list_input():
    source = ["  M\u00f6tley/Cr\u00fce ", "A:B|C"]
    assert _general.string_cleaner(source) == ["Motley Crue", "A B C"]


def test_string_cleaner_returns_none_for_unsupported_type():
    assert _general.string_cleaner(42) is None


def test_add_metadata_logs_and_skips_unsupported_extensions():
    logger = Mock()
    song = {"track_title": "Song", "track_number": 1, "artist": "Artist"}
    req_album = {"artist": "Artist", "album_name": "Album", "album_year": 2024, "album_genres": "Rock"}

    _general.add_metadata(logger, song, req_album, "/tmp/test.wav")

    logger.warning.assert_called_once_with("Metadata added for /tmp/test.wav")
    logger.error.assert_not_called()


def test_add_metadata_writes_mp3_tags(monkeypatch):
    logger = Mock()
    metadata = Mock()
    monkeypatch.setattr(_general, "ID3", lambda _: metadata)
    monkeypatch.setattr(_general, "TIT2", lambda **kwargs: ("TIT2", kwargs))
    monkeypatch.setattr(_general, "TRCK", lambda **kwargs: ("TRCK", kwargs))
    monkeypatch.setattr(_general, "TPE1", lambda **kwargs: ("TPE1", kwargs))
    monkeypatch.setattr(_general, "TPE2", lambda **kwargs: ("TPE2", kwargs))
    monkeypatch.setattr(_general, "TALB", lambda **kwargs: ("TALB", kwargs))
    monkeypatch.setattr(_general, "TYER", lambda **kwargs: ("TYER", kwargs))
    monkeypatch.setattr(_general, "TCON", lambda **kwargs: ("TCON", kwargs))

    song = {"track_title": "Song", "track_number": 1, "artist": "Track Artist"}
    req_album = {"artist": "Album Artist", "album_name": "Album", "album_year": 2024, "album_genres": "Rock"}

    _general.add_metadata(logger, song, req_album, "/tmp/test.mp3")

    assert metadata.add.call_count == 7
    metadata.save.assert_called_once()
    logger.error.assert_not_called()


def test_add_metadata_writes_flac_tags(monkeypatch):
    logger = Mock()

    class FakeFlac(dict):
        def __init__(self):
            super().__init__()
            self.saved = False

        def save(self):
            self.saved = True

    audio_file = FakeFlac()
    monkeypatch.setattr(_general, "FLAC", lambda _: audio_file)

    song = {"track_title": "Song", "track_number": 1, "artist": "Track Artist"}
    req_album = {"artist": "Album Artist", "album_name": "Album", "album_year": 2024, "album_genres": "Rock"}

    _general.add_metadata(logger, song, req_album, "/tmp/test.flac")

    assert audio_file["title"] == "Song"
    assert audio_file["tracknumber"] == "1"
    assert audio_file["artist"] == "Track Artist"
    assert audio_file["albumartist"] == "Album Artist"
    assert audio_file["album"] == "Album"
    assert audio_file["date"] == "2024"
    assert audio_file["genre"] == "Rock"
    assert audio_file.saved is True
    logger.error.assert_not_called()
