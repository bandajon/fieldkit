#!/usr/bin/env python3
"""Optional ops-console client: outbound heartbeat + remote start/stop.

Idle unless config.yaml has an `ops:` block with url, token and hive — a
self-hosting node never phones home. Protocol: docs/HIVE_PROTOCOL.md.
"""

import base64
import json
import platform
import socket
import subprocess
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from recorder import SEGMENT_SECONDS

try:
    from websockets.sync.client import connect      # threads, not asyncio
except ImportError:                                 # an old node that skipped pip install
    connect = None

BEAT_SECONDS = 30
COVERAGE_HOURS = 24
MAX_GAPS = 50            # oldest dropped first
MAX_SNAPSHOT = 60_000    # bytes; a bigger still is skipped for this round
MAX_BACKOFF = 60.0
REVOKED_SLEEP = 3600     # a revoked node must not hammer the console


def git_sha():
    """Version for the heartbeat. Tarball deploys have no .git — that is fine."""
    try:
        r = subprocess.run(["git", "-C", str(Path(__file__).resolve().parent),
                            "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


VERSION = git_sha()


def segment_spans(cam_dir):
    """(start_epoch, seconds) per segment under one camera directory.

    Start comes from the wallclock filename ffmpeg wrote; length from mtime, so
    the segment still being written — and one ffmpeg died inside — count for
    what they actually hold, not a full 600 s.
    """
    spans = []
    for f in Path(cam_dir).glob("*.mkv"):
        try:
            start = datetime.strptime(f.stem, "%Y%m%d-%H%M%S").timestamp()
            mtime = f.stat().st_mtime
        except (ValueError, OSError):
            continue          # anything not named by our -strftime pattern
        spans.append((start, max(0.0, min(SEGMENT_SECONDS, mtime - start))))
    return spans


def coverage(spans, now, hours=COVERAGE_HOURS):
    """Recorded fraction of the last `hours`, plus the holes as epoch pairs."""
    win = hours * 3600
    start = now - win
    clipped = sorted((max(a, start), min(a + d, now))
                     for a, d in spans if a + d > start and a < now)
    gaps, cursor = [], start
    for a, b in clipped:
        if a > cursor:
            gaps.append([round(cursor), round(a)])
        cursor = max(cursor, b)
    if cursor < now:
        gaps.append([round(cursor), round(now)])
    missing = sum(b - a for a, b in gaps)
    return {"pct": round(100 * (1 - missing / win), 1), "gaps": gaps[-MAX_GAPS:]}


class Hive:
    def __init__(self, cfg, rec, record_status, snapshot, ips):
        self.cfg = cfg                  # the live CONFIG dict: edits land without a reload
        self.rec = rec
        self.record_status = record_status
        self.snapshot = snapshot
        self.ips = ips
        self.seq = 0
        self.state = "OFF"
        self.last_beat = 0.0
        self.log = deque(maxlen=20)     # app-appendable; recorder lines join it per beat

    def opts(self):
        return self.cfg.get("ops") or {}

    def configured(self):
        o = self.opts()
        return all(o.get(k) for k in ("url", "token", "hive"))

    def info(self):
        return {"configured": self.configured(), "state": self.state,
                "last_beat": self.last_beat, "seq": self.seq}

    def start(self):
        if not self.configured():
            return                      # no ops block = never phones home
        if connect is None:
            print("hive: ops configured but websockets missing — pip install websockets",
                  flush=True)
            return
        threading.Thread(target=self._loop, daemon=True).start()

    # --- heartbeat ---

    def coverage_all(self, now=None):
        now = now or time.time()
        return {n: coverage(segment_spans(self.rec.cam_dir(n)), now)
                for n in self.rec.cams}

    def snapshots(self):
        """One still per camera, in parallel: eight serial 8 s timeouts would
        eat the whole beat interval."""
        cams = self.cfg.get("cameras") or []

        def shot(c):
            data, err = self.snapshot(c.get("ip", ""), c.get("user", ""),
                                      c.get("password", ""))
            if err or not data or len(data) > MAX_SNAPSHOT:
                return None
            return c["name"], base64.b64encode(data).decode()

        if not cams:
            return {}
        with ThreadPoolExecutor(max_workers=len(cams)) as ex:
            return dict(r for r in ex.map(shot, cams) if r)

    def heartbeat(self, with_snapshots):
        o = self.opts()
        rec = self.record_status()
        lines = [f"{n} {line}" for n, c in rec.get("cameras", {}).items()
                 for line in c.get("log", [])]
        self.seq += 1
        doc = {"type": "heartbeat", "token": o.get("token", ""),
               "hive": o.get("hive", ""), "node": socket.gethostname(),
               "seq": self.seq, "sent": time.time(), "ips": self.ips(),
               "versions": {"fieldkit": VERSION, "python": platform.python_version()},
               "record": rec, "coverage": self.coverage_all(),
               "log": (list(self.log) + lines)[-20:]}
        if with_snapshots:
            doc["snapshots"] = self.snapshots()
        return doc

    # --- commands ---

    def apply(self, cmd):
        """Same recorder calls the local Record tab makes. Idempotent: the
        console re-sends an unacked command when a node reconnects."""
        cmd_id, action = cmd.get("cmd_id", ""), cmd.get("action")
        cams = cmd.get("cams") or None
        ack = {"type": "ack", "cmd_id": cmd_id, "ok": True, "applied": [], "error": ""}
        try:
            if action == "start":
                self.rec.start(cams, hours=cmd.get("hours"))
            elif action == "stop":
                self.rec.stop(cams)
            else:
                raise ValueError(f"unknown action {action!r}")
        except Exception as e:
            ack.update(ok=False, error=f"{type(e).__name__}: {e}")
            self.log.append(f"{time.strftime('%H:%M:%S')} ops {action} failed: {e}")
            return ack
        # Names this node does not have are silently skipped by the recorder —
        # the ack must report what actually moved, not what was asked for.
        ack["applied"] = sorted(n for n in (cams or self.rec.cams) if n in self.rec.cams)
        self.log.append(f"{time.strftime('%H:%M:%S')} ops {action} "
                        f"{', '.join(ack['applied']) or '(none)'} [{cmd_id}]")
        print(f"hive: {action} from console: {ack['applied']}", flush=True)
        return ack

    # --- connection ---

    def _session(self, ws):
        """Beat, then listen until the next beat is due. Returns on revocation."""
        beats = 0
        while True:
            ws.send(json.dumps(self.heartbeat(beats % 2 == 0)))
            self.last_beat = time.time()
            beats += 1
            deadline = self.last_beat + BEAT_SECONDS
            while True:
                left = deadline - time.time()
                if left <= 0:
                    break
                try:
                    msg = json.loads(ws.recv(timeout=left))
                except TimeoutError:
                    break
                if msg.get("type") == "command":
                    ws.send(json.dumps(self.apply(msg)))
                    break                      # then an immediate fresh heartbeat
                if msg.get("type") == "rejected":
                    self.state = "REVOKED"
                    reason = msg.get("reason", "")
                    self.log.append(f"{time.strftime('%H:%M:%S')} rejected: {reason}")
                    print(f"hive: rejected by console: {reason}", flush=True)
                    return

    def _loop(self):
        backoff = 2.0
        while True:
            if self.state == "REVOKED":
                time.sleep(REVOKED_SLEEP)      # retry hourly, not every 2 s
            self.state = "CONNECTING"
            try:
                with connect(self.opts()["url"], open_timeout=10) as ws:
                    self.state = "CONNECTED"
                    backoff = 2.0
                    self._session(ws)
            except Exception as e:             # any failure reconnects; never dies
                self.log.append(f"{time.strftime('%H:%M:%S')} {type(e).__name__}: {e}")
                print(f"hive: {type(e).__name__}: {e}", flush=True)
            if self.state != "REVOKED":
                time.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)


if __name__ == "__main__":
    import os
    import tempfile

    NOW = 1754820000.0
    DAY = 24 * 3600

    # Coverage arithmetic, straight on spans — no directory, no sockets.
    assert coverage([(NOW - DAY, DAY)], NOW) == {"pct": 100.0, "gaps": []}
    assert coverage([], NOW)["pct"] == 0.0
    assert coverage([], NOW)["gaps"] == [[round(NOW - DAY), round(NOW)]]
    # One 600 s hole in an otherwise full day.
    full = [(NOW - DAY + i * 600, 600) for i in range(DAY // 600)]
    c = coverage([s for s in full if s[0] != NOW - DAY + 6000], NOW)
    assert c["gaps"] == [[round(NOW - DAY + 6000), round(NOW - DAY + 6600)]], c
    assert c["pct"] == round(100 * (1 - 600 / DAY), 1), c
    # Segments older than the window, and overlapping ones, must not double-count.
    assert coverage([(NOW - 3 * DAY, 600)], NOW)["pct"] == 0.0
    assert coverage([(NOW - 1200, 600), (NOW - 1500, 900)], NOW)["gaps"][-1] == \
        [round(NOW - 600), round(NOW)]
    # Cap: 61 holes in (one before each segment, one trailing), 50 out, oldest dropped.
    many = coverage([(NOW - DAY + i * 1200 + 600, 600) for i in range(60)], NOW)
    assert len(many["gaps"]) == MAX_GAPS, len(many["gaps"])
    assert many["gaps"][0][0] == round(NOW - DAY + 11 * 1200), many["gaps"][0]
    assert many["gaps"][-1][1] == round(NOW), many["gaps"][-1]

    # Filenames on disk: only our -strftime pattern counts, length comes from mtime.
    d = Path(tempfile.mkdtemp())
    for i, name in enumerate(("20260808-100000.mkv", "20260808-101000.mkv")):
        (d / name).write_bytes(b"x")
        base = datetime(2026, 8, 8, 10, i * 10).timestamp()
        os.utime(d / name, (base + 600, base + 600))
    (d / "notes.txt").write_text("x")
    (d / "partial.mkv").write_bytes(b"x")            # no wallclock name: ignored
    spans = sorted(segment_spans(d))
    assert len(spans) == 2, spans
    assert spans[0] == (datetime(2026, 8, 8, 10, 0).timestamp(), 600.0), spans
    later = coverage(spans, spans[0][0] + 1200)
    assert later["pct"] == round(100 * 1200 / DAY, 1), later
    # A segment ffmpeg is still writing counts for the seconds it actually holds.
    live = d / "20260808-102000.mkv"
    live.write_bytes(b"x")
    base = datetime(2026, 8, 8, 10, 20).timestamp()
    os.utime(live, (base + 120, base + 120))
    assert sorted(segment_spans(d))[-1][1] == 120.0, sorted(segment_spans(d))

    # No ops block: configured False, start() spawns nothing at all.
    class FakeRec:
        def __init__(self):
            self.cams, self.calls = {"cam1": {}, "cam2": {}}, []

        def start(self, names=None, hours=None):
            self.calls.append(("start", names, hours))

        def stop(self, names=None):
            self.calls.append(("stop", names))

        def cam_dir(self, name):
            return d

    def status():
        return {"cameras": {"cam1": {"state": "RECORDING", "log": ["frame=1"]}},
                "disk_free_gb": 9.9}

    def no_snapshot(ip, user, password):
        return None, "no camera here"

    before = threading.active_count()
    off = Hive({}, FakeRec(), status, no_snapshot, lambda: ["10.0.0.2"])
    off.start()
    assert off.info() == {"configured": False, "state": "OFF", "last_beat": 0.0, "seq": 0}
    assert threading.active_count() == before, "started a thread without an ops block"
    assert not Hive({"ops": {"url": "ws://x/ingest", "token": ""}}, FakeRec(), status,
                    no_snapshot, list).configured(), "half-filled ops block counts as on"

    # Command dispatch over a fake socket: heartbeat, ack, then a fresh heartbeat.
    class Done(Exception):
        pass

    class FakeWS:
        def __init__(self, frames):
            self.frames, self.sent = list(frames), []

        def send(self, text):
            self.sent.append(json.loads(text))

        def recv(self, timeout=None):
            if not self.frames:
                raise Done
            f = self.frames.pop(0)
            if f is None:
                raise TimeoutError
            return json.dumps(f)

    CFG = {"ops": {"url": "ws://console:8090/ingest", "token": "tok", "hive": "kalambo"},
           "cameras": [{"name": "cam1", "ip": "10.0.0.5", "user": "u", "password": "p"}]}
    rec = FakeRec()
    h = Hive(CFG, rec, status, no_snapshot, lambda: ["10.0.0.2", "100.64.0.7"])
    ws = FakeWS([{"type": "command", "cmd_id": "0142", "action": "start",
                  "cams": ["cam1", "ghost"], "hours": 4.0}])
    try:
        h._session(ws)
    except Done:
        pass
    hb, ack, hb2 = ws.sent
    assert hb["type"] == "heartbeat" and hb["seq"] == 1 and hb2["seq"] == 2, ws.sent
    assert hb["token"] == "tok" and hb["hive"] == "kalambo", hb
    assert hb["record"]["disk_free_gb"] == 9.9 and hb["ips"][0] == "10.0.0.2", hb
    assert hb["coverage"]["cam1"]["pct"] >= 0 and "cam2" in hb["coverage"], hb["coverage"]
    assert hb["log"][-1] == "cam1 frame=1", hb["log"]
    assert hb["snapshots"] == {}, hb["snapshots"]      # unreachable camera is skipped
    assert "snapshots" not in hb2, "snapshots must ride every 2nd beat only"
    assert rec.calls == [("start", ["cam1", "ghost"], 4.0)], rec.calls
    assert ack == {"type": "ack", "cmd_id": "0142", "ok": True,
                   "applied": ["cam1"], "error": ""}, ack     # ghost is not on this node
    assert h.log and "0142" in h.log[-1], list(h.log)

    # stop, cams: null = every camera; an unknown action acks a failure, never crashes.
    ws = FakeWS([{"type": "command", "cmd_id": "0143", "action": "stop", "cams": None},
                 {"type": "command", "cmd_id": "0144", "action": "wipe"}])
    try:
        h._session(ws)
    except Done:
        pass
    assert rec.calls[-1] == ("stop", None), rec.calls
    assert ws.sent[1]["applied"] == ["cam1", "cam2"], ws.sent[1]
    bad = ws.sent[3]
    assert bad["ok"] is False and "unknown action" in bad["error"], bad

    # A rejected node stops beating and marks itself for the hourly retry.
    ws = FakeWS([{"type": "rejected", "reason": "unknown or revoked token"}])
    h.state = "CONNECTED"
    h._session(ws)
    assert h.state == "REVOKED" and len(ws.sent) == 1, (h.state, ws.sent)
    assert h.info()["configured"] and h.info()["last_beat"] > 0, h.info()

    print("hive self-check ok: coverage arithmetic, command dispatch, off without config")
