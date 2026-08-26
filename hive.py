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
SWITCHES = ("mirror", "contribute", "detect")   # node-wide on/off, from app.py CONTROLS


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


def enrolled(cfg):
    """True when config.yaml has a complete ops block — a node the console can reach.

    offload.py asks this too, to tell "waiting on the console" from "you forgot to
    fill this in"; keep it the one definition of enrolled.
    """
    o = (cfg or {}).get("ops") or {}
    return all(o.get(k) for k in ("url", "token", "hive"))


class Hive:
    def __init__(self, cfg, rec, record_status, snapshot, ips, console_creds=None,
                 controls=None):
        self.cfg = cfg                  # the live CONFIG dict: edits land without a reload
        # Console-pushed creds live in their own dict, shared with Offload — never in
        # cfg, which app.py writes back to config.yaml whenever a camera is edited.
        self.console_creds = {} if console_creds is None else console_creds
        self.rec = rec
        # app.py's CONTROLS: {"mirror"|"contribute"|"detect": fn(bool)}. Absent means
        # this build has no such switch — apply() then says so instead of crashing.
        self.controls = controls or {}
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
        return enrolled(self.cfg)

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
        # offload.info() reports `mirror`; `contribute` is the sibling switch in the
        # same config block and is not in that dict yet. setdefault, so the day
        # offload.info() does report it — override included — the real value wins.
        if isinstance(rec.get("offload"), dict):
            rec["offload"].setdefault(
                "contribute", bool((self.cfg.get("offload") or {}).get("contribute")))
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
        """The same calls the local UI makes — recorder start/stop, plus the
        node-wide switches in `controls`. Idempotent: the console re-sends an
        unacked command when a node reconnects."""
        cmd_id, action = cmd.get("cmd_id", ""), cmd.get("action")
        cams = cmd.get("cams") or None
        on = bool(cmd.get("on"))
        ack = {"type": "ack", "cmd_id": cmd_id, "ok": True, "applied": [], "error": ""}
        try:
            if action == "start":
                self.rec.start(cams, hours=cmd.get("hours"))
            elif action == "stop":
                self.rec.stop(cams)
            elif action in SWITCHES:
                if action not in self.controls:
                    raise ValueError(f"this node has no {action} switch")
                self.controls[action](on)
            else:
                raise ValueError(f"unknown action {action!r}")
        except Exception as e:
            ack.update(ok=False, error=f"{type(e).__name__}: {e}")
            self.log.append(f"{time.strftime('%H:%M:%S')} ops {action} failed: {e}")
            return ack
        # The ack reports what actually moved, not what was asked for: names this node
        # does not have are silently skipped by the recorder, and a switch is node-wide,
        # so it names the state it moved to instead of a camera list.
        ack["applied"] = (["on" if on else "off"] if action in SWITCHES else
                          sorted(n for n in (cams or self.rec.cams) if n in self.rec.cams))
        self.log.append(f"{time.strftime('%H:%M:%S')} ops {action} "
                        f"{', '.join(ack['applied']) or '(none)'} [{cmd_id}]")
        print(f"hive: {action} from console: {ack['applied']}", flush=True)
        return ack

    def offload_creds(self, msg):
        """Console-pushed R2 credentials into the overlay dict. Never cfg, never disk.

        Each frame REPLACES the overlay wholesale, so rotating the key on the
        console reaches a running node on its next connect. Which value actually
        gets used is offload.py's merge — config.yaml still wins field by field.
        """
        need = ("account_id", "access_key_id", "secret_access_key")
        fresh = {f: msg[f] for f in need + ("bucket",)
                 if isinstance(msg.get(f), str) and msg[f]}
        if not all(f in fresh for f in need):
            return          # not a usable set: a malformed frame must not evict good creds
        if fresh == self.console_creds:
            return          # a flapping link must not push real diagnostics out of the log
        self.console_creds.clear()
        self.console_creds.update(fresh)
        self.log.append(f"{time.strftime('%H:%M:%S')} offload creds received from console")
        print("hive: offload creds received from console", flush=True)

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
                if msg.get("type") == "offload_creds":
                    self.offload_creds(msg)
                    continue                   # fire-and-forget: no ack, keep listening
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

    # Node-wide switches. Without a controls dict — an old node, or a build compiled
    # without the feature — the ack names what this node cannot do instead of crashing,
    # and the recorder is never touched on the way past.
    ws = FakeWS([{"type": "command", "cmd_id": "0145", "action": "mirror", "on": True}])
    try:
        h._session(ws)
    except Done:
        pass
    nope = ws.sent[1]
    assert nope["ok"] is False and "no mirror switch" in nope["error"], nope
    assert rec.calls[-1] == ("stop", None), rec.calls

    flipped = []
    hs = Hive(CFG, rec, status, no_snapshot, list,
              controls={"mirror": lambda on: flipped.append(("mirror", on)),
                        "contribute": lambda on: flipped.append(("contribute", on))})
    ws = FakeWS([{"type": "command", "cmd_id": "0146", "action": "mirror", "on": True},
                 {"type": "command", "cmd_id": "0147", "action": "contribute", "on": False},
                 {"type": "command", "cmd_id": "0148", "action": "detect", "on": True}])
    try:
        hs._session(ws)
    except Done:
        pass
    assert flipped == [("mirror", True), ("contribute", False)], flipped
    acks = [m for m in ws.sent if m["type"] == "ack"]
    assert acks[0] == {"type": "ack", "cmd_id": "0146", "ok": True,
                       "applied": ["on"], "error": ""}, acks[0]
    assert acks[1]["applied"] == ["off"], acks[1]
    # A switch this build lacks is a missing control, never "unknown action".
    assert acks[2]["ok"] is False and "no detect switch" in acks[2]["error"], acks[2]
    assert "mirror on" in hs.log[0] and hs.log[0].endswith("[0146]"), list(hs.log)

    # Both switch states must reach the console on the beat. `mirror` already rides in
    # the record status; `contribute` is filled in from config — but never invented on a
    # node that reports no offload block at all, and never over a real reported value.
    def off_status():
        return dict(status(), offload={"enabled": True, "mirror": True})

    beat = Hive(dict(CFG, offload={"contribute": True}), rec, off_status, no_snapshot,
                list).heartbeat(False)
    assert beat["record"]["offload"] == {"enabled": True, "mirror": True,
                                         "contribute": True}, beat["record"]["offload"]
    assert "offload" not in Hive(CFG, rec, status, no_snapshot,
                                 list).heartbeat(False)["record"], "invented an offload block"
    beat = Hive(dict(CFG, offload={"contribute": True}), rec,
                lambda: dict(status(), offload={"contribute": False}),
                no_snapshot, list).heartbeat(False)
    assert beat["record"]["offload"]["contribute"] is False, beat["record"]["offload"]

    # Console-pushed creds land in the overlay, never in CONFIG, and are never acked.
    CFG["offload"] = {"enabled": True, "bucket": "operator-bucket"}   # from config.yaml
    overlay = {}
    hc = Hive(CFG, rec, status, no_snapshot, list, console_creds=overlay)
    frame = {"type": "offload_creds", "account_id": "acc", "access_key_id": "AK",
             "secret_access_key": "SK", "bucket": "console-bucket"}
    ws = FakeWS([frame])
    try:
        hc._session(ws)
    except Done:
        pass
    assert overlay == {"account_id": "acc", "access_key_id": "AK",
                       "secret_access_key": "SK", "bucket": "console-bucket"}, overlay
    assert CFG["offload"] == {"enabled": True, "bucket": "operator-bucket"}, CFG["offload"]
    assert [m["type"] for m in ws.sent] == ["heartbeat"], ws.sent   # fire-and-forget
    assert "offload creds" in hc.log[-1] and "SK" not in hc.log[-1], list(hc.log)

    # A re-send replaces the overlay wholesale, so a rotated key reaches the node —
    # and an identical re-send is silent, or a flapping link would flood the log.
    logged = len(hc.log)
    hc.offload_creds(frame)
    assert len(hc.log) == logged, list(hc.log)
    hc.offload_creds(dict(frame, access_key_id="AK2", bucket="b2"))
    assert overlay["access_key_id"] == "AK2" and overlay["bucket"] == "b2", overlay
    assert len(hc.log) == logged + 1, list(hc.log)

    # Garbage changes nothing, logs nothing, and never breaks the session. An empty
    # `offload:` block in config.yaml parses as None — that must not crash either.
    h2 = Hive({"offload": None}, rec, status, no_snapshot, list, console_creds=overlay)
    ws = FakeWS([{"type": "offload_creds", "access_key_id": 42},   # not a string
                 {"type": "offload_creds"},                        # no fields at all
                 {"type": "offload_creds", "bucket": "only-bucket"},   # no cred triple
                 {"type": "nonsense"}])
    try:
        h2._session(ws)
    except Done:
        pass
    assert overlay["access_key_id"] == "AK2" and overlay["bucket"] == "b2", overlay
    assert not h2.log, list(h2.log)
    assert h2.cfg == {"offload": None}, h2.cfg     # the node's own config is untouched

    # A rejected node stops beating and marks itself for the hourly retry.
    ws = FakeWS([{"type": "rejected", "reason": "unknown or revoked token"}])
    h.state = "CONNECTED"
    h._session(ws)
    assert h.state == "REVOKED" and len(ws.sent) == 1, (h.state, ws.sent)
    assert h.info()["configured"] and h.info()["last_beat"] > 0, h.info()

    print("hive self-check ok: coverage arithmetic, command dispatch, node switches, "
          "offload creds, off without config")
