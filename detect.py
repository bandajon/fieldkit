#!/usr/bin/env python3
"""Monitor detection: RTSP sub-stream -> YOLO+ByteTrack -> daily vehicle totals.

One ffmpeg reader per camera pipes ~5 fps of mjpeg; the worker tracks by ByteTrack
id and counts each id once. ultralytics and Pillow are optional and imported inside
the worker thread, so a node without them reports state "absent" and the rest of
FieldKit runs. Design: docs/superpowers/specs/2026-08-19-video-feed-and-labeling-design.md
"""

import io
import json
import subprocess
import threading
import time
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from statistics import median

from live import sub_url          # /102 by contract — the recorder owns /101

CLASSES = {2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}      # COCO ids
CLASS_IDS = {n: i for i, n in enumerate(CLASSES.values())}       # dataset ids, classes.txt order
COLORS = {}               # class -> RGB, filled by palette() when a model loads
WEIGHTS = "yolov8n.pt"
WHEEL = "wheel"          # a detector class, but never a vehicle: it feeds axle counting
CONF = 0.4
IMGSZ = 1280      # sub-streams are 1280x720, so this is ~native; 640 missed distant trucks
FPS = 5           # ffmpeg decimates to this — the loop then runs flat out
COUNT_AT_HITS = 2        # an id counts on its 2nd sighting: one-frame blips never count
ID_EXPIRY = 10.0         # forget an id unseen this long (ByteTrack recycles ids; caps memory)
RECOUNT_GUARD = 45.0     # a counted vehicle's resting place is remembered this long: a new
                         # id of the same class in the same spot is the same vehicle, not
                         # a second count. ponytail: IoU-at-rest is the assumption — one
                         # that moved far while untracked still recounts; the honest fix
                         # there is higher effective fps, not longer memory.
GUARD_IOU = 0.5
TRACKER_CFG = Path(__file__).resolve().parent / "bytetrack-fieldkit.yaml"
TRACKER_YAML = """tracker_type: bytetrack
track_high_thresh: 0.25
track_low_thresh: 0.1
new_track_thresh: 0.25
track_buffer: 90
match_thresh: 0.8
fuse_score: True
"""
FRAME_STALE = 10.0       # a frame older than this is not "live" any more
IDLE_WAIT = 0.2          # no new frames anywhere: don't spin
SETTLED = 5.0            # a reader that survives this long resets its backoff
MAX_BACKOFF = 30.0
SAVE_EVERY = 60.0        # persist the day's tallies at most this often
CAPTURE_EVERY = 10.0     # dataset samples per camera; dedup already skips unchanged scenes
DATASET_CAP = 10000      # rolling buffer of the freshest unlabeled frames; ~2-3 GB of JPEGs.
                         # Sized for the fleet goal: 9 toll gates, >=10k contributed each,
                         # 100k-image combined dataset.
SOI, EOI = b"\xff\xd8", b"\xff\xd9"
MAX_BUF = 8_000_000      # a camera emitting garbage must not grow the buffer forever
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


def same_scene(a, b, iou_min=0.85):
    """Two `shown` lists showing the same vehicles in the same places. Greedy same-class
    matching; equal lengths plus one consumed match each means equal class multisets."""
    if len(a) != len(b):
        return False
    pool = list(b)
    for cls, _conf, box in a:
        hit = next((o for o in pool if o[0] == cls and iou(box, o[2]) >= iou_min), None)
        if hit is None:
            return False
        pool.remove(hit)
    return True


def crop(img, box, pad=0.1):
    """Box crop with a little context — a tight crop cuts the wheels off a truck."""
    x1, y1, x2, y2 = box
    dx, dy = (x2 - x1) * pad, (y2 - y1) * pad
    return img.crop((max(0, x1 - dx), max(0, y1 - dy),
                     min(img.width, x2 + dx), min(img.height, y2 + dy)))


def x_clusters(centres, width, gap=1.5):
    """Axles from wheel centres: wheels sharing an x-position are one axle, and a gap
    wider than `gap` wheel-widths starts the next."""
    xs = sorted(centres)
    return 1 + sum(1 for a, b in zip(xs, xs[1:]) if b - a > gap * width)


def wheel_axles(vehicles, wheels):
    """-> {vehicle index: axle count}. A wheel belongs to the smallest box containing its
    centre; one wheel proves nothing, so only pairs and up get an estimate."""
    groups = {}
    for _c, _cf, (wx1, wy1, wx2, wy2), *_ in wheels:
        cx, cy = (wx1 + wx2) / 2, (wy1 + wy2) / 2
        best, area = None, float("inf")
        for i, (_n, _n2, (x1, y1, x2, y2), *_) in enumerate(vehicles):
            if x1 <= cx <= x2 and y1 <= cy <= y2 and (x2 - x1) * (y2 - y1) < area:
                best, area = i, (x2 - x1) * (y2 - y1)
        if best is not None:
            groups.setdefault(best, []).append((cx, wx2 - wx1))
    return {i: x_clusters([c for c, _ in ws], median([w for _, w in ws]))
            for i, ws in groups.items() if len(ws) >= 2}


def attr_text(attrs):
    """type · 6ax · cargo, for a box label."""
    return " · ".join(f"{v}ax" if h == "axles" else str(v) for h, v in attrs.items() if v)


def attr_classifier(path, dev="cpu"):
    """-> fn(PIL crop) -> {head: value}. Rebuilds train_attrs.py's model (mobilenet_v3_small
    backbone + one linear head per attribute) around the saved vocab — the ModuleDict keys
    below must stay in step with that script's module attributes."""
    import torch
    from torchvision import models, transforms

    # weights_only: the checkpoint is tensors, strings and ints — never unpickle a model
    # file as arbitrary code just because it sits in the operator's dataset directory.
    ck = torch.load(path, map_location=dev, weights_only=True)
    vocab = ck["heads"]
    net = models.mobilenet_v3_small()
    feats = net.classifier[3].in_features
    net.classifier[3] = torch.nn.Identity()
    model = torch.nn.ModuleDict({
        "backbone": net,
        "heads": torch.nn.ModuleDict({h: torch.nn.Linear(feats, len(v))
                                      for h, v in vocab.items()})})
    model.load_state_dict(ck["state_dict"])
    model.eval().to(dev)
    size = ck.get("input", 224)
    prep = transforms.Compose([transforms.Resize((size, size)), transforms.ToTensor(),
                               transforms.Normalize([0.485, 0.456, 0.406],
                                                    [0.229, 0.224, 0.225])])

    def classify(pil):
        with torch.no_grad():
            f = model["backbone"](prep(pil).unsqueeze(0).to(dev))
            return {h: vocab[h][int(model["heads"][h](f).argmax())] for h in vocab}

    return classify


def palette(names):
    """A unique, stable colour per class: golden-angle hues by position, so any number
    of classes stays visually distinct and the UI (same formula in hsl) matches."""
    import colorsys
    out = {}
    for i, n in enumerate(names):
        r, g, b = colorsys.hsv_to_rgb((i * 137.508 / 360) % 1.0, 0.85, 1.0)
        out[n] = (int(r * 255), int(g * 255), int(b * 255))
    return out


def label_of(t):
    """Majority class of a track. A tie keeps the current label: deterministic, and a
    50/50 track then stays put instead of flapping between two names."""
    top = max(t["votes"].values())
    return t["label"] if t["votes"][t["label"]] == top else t["votes"].most_common(1)[0][0]


def annotate(img, dets, quality=85):
    """Boxes + labels burnt into the frame; returns JPEG bytes. Matches the Label tab's
    look: translucent outlines and chips (alpha-composited), chip above the box flipping
    inside at the top edge, white shadowed text readable on day and night frames."""
    from PIL import Image, ImageDraw, ImageFont

    try:
        # Scale with the frame: the ~11 px default font is unreadable once a 1280-wide
        # frame is shrunk into a browser tile.
        font = ImageFont.load_default(size=max(14, img.width // 60))
    except TypeError:                     # Pillow < 10.1 has no size argument
        font = ImageFont.load_default()
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for cls, conf, (x1, y1, x2, y2), *rest in dets:
        color = COLORS.get(cls, (255, 255, 255))
        if cls == WHEEL:
            # ponytail: thin, unlabelled — a dozen wheel chips would smother the vehicles.
            d.rectangle([x1, y1, x2, y2], outline=color + (110,), width=2)
            continue
        d.rectangle([x1, y1, x2, y2], outline=color + (90,), width=max(2, img.width // 640))
        label = f"{cls} {conf:.0%}" + (f" · {rest[0]}" if rest and rest[0] else "")
        lx1, ly1, lx2, ly2 = d.textbbox((0, 0), label, font=font)
        tw, th = lx2 - lx1, ly2 - ly1
        ch = th + 5
        top = y1 - ch if y1 >= ch else y1        # above the box; inside when at the top edge
        d.rectangle([x1, top, x1 + tw + 7, top + ch], fill=color + (77,))
        d.text((x1 + 4, top + 3), label, font=font, fill=(0, 0, 0, 230))   # shadow
        d.text((x1 + 3, top + 2), label, font=font, fill=(255, 255, 255, 255))
    img = Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


_review = {}                      # lazy singleton: model + class lookup, built on first call
_review_lock = threading.Lock()


def review_frame(jpeg, cfg):
    """Annotate one still for the Review tab. -> (jpeg bytes, None) | (None, error).

    predict(), not track(): stills have no continuity, and the live models belong to the
    worker thread — sharing one would corrupt its ByteTrack state.

    ponytail: one model behind one lock, so concurrent scrubs queue instead of racing it.
    Upgrade path: a small pool if review ever needs parallelism.
    """
    weights = str(cfg.get("detect_weights", "") or "").strip()
    if weights and not Path(weights).exists():
        return None, f"detect_weights not found: {weights}"
    attrs_path = str(cfg.get("attr_weights", "") or "").strip()
    if attrs_path and not Path(attrs_path).exists():
        return None, f"attr_weights not found: {attrs_path}"
    path = weights or WEIGHTS
    with _review_lock:
        try:
            if _review.get("path") != path:
                from ultralytics import YOLO
                try:
                    import torch
                    dev = "mps" if torch.backends.mps.is_available() else "cpu"
                except Exception:
                    dev = "cpu"
                model = YOLO(path)
                # Fine-tuned weights carry the operator's taxonomy; stock weights are COCO.
                lookup = dict(model.names) if weights else CLASSES
                COLORS.update(palette(lookup.values()))
                _review.update(path=path, model=model, lookup=lookup, dev=dev,
                               extra={} if weights else {"classes": list(CLASSES)})
            if _review.get("attrs_path") != attrs_path:
                _review["attrs"] = (attr_classifier(attrs_path, _review["dev"])
                                    if attrs_path else None)
                _review["attrs_path"] = attrs_path
            from PIL import Image
            img = Image.open(io.BytesIO(jpeg)).convert("RGB")
            dets = []
            for r in _review["model"].predict(img, imgsz=IMGSZ, conf=CONF, agnostic_nms=True,
                                              device=_review["dev"], verbose=False,
                                              **_review["extra"]):
                for box, cid, conf in zip(r.boxes.xyxy.tolist(), r.boxes.cls.tolist(),
                                          r.boxes.conf.tolist()):
                    cls = _review["lookup"].get(int(cid))
                    if not cls:
                        continue
                    box = tuple(float(v) for v in box)
                    attrs = _review["attrs"](crop(img, box)) if (
                        _review["attrs"] and cls != WHEEL) else {}
                    dets.append((cls, float(conf), box, attr_text(attrs)))
            return annotate(img, dets), None
        except ImportError:
            return None, PIP_HINT
        except Exception as e:
            return None, str(e)


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
    def __init__(self, cameras, snapshot_fn, cfg, creds_fn=None, dataset_dir=None,
                 counts_dir=None):
        self.cams = [dict(c) for c in (cameras or [])]
        self.snapshot = snapshot_fn     # unused here: app.py's frame() fallback while a reader is down
        self.creds_fn = creds_fn        # ip -> (user, password); else the cam dict's own
        self.dataset_dir = Path(dataset_dir) if dataset_dir else None
        self.counts_dir = Path(counts_dir) if counts_dir else None
        self.dirty = False              # a tally moved since the last write
        self.backend = str(cfg.get("detect_backend", "cpu") or "cpu").strip().lower()
        self.hef_path = cfg.get("hef_path", "")   # hailo model; read here, used once that backend lands
        # Fine-tuned weights bring their own taxonomy: model.names replaces the COCO four.
        self.weights = str(cfg.get("detect_weights", "") or "").strip()
        # Stage 2: attributes off the counted vehicle's best crop (type/axles/cargo).
        self.attr_weights = str(cfg.get("attr_weights", "") or "").strip()
        self.attrs = None
        self.state = "stopped"
        self.error = ""
        self.day = date.today().isoformat()
        self.display = {n: n for n in CLASS_IDS}    # internal class -> operator's name
        self.display_ids = dict(CLASS_IDS)          # operator's name -> dataset id
        self.totals = {n: 0 for n in CLASS_IDS}
        self.breakdown = {}   # category -> {head: Counter of attribute values}
        self.tracks = {}      # camera name -> {track id: state}
        self.frames = {}      # camera name -> (annotated jpeg, wallclock ts)
        self.visible = {}     # camera name -> {class: count} on the latest frame
        self.readers = {}     # camera name -> Reader; owned by the worker thread
        self.models = {}      # camera name -> YOLO; ByteTrack state lives in the instance
        self.last_capture = {}
        self.last_boxes = {}  # camera name -> last captured `shown`, for scene dedup
        self.recent = {}      # camera name -> [(label, box, expires)]: the recount guard
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
            doc = self._snapshot()
        self._save(doc)
        return self.state

    def set_cameras(self, cameras):
        """Totals are per-day, not per-camera, so they survive a camera being removed.
        Readers are started/stopped by the worker on its next pass."""
        cams = [dict(c) for c in (cameras or [])]
        keep = {c["name"] for c in cams}
        with self.lock:
            self.cams = cams
            for d in (self.frames, self.tracks, self.visible, self.models,
                      self.last_capture, self.last_boxes, self.recent):
                for gone in [n for n in d if n not in keep]:
                    d.pop(gone)

    def frame(self, name):
        with self.lock:
            f = self.frames.get(name)
        if not f or time.time() - f[1] > FRAME_STALE:
            return None                 # a stale frame must not masquerade as live
        return f[0]

    def _snapshot(self):
        """The day's tallies, as counts() reports them and as they persist. Caller
        holds the lock. Categories a reassignment emptied are dropped, not sent as {}."""
        return {"date": self.day, "totals": dict(self.totals),
                "breakdown": {cat: {h: dict(c) for h, c in heads.items() if c}
                              for cat, heads in self.breakdown.items() if any(heads.values())}}

    def counts(self):
        with self.lock:
            return {**self._snapshot(), "state": self.state,
                    "visible": {n: dict(v) for n, v in self.visible.items()}}

    def info(self):
        with self.lock:
            return {"state": self.state, "backend": self.backend, "error": self.error,
                    "weights": self.weights}

    # --- worker -----------------------------------------------------------

    def _set(self, state, error):
        with self.lock:
            self.state, self.error = state, error

    def _save(self, doc):
        """One file per day, atomically: a field node loses power mid-write, and the
        authority's numbers must not come back torn."""
        if not self.counts_dir:
            return
        self.dirty = False
        try:
            self.counts_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.counts_dir / f"{doc['date']}.tmp"
            tmp.write_text(json.dumps(doc))
            tmp.replace(self.counts_dir / f"{doc['date']}.json")
        except OSError as e:              # a read-only disk loses history, not detection
            self.counts_dir = None
            with self.lock:
                self.error = f"count persistence off: {e}"

    def _restore(self):
        """A restart mid-day continues the day's tallies instead of zeroing them."""
        if not self.counts_dir:
            return
        f = self.counts_dir / f"{self.day}.json"
        try:
            doc = json.loads(f.read_text())
            with self.lock:
                self.totals.update({k: int(v) for k, v in doc["totals"].items()
                                    if k in self.totals})
                self.breakdown = {cat: {h: Counter(c) for h, c in heads.items()}
                                  for cat, heads in doc["breakdown"].items()}
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            pass          # torn or hand-edited: start the day fresh rather than crash

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
        if self.weights and not Path(self.weights).exists():
            # Never fall back to the COCO weights: the counts would silently change meaning.
            raise RuntimeError(f"detect_weights not found: {self.weights}")
        if self.attr_weights and not Path(self.attr_weights).exists():
            raise RuntimeError(f"attr_weights not found: {self.attr_weights}")
        from ultralytics import YOLO
        try:
            import torch
            dev = "mps" if torch.backends.mps.is_available() else "cpu"
        except Exception:                 # torch rides along with ultralytics; CPU regardless
            dev = "cpu"
        path = self.weights or WEIGHTS
        lookup, extra = CLASSES, {"classes": list(CLASSES)}
        if self.weights:
            # A fine-tuned model was trained from classes.txt, so its names already are
            # the operator's taxonomy: keep every class it knows, and no COCO filter.
            lookup, extra = dict(YOLO(path).names), {}
            self.display = {n: n for n in lookup.values()}
            self.display_ids = {n: i for i, n in lookup.items()}
            self.totals = {n: 0 for n in lookup.values()}
        COLORS.update(palette(lookup.values()))   # both modes: every class gets its colour
        if self.attr_weights:
            self.attrs = attr_classifier(self.attr_weights, dev)   # worker thread only

        def track(name, img):
            model = self.models.get(name)
            if model is None:
                if not TRACKER_CFG.exists():
                    # track_buffer 90 (~20 s at our fps) rides out booth-stop occlusions
                    # that the ~30 default drops, which re-ids and re-counts the vehicle.
                    TRACKER_CFG.write_text(TRACKER_YAML)
                model = self.models[name] = YOLO(path)   # one per camera: tracker state
            out = []
            # agnostic_nms: one vehicle must not survive NMS as both a car and a truck box.
            for r in model.track(img, imgsz=IMGSZ, conf=CONF, agnostic_nms=True,
                                 persist=True, tracker=str(TRACKER_CFG),
                                 device=dev, verbose=False, **extra):
                ids = r.boxes.id
                ids = ids.tolist() if ids is not None else [None] * len(r.boxes)
                for box, cid, conf, tid in zip(r.boxes.xyxy.tolist(), r.boxes.cls.tolist(),
                                               r.boxes.conf.tolist(), ids):
                    cls = lookup.get(int(cid))
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
        self._dataset_init()      # may re-key totals from classes.txt, so restore after it
        self._restore()
        seen_seq = {}
        saved = time.monotonic()
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
                    shown = self._track(name, dets, img)  # voted labels, ready to draw
                    shot = annotate(img, shown)
                except Exception as e:    # a truncated frame must not kill the thread
                    self._set("running", str(e))
                    continue
                with self.lock:
                    if self._known(name):
                        self.frames[name] = (shot, time.time())
                    self.error = ""       # a one-off bad frame must not look permanent
                self._capture(name, jpeg, shown, img.width, img.height)
            if self.dirty and time.monotonic() - saved >= SAVE_EVERY:
                saved = time.monotonic()
                with self.lock:
                    doc = self._snapshot()
                self._save(doc)
            if idle:
                self.stopping.wait(IDLE_WAIT)
        self._set("stopped", "")

    def _roll_date(self):
        today = date.today().isoformat()
        with self.lock:
            if today == self.day:
                return
            closing = self._snapshot()
            self.day = today
            self.totals = {d: 0 for d in self.display.values()}
            self.breakdown = {}
            self.tracks = {}
            self.recent = {}
        self._save(closing)      # the closing day's final word, before anyone counts again

    def _track(self, name, dets, img=None):
        """Count by ByteTrack id, one count per id, into its majority class.
        Returns [(voted label, conf, box)] for drawing — the raw per-frame class is
        exactly what flickers when a car passes a truck.

        Wheel boxes never become tracks: they are grouped into their vehicle's box and
        clustered by x-position, which counts axles far better than a crop classifier can.

        ponytail: ByteTrack at ~5 fps absorbs the old 1 fps ceiling (a parked or briefly
        occluded vehicle no longer re-counts). What remains: counts follow the effective
        fps the node sustains — under CPU load frames are dropped and a fast vehicle can
        cross unseen. Upgrade path: per-camera fps tuning, or the Hailo backend so
        inference stops competing with the recorder for CPU.
        """
        now = time.monotonic()
        shown = []
        wheels = [d for d in dets if d[0] == WHEEL]
        dets = [d for d in dets if d[0] != WHEEL]
        axles = wheel_axles(dets, wheels)         # {det index: axles seen this frame}
        with self.lock:
            if not self._known(name):
                return shown
            ids = self.tracks.setdefault(name, {})
            live = []
            shown += [(self.display.get(c, c), cf, b) for c, cf, b, _t in wheels]
            for i, (cls, conf, box, tid) in enumerate(dets):
                if tid is None:
                    # untracked: draw it, never count it
                    shown.append((self.display[cls], conf, box))
                    continue
                t = ids.get(tid)
                if t is None:
                    t = ids[tid] = {"votes": Counter(), "label": cls, "hits": 0,
                                    "counted_as": None, "last_seen": now,
                                    "axles": 0, "best": (0.0, None), "attrs": {}}
                t["votes"][cls] += 1
                t["hits"] += 1
                t["last_seen"] = now
                t["box"] = box                    # resting place, for the recount guard
                t["label"] = label_of(t)
                t["axles"] = max(t["axles"], axles.get(i, 0))
                # Best crop only until the id counts — attributes are classified once, at
                # that moment, so cropping afterwards would be work nobody reads.
                if self.attrs and img is not None and t["counted_as"] is None:
                    area = (box[2] - box[0]) * (box[3] - box[1])
                    if area > t["best"][0]:
                        buf = io.BytesIO()
                        crop(img, box).save(buf, "JPEG", quality=80)
                        t["best"] = (area, buf.getvalue())
                # Votes and labels stay internal; only what leaves here is renamed.
                disp = self.display[t["label"]]
                if t["counted_as"] is None:
                    if t["hits"] >= COUNT_AT_HITS:
                        mem = self.recent.get(name, [])
                        mem[:] = [m for m in mem if m[2] > now]
                        ghost = next((m for m in mem
                                      if m[0] == disp and iou(m[1], box) >= GUARD_IOU), None)
                        if ghost:
                            # Same class, same resting place, within the guard window:
                            # this is the vehicle we already counted, re-tracked after an
                            # occlusion — never a second tally, never a second attrs run.
                            mem.remove(ghost)
                            t["counted_as"] = disp
                            t["ghost"] = True
                        else:
                            t["attrs"] = self._classify(t)
                            t["counted_as"] = disp
                            self.totals[disp] += 1
                            self._bump(disp, t["attrs"], 1)
                elif t["counted_as"] != disp and not t.get("ghost"):
                    # The vote settled on another class: move this id's single tally.
                    # Never negative — counted_as is a class this id incremented.
                    self.totals[t["counted_as"]] -= 1
                    self.totals[disp] += 1
                    self._bump(t["counted_as"], t["attrs"], -1)
                    self._bump(disp, t["attrs"], 1)
                    t["counted_as"] = disp
                live.append(disp)
                shown.append((disp, conf, box))
            for tid in [i for i, t in ids.items() if now - t["last_seen"] > ID_EXPIRY]:
                t = ids.pop(tid)
                # Counted vehicles (ghosts included: a long stop chains through several
                # track lives) leave their resting place behind for the recount guard.
                if t["counted_as"] and t.get("box"):
                    self.recent.setdefault(name, []).append(
                        (t["counted_as"], t["box"], now + RECOUNT_GUARD))
            self.visible[name] = Counter(live)
        return shown

    def _classify(self, t):
        """Attributes for an id at the moment it counts. Caller holds the lock.
        A wheel-derived axle count beats the classifier's guess whenever there is one."""
        a = {}
        if self.attrs and t["best"][1]:
            try:
                from PIL import Image
                a = dict(self.attrs(Image.open(io.BytesIO(t["best"][1])).convert("RGB")))
            except Exception as e:      # a bad crop costs attributes, never the count
                self.error = f"attrs: {e}"
        if t["axles"] >= 2:
            a["axles"] = "7plus" if t["axles"] >= 7 else str(t["axles"])
        return a

    def _bump(self, category, attrs, n):
        """Move one vehicle's attributes into (n=1) or out of (n=-1) a category's tally.
        Caller holds the lock."""
        self.dirty = True     # every count and every reassignment lands here
        b = self.breakdown.setdefault(category, {})
        for head, value in attrs.items():
            c = b.setdefault(head, Counter())
            c[value] += n
            if c[value] <= 0:
                del c[value]            # a moved tally must not leave a zero behind

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
            if self.weights:
                return          # classes.txt is what this model was trained from: hands off
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
                base = palette(CLASS_IDS)   # positional colours even before a model loads
                COLORS.update({d: COLORS.get(n) or base[n] for n, d in self.display.items()})
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
        # Before the cap check, not after: a duplicate must not evict a real sample.
        # A vehicle parked for hours is one sample; any new vehicle changes the set and
        # capture resumes.
        if same_scene(shown, self.last_boxes.get(name, [])):
            return
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
            self.last_boxes[name] = shown
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

    # Recount guard: an expired counted vehicle leaves its resting place behind, and a
    # new same-class id there is the same vehicle — a ghost, never a second tally.
    assert d.recent["c"] and d.recent["c"][0][0] == "car", d.recent
    for tid in (9, 9):
        d._track("c", det("car", tid))
    assert d.totals["car"] == 1, "same class, same spot: no second count"
    assert d.tracks["c"][9]["ghost"] and not d.recent["c"], "memory consumed"
    # A ghost's label flip must not move totals or breakdown.
    d.tracks["c"][9]["votes"]["truck"] += 5
    d._track("c", det("truck", 9))
    assert d.totals == {"car": 1, "motorcycle": 0, "bus": 0, "truck": 0}, d.totals
    # A ghost's expiry re-arms the guard: a long stop chains through track lives.
    d.tracks["c"][9]["last_seen"] -= ID_EXPIRY + 1
    d._track("c", [])
    assert d.recent["c"], "ghost re-remembered"
    # A DIFFERENT class in the same spot is a new vehicle and counts.
    for tid in (10, 10):
        d._track("c", det("bus", tid))
    assert d.totals["bus"] == 1, d.totals
    # Same class far away counts too (the ghost's tally never existed, so truck goes 0 -> 1).
    for tid in (11, 11):
        d._track("c", det("truck", tid, FAR))
    assert d.totals["truck"] == 1, d.totals
    # An expired guard entry no longer suppresses.
    d.recent["c"] = [(m[0], m[1], time.monotonic() - 1) for m in d.recent["c"]]
    for tid in (12, 12):
        d._track("c", det("car", tid))
    assert d.totals["car"] == 2, "expired memory must not suppress"

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
                           [("truck", 0.93, (5.0, 2.0, 90.0, 80.0), "articulated · 6ax"),
                            ("wheel", 0.7, (10.0, 60.0, 26.0, 76.0)),   # thin, no chip
                            ("car", 0.5, BOX)])
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
            assert d.info() == {"state": "running", "backend": "cpu", "error": "",
                                "weights": ""}, d.info()
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

    # A scene that has not changed is not captured again — hours of a parked vehicle
    # would otherwise fill the pending pool with one duplicate per interval.
    A = [("car", 0.9, BOX)]
    assert same_scene(A, A)
    assert not same_scene(A, [("car", 0.9, (30.0, 30.0, 80.0, 80.0))])   # same car, moved
    assert not same_scene(A, A + [("truck", 0.8, FAR)])                  # one more vehicle
    assert not same_scene(A, [("truck", 0.9, BOX)])                      # relabelled

    dup = Path(tempfile.mkdtemp())
    d = fresh(dataset_dir=dup)
    d._dataset_init()
    d._capture("c", b"one", A, 64, 64)
    kept = list((dup / "pending" / "images").glob("*.jpg"))
    assert len(kept) == 1, kept
    d.last_capture.clear()                       # cadence gate open again
    d._capture("c", b"two", A, 64, 64)
    assert kept[0].read_bytes() == b"one", "an unchanged scene must not be recaptured"
    d.last_capture.clear()
    d._capture("c", b"three", A + [("truck", 0.8, FAR)], 64, 64)
    assert any(p.read_bytes() == b"three"
               for p in (dup / "pending" / "images").glob("*.jpg")), "a new vehicle captures"

    # The day's tallies survive midnight and a restart: authority reporting reads them.
    books = Path(tempfile.mkdtemp())
    d = fresh(counts_dir=books)
    d.totals["truck"] = 3
    d._bump("truck", {"axles": "6"}, 1)
    d.day = "2000-01-01"                          # pretend the clock just passed midnight
    d._roll_date()
    closing = json.loads((books / "2000-01-01.json").read_text())
    assert closing["totals"]["truck"] == 3, closing
    assert closing["breakdown"] == {"truck": {"axles": {"6": 1}}}, closing
    assert d.totals["truck"] == 0 and d.counts()["breakdown"] == {}, d.counts()

    d._bump("bus", {"type": "coaster"}, 1)        # today's tallies, then a "crash"
    d.totals["bus"] = 2
    d.stop()
    back = fresh(counts_dir=books)
    back._restore()
    assert back.totals["bus"] == 2, back.totals    # the day continues, it does not restart
    assert back.counts()["breakdown"] == {"bus": {"type": {"coaster": 1}}}, back.counts()
    back._track("c", det("bus"))
    back._track("c", det("bus"))
    assert back.totals["bus"] == 3, back.totals

    (books / f"{date.today().isoformat()}.json").write_text("{ this is not json")
    fresh_start = fresh(counts_dir=books)
    fresh_start._restore()
    assert fresh_start.totals["bus"] == 0, "a torn file starts the day fresh, never crashes"
    assert not (books / f"{date.today().isoformat()}.tmp").exists(), "no tmp left behind"

    # Axles from wheels. Gaps of 2 and 1 px are the same axle seen doubled; 38-40 px
    # gaps are separate axles, so these six wheels are 4 axles.
    assert x_clusters([10, 12, 50, 90, 130, 131], 8) == 4
    assert x_clusters([10], 8) == 1 and x_clusters([10, 200], 8) == 2

    VEH = (0.0, 0.0, 200.0, 100.0)
    def wheel(cx):
        return (WHEEL, 0.8, (cx - 4.0, 80.0, cx + 4.0, 96.0), None)
    six = [wheel(20), wheel(24), wheel(90), wheel(140), wheel(180), wheel(184)]
    assert wheel_axles([("truck", 0.9, VEH, 1)], six) == {0: 4}
    assert wheel_axles([("truck", 0.9, VEH, 1)], [wheel(20)]) == {}, "one wheel proves nothing"
    # The smallest containing box wins, so a wheel is never claimed by the scene-wide box.
    assert wheel_axles([("bus", 0.9, (0.0, 0.0, 600.0, 400.0), 1),
                        ("truck", 0.9, VEH, 2)], six) == {1: 4}

    # Wheels never become tracks, but are still drawn (and captured) as boxes.
    d = fresh()
    shown = d._track("c", det("truck") + [wheel(20), wheel(120)])
    assert len(d.tracks["c"]) == 1, d.tracks
    assert sorted(s[0] for s in shown) == ["truck", "wheel", "wheel"], shown

    # Attributes: classified once at count time, wheel axles overriding the classifier.
    d = fresh()
    d.attrs = lambda pil: {"type": "articulated", "axles": "2", "cargo": "mineral"}
    if jpeg:
        from PIL import Image
        pic = Image.new("RGB", (300, 200))
        d._track("c", [("truck", 0.9, (0.0, 0.0, 40.0, 40.0), 1)], pic)
        small = d.tracks["c"][1]["best"][0]
        assert small == 1600.0, small
        d._track("c", [("truck", 0.9, VEH, 1)] + six, pic)   # bigger box, and 4 axles
        t = d.tracks["c"][1]
        assert t["best"][0] == 20000.0, t["best"][0]         # best crop is the largest box
        assert t["attrs"] == {"type": "articulated", "axles": "4", "cargo": "mineral"}, t
        assert d.counts()["breakdown"] == {
            "truck": {"type": {"articulated": 1}, "axles": {"4": 1}, "cargo": {"mineral": 1}}}
        d._track("c", [("truck", 0.9, (0.0, 0.0, 300.0, 200.0), 1)], pic)
        assert d.tracks["c"][1]["best"][0] == 20000.0, "counted ids stop cropping"

        # A category flip moves the whole breakdown with the tally — no zeros left behind.
        for _ in range(4):              # outvote the three truck frames above
            d._track("c", [("bus", 0.9, VEH, 1)], pic)
        assert d.totals["bus"] == 1 and d.totals["truck"] == 0, d.totals
        assert d.counts()["breakdown"] == {
            "bus": {"type": {"articulated": 1}, "axles": {"4": 1}, "cargo": {"mineral": 1}}}
        assert min(d.totals.values()) >= 0, d.totals

    # Attributes off: everything else still works, and no crops are taken.
    d = fresh()
    d._track("c", det("truck"))
    d._track("c", det("truck"))
    assert d.totals["truck"] == 1 and d.counts()["breakdown"] == {}, d.counts()

    # A missing attr_weights file is loud, like a missing detector model.
    n = Detector([], nosnap, {"attr_weights": "/nope/attrs.pt"})
    n.start()
    n.thread.join(timeout=3)
    assert n.info()["state"] == "error" and "/nope/attrs.pt" in n.info()["error"], n.info()

    # Fine-tuned weights: the model's own names are the taxonomy, end to end.
    import sys
    import types

    class T(list):
        def tolist(self):
            return list(self)

    class FakeBoxes:
        xyxy, cls, conf, id = T([[10.0, 10.0, 60.0, 60.0]]), T([4.0]), T([0.9]), T([7.0])

        def __len__(self):
            return 1

    class FakeModel:
        names = {0: "bike", 1: "minibus", 2: "car", 3: "tipper", 4: "haulage", 5: "tractor"}
        seen = {}

        def track(self, img, **kw):
            FakeModel.seen = kw
            return [types.SimpleNamespace(boxes=FakeBoxes())]

        predict = track

    fine = Path(tempfile.mkdtemp())
    weights = fine / "custom.pt"
    weights.write_bytes(b"")                     # _load only checks that it exists
    (fine / "classes.txt").write_text("bike\nminibus\ncar\ntipper\nharvester\n")
    built = []
    sys.modules["ultralytics"] = types.SimpleNamespace(
        YOLO=lambda p: (built.append(p), FakeModel())[1])
    try:
        d = Detector([CAM], nosnap, {"detect_weights": str(weights)}, dataset_dir=fine)
        run = d._load()
        assert d.info()["weights"] == str(weights), d.info()
        assert set(d.display) == set(FakeModel.names.values()), d.display
        assert d.display_ids["tipper"] == 3 and d.display_ids["tractor"] == 5, d.display_ids
        assert set(d.counts()["totals"]) == set(FakeModel.names.values()), d.counts()
        assert d.counts()["totals"]["haulage"] == 0
        assert run("c", None) == [("haulage", 0.9, (10.0, 10.0, 60.0, 60.0), 7)], run("c", None)
        assert "classes" not in FakeModel.seen, FakeModel.seen   # no COCO filter
        # Golden-angle palette: every class present, all colours pairwise distinct.
        fake_colors = [COLORS[n] for n in FakeModel.names.values()]
        assert len(set(fake_colors)) == len(fake_colors), fake_colors
        d._dataset_init()
        assert (fine / "classes.txt").read_text().endswith("harvester\n"), "hands off"
        d._capture("c", b"raw", [("haulage", 0.9, BOX)], 64, 64)
        stem = next((fine / "pending" / "images").glob("*.jpg")).stem
        assert (fine / "pending" / "labels" / f"{stem}.txt").read_text().startswith("4 ")

        # review_frame: one still, one shared model, never the worker's tracking models.
        assert review_frame(b"x", {"detect_weights": "/nope.pt"}) == (
            None, "detect_weights not found: /nope.pt")
        if jpeg:                       # needs Pillow, same as annotate
            built.clear()
            worker_models = dict(d.models)
            out, err = review_frame(jpeg, {"detect_weights": str(weights)})
            assert err is None and out.startswith(SOI), (err, out and out[:4])
            assert review_frame(jpeg, {"detect_weights": str(weights)})[0], "second call"
            assert len(built) == 1, built            # built once, reused
            assert d.models == worker_models, "review must not touch the worker's models"
            assert review_frame(b"not a jpeg", {})[0] is None
            assert review_frame(b"not a jpeg", {})[1], "a bad still must report why"
    finally:
        del sys.modules["ultralytics"]

    # Missing weights must fail loudly — falling back to COCO would silently change counts.
    m = Detector([], nosnap, {"detect_weights": "/nope/custom.pt"})
    m.start()
    m.thread.join(timeout=3)
    assert m.info()["state"] == "error" and "/nope/custom.pt" in m.info()["error"], m.info()

    # Missing heavy deps report absent with the pip hint; hailo reports its own error.
    a = fresh()
    with patch.object(Detector, "_load", side_effect=ImportError("no ultralytics")):
        a.start()
        a.thread.join(timeout=3)
    assert a.info() == {"state": "absent", "backend": "cpu", "error": PIP_HINT,
                        "weights": ""}, a.info()
    assert a.stop() == "absent", "stop must not erase the pip hint"

    h = Detector([], nosnap, {"detect_backend": "hailo", "hef_path": "/opt/yolov8n.hef"})
    h.start()
    h.thread.join(timeout=3)
    assert h.info()["state"] == "error" and "hailo" in h.info()["error"], h.info()

    print("detect self-check ok: mjpeg split keeps the newest frame, ids count once at",
          COUNT_AT_HITS, "hits, class flips outvoted, ids expire after", ID_EXPIRY,
          "s, daily reset + dataset capture ok")
