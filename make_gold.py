#!/usr/bin/env python3
"""Build gold check samples from approved work.

    python make_gold.py 20                    # 20 random approved samples
    python make_gold.py 20 --from-who jonah   # only samples this curator approved

Each gold gets dataset/gold/<id>/: the approved truth (key.txt, key-attrs.json,
image.jpg), a copy carrying 1-2 deliberate errors (perturbed.txt,
perturbed-attrs.json) and plant.json saying what was broken. The Label tab serves
the perturbed copy disguised as an ordinary sample; app.py scores the fix against
the key. Approved originals are never touched.
"""

import json
import random
import shutil
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
DATASET = ROOT / "dataset"
APPROVED = DATASET / "approved"
GOLD = DATASET / "gold"

MAX_PLANTS = 2
SHIFT = 0.15          # of the box's own width — visible, but still the same vehicle


def classes():
    return [c.strip() for c in (DATASET / "classes.txt").read_text().splitlines() if c.strip()]


def curator_of():
    """{sample id: who approved it} from the audit log — empty when nobody logged a name."""
    out = {}
    try:
        lines = (DATASET / "audit.jsonl").read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict) and r.get("action") == "approve" and r.get("id"):
            out[r["id"]] = r.get("who") or "anon"
    return out


def plant_class(boxes, attrs, names):
    """Swap one box's class for a different valid one."""
    i = random.randrange(len(boxes))
    was = boxes[i]["cls"]
    others = [c for c in range(len(names)) if c != was]
    if not others:
        return None
    boxes[i]["cls"] = random.choice(others)
    return {"box": i, "kind": "class", "was": names[was], "now": names[boxes[i]["cls"]]}


def plant_attrs(boxes, attrs, names):
    """Clear one enriched box's attributes — the labeller has to put them back."""
    enriched = [k for k, v in attrs.items() if v and int(k) < len(boxes)]
    if not enriched:
        return None
    k = random.choice(enriched)
    heads = list(attrs[k])
    attrs.pop(k)
    return {"box": int(k), "kind": "attrs", "heads": heads}


def plant_shift(boxes, attrs, names):
    """Nudge one box sideways, staying inside the frame."""
    i = random.randrange(len(boxes))
    b = boxes[i]
    step = b["w"] * SHIFT
    room_r, room_l = 1 - (b["cx"] + b["w"] / 2), b["cx"] - b["w"] / 2
    step = step if room_r >= step else (-step if room_l >= step else 0.0)
    if not step:
        return None
    b["cx"] = round(b["cx"] + step, 6)
    return {"box": i, "kind": "shift", "by": round(step, 6)}


PLANTS = (plant_class, plant_attrs, plant_shift)


def read_boxes(path):
    boxes = []
    for line in path.read_text().splitlines():
        f = line.split()
        if len(f) == 5:
            try:
                boxes.append({"cls": int(f[0]), "cx": float(f[1]), "cy": float(f[2]),
                              "w": float(f[3]), "h": float(f[4])})
            except ValueError:
                continue
    return boxes


def write_boxes(path, boxes):
    path.write_text("".join(" ".join([str(b["cls"])] + [f"{b[k]:.6f}"
                    for k in ("cx", "cy", "w", "h")]) + "\n" for b in boxes))


def build(stem, names):
    """-> plant list, or None if this sample cannot carry a fair error."""
    lbl, img = APPROVED / "labels" / f"{stem}.txt", APPROVED / "images" / f"{stem}.jpg"
    if not (lbl.is_file() and img.is_file()):
        return None
    boxes = read_boxes(lbl)
    if not boxes:
        return None            # an empty frame has nothing to get wrong
    try:
        attrs = json.loads((APPROVED / "attrs" / f"{stem}.json").read_text())
    except (OSError, ValueError):
        attrs = {}
    perturbed, pattrs = [dict(b) for b in boxes], json.loads(json.dumps(attrs))
    plants = []
    for fn in random.sample(PLANTS, k=random.randint(1, MAX_PLANTS)):
        p = fn(perturbed, pattrs, names)
        if p and not any(q["box"] == p["box"] for q in plants):   # one error per box, scorable
            plants.append(p)
    if not plants:
        return None
    d = GOLD / stem
    d.mkdir(parents=True)
    shutil.copyfile(img, d / "image.jpg")
    write_boxes(d / "key.txt", boxes)
    write_boxes(d / "perturbed.txt", perturbed)
    (d / "key-attrs.json").write_text(json.dumps(attrs))
    (d / "perturbed-attrs.json").write_text(json.dumps(pattrs))
    (d / "plant.json").write_text(json.dumps(plants))
    return plants


def main():
    args = sys.argv[1:]
    from_who = ""
    if "--from-who" in args:
        i = args.index("--from-who")
        from_who = args[i + 1] if i + 1 < len(args) else ""
        del args[i:i + 2]
    n = int(args[0]) if args and args[0].isdigit() else 10
    names = classes()
    have = {p.name for p in GOLD.iterdir()} if GOLD.is_dir() else set()
    stems = [p.stem for p in sorted((APPROVED / "labels").glob("*.txt")) if p.stem not in have]
    if from_who:
        by = curator_of()
        stems = [s for s in stems if by.get(s) == from_who]
    if not stems:
        sys.exit(f"no approved samples to draw from{' for ' + from_who if from_who else ''} "
                 f"({len(have)} gold(s) already built)")
    random.shuffle(stems)
    made = 0
    for stem in stems:
        if made >= n:
            break
        plants = build(stem, names)
        if plants:
            made += 1
            print(f"  {stem}: " + ", ".join(f"box {p['box']} {p['kind']}" for p in plants))
    print(f"{made} gold sample(s) in {GOLD}"
          + (f" (asked for {n}; the rest could not carry a fair error)" if made < n else ""))


if __name__ == "__main__":
    main()
