#!/usr/bin/env python3
"""Turn a folder of external images into pending samples for the curation tool.

  python ingest_images.py dataset/external/f-abnormal
  python ingest_images.py dataset/external/a-motorcycle --as a-motorcycle --push
  python ingest_images.py                      self-check

Same dataset/pending layout as ingest_video.py, so a web image and a toll-gate frame
look identical on the Label tab — except for the stem: external-<hash>, which train.py
reads to keep these frames out of the validation split. A pre-label is a suggestion; the
curator still confirms or draws every box, and an image the detector finds nothing in
still becomes a sample — for abnormal loads that is the whole point.
"""

import hashlib
import json
import io
import sys
from pathlib import Path

import ingest_video

ROOT = Path(__file__).resolve().parent
DATASET = ROOT / "dataset"
EXTERNAL = DATASET / "external"
CHAMPION = DATASET / "champion.pt"
ATTRS_CHAMPION = DATASET / "attrs-champion.pt"
IMG = (".jpg", ".jpeg", ".png")
PENDING = ("pending/images", "pending/labels", "pending/attrs", "pending/suggest")
# Classes the COCO base model already knows better than any fine-tune of ours will: it
# has seen thousands of motorcycles, our curated set has seen four.
BASE_OF = {"a-motorcycle": "motorcycle"}


def class_ids():
    import train
    return {n: i for i, n in enumerate(train.classes())}


def images(args):
    out = []
    for a in args:
        p = Path(a)
        if p.is_dir():
            out += sorted(f for f in p.rglob("*") if f.suffix.lower() in IMG)
        elif p.suffix.lower() in IMG:
            out.append(p)
        else:
            print(f"  ! {p}: not an image, skipped")
    return out


MANIFEST = DATASET / "external.json"      # {stem: class it was fetched for}; synced as config


def load_manifest():
    try:
        return json.loads(MANIFEST.read_text())
    except (OSError, ValueError):
        return {}


def save_manifest(m):
    """Which class each web image was fetched for. The champion boxes few of them — an
    abnormal load or a grader in a random photo is not what it trained on — and the
    queue's focus filters on pre-labelled classes, so without this record the images
    fetched FOR a class are invisible under that class's focus. Synced as config, the
    curation service reads it the way it reads reference.txt."""
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(m, indent=0, sort_keys=True))


def ingest(files, as_class=None):
    """-> the stems written. Each image becomes one pending sample, boxes or not."""
    from PIL import Image

    base = BASE_OF.get(as_class)
    if base:
        run, _ids = ingest_video.detector({})       # stock COCO weights
        keep = {base: class_ids()[as_class]}        # its box, our class id
    else:
        run, keep = ingest_video.detector(
            {"detect_weights": str(CHAMPION) if CHAMPION.exists() else ""})
    imgs, labels = DATASET / "pending" / "images", DATASET / "pending" / "labels"
    for d in (imgs, labels):
        d.mkdir(parents=True, exist_ok=True)
    stems = []
    manifest = load_manifest()
    for f in files:
        blob = f.read_bytes()
        stem = "external-" + hashlib.sha1(blob).hexdigest()[:12]
        fetched_for = as_class or (f.parent.name if f.parent.parent == EXTERNAL else None)
        if fetched_for:
            manifest[stem] = fetched_for
        try:
            img = Image.open(io.BytesIO(blob)).convert("RGB")
        except Exception as e:          # a truncated download costs one sample, not the run
            print(f"  ! {f}: {e}")
            continue
        w, h = img.width, img.height
        lines = [f"{keep[cls]} {(x1 + x2) / 2 / w:.6f} {(y1 + y2) / 2 / h:.6f} "
                 f"{(x2 - x1) / w:.6f} {(y2 - y1) / h:.6f}"
                 for cls, _conf, (x1, y1, x2, y2) in run(img) if cls in keep]
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=90)     # PNG or CMYK in, one jpeg convention out
        (imgs / f"{stem}.jpg").write_bytes(buf.getvalue())
        (labels / f"{stem}.txt").write_text("".join(ln + "\n" for ln in lines))
        stems.append(stem)
        print(f"  {f.name} -> {stem}  {len(lines)} box(es)", flush=True)
    save_manifest(manifest)
    return stems


def suggest(stems):
    if not ATTRS_CHAMPION.exists():
        print(f"no {ATTRS_CHAMPION.name} here — no attribute suggestions")
        return
    import selfloop
    selfloop.suggest(stems)               # the one suggester, shared with the loop's ingest


def push():
    import dataset_sync as d
    o = d.creds()
    if not all(o.get(k) for k in ("account_id", "access_key_id", "secret_access_key")):
        print("no R2 credentials here — push from the training machine:\n"
              "  python dataset_sync.py push")
        return
    d.push(d.client(o), o["bucket"], names=PENDING)


def main(argv):
    as_class = None
    if "--as" in argv:
        i = argv.index("--as")
        as_class = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    wanted = "--push" in argv
    files = images([a for a in argv if a != "--push"])
    if not files:
        sys.exit("no images found — give a folder or files (.jpg/.png)")
    stems = ingest(files, as_class)
    suggest(stems)
    print(f"\n{len(stems)} sample(s) written to {DATASET / 'pending' / 'images'}")
    if wanted:
        push()
    else:
        print("run: python ingest_images.py <dir> --push   # send them to the curation tool")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        main(sys.argv[1:])
        sys.exit(0)

    # Self-check: the real writer, a fake detector — no weights, no network.
    import tempfile
    from unittest.mock import patch

    import train
    from PIL import Image

    tmp = Path(tempfile.mkdtemp())
    src = tmp / "in"
    src.mkdir()
    Image.new("RGB", (800, 400), (30, 30, 30)).save(src / "a.png")
    Image.new("RGB", (800, 400), (90, 90, 90)).save(src / "b.jpg")
    (src / "notes.txt").write_text("not an image")

    boxes = {}      # first image gets a box, second gets none
    def fake(cfg):
        return (lambda img: boxes.pop("one", []), {"truck": 3})
    def fakebase(cfg):
        return (lambda img: [("motorcycle", 0.9, (0.0, 0.0, 400.0, 200.0)),
                             ("car", 0.9, (0.0, 0.0, 10.0, 10.0))], dict(ingest_video.detect.CLASS_IDS))

    with patch.object(sys.modules[__name__], "DATASET", tmp / "ds"), \
         patch.object(ingest_video, "detector", fake):
        boxes["one"] = [("truck", 0.9, (200.0, 100.0, 600.0, 300.0)),
                        ("wheel", 0.9, (0.0, 0.0, 10.0, 10.0))]   # unknown class: dropped
        files = images([str(src)])
        assert [f.name for f in files] == ["a.png", "b.jpg"], files
        stems = ingest(files)
        lab = tmp / "ds" / "pending" / "labels"
        assert len(stems) == 2 and all(s.startswith("external-") and len(s) == 21
                                       for s in stems), stems
        assert (tmp / "ds" / "pending" / "images" / f"{stems[0]}.jpg").exists()
        assert lab.joinpath(f"{stems[0]}.txt").read_text() == "3 0.500000 0.500000 0.500000 0.500000\n"
        # An image the detector finds nothing in is still a sample: the curator draws it.
        assert lab.joinpath(f"{stems[1]}.txt").read_text() == "", "empty label file expected"
        assert ingest(files) == stems, "same bytes, same stem — re-ingest overwrites"

        # --as a-motorcycle: the base model's motorcycles, under our class id, nothing else.
        with patch.object(ingest_video, "detector", fakebase):
            ingest([files[0]], "a-motorcycle")
        assert lab.joinpath(f"{stems[0]}.txt").read_text() == "1 0.250000 0.250000 0.500000 0.500000\n"

    # External frames train, always: is_val never gets a say.
    asked = []
    with patch.object(train, "is_val", lambda s: asked.append(s) or True):
        assert train.split_of("external-abc123") == "train", "web images must never validate"
        assert not asked, f"is_val consulted for an external stem: {asked}"
        assert train.split_of("RDA-TG-KTB-cam1-20260828-071500") == "val" and asked

    print(f"ingest_images self-check ok: external-<sha1> stems, {len(stems)} samples, "
          "empty labels kept, base-model motorcycles relabelled, val split untouched")
