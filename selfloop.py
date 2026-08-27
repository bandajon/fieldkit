#!/usr/bin/env python3
"""The self-improving loop, for the always-on training machine.

  python selfloop.py ingest        newest mirrored segments, every camera -> pending -> bucket
  python selfloop.py train         pull the curated set; train once enough is new; promote if better
  python selfloop.py train --now   train now, threshold or not
  python selfloop.py status        where the loop stands
  python selfloop.py adopt <run>   crown a run trained by hand (v2 was)
  python selfloop.py               self-check

Two launchd agents call ingest every few hours and train every hour (docs/SELF_LOOP.md).
Toll gates mirror their recordings into the bucket; ingest samples frames from the newest
segments with the champion model pre-labelling them, so curators correct rather than
draw. Curators approve on the online tool; approvals land in the bucket; once THRESHOLD
new frames sit outside the frozen reference set, train fine-tunes, scores the result on
the reference set, and promotes it to champion only if that score improved. The champion
pre-labels the next ingest pass — the loop closes there. Site boxes keep whatever weights
they were given until someone deploys models/champion.pt: promoting the pre-labeller is
cheap to be wrong about, promoting a toll gate's detector is not.
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
THRESHOLD = 1000      # new curated frames (outside the reference set) that earn a run
PER_CAM = 6           # newest segments per camera per pass: coverage, not volume
REMEMBER = 5000       # segment keys remembered as already sampled
MODELS = "models/"    # bucket prefix for published weights
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


# ---- the machinery ----

class Lock:
    """One thing at a time on this machine: ingest and train both want the GPU, and two
    trains at once would fight over dataset/run."""
    def __enter__(self):
        if LOCK.exists():
            try:
                pid = int(LOCK.read_text().split()[0])
                os.kill(pid, 0)
                sys.exit(f"busy: {LOCK.read_text().strip()}")
            except (ValueError, IndexError, ProcessLookupError, PermissionError):
                pass                              # stale lock from a dead process
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
        save_state(s)


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


def status():
    s = load_state()
    try:
        count = new_frames()
    except SystemExit:
        count = None
    print(f"champion:      {s.get('champion')}")
    print(f"last train:    {s.get('last_train')}")
    print(f"last ingest:   {s.get('last_ingest')}  ({len(s.get('ingested', []))} segments remembered)")
    if count is not None:
        gap = THRESHOLD - (count - s.get("trained_frames", 0))
        print(f"new frames:    {count} outside the reference set, {s.get('trained_frames', 0)} at the last run"
              f" — {'ready to train' if gap <= 0 else f'{gap} more before the next run'}")
    print(f"lock:          {LOCK.read_text().strip() if LOCK.exists() else 'free'}")


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
    print("selfloop self-check ok: cameras found under any gate prefix, newest unsampled segments "
          "picked per camera, training triggers on the cumulative threshold, promotion needs a "
          "strictly better reference score unless there is no comparable champion")


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
    else:
        sys.exit(__doc__)
