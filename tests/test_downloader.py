import threading
import pytest
from unittest.mock import Mock, patch, MagicMock

from downloader import Downloader


@pytest.fixture
def config():
    cfg = Mock()
    cfg.preferred_codec = "mp3"
    cfg.cookies_path = None
    cfg.download_folder = "/downloads"
    return cfg


@pytest.fixture
def stop_event():
    return threading.Event()


@pytest.fixture
def dl(config, stop_event):
    return Downloader(config, stop_event, Mock())


# --- _get_ydl_opts ---


def test_ydl_opts_uses_preferred_codec(dl):
    opts = dl._get_ydl_opts("file", "/tmp")
    extract_pp = next(p for p in opts["postprocessors"] if p["key"] == "FFmpegExtractAudio")
    assert extract_pp["preferredcodec"] == "mp3"


def test_ydl_opts_no_cookiefile_when_none(dl):
    opts = dl._get_ydl_opts("file", "/tmp")
    assert "cookiefile" not in opts


def test_ydl_opts_includes_cookiefile_when_set(dl):
    dl.config.cookies_path = "/config/cookies.txt"
    opts = dl._get_ydl_opts("file", "/tmp")
    assert opts["cookiefile"] == "/config/cookies.txt"


def test_ydl_opts_includes_embed_thumbnail_postprocessor(dl):
    opts = dl._get_ydl_opts("file", "/tmp")
    keys = [p["key"] for p in opts["postprocessors"]]
    assert "EmbedThumbnail" in keys


def test_ydl_opts_sets_bestaudio_format(dl):
    opts = dl._get_ydl_opts("file", "/tmp")
    assert opts["format"] == "bestaudio"


def test_ydl_opts_sets_output_template(dl):
    opts = dl._get_ydl_opts("my_file", "/tmp/dir")
    assert opts["outtmpl"] == "my_file.%(ext)s"


def test_ydl_opts_sets_paths(dl):
    opts = dl._get_ydl_opts("file", "/tmp/dir")
    assert opts["paths"]["temp"] == "/tmp/dir"
    assert opts["paths"]["home"] == "/downloads"


# --- _progress_hook ---


def test_progress_hook_raises_when_stopped(dl, stop_event):
    stop_event.set()
    with pytest.raises(Exception, match="Cancelled"):
        dl._progress_hook({"status": "downloading", "_percent_str": "0%", "_total_bytes_str": "0", "_speed_str": "0"})


def test_progress_hook_does_not_raise_when_not_stopped(dl):
    dl._progress_hook({"status": "finished"})  # should not raise


def test_progress_hook_logs_download_progress(dl):
    dl._progress_hook({"status": "downloading", "_percent_str": "25%", "_total_bytes_str": "4.0MiB", "_speed_str": "1.0MiB/s"})
    dl.logger.warning.assert_called_once_with("Downloaded 25% of 4.0MiB at 1.0MiB/s")


def test_progress_hook_logs_finished_status(dl):
    dl._progress_hook({"status": "finished"})
    dl.logger.warning.assert_called_once_with("Download complete")


# --- download ---


def test_download_returns_true_on_success(dl):
    with patch("downloader.yt_dlp.YoutubeDL") as mock_ydl_class:
        mock_instance = MagicMock()
        mock_ydl_class.return_value = mock_instance
        mock_instance.__enter__.return_value = mock_instance
        result = dl.download("https://example.com/vid", "test_file")
    assert result is True
    mock_instance.download.assert_called_once_with(["https://example.com/vid"])


def test_download_returns_false_on_exception(dl):
    with patch("downloader.yt_dlp.YoutubeDL") as mock_ydl_class:
        mock_instance = MagicMock()
        mock_ydl_class.return_value = mock_instance
        mock_instance.__enter__.return_value = mock_instance
        mock_instance.download.side_effect = Exception("network error")
        result = dl.download("https://example.com/vid", "test_file")
    assert result is False


def test_download_logs_error_on_failure(dl):
    with patch("downloader.yt_dlp.YoutubeDL") as mock_ydl_class:
        mock_instance = MagicMock()
        mock_ydl_class.return_value = mock_instance
        mock_instance.__enter__.return_value = mock_instance
        mock_instance.download.side_effect = Exception("oops")
        dl.download("https://example.com/vid", "test_file")
    dl.logger.error.assert_called_once()


def test_download_logs_completion_on_success(dl):
    with patch("downloader.yt_dlp.YoutubeDL") as mock_ydl_class:
        mock_instance = MagicMock()
        mock_ydl_class.return_value = mock_instance
        mock_instance.__enter__.return_value = mock_instance
        dl.download("https://example.com/vid", "test_file")

    dl.logger.warning.assert_any_call("DL Complete: https://example.com/vid")
