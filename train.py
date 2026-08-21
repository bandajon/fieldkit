#!/usr/bin/env python3
"""Fine-tune the Monitor detector on the operator-approved dataset.

    python train.py          # build the split, train, report
    python train.py check    # build the split only, then sanity-check it

Everything it writes stays under dataset/ (gitignored): dataset/run/ is the
symlinked training tree, dataset/train_runs/ holds ultralytics' output.
"""

import hashlib
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
DATASET = ROOT / "dataset"
APPROVED = DATASET / "approved"
RUN = DATASET / "run"                 # rebuilt from scratch every run
RUNS = DATASET / "train_runs"

WEIGHTS = "yolov8n.pt"                # same base the console runs; fine-tune from it
EPOCHS = 100
PATIENCE = 20
IMGSZ = 1280                          # matches detect.py: sub-streams are 1280x720
BATCH = 8                             # 18 GB unified memory at imgsz 1280
VAL_EVERY = 10                        # 1 in N frames held out, by stem hash: re-runs keep the split
MIN_FRAMES = 100                      # below this a fine-tune learns noise, not vehicles
NOISY_VAL_BOXES = 50                  # per-class mAP under this many boxes is not a real number


def classes():
    f = DATASET / "classes.txt"
    if not f.is_file():
        sys.exit(f"no {f} — nothing says what the class ids mean")
    return [line.strip() for line in f.read_text().splitlines() if line.strip()]


def is_val(stem):
    return int(hashlib.sha1(stem.encode()).hexdigest(), 16) % VAL_EVERY == 0


def build(names):
    """approved/ -> dataset/run/{images,labels}/{train,val} as symlinks. -> {split: [stems]}"""
    shutil.rmtree(RUN, ignore_errors=True)
    split = {"train": [], "val": []}
    orphans = 0
    for lbl in sorted((APPROVED / "labels").glob("*.txt")):
        img = APPROVED / "images" / f"{lbl.stem}.jpg"
        if not img.is_file():
            orphans += 1
            continue
        s = "val" if is_val(lbl.stem) else "train"
        for kind, src in (("images", img), ("labels", lbl)):
            d = RUN / kind / s
            d.mkdir(parents=True, exist_ok=True)
            (d / src.name).symlink_to(src)
        split[s].append(lbl.stem)
    images = {p.stem for p in (APPROVED / "images").glob("*.jpg")}
    orphans += len(images - set(split["train"]) - set(split["val"]))
    total = len(split["train"]) + len(split["val"])
    if total < MIN_FRAMES:
        sys.exit(f"only {total} approved frames — label at least {MIN_FRAMES} on the Label tab first")
    (RUN / "data.yaml").write_text(yaml.safe_dump(
        {"path": str(RUN), "train": "images/train", "val": "images/val", "names": names},
        sort_keys=False))
    print(f"{total} approved frames: {len(split['train'])} train / {len(split['val'])} val"
          + (f" ({orphans} unpaired file(s) skipped)" if orphans else ""))
    return split


def val_boxes(names):
    """Boxes per class in the val split — ultralytics' own per-class counts move
    between versions, and these files are right here."""
    counts = {n: 0 for n in names}
    for lbl in (RUN / "labels" / "val").glob("*.txt"):
        for line in lbl.read_text().splitlines():
            f = line.split()
            if f and f[0].isdigit() and int(f[0]) < len(names):
                counts[names[int(f[0])]] += 1
    return counts


def check(names, split):
    for s in ("train", "val"):
        for p in (RUN / "images" / s).glob("*.jpg"):
            assert p.exists(), f"dangling symlink: {p}"     # exists() follows the link
    total = len(split["train"]) + len(split["val"])
    frac = len(split["val"]) / total
    assert 0.03 <= frac <= 0.25, f"val fraction {frac:.0%} is off — check is_val()"
    cfg = yaml.safe_load((RUN / "data.yaml").read_text())
    assert cfg["names"] == names and len(cfg["names"]) == len(names)
    ids = {int(f[0]) for lbl in (RUN / "labels").rglob("*.txt")
           for line in lbl.read_text().splitlines()
           for f in [line.split()] if f and f[0].isdigit()}
    assert not ids or max(ids) < len(names), f"label id {max(ids)} past {len(names)} classes"
    boxes = val_boxes(names)
    print(f"val split {frac:.0%}, {len(names)} classes, {sum(boxes.values())} val boxes")
    for n in names:
        print(f"  {n:<32} {boxes[n]:>5} val boxes"
              + ("   (thin — its mAP will be noise)" if boxes[n] < NOISY_VAL_BOXES else ""))
    print(f"ok — {RUN / 'data.yaml'}")


def train(names):
    from ultralytics import YOLO
    try:
        import torch
        dev = "mps" if torch.backends.mps.is_available() else "cpu"
    except Exception:                 # torch rides along with ultralytics
        dev = "cpu"
    data, run = str(RUN / "data.yaml"), datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    print(f"training on {dev}: {EPOCHS} epochs, imgsz {IMGSZ}, batch {BATCH}", flush=True)
    YOLO(WEIGHTS).train(data=data, imgsz=IMGSZ, epochs=EPOCHS, patience=PATIENCE, batch=BATCH,
                        device=dev, project=str(RUNS), name=run, exist_ok=True)
    best = RUNS / run / "weights" / "best.pt"
    m = YOLO(best).val(data=data, imgsz=IMGSZ, device=dev).box
    boxes = val_boxes(names)
    print(f"\nmAP50 {m.map50:.3f}   mAP50-95 {m.map:.3f}")
    per_class = dict(zip([int(i) for i in m.ap_class_index], m.ap50))
    for i, n in enumerate(names):
        ap = f"{per_class[i]:.3f}" if i in per_class else "   —"
        print(f"  {n:<32} mAP50 {ap}  ({boxes[n]} val boxes)"
              + ("   noise — needs more labels" if boxes[n] < NOISY_VAL_BOXES else ""))
    print(f"\nweights: {best}")
    print(f"to deploy: set detect_weights: {best} in config.yaml and restart FieldKit")


if __name__ == "__main__":
    names = classes()
    split = build(names)
    if len(sys.argv) > 1 and sys.argv[1] == "check":
        check(names, split)
    else:
        train(names)
