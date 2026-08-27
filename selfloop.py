#!/usr/bin/env python3
"""The self-improving loop, for the always-on training machine.

  python selfloop.py ingest        newest mirrored segments, every camera -> pending -> bucket
  python selfloop.py train         pull the curated set; train once enough is new; promote if better
  python selfloop.py train --now   train now, threshold or not
  python selfloop.py status        where the loop stands
  python selfloop.py adopt <run>   crown a run trained by hand (v2 was)
  python selfloop.py adopt-attrs <run>   the same, for an attribute run
  python selfloop.py               self-check

Two launchd agents call ingest every few hours and train every hour (docs/SELF_LOOP.md).
Toll gates mirror their recordings into the bucket; ingest samples frames from the newest
segments with the champion model pre-labelling them, so curators correct rather than
draw. Curators approve on the online tool; approvals land in the bucket; once THRESHOLD
new frames sit outside the frozen reference set, train fine-tunes, scores the result on
the reference set, and promotes it to champion only if that score improved. The champion
pre-labels the next ingest pass — the loop closes there. Site boxes keep whatever weights
they were given until someone deploys models/champion.pt: promoting the pre-labeller is
cheap to be wrong about, promoting a toll gate's detector is not. The attribute classifier
rides the same pass on the same curated set — trained after the detector, promoted on its
own mean val accuracy, and used by the next ingest to pre-fill attribute suggestions.
"""
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATASET = ROOT / "dataset"
STATE = DATASET / "loop-state.json"
LOCK = DATASET / "loop.lock"
VIDEOS = DATASET / "loop-videos"      # a segment lives here only while it is being sampled
CHAMPION = DATASET / "champion.pt"
ATTRS_CHAMPION = DATASET / "attrs-champion.pt"
THRESHOLD = 1000      # new curated frames (outside the reference set) that earn a run
PER_CAM = 6           # newest segments per camera per pass: coverage, not volume
REMEMBER = 5000       # segment keys remembered as already sampled
MODELS = "models/"    # bucket prefix for published weights
ATTR_MODELS = MODELS + "attrs/"
SKIP = ("curation/", MODELS)
PENDING = ("pending/images", "pending/labels", "pending/attrs", "pending/suggest")


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_state():
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {"ingested": [], "trained_frames": 0, "champion": None}


def save_state(s):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(s, indent=1))


# ---- pure pieces, the ones the self-check exercises ----

def cameras(keys):
    """{"<site>/<cam>": [segment keys, newest last]} from a bucket listing; anything under
    curation/ or models/ is not footage."""
    out = {}
    for k in keys:
        if k.startswith(SKIP) or not k.endswith(".mkv"):
            continue
        parts = k.split("/")
        if len(parts) != 3:
            continue
        out.setdefault(parts[0] + "/" + parts[1], []).append(k)
    return {c: sorted(v) for c, v in out.items()}


def pick(keys, ingested, per_cam=PER_CAM):
    """The newest `per_cam` segments of every camera that have not been sampled yet."""
    done = set(ingested)
    chosen = []
    for cam, segs in sorted(cameras(keys).items()):
        fresh = [k for k in segs if k not in done]
        chosen += fresh[-per_cam:]
    return chosen


def should_train(new_frames, trained_frames, threshold=THRESHOLD):
    """Another THRESHOLD frames since the last run — cumulative, so a run that used 1,400
    is followed by one at 2,400, never by one at 2,000."""
    return new_frames - trained_frames >= threshold


def promote(candidate, champion):
    """Does this run's reference score earn it the champion slot? No champion, or a
    champion with no comparable score (v2 trained on the reference set, so its number
    would be inflated), is beaten by anything. Otherwise strictly better mAP50."""
    if champion is None or champion.get("map50") is None:
        return True
    return bool(candidate) and candidate.get("map50") is not None and candidate["map50"] > champion["map50"]


def promote_attrs(candidate, champion):
    """The same rule for the attribute classifier, on the mean val accuracy across its
    heads: a hand-trained champion with no report to compare against loses to anything,
    otherwise a run has to be strictly better. Mean, not per-head — the heads share one
    backbone, so they are promoted or not as one model."""
    if champion is None or champion.get("mean_acc") is None:
        return True
    return bool(candidate) and candidate.get("mean_acc") is not None \
        and candidate["mean_acc"] > champion["mean_acc"]


# ---- the machinery ----

WAIT = 900            # seconds a pass waits for the lock before giving up on this tick


def holder():
    """Pid of a live lock holder, or None (no lock, or a dead process left it)."""
    try:
        pid = int(LOCK.read_text().split()[0])
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError, IndexError):
        return None


class Lock:
    """One thing at a time on this machine: ingest and train both want the GPU, and two
    trains at once would fight over dataset/run. A pass that finds the lock taken waits
    a while rather than skipping its whole interval — the two agents fire together at
    login, and an hourly check colliding with a ten-minute ingest is routine."""
    def __enter__(self):
        import time
        deadline = time.monotonic() + WAIT
        while holder() is not None:
            if time.monotonic() > deadline:
                sys.exit(f"busy: {LOCK.read_text().strip()}")
            time.sleep(15)
        LOCK.parent.mkdir(parents=True, exist_ok=True)
        LOCK.write_text(f"{os.getpid()} {sys.argv[1:]} since {now()}")
        return self

    def __exit__(self, *_):
        LOCK.unlink(missing_ok=True)


def r2():
    import dataset_sync
    o = dataset_sync.creds()
    return dataset_sync, dataset_sync.client(o), o["bucket"]


def bucket_keys(cl, bucket):
    for page in cl.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        for obj in page.get("Contents", []):
            yield obj["Key"]


def new_frames():
    """Approved frames outside the reference set — what the next run would train on."""
    import train
    ref = train.reference()
    return sum(1 for p in (DATASET / "approved" / "labels").glob("*.txt") if p.stem not in ref)


def suggest(stems):
    """Pre-fill attribute suggestions for the samples this pass just wrote.

    The champion's guess is a suggestion, never a record: the Label tab shows it greyed
    and app.py deletes the sidecar the moment a curator saves the sample. That is the
    whole point — correcting five taps is faster than typing five, and a wrong guess
    costs a tap rather than a bad label.
    """
    import detect
    import train_attrs
    from PIL import Image
    try:
        import torch
        dev = "mps" if torch.backends.mps.is_available() else "cpu"
    except Exception:
        dev = "cpu"
    classify = detect.attr_classifier(str(ATTRS_CHAMPION), dev)   # the one loader, shared with live detection
    out = DATASET / "pending" / "suggest"
    out.mkdir(parents=True, exist_ok=True)
    written = 0
    for stem in stems:
        js = out / f"{stem}.json"
        if js.exists():        # suggested already, or a re-ingest of the same stem
            continue
        try:
            img = Image.open(DATASET / "pending" / "images" / f"{stem}.jpg").convert("RGB")
            lines = (DATASET / "pending" / "labels" / f"{stem}.txt").read_text().splitlines()
            got = {}
            # The key is the box's index as app.py counts them, which is its position
            # among the well-formed lines — skipping a short row here and keeping it
            # there would shift every suggestion after it onto the wrong vehicle.
            for i, box in enumerate(b for b in map(str.split, lines) if len(b) == 5):
                crop = train_attrs.crop_box(img, box[1:5])   # same padding the heads trained on
                if crop is not None:
                    got[str(i)] = classify(crop)
            if got:
                js.write_text(json.dumps(got, indent=1))
                written += 1
        except Exception as e:      # one unreadable sample must not cost the whole pass
            print(f"  ! suggest {stem}: {e}", flush=True)
    print(f"{now()} ingest: attributes suggested for {written} sample(s)", flush=True)


def ingest_pass():
    import ingest_video
    with Lock():
        s = load_state()
        ds, cl, bucket = r2()
        todo = pick(list(bucket_keys(cl, bucket)), s["ingested"])
        print(f"{now()} ingest: {len(todo)} new segment(s) across the cameras", flush=True)
        if not todo:
            return
        cfg = ingest_video.config()
        if CHAMPION.is_file():
            cfg["detect_weights"] = str(CHAMPION)   # the loop's own model pre-labels
        VIDEOS.mkdir(parents=True, exist_ok=True)
        files = []
        for key in todo:
            dest = VIDEOS / key.replace("/", "__")
            print(f"  v {key}", flush=True)
            cl.download_file(bucket, key, str(dest))
            files.append(dest)
        try:
            sink = ingest_video.ingest(files, cfg)
        finally:
            shutil.rmtree(VIDEOS, ignore_errors=True)  # sampled: the footage stays in the bucket
        if ATTRS_CHAMPION.is_file():
            suggest(sink.stems)      # pushed with the samples: PENDING covers pending/suggest
        s["ingested"] = (s["ingested"] + todo)[-REMEMBER:]
        s["last_ingest"] = {"at": now(), "segments": len(todo), "samples": sum(sink.written.values())}
        save_state(s)
        sent, _ = ds.push(cl, bucket, names=PENDING)
        print(f"{now()} ingest: {s['last_ingest']['samples']} samples written, {sent} files pushed", flush=True)


def train_pass(force=False):
    import train as trainer
    with Lock():
        s = load_state()
        ds, cl, bucket = r2()
        ds.pull(cl, bucket, names=ds.CONFIG)
        ds.pull(cl, bucket, names=ds.LEDGERS)
        count = new_frames()
        if not force and not should_train(count, s.get("trained_frames", 0)):
            print(f"{now()} train: {count} new frames, {s.get('trained_frames', 0)} at the last run "
                  f"— {THRESHOLD - (count - s.get('trained_frames', 0))} more to go", flush=True)
            return
        print(f"{now()} train: {count} new frames — training", flush=True)
        r = subprocess.run([sys.executable, str(ROOT / "train.py")], cwd=ROOT)
        if r.returncode:
            sys.exit(f"train.py failed ({r.returncode}); champion untouched")
        run = max(trainer.RUNS.glob("*/"), key=lambda p: p.stat().st_mtime)
        best = run / "weights" / "best.pt"
        ev = run_eval(run)
        s["trained_frames"], s["last_train"] = count, {"at": now(), "run": run.name, "eval": ev}
        if promote(ev, s.get("champion")):
            crown(s, run, count, ev, cl, bucket)
        else:
            print(f"{now()} train: {run.name} scored {ev['map50'] if ev else 'n/a'} on the reference, "
                  f"champion stays at {s['champion']['map50']}", flush=True)
        attrs_pass(s, cl, bucket)
        save_state(s)


def attrs_pass(s, cl, bucket):
    """Train and judge the attribute classifier on the same curated set, after the
    detector. Not `all`: the reference frames trained the baseline and nothing since.
    A failure here leaves the detector's result standing — the two models are promoted
    independently, and a head that will not train is no reason to lose a good detector."""
    import train_attrs

    r = subprocess.run([sys.executable, str(ROOT / "train_attrs.py")], cwd=ROOT)
    if r.returncode:
        print(f"{now()} attrs: train_attrs.py failed ({r.returncode}); attrs champion untouched",
              flush=True)
        return
    run = max(train_attrs.RUNS.glob("*/"), key=lambda p: p.stat().st_mtime)
    rep = attrs_report(run)
    s["last_attrs"] = {"at": now(), "run": run.name, "mean_acc": rep and rep["mean_acc"]}
    if promote_attrs(rep, s.get("attrs_champion")):
        crown_attrs(s, run, rep, cl, bucket)
    else:
        print(f"{now()} attrs: {run.name} scored {rep['mean_acc'] if rep else 'n/a'} mean val "
              f"accuracy, attrs champion stays at {s['attrs_champion']['mean_acc']}", flush=True)


def attrs_report(run):
    try:
        return json.loads((run / "report.json").read_text())
    except (OSError, ValueError):
        return None


def run_eval(run):
    try:
        return json.loads((run / "reference-eval.json").read_text())
    except (OSError, ValueError):
        return None


def crown(s, run, frames, ev, cl, bucket):
    """Make this run the champion: the pre-labeller here, and models/champion.pt in the
    bucket for whoever deploys it to a gate."""
    best = run / "weights" / "best.pt"
    shutil.copy2(best, CHAMPION)
    s["champion"] = {"run": run.name, "frames": frames, "at": now(),
                     **({k: ev[k] for k in ("map50", "map50_95")} if ev else {"map50": None})}
    for key in (f"{MODELS}{run.name}/best.pt", f"{MODELS}champion.pt"):
        cl.upload_file(str(best), bucket, key)
    if ev:
        cl.upload_file(str(run / "reference-eval.json"), bucket, f"{MODELS}{run.name}/reference-eval.json")
    cl.put_object(Bucket=bucket, Key=f"{MODELS}champion.json", Body=json.dumps(s["champion"]).encode())
    print(f"{now()}: {run.name} is the champion (reference mAP50 {ev['map50'] if ev else 'n/a'}) "
          f"— published under {MODELS}", flush=True)


def crown_attrs(s, run, rep, cl, bucket):
    """Make this run the attribute champion: it suggests attributes on the next ingest
    here, and models/attrs-champion.pt is the offer for whoever deploys `attr_weights`."""
    weights = run / "attrs.pt"
    shutil.copy2(weights, ATTRS_CHAMPION)
    s["attrs_champion"] = {"run": run.name, "at": now(), "mean_acc": rep and rep["mean_acc"],
                           "per_head_acc": rep and rep["per_head_acc"]}
    for key in (f"{ATTR_MODELS}{run.name}/attrs.pt", f"{MODELS}attrs-champion.pt"):
        cl.upload_file(str(weights), bucket, key)
    if (run / "report.json").is_file():
        cl.upload_file(str(run / "report.json"), bucket, f"{ATTR_MODELS}{run.name}/report.json")
    cl.put_object(Bucket=bucket, Key=f"{MODELS}attrs-champion.json",
                  Body=json.dumps(s["attrs_champion"]).encode())
    print(f"{now()}: {run.name} is the attrs champion (mean val accuracy "
          f"{rep['mean_acc'] if rep else 'n/a'}) — published under {ATTR_MODELS}", flush=True)


def adopt(name):
    """Crown a run trained outside the loop — v2, trained by hand before the loop
    existed. No threshold, no comparison: the operator has decided."""
    import train as trainer
    run = trainer.RUNS / name
    if not (run / "weights" / "best.pt").is_file():
        sys.exit(f"no weights under {run}")
    with Lock():
        s = load_state()
        ds, cl, bucket = r2()
        s["trained_frames"] = max(s.get("trained_frames", 0), new_frames())
        crown(s, run, s["trained_frames"], run_eval(run), cl, bucket)
        save_state(s)


def adopt_attrs(name):
    """Crown an attribute run trained outside the loop — the baseline was. As with
    adopt(): no comparison, the operator has decided."""
    import train_attrs
    run = train_attrs.RUNS / name
    if not (run / "attrs.pt").is_file():
        sys.exit(f"no attrs.pt under {run}")
    with Lock():
        s = load_state()
        _ds, cl, bucket = r2()
        crown_attrs(s, run, attrs_report(run), cl, bucket)
        save_state(s)


def status():
    s = load_state()
    try:
        count = new_frames()
    except SystemExit:
        count = None
    print(f"champion:      {s.get('champion')}")
    print(f"attrs champ:   {s.get('attrs_champion')}")
    print(f"last train:    {s.get('last_train')}")
    print(f"last attrs:    {s.get('last_attrs')}")
    print(f"last ingest:   {s.get('last_ingest')}  ({len(s.get('ingested', []))} segments remembered)")
    if count is not None:
        gap = THRESHOLD - (count - s.get("trained_frames", 0))
        print(f"new frames:    {count} outside the reference set, {s.get('trained_frames', 0)} at the last run"
              f" — {'ready to train' if gap <= 0 else f'{gap} more before the next run'}")
    print(f"lock:          {LOCK.read_text().strip() if LOCK.exists() else 'free'}")


def suggest_check():
    """detect.attr_classifier() must load what train_attrs.py saves, and suggest() must
    write the sidecar the Label tab reads. Untested, this pair rots silently — the loader
    once rebuilt a different network and could not load a real checkpoint at all. Random
    weights: only the shapes, the vocabulary and the file layout are under test here."""
    import contextlib
    import io
    import tempfile
    from unittest.mock import patch
    try:
        import torch
        from torch import nn
        from torchvision.models import mobilenet_v3_small
        from PIL import Image
    except ImportError as e:
        print(f"  (suggest check skipped: {e})")
        return

    tmp = Path(tempfile.mkdtemp())
    heads = {"type": ["car", "bus"], "axles": ["2", "3", "4"]}
    m = mobilenet_v3_small(weights=None)
    fc = nn.ModuleList([nn.Linear(576, len(v)) for v in heads.values()])
    ckpt = tmp / "attrs.pt"
    torch.save({"state_dict": {**{f"features.{k}": v for k, v in m.features.state_dict().items()},
                               **{f"fc.{k}": v for k, v in fc.state_dict().items()}},
                "heads": heads, "backbone": "mobilenet_v3_small", "input": 224}, ckpt)

    pend = tmp / "dataset" / "pending"
    (pend / "images").mkdir(parents=True)
    (pend / "labels").mkdir(parents=True)
    Image.new("RGB", (640, 480), "grey").save(pend / "images" / "gate-1.jpg")
    # The short middle row is what app.py's reader skips: box "1" must be the third line.
    (pend / "labels" / "gate-1.txt").write_text(
        "0 0.5 0.5 0.2 0.3\n1 0.1 0.1\n1 0.1 0.1 0.05 0.05\n")

    me = sys.modules[__name__]
    # suggest() reports to the ingest log; here its chatter, the deliberate failure
    # included, would drown the one line a self-check is supposed to print.
    with patch.object(me, "DATASET", tmp / "dataset"), \
         patch.object(me, "ATTRS_CHAMPION", ckpt), \
         contextlib.redirect_stdout(io.StringIO()) as log:
        suggest(["gate-1", "missing-stem"])       # a stem with no image must not stop the pass
        got = json.loads((pend / "suggest" / "gate-1.json").read_text())
        before = (pend / "suggest" / "gate-1.json").stat().st_mtime_ns
        suggest(["gate-1"])                       # already suggested: left alone
        assert (pend / "suggest" / "gate-1.json").stat().st_mtime_ns == before, "re-suggested"
    assert "! suggest missing-stem" in log.getvalue(), log.getvalue()
    assert "suggested for 1 sample" in log.getvalue(), log.getvalue()
    assert list(got) == ["0", "1"], got           # keyed by label line, the box index
    assert all(set(v) == set(heads) and v["type"] in heads["type"]
               and v["axles"] in heads["axles"] for v in got.values()), got


def selfcheck():
    keys = ["curation/pending/images/x.jpg", "models/champion.pt",
            "site1/cam3/20260827-100000.mkv", "site1/cam3/20260827-101000.mkv", "site1/cam3/20260827-102000.mkv",
            "RDA-TG-KTB/north/20260827-100000.mkv", "site1/cam3/notes.txt", "loose.mkv"]
    cams = cameras(keys)
    assert set(cams) == {"site1/cam3", "RDA-TG-KTB/north"}, cams
    assert cams["site1/cam3"][-1].endswith("102000.mkv"), "newest last"
    got = pick(keys, ingested=["site1/cam3/20260827-102000.mkv"], per_cam=2)
    assert got == ["RDA-TG-KTB/north/20260827-100000.mkv",
                   "site1/cam3/20260827-100000.mkv", "site1/cam3/20260827-101000.mkv"], got
    assert pick(keys, ingested=keys) == [], "everything sampled: nothing to do"
    assert should_train(1000, 0) and not should_train(999, 0)
    assert should_train(2400, 1400) and not should_train(2000, 1400), "cumulative, from the last run"
    assert promote({"map50": 0.5}, None) and promote({"map50": 0.1}, {"map50": None})
    assert promote({"map50": 0.71}, {"map50": 0.70}) and not promote({"map50": 0.70}, {"map50": 0.70})
    assert not promote(None, {"map50": 0.70}), "no score, no promotion"
    assert promote_attrs({"mean_acc": 0.5}, None) and promote_attrs({"mean_acc": 0.1}, {"mean_acc": None})
    assert promote_attrs({"mean_acc": 0.88}, {"mean_acc": 0.87})
    assert not promote_attrs({"mean_acc": 0.87}, {"mean_acc": 0.87}), "a tie is not an improvement"
    assert not promote_attrs(None, {"mean_acc": 0.87}), "no report, no promotion"
    suggest_check()
    print("selfloop self-check ok: cameras found under any gate prefix, newest unsampled segments "
          "picked per camera, training triggers on the cumulative threshold, promotion needs a "
          "strictly better reference score (detector) or mean val accuracy (attributes) unless "
          "there is no comparable champion")


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        selfcheck()
    elif a[0] == "ingest":
        ingest_pass()
    elif a[0] == "train":
        train_pass(force="--now" in a)
    elif a[0] == "status":
        status()
    elif a[0] == "adopt" and len(a) == 2:
        adopt(a[1])
    elif a[0] == "adopt-attrs" and len(a) == 2:
        adopt_attrs(a[1])
    else:
        sys.exit(__doc__)
