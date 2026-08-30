#!/usr/bin/env python3
"""Curate dataset samples from recorded segments — the live capture pipeline, offline.

  python ingest_video.py <file-or-dir> [...]   ingest local .mkv/.mp4 segments
  python ingest_video.py pull <site>/<cam>     fetch missing segments from R2, then ingest
  python ingest_video.py incoming              ingest the R2 drop-box, then archive it
  python ingest_video.py check <file>          probe one file, write nothing
  python ingest_video.py hunt <classes> [--max N] <dir> [...]
                                               only frames holding one of the classes
                                               (comma-separated), up to N per class, each
                                               directory's name prefixing its sample ids
  python ingest_video.py                       self-check

Same detector, same dedup, same dataset/pending layout as detect.py, so a sample from
footage and a sample from the wire are indistinguishable on the Label tab.
"""

import io
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import yaml

import detect

ROOT = Path(__file__).resolve().parent
DATASET = ROOT / "dataset"
STEM_TS = "%Y%m%d-%H%M%S"        # the recorder's segment names, and our sample stems
INGEST_FPS = 1.0                 # recorded curation: dedup drops unchanged scenes anyway
VIDEO = (".mkv", ".mp4")
INCOMING = "incoming/"           # where /api/dataset/upload_video parks contributed footage
DONE = "incoming-done/"          # ...and where it lands once ingested, so re-runs skip it
CAM_CHARS = re.compile(r"[^A-Za-z0-9_-]")   # sample ids become filenames: no dots, no spaces


def config():
    return yaml.safe_load((ROOT / "config.yaml").read_text()) or {}


def gate_id(cfg):
    """The RDA toll gate id this node sits at, or "" — it prefixes every sample name so a
    frame arriving in a shared queue says which of nine gates it came from."""
    return str((cfg.get("offload") or {}).get("toll_gate_id")
               or cfg.get("toll_gate_id") or "").strip()


def record_root(cfg):
    return ROOT / (cfg.get("record_dir") or "./recordings")


def cam_and_start(path):
    """(camera, start epoch) from the recorder's <site>/<cam>/YYYYMMDD-HHMMSS.mkv —
    local wallclock, per its contract. Anything else falls back to the parent directory
    name and the file's mtime, which is close enough to order samples by."""
    path = Path(path)
    try:
        return path.parent.name, datetime.strptime(path.stem, STEM_TS).timestamp()
    except ValueError:
        print(f"  ! {path.name}: not a recorder segment name — using mtime")
        return path.parent.name, path.stat().st_mtime


def split_jpegs(stream):
    """Every complete JPEG in a byte stream, in order — unlike detect.Reader, which
    keeps only the newest frame because live inference cannot catch up."""
    buf = b""
    while True:
        chunk = stream.read1(1 << 16)
        if not chunk:
            return
        buf += chunk
        while True:
            i = buf.find(detect.SOI)
            j = buf.find(detect.EOI, i + 2) if i >= 0 else -1
            if j < 0:
                break
            yield buf[i:j + 2]
            buf = buf[j + 2:]


def frames(path, fps=INGEST_FPS):
    """Decode one file to JPEGs at `fps`. Sequential, one ffmpeg, no threads."""
    p = subprocess.Popen(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(path),
         "-vf", f"fps={fps}", "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "3", "-"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        yield from split_jpegs(p.stdout)
    finally:
        p.kill()          # `check` reads one frame and walks away; don't leak the rest


def detector(cfg):
    """-> (fn(PIL image) -> [(cls, conf, box)], {class name: dataset id}).

    predict(), not track(): unrelated segments have no continuity to carry, and
    ByteTrack state across file boundaries would be noise.
    """
    from ultralytics import YOLO

    weights = str(cfg.get("detect_weights", "") or "").strip()
    if weights and not Path(weights).exists():
        sys.exit(f"detect_weights not found: {weights}")
    try:
        import torch
        dev = "mps" if torch.backends.mps.is_available() else "cpu"
    except Exception:
        dev = "cpu"
    model = YOLO(weights or detect.WEIGHTS)
    # Fine-tuned weights carry the operator's taxonomy and its positional ids; stock
    # weights are COCO, filtered to the four classes detect.py counts.
    lookup = dict(model.names) if weights else detect.CLASSES
    extra = {} if weights else {"classes": list(detect.CLASSES)}
    ids = ({n: i for i, n in lookup.items()} if weights else dict(detect.CLASS_IDS))

    def run(img):
        out = []
        for r in model.predict(img, imgsz=detect.IMGSZ, conf=detect.CONF,
                               agnostic_nms=True, device=dev, verbose=False, **extra):
            for box, cid, conf in zip(r.boxes.xyxy.tolist(), r.boxes.cls.tolist(),
                                      r.boxes.conf.tolist()):
                cls = lookup.get(int(cid))
                if cls:
                    out.append((cls, float(conf), tuple(float(v) for v in box)))
        return out

    return run, ids


class Sink:
    """dataset/pending writer with the live capture's gates, measured in footage time:
    no detections, an unchanged scene, or a too-recent sample and nothing lands."""

    def __init__(self, dataset, ids, gate="", wanted=(), only_wanted=False):
        self.images = Path(dataset) / "pending" / "images"
        self.labels = Path(dataset) / "pending" / "labels"
        for d in (self.images, self.labels):
            d.mkdir(parents=True, exist_ok=True)
        self.ids, self.gate = ids, gate
        self.wanted = set(wanted)   # classes worth a sample off-cadence (detect._capture)
        self.only_wanted = only_wanted   # a hunt: the cadence captures nothing at all
        self.at = {}          # cam -> footage timestamp of its last capture
        self.rare_at = {}     # cam -> footage timestamp of its last off-cadence capture
        self.dets = {}        # cam -> its `shown`, for the scene comparison
        self.written = Counter()
        self.hits = Counter()   # wanted class -> frames written holding it: a hunt's quota
        self.stems = []       # what this run wrote, in order: the caller pre-fills suggestions

    def offer(self, cam, ts, jpeg, shown, w, h):
        # Cadence counts from the last real capture, not the last attempt: a scene that
        # changes right after a skipped duplicate should be caught, not waited out. A
        # class the curated set is short of skips the cadence entirely — dedup still
        # stops a parked one from becoming fifty samples.
        if not shown:
            return False
        rare = any(d[0] in self.wanted for d in shown) \
            and ts - self.rare_at.get(cam, float("-inf")) >= detect.RARE_EVERY
        if not rare and (self.only_wanted
                         or ts - self.at.get(cam, float("-inf")) < detect.CAPTURE_EVERY):
            return False
        if detect.same_scene(shown, self.dets.get(cam, [])):
            return False
        self.at[cam], self.dets[cam] = ts, shown
        if rare:
            self.rare_at[cam] = ts
        self._evict()
        stem = detect.sample_stem(self.gate, cam, ts, detect.RARE_STEP if rare else None)
        lines = [f"{self.ids[cls]} {(x1 + x2) / 2 / w:.6f} {(y1 + y2) / 2 / h:.6f} "
                 f"{(x2 - x1) / w:.6f} {(y2 - y1) / h:.6f}"
                 for cls, _conf, (x1, y1, x2, y2) in shown if cls in self.ids]
        (self.images / f"{stem}.jpg").write_bytes(jpeg)      # re-ingest overwrites: idempotent
        (self.labels / f"{stem}.txt").write_text("\n".join(lines) + "\n")
        self.written[cam] += 1
        self.hits.update({cls for cls, _c, _b in shown if cls in self.wanted})
        self.stems.append(stem)
        return True

    def _evict(self):
        """Same rolling window as live capture: at the cap the oldest pending sample goes."""
        imgs = list(self.images.glob("*.jpg"))
        if len(imgs) >= detect.DATASET_CAP:
            oldest = min(imgs, key=lambda p: p.stat().st_mtime)
            oldest.unlink()
            (self.labels / f"{oldest.stem}.txt").unlink(missing_ok=True)


def segments(args):
    """Files from the paths given; a directory contributes its videos recursively."""
    out = []
    for a in args:
        p = Path(a)
        if p.is_dir():
            out += sorted(f for f in p.rglob("*") if f.suffix.lower() in VIDEO)
        elif p.suffix.lower() in VIDEO:
            out.append(p)
        else:
            print(f"  ! {p}: not a video file, skipped")
    return out


def ingest_one(f, run, sink, cam=None):
    """One file through the detector and the gates -> (frames decoded, samples written).
    `cam` names footage that never came from a recorder tree; it keys the dedup state, so
    two unrelated files must never share one."""
    from PIL import Image

    named, start = cam_and_start(f)
    cam = cam or named
    n, before = 0, sink.written[cam]
    for n, jpeg in enumerate(frames(f), 1):
        img = Image.open(io.BytesIO(jpeg)).convert("RGB")
        ts = start + (n - 1) / INGEST_FPS
        sink.offer(cam, ts, jpeg, run(img), img.width, img.height)
    got = sink.written[cam] - before
    print(f"  {cam}/{f.name}: {n} frames -> {got} samples", flush=True)
    return n, got


def ingest(files, cfg):
    run, ids = detector(cfg)
    sink = Sink(DATASET, ids, gate_id(cfg), cfg.get("capture_wanted") or (),
                bool(cfg.get("capture_only_wanted")))
    sampled = sum(ingest_one(f, run, sink)[0] for f in files)
    print(f"\n{len(files)} file(s), {sampled} frames sampled at {INGEST_FPS} fps, "
          f"{sum(sink.written.values())} samples written to {sink.images.parent}")
    for cam, n in sorted(sink.written.items()):
        print(f"  {cam}: {n}")
    return sink


def hunt(classes, dirs, cfg, cap):
    """Sweep whole recording trees for the classes the curated set is short of — the
    motorcycles and plant a season of footage holds but the cadence never kept. Each
    directory is one site, its name the sample prefix, so ids from two sites with a cam1
    each never meet. Stops early once every class has `cap` frames."""
    run, ids = detector(cfg)
    missing = [c for c in classes if c not in ids]
    if missing:
        sys.exit(f"the detector has no class {', '.join(missing)} — it knows: {', '.join(ids)}")
    grand = Counter()
    for d in dirs:
        files = segments([d])
        gate = CAM_CHARS.sub("-", Path(d).name).strip("-")
        sink = Sink(DATASET, ids, gate, classes, only_wanted=True)
        sink.hits.update(grand)                       # the quota is across sites
        print(f"\n{gate}: {len(files)} segment(s)", flush=True)
        for f in files:
            if all(sink.hits[c] >= cap for c in classes):
                break
            ingest_one(f, run, sink)
            print(f"  {f.parent.name}/{f.name}: " + ", ".join(f"{c} {sink.hits[c]}" for c in classes), flush=True)
        grand = Counter(sink.hits)
    print("\nhunt done: " + ", ".join(f"{c} {grand[c]}" for c in classes) + f" frames in {DATASET / 'pending'}")
    return grand


def pull(prefix, cfg):
    """Download the keys under <toll gate>/<cam> we do not already have locally."""
    o = cfg.get("offload") or {}
    missing = [k for k in ("account_id", "access_key_id", "secret_access_key")
               if not o.get(k)]
    if missing:
        sys.exit("config.yaml offload is missing: " + ", ".join(missing))
    import dataset_sync
    client = dataset_sync.client(o)      # one R2 client for the repo, checksum guard included
    bucket = o.get("bucket") or "fieldkit-recordings"
    root, got = record_root(cfg), []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket,
                                                                 Prefix=prefix.strip("/")):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.lower().endswith(VIDEO):
                continue
            dest = root / key
            if dest.exists():
                print(f"  = {key}")
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            print(f"  v {key} ({obj['Size'] / 1e6:.0f} MB)", flush=True)
            client.download_file(bucket, key, str(dest))
            got.append(dest)
    print(f"{len(got)} new file(s) under {root}")
    return got


def incoming(cfg):
    """Drain the R2 drop-box: whatever /api/dataset/upload_video parked under incoming/,
    oldest upload first. A key is archived only once its footage really decoded, so a
    truncated or corrupt upload stays put and retries on the next run."""
    import dataset_sync

    o = dataset_sync.creds()
    cl, bucket = dataset_sync.client(o), o["bucket"]
    objs = []
    for page in cl.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=INCOMING):
        for obj in page.get("Contents", []):
            if obj["Key"].lower().endswith(VIDEO):
                objs.append(obj)
            elif not obj["Key"].endswith("/"):
                print(f"  ! {obj['Key']}: not {' or '.join(VIDEO)}, left in place")
    if not objs:
        print(f"nothing to ingest under {INCOMING} on {bucket}")
        return
    root = record_root(cfg) / "incoming"
    root.mkdir(parents=True, exist_ok=True)
    run, ids = detector(cfg)          # one model load for the whole drop-box
    sink = Sink(DATASET, ids, gate_id(cfg))
    done = total = 0
    # Upload time orders the queue: these names came off someone's phone and mean nothing.
    for obj in sorted(objs, key=lambda x: x["LastModified"]):
        key, dest = obj["Key"], root / Path(obj["Key"]).name
        print(f"v {key} ({obj['Size'] / 1e6:.0f} MB)", flush=True)
        try:
            cl.download_file(bucket, key, str(dest))
            # The upload is the only clock this footage has — cam_and_start falls back to
            # mtime, so stamping it here is what dates the samples.
            when = obj["LastModified"].timestamp()
            os.utime(dest, (when, when))
            # <cam>-YYYYmmdd-HHMMSS must stay a legal sample id: 64 chars, no dots.
            n, got = ingest_one(dest, run, sink, cam=CAM_CHARS.sub("-", dest.stem)[:40])
            if not n:
                raise ValueError("no frames decoded — not a video, or a truncated upload")
        except Exception as e:        # one bad upload must not strand the rest of the box
            print(f"  ! {key}: {e}")
            continue
        cl.copy_object(Bucket=bucket, CopySource={"Bucket": bucket, "Key": key},
                       Key=DONE + key[len(INCOMING):])
        cl.delete_object(Bucket=bucket, Key=key)
        done, total = done + 1, total + got
    print(f"\n{done}/{len(objs)} file(s) ingested, {total} sample(s) written to "
          f"{sink.images.parent}; archived under {DONE}")
    print("run: python dataset_sync.py push   # send the new samples to the curation instance")


def check(path, cfg):
    from PIL import Image

    path = Path(path)
    cam, start = cam_and_start(path)
    first, n = None, 0
    for n, jpeg in enumerate(frames(path), 1):
        first = first or jpeg
    print(f"{path}\n  camera={cam} start={datetime.fromtimestamp(start).isoformat()}")
    print(f"  frames at {INGEST_FPS} fps: {n}")
    if first is None:
        print("  no frames decoded — not a video, or ffmpeg could not read it")
        return
    run, _ids = detector(cfg)
    img = Image.open(io.BytesIO(first)).convert("RGB")
    shown = run(img)
    print(f"  first frame {img.width}x{img.height}: {len(shown)} detection(s)")
    for cls, conf, box in shown:
        print(f"    {cls:<14} {conf:.0%}  {[round(v) for v in box]}")


def selfcheck():
    import tempfile

    tmp = Path(tempfile.mkdtemp())
    seg = tmp / "site1" / "katuba-north" / "20260819-151108.mkv"
    seg.parent.mkdir(parents=True)
    seg.write_bytes(b"")
    cam, start = cam_and_start(seg)
    assert cam == "katuba-north", cam
    assert datetime.fromtimestamp(start).strftime(STEM_TS) == "20260819-151108", start

    odd = tmp / "site1" / "cam9" / "clip.mkv"          # not a recorder name: mtime fallback
    odd.parent.mkdir(parents=True)
    odd.write_bytes(b"")
    cam2, start2 = cam_and_start(odd)
    assert cam2 == "cam9" and abs(start2 - odd.stat().st_mtime) < 1, (cam2, start2)

    # Frame splitting: two whole JPEGs out of one stream, the trailing partial ignored.
    stream = io.BytesIO(detect.SOI + b"one" + detect.EOI + detect.SOI + b"two"
                        + detect.EOI + detect.SOI + b"half")
    assert list(split_jpegs(stream)) == [detect.SOI + b"one" + detect.EOI,
                                         detect.SOI + b"two" + detect.EOI]

    # Capture gates, in footage time: cadence, then the unchanged scene, then a change.
    sink = Sink(tmp / "dataset", dict(detect.CLASS_IDS))
    car = [("car", 0.9, (10.0, 10.0, 60.0, 60.0))]
    moved = [("car", 0.9, (300.0, 10.0, 350.0, 60.0))]
    base = datetime(2026, 8, 19, 15, 11, 8, tzinfo=timezone.utc).timestamp()
    assert not sink.offer("c", base, b"jpeg", [], 64, 64), "no detections, no sample"
    assert sink.offer("c", base, b"jpeg", car, 64, 64)
    assert not sink.offer("c", base + 5, b"jpeg", moved, 64, 64), "too soon"
    assert not sink.offer("c", base + 40, b"jpeg", car, 64, 64), "unchanged scene"
    assert sink.offer("c", base + 40, b"jpeg", moved, 64, 64)
    assert sink.written["c"] == 2, sink.written

    # Stems are UTC even though segment names are local wallclock, and the seconds are
    # floored to the capture interval so a node re-ingesting footage lands on the same id
    # the node that filmed it minted live. Captures are >= CAPTURE_EVERY apart, so the
    # flooring can never fold two of one node's own samples together: :08 and :48 stay two.
    stems = sorted(p.stem for p in sink.images.glob("*.jpg"))
    assert stems == ["c-20260819-151100", "c-20260819-151140"], stems
    label = (sink.labels / "c-20260819-151100.txt").read_text()
    assert label == "0 0.546875 0.546875 0.781250 0.781250\n", label   # detect._capture's format

    # Re-ingesting the same footage overwrites its own stems rather than duplicating.
    sink2 = Sink(tmp / "dataset", dict(detect.CLASS_IDS))
    assert sink2.offer("c", base, b"again", car, 64, 64)
    assert len(list(sink.images.glob("*.jpg"))) == 2, "same stem, same file"
    assert (sink.images / "c-20260819-151100.jpg").read_bytes() == b"again"

    # A wanted class ignores the cadence, so the rare vehicles the curated set is short
    # of stop losing their frames to the common ones. Dedup still applies to them.
    rare = Sink(tmp / "rare", dict(detect.CLASS_IDS), wanted=["bus"])
    bus = [("bus", 0.9, (10.0, 10.0, 60.0, 60.0))]
    bus2 = [("bus", 0.9, (300.0, 10.0, 350.0, 60.0))]
    assert rare.offer("c", base, b"a", bus, 64, 64)
    assert not rare.offer("c", base + 2, b"b", bus2, 64, 64), "one wanted frame per RARE_EVERY"
    assert rare.offer("c", base + 5, b"b", bus2, 64, 64), "wanted: no cadence"
    assert not rare.offer("c", base + 10, b"c", bus2, 64, 64), "wanted, but the same scene"
    assert not rare.offer("c", base + 10, b"d", car, 64, 64), "an ordinary class still waits"
    assert len(set(rare.stems)) == 2, rare.stems      # finer buckets: no id collision
    only = Sink(tmp / "only", dict(detect.CLASS_IDS), wanted=["bus"], only_wanted=True)
    assert not only.offer("c", base, b"a", car, 64, 64), "a hunt keeps no ordinary frames"
    assert not only.offer("c", base + 60, b"b", car, 64, 64), "...however long it waits"
    assert only.offer("c", base + 61, b"c", bus, 64, 64), "a hunt keeps the wanted ones"
    assert only.hits == {"bus": 1}, f"a hunt counts its frames per wanted class: {only.hits}"
    assert not only.offer("c", base + 62, b"d", bus2, 64, 64), "...spaced like any rare capture"

    assert segments([str(tmp)]) == [odd, seg], segments([str(tmp)])

    # The drop-box: oldest upload first, and a key leaves incoming/ only once its footage
    # really decoded. Mocked R2, mocked decode — no bucket, no ffmpeg, no model.
    from datetime import timedelta
    from unittest.mock import patch

    import dataset_sync

    class FakeS3:
        """Enough R2 for the drop-box: list, download, copy, delete."""

        def __init__(self, keys):
            self.keys, self.copied = dict(keys), []

        def get_paginator(self, _op):
            return self

        def paginate(self, Bucket, Prefix):
            yield {"Contents": [{"Key": k, "Size": 1, "LastModified": t}
                                for k, t in self.keys.items() if k.startswith(Prefix)]}

        def download_file(self, Bucket, Key, path):
            if "gone" in Key:
                raise OSError("connection reset")      # a download that dies mid-flight
            Path(path).write_bytes(b"x")

        def copy_object(self, Bucket, CopySource, Key):
            self.copied.append((CopySource["Key"], Key))

        def delete_object(self, Bucket, Key):
            del self.keys[Key]

    t0 = datetime(2026, 8, 25, 9, 0, tzinfo=timezone.utc)
    cl = FakeS3({"incoming/jonah/first.mp4": t0,
                 "incoming/dead.mp4": t0 + timedelta(minutes=30),      # decodes to nothing
                 "incoming/jonah/gone.mp4": t0 + timedelta(hours=1),   # download fails
                 "incoming/curator02/my clip.v2.mp4": t0 + timedelta(hours=2),
                 "incoming/notes.txt": t0})
    seen = []

    def fake_one(f, run, sink, cam=None):
        seen.append((f.name, cam))
        return (0, 0) if "dead" in f.name else (10, 2)

    rec = tmp / "rec"
    me = sys.modules[__name__]     # patching by module name would import a second copy
    with patch.object(dataset_sync, "creds", lambda: {"bucket": "buck"}), \
         patch.object(dataset_sync, "client", lambda o: cl), \
         patch.object(me, "DATASET", tmp / "dataset"), \
         patch.object(me, "detector", lambda cfg: (None, dict(detect.CLASS_IDS))), \
         patch.object(me, "ingest_one", fake_one):
        incoming({"record_dir": str(rec)})

    assert seen == [("first.mp4", "first"), ("dead.mp4", "dead"),
                    ("my clip.v2.mp4", "my-clip-v2")], seen      # oldest first, name made safe
    assert cl.copied == [("incoming/jonah/first.mp4", "incoming-done/jonah/first.mp4"),
                         ("incoming/curator02/my clip.v2.mp4",
                          "incoming-done/curator02/my clip.v2.mp4")], cl.copied
    assert sorted(cl.keys) == ["incoming/dead.mp4", "incoming/jonah/gone.mp4",
                               "incoming/notes.txt"], sorted(cl.keys)
    assert (rec / "incoming" / "my clip.v2.mp4").stat().st_mtime == (
        t0 + timedelta(hours=2)).timestamp(), "samples must be dated by the upload"

    print("ingest_video self-check ok: segment names parsed, frames split, capture gated "
          "in footage time, stems UTC and idempotent, drop-box archives only what ingested")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        selfcheck()
    elif args[0] == "pull":
        if len(args) != 2:
            sys.exit("usage: ingest_video.py pull <site>/<cam>")
        cfg = config()
        got = pull(args[1], cfg)
        if got:
            ingest(got, cfg)
    elif args[0] == "incoming":
        if len(args) != 1:
            sys.exit("usage: ingest_video.py incoming")
        incoming(config())
    elif args[0] == "check":
        if len(args) != 2:
            sys.exit("usage: ingest_video.py check <file>")
        check(args[1], config())
    elif args[0] == "hunt":
        cap, rest = 1000, args[1:]
        if "--max" in rest:
            i = rest.index("--max")
            cap, rest = int(rest[i + 1]), rest[:i] + rest[i + 2:]
        if len(rest) < 2:
            sys.exit("usage: ingest_video.py hunt <classes> [--max N] <dir> [...]")
        hunt([c.strip() for c in rest[0].split(",") if c.strip()], rest[1:], config(), cap)
    else:
        files = segments(args)
        if not files:
            sys.exit("nothing to ingest")
        ingest(files, config())
