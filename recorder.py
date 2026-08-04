#!/usr/bin/env python3
"""ffmpeg stream-copy recorder: one supervised process per camera."""

import signal
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from urllib.parse import quote

WINDOWS = sys.platform == "win32"
SEGMENT_SECONDS = 600
SETTLED = 5.0            # seconds a process must survive before it counts as RECORDING
MAX_BACKOFF = 30.0
STALL_SECONDS = 30.0     # RECORDING but no new bytes — matroska flushes every ~5 s,
NO_FILE_SECONDS = 60.0   # alive but never opened the stream at all


def rtsp_url(cam):
    """Main stream. Never /102 — the sub stream is for live view only."""
    return (f"rtsp://{quote(cam['user'], safe='')}:{quote(cam['password'], safe='')}"
            f"@{cam['ip']}:554/Streaming/Channels/101")


def _graceful(p):
    """SIGINT (CTRL_BREAK on Windows) so ffmpeg finalises the last segment."""
    try:
        p.send_signal(signal.CTRL_BREAK_EVENT if WINDOWS else signal.SIGINT)
    except (OSError, ValueError):
        return
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()


class Recorder:
    def __init__(self, cameras, out_root, site):
        self.cams = {c["name"]: c for c in cameras}
        self.out_root = Path(out_root)
        self.site = site
        self.st = {n: {"desired": False, "proc": None, "state": "STOPPED",
                       "started": None, "restarts": 0, "alive_since": 0.0,
                       "next_spawn": 0.0, "backoff": 2.0, "tail": deque(maxlen=20),
                       "last_bytes": 0, "last_progress": 0.0}
                   for n in self.cams}
        self.lock = threading.Lock()
        threading.Thread(target=self._supervise, daemon=True).start()

    def add_camera(self, cam):
        """Make a newly configured camera recordable without a restart.
        ponytail: no remove API — dropping a camera stays an edit + restart."""
        with self.lock:
            name = cam["name"]
            self.cams[name] = cam
            if self.st.get(name, {}).get("desired"):
                return          # already recording: leave the running session alone
            self.st[name] = {"desired": False, "proc": None, "state": "STOPPED",
                             "started": None, "restarts": 0, "alive_since": 0.0,
                             "next_spawn": 0.0, "backoff": 2.0, "tail": deque(maxlen=20),
                             "last_bytes": 0, "last_progress": 0.0}

    def remove_camera(self, name):
        """Forget a camera. Refuses while its session is active; files on disk stay."""
        with self.lock:
            s = self.st.get(name)
            if s and s["desired"]:
                return False
            self.cams.pop(name, None)
            self.st.pop(name, None)
            return True

    def cam_dir(self, name):
        return self.out_root / self.site / name

    def _session_files(self, name):
        """Segments written since this session's Start. Caller holds the lock."""
        start = self.st[name]["started"] or 0
        d = self.cam_dir(name)
        if not start or not d.is_dir():
            return []
        # -1s: coarse mtime granularity
        return [f for f in d.glob("*.mkv") if f.stat().st_mtime >= start - 1]

    def _spawn(self, name):
        d = self.cam_dir(name)
        d.mkdir(parents=True, exist_ok=True)
        cmd = ["ffmpeg", "-rtsp_transport", "tcp", "-use_wallclock_as_timestamps", "1",
               "-i", rtsp_url(self.cams[name]), "-c", "copy", "-f", "segment",
               "-segment_time", str(SEGMENT_SECONDS), "-reset_timestamps", "1",
               "-strftime", "1", str(d / "%Y%m%d-%H%M%S.mkv")]
        kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if WINDOWS else {}
        s = self.st[name]
        try:
            p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.PIPE, text=True, errors="replace", **kw)
        except OSError as e:      # ffmpeg missing from PATH must not kill the supervisor
            s["tail"].append(f"[fieldkit] cannot start ffmpeg: {e}")
            s["proc"] = None
            s["backoff"] = min(s["backoff"] * 2, MAX_BACKOFF)
            s["next_spawn"] = time.time() + s["backoff"]
            return
        s["proc"] = p
        s["alive_since"] = s["last_progress"] = time.time()
        threading.Thread(target=self._drain, args=(p, s["tail"]), daemon=True).start()

    @staticmethod
    def _drain(p, tail):
        """Must consume stderr or the pipe fills and ffmpeg blocks."""
        for line in p.stderr:
            tail.append(line.rstrip())

    def _supervise(self):
        while True:
            now = time.time()
            with self.lock:
                for name, s in self.st.items():
                    if not s["desired"]:
                        continue
                    p = s["proc"]
                    if p is not None and p.poll() is None:
                        # Liveness alone lies: ffmpeg stuck in TCP connect to a dead
                        # camera never exits. It only creates the segment file once the
                        # input is open and the header written, so existence proves a
                        # real connection — size can lag, matroska flushes in clusters.
                        files = self._session_files(name)
                        nbytes = sum(f.stat().st_size for f in files)
                        if nbytes > s["last_bytes"]:
                            s["last_bytes"], s["last_progress"] = nbytes, now
                        if now - s["alive_since"] >= SETTLED and files:
                            s["state"] = "RECORDING"
                            s["backoff"] = 2.0
                        # Watchdog. ffmpeg's own timeout flags differ across 4.x/5+/Jetson
                        # builds, so judge by bytes landing on disk instead.
                        stalled = (s["state"] == "RECORDING"
                                   and now - s["last_progress"] > STALL_SECONDS)
                        wedged = not files and now - s["alive_since"] > NO_FILE_SECONDS
                        if stalled or wedged:
                            s["tail"].append("[fieldkit] " + ("stalled" if stalled else
                                             "no stream opened") + " — restarting")
                            p.terminate()   # the died-branch handles RECONNECTING + backoff
                        continue
                    if p is not None:            # it died while we still want it
                        s["restarts"] += 1
                        s["state"] = "RECONNECTING"
                        alive = now - s["alive_since"]
                        s["backoff"] = 2.0 if alive >= SETTLED else min(s["backoff"] * 2, MAX_BACKOFF)
                        s["next_spawn"] = now + s["backoff"]
                        s["proc"] = None
                    elif now >= s["next_spawn"]:
                        self._spawn(name)
            time.sleep(1)

    def start(self, names=None):
        with self.lock:
            for n in names or self.cams:
                s = self.st.get(n)
                if s is None or s["desired"]:
                    continue
                s.update(desired=True, state="RECONNECTING", started=time.time(),
                         restarts=0, backoff=2.0, next_spawn=0.0, last_bytes=0)

    def stop(self, names=None):
        names = list(names or self.cams)
        with self.lock:
            procs = []
            for n in names:
                s = self.st.get(n)
                if s is None:
                    continue
                s["desired"] = False
                if s["proc"]:
                    procs.append(s["proc"])
        for p in procs:                          # signal outside the lock: waits up to 15 s
            _graceful(p)
        with self.lock:
            for n in names:
                s = self.st.get(n)
                # A start() that landed mid-stop owns the state now; don't clobber it.
                if s and not s["desired"]:
                    s.update(proc=None, state="STOPPED", started=None)

    def status(self):
        now = time.time()
        out = {}
        with self.lock:
            for n, s in self.st.items():
                start = s["started"] or 0
                out[n] = {
                    "state": s["state"],
                    "bytes": sum(f.stat().st_size for f in self._session_files(n)),
                    # ponytail: wallclock since Start, not decoded duration — close enough
                    # for an operator; ffprobe the segments if exact footage time matters.
                    "minutes": round((now - start) / 60, 1) if start else 0,
                    "restarts": s["restarts"],
                    "log": list(s["tail"])[-5:],
                }
        return out


if __name__ == "__main__":
    # Self-check: unreachable camera must be supervised, then stop cleanly.
    import tempfile
    root = tempfile.mkdtemp()
    # 127.0.0.1:554 has no RTSP server, so ffmpeg exits immediately every time.
    r = Recorder([{"name": "fake", "ip": "127.0.0.1", "user": "u", "password": "p"}],
                 root, "selftest")
    r.start(["fake"])
    time.sleep(8)
    st = r.status()["fake"]
    assert st["state"] == "RECONNECTING", st
    assert st["restarts"] >= 1, st
    r.stop(["fake"])
    st = r.status()["fake"]
    assert st["state"] == "STOPPED", st
    assert r.st["fake"]["proc"] is None

    assert "p%40ss%3Aw%2Frd" in rtsp_url(
        {"user": "admin", "password": "p@ss:w/rd", "ip": "10.0.0.1"})

    from unittest.mock import patch
    cam = {"name": "wedged", "ip": "10.0.0.1", "user": "u", "password": "p"}

    # Watchdog: alive but never opens the stream (stand-in: `sleep`, which writes
    # nothing) must be killed and retried rather than counted as RECORDING.
    NO_FILE_SECONDS = 3.0
    real_popen = subprocess.Popen
    w = Recorder([cam], tempfile.mkdtemp(), "selftest")
    with patch.object(subprocess, "Popen", lambda cmd, **kw: real_popen(["sleep", "300"], **kw)):
        w.start(["wedged"])
        time.sleep(8)
        assert w.status()["wedged"]["restarts"] >= 1, w.status()["wedged"]
        assert any("no stream opened" in x for x in w.st["wedged"]["tail"]), w.st["wedged"]["tail"]
        assert w.status()["wedged"]["state"] == "RECONNECTING", w.status()["wedged"]
    w.stop(["wedged"])

    # ffmpeg missing from PATH must back off, not kill the supervisor thread.
    b = Recorder([cam], tempfile.mkdtemp(), "selftest")
    with patch.object(subprocess, "Popen", side_effect=OSError("no ffmpeg")):
        b.start(["wedged"])
        time.sleep(4)
        assert b.st["wedged"]["backoff"] > 2.0, b.st["wedged"]["backoff"]   # kept looping
        assert any("cannot start ffmpeg" in x for x in b.st["wedged"]["tail"])
    b.stop(["wedged"])
    print("recorder self-check ok:", st["restarts"], "restart(s), watchdog + spawn-fail ok")
