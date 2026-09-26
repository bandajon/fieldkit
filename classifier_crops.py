#!/usr/bin/env python3
"""Durable, content-addressed archive of attribute-training crops."""
import hashlib
import json
import math
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

SID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
ROOT_NAME = "classifier-crops"


def _safe_sid(sid):
    if not isinstance(sid, str) or not sid or not SID_RE.fullmatch(sid) or sid in {".", ".."}:
        raise ValueError(f"unsafe SID: {sid!r}")


def _regular(path):
    st = path.lstat()
    if not path.is_file() or not __import__("stat").S_ISREG(st.st_mode):
        raise ValueError(f"not a regular file: {path}")


def _no_symlink(path, anchor):
    path, anchor = Path(path), Path(anchor)
    try: parts = path.relative_to(anchor).parts
    except ValueError: raise ValueError(f"path escapes dataset: {path}")
    cur = anchor
    for part in (".",) + parts:
        cur /= part
        if cur.is_symlink():
            raise ValueError(f"symlink not allowed: {cur}")


def _sha(path):
    _regular(path)
    h = hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def _json(path):
    _regular(path)
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise ValueError(f"bad attrs: {path}") from e


def _classes(dataset):
    p = dataset / "classes.txt"
    _no_symlink(p, dataset)
    return p.read_text().split() if p.is_file() else []


def _split(sid):
    from train import split_of
    return split_of(sid)


def _references(dataset):
    p = dataset / "reference.txt"
    _no_symlink(p, dataset)
    return {x.strip() for x in p.read_text().splitlines() if x.strip()} if p.is_file() else set()


def export_sample(dataset_path, sid):
    dataset = Path(dataset_path).absolute()
    _no_symlink(dataset, dataset.parent)
    _safe_sid(sid)
    out = dataset / ROOT_NAME / sid
    root = dataset / ROOT_NAME
    if root.exists() and root.is_symlink():
        raise ValueError(f"archive root is symlink: {root}")
    if out.exists() and out.is_symlink():
        raise ValueError(f"archive SID is symlink: {out}")
    if (out / "manifest.json").is_file():
        verified_archive(dataset, sid)
    image = dataset / "approved" / "images" / f"{sid}.jpg"
    labels = dataset / "approved" / "labels" / f"{sid}.txt"
    attrs_path = dataset / "approved" / "attrs" / f"{sid}.json"
    for p in (image, labels):
        _no_symlink(p, dataset); _regular(p)
    _no_symlink(attrs_path, dataset)
    classes = _classes(dataset)
    label_sha, image_sha = _sha(labels), _sha(image)
    attrs_sha = _sha(attrs_path) if attrs_path.exists() else None
    rows = [x.split() for x in labels.read_text().splitlines() if x.split()]
    boxes = []
    for k, row in enumerate(rows):
        if len(row) != 5:
            raise ValueError(f"malformed label row {sid}:{k}")
        try:
            cls = int(row[0]); vals = [float(v) for v in row[1:]]
        except ValueError as e:
            raise ValueError(f"malformed label row {sid}:{k}") from e
        if not 0 <= cls < len(classes) or not (0 <= vals[0] <= 1 and 0 <= vals[1] <= 1 and 0 < vals[2] <= 1 and 0 < vals[3] <= 1) or not all(math.isfinite(v) for v in vals):
            raise ValueError(f"invalid label row {sid}:{k}")
        boxes.append((cls, vals))
    attrs = _json(attrs_path) if attrs_path.exists() else {}
    if not isinstance(attrs, dict):
        raise ValueError(f"attrs is not an object: {sid}")
    amap = {}
    for key, value in attrs.items():
        if not str(key).lstrip("-").isdigit() or not 0 <= int(key) < len(boxes) or not isinstance(value, dict):
            raise ValueError(f"invalid attrs record {sid}:{key}")
        if any(not isinstance(v, str) for v in value.values()):
            raise ValueError(f"invalid attrs values {sid}:{key}")
        amap[int(key)] = value
    try:
        from train_attrs import crop_box, PAD, INPUT
        with Image.open(image) as opened:
            img = opened.convert("RGB")
            dimensions = [img.width, img.height]
            crops = [(i, crop_box(img, vals)) for i, (_, vals) in enumerate(boxes)]
    except Exception as e:
        raise ValueError(f"cannot read image {image}") from e
    with tempfile.TemporaryDirectory(prefix=".classifier-crops-", dir=dataset) as staging_parent:
        old_ref = False
        old_manifest = out / "manifest.json"
        if old_manifest.exists():
            _no_symlink(old_manifest, dataset); _regular(old_manifest)
            old_ref = bool(verified_archive(dataset, sid)["reference"])
        ref = old_ref or sid in _references(dataset)
        capture = None
        m = re.search(r"(\d{8}-\d{6})$", sid)
        if m:
            capture = datetime.strptime(m.group(1), "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc).isoformat()
        records = []
        stage = Path(staging_parent) / sid
        stage.mkdir()
        out_work = stage
        for i, crop in crops:
            if crop is None:
                raise ValueError(f"invalid crop {sid}:{i}")
            import io
            b = io.BytesIO(); crop.save(b, format="PNG"); data = b.getvalue()
            ch = hashlib.sha256(data).hexdigest(); name = f"{ch}.png"
            cp = out_work / name
            _no_symlink(cp, dataset)
            if cp.exists() and cp.is_symlink(): raise ValueError(f"crop is symlink: {cp}")
            if not cp.exists():
                fd, tmpname = tempfile.mkstemp(prefix=f".{name}.", suffix=".tmp", dir=out_work)
                with os.fdopen(fd, "wb") as f: f.write(data); f.flush(); os.fsync(f.fileno())
                os.replace(tmpname, cp)
            if _sha(cp) != ch:
                raise ValueError(f"crop verification failed: {cp}")
            cls, vals = boxes[i]
            records.append({"source_sid": sid, "box_index": i, "class_id": cls,
                            "class_name": classes[cls], "attrs": amap.get(i, {}),
                            "bbox": vals, "crop": name, "crop_sha256": ch})
        current_attrs_sha = _sha(attrs_path) if attrs_path.exists() else None
        if _sha(labels) != label_sha or _sha(image) != image_sha or current_attrs_sha != attrs_sha:
            raise ValueError(f"source changed during export: {sid}")
        manifest = {"version": 1, "source_sid": sid, "source_dimensions": dimensions,
                    "source_sha256": {"image": image_sha, "labels": label_sha, "attrs": attrs_sha},
                    "crop": {"input": INPUT, "pad": PAD, "format": "PNG"},
                    "reference": ref, "split": _split(sid),
                    "capture_utc": capture, "samples": records}
        encoded = json.dumps(manifest, indent=2, sort_keys=True).encode() + b"\n"
        revision = out_work / (hashlib.sha256(encoded).hexdigest() + ".json")
        if revision.exists() or revision.is_symlink():
            _no_symlink(revision, dataset); _regular(revision)
            if _sha(revision) != hashlib.sha256(encoded).hexdigest():
                raise ValueError(f"corrupt archive revision: {sid}")
        else:
            fd, revtmp = tempfile.mkstemp(prefix=".revision-", dir=out_work)
            with os.fdopen(fd, "wb") as f: f.write(encoded); f.flush(); os.fsync(f.fileno())
            os.replace(revtmp, revision)
        fd, tmpname = tempfile.mkstemp(prefix=".manifest-", dir=out_work)
        try:
            with os.fdopen(fd, "wb") as f: f.write(encoded); f.flush(); os.fsync(f.fileno())
            root.mkdir(parents=True, exist_ok=True)
            if not out.exists():
                os.replace(tmpname, stage / "manifest.json")
                os.replace(stage, out)
            else:
                for item in stage.iterdir():
                    if item.name != "manifest.json" and item.name != Path(tmpname).name:
                        target = out / item.name
                        if target.exists():
                            if _sha(target) != _sha(item): raise ValueError(f"archive revision collision: {sid}")
                        else: os.replace(item, target)
                os.replace(tmpname, out / "manifest.json")
        finally:
            if os.path.exists(tmpname): os.unlink(tmpname)
    return manifest


def export_approved(dataset_path):
    dataset = Path(dataset_path)
    ids = sorted({p.stem for kind in ("images", "labels", "attrs")
                  for p in (dataset / "approved" / kind).glob("*")})
    result = {"successes": [], "failures": []}
    for sid in ids:
        try: export_sample(dataset, sid); result["successes"].append(sid)
        except (OSError, ValueError) as e: result["failures"].append({"sid": sid, "error": str(e)})
    return result


def verified_archive(dataset_path, sid):
    dataset = Path(dataset_path).absolute(); _safe_sid(sid)
    _no_symlink(dataset, dataset.parent)
    d = dataset / ROOT_NAME / sid; mpath = d / "manifest.json"; _no_symlink(mpath, dataset); _regular(mpath)
    for rev in d.glob("[0-9a-f]*.json"):
        if not re.fullmatch(r"[0-9a-f]{64}\.json", rev.name) or rev.is_symlink():
            raise ValueError(f"invalid archive revision: {sid}")
        if _sha(rev) != rev.stem:
            raise ValueError(f"corrupt archive revision: {sid}")
    m = json.loads(mpath.read_text())
    latest_hash = _sha(mpath)
    if not (d / f"{latest_hash}.json").is_file() or _sha(d / f"{latest_hash}.json") != latest_hash:
        raise ValueError(f"latest manifest revision missing or corrupt: {sid}")
    if m.get("source_sid") != sid or m.get("version") != 1 or not isinstance(m.get("samples"), list):
        raise ValueError(f"invalid archive manifest: {sid}")
    dims = m.get("source_dimensions")
    hashes = m.get("source_sha256")
    crop = m.get("crop")
    if (not isinstance(dims, list) or len(dims) != 2 or not all(type(v) is int and v > 0 for v in dims)
            or not isinstance(hashes, dict) or not re.fullmatch(r"[0-9a-f]{64}", hashes.get("image", ""))
            or not re.fullmatch(r"[0-9a-f]{64}", hashes.get("labels", ""))
            or (hashes.get("attrs") is not None and not re.fullmatch(r"[0-9a-f]{64}", hashes.get("attrs", "")))
            or not isinstance(crop, dict) or crop.get("format") != "PNG" or crop.get("input") != 224
            or crop.get("pad") != 0.10
            or type(m.get("reference")) is not bool or m.get("split") not in {"train", "val"}
            or (m.get("capture_utc") is not None and (not isinstance(m.get("capture_utc"), str)
                or datetime.fromisoformat(m["capture_utc"]).utcoffset() is None))):
        raise ValueError(f"invalid archive metadata: {sid}")
    seen = set()
    for s in m["samples"]:
        name = s.get("crop", "")
        if (Path(name).name != name or not re.fullmatch(r"[0-9a-f]{64}\.png", name)
                or name != str(s.get("crop_sha256", "")) + ".png" or _sha(d / name) != s.get("crop_sha256")):
            raise ValueError(f"corrupt archive crop: {sid}")
        vals = s.get("bbox")
        if (s.get("source_sid") != sid or not isinstance(s.get("attrs"), dict)
                or type(s.get("box_index")) is not int or type(s.get("class_id")) is not int or s["class_id"] < 0
                or not isinstance(s.get("class_name"), str) or not isinstance(vals, list) or len(vals) != 4
                or not all(type(v) in (int, float) and math.isfinite(v) for v in vals)
                or not (0 <= vals[0] <= 1 and 0 <= vals[1] <= 1 and 0 < vals[2] <= 1 and 0 < vals[3] <= 1)
                or any(not isinstance(v, str) for v in s["attrs"].values())):
            raise ValueError(f"invalid archive sample: {sid}")
        if s["box_index"] in seen: raise ValueError(f"duplicate archive box: {sid}")
        seen.add(s["box_index"])
        with Image.open(d / name) as im:
            if im.size != (224, 224) or im.format != "PNG":
                raise ValueError(f"invalid archive crop metadata: {sid}")
    if seen != set(range(len(m["samples"]))):
        raise ValueError(f"noncontiguous archive boxes: {sid}")
    if m["split"] != _split(sid):
        raise ValueError(f"archive split drift: {sid}")
    match = re.search(r"(\d{8}-\d{6})$", sid)
    expected_capture = None
    if match:
        expected_capture = datetime.strptime(match.group(1), "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc).isoformat()
    if m.get("capture_utc") != expected_capture:
        raise ValueError(f"archive capture drift: {sid}")
    return m


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1:3] != ["export", "--dataset"]:
        raise SystemExit("usage: classifier_crops.py export --dataset PATH")
    result = export_approved(sys.argv[3])
    print(json.dumps(result, indent=2))
    raise SystemExit(1 if result["failures"] else 0)
