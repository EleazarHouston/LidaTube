"""Rotate the VPN exit through Gluetun's control server when YouTube blocks searches.

LidaTube runs inside Gluetun's network namespace, so Gluetun's control server answers on
127.0.0.1. Stopping and restarting the VPN makes Gluetun connect to another server and so
another exit IP, which is often unblocked. Rotations are spaced at least
vpn_rotation_min_minutes apart, so a block that follows LidaTube to every exit falls back to
the ordinary shared pause instead of cycling through servers.

Gluetun's control server needs a role for LidaTube's key allowing GET and PUT
/v1/vpn/status and GET /v1/publicip/ip (see README).
"""

import logging
import threading
import time

import requests

# A rotation this recent is shared by every searcher that hit the same block.
FRESH_SECONDS = 120
STOP_TIMEOUT_SECONDS = 30
RECONNECT_TIMEOUT_SECONDS = 120
POLL_SECONDS = 3
REQUEST_TIMEOUT_SECONDS = 10


class RotationFailed(Exception):
    pass


class VpnRotator:
    def __init__(self, config, logger=None, clock=time.monotonic, sleep=time.sleep, http=requests):
        self.config = config
        self.logger = logger or logging.getLogger(__name__)
        self.clock = clock
        self.sleep = sleep
        self.http = http
        self._lock = threading.Lock()
        self._last_attempt = None
        self._last_success = None

    @property
    def enabled(self):
        return bool(self.config.gluetun_control_url)

    def log_status(self):
        if self.enabled:
            self.logger.warning(
                f"VPN rotation is on: a YouTube block that outlasts {int(self.config.vpn_rotation_after_backoffs)} "
                f"back-off(s) moves to a new exit, at most every {float(self.config.vpn_rotation_min_minutes):g} minutes")

    def _url(self, path):
        return self.config.gluetun_control_url.rstrip("/") + path

    def _headers(self):
        return {"X-API-Key": self.config.gluetun_api_key} if self.config.gluetun_api_key else {}

    def _get(self, path):
        response = self.http.get(self._url(path), headers=self._headers(), timeout=REQUEST_TIMEOUT_SECONDS)
        if response.status_code != 200:
            raise RotationFailed(f"GET {path} returned HTTP {response.status_code}")
        return response.json()

    def _set_status(self, status):
        response = self.http.put(self._url("/v1/vpn/status"), json={"status": status}, headers=self._headers(),
                                 timeout=REQUEST_TIMEOUT_SECONDS)
        if response.status_code != 200:
            raise RotationFailed(f"setting the VPN {status} returned HTTP {response.status_code}")

    def _public_ip(self):
        return (self._get("/v1/publicip/ip").get("public_ip") or "").strip()

    def _wait(self, timeout, ready):
        deadline = self.clock() + timeout
        while self.clock() < deadline:
            value = ready()
            if value:
                return value
            self.sleep(POLL_SECONDS)
        return None

    def rotate(self):
        """Move to a new VPN exit. True when searching should resume now, False to pause instead."""
        with self._lock:
            now = self.clock()
            if self._last_success is not None and now - self._last_success < FRESH_SECONDS:
                return True
            if self._last_attempt is not None and now - self._last_attempt < float(self.config.vpn_rotation_min_minutes) * 60:
                return False
            self._last_attempt = now
            stopped = False
            try:
                old_ip = self._public_ip()
                self._set_status("stopped")
                stopped = True
                if not self._wait(STOP_TIMEOUT_SECONDS, lambda: self._get("/v1/vpn/status").get("status") == "stopped"):
                    raise RotationFailed("the VPN did not stop")
                self._set_status("running")
                stopped = False
                new_ip = self._wait(RECONNECT_TIMEOUT_SECONDS, self._public_ip)
            except (RotationFailed, requests.RequestException, ValueError) as error:
                self.logger.error(f"VPN rotation failed: {type(error).__name__}: {error}")
                if stopped:
                    self._restart_quietly()
                return False
            if not new_ip:
                self.logger.error("VPN rotation failed: no public IP after reconnecting")
                return False
            if new_ip == old_ip:
                self.logger.warning("VPN rotation reconnected to the same exit")
                return False
            self._last_success = self.clock()
            self.logger.warning(f"Rotated the VPN exit after a YouTube block ({_masked(old_ip)} -> {_masked(new_ip)})")
            return True

    def _restart_quietly(self):
        try:
            self._set_status("running")
        except Exception as error:
            self.logger.error(f"Could not restart the VPN after a failed rotation: {type(error).__name__}")


def _masked(ip):
    parts = ip.split(".")
    return ".".join(parts[:2] + ["x", "x"]) if len(parts) == 4 else "?"
