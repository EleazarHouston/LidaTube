import threading
import pytest
from unittest.mock import Mock, patch, MagicMock, call

from downloader import Downloader, RATE_LIMIT_BACKOFF_SECONDS, CONSECUTIVE_UNAVAILABLE_THRESHOLD, UNAVAILABLE_BACKOFF_SECONDS


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
    assert opts["format"] == "bestaudio/best"


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


# --- rate-limit backoff ---

RATE_LIMIT_ERR = Exception(
    "ERROR: [youtube] abc: Video unavailable. "
    "The current session has been rate-limited by YouTube for up to an hour."
)


def test_download_retries_after_rate_limit_and_succeeds(dl, stop_event):
    call_count = 0

    def side_effect(links):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RATE_LIMIT_ERR

    with patch("downloader.yt_dlp.YoutubeDL") as mock_ydl_class, \
         patch.object(stop_event, "wait", return_value=False):
        mock_instance = MagicMock()
        mock_ydl_class.return_value.__enter__.return_value = mock_instance
        mock_instance.download.side_effect = side_effect
        result = dl.download("https://example.com/vid", "test_file")

    assert result is True
    assert call_count == 2


def test_download_returns_false_after_all_retries_exhausted(dl, stop_event):
    with patch("downloader.yt_dlp.YoutubeDL") as mock_ydl_class, \
         patch.object(stop_event, "wait", return_value=False):
        mock_instance = MagicMock()
        mock_ydl_class.return_value.__enter__.return_value = mock_instance
        mock_instance.download.side_effect = RATE_LIMIT_ERR
        result = dl.download("https://example.com/vid", "test_file")

    assert result is False
    assert mock_instance.download.call_count == len(RATE_LIMIT_BACKOFF_SECONDS) + 1


def test_download_waits_backoff_between_rate_limit_retries(dl, stop_event):
    with patch("downloader.yt_dlp.YoutubeDL") as mock_ydl_class, \
         patch.object(stop_event, "wait", return_value=False) as mock_wait:
        mock_instance = MagicMock()
        mock_ydl_class.return_value.__enter__.return_value = mock_instance
        mock_instance.download.side_effect = RATE_LIMIT_ERR
        dl.download("https://example.com/vid", "test_file")

    wait_durations = [c.args[0] for c in mock_wait.call_args_list]
    assert wait_durations == RATE_LIMIT_BACKOFF_SECONDS


def test_download_stops_during_rate_limit_backoff_if_stop_event_set(dl, stop_event):
    with patch("downloader.yt_dlp.YoutubeDL") as mock_ydl_class, \
         patch.object(stop_event, "wait", return_value=True):
        mock_instance = MagicMock()
        mock_ydl_class.return_value.__enter__.return_value = mock_instance
        mock_instance.download.side_effect = RATE_LIMIT_ERR
        result = dl.download("https://example.com/vid", "test_file")

    assert result is False
    assert mock_instance.download.call_count == 1


def test_ydl_opts_includes_socket_timeout(dl):
    opts = dl._get_ydl_opts("file", "/tmp")
    assert "socket_timeout" in opts


def test_download_returns_false_immediately_if_stop_event_already_set(dl, stop_event):
    stop_event.set()
    with patch("downloader.yt_dlp.YoutubeDL") as mock_ydl_class:
        result = dl.download("https://example.com/vid", "test_file")
    mock_ydl_class.assert_not_called()
    assert result is False


# --- consecutive unavailable backoff ---

UNAVAILABLE_ERR = Exception(
    "ERROR: [youtube] 4egsH0KuNeE: Video unavailable. This content isn't available."
)


def test_consecutive_unavailable_counter_starts_at_zero(dl):
    assert dl._consecutive_unavailable == 0


def test_unavailable_error_increments_consecutive_counter(dl):
    with patch("downloader.yt_dlp.YoutubeDL") as mock_ydl_class:
        mock_instance = MagicMock()
        mock_ydl_class.return_value.__enter__.return_value = mock_instance
        mock_instance.download.side_effect = UNAVAILABLE_ERR
        dl.download("https://example.com/vid", "test_file")
    assert dl._consecutive_unavailable == 1


def test_success_resets_consecutive_unavailable_counter(dl):
    dl._consecutive_unavailable = 3
    with patch("downloader.yt_dlp.YoutubeDL") as mock_ydl_class:
        mock_instance = MagicMock()
        mock_ydl_class.return_value.__enter__.return_value = mock_instance
        dl.download("https://example.com/vid", "test_file")
    assert dl._consecutive_unavailable == 0


def test_non_unavailable_error_does_not_increment_counter(dl):
    with patch("downloader.yt_dlp.YoutubeDL") as mock_ydl_class:
        mock_instance = MagicMock()
        mock_ydl_class.return_value.__enter__.return_value = mock_instance
        mock_instance.download.side_effect = Exception("some other network error")
        dl.download("https://example.com/vid", "test_file")
    assert dl._consecutive_unavailable == 0


def test_consecutive_unavailable_triggers_backoff_at_threshold(dl, stop_event):
    dl._consecutive_unavailable = CONSECUTIVE_UNAVAILABLE_THRESHOLD - 1
    with patch("downloader.yt_dlp.YoutubeDL") as mock_ydl_class, \
         patch.object(stop_event, "wait", return_value=False) as mock_wait:
        mock_instance = MagicMock()
        mock_ydl_class.return_value.__enter__.return_value = mock_instance
        mock_instance.download.side_effect = UNAVAILABLE_ERR
        dl.download("https://example.com/vid", "test_file")
    mock_wait.assert_called_once_with(UNAVAILABLE_BACKOFF_SECONDS)


def test_consecutive_unavailable_counter_resets_after_backoff(dl, stop_event):
    dl._consecutive_unavailable = CONSECUTIVE_UNAVAILABLE_THRESHOLD - 1
    with patch("downloader.yt_dlp.YoutubeDL") as mock_ydl_class, \
         patch.object(stop_event, "wait", return_value=False):
        mock_instance = MagicMock()
        mock_ydl_class.return_value.__enter__.return_value = mock_instance
        mock_instance.download.side_effect = UNAVAILABLE_ERR
        dl.download("https://example.com/vid", "test_file")
    assert dl._consecutive_unavailable == 0


def test_consecutive_unavailable_below_threshold_does_not_trigger_backoff(dl, stop_event):
    dl._consecutive_unavailable = CONSECUTIVE_UNAVAILABLE_THRESHOLD - 2
    with patch("downloader.yt_dlp.YoutubeDL") as mock_ydl_class, \
         patch.object(stop_event, "wait", return_value=False) as mock_wait:
        mock_instance = MagicMock()
        mock_ydl_class.return_value.__enter__.return_value = mock_instance
        mock_instance.download.side_effect = UNAVAILABLE_ERR
        dl.download("https://example.com/vid", "test_file")
    mock_wait.assert_not_called()


def test_backoff_logs_warning_at_threshold(dl, stop_event):
    dl._consecutive_unavailable = CONSECUTIVE_UNAVAILABLE_THRESHOLD - 1
    with patch("downloader.yt_dlp.YoutubeDL") as mock_ydl_class, \
         patch.object(stop_event, "wait", return_value=False):
        mock_instance = MagicMock()
        mock_ydl_class.return_value.__enter__.return_value = mock_instance
        mock_instance.download.side_effect = UNAVAILABLE_ERR
        dl.download("https://example.com/vid", "test_file")
    warning_calls = [str(c) for c in dl.logger.warning.call_args_list]
    assert any("in a row" in w.lower() for w in warning_calls)


def test_download_logs_warning_before_each_retry(dl, stop_event):
    with patch("downloader.yt_dlp.YoutubeDL") as mock_ydl_class, \
         patch.object(stop_event, "wait", return_value=False):
        mock_instance = MagicMock()
        mock_ydl_class.return_value.__enter__.return_value = mock_instance
        mock_instance.download.side_effect = RATE_LIMIT_ERR
        dl.download("https://example.com/vid", "test_file")

    warning_calls = [str(c) for c in dl.logger.warning.call_args_list]
    assert any("rate" in w.lower() for w in warning_calls)
