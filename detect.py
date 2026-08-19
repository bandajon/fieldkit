#!/usr/bin/env python3
"""Monitor detection: 1 fps snapshot -> YOLO -> IoU tracker -> daily vehicle totals.

ultralytics and Pillow are optional. They are imported inside the worker thread,
so a node without them reports state "absent" and the rest of FieldKit runs.
"""

import io
import threading
import time
from datetime import date

CLASSES = {2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}      # COCO ids
COLORS = {"car": (255, 208, 0), "motorcycle": (255, 0, 200),
          "bus": (0, 210, 255), "truck": (230, 40, 40)}
CONF = 0.4
IMGSZ = 1280      # ultralytics' 640 default misses distant trucks on a 1920-wide scene;
                  # 1280 found 9 vehicles vs 2 on a real site frame at ~300 ms on a Mac CPU
IOU_MATCH = 0.3
MAX_MISSES = 5           # dropped once misses exceed this
COUNT_AT_HITS = 2        # a track counts on its 2nd sighting: one-frame blips never count
FRAME_STALE = 10.0       # a frame older than this is not "live" any more
TICK = 1.0
PIP_HINT = "detection needs: pip install ultralytics pillow"
HAILO_HINT = ("hailo backend not implemented yet — runs with detect_backend: cpu; "
              "HailoRT integration lands with the site deploy")


def iou(a, b):
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    w = min(ax2, bx2) - max(ax1, bx1)
    h = min(ay2, by2) - max(ay1, by1)
    if w <= 0 or h <= 0:
        return 0.0
    inter = w * h
    union = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
    return inter / union if union > 0 else 0.0


def annotate(img, dets, quality=85):
    """Boxes + labels burnt into the frame; returns JPEG bytes."""
    from PIL import ImageDraw, ImageFont

    try:
        # Scale with the frame: the ~11 px default font is unreadable once a 1920-wide
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


class Detector:
    def __init__(self, cameras, snapshot_fn, cfg):
        self.cams = [dict(c) for c in (cameras or [])]
        self.snapshot = snapshot_fn
        self.backend = str(cfg.get("detect_backend", "cpu") or "cpu").strip().lower()
        self.hef_path = cfg.get("hef_path", "")   # hailo model; read here, used once that backend lands
        self.state = "stopped"
        self.error = ""
        self.day = date.today().isoformat()
        self.totals = {n: 0 for n in CLASSES.values()}
        self.tracks = {}      # camera name -> list of track dicts
        self.frames = {}      # camera name -> (jpeg bytes, wallclock ts)
        self.visible = {}     # camera name -> {class: count} on the latest frame
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
        with self.lock:
            if self.state in ("starting", "running"):   # absent/error keep their hint
                self.state = "stopped"
        return self.state

    def set_cameras(self, cameras):
        """Totals are per-day, not per-camera, so they survive a camera being removed."""
        cams = [dict(c) for c in (cameras or [])]
        keep = {c["name"] for c in cams}
        with self.lock:
            self.cams = cams
            for d in (self.frames, self.tracks, self.visible):
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

    def _load(self):
        """Backend + weights. Runs in the thread: the first weight download (tens of MB
        over a field link) must not block app boot."""
        if self.backend != "cpu":
            raise RuntimeError(HAILO_HINT if self.backend == "hailo"
                               else f"unknown detect_backend: {self.backend}")
        from ultralytics import YOLO

        model = YOLO("yolov8n.pt")

        def infer(img):
            """-> [(cls_name, conf, (x1, y1, x2, y2))]. A hailo backend is a drop-in
            replacement for exactly this shape — tracker and drawing see nothing else."""
            out = []
            for r in model.predict(img, imgsz=IMGSZ, conf=CONF, classes=list(CLASSES),
                                   verbose=False):
                for box, cid, conf in zip(r.boxes.xyxy.tolist(), r.boxes.cls.tolist(),
                                          r.boxes.conf.tolist()):
                    name = CLASSES.get(int(cid))
                    if name:
                        out.append((name, float(conf), tuple(float(v) for v in box)))
            return out

        return infer

    def _run(self):
        try:
            infer = self._load()
            from PIL import Image
        except ImportError:
            self._set("absent", PIP_HINT)
            return
        except Exception as e:
            self._set("error", str(e))
            return
        self._set("running", "")
        while not self.stopping.is_set():
            began = time.monotonic()
            self._roll_date()
            with self.lock:
                cams = list(self.cams)
            for cam in cams:
                if self.stopping.is_set():
                    break
                name = cam["name"]
                jpeg, err = self.snapshot(cam.get("ip", ""), cam.get("user", ""),
                                          cam.get("password", ""))
                if err or not jpeg:
                    self._track(name, [])     # a camera that is down must still expire its tracks
                    continue
                try:
                    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
                    dets = infer(img)
                    shot = annotate(img, dets)
                except Exception as e:        # a truncated frame must not kill the thread
                    self._set("running", str(e))
                    continue
                self._track(name, dets)
                with self.lock:
                    self.frames[name] = (shot, time.time())
                    self.error = ""       # a one-off bad frame must not look permanent
            # Inference slower than a tick just runs flat out — no queue, latest wins.
            self.stopping.wait(max(0.0, TICK - (time.monotonic() - began)))
        self._set("stopped", "")

    def _roll_date(self):
        today = date.today().isoformat()
        with self.lock:
            if today != self.day:
                self.day = today
                self.totals = {n: 0 for n in CLASSES.values()}
                self.tracks = {}

    def _track(self, name, dets):
        """Greedy same-class IoU matching. Called with [] when the snapshot failed.

        ponytail: at 1 fps a vehicle that parks, is occluded, or crosses the frame in
        under a second can be counted twice or missed entirely — totals are a traffic
        estimate, not a toll gate. Upgrade path: feed this tracker from the go2rtc sub
        stream at ~5 fps server-side instead of snapshot polling.
        """
        with self.lock:
            tracks = self.tracks.setdefault(name, [])
            for t in tracks:
                t["seen"] = False
            for cls, conf, box in dets:
                best, best_iou = None, IOU_MATCH
                for t in tracks:
                    if t["cls"] != cls or t["seen"]:
                        continue
                    v = iou(t["box"], box)
                    if v >= best_iou:
                        best, best_iou = t, v
                if best is None:
                    tracks.append({"cls": cls, "box": box, "hits": 1, "misses": 0,
                                   "seen": True, "counted": False})
                else:
                    best.update(box=box, hits=best["hits"] + 1, misses=0, seen=True)
            for t in tracks:
                if not t["seen"]:
                    t["misses"] += 1
                elif t["hits"] >= COUNT_AT_HITS and not t["counted"]:
                    t["counted"] = True
                    self.totals[t["cls"]] += 1
            self.tracks[name] = [t for t in tracks if t["misses"] <= MAX_MISSES]
            self.visible[name] = {c: sum(1 for x in dets if x[0] == c) for c, _, _ in dets}


if __name__ == "__main__":
    # Self-check: real tracker code path, mocked inference — no weights, no network.
    BOX = (10.0, 10.0, 60.0, 60.0)
    SHIFT = (13.0, 13.0, 63.0, 63.0)          # IoU ~0.7 with BOX: same vehicle
    FAR = (400.0, 400.0, 450.0, 450.0)

    def nosnap(ip, user, password):
        return None, "no camera"

    def det(cls, box=BOX):
        return [(cls, 0.9, box)]

    def fresh():
        return Detector([{"name": "c", "ip": "10.0.0.1", "user": "u", "password": ""}],
                        nosnap, {})

    assert iou(BOX, BOX) == 1.0 and iou(BOX, FAR) == 0.0
    assert 0.5 < iou(BOX, SHIFT) < 1.0

    # (a) two sightings count exactly once, and never again while the track lives.
    d = fresh()
    d._track("c", det("truck"))
    assert d.totals["truck"] == 0, d.totals            # one frame is not a vehicle yet
    d._track("c", det("truck", SHIFT))
    assert d.totals["truck"] == 1, d.totals
    d._track("c", det("truck", SHIFT))
    assert d.totals["truck"] == 1, d.totals
    assert d.counts()["visible"] == {"c": {"truck": 1}}, d.counts()

    # (b) a one-tick flicker never counts.
    d = fresh()
    d._track("c", det("car"))
    d._track("c", [])
    d._track("c", [])
    assert d.totals["car"] == 0, d.totals
    # ...and a class change over the same pixels is a different vehicle, not a match.
    d = fresh()
    d._track("c", det("bus"))
    d._track("c", det("truck"))
    assert d.totals == {"car": 0, "motorcycle": 0, "bus": 0, "truck": 0}, d.totals

    # (c) expire after MAX_MISSES, then the same spot is a new vehicle.
    d = fresh()
    for _ in range(2):
        d._track("c", det("bus"))
    assert d.totals["bus"] == 1, d.totals
    for _ in range(MAX_MISSES):
        d._track("c", [])
    assert d.tracks["c"], "must survive exactly MAX_MISSES"
    d._track("c", [])
    assert d.tracks["c"] == [], d.tracks
    for _ in range(2):
        d._track("c", det("bus"))
    assert d.totals["bus"] == 2, d.totals

    # (d) date rollover clears totals and tracks.
    d.day = "2000-01-01"
    d._roll_date()
    assert d.totals == {"car": 0, "motorcycle": 0, "bus": 0, "truck": 0}, d.totals
    assert d.tracks == {} and d.day == date.today().isoformat()

    # (e) frames: fresh serves, stale and unknown do not.
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

    # Drawing, if Pillow is here: a box at the top edge must keep its label inside frame,
    # at both a thumbnail and a real 1080p frame size (font/outline scale with the width).
    jpeg = None
    try:
        from PIL import Image
        for w, h in ((200, 200), (1920, 1080)):
            out = annotate(Image.new("RGB", (w, h)),
                           [("truck", 0.93, (5.0, 2.0, 90.0, 80.0)), ("car", 0.5, BOX)])
            assert out.startswith(b"\xff\xd8") and len(out) > 500, (w, len(out))
        buf = io.BytesIO()
        Image.new("RGB", (64, 64)).save(buf, "JPEG")
        jpeg = buf.getvalue()
    except ImportError:
        print("detect self-check: Pillow absent, drawing check skipped")

    # A camera that never answers must reach "running" and stay there (snapshot errors
    # are per-camera, not fatal) — with inference mocked so no weights are fetched.
    from unittest.mock import patch
    live = fresh()
    with patch.object(Detector, "_load", lambda self: (lambda img: det("car"))):
        assert live.start() == "starting"
        for _ in range(20):
            if live.info()["state"] == "running":
                break
            time.sleep(0.1)
        assert live.info() == {"state": "running", "backend": "cpu", "error": ""}, live.info()
        assert live.start() == "running", "start must be idempotent"
        assert live.frame("c") is None                  # snapshot failed: no frame stored
        assert live.stop() == "stopped"

    # A corrupt JPEG reports an error, and the next good frame clears it — a one-off bad
    # frame must not leave the console showing a fault forever.
    if jpeg:
        shot = [b"not a jpeg"]
        bad = fresh()
        bad.snapshot = lambda *a: (shot[0], None)
        with patch.object(Detector, "_load", lambda self: (lambda img: det("car"))):
            bad.start()
            for _ in range(60):
                if bad.info()["error"]:
                    break
                time.sleep(0.1)
            assert bad.info()["error"] and bad.info()["state"] == "running", bad.info()
            assert bad.frame("c") is None
            shot[0] = jpeg
            for _ in range(60):
                if bad.frame("c"):
                    break
                time.sleep(0.1)
            assert bad.info() == {"state": "running", "backend": "cpu", "error": ""}, bad.info()
            bad.stop()

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

    print("detect self-check ok: counts once at 2 hits, flickers ignored, "
          "tracks expire after", MAX_MISSES, "misses, daily reset + stale frames ok")
