from unittest.mock import Mock

import _general
from _general import is_resource_exhaustion_error


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


def test_string_cleaner_list_uses_same_logic_as_str():
    """Each list element should be cleaned identically to passing the string directly."""
    elements = ["Björk/Live?", "AC DC"]
    result_list = _general.string_cleaner(elements)
    result_each = [_general.string_cleaner(e) for e in elements]
    assert result_list == result_each


# --- is_resource_exhaustion_error ---


def test_is_resource_exhaustion_error_none_returns_false():
    assert is_resource_exhaustion_error(None) is False


def test_is_resource_exhaustion_error_errno_23():
    err = OSError(23, "Too many open files in system")
    assert is_resource_exhaustion_error(err) is True


def test_is_resource_exhaustion_error_errno_24():
    err = OSError(24, "Too many open files")
    assert is_resource_exhaustion_error(err) is True


def test_is_resource_exhaustion_error_message_match():
    err = Exception("no file descriptors available")
    assert is_resource_exhaustion_error(err) is True


def test_is_resource_exhaustion_error_message_case_insensitive():
    err = Exception("No File Descriptors Available")
    assert is_resource_exhaustion_error(err) is True


def test_is_resource_exhaustion_error_unrelated_oserror():
    err = OSError(2, "No such file or directory")
    assert is_resource_exhaustion_error(err) is False


def test_is_resource_exhaustion_error_unrelated_exception():
    assert is_resource_exhaustion_error(ValueError("bad value")) is False


def test_is_resource_exhaustion_error_chained_cause():
    inner = OSError(24, "Too many open files")
    outer = RuntimeError("wrapped")
    outer.__cause__ = inner
    assert is_resource_exhaustion_error(outer) is True


def test_is_resource_exhaustion_error_chained_context():
    inner = OSError(23, "Too many open files in system")
    outer = RuntimeError("wrapped")
    outer.__context__ = inner
    assert is_resource_exhaustion_error(outer) is True


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
