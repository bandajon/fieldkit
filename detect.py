#!/usr/bin/env python3
"""Monitor detection: RTSP sub-stream -> YOLO+ByteTrack -> daily vehicle totals.

One ffmpeg reader per camera pipes ~5 fps of mjpeg; the worker tracks by ByteTrack
id and counts each id once. ultralytics and Pillow are optional and imported inside
the worker thread, so a node without them reports state "absent" and the rest of
FieldKit runs. Design: docs/superpowers/specs/2026-08-19-video-feed-and-labeling-design.md
"""

import io
import subprocess
import threading
import time
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

from live import sub_url          # /102 by contract — the recorder owns /101

CLASSES = {2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}      # COCO ids
CLASS_IDS = {n: i for i, n in enumerate(CLASSES.values())}       # dataset ids, classes.txt order
COLORS = {"car": (255, 208, 0), "motorcycle": (255, 0, 200),
          "bus": (0, 210, 255), "truck": (230, 40, 40)}
WEIGHTS = "yolov8n.pt"
CONF = 0.4
IMGSZ = 1280      # sub-streams are 1280x720, so this is ~native; 640 missed distant trucks
FPS = 5           # ffmpeg decimates to this — the loop then runs flat out
COUNT_AT_HITS = 2        # an id counts on its 2nd sighting: one-frame blips never count
ID_EXPIRY = 10.0         # forget an id unseen this long (ByteTrack recycles ids; caps memory)
FRAME_STALE = 10.0       # a frame older than this is not "live" any more
IDLE_WAIT = 0.2          # no new frames anywhere: don't spin
SETTLED = 5.0            # a reader that survives this long resets its backoff
MAX_BACKOFF = 30.0
CAPTURE_EVERY = 30.0     # dataset samples per camera
DATASET_CAP = 1000       # rolling buffer of the freshest unlabeled frames
SOI, EOI = b"\xff\xd8", b"\xff\xd9"
MAX_BUF = 8_000_000      # a camera emitting garbage must not grow the buffer forever
PIP_HINT = "detection needs: pip install ultralytics pillow"
HAILO_HINT = ("hailo backend not implemented yet — runs with detect_backend: cpu; "
              "HailoRT integration lands with the site deploy")


def label_of(t):
    """Majority class of a track. A tie keeps the current label: deterministic, and a
    50/50 track then stays put instead of flapping between two names."""
    top = max(t["votes"].values())
    return t["label"] if t["votes"][t["label"]] == top else t["votes"].most_common(1)[0][0]


def annotate(img, dets, quality=85):
    """Boxes + labels burnt into the frame; returns JPEG bytes."""
    from PIL import ImageDraw, ImageFont

    try:
        # Scale with the frame: the ~11 px default font is unreadable once a 1280-wide
        # frame is shrunk into a browser tile.
        font = ImageFont.load_default(size=max(14, img.width // 60))
    except TypeError:                     # Pillow < 10.1 has no size argument
        font = ImageFont.load_default()
    d = ImageDraw.Draw(img)
    for cls, conf, (x1, y1, x2, y2) in dets:
        color = COLORS.get(cls, (255, 255, 255))
        d.rectangle([x1, y1, x2, y2], outline=color, width=max(2, img.width // 640))
        label = f"{cls} {conf:.0%}"
        lx1, ly1, lx2, ly2 = d.textbbox((0, 0), label, font=font)
        tw, th = lx2 - lx1, ly2 - ly1
        top = max(0, y1 - th - 5)                 # a box at the top edge keeps its label
        d.rectangle([x1, top, x1 + tw + 7, top + th + 5], fill=color)
        d.text((x1 + 3, top + 2), label, font=font, fill=(0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


class Reader:
    """One ffmpeg per camera decoding the sub stream to mjpeg on stdout. Keeps only the
    latest complete frame — the inference loop is slower than the stream and old frames
    are worthless. Restarts itself with backoff, recorder-style."""

    def __init__(self, url):
        self.url = url
        self.frame = None
        self.seq = 0
        self.proc = None
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def latest(self):
        with self.lock:
            return self.frame, self.seq

    def stop(self):
        self.stopping.set()
        p = self.proc
        if p:
            p.kill()      # nothing to finalise here, unlike the recorder's SIGINT dance
        self.thread.join(timeout=3)

    def _run(self):
        backoff = 2.0
        while not self.stopping.is_set():
            cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-rtsp_transport", "tcp",
                   "-i", self.url, "-vf", f"fps={FPS}", "-f", "image2pipe",
                   "-c:v", "mjpeg", "-q:v", "3", "-"]
            began = time.monotonic()
            try:
                # stderr to /dev/null, not a pipe: nothing reads it, and an unread pipe
                # would fill and wedge ffmpeg (recorder.py drains its own for the log UI).
                self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                             stdout=subprocess.PIPE,
                                             stderr=subprocess.DEVNULL)
                self._pump(self.proc.stdout)
                self.proc.kill()
            except OSError:               # ffmpeg missing or the pipe broke: just retry
                pass
            backoff = 2.0 if time.monotonic() - began >= SETTLED else min(backoff * 2, MAX_BACKOFF)
            self.stopping.wait(backoff)

    def _pump(self, out):
        buf = b""
        while not self.stopping.is_set():
            chunk = out.read1(65536)      # read1: whatever is ready, no 64 KB of latency
            if not chunk:
                return                    # ffmpeg died or the camera dropped us
            buf += chunk
            end = buf.rfind(EOI)          # rfind: skip straight to the newest whole frame
            start = buf.rfind(SOI, 0, end) if end > 0 else -1
            if start >= 0:
                with self.lock:
                    self.frame = buf[start:end + 2]
                    self.seq += 1
            buf = buf[end + 2:] if end > 0 else buf[-MAX_BUF:]


class Detector:
    def __init__(self, cameras, snapshot_fn, cfg, creds_fn=None, dataset_dir=None):
        self.cams = [dict(c) for c in (cameras or [])]
        self.snapshot = snapshot_fn     # unused here: app.py's frame() fallback while a reader is down
        self.creds_fn = creds_fn        # ip -> (user, password); else the cam dict's own
        self.dataset_dir = Path(dataset_dir) if dataset_dir else None
        self.backend = str(cfg.get("detect_backend", "cpu") or "cpu").strip().lower()
        self.hef_path = cfg.get("hef_path", "")   # hailo model; read here, used once that backend lands
        self.state = "stopped"
        self.error = ""
        self.day = date.today().isoformat()
        self.display = {n: n for n in CLASS_IDS}    # internal class -> operator's name
        self.display_ids = dict(CLASS_IDS)          # operator's name -> dataset id
        self.totals = {n: 0 for n in CLASS_IDS}
        self.tracks = {}      # camera name -> {track id: state}
        self.frames = {}      # camera name -> (annotated jpeg, wallclock ts)
        self.visible = {}     # camera name -> {class: count} on the latest frame
        self.readers = {}     # camera name -> Reader; owned by the worker thread
        self.models = {}      # camera name -> YOLO; ByteTrack state lives in the instance
        self.last_capture = {}
        self.lock = threading.Lock()
        self.thread = None
        self.stopping = threading.Event()

    # --- public API -------------------------------------------------------

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return self.state
            self.stopping.clear()
            self.state, self.error = "starting", ""
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()
        return self.state

    def stop(self):
        self.stopping.set()
        t = self.thread
        if t:
            t.join(timeout=3)
        for r in list(self.readers.values()):    # the worker is done with them by now
            r.stop()
        self.readers.clear()
        with self.lock:
            if self.state in ("starting", "running"):   # absent/error keep their hint
                self.state = "stopped"
        return self.state

    def set_cameras(self, cameras):
        """Totals are per-day, not per-camera, so they survive a camera being removed.
        Readers are started/stopped by the worker on its next pass."""
        cams = [dict(c) for c in (cameras or [])]
        keep = {c["name"] for c in cams}
        with self.lock:
            self.cams = cams
            for d in (self.frames, self.tracks, self.visible, self.models, self.last_capture):
                for gone in [n for n in d if n not in keep]:
                    d.pop(gone)

    def frame(self, name):
        with self.lock:
            f = self.frames.get(name)
        if not f or time.time() - f[1] > FRAME_STALE:
            return None                 # a stale frame must not masquerade as live
        return f[0]

    def counts(self):
        with self.lock:
            return {"state": self.state, "date": self.day, "totals": dict(self.totals),
                    "visible": {n: dict(v) for n, v in self.visible.items()}}

    def info(self):
        with self.lock:
            return {"state": self.state, "backend": self.backend, "error": self.error}

    # --- worker -----------------------------------------------------------

    def _set(self, state, error):
        with self.lock:
            self.state, self.error = state, error

    def _known(self, name):
        """Still configured? A pass in flight when set_cameras() drops a camera would
        otherwise re-insert the state it just cleaned up. Caller holds the lock."""
        return any(c["name"] == name for c in self.cams)

    def _url(self, cam):
        u, p = cam.get("user", ""), cam.get("password", "")
        if self.creds_fn:
            u, p = self.creds_fn(cam.get("ip", "")) or (u, p)
        return sub_url({**cam, "user": u, "password": p})

    def _sync_readers(self):
        """Worker-thread only, so self.readers needs no lock."""
        with self.lock:
            cams = {c["name"]: dict(c) for c in self.cams}
        for gone in [n for n in self.readers if n not in cams]:
            self.readers.pop(gone).stop()
        for name, cam in cams.items():
            if name not in self.readers:
                self.readers[name] = Reader(self._url(cam))

    def _load(self):
        """-> fn(camera name, image) -> [(cls, conf, box, track id)]. Runs in the thread:
        the first weight download (tens of MB over a field link) must not block app boot."""
        if self.backend != "cpu":
            raise RuntimeError(HAILO_HINT if self.backend == "hailo"
                               else f"unknown detect_backend: {self.backend}")
        from ultralytics import YOLO
        try:
            import torch
            dev = "mps" if torch.backends.mps.is_available() else "cpu"
        except Exception:                 # torch rides along with ultralytics; CPU regardless
            dev = "cpu"

        def track(name, img):
            model = self.models.get(name)
            if model is None:
                model = self.models[name] = YOLO(WEIGHTS)   # one per camera: tracker state
            out = []
            # agnostic_nms: one vehicle must not survive NMS as both a car and a truck box.
            for r in model.track(img, imgsz=IMGSZ, conf=CONF, classes=list(CLASSES),
                                 agnostic_nms=True, persist=True, tracker="bytetrack.yaml",
                                 device=dev, verbose=False):
                ids = r.boxes.id
                ids = ids.tolist() if ids is not None else [None] * len(r.boxes)
                for box, cid, conf, tid in zip(r.boxes.xyxy.tolist(), r.boxes.cls.tolist(),
                                               r.boxes.conf.tolist(), ids):
                    cls = CLASSES.get(int(cid))
                    if cls:
                        out.append((cls, float(conf), tuple(float(v) for v in box),
                                    None if tid is None else int(tid)))
            return out

        return track

    def _run(self):
        try:
            track = self._load()
            from PIL import Image
        except ImportError:
            self._set("absent", PIP_HINT)
            return
        except Exception as e:
            self._set("error", str(e))
            return
        self._set("running", "")
        self._dataset_init()
        seen_seq = {}
        while not self.stopping.is_set():
            self._roll_date()
            self._sync_readers()
            idle = True
            for name, reader in list(self.readers.items()):
                if self.stopping.is_set():
                    break
                jpeg, seq = reader.latest()
                if not jpeg or seq == seen_seq.get(name):
                    continue              # nothing new since the last pass
                seen_seq[name] = seq
                idle = False
                try:
                    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
                    dets = track(name, img)
                    shown = self._track(name, dets)      # voted labels, ready to draw
                    shot = annotate(img, shown)
                except Exception as e:    # a truncated frame must not kill the thread
                    self._set("running", str(e))
                    continue
                with self.lock:
                    if self._known(name):
                        self.frames[name] = (shot, time.time())
                    self.error = ""       # a one-off bad frame must not look permanent
                self._capture(name, jpeg, shown, img.width, img.height)
            if idle:
                self.stopping.wait(IDLE_WAIT)
        self._set("stopped", "")

    def _roll_date(self):
        today = date.today().isoformat()
        with self.lock:
            if today != self.day:
                self.day = today
                self.totals = {d: 0 for d in self.display.values()}
                self.tracks = {}

    def _track(self, name, dets):
        """Count by ByteTrack id, one count per id, into its majority class.
        Returns [(voted label, conf, box)] for drawing — the raw per-frame class is
        exactly what flickers when a car passes a truck.

        ponytail: ByteTrack at ~5 fps absorbs the old 1 fps ceiling (a parked or briefly
        occluded vehicle no longer re-counts). What remains: counts follow the effective
        fps the node sustains — under CPU load frames are dropped and a fast vehicle can
        cross unseen. Upgrade path: per-camera fps tuning, or the Hailo backend so
        inference stops competing with the recorder for CPU.
        """
        now = time.monotonic()
        shown = []
        with self.lock:
            if not self._known(name):
                return shown
            ids = self.tracks.setdefault(name, {})
            live = []
            for cls, conf, box, tid in dets:
                if tid is None:
                    # untracked: draw it, never count it
                    shown.append((self.display[cls], conf, box))
                    continue
                t = ids.get(tid)
                if t is None:
                    t = ids[tid] = {"votes": Counter(), "label": cls, "hits": 0,
                                    "counted_as": None, "last_seen": now}
                t["votes"][cls] += 1
                t["hits"] += 1
                t["last_seen"] = now
                t["label"] = label_of(t)
                # Votes and labels stay internal; only what leaves here is renamed.
                disp = self.display[t["label"]]
                if t["counted_as"] is None:
                    if t["hits"] >= COUNT_AT_HITS:
                        t["counted_as"] = disp
                        self.totals[disp] += 1
                elif t["counted_as"] != disp:
                    # The vote settled on another class: move this id's single tally.
                    # Never negative — counted_as is a class this id incremented.
                    self.totals[t["counted_as"]] -= 1
                    self.totals[disp] += 1
                    t["counted_as"] = disp
                live.append(disp)
                shown.append((disp, conf, box))
            for tid in [i for i, t in ids.items() if now - t["last_seen"] > ID_EXPIRY]:
                del ids[tid]
            self.visible[name] = Counter(live)
        return shown

    # --- dataset capture --------------------------------------------------

    def _dataset_init(self):
        if not self.dataset_dir:
            return
        try:
            for sub in ("pending/images", "pending/labels"):
                (self.dataset_dir / sub).mkdir(parents=True, exist_ok=True)
            # Append-only, ids are positional: the operator's file wins, or the classes
            # they added on the Label tab would lose their ids on every restart.
            classes = self.dataset_dir / "classes.txt"
            if not classes.exists():
                classes.write_text("\n".join(CLASS_IDS) + "\n")
                return
            # Positions 0-3 are this detector's four classes under whatever names the
            # operator gave them; anything after is theirs alone. Renames reach the
            # console on restart — nothing re-reads this file while the loop runs.
            names = [ln.strip() for ln in classes.read_text().splitlines() if ln.strip()]
            if len(names) >= len(CLASS_IDS):
                self.display = dict(zip(CLASS_IDS, names))
                self.display_ids = {d: i for i, d in enumerate(self.display.values())}
                COLORS.update({d: COLORS[n] for n, d in self.display.items()})
                self.totals = {d: 0 for d in self.display.values()}
        except OSError as e:              # a read-only disk disables capture, not detection
            self.dataset_dir = None
            self._set("running", f"dataset capture off: {e}")

    def _capture(self, name, jpeg, shown, w, h):
        """Raw (un-annotated) frame + YOLO labels for the Label tab to review.

        Pending is a rolling window: at the cap the oldest unlabeled frame is dropped so
        capture never stops (an overnight fill used to mean no morning-peak samples).
        Eviction only ever touches pending — approved samples are permanent.

        ponytail: the pending cap is counted per capture attempt (at most once per
        CAPTURE_EVERY per camera), not per pass — a glob of 1000 names every frame buys
        nothing. Upgrade path: keep a counter once something else writes the directory.
        """
        now = time.monotonic()
        if (not self.dataset_dir or not shown
                or now - self.last_capture.get(name, float("-inf")) < CAPTURE_EVERY):
            return
        self.last_capture[name] = now     # set first: a failing disk must not retry per frame
        pending = self.dataset_dir / "pending"
        try:
            imgs = list((pending / "images").glob("*.jpg"))
            if len(imgs) >= DATASET_CAP:
                # By mtime, not by name: a camera name may contain dashes, so the stem's
                # timestamp is not the lexicographic tail. Files are written once, so
                # mtime is the capture time.
                oldest = min(imgs, key=lambda p: p.stat().st_mtime)
                oldest.unlink()
                (pending / "labels" / f"{oldest.stem}.txt").unlink(missing_ok=True)
            stem = f"{name}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
            lines = [f"{self.display_ids[cls]} {(x1 + x2) / 2 / w:.6f} {(y1 + y2) / 2 / h:.6f} "
                     f"{(x2 - x1) / w:.6f} {(y2 - y1) / h:.6f}"
                     for cls, _conf, (x1, y1, x2, y2) in shown if cls in self.display_ids]
            (pending / "images" / f"{stem}.jpg").write_bytes(jpeg)
            (pending / "labels" / f"{stem}.txt").write_text("\n".join(lines) + "\n")
        except OSError as e:
            self._set("running", f"dataset capture failed: {e}")


if __name__ == "__main__":
    # Self-check: real counting/reader code, mocked model — no weights, no camera, no net.
    import tempfile
    from unittest.mock import patch

    BOX = (10.0, 10.0, 60.0, 60.0)
    FAR = (400.0, 400.0, 450.0, 450.0)
    CAM = {"name": "c", "ip": "10.0.0.1", "user": "u", "password": ""}

    def nosnap(ip, user, password):
        return None, "no camera"

    def det(cls, tid=1, box=BOX):
        return [(cls, 0.9, box, tid)]

    def fresh(**kw):
        return Detector([CAM], nosnap, {}, **kw)

    # Frame splitting: two frames in one read keep only the newest, no partial frame.
    r = object.__new__(Reader)
    r.frame, r.seq, r.lock, r.stopping = None, 0, threading.Lock(), threading.Event()
    r._pump(io.BytesIO(SOI + b"one" + EOI + SOI + b"two" + EOI + SOI + b"half"))
    assert r.latest() == (SOI + b"two" + EOI, 1), r.latest()

    # Sub-stream URL only, with creds_fn overriding the camera dict and quoting applied.
    u = Detector([], nosnap, {}, creds_fn=lambda ip: ("ad@min", "p@ss"))._url(CAM)
    assert u == "rtsp://ad%40min:p%40ss@10.0.0.1:554/Streaming/Channels/102", u
    assert fresh()._url(CAM).endswith("/102"), "never the recorder's /101"

    # (a) an id counts once, on its 2nd sighting, and never again while it lives.
    d = fresh()
    d._track("c", det("truck"))
    assert d.totals["truck"] == 0, d.totals            # one frame is not a vehicle yet
    d._track("c", det("truck"))
    assert d.totals["truck"] == 1, d.totals
    d._track("c", det("truck"))
    assert d.totals["truck"] == 1, d.totals
    assert d.counts()["visible"] == {"c": {"truck": 1}}, d.counts()

    # (b) an id seen once never counts.
    d = fresh()
    d._track("c", det("car"))
    d._track("c", [])
    assert d.totals["car"] == 0, d.totals
    assert d.counts()["visible"] == {"c": {}}, d.counts()

    # (c) occlusion: car, car, truck, car on one id is one car and no truck.
    d = fresh()
    for cls in ("car", "car", "truck", "car"):
        d._track("c", det(cls))
    assert d.totals == {"car": 1, "motorcycle": 0, "bus": 0, "truck": 0}, d.totals

    # (d) a majority flip after counting moves the tally instead of doubling it.
    d = fresh()
    d._track("c", det("truck"))
    d._track("c", det("car"))                  # hit 2: counted under the 1-1 tie label
    assert d.totals["truck"] == 1, d.totals
    for _ in range(3):
        d._track("c", det("car"))
    assert d.totals == {"car": 1, "motorcycle": 0, "bus": 0, "truck": 0}, d.totals
    assert min(d.totals.values()) >= 0, d.totals

    # Two ids are two vehicles; an untracked box is drawn but never counted.
    d = fresh()
    for _ in range(2):
        shown = d._track("c", det("car", 1) + det("truck", 2, FAR) + det("bus", None, FAR))
    assert d.totals["car"] == 1 and d.totals["truck"] == 1 and d.totals["bus"] == 0, d.totals
    assert len(shown) == 3 and shown[2][0] == "bus", shown
    assert d.counts()["visible"] == {"c": {"car": 1, "truck": 1}}, d.counts()

    # Drawing uses the voted label, not the raw per-frame class.
    d = fresh()
    d._track("c", det("car"))
    d._track("c", det("car"))
    assert d._track("c", det("truck"))[0][0] == "car", "one truck frame must not relabel"

    # Ids unseen for ID_EXPIRY are forgotten (ByteTrack recycles them; this caps memory).
    d.tracks["c"][1]["last_seen"] -= ID_EXPIRY + 1
    d._track("c", [])
    assert d.tracks["c"] == {}, d.tracks
    assert d.totals["car"] == 1, "expiry must not touch the day's totals"

    # (e) date rollover clears totals and ids.
    d.day = "2000-01-01"
    d._roll_date()
    assert d.totals == {"car": 0, "motorcycle": 0, "bus": 0, "truck": 0}, d.totals
    assert d.tracks == {} and d.day == date.today().isoformat()

    # Frames: fresh serves, stale and unknown do not.
    d.frames["c"] = (b"jpeg", time.time())
    assert d.frame("c") == b"jpeg"
    d.frames["c"] = (b"jpeg", time.time() - FRAME_STALE - 1)
    assert d.frame("c") is None
    assert d.frame("nosuchcam") is None

    # set_cameras drops per-camera state but keeps the day's totals.
    d.totals["car"] = 7
    d.frames["c"] = (b"jpeg", time.time())
    d.set_cameras([{"name": "other", "ip": "10.0.0.2", "user": "u", "password": ""}])
    assert d.frame("c") is None and "c" not in d.tracks and "c" not in d.visible
    assert d.totals["car"] == 7, d.totals
    # A pass already in flight over the removed camera must not resurrect its state.
    assert d._track("c", det("car")) == []
    assert "c" not in d.tracks and "c" not in d.visible, (d.tracks, d.visible)
    assert d.totals["car"] == 7, d.totals

    # Drawing, if Pillow is here: a box at the top edge must keep its label inside frame,
    # at both a thumbnail and a real sub-stream frame size (font/outline scale with width).
    jpeg = None
    try:
        from PIL import Image
        for w, h in ((200, 200), (1280, 720)):
            out = annotate(Image.new("RGB", (w, h)),
                           [("truck", 0.93, (5.0, 2.0, 90.0, 80.0)), ("car", 0.5, BOX)])
            assert out.startswith(SOI) and len(out) > 500, (w, len(out))
        buf = io.BytesIO()
        Image.new("RGB", (64, 64)).save(buf, "JPEG")
        jpeg = buf.getvalue()
    except ImportError:
        print("detect self-check: Pillow absent, drawing + capture checks skipped")

    # The whole loop against a fake reader: corrupt frame -> error, good frame -> annotated
    # frame + one dataset sample, one count for the id however many frames arrive.
    if jpeg:
        stream = [b"not a jpeg"]

        class FakeReader:
            def __init__(self):
                self.n = 0

            def latest(self):
                self.n += 1
                return stream[0], self.n

            def stop(self):
                pass

        tmp = Path(tempfile.mkdtemp())
        # An operator-extended classes.txt must survive a restart: ids are positional.
        keep = "car\nmotorcycle\nbus\ntruck\ntipper\n"
        (tmp / "classes.txt").write_text(keep)
        fresh(dataset_dir=tmp)._dataset_init()
        assert (tmp / "classes.txt").read_text() == keep, "must not clobber added classes"
        (tmp / "classes.txt").unlink()

        d = fresh(dataset_dir=tmp)
        d.readers["c"] = FakeReader()
        with patch.object(Detector, "_sync_readers", lambda self: None), \
             patch.object(Detector, "_load", lambda self: (lambda name, img: det("car"))):
            d.start()
            for _ in range(60):
                if d.info()["error"]:
                    break
                time.sleep(0.1)
            assert d.info()["error"] and d.info()["state"] == "running", d.info()
            assert d.frame("c") is None                # a frame that would not decode
            stream[0] = jpeg
            for _ in range(60):
                if d.frame("c"):
                    break
                time.sleep(0.1)
            assert d.info() == {"state": "running", "backend": "cpu", "error": ""}, d.info()
            assert d.counts()["totals"]["car"] == 1, d.counts()   # one id, one count

            assert (tmp / "classes.txt").read_text() == "car\nmotorcycle\nbus\ntruck\n"
            imgs = list((tmp / "pending" / "images").glob("*.jpg"))
            assert len(imgs) == 1 and imgs[0].name.startswith("c-"), imgs
            assert imgs[0].read_bytes() == jpeg, "the sample must be the RAW frame"
            label = (tmp / "pending" / "labels" / f"{imgs[0].stem}.txt").read_text()
            assert label == "0 0.546875 0.546875 0.781250 0.781250\n", label
            time.sleep(0.5)               # frames keep coming; CAPTURE_EVERY gates the rest
            assert len(list((tmp / "pending" / "images").glob("*.jpg"))) == 1

            d.set_cameras([])             # removal must stick against the in-flight pass
            time.sleep(0.5)
            assert d.frames == {} and d.counts()["visible"] == {}, d.frames
            assert d.stop() == "stopped"

    # Renamed classes: positions 0-3 of classes.txt are this detector's classes, so the
    # console speaks the operator's words while the dataset keeps the positional ids.
    tmp = Path(tempfile.mkdtemp())
    (tmp / "classes.txt").write_text("car\nmotorcycle\nbus\nheavy_goods\ntipper\n")
    d = fresh(dataset_dir=tmp)
    d._dataset_init()
    assert set(d.counts()["totals"]) == {"car", "motorcycle", "bus", "heavy_goods"}, d.counts()
    d._track("c", det("truck"))
    shown = d._track("c", det("truck"))
    assert shown[0][0] == "heavy_goods", shown          # what annotate() draws
    assert d.counts()["totals"]["heavy_goods"] == 1, d.counts()
    assert d.counts()["visible"] == {"c": {"heavy_goods": 1}}, d.counts()
    assert COLORS["heavy_goods"] == COLORS["truck"], "a rename must keep its colour"
    d._capture("c", b"raw", shown, 64, 64)
    stem = next((tmp / "pending" / "images").glob("*.jpg")).stem
    label = (tmp / "pending" / "labels" / f"{stem}.txt").read_text()
    assert label == "3 0.546875 0.546875 0.781250 0.781250\n", label   # truck's slot, renamed

    # At the cap the OLDEST pending sample is evicted, so capture never stops. Camera
    # names run reverse-alphabetically here: sorting by name would evict the newest.
    cap = Path(tempfile.mkdtemp())
    d = fresh(dataset_dir=cap)
    d._dataset_init()
    DATASET_CAP = 2
    for who, blob in (("z", b"old"), ("m", b"mid"), ("a", b"new")):
        d._capture(who, blob, [("car", 0.9, BOX)], 64, 64)
        time.sleep(0.01)                                   # distinct mtimes
    imgs = list((cap / "pending" / "images").glob("*.jpg"))
    assert len(imgs) == DATASET_CAP, imgs                  # count holds at the cap
    assert {p.read_bytes() for p in imgs} == {b"mid", b"new"}, [p.name for p in imgs]
    assert not list((cap / "pending" / "labels").glob("z-*.txt")), "label must go too"
    assert len(list((cap / "pending" / "labels").glob("*.txt"))) == DATASET_CAP

    # Missing heavy deps report absent with the pip hint; hailo reports its own error.
    a = fresh()
    with patch.object(Detector, "_load", side_effect=ImportError("no ultralytics")):
        a.start()
        a.thread.join(timeout=3)
    assert a.info() == {"state": "absent", "backend": "cpu", "error": PIP_HINT}, a.info()
    assert a.stop() == "absent", "stop must not erase the pip hint"

    h = Detector([], nosnap, {"detect_backend": "hailo", "hef_path": "/opt/yolov8n.hef"})
    h.start()
    h.thread.join(timeout=3)
    assert h.info()["state"] == "error" and "hailo" in h.info()["error"], h.info()

    print("detect self-check ok: mjpeg split keeps the newest frame, ids count once at",
          COUNT_AT_HITS, "hits, class flips outvoted, ids expire after", ID_EXPIRY,
          "s, daily reset + dataset capture ok")
