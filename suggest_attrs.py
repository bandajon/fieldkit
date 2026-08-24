#!/usr/bin/env python3
"""Pre-fill attribute suggestions for pending samples with a LOCAL vision model.

    python suggest_attrs.py          # ask gemma about every un-suggested heavy box
    python suggest_attrs.py check    # count what would be asked, no requests

Writes dataset/pending/suggest/<id>.json in the attrs-sidecar shape. Suggestions
are never a record: the Label tab shows them, the operator's tap is what gets
saved. Re-runnable — anything already suggested or already labelled is skipped.
"""

import base64
import io
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml
from PIL import Image

ROOT = Path(__file__).resolve().parent
DATASET = ROOT / "dataset"
PENDING = DATASET / "pending"

OLLAMA = "http://localhost:11434"    # local only: crops carry plates
MODEL = "gemma4:12b"
TARGET_CLASSES = ("d-medium", "e-heavy", "f-abnormal", "e-plant", "b-light")
PAD = 0.10                # same context padding the classifier trains on
CROP_MAX = 896            # axle counting needs pixels; 224 is the classifier's problem
TIMEOUT_S = 300           # one crop, cold model load included
PROGRESS_EVERY = 5    # ~40 s/crop on this Mac: silence for 25 crops reads as a hang


def vocab():
    f = DATASET / "attributes.yaml"
    if not f.is_file():
        sys.exit(f"no {f}")
    cfg = yaml.safe_load(f.read_text()) or {}
    heads = {k: [str(x) for x in v] for k, v in cfg.items() if isinstance(v, list)}
    return heads, cfg.get("constraints") or {}, cfg.get("implies") or {}


def allowed(cls, heads, constraints):
    """Head -> values askable for this class. Constraint list wins; an empty list
    means the head does not apply and is not asked at all."""
    c = constraints.get(cls) or {}
    out = {}
    for h, vals in heads.items():
        a = [str(x) for x in c[h]] if h in c else vals
        if a:
            out[h] = a
    return out


def crop_jpeg(img, box):
    """YOLO cx cy w h -> padded, clamped crop as JPEG bytes, or None if degenerate."""
    W, H = img.size
    cx, cy, w, h = (float(v) for v in box)
    w, h = w * (1 + 2 * PAD), h * (1 + 2 * PAD)
    x1, y1 = max(0, (cx - w / 2) * W), max(0, (cy - h / 2) * H)
    x2, y2 = min(W, (cx + w / 2) * W), min(H, (cy + h / 2) * H)
    if x2 - x1 < 8 or y2 - y1 < 8:
        return None
    crop = img.crop((x1, y1, x2, y2))
    crop.thumbnail((CROP_MAX, CROP_MAX))
    buf = io.BytesIO()
    crop.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def ask(cls, opts, jpeg):
    """One call, all heads. -> {head: value} straight from the model (unvalidated)."""
    lines = "\n".join(f"- {h}: " + ", ".join(v) for h, v in opts.items())
    prompt = (
        f"You are labelling ONE vehicle from a Zambian toll-gate camera. A detector has "
        f"already classified it as '{cls}'; classify the largest, most central vehicle in "
        f"the crop and ignore anything behind it.\n"
        f"Answer each attribute with exactly one of its listed values, or \"unknown\" if the "
        f"image does not show it:\n{lines}\n"
        "axles counts axles on the ground, tractor plus trailers together. axle-config groups "
        "them front to back (1+2 = single steer, tandem drive). trailers counts towed units.\n"
        "Answer with a single JSON object mapping each attribute to its value.")
    schema = {"type": "object",
              "properties": {h: {"type": "string", "enum": v + ["unknown"]}
                             for h, v in opts.items()},
              "required": list(opts)}
    body = json.dumps({
        "model": MODEL,
        # image before text is the documented order for gemma vision
        "messages": [{"role": "user", "content": prompt,
                      "images": [base64.b64encode(jpeg).decode()]}],
        "stream": False, "format": schema, "options": {"temperature": 0},
    }).encode()
    req = urllib.request.Request(OLLAMA + "/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        return json.loads(json.load(r)["message"]["content"])


def clean(answer, opts, implies):
    """Keep only in-vocabulary values. Where axle-config and axles disagree, the
    config wins: counting axles off a photo is the guess, reading the grouping is
    the observation."""
    out = {h: v for h, v in (answer or {}).items()
           if h in opts and isinstance(v, str) and v in opts[h]}
    cfg = out.get("axle-config")
    implied = ((implies.get("axle-config") or {}).get(cfg) or {}).get("axles")
    if implied and out.get("axles") and out["axles"] != implied:
        out.pop("axles")
    return out


def load_json(path):
    try:
        v = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def todo(classes):
    """-> [(stem, image path, {box index: (class name, box)})] for un-suggested boxes."""
    work = []
    for lbl in sorted((PENDING / "labels").glob("*.txt")):
        img = PENDING / "images" / f"{lbl.stem}.jpg"
        if not img.is_file():
            continue
        done = set(load_json(PENDING / "attrs" / f"{lbl.stem}.json")) | \
            set(load_json(PENDING / "suggest" / f"{lbl.stem}.json"))
        boxes = {}
        for i, line in enumerate(lbl.read_text().splitlines()):
            f = line.split()
            if len(f) != 5 or str(i) in done or not f[0].isdigit():
                continue
            cls = classes[int(f[0])] if int(f[0]) < len(classes) else ""
            if cls in TARGET_CLASSES:
                boxes[i] = (cls, f[1:])
        if boxes:
            work.append((lbl.stem, img, boxes))
    return work


def main():
    heads, constraints, implies = vocab()
    classes = [c.strip() for c in (DATASET / "classes.txt").read_text().splitlines() if c.strip()]
    work = todo(classes)
    crops = sum(len(b) for _, _, b in work)
    print(f"{len(work)} samples, {crops} boxes to suggest "
          f"({', '.join(TARGET_CLASSES)})")
    if len(sys.argv) > 1 and sys.argv[1] == "check":
        by_class = {}
        for _, _, boxes in work:
            for cls, _ in boxes.values():
                by_class[cls] = by_class.get(cls, 0) + 1
        for cls, n in sorted(by_class.items(), key=lambda kv: -kv[1]):
            print(f"  {cls:<12} {n:>6}   heads: " + ", ".join(allowed(cls, heads, constraints)))
        return
    if not crops:
        return
    try:
        urllib.request.urlopen(OLLAMA + "/api/tags", timeout=5).close()
    except (urllib.error.URLError, OSError):
        sys.exit(f"no ollama at {OLLAMA} — start it (ollama serve) and pull {MODEL}")

    began, done, skipped, stopping = time.monotonic(), 0, 0, False
    for stem, img_path, boxes in work:
        got = {}
        try:
            img = Image.open(img_path).convert("RGB")
            for i, (cls, box) in boxes.items():
                jpeg = crop_jpeg(img, box)
                if jpeg is None:
                    skipped += 1
                    continue
                opts = allowed(cls, heads, constraints)
                try:
                    v = clean(ask(cls, opts, jpeg), opts, implies)
                except Exception:      # timeout, refused, garbage JSON: one crop, not the batch
                    skipped += 1
                    continue
                done += 1
                if v:
                    got[str(i)] = v
                if (done + skipped) % PROGRESS_EVERY == 0:
                    rate = done / max(time.monotonic() - began, 1e-6)
                    print(f"  {done} done, {skipped} skipped, {rate * 60:.1f}/min", flush=True)
        except KeyboardInterrupt:
            stopping = True            # finish this sample, then stop: written files stand
        except OSError:
            skipped += len(boxes)
        if got:
            out = PENDING / "suggest" / f"{stem}.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps({**load_json(out), **got}))
        if stopping:
            break
    print(f"{done} suggested, {skipped} skipped in {(time.monotonic() - began) / 60:.1f} min")


if __name__ == "__main__":
    main()
