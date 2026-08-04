#!/usr/bin/env python3
"""go2rtc sidecar: generate its config from the camera list, supervise one process."""

import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

from recorder import _graceful as graceful_stop   # same SIGINT-first stop as ffmpeg

WINDOWS = sys.platform == "win32"
API = "http://127.0.0.1:1984/api"
SETTLED = 5.0
MAX_BACKOFF = 30.0


def sub_url(cam):
    """SUB stream only. Hard rule: /101 is for recording, never for live view."""
    return f"rtsp://{cam['user']}:{cam['password']}@{cam['ip']}:554/Streaming/Channels/102"


def write_config(cameras, path):
    """Generate go2rtc.yaml. Contains passwords — keep it gitignored."""
    path = Path(path)
    path.write_text(yaml.safe_dump({"streams": {c["name"]: sub_url(c) for c in cameras}},
                                   sort_keys=False))
    return path


class Live:
    def __init__(self, cameras, binary, config_path):
        self.cameras = cameras
        self.binary = binary or ""
        self.config_path = Path(config_path)
        self.proc = None
        self.desired = False
        self.started_at = 0.0
        self.backoff = 2.0
        self.next_spawn = 0.0
        self.error = ""
        self._api_ok = False
        self._api_checked = 0.0
        self.lock = threading.Lock()
        threading.Thread(target=self._supervise, daemon=True).start()

    def available(self):
        """No binary configured = feature off. Never download one in the field."""
        return bool(self.binary) and Path(self.binary).exists()

    def _api_up(self, ttl=3.0):
        now = time.time()
        if now - self._api_checked < ttl:
            return self._api_ok
        self._api_checked = now
        try:
            with urllib.request.urlopen(API, timeout=1) as r:
                self._api_ok = r.status == 200
        except (urllib.error.URLError, OSError):
            self._api_ok = False
        return self._api_ok

    def state(self):
        if not self.available():
            return "absent"
        with self.lock:
            if not self.desired:
                return "stopped"
            if self.proc is None or self.proc.poll() is not None:
                return "reconnecting"
        # Alive is not enough: the player only works once the API answers on :1984.
        return "running" if self._api_up() else "starting"

    def info(self):
        return {"state": self.state(), "streams": len(self.cameras),
                "binary": self.binary, "error": self.error}

    def _spawn(self):
        write_config(self.cameras, self.config_path)
        kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if WINDOWS else {}
        try:
            self.proc = subprocess.Popen(
                [self.binary, "-config", str(self.config_path)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, **kw)
            self.started_at = time.time()
            self.error = ""
        except OSError as e:
            self.proc = None
            self.error = str(e)

    def _supervise(self):
        while True:
            with self.lock:
                if self.desired and self.available():
                    now = time.time()
                    p = self.proc
                    if p is not None and p.poll() is not None:
                        alive = now - self.started_at
                        self.backoff = 2.0 if alive >= SETTLED else min(self.backoff * 2, MAX_BACKOFF)
                        self.next_spawn = now + self.backoff
                        self.proc = None
                    elif p is None and now >= self.next_spawn:
                        self._spawn()
            time.sleep(1)

    def start(self):
        if self.available():
            with self.lock:
                self.desired = True
                self.backoff = 2.0
                self.next_spawn = 0.0
        return self.state()

    def stop(self):
        with self.lock:
            self.desired = False
            p, self.proc = self.proc, None
        if p:
            graceful_stop(p)
        return self.state()


if __name__ == "__main__":
    import tempfile
    cams = [{"name": "cam1", "ip": "192.168.1.111", "user": "admin", "password": "pw"}]
    p = Path(tempfile.mkdtemp()) / "go2rtc.yaml"
    text = write_config(cams, p).read_text()
    assert "/Streaming/Channels/102" in text, text
    assert "/101" not in text, text          # sub-stream discipline
    assert yaml.safe_load(text)["streams"]["cam1"].startswith("rtsp://admin:pw@")

    off = Live(cams, "", p)
    assert off.state() == "absent", off.state()
    assert off.start() == "absent"           # start must not resurrect a missing binary
    assert Live(cams, "/nonexistent/go2rtc", p).state() == "absent"
    print("live self-check ok: absent when unconfigured, config is sub-stream only")
