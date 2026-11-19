"""Rotating the VPN exit through Gluetun's control server when YouTube blocks searches."""

from unittest.mock import Mock

import pytest

import vpn_rotator
from vpn_rotator import VpnRotator


class FakeGluetun:
    """Gluetun's control API: /v1/vpn/status (GET, PUT) and /v1/publicip/ip (GET)."""

    def __init__(self, ips=("1.1.1.1", "2.2.2.2"), fail_put=False):
        self.status = "running"
        self.ips = list(ips)
        self.ip = self.ips.pop(0)
        self.fail_put = fail_put
        self.calls = []

    def _response(self, status_code, payload):
        response = Mock()
        response.status_code = status_code
        response.json.return_value = payload
        return response

    def get(self, url, headers=None, timeout=None):
        self.calls.append(("GET", url, headers))
        if url.endswith("/v1/vpn/status"):
            return self._response(200, {"status": self.status})
        if url.endswith("/v1/publicip/ip"):
            return self._response(200, {"public_ip": self.ip if self.status == "running" else ""})
        return self._response(404, {})

    def put(self, url, json=None, headers=None, timeout=None):
        self.calls.append(("PUT", url, json))
        if self.fail_put:
            return self._response(401, {})
        self.status = json["status"]
        if self.status == "running" and self.ips:
            self.ip = self.ips.pop(0)
        return self._response(200, {"outcome": "ok"})


def build(gluetun, url="http://127.0.0.1:8000", key="secret", now=None):
    config = Mock()
    config.gluetun_control_url = url
    config.gluetun_api_key = key
    config.vpn_rotation_min_minutes = 15
    clock = now or [1000.0]
    rotator = VpnRotator(config, logger=Mock(), clock=lambda: clock[0], sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
                         http=gluetun)
    return rotator, clock


@pytest.mark.parametrize("url, expected", [("http://127.0.0.1:8000", True), ("", False)])
def test_rotation_is_available_only_with_a_control_url(url, expected):
    assert build(FakeGluetun(), url=url)[0].enabled is expected


def test_rotation_stops_and_restarts_the_vpn_and_waits_for_a_new_exit():
    gluetun = FakeGluetun()
    rotator, _ = build(gluetun)
    assert rotator.rotate() is True
    puts = [call[2] for call in gluetun.calls if call[0] == "PUT"]
    assert puts == [{"status": "stopped"}, {"status": "running"}]
    assert gluetun.ip == "2.2.2.2"
    assert all(call[2] == {"X-API-Key": "secret"} for call in gluetun.calls if call[0] == "GET")


def test_rotation_is_limited_to_one_per_interval_but_reports_a_fresh_one():
    gluetun = FakeGluetun(ips=("1.1.1.1", "2.2.2.2", "3.3.3.3"))
    rotator, clock = build(gluetun)
    assert rotator.rotate() is True
    clock[0] += 30
    # Another searcher blocked moments later shares the rotation that just happened.
    assert rotator.rotate() is True
    clock[0] += 300
    assert rotator.rotate() is False
    assert gluetun.ip == "2.2.2.2"
    clock[0] += 15 * 60
    assert rotator.rotate() is True
    assert gluetun.ip == "3.3.3.3"


def test_failed_rotation_returns_false_and_still_counts_against_the_interval():
    gluetun = FakeGluetun(fail_put=True)
    rotator, clock = build(gluetun)
    assert rotator.rotate() is False
    clock[0] += 30
    assert rotator.rotate() is False
    assert len([c for c in gluetun.calls if c[0] == "PUT"]) == 1


def test_rotation_that_keeps_the_same_exit_is_not_a_success():
    gluetun = FakeGluetun(ips=("1.1.1.1", "1.1.1.1"))
    rotator, _ = build(gluetun)
    assert rotator.rotate() is False
    assert gluetun.status == "running"


def test_unreachable_control_server_is_a_failed_rotation(monkeypatch):
    http = Mock()
    http.get.side_effect = vpn_rotator.requests.ConnectionError("refused")
    rotator, _ = build(http)
    assert rotator.rotate() is False


def test_api_key_never_appears_in_logs():
    gluetun = FakeGluetun(fail_put=True)
    rotator, _ = build(gluetun, key="topsecret")
    rotator.rotate()
    logged = " ".join(str(call) for call in rotator.logger.method_calls)
    assert "topsecret" not in logged
