"""The rejected-videos CLI posts a JSONL file to the running app's import API."""

import json

import pytest

import reject_videos


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def test_cli_posts_the_file_to_the_import_api_and_reports_counts(tmp_path, monkeypatch, capsys):
    path = tmp_path / "rejects.jsonl"
    path.write_text('{"track_id": 7, "video_id": "abcdefghijk", "reason": "acoustid_mismatch"}\n', encoding="utf-8")
    calls = []

    def fake_post(url, data, headers, timeout, params):
        calls.append((url, data, params))
        return FakeResponse(200, {"inserted": 1, "duplicate": 0, "invalid": 0, "errors": []})

    monkeypatch.setattr(reject_videos.requests, "post", fake_post)

    assert reject_videos.main([str(path), "--url", "http://127.0.0.1:5000", "--source", "bulk"]) == 0
    assert calls[0][0] == "http://127.0.0.1:5000/api/rejected_videos/import"
    assert calls[0][2] == {"source": "bulk"}
    assert b"abcdefghijk" in calls[0][1]
    assert json.loads(capsys.readouterr().out) == {"inserted": 1, "duplicate": 0, "invalid": 0, "errors": []}


def test_cli_exits_non_zero_when_lines_are_invalid_or_the_api_fails(tmp_path, monkeypatch):
    path = tmp_path / "rejects.jsonl"
    path.write_text("not json\n", encoding="utf-8")
    monkeypatch.setattr(reject_videos.requests, "post",
                        lambda *a, **k: FakeResponse(200, {"inserted": 0, "duplicate": 0, "invalid": 1, "errors": [1]}))
    assert reject_videos.main([str(path)]) == 1
    monkeypatch.setattr(reject_videos.requests, "post", lambda *a, **k: FakeResponse(400, {"error": "empty"}))
    assert reject_videos.main([str(path)]) == 2
