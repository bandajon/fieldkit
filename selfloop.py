#!/usr/bin/env python3
"""The self-improving loop, for the always-on training machine.

  python selfloop.py ingest        newest mirrored segments, every camera -> pending -> bucket
  python selfloop.py classify      newest un-classified segments -> vehicle events -> bucket
  python selfloop.py hunt          last HUNT_HOURS of footage, newest first -> only frames of
                                   wanted classes -> the queue
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

Classify is the other direction: the same champion, run over the mirrored recordings, is
what turns a gate that only records into a gate that reports. Its events go to the bucket
for the RDA importer, ten to twenty minutes behind live.
"""
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

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
EVENTS = "fieldkit-events/"   # bucket prefix the RDA importer reads
# The Katuba box still calls itself site1; anything already named RDA-TG-* is its own id.
GATES = {"site1": "RDA-TG-KTB"}
TZ = "Africa/Lusaka"          # the gates and this machine; segment names are local wallclock
WANT_BOXES = 300      # per-class floor for the next run; under it, a class is hunted
CLASSIFY_PER_PASS = 12        # ~10 min of footage per camera per pass, at 600 s segments
HUNT_HOURS = 48               # how far back a hunt looks: what the gates keep mirrored
HUNT_PER_PASS = 30            # segments per pass, ~25 s each at 1 fps on the GPU: the
                              # lock is back within classify's patience (WAIT)
REMEMBER_CLASSIFIED = 20000


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


def pick_hunt(keys, hunted, since, per_pass=HUNT_PER_PASS):
    """The newest un-hunted segments recorded after `since` (a YYYYMMDD-HHMMSS stem,
    local wallclock like the segment names), newest first across every camera."""
    done = set(hunted)
    fresh = sorted((k for segs in cameras(keys).values() for k in segs
                    if k not in done and Path(k).stem >= since), key=lambda k: Path(k).stem)
    return fresh[::-1][:per_pass]


def should_train(new_frames, trained_frames, threshold=THRESHOLD):
    """Another THRESHOLD frames since the last run — cumulative, so a run that used 1,400
    is followed by one at 2,400, never by one at 2,000."""
    return new_frames - trained_frames >= threshold


def champion_score(ev):
    """What a crowned run is later compared against. A run that trained on the reference
    set (train.py baseline — v2, and the YOLO26 comparison runs) scored itself on its own
    training frames, so its number would be a bar no honest run could clear: recorded as
    none, and the next loop-trained model replaces it unconditionally."""
    if not ev or ev.get("baseline"):
        return {"map50": None}
    return {k: ev.get(k) for k in ("map50", "map50_95")}


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


def local_path(key):
    """Where a bucket segment lands while it is being sampled: the same <site>/<cam>/
    <segment> shape as the bucket, because the camera's name is read off the path."""
    return VIDEOS / key


def wanted_classes(counts, target=WANT_BOXES):
    """Classes still under the floor — what the sampling passes capture off-cadence.
    Counted in NEW boxes only: the frozen reference frames train nothing, so they say
    nothing about where the next run is thin."""
    return sorted(n for n, c in counts.items() if c < target)


def new_box_counts():
    """{class: boxes in approved frames outside the reference set}.

    ponytail: full scan of approved/labels each pass, the same one app.py's counter does
    — a few thousand small files, seconds. Upgrade path: cache it beside the ledger if
    the set outgrows that. Kept here rather than imported from app.py: a launchd job
    must not drag FastAPI in to count lines.
    """
    import train
    names, ref = train.classes(), train.reference()
    counts = {}
    for p in (DATASET / "approved" / "labels").glob("*.txt"):
        if p.stem in ref:
            continue
        try:
            text = p.read_text()
        except OSError:
            continue
        for line in text.splitlines():
            f = line.split()
            if not f:
                continue
            try:
                cls = int(f[0])
            except ValueError:
                continue
            if 0 <= cls < len(names):      # a stale id past the class list is skipped
                counts[names[cls]] = counts.get(names[cls], 0) + 1
    return {n: counts.get(n, 0) for n in names}


def gate_of(prefix):
    """The RDA gate id for a bucket prefix. The importer resolves events by gate, so a
    prefix with no mapping is passed through — a gate already named for itself is right,
    and a genuinely unknown one is better filed visibly wrong than silently dropped."""
    return GATES.get(prefix, prefix)


def pick_classify(keys, classified, per_pass=CLASSIFY_PER_PASS):
    """The newest unclassified segments across every camera, newest first: the portal
    wants today's traffic on screen, and the backlog drains whenever the gates are quiet.
    Ordered by segment name, not by key — sorting whole keys would work one camera dry
    before the other was touched."""
    done = set(classified)
    todo = [k for segs in cameras(keys).values() for k in segs if k not in done]
    return sorted(todo, key=lambda k: (k.rsplit("/", 1)[1], k), reverse=True)[:per_pass]


def for_upload(doc, gate):
    """One event as the importer reads it: stamped with the gate it came from, and its
    crop paths flattened — the bucket keeps one crops/ folder per day, not per day-inside-
    a-day like the local events dir does."""
    return {**doc, "gate": gate,
            "crops": {tag: f"crops/{Path(rel).name}"
                      for tag, rel in (doc.get("crops") or {}).items()}}


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
        LOCK.parent.mkdir(parents=True, exist_ok=True)
        while True:
            # O_EXCL makes the check and the take one step. The old check-then-write let
            # two agents that woke in the same second both see it free and both proceed
            # — ingest then deleted the download folder under a running classify pass.
            try:
                fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if holder() is None:
                    LOCK.unlink(missing_ok=True)      # a dead process left it: take over
                    continue                          # ...through O_EXCL, never around it
                if time.monotonic() > deadline:
                    sys.exit(f"busy: {LOCK.read_text().strip()}")
                time.sleep(15)
                continue
            with os.fdopen(fd, "w") as f:
                f.write(f"{os.getpid()} {sys.argv[1:]} since {now()}")
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


def hunting():
    """The wanted list for this pass, announced so the log says what it was aiming at."""
    wanted = wanted_classes(new_box_counts())
    what = ", ".join(wanted) or f"nothing — every class is past {WANT_BOXES}"
    print(f"{now()} hunting: {what}", flush=True)
    return wanted


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
        cfg["capture_wanted"] = hunting()
        files = []
        for key in todo:
            # Mirror the bucket's <site>/<cam>/<segment> on disk: the sampler names the
            # camera after the parent directory, exactly as it does for a recorder's own
            # tree. One flat folder had every camera sampling as "loop-videos" — one shared
            # dedup clock, and stems that could collide across cameras.
            dest = local_path(key)
            dest.parent.mkdir(parents=True, exist_ok=True)
            print(f"  v {key}", flush=True)
            cl.download_file(bucket, key, str(dest))
            files.append(dest)
        stems, written = [], 0
        try:
            # One sampling run per gate: the sink stamps every stem with the gate id.
            by_gate = {}
            for f in files:
                by_gate.setdefault(gate_of(f.relative_to(VIDEOS).parts[0]), []).append(f)
            for gate, fs in by_gate.items():
                sink = ingest_video.ingest(fs, {**cfg, "toll_gate_id": gate})
                stems += sink.stems
                written += sum(sink.written.values())
        finally:
            for f in files:                # only what this pass downloaded; the folder is shared
                f.unlink(missing_ok=True)  # sampled: the footage stays in the bucket
        if ATTRS_CHAMPION.is_file():
            suggest(stems)           # pushed with the samples: PENDING covers pending/suggest
        s["ingested"] = (s["ingested"] + todo)[-REMEMBER:]
        s["last_ingest"] = {"at": now(), "segments": len(todo), "samples": written}
        save_state(s)
        sent, _ = ds.push(cl, bucket, names=PENDING)
        print(f"{now()} ingest: {s['last_ingest']['samples']} samples written, {sent} files pushed", flush=True)


def hunt_pass():
    """Frames of the classes the curated set is short of, from the last HUNT_HOURS.

    The sampling passes see a slice of the footage and the classify pass sees all of it
    but slowly (it tracks every frame). This decodes at the ingest rate and keeps nothing
    but wanted-class frames, so two days of footage are swept in hours and the queue's
    bus, plant and abnormal-load frames are days old at most, not a week.
    """
    import ingest_video
    from datetime import timedelta
    with Lock():
        s = load_state()
        wanted = hunting()
        if not wanted:
            return
        ds, cl, bucket = r2()
        since = (datetime.now(ZoneInfo(TZ)) - timedelta(hours=HUNT_HOURS)).strftime("%Y%m%d-%H%M%S")
        todo = pick_hunt(list(bucket_keys(cl, bucket)), s.get("hunted", []), since)
        print(f"{now()} hunt: {len(todo)} segment(s) since {since} to sweep", flush=True)
        if not todo:
            return
        cfg = ingest_video.config()
        if CHAMPION.is_file():
            cfg["detect_weights"] = str(CHAMPION)
        cfg["capture_wanted"], cfg["capture_only_wanted"] = wanted, True
        files = []
        for key in todo:
            dest = local_path(key)
            dest.parent.mkdir(parents=True, exist_ok=True)
            cl.download_file(bucket, key, str(dest))
            files.append(dest)
        stems, written = [], 0
        try:
            by_gate = {}
            for f in files:
                by_gate.setdefault(gate_of(f.relative_to(VIDEOS).parts[0]), []).append(f)
            for gate, fs in by_gate.items():
                sink = ingest_video.ingest(fs, {**cfg, "toll_gate_id": gate})
                stems += sink.stems
                written += sum(sink.written.values())
        finally:
            for f in files:
                f.unlink(missing_ok=True)
        if ATTRS_CHAMPION.is_file():
            suggest(stems)
        s["hunted"] = (s.get("hunted", []) + todo)[-REMEMBER:]
        s["last_hunt"] = {"at": now(), "segments": len(todo), "samples": written, "wanted": wanted}
        save_state(s)
        sent, _ = ds.push(cl, bucket, names=PENDING)
        print(f"{now()} hunt: {written} wanted-class samples written, {sent} files pushed", flush=True)


def publish(cl, bucket, out, gate, seg, day):
    """Send one segment's events and crops to fieldkit-events/<gate>/<YYYYMMDD>/, the
    layout the RDA importer reads. -> events published."""
    # A quiet segment still publishes an empty file: that is what marks it done, and it
    # tells the importer the gate was watched and idle rather than never processed.
    (out / f"{day}.jsonl").touch()
    total = 0
    for js in sorted(out.glob("*.jsonl")):      # a segment can straddle midnight
        lines = [json.dumps(for_upload(json.loads(l), gate))
                 for l in js.read_text().splitlines() if l.strip()]
        total += len(lines)
        where = f"{EVENTS}{gate}/{js.stem.replace('-', '')}"
        cl.put_object(Bucket=bucket, Key=f"{where}/{seg}.jsonl",
                      Body="".join(l + "\n" for l in lines).encode(),
                      ContentType="application/json")
        for crop in sorted((out / "crops" / js.stem).glob("*.jpg")):
            cl.upload_file(str(crop), bucket, f"{where}/crops/{crop.name}")
    return total


def classify_pass():
    """Vehicle events from the footage the gates mirror to the bucket.

    The live pipeline already turns frames into counted vehicles; this runs that same
    pipeline over the recordings instead of the wire, so a gate box that only records
    still produces events. Ten to twenty minutes behind live, which the portal is happy
    with — and the alternative is a second detector to keep in step with the first.
    """
    import tempfile

    import detect
    import ingest_video

    if not CHAMPION.is_file():
        print(f"{now()} classify: no detector at {CHAMPION} — nothing to classify", flush=True)
        return
    with Lock():
        s = load_state()
        ds, cl, bucket = r2()
        todo = pick_classify(list(bucket_keys(cl, bucket)), s.get("classified", []))
        print(f"{now()} classify: {len(todo)} segment(s)", flush=True)
        if not todo:
            return
        base, tz = ingest_video.config(), ZoneInfo(TZ)
        # This pass decodes every mirrored segment anyway, so it sees all 48 h of footage
        # — the widest net there is for a class that shows up twice a day.
        base["capture_wanted"], base["dataset_dir"] = hunting(), str(DATASET)
        VIDEOS.mkdir(parents=True, exist_ok=True)
        done = events = 0
        stems = []
        for key in todo:
            prefix, cam_name, seg = key.split("/")
            gate = gate_of(prefix)
            cfg = {**base, "detect_weights": str(CHAMPION), "toll_gate_id": gate}
            if ATTRS_CHAMPION.is_file():
                cfg["attr_weights"] = str(ATTRS_CHAMPION)
            # Heading rides on the camera's config entry; without one there is no
            # direction on these events, which is right — it is never guessed.
            cam = next((c for c in (base.get("cameras") or []) if c.get("name") == cam_name),
                       {"name": cam_name})
            # The recorder's own filename is the footage clock: keep it, or cam_and_start
            # falls back to mtime and every event is stamped with the download.
            video, out = local_path(key), Path(tempfile.mkdtemp())
            video.parent.mkdir(parents=True, exist_ok=True)
            try:
                print(f"  v {key}", flush=True)
                cl.download_file(bucket, key, str(video))
                day, captured = detect.classify_segment(video, cam, cfg, tz, out)
                stems += captured
                n = publish(cl, bucket, out, gate, Path(seg).stem, day)
                # Recorded only once published: a segment that died mid-pass is retried
                # next tick rather than lost, and re-running it rewrites the same keys.
                s["classified"] = (s.get("classified", []) + [key])[-REMEMBER_CLASSIFIED:]
                save_state(s)
                done, events = done + 1, events + n
                print(f"    {n} event(s) -> {EVENTS}{gate}/{day.replace('-', '')}/", flush=True)
            except Exception as e:       # one bad segment must not strand the rest
                print(f"  ! {key}: {e}", flush=True)
            finally:
                video.unlink(missing_ok=True)
                shutil.rmtree(out, ignore_errors=True)
        if stems:
            if ATTRS_CHAMPION.is_file():
                suggest(stems)       # same courtesy the ingest pass does: correct, not type
            sent, _ = ds.push(cl, bucket, names=PENDING)
            print(f"{now()} classify: {len(stems)} frame(s) captured for the queue, "
                  f"{sent} file(s) pushed", flush=True)
        s["last_classify"] = {"at": now(), "segments": done, "events": events,
                              "captured": len(stems)}
        save_state(s)
        print(f"{now()} classify: {done}/{len(todo)} segment(s), {events} event(s) published",
              flush=True)


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
    s["champion"] = {"run": run.name, "frames": frames, "at": now(), **champion_score(ev)}
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
    print(f"last classify: {s.get('last_classify')}  "
          f"({len(s.get('classified', []))} segments remembered)")
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


def lock_check():
    """The lock is the only thing between two GPU jobs. A dead holder must be taken over;
    a live one must be waited for; and the take must be atomic."""
    import tempfile
    from unittest.mock import patch
    me = sys.modules[__name__]
    tmp = Path(tempfile.mkdtemp()) / "loop.lock"
    with patch.object(me, "LOCK", tmp), patch.object(me, "WAIT", 0):
        tmp.write_text("999999999 ['ghost'] since never")   # no such pid: stale
        with Lock():
            assert tmp.read_text().startswith(str(os.getpid())), "stale lock not taken over"
            try:
                with Lock():
                    raise AssertionError("a live lock was granted twice")
            except SystemExit as e:
                assert "busy" in str(e), e
        assert not tmp.exists(), "lock not released"


def publish_check():
    """The bucket layout the RDA importer reads is a contract: gate, day, one jsonl per
    segment, crops beside it. Nothing downstream is ours, so this is where a rename or a
    stray path separator has to be caught."""
    import tempfile

    class FakeS3:
        def __init__(self):
            self.put, self.sent = {}, []

        def put_object(self, Bucket, Key, Body, ContentType=None):
            self.put[Key] = Body

        def upload_file(self, path, Bucket, Key):
            self.sent.append(Key)

    out = Path(tempfile.mkdtemp())
    (out / "crops" / "2026-08-19").mkdir(parents=True)
    (out / "crops" / "2026-08-19" / "c-123-best.jpg").write_bytes(b"jpeg")
    (out / "2026-08-19.jsonl").write_text(
        json.dumps({"id": "c-123", "class": "e-heavy",
                    "crops": {"best": "crops/2026-08-19/c-123-best.jpg"}}) + "\n")
    cl = FakeS3()
    assert publish(cl, "buck", out, "RDA-TG-KTB", "20260819-151108", "2026-08-19") == 1
    key = "fieldkit-events/RDA-TG-KTB/20260819/20260819-151108.jsonl"
    assert list(cl.put) == [key], cl.put
    doc = json.loads(cl.put[key].decode())
    assert doc["gate"] == "RDA-TG-KTB" and doc["crops"] == {"best": "crops/c-123-best.jpg"}, doc
    assert cl.sent == ["fieldkit-events/RDA-TG-KTB/20260819/crops/c-123-best.jpg"], cl.sent

    # A segment with no vehicles still publishes, or it would be classified again forever.
    quiet, cl = Path(tempfile.mkdtemp()), FakeS3()
    assert publish(cl, "buck", quiet, "RDA-TG-KTB", "20260819-152108", "2026-08-19") == 0
    assert cl.put == {"fieldkit-events/RDA-TG-KTB/20260819/20260819-152108.jsonl": b""}, cl.put


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
    assert champion_score({"map50": 0.9, "map50_95": 0.6, "baseline": True}) == {"map50": None}, \
        "a baseline run's inflated score must not become the bar"
    assert champion_score({"map50": 0.71, "map50_95": 0.5}) == {"map50": 0.71, "map50_95": 0.5}
    assert promote_attrs({"mean_acc": 0.5}, None) and promote_attrs({"mean_acc": 0.1}, {"mean_acc": None})
    assert promote_attrs({"mean_acc": 0.88}, {"mean_acc": 0.87})
    assert not promote_attrs({"mean_acc": 0.87}, {"mean_acc": 0.87}), "a tie is not an improvement"
    assert not promote_attrs(None, {"mean_acc": 0.87}), "no report, no promotion"
    assert local_path("site1/cam3/20260827-100000.mkv").parent.name == "cam3", \
        "the camera must be the parent directory, or every camera samples as one"
    assert wanted_classes({"a": 5, "b": 300, "c": 299, "d": 0}, target=300) == ["a", "c", "d"]
    assert wanted_classes({"a": 5}, target=0) == [], "a floor of 0 hunts nothing"
    assert gate_of("site1") == "RDA-TG-KTB", "the Katuba box still calls itself site1"
    assert gate_of("RDA-TG-KTB") == "RDA-TG-KTB", "a gate already named for itself"
    # Newest segment first, across cameras, so the portal gets current traffic first.
    got = pick_classify(keys, classified=["site1/cam3/20260827-102000.mkv"], per_pass=2)
    assert got == ["site1/cam3/20260827-101000.mkv", "site1/cam3/20260827-100000.mkv"], got
    assert pick_classify(keys, classified=[])[0].endswith("102000.mkv"), "newest first"
    assert pick_classify(keys, classified=keys) == [], "everything classified: nothing to do"
    hunted = pick_hunt(keys, hunted=["site1/cam3/20260827-102000.mkv"], since="20260827-100000")
    assert hunted and all(Path(k).stem >= "20260827-100000" for k in hunted) \
        and "site1/cam3/20260827-102000.mkv" not in hunted, "hunt: newest un-hunted since the cutoff"
    assert hunted == sorted(hunted, key=lambda k: Path(k).stem, reverse=True), "hunt: newest first"
    assert pick_hunt(keys, hunted=[], since="20990101-000000") == [], "hunt: nothing that recent"
    up = for_upload({"id": "c-1", "crops": {"best": "crops/2026-08-19/c-1-best.jpg"}},
                    "RDA-TG-KTB")
    assert up == {"id": "c-1", "gate": "RDA-TG-KTB",
                  "crops": {"best": "crops/c-1-best.jpg"}}, up
    assert for_upload({"id": "q"}, "G")["crops"] == {}, "a crop-less event still uploads"
    lock_check()
    publish_check()
    suggest_check()
    print("selfloop self-check ok: cameras found under any gate prefix, newest unsampled segments "
          "picked per camera, training triggers on the cumulative threshold, promotion needs a "
          "strictly better reference score (detector) or mean val accuracy (attributes) unless "
          "there is no comparable champion, newest segments classified first under their "
          "gate's id")


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        selfcheck()
    elif a[0] == "ingest":
        ingest_pass()
    elif a[0] == "classify":
        classify_pass()
    elif a[0] == "hunt":
        hunt_pass()
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
