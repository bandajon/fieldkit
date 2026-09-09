#!/usr/bin/env python3
"""ffmpeg stream-copy recorder: one supervised process per camera."""

import json
import os
import re
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


def _alive(p):
    try:
        return p is not None and p.poll() is None
    except (OSError, ValueError):
        return p is not None       # uncertainty must retain ownership and file protection


def rtsp_url(cam):
    """Main stream. Never /102 — the sub stream is for live view only."""
    return (f"rtsp://{quote(cam['user'], safe='')}:{quote(cam['password'], safe='')}"
            f"@{cam['ip']}:554/Streaming/Channels/101")


def _graceful(p):
    """SIGINT (CTRL_BREAK on Windows) so ffmpeg finalises the last segment."""
    try:
        p.send_signal(signal.CTRL_BREAK_EVENT if WINDOWS else signal.SIGINT)
    except (OSError, ValueError):
        pass
    try:
        p.wait(timeout=10)
        return True
    except subprocess.TimeoutExpired:
        pass
    except (OSError, ValueError):
        if not _alive(p):
            return True
    try:
        p.terminate()
    except (OSError, ValueError):
        pass
    try:
        p.wait(timeout=5)
        return True
    except subprocess.TimeoutExpired:
        pass
    except (OSError, ValueError):
        if not _alive(p):
            return True
    try:
        p.kill()
    except (OSError, ValueError):
        pass
    try:
        p.wait(timeout=5)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        pass
    return not _alive(p)


class Recorder:
    def __init__(self, cameras, out_root, site, state_path=None):
        self.cams = {c["name"]: c for c in cameras}
        self.out_root = Path(out_root)
        self.site = site
        # Desired state survives the process: which cameras should record, until when.
        # Without it, a crash or reboot on an always-on field node silently ends recording.
        self.state_path = Path(state_path) if state_path else None
        self.st = {n: {"desired": False, "proc": None, "state": "STOPPED",
                       "started": None, "until": None, "restarts": 0, "alive_since": 0.0,
                       "next_spawn": 0.0, "backoff": 2.0, "tail": deque(maxlen=20),
                       "last_bytes": 0, "last_progress": 0.0, "active_file": None,
                       "writer_dir": None, "stopping": False}
                   for n in self.cams}
        self.lock = threading.Lock()
        threading.Thread(target=self._supervise, daemon=True).start()

    def add_camera(self, cam):
        """Make a newly configured camera recordable without a restart.
        ponytail: no remove API — dropping a camera stays an edit + restart."""
        with self.lock:
            name = cam["name"]
            self.cams[name] = cam
            old = self.st.get(name)
            if old and (old["desired"] or _alive(old["proc"])):
                return          # preserve ownership while recording or stopping
            self.st[name] = {"desired": False, "proc": None, "state": "STOPPED",
                             "started": None, "until": None, "restarts": 0, "alive_since": 0.0,
                             "next_spawn": 0.0, "backoff": 2.0, "tail": deque(maxlen=20),
                             "last_bytes": 0, "last_progress": 0.0, "active_file": None,
                             "writer_dir": None, "stopping": False}

    def remove_camera(self, name):
        """Forget a camera. Refuses while its session is active; files on disk stay."""
        with self.lock:
            s = self.st.get(name)
            if s and (s["desired"] or _alive(s["proc"])):
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
        return [f for f, _ in self._session_stats(name)]

    def _session_stats(self, name):
        """Stat each segment once; offload may unlink it at any point."""
        start = self.st[name]["started"] or 0
        if not start:
            return []
        found = []
        try:
            for f in self.cam_dir(name).glob("*.mkv"):
                try:
                    stat = f.stat()
                except OSError:
                    continue
                if stat.st_mtime >= start - 1:
                    found.append((f, stat.st_size))
        except OSError:
            pass
        return found

    def _spawn(self, name):
        d = self.cam_dir(name)
        cmd = ["ffmpeg", "-rtsp_transport", "tcp", "-use_wallclock_as_timestamps", "1",
               "-i", rtsp_url(self.cams[name]), "-c", "copy", "-f", "segment",
               "-segment_time", str(SEGMENT_SECONDS), "-reset_timestamps", "1",
               "-strftime", "1", str(d / "%Y%m%d-%H%M%S.mkv")]
        kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if WINDOWS else {}
        s = self.st[name]
        try:
            d.mkdir(parents=True, exist_ok=True)
            writer_dir = d.resolve()
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
        s.update(active_file=None, writer_dir=writer_dir, last_bytes=0, stopping=False)
        threading.Thread(target=self._drain, args=(name, p), daemon=True).start()

    def _drain(self, name, p):
        """Must consume stderr or the pipe fills and ffmpeg blocks."""
        for line in p.stderr:
            line = line.rstrip()
            with self.lock:
                s = self.st.get(name)
                if not s or s["proc"] is not p:
                    continue
                s["tail"].append(line)
                match = re.search(r"Opening '(.+)' for writing", line)
                if not match:
                    continue
                try:
                    path = Path(match.group(1)).resolve()
                    path.relative_to(s["writer_dir"])
                except (OSError, ValueError):
                    continue
                if path != s["active_file"]:
                    s.update(active_file=path, last_bytes=0, last_progress=time.time())

    def file_active(self, path):
        """Protect the writer; until ffmpeg names it, protect that camera's tree."""
        path = Path(path).resolve()
        with self.lock:
            for name, s in self.st.items():
                try:
                    if s["writer_dir"] is None:
                        continue
                    path.relative_to(s["writer_dir"])
                except ValueError:
                    continue
                p = s["proc"]
                if not _alive(p):
                    return False
                return s["active_file"] is None or path == s["active_file"]
        return False

    def _supervise(self, once=False):
        while True:
            now = time.time()
            expired = []
            restart = []
            with self.lock:
                for name, s in self.st.items():
                    if not s["desired"]:
                        p = s["proc"]
                        if _alive(p) and not s["stopping"]:
                            s["stopping"] = True
                            restart.append(p)
                        elif p is not None and not _alive(p):
                            s.update(proc=None, state="STOPPED", started=None, until=None,
                                     active_file=None, writer_dir=None, stopping=False)
                        continue
                    if s["until"] and now >= s["until"]:
                        expired.append(name)
                        continue
                    p = s["proc"]
                    if _alive(p):
                        # Liveness alone lies: ffmpeg stuck in TCP connect to a dead
                        # camera never exits. It only creates the segment file once the
                        # input is open and the header written, so existence proves a
                        # real connection — size can lag, matroska flushes in clusters.
                        files = self._session_stats(name)
                        active = s["active_file"]
                        nbytes = next((size for f, size in files if f == active), None)
                        if nbytes is None and active is not None:
                            try:
                                nbytes = active.stat().st_size
                            except OSError:
                                pass
                        if nbytes is not None and nbytes > s["last_bytes"]:
                            s["last_bytes"], s["last_progress"] = nbytes, now
                        if now - s["alive_since"] >= SETTLED and active is not None:
                            s["state"] = "RECORDING"
                            s["backoff"] = 2.0
                        # Watchdog. ffmpeg's own timeout flags differ across 4.x/5+/Jetson
                        # builds, so judge by bytes landing on disk instead.
                        stalled = (s["state"] == "RECORDING"
                                   and now - s["last_progress"] > STALL_SECONDS)
                        wedged = active is None and now - s["alive_since"] > NO_FILE_SECONDS
                        if (stalled or wedged) and not s["stopping"]:
                            s["tail"].append("[fieldkit] " + ("stalled" if stalled else
                                             "no stream opened") + " — restarting")
                            s["stopping"] = True
                            s["state"] = "RECONNECTING"
                            restart.append(p)
                        continue
                    if p is not None:            # it died while we still want it
                        s["restarts"] += 1
                        s["state"] = "RECONNECTING"
                        alive = now - s["alive_since"]
                        s["backoff"] = 2.0 if alive >= SETTLED else min(s["backoff"] * 2, MAX_BACKOFF)
                        s["next_spawn"] = now + s["backoff"]
                        s["proc"] = None
                        s["active_file"] = None
                        s["writer_dir"] = None
                    elif now >= s["next_spawn"]:
                        self._spawn(name)
            # Outside the lock: stop() takes it, and _graceful blocks up to 20 s per
            # process — supervision of the others pauses meanwhile, same as an operator Stop.
            if expired:
                self.stop(expired)
            for p in restart:
                if not _graceful(p):
                    with self.lock:
                        for s in self.st.values():
                            if s["proc"] is p:
                                s["stopping"] = False   # retry the bounded escalation next tick
            if once:
                return
            time.sleep(1)

    def _save_state(self):
        """Persist desired sessions. Caller holds the lock. Atomic: field nodes lose
        power mid-write, and a torn file must not poison the next boot."""
        if not self.state_path:
            return
        state = {n: {"until": s["until"]} for n, s in self.st.items() if s["desired"]}
        tmp = self.state_path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(state))
            os.replace(tmp, self.state_path)
        except OSError as e:      # a read-only or full disk must not break start/stop
            print(f"recorder: cannot save state: {e}", flush=True)

    def resume(self):
        """Re-arm the sessions that were live when the process last died."""
        if not self.state_path or not self.state_path.exists():
            return []
        try:
            saved = json.loads(self.state_path.read_text())
        except (json.JSONDecodeError, OSError):
            return []
        now, resumed = time.time(), []
        for n, e in saved.items():
            until = e.get("until") if isinstance(e, dict) else None
            if n not in self.cams:
                continue
            if until and until <= now:
                continue          # the deadline passed while the node was powered off
            self.start([n], hours=(until - now) / 3600 if until else None)
            resumed.append(n)
        if not resumed:
            with self.lock:
                self._save_state()   # drop expired/unknown entries so they never revive
        return resumed

    def start(self, names=None, hours=None):
        # A run length is optional: no hours (or a non-positive/garbage one) records
        # until someone presses Stop.
        try:
            hours = float(hours)
        except (TypeError, ValueError):
            hours = 0.0
        until = time.time() + hours * 3600 if hours > 0 else None
        with self.lock:
            for n in names or self.cams:
                s = self.st.get(n)
                if s is None or s["desired"]:
                    continue
                live = _alive(s["proc"])
                s.update(desired=True, state="RECONNECTING", started=time.time(),
                         until=until, restarts=0, backoff=2.0, next_spawn=0.0,
                         last_bytes=0)
                if not live:
                    s.update(active_file=None, writer_dir=None, stopping=False)
            self._save_state()

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
                    s["state"] = "RECONNECTING"
                    s["stopping"] = True
                    procs.append((n, s["proc"]))
        stopped = {n: (p, _graceful(p)) for n, p in procs}  # waits outside the lock
        with self.lock:
            for n in names:
                s = self.st.get(n)
                result = stopped.get(n)
                # A start() that landed mid-stop owns the state now; don't clobber it.
                if (s and not s["desired"] and
                        (not s["proc"] or (result and s["proc"] is result[0] and result[1]))):
                    s.update(proc=None, state="STOPPED", started=None, until=None,
                             active_file=None, writer_dir=None, stopping=False)
                elif s and not s["desired"] and result and s["proc"] is result[0]:
                    s["stopping"] = False       # supervisor retries the bounded stop
                elif s and s["desired"] and result and s["proc"] is result[0] and not result[1]:
                    s["stopping"] = False       # start() reclaimed the still-live process
            self._save_state()

    def shutdown(self):
        """Process exit: kill the children so an orphaned ffmpeg can't fight the
        restarted app for segment paths — but leave `desired` and the state file
        alone, so resume() re-arms these sessions on the next boot. stop() is the
        operator's intent; this is not."""
        with self.lock:
            procs = [s["proc"] for s in self.st.values() if s["proc"]]
        for p in procs:
            _graceful(p)     # SIGINT first: the last segment must finalise

    def status(self):
        now = time.time()
        out = {}
        with self.lock:
            for n, s in self.st.items():
                start = s["started"] or 0
                out[n] = {
                    "state": s["state"],
                    "bytes": sum(size for _, size in self._session_stats(n)),
                    # ponytail: wallclock since Start, not decoded duration — close enough
                    # for an operator; ffprobe the segments if exact footage time matters.
                    "minutes": round((now - start) / 60, 1) if start else 0,
                    "until": s["until"],
                    "restarts": s["restarts"],
                    # Whole tail (20 lines): the ops console's log drawer diagnoses a
                    # remote camera from exactly this — 5 lines hid the retry history.
                    "log": list(s["tail"]),
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

    # Timed run: hours= must end the session on its own — nobody calls stop() here.
    t = Recorder([{"name": "timed", "ip": "127.0.0.1", "user": "u", "password": "p"}],
                 tempfile.mkdtemp(), "selftest")
    t.start(["timed"], hours=0.001)          # 3.6 s
    assert t.status()["timed"]["until"], t.status()["timed"]
    for _ in range(20):
        if t.status()["timed"]["state"] == "STOPPED":
            break
        time.sleep(0.5)
    assert t.status()["timed"]["state"] == "STOPPED", t.status()["timed"]
    assert t.st["timed"]["desired"] is False, t.st["timed"]
    assert t.st["timed"]["until"] is None, t.st["timed"]
    t.start(["timed"], hours=0)              # non-positive = record until Stop
    assert t.st["timed"]["until"] is None
    t.stop(["timed"])

    # Desired state must survive the process: same state file, fresh Recorder, resume().
    sp = Path(tempfile.mkdtemp()) / "record_state.json"
    fake = {"name": "fake", "ip": "127.0.0.1", "user": "u", "password": "p"}
    p1 = Recorder([fake], tempfile.mkdtemp(), "selftest", state_path=sp)
    p1.start(["fake"], hours=1.0)                # then the process "dies" — no stop()
    p2 = Recorder([fake], tempfile.mkdtemp(), "selftest", state_path=sp)
    assert p2.resume() == ["fake"]
    s2 = p2.st["fake"]
    assert s2["desired"] and s2["until"] and s2["until"] > time.time() + 3500, s2["until"]
    p2.stop(["fake"])
    assert json.loads(sp.read_text()) == {}, "stop must clear the persisted session"
    assert p2.resume() == []                     # a clean stop stays stopped after reboot

    # A deadline that passed while the node was powered off must not revive.
    sp.write_text(json.dumps({"fake": {"until": time.time() - 5}, "ghost": {"until": None}}))
    p3 = Recorder([fake], tempfile.mkdtemp(), "selftest", state_path=sp)
    assert p3.resume() == []                     # expired + unconfigured camera
    assert not p3.st["fake"]["desired"]
    assert json.loads(sp.read_text()) == {}, "dead entries must be dropped, not kept"

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

    # An unresponsive child gets every bounded step, in order, and is reaped.
    class Stubborn:
        def __init__(self):
            self.calls, self.dead = [], False
        def send_signal(self, _sig):
            self.calls.append("sigint")
        def terminate(self):
            self.calls.append("terminate")
        def kill(self):
            self.calls.append("kill")
            self.dead = True
        def wait(self, timeout):
            self.calls.append(("wait", timeout))
            if not self.dead:
                raise subprocess.TimeoutExpired("ffmpeg", timeout)
        def poll(self):
            return 0 if self.dead else None
    stubborn = Stubborn()
    assert _graceful(stubborn)
    assert stubborn.calls == ["sigint", ("wait", 10), "terminate", ("wait", 5),
                              "kill", ("wait", 5)], stubborn.calls

    # Progress follows only the current writer: removed old segments, rotation and
    # disappearing files are routine, and output from an old process is ignored.
    class Alive:
        def __init__(self, lines=()):
            self.stderr = lines
        def poll(self):
            return None
    probe = Recorder.__new__(Recorder)
    probe.out_root, probe.site, probe.lock = Path(tempfile.mkdtemp()), "site", threading.Lock()
    probe.state_path = None
    probe.cams = {"cam": cam}
    d = probe.cam_dir("cam")
    d.mkdir(parents=True)
    old, current = d / "old.mkv", d / "current.mkv"
    old.write_bytes(b"x" * 1000)
    current.write_bytes(b"x" * 100)
    proc = Alive([f"Opening '{old}' for writing\n", f"Opening '{current}' for writing\n"])
    probe.st = {"cam": {"desired": True, "proc": proc, "state": "RECORDING",
                         "started": time.time() - 100, "until": None, "restarts": 0,
                         "alive_since": time.time() - SETTLED, "next_spawn": 0,
                         "backoff": 2, "tail": deque(maxlen=20), "last_bytes": 99,
                         "last_progress": time.time() - STALL_SECONDS - 1,
                         "active_file": None, "writer_dir": d.resolve(), "stopping": False}}
    probe._drain("cam", proc)
    assert probe.st["cam"]["active_file"] == current.resolve()
    assert probe.st["cam"]["last_bytes"] == 0, "rotation did not reset progress"
    probe._supervise(once=True)
    assert probe.st["cam"]["last_bytes"] == 100, "retained bytes counted as progress"
    old.unlink()                              # offload removed a retained segment
    with open(current, "ab") as f:
        f.write(b"x" * 20)
    probe.st["cam"]["last_progress"] = time.time() - STALL_SECONDS - 1
    probe._supervise(once=True)
    assert probe.st["cam"]["last_bytes"] == 120
    assert not probe.st["cam"]["stopping"], "old-segment deletion caused a false stall"
    stale = Alive([f"Opening '{old}' for writing\n"])
    probe._drain("cam", stale)
    assert probe.st["cam"]["active_file"] == current.resolve(), "stale stderr won"
    current.unlink()
    assert probe.status()["cam"]["bytes"] == 0, "vanished segment broke status"
    probe.st["cam"]["active_file"] = None
    old.write_bytes(b"old")
    assert probe.file_active(old), "unknown current filename did not protect the camera"
    probe.st["cam"]["desired"] = False
    probe.site = "changed-while-stopping"
    assert probe.file_active(old), "site change lost protection for the live writer"
    replacement = dict(cam, ip="10.0.0.2")
    probe.add_camera(dict(replacement, name="cam"))
    assert probe.cams["cam"]["ip"] == "10.0.0.2", "camera config was not updated"
    assert probe.st["cam"]["proc"] is proc and probe.file_active(old), \
        "config update detached the stopping writer"
    writer_dir = probe.st["cam"]["writer_dir"]
    probe.start(["cam"])
    assert probe.st["cam"]["writer_dir"] == writer_dir, "start detached the stopping writer"
    print("recorder self-check ok:", st["restarts"], "restart(s), watchdog + spawn-fail ok")
