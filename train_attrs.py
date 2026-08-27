#!/usr/bin/env python3
"""Fine-tune the attribute classifier on operator-enriched box crops.

    python train_attrs.py          # build crops, train, report
    python train_attrs.py check    # build crops and count them only

One mobilenet backbone with a linear head per attribute (spec:
docs/superpowers/specs/2026-08-23-toll-taxonomy-attributes-design.md). Partial
enrichment is normal: a head with no value on a crop is ignored by its loss,
never guessed at.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml
from PIL import Image

from train import is_val, reference   # same split, same frozen benchmark

ROOT = Path(__file__).resolve().parent
DATASET = ROOT / "dataset"
APPROVED = DATASET / "approved"
RUNS = DATASET / "attr_runs"

INPUT = 224               # mobilenet's native size
PAD = 0.10                # crop context: a truck's neighbours help read its type
EPOCHS = 30
BATCH = 64
LR = 3e-4
MIN_BOXES = 200           # below this the heads memorise instead of generalising
IMAGENET = ((0.485, 0.456, 0.406), (0.229, 0.224, 0.225))


def vocab():
    f = DATASET / "attributes.yaml"
    if not f.is_file():
        sys.exit(f"no {f} — label some attributes on the Label tab first")
    v = yaml.safe_load(f.read_text()) or {}
    return {h: [str(x) for x in vals] for h, vals in v.items() if isinstance(vals, list)}


def crop_box(img, box):
    """YOLO cx cy w h (normalized) -> padded, clamped, resized RGB crop."""
    W, H = img.size
    cx, cy, w, h = (float(v) for v in box)
    w, h = w * (1 + 2 * PAD), h * (1 + 2 * PAD)
    x1, y1 = max(0, (cx - w / 2) * W), max(0, (cy - h / 2) * H)
    x2, y2 = min(W, (cx + w / 2) * W), min(H, (cy + h / 2) * H)
    if x2 - x1 < 2 or y2 - y1 < 2:
        return None
    return img.crop((x1, y1, x2, y2)).resize((INPUT, INPUT))


def build(heads):
    """-> ([crop], [targets], [stem]); targets are value ids per head, -1 = not labelled."""
    names = list(heads)
    crops, targets, stems, skipped = [], [], [], 0
    ref = reference()                 # the benchmark frames train nothing, this head included
    for js in sorted((APPROVED / "attrs").glob("*.json")):
        if js.stem in ref:
            continue
        img_p = APPROVED / "images" / f"{js.stem}.jpg"
        lbl_p = APPROVED / "labels" / f"{js.stem}.txt"
        if not (img_p.is_file() and lbl_p.is_file()):
            skipped += 1
            continue
        try:
            attrs = json.loads(js.read_text())
            boxes = [line.split() for line in lbl_p.read_text().splitlines() if line.split()]
            img = Image.open(img_p).convert("RGB")
        except (OSError, ValueError):
            skipped += 1
            continue
        for k, a in attrs.items():
            i = int(k) if str(k).lstrip("-").isdigit() else -1
            if not 0 <= i < len(boxes) or not isinstance(a, dict):
                skipped += 1          # sidecar written against a different label file
                continue
            t = [heads[n].index(a[n]) if a.get(n) in heads[n] else -1 for n in names]
            crop = crop_box(img, boxes[i][1:5])
            if crop is None or all(v < 0 for v in t):
                skipped += 1
                continue
            crops.append(crop)
            targets.append(t)
            stems.append(js.stem)
    if skipped:
        print(f"{skipped} enriched box(es) skipped — no image/label pair, or a stale sidecar")
    return crops, targets, stems


def report_counts(heads, targets, stems):
    names = list(heads)
    val = [i for i, s in enumerate(stems) if is_val(s)]
    print(f"{len(targets)} enriched crops from {len(set(stems))} samples: "
          f"{len(targets) - len(val)} train / {len(val)} val")
    for j, n in enumerate(names):
        seen = [t[j] for t in targets if t[j] >= 0]
        top = sorted({heads[n][v]: seen.count(v) for v in set(seen)}.items(),
                     key=lambda kv: -kv[1])[:4]
        print(f"  {n:<8} {len(seen):>5} labelled   " +
              ", ".join(f"{v} {c}" for v, c in top) + ("" if seen else "  (none yet)"))
    return val


def check(heads, crops, targets, stems):
    names = list(heads)
    val = report_counts(heads, targets, stems)
    assert all(c.size == (INPUT, INPUT) for c in crops), "a crop is not INPUT square"
    assert all(-1 <= v < len(heads[names[j]]) for t in targets for j, v in enumerate(t)), \
        "a target id is outside its head's vocabulary"
    assert all(any(v >= 0 for v in t) for t in targets), "a crop with no labelled head got in"
    frac = len(val) / len(targets)
    assert 0.03 <= frac <= 0.25, f"val fraction {frac:.0%} is off — check is_val()"
    print(f"ok — {len(heads)} heads, val split {frac:.0%}")


def train(heads, crops, targets, stems):
    import numpy as np
    import torch
    from torch import nn
    from torchvision.models import MobileNet_V3_Small_Weights, mobilenet_v3_small

    try:
        dev = "mps" if torch.backends.mps.is_available() else "cpu"
    except Exception:                 # torch build without the mps probe
        dev = "cpu"
    names = list(heads)
    mean, std = (torch.tensor(v).view(1, 3, 1, 1) for v in IMAGENET)
    # .contiguous(): permute leaves channels-last strides that survive the arithmetic,
    # and the backward pass then refuses to reshape the pooled features.
    X = torch.from_numpy(np.stack([np.asarray(c) for c in crops])).permute(0, 3, 1, 2).contiguous()
    X = (X.float().div(255) - mean) / std
    Y = torch.tensor(targets)
    val = torch.tensor([is_val(s) for s in stems])
    xt, yt, xv, yv = X[~val], Y[~val], X[val], Y[val]

    model = mobilenet_v3_small(weights=MobileNet_V3_Small_Weights.DEFAULT)
    backbone, pool = model.features, model.avgpool
    heads_fc = nn.ModuleList([nn.Linear(576, len(heads[n])) for n in names])
    net = nn.Sequential()      # keeps one .to(dev)/.parameters() over both parts
    net.add_module("features", backbone)
    net.add_module("fc", heads_fc)
    net.to(dev)
    opt = torch.optim.AdamW(net.parameters(), lr=LR)
    loss_fn = nn.CrossEntropyLoss(ignore_index=-1)

    def forward(xb):
        f = pool(backbone(xb)).reshape(len(xb), -1)   # flatten() is a view; MPS strides refuse it
        return [h(f) for h in heads_fc]

    print(f"training on {dev}: {len(xt)} crops, {EPOCHS} epochs", flush=True)
    for epoch in range(EPOCHS):
        net.train()
        total = 0.0
        for i in torch.randperm(len(xt)).split(BATCH):
            xb, yb = xt[i].to(dev), yt[i].to(dev)
            flip = torch.rand(len(xb), device=dev) < 0.5     # side-view symmetric; no colour jitter
            xb[flip] = torch.flip(xb[flip], dims=[3])
            out = forward(xb)
            # A head with nothing labelled in this batch would give CE a nan.
            loss = sum(loss_fn(o, yb[:, j]) for j, o in enumerate(out) if (yb[:, j] >= 0).any())
            opt.zero_grad()
            loss.backward()
            opt.step()
            total += loss.item() * len(xb)
        print(f"  epoch {epoch + 1:>2}/{EPOCHS}  loss {total / max(len(xt), 1):.3f}", flush=True)

    net.eval()
    preds = []
    with torch.no_grad():
        for i in torch.arange(len(xv)).split(BATCH):
            preds.append(torch.stack([o.argmax(1).cpu() for o in forward(xv[i].to(dev))], 1))
    P = torch.cat(preds) if preds else torch.zeros((0, len(names)), dtype=torch.long)
    print()
    for j, n in enumerate(names):
        m = yv[:, j] >= 0
        acc = float((P[m][:, j] == yv[m][:, j]).float().mean()) if int(m.sum()) else float("nan")
        print(f"  {n:<8} val acc {acc:.1%}  ({int(m.sum())} labelled)")
    if "axles" in names:      # adjacent-bin confusion is the expected failure; show it
        j = names.index("axles")
        for v, label in enumerate(heads["axles"]):
            m = yv[:, j] == v
            if int(m.sum()):
                dist = [f"{heads['axles'][p]}×{int((P[m][:, j] == p).sum())}"
                        for p in P[m][:, j].unique().tolist()]
                print(f"    true {label:<6} -> " + " ".join(dist))

    run = RUNS / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run.mkdir(parents=True, exist_ok=True)
    out = run / "attrs.pt"
    torch.save({"state_dict": {**{f"features.{k}": v for k, v in backbone.state_dict().items()},
                               **{f"fc.{k}": v for k, v in heads_fc.state_dict().items()}},
                "heads": {n: heads[n] for n in names},
                "backbone": "mobilenet_v3_small", "input": INPUT}, out)
    print(f"\nattrs: {out}")
    print(f"to deploy: set attr_weights: {out} in config.yaml and restart FieldKit")


if __name__ == "__main__":
    heads = vocab()
    crops, targets, stems = build(heads)
    if len(targets) < MIN_BOXES:
        report_counts(heads, targets, stems) if targets else None
        sys.exit(f"only {len(targets)} enriched boxes — attribute training needs at least "
                 f"{MIN_BOXES}. Tap boxes on the Label tab and set type/axles/cargo.")
    if len(sys.argv) > 1 and sys.argv[1] == "check":
        check(heads, crops, targets, stems)
    else:
        train(heads, crops, targets, stems)
