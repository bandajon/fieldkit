#!/usr/bin/env python3
"""Fine-tune the Monitor detector on the operator-approved dataset.

    python train.py            # build the split, train, report
    python train.py check      # build the split only, then sanity-check it
    python train.py baseline   # train on the reference set itself (v2's exact split): like-for-like runs
    python train.py eval <run> # score an existing run on that same split and the reference tree
    python train.py bench <run> [<run>...]   # ms/frame on CPU (the gate boxes) and MPS at IMGSZ

FIELDKIT_TRAIN_ARGS='{"lr0": 0.002}' passes a recipe straight through to ultralytics, on top of
RECIPE — one knob, so a run's hyperparameters are on its command line and in its args.yaml,
never hidden in an edit.

Everything it writes stays under dataset/ (gitignored): dataset/run/ is the
symlinked training tree, dataset/train_runs/ holds ultralytics' output.
"""

import hashlib
import json
import os
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
REFERENCE = DATASET / "reference.txt" # the frozen benchmark: never trained on, always scored on
REF_RUN = DATASET / "reference-run"   # its symlink tree, rebuilt alongside RUN

WEIGHTS = "yolo26n.pt"                # NMS-free, DFL-free: lighter on a CPU box, simpler to compile for Hailo
BASELINE = "baseline" in sys.argv[1:] # see build(): the reference set itself, v2's split
# What won the 2026-08-28 comparison (docs/YOLO26.md): YOLO26's defaults are tuned for
# COCO-scale data and under-fit a thousand frames — the trainer's `auto` optimiser picked a
# lower lr and full mosaic and lost to yolov8n; this beat it.
RECIPE = {"optimizer": "AdamW", "lr0": 0.001, "mosaic": 0.5}
EXTRA = {**RECIPE, **json.loads(os.environ.get("FIELDKIT_TRAIN_ARGS", "{}"))}
BENCH_FRAMES = 40
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


def split_of(stem):
    """Which split a frame belongs to. Web images (ingest_images.py) always train: they
    are signal for the rare classes the road will not supply, never a measure of the
    gate — the val split and the frozen reference set stay pure toll-gate footage."""
    return "train" if stem.startswith("external-") else ("val" if is_val(stem) else "train")


def reference():
    """Stems of the frozen reference set, or an empty set.

    Everything curated up to 2026-08-27 — 1,058 frames, what the v2 model trained on —
    was frozen as the benchmark: every later model is scored on it and none is trained
    on it, which is the only way "v3 beats v2" means anything. The list lives in the
    bucket as config, so every training machine holds the same one; a frame that is in
    it stays in approved/ (curators still see it as an example) but never enters a split."""
    if not REFERENCE.is_file():
        return set()
    return {ln.strip() for ln in REFERENCE.read_text().splitlines() if ln.strip()}


def link_tree(root, choose):
    """approved/ -> root/{images,labels}/<split> as symlinks, rebuilt from scratch.
    choose(stem) names the split, or None to leave that frame out. -> ({split: [stems]},
    unpaired files skipped)."""
    shutil.rmtree(root, ignore_errors=True)
    out, orphans = {}, 0
    for lbl in sorted((APPROVED / "labels").glob("*.txt")):
        img = APPROVED / "images" / f"{lbl.stem}.jpg"
        if not img.is_file():
            orphans += 1
            continue
        s = choose(lbl.stem)
        if s is None:
            continue
        for kind, src in (("images", img), ("labels", lbl)):
            d = root / kind / s
            d.mkdir(parents=True, exist_ok=True)
            (d / src.name).symlink_to(src)
        out.setdefault(s, []).append(lbl.stem)
    return out, orphans


def build(names):
    """The training split (reference frames left out) and, when there is a reference
    set, its own tree to score against. -> {split: [stems]}"""
    ref = reference()
    part = split_of
    if BASELINE and ref:
        # The reference set itself, and nothing else: v2 trained on exactly these frames with
        # exactly this split, so a run here compares with v2 like for like — same train frames,
        # same 116 val frames. Its reference score is inflated the same way v2's is, and
        # reference-eval.json says so ("baseline"), which keeps it out of the loop's promotion.
        choose, excluded = (lambda s: part(s) if s in ref else None), set()
    else:
        choose, excluded = (lambda s: None if s in ref else part(s)), ref
    split, orphans = link_tree(RUN, choose)
    split = {"train": split.get("train", []), "val": split.get("val", [])}
    images = {p.stem for p in (APPROVED / "images").glob("*.jpg")}
    orphans += len((images & ref if BASELINE and ref else images) - set(split["train"]) - set(split["val"]) - excluded)
    total = len(split["train"]) + len(split["val"])
    if total < MIN_FRAMES:
        sys.exit(f"only {total} approved frames outside the reference set — label at least "
                 f"{MIN_FRAMES} new ones on the Label tab first" if ref and not BASELINE else
                 f"only {total} approved frames — label at least {MIN_FRAMES} on the Label tab first")
    (RUN / "data.yaml").write_text(yaml.safe_dump(
        {"path": str(RUN), "train": "images/train", "val": "images/val", "names": names},
        sort_keys=False))
    if ref:
        held, _ = link_tree(REF_RUN, lambda s: "val" if s in ref else None)
        (REF_RUN / "data.yaml").write_text(yaml.safe_dump(   # val only; train key is required, unused
            {"path": str(REF_RUN), "train": "images/val", "val": "images/val", "names": names},
            sort_keys=False))
        print(f"{len(held.get('val', []))} reference frames" + (
            " — this BASELINE run trains on them; its reference score is not comparable to the loop's"
            if BASELINE else " held out — the benchmark, never trained on"))
    print(f"{total} approved frames: {len(split['train'])} train / {len(split['val'])} val"
          + (f" ({orphans} unpaired file(s) skipped)" if orphans else ""))
    return split


def val_boxes(names, root=RUN):
    """Boxes per class in the val split — ultralytics' own per-class counts move
    between versions, and these files are right here."""
    counts = {n: 0 for n in names}
    for lbl in (root / "labels" / "val").glob("*.txt"):
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


def report(m, names, boxes, title):
    """Print one evaluation; -> {class: mAP50} for the record."""
    print(f"\n{title}: mAP50 {m.map50:.3f}   mAP50-95 {m.map:.3f}")
    per_class = dict(zip([int(i) for i in m.ap_class_index], m.ap50))
    out = {}
    for i, n in enumerate(names):
        ap = f"{per_class[i]:.3f}" if i in per_class else "   —"
        out[n] = round(float(per_class[i]), 4) if i in per_class else None
        print(f"  {n:<32} mAP50 {ap}  ({boxes[n]} val boxes)"
              + ("   noise — needs more labels" if boxes[n] < NOISY_VAL_BOXES else ""))
    return out


def device():
    try:
        import torch
        return "mps" if torch.backends.mps.is_available() else "cpu"
    except Exception:                 # torch rides along with ultralytics
        return "cpu"


def evaluate(names, run):
    """Score a run's best.pt on the current split's val frames and on the reference tree,
    and leave both numbers beside the weights: val-eval.json and reference-eval.json. The
    same function scores a fresh run and re-scores an old one, so v2 and a challenger are
    measured by one yardstick. -> the reference eval dict, or None."""
    from ultralytics import YOLO
    dev, best = device(), RUNS / run / "weights" / "best.pt"
    if not best.is_file():
        sys.exit(f"no weights under {RUNS / run}")
    model = YOLO(best)
    m = model.val(data=str(RUN / "data.yaml"), imgsz=IMGSZ, device=dev).box
    boxes = val_boxes(names)
    per = report(m, names, boxes, f"{run}: own val split ({sum(boxes.values())} boxes)")
    (RUNS / run / "val-eval.json").write_text(json.dumps(
        {"run": run, "map50": round(float(m.map50), 4), "map50_95": round(float(m.map), 4),
         "per_class_map50": per, "frames": len(split_stems("val")), "baseline": BASELINE}, indent=1))
    if not (REF_RUN / "data.yaml").is_file():
        return None
    # The number that compares versions: same frames for every model. Not comparable when the
    # run trained on those very frames (baseline) — the loop's promotion ignores such a score.
    m = model.val(data=str(REF_RUN / "data.yaml"), imgsz=IMGSZ, device=dev).box
    boxes = val_boxes(names, REF_RUN)
    per = report(m, names, boxes, f"{run}: reference set ({sum(boxes.values())} boxes)")
    ev = {"run": run, "map50": round(float(m.map50), 4), "map50_95": round(float(m.map), 4),
          "per_class_map50": per, "frames": len(list((REF_RUN / "labels" / "val").glob("*.txt"))),
          "baseline": BASELINE, "weights": WEIGHTS}
    (RUNS / run / "reference-eval.json").write_text(json.dumps(ev, indent=1))
    return ev


def split_stems(part):
    return [p.stem for p in (RUN / "labels" / part).glob("*.txt")]


def train(names):
    from ultralytics import YOLO
    dev = device()
    data, run = str(RUN / "data.yaml"), datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    print(f"training on {dev}: {WEIGHTS}, {EPOCHS} epochs, imgsz {IMGSZ}, batch {BATCH}"
          + (f", recipe {EXTRA}" if EXTRA else "")
          + (" — BASELINE: the reference set itself" if BASELINE else ""), flush=True)
    YOLO(WEIGHTS).train(data=data, imgsz=IMGSZ, epochs=EPOCHS, patience=PATIENCE, batch=BATCH,
                        device=dev, project=str(RUNS), name=run, exist_ok=True, **EXTRA)
    evaluate(names, run)
    best = RUNS / run / "weights" / "best.pt"
    print(f"\nweights: {best}")
    print(f"to deploy: set detect_weights: {best} in config.yaml and restart FieldKit")


def bench(names, runs):
    """ms per frame at IMGSZ, on real held-out frames, per run: CPU is what the gate boxes
    have, MPS is this machine. Includes JPEG decode and letterboxing, the same for every
    model, so the numbers compare models rather than measure a kernel."""
    import time
    from ultralytics import YOLO
    frames = sorted((REF_RUN / "images" / "val").glob("*.jpg"))[:BENCH_FRAMES] or \
        sorted((RUN / "images" / "val").glob("*.jpg"))[:BENCH_FRAMES]
    devs = ["cpu"] + (["mps"] if device() == "mps" else [])
    out = {}
    for run in runs:
        model = YOLO(RUNS / run / "weights" / "best.pt")
        for dev in devs:
            for f in frames[:3]:
                model.predict(f, imgsz=IMGSZ, device=dev, verbose=False)      # warm-up
            t0 = time.perf_counter()
            for f in frames:
                model.predict(f, imgsz=IMGSZ, device=dev, verbose=False)
            out.setdefault(run, {})[dev] = round((time.perf_counter() - t0) / len(frames) * 1000, 1)
        print(f"  {run:<22} " + "   ".join(f"{d} {ms:7.1f} ms/frame" for d, ms in out[run].items()),
              flush=True)
    (RUNS / "bench.json").write_text(json.dumps(
        {"imgsz": IMGSZ, "frames": len(frames), "ms_per_frame": out}, indent=1))
    return out

if __name__ == "__main__":
    a = sys.argv[1:]
    cmd = a[0] if a else ""
    if cmd in ("eval", "bench"):
        BASELINE = True           # scoring and timing happen on the reference set's split
    names = classes()
    split = build(names)
    if cmd == "check":
        check(names, split)
    elif cmd == "eval" and len(a) == 2:
        evaluate(names, a[1])
    elif cmd == "bench" and len(a) >= 2:
        bench(names, a[1:])
    elif cmd in ("", "baseline"):
        train(names)
    else:
        sys.exit(__doc__)
