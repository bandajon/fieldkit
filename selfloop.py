#!/usr/bin/env python3
"""The self-improving loop, for the always-on training machine.

  python selfloop.py ingest        newest mirrored segments, every camera -> pending -> bucket
  python selfloop.py classify      drains unclassified segments of the last CLASSIFY_HOURS,
                                   oldest first -> vehicle events -> bucket
  python selfloop.py hunt          last HUNT_HOURS of footage, newest first -> only frames of
                                   wanted classes -> the queue
  python selfloop.py train         pull the curated set; train once enough is new; promote if better
  python selfloop.py train --now   train now, threshold or not
  python selfloop.py status        where the loop stands
  python selfloop.py audit [YYYYMMDD]   duplicate suspects among yesterday's (Lusaka) journeys -> bucket
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
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
DATASET = ROOT / "dataset"
R2_CACHE = DATASET / "r2-cache"   # manifest bodies by key+ETag: a classify batch re-reads its day and
                                  # neighbours (~10 min over the office uplink), but only new segments changed
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
COVERAGE = "fieldkit-coverage/"   # per-gate per-day manifests: recorded vs classified stems
TRACKLETS = "fieldkit-tracklets/"   # every track, counted or not: the journey builder's input
# One line per vehicle across a camera pair — beside EVENTS, not instead of it, until the
# counts are compared.
JOURNEYS = "fieldkit-journeys/"
HEALTH = "fieldkit-health/"   # per-gate health JSON: backlog, per-camera lag, last-24h alerts
AUDIT_STATE = DATASET / "dup-audit.json"   # dup_audit summaries by gate: never state.json (see audit_pass)
ANNOTATED = "fieldkit-annotated/"   # DeepStream-style rendered clips, for the RDA dashboard's live view
LIVE_HOURS = 6         # only this recent a segment is worth rendering — a backlog must
                       # never slow classification down waiting on video encodes
ANNOTATED_KEEP_DAYS = 3
PLATE = DATASET / "plate.pt"  # optional plate model: evidence crops only, never a count
# The Katuba box still calls itself site1; anything already named RDA-TG-* is its own id.
GATES = {"site1": "RDA-TG-KTB"}
TZ = "Africa/Lusaka"          # the gates and this machine; segment names are local wallclock
WANT_BOXES = 300      # per-class floor for the next run; under it, a class is hunted
HUNT_TOP = 6          # ...but only the thinnest few at once: with most classes under the
                      # floor a hunt kept every frame with a vehicle in it (400 per segment)
CLASSIFY_PER_PASS = 12        # ~10 min of footage per camera per pass, at 600 s segments
CLASSIFY_HOURS = 48           # what the gates keep mirrored: one backlog may span this;
                              # older gaps are a backfill job, not this pass's problem
CLASSIFY_BUDGET = 3 * 3600    # seconds one classify pass drains for before checkpointing
NIGHT_LEFT = 24               # segments; a backlog this small drains in one classify pass after a long train — a bigger one keeps the yield, so a night train can't age footage past CLASSIFY_HOURS
NIGHT = range(0, 5)           # Lusaka hours the gates are near-empty: scheduled train may take the GPU despite a classify backlog
JOURNEYS_EVERY_S = 1200       # journeys_pass also runs mid-pass at this cadence: the RDA
                              # dashboard should lag by tens of minutes, not by up to a
                              # whole CLASSIFY_BUDGET waiting for the pass to finish.
                              # (state saved, lock released) — ingest (and train, outside
                              # NIGHT) wait out a backlog via classify_behind(); this just
                              # bounds how long one pass can hold the lock. train --now
                              # overrides the wait.
HUNT_HOURS = 48               # how far back a hunt looks: what the gates keep mirrored
HUNT_PER_PASS = 30            # segments per pass, ~25 s each at 1 fps on the GPU: the
                              # lock is back within classify's patience (WAIT)
REMEMBER_CLASSIFIED = 20000


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


ALERTS = []   # this pass's operational warnings; classify_pass folds them into state + R2


def alert(kind, gate, detail):
    """A warning that used to be a bare print only the office Mac's log showed."""
    print(detail, flush=True)
    ALERTS.append({"at": now(), "kind": kind, "gate": gate, "detail": detail})


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


def camera_lag(keys, classified, since=""):
    """{gate: {cam: (newest recorded start, newest classified start | None, oldest unclassified
    start since `since` | None)}} as epochs — how far classify trails each camera's recorder.
    The newest-vs-newest gap reads ~0 under the live lane with an old hole behind it; the
    oldest unclassified start shows that hole. Unparseable keys are skipped."""
    import ingest_video
    done, out = set(classified), {}
    for pc, segs in cameras(keys).items():
        prefix, cam = pc.split("/")
        slot = out.setdefault(gate_of(prefix), {}).setdefault(cam, [None, None, None])   # two prefixes, one gate
        for k in segs:
            try:
                t = ingest_video.cam_and_start(Path(k))[1]
            except Exception:
                continue
            slot[0] = max(slot[0] or t, t)
            if k in done:
                slot[1] = max(slot[1] or t, t)
            elif Path(k).stem >= since:
                slot[2] = min(slot[2] or t, t)
    return {g: {c: tuple(v) for c, v in cams.items() if v[0] is not None} for g, cams in out.items()}


def fold_alerts(s, fresh, cutoff=None):
    """s["alerts"] += fresh, minus anything older than 24 h, identical kind+gate+detail
    collapsed to the newest; newest first. Malformed entries (state is hand-editable) are dropped."""
    from datetime import timedelta
    cutoff = cutoff or (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    newest = {}
    for a in s.get("alerts", []) + fresh:
        if not isinstance(a, dict) or not {"at", "kind", "gate", "detail"} <= a.keys():
            continue
        k = (a["kind"], a["gate"], a["detail"])
        if a["at"] >= cutoff and a["at"] >= newest.get(k, a)["at"]:
            newest[k] = a
    s["alerts"] = sorted(newest.values(), key=lambda a: a["at"], reverse=True)


def audit_summary(gate):
    """The latest dup_audit summary for gate (audit_pass writes AUDIT_STATE), or None."""
    try:
        return json.loads(AUDIT_STATE.read_text()).get(gate)
    except (OSError, ValueError):
        return None


def publish_health(s, cl, bucket, keys, since=""):
    """One small JSON per gate for the RDA dashboard. Never fails the pass."""
    import time
    iso = lambda t: t and datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        lag = camera_lag(keys, s.get("classified", []), since)
    except Exception as e:
        print(f"  ! health: {e}", flush=True)
        return
    for gate, cams in lag.items():
        try:
            doc = {"gate": gate, "updated": now(), "backlog": s.get("classify_backlog"),
                   "backlog_scope": "loop",   # the whole classify window, not this gate's share
                   "last_classify": s.get("last_classify"),
                   "lag_s": {c: r - c_ if c_ is not None else None for c, (r, c_, _) in cams.items()},
                   "oldest_unclassified_s": {c: round(time.time() - o) if o is not None else None
                                             for c, (_, _, o) in cams.items()},
                   "newest": {c: {"recorded": iso(r), "classified": iso(c_)} for c, (r, c_, _) in cams.items()},
                   "alerts": [a for a in s.get("alerts", []) if a["gate"] == gate][:50],
                   "duplicate_audit": audit_summary(gate)}
            cl.put_object(Bucket=bucket, Key=f"{HEALTH}{gate}.json", Body=json.dumps(doc).encode(),
                          ContentType="application/json")
        except Exception as e:
            print(f"  ! health {gate}: {e}", flush=True)


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


def promote(candidate, champion):
    """Does this run earn the champion slot? Both are scored on the run's val split —
    new curations neither trained on (the run continued from the champion, and the
    champion's own training frames are the frozen reference set), so the numbers are
    comparable. No champion is beaten by anything; otherwise strictly better mAP50."""
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


def wanted_classes(counts, target=WANT_BOXES, top=HUNT_TOP):
    """The thinnest classes still under the floor — what the sampling passes capture
    off-cadence. Counted in NEW boxes only: the frozen reference frames train nothing,
    so they say nothing about where the next run is thin."""
    under = sorted((c, n) for n, c in counts.items() if c < target)
    return sorted(n for _, n in under[:top])


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


def classify_behind(s=None):
    """True if classify still has a fresh backlog — checked by the other passes so
    counting beats sampling: footage waiting to be classified matters more than another
    round of curation sampling or a training run that can wait an hour."""
    from datetime import timedelta
    backlog = (s if s is not None else load_state()).get("classify_backlog")
    if not backlog or not backlog.get("left"):
        return False
    at = datetime.strptime(backlog["at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - at < timedelta(hours=2)


def paused(keys, cfg):
    """Drop keys whose gate is in config.yaml's curation_paused — matched against both
    the bucket prefix and its gate id (RDA-TG-KTB and site1 name the same box), the
    owner's off switch for a gate that has plenty of data."""
    off = set(cfg.get("curation_paused") or [])
    live = lambda p: p not in off and gate_of(p) not in off
    return [k for k in keys if live(k.split("/", 1)[0])]


def curation_settings(cl, bucket):
    """The curation server's curation/curation.yaml as {"cap": int|None, "paused": [gate
    ids]} — same shape as curation_cap.load_settings, fetched fresh each pass since that
    server owns the file. Missing key or any error is the safe default: no cap, nothing
    paused."""
    import tempfile
    import curation_cap
    try:
        body = cl.get_object(Bucket=bucket, Key=f"curation/{curation_cap.SETTINGS}")["Body"].read()
    except Exception as e:
        print(f"{now()} curation settings: unavailable ({e}) — no cap, nothing paused", flush=True)
        return {"cap": None, "paused": []}
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / curation_cap.SETTINGS).write_bytes(body)
        return curation_cap.load_settings(tmp)


def prune_pending(cl, bucket, cap):
    """Delete local pending/{images,labels,attrs,suggest} samples that fall in the cap's
    eviction set, before this pass pushes. Ranked over LOCAL stems union the BUCKET's
    pending stems, not local alone — a sample the bucket already holds past cap for its
    gate is older than everything local, and ranking local stems by themselves would
    call it safe and push a copy right back. -> (samples pruned, ok to push).

    A failed bucket listing prunes nothing and tells the caller to skip its push this
    pass too: pushing unpruned against a stale (empty) view would resurrect exactly what
    this exists to stop."""
    import curation_cap
    if cap is None:
        return 0, True
    local = {p.stem for p in (DATASET / "pending" / "images").glob("*.jpg")}
    try:
        remote = {Path(k).stem for k in bucket_keys(cl, bucket, "curation/pending/images/")}
    except Exception as e:
        print(f"{now()} curation prune: bucket listing failed ({e}) — push skipped this pass",
              flush=True)
        return 0, False
    import dataset_sync
    # Finished (approved/discarded) originals still sit in the bucket's pending/: the server
    # leaves them out of its count, so this must too, or it prunes new, never-pushed samples.
    done = dataset_sync.consumed(DATASET)
    evicted = {sid for sids in curation_cap.evictions((local | remote) - done, cap).values() for sid in sids}
    pruned = 0
    for sid in local & evicted:
        for part in curation_cap.PARTS:
            (DATASET / "pending" / part / f"{sid}.{curation_cap.EXT[part]}").unlink(missing_ok=True)
        pruned += 1
    return pruned, True


def gate_of(prefix):
    """The RDA gate id for a bucket prefix. The importer resolves events by gate, so a
    prefix with no mapping is passed through — a gate already named for itself is right,
    and a genuinely unknown one is better filed visibly wrong than silently dropped."""
    return GATES.get(prefix, prefix)


def pick_classify(keys, classified, since, per_pass=CLASSIFY_PER_PASS):
    """Unclassified segments recorded after `since` (a YYYYMMDD-HHMMSS stem, local
    wallclock), OLDEST first across every camera: newest-first left permanent holes —
    hours recorded while another pass held the lock were never the newest and so were
    never picked, measured at 33-38% coverage. Ordered by (stem, key) so a camera pair's
    two segments of the same ten minutes go together."""
    done = set(classified)
    todo = [k for segs in cameras(keys).values() for k in segs
            if k not in done and Path(k).stem >= since]
    todo.sort(key=lambda k: (Path(k).stem, k))
    return todo if per_pass is None else todo[:per_pass]


def classify_since(now_dt, cfg, floor=""):
    from datetime import timedelta
    default = (now_dt - timedelta(hours=CLASSIFY_HOURS)).strftime("%Y%m%d-%H%M%S")
    # Retain the highest cutoff so abandoned or frozen segments aren't revisited.
    default = max(default, floor)
    if "classify_from" not in cfg:
        return default
    configured = cfg["classify_from"]
    try:
        parsed = datetime.strptime(configured, "%Y%m%d-%H%M%S")
        if parsed.strftime("%Y%m%d-%H%M%S") != configured:
            raise ValueError
    except (TypeError, ValueError):
        print("WARNING config classify_from must be YYYYMMDD-HHMMSS; ignoring it", flush=True)
        return default
    if parsed.replace(tzinfo=now_dt.tzinfo) > now_dt:
        print("WARNING config classify_from is in the future; ignoring it", flush=True)
        return default
    return max(default, configured)


def live_lane(keys, classified, since, now, failed=(), limit=2):
    """Keep up to two live clips available while oldest-first classification drains a backlog."""
    import ingest_video
    excluded = set(classified) | set(failed)
    per_camera = []
    for segs in cameras(keys).values():
        eligible = []
        for key in reversed(segs):
            if key in excluded or Path(key).stem < since:
                continue
            try:
                start = ingest_video.cam_and_start(Path(key))[1]
            except (OSError, ValueError, OverflowError):
                continue
            if 0 <= now - start < LIVE_HOURS * 3600 - 1800:
                eligible.append(key)
        if eligible:
            per_camera.append(eligible)
    per_camera.sort(key=lambda segs: (Path(segs[0]).stem, segs[0]), reverse=True)
    return list(islice((segs[i] for i in range(max(map(len, per_camera), default=0))
                        for segs in per_camera if i < len(segs)), limit))


def coverage(keys, classified, since):
    """{"<hour YYYYMMDD-HH>": {"<cam>": (classified, recorded)}} over the window — what
    status() prints: classify's actual reach, not just whether the last pass emptied
    its queue."""
    done = set(classified)
    out = {}
    for cam, segs in cameras(keys).items():
        for k in segs:
            stem = Path(k).stem
            if stem < since:
                continue
            slot = out.setdefault(stem[:11], {}).setdefault(cam, [0, 0])
            slot[1] += 1
            slot[0] += k in done
    return {h: {c: tuple(v) for c, v in cams.items()} for h, cams in out.items()}


def coverage_manifests(keys, classified, since):
    """{(gate, day): {cam: {"recorded": [stems], "classified": [stems]}}} — the per-day
    truth published to COVERAGE, so a dashboard never treats a partial day as whole.

    Which days to write is decided by `since` (a day touched anywhere after the cutoff
    is due a fresh manifest); each day written is filled from ALL its keys, cutoff or
    not — otherwise the oldest day in the window shrinks to the one segment past `since`
    and reads as fully covered."""
    done = set(classified)
    by_gate_cam = {}
    for key, segs in cameras(keys).items():
        prefix, cam = key.split("/")
        by_gate_cam.setdefault(gate_of(prefix), {}).setdefault(cam, []).extend(segs)   # one gate, two prefixes
    days = {(gate_of(key.split("/", 1)[0]), Path(k).stem[:8])
            for key, segs in cameras(keys).items() for k in segs if Path(k).stem >= since}
    out = {}
    for gate, day in days:
        for cam, segs in by_gate_cam[gate].items():
            entry = out.setdefault((gate, day), {}).setdefault(cam, {"recorded": [], "classified": []})
            for k in segs:
                stem = Path(k).stem
                if stem[:8] != day:
                    continue
                entry["recorded"].append(stem)
                if k in done:
                    entry["classified"].append(stem)
    return out


DEAD_AFTER_S = 24 * 3600  # a handoff camera silent this much longer than its gate's newest
                          # segment (any camera) is dead, not just behind: stop blocking on it.
                          # 24h, not 6h: Katuba upload lag has exceeded 6h on its own; open
                          # days live 7 days, so late-but-arriving footage still has time to
                          # block before it would otherwise be silently dropped.
GAP_S = 660               # a gap this long between a camera's consecutive segment starts is
                          # a real outage, not jitter — segments target 600s but aren't aligned.


def horizon(keys, classified, gate, day, since, cam_cfg, now=None):
    """{cam: epoch} for every camera in `cam_cfg` with a `handoff` entry — the moment past
    which that camera can no longer place a tracklet inside `day`'s handoff window, so a
    journey whose latest member is safely behind every camera's horizon will never gain a
    new link. A non-handoff camera, or a second bucket prefix mapping to the same gate+camera
    (e.g. site1 and its RDA-TG-* alias), is merged into the one camera it names — never lets
    one prefix's segments overwrite the other's.

    Segment starts are ingest_video.cam_and_start's clock — the recorder's own filename,
    parsed in the HOST's local tz (classify_pass logs a warning at startup if that isn't
    Lusaka's +02:00) — the same clock detect.py stamps tracklets with; segments are not
    clock-aligned (a reconnect cuts one short), so "last classified segment + 600" is wrong.
    `day_start` is Lusaka midnight specifically — detect.py files a tracklet by ZoneInfo(TZ)
    date regardless of the host's own tz, so the window boundary must use the same clock.

    A camera whose newest segment (any day) is DEAD_AFTER_S or more behind the gate's own
    newest is dead: +inf, unconditionally — checked before anything else, so a camera that
    goes silent mid-day (not just one that was never caught up) still stops blocking once
    it's clearly not coming back soon. Otherwise, a camera with no segment starting at
    day_start - 600s or later is not yet known to be caught up (footage may simply not be
    uploaded yet): horizon -inf, blocking the day. Otherwise horizon is the earliest of: the
    first still-unclassified relevant segment's
    start, or a > GAP_S gap between two relevant segments' starts (AT THE START OF THE
    SEGMENT BEFORE THE GAP — a reconnect can cut that segment short, so the missing footage
    may begin well before its nominal end) if the segment after it is itself less than
    DEAD_AFTER_S old as of `now` (a late upload may yet fill an older gap, which is a real
    outage instead and doesn't block). If nothing blocks, horizon
    is the start of that camera's single latest recorded segment (any day) — its true end is
    unknown, so nothing can be assumed settled past where it begins. A segment older than
    `since` is abandoned: it will never be classified, and is ignored rather than pinning the
    horizon forever. -> (horizon, abandoned count)."""
    import time as time_mod
    import ingest_video
    now = time_mod.time() if now is None else now
    handoff = {c["name"] for c in cam_cfg if c.get("handoff")}
    done, abandoned = set(classified), 0
    day_start = datetime.strptime(day, "%Y%m%d").replace(tzinfo=ZoneInfo(TZ)).timestamp()
    by_cam = {}
    for key_prefix, segs in cameras(keys).items():
        prefix, cam = key_prefix.split("/")
        if gate_of(prefix) == gate and cam in handoff:
            by_cam.setdefault(cam, []).extend(segs)   # merge, never overwrite
    starts = {}
    for segs in by_cam.values():
        for k in segs:
            try:
                starts[k] = ingest_video.cam_and_start(Path(k))[1]
            except Exception as e:     # not a real local file here (a bucket key) — mtime
                print(f"  ! horizon: can't read {k}'s start ({e}) — skipping it", flush=True)
    by_cam = {cam: [k for k in segs if k in starts] for cam, segs in by_cam.items()}
    newest = {cam: max((starts[k] for k in segs), default=float("-inf")) for cam, segs in by_cam.items()}
    gate_newest = max(newest.values(), default=float("-inf"))
    out = {}
    for cam in handoff:
        segs = by_cam.get(cam, [])
        dead = gate_newest - newest.get(cam, float("-inf")) >= DEAD_AFTER_S
        if dead:
            alert("dead_camera", gate, f"  camera {cam} silent since way behind the gate: not blocking")
            out[cam] = float("inf")
            continue
        relevant = sorted((k for k in segs if starts[k] >= day_start - 600), key=starts.get)
        if not relevant:
            out[cam] = float("-inf")     # not dead — just not yet known to be caught up
            continue
        # Segments before since are abandoned and do not block freezing.
        live = [k for k in relevant if k not in done and Path(k).stem >= since]
        abandoned += sum(1 for k in relevant if k not in done and k not in live)
        gap = min((starts[a] for a, b in zip(relevant, relevant[1:])
                   if starts[b] - starts[a] > GAP_S and now - starts[b] < DEAD_AFTER_S),
                  default=None)
        candidates = [starts[k] for k in live] + ([gap] if gap is not None else [])
        out[cam] = min(candidates) if candidates else max(starts[k] for k in segs)
    return out, abandoned


def prune_open_days(candidates, still_open, today):
    """Which (gate, day) pairs classify_pass should keep asking journeys_pass to revisit —
    a day's tail journeys (or one held back by its partner camera lagging) only clear
    FINAL_MARGIN once a LATER day's segments are classified, and it's that later day which
    gets touched, not this one, so without tracking it a day would be rebuilt once and never
    revisited to freeze its tail. Dropped once `journeys_pass` reports it has nothing left
    provisional, or once it turns 7 days old — but a day dropped at 7 days with provisional
    journeys still open is lost counts, and that must be loud, not a silent forget."""
    kept = set()
    for gate, day in candidates:
        if (gate, day) not in still_open:
            continue
        age = (today - datetime.strptime(day, "%Y-%m-%d").date()).days
        if age <= 7:
            kept.add((gate, day))
        else:
            alert("lost_counts", gate, f"{now()} journeys: dropping ({gate}, {day}) after {age}d with "
                                       f"provisional journeys still open — LOST COUNTS")
    return kept


def for_upload(doc, gate, to="crops/"):
    """One event (or tracklet) as the importer reads it: stamped with the gate it came
    from, and its crop paths flattened — the bucket keeps one crops/ folder per day, not
    per day-inside-a-day like the local events dir does. `to` re-roots them: a journey
    cites its crops by whole bucket key."""
    up = {**doc, "gate": gate,
          "crops": {tag: to + Path(rel).name for tag, rel in (doc.get("crops") or {}).items()}}
    if doc.get("plate"):          # a tracklet's plate read; events carry none
        up["plate"] = {**doc["plate"], "crop": to + Path(doc["plate"]["crop"]).name}
    return up


# ---- the machinery ----

WAIT = 900            # seconds a pass waits for the lock before giving up on this tick


class Lock:
    """One thing at a time on this machine: ingest, hunt, classify and train all want the
    GPU, and two trains at once would fight over dataset/run. A pass that finds the lock
    taken waits a while rather than skipping its whole interval — the agents fire together
    at login, and an hourly check colliding with a ten-minute pass is routine.

    A kernel flock, not a pid file: the lock dies with its process, so there is no stale-
    holder takeover — and the takeover was the bug, three agents waking together each
    unlinked the other's freshly written lock and all three ran on the GPU at once."""
    def __init__(self, wait=None):
        self.wait = WAIT if wait is None else wait   # read at call time: tests patch WAIT

    def __enter__(self):
        import fcntl
        import time
        deadline = time.monotonic() + self.wait
        LOCK.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(LOCK, os.O_CREAT | os.O_RDWR)
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() > deadline:
                    os.close(self.fd)
                    sys.exit(f"busy: {LOCK.read_text().strip()}")
                time.sleep(15)
        os.ftruncate(self.fd, 0)
        os.write(self.fd, f"{os.getpid()} {sys.argv[1:]} since {now()}".encode())
        return self

    def __exit__(self, *_):
        os.ftruncate(self.fd, 0)     # status reads "free" from an empty file
        os.close(self.fd)            # ...and this releases the flock


def r2():
    import dataset_sync
    o = dataset_sync.creds()
    return dataset_sync, dataset_sync.client(o), o["bucket"]


def recording_keys(cl, bucket):
    """Keys under the gates' recording folders only (site1/, RDA-TG-*/). The bucket is ~900k
    objects, nearly all curation samples and evidence crops: listing it whole cost 15-20 min
    of every pass and grew by the day. The top level is one Delimiter page."""
    tops = [p["Prefix"] for page in cl.get_paginator("list_objects_v2").paginate(Bucket=bucket, Delimiter="/")
            for p in page.get("CommonPrefixes", [])]
    for top in tops:
        if not top.startswith(SKIP + ("curation-", "fieldkit-", "classifier-crops-")):
            yield from bucket_keys(cl, bucket, top)


def bucket_keys(cl, bucket, prefix="", delimiter=None):
    # A delimiter lists one level: the day's manifests without its thousands of crops.
    by = {"Delimiter": delimiter} if delimiter else {}
    for page in cl.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix, **by):
        for obj in page.get("Contents", []):
            yield obj["Key"]


def r2_jsonl(cl, bucket, prefix):
    """Parsed docs of every .jsonl one level under prefix, keys sorted; bodies cached on disk by (key, ETag)."""
    import concurrent.futures
    objs = sorted((o for p in cl.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix,
                                                                           Delimiter="/")
                   for o in p.get("Contents", []) if o["Key"].endswith(".jsonl")), key=lambda o: o["Key"])
    try:
        R2_CACHE.mkdir(parents=True, exist_ok=True)
        for f in R2_CACHE.iterdir():
            if f.stat().st_mtime < time.time() - 3 * 86400:
                f.unlink()
    except OSError:
        pass

    def name(key, etag):
        return etag and R2_CACHE / f"{hashlib.sha256(key.encode()).hexdigest()[:32]}-{etag.strip(chr(34))}.jsonl"

    def body(o):
        f = name(o["Key"], o.get("ETag"))
        try:
            # A cut power can leave a short file under the final name: frozen batches read short
            # would refreeze journeys, so the listed size must match or it is fetched again.
            if f and o.get("Size", f.stat().st_size) == f.stat().st_size:
                os.utime(f)
                return f.read_bytes()
        except OSError:
            pass
        r = cl.get_object(Bucket=bucket, Key=o["Key"])
        b = r["Body"].read()
        f = name(o["Key"], r.get("ETag") or o.get("ETag"))   # what was fetched, if rewritten since the list
        try:
            if f:
                tmp = f.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
                tmp.write_bytes(b)
                os.replace(tmp, f)
        except OSError:
            pass
        return b
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        return [json.loads(l) for b in pool.map(body, objs) for l in b.decode().splitlines() if l.strip()]


def audit_pass(day8=None):
    """Daily: flag probable double counts among a day's frozen journeys (dup_audit.py). By default
    yesterday and the day before: a day still open (classify behind, journeys not all frozen) is
    marked partial, and the next morning's run audits it again once it has closed."""
    import dup_audit
    from datetime import timedelta
    today = datetime.now(ZoneInfo(TZ))
    days = [day8] if day8 else [(today - timedelta(days=k)).strftime("%Y%m%d") for k in (2, 1)]
    ds, cl, bucket = r2()
    for gate in GATES.values():
        for d8 in days:
            try:
                summary = dup_audit.run(cl, bucket, gate, d8)   # reads take minutes: no lock held
            except Exception as e:
                print(f"  ! audit {gate} {d8}: {e}", flush=True)
                continue
            summary["partial"] = [gate, f"{d8[:4]}-{d8[4:6]}-{d8[6:]}"] in load_state().get("open_days", []) \
                or not summary["journeys"]
            try:   # its own file, not state.json: classify holds loop.lock for hours and rewrites state
                try:
                    done = json.loads(AUDIT_STATE.read_text())
                    done = done if isinstance(done, dict) else {}
                except (OSError, ValueError):
                    done = {}                                       # missing or damaged: start over
                done[gate] = summary                                # the last day audited: yesterday
                tmp = AUDIT_STATE.with_suffix(f".{os.getpid()}.tmp")
                tmp.write_text(json.dumps(done))
                os.replace(tmp, AUDIT_STATE)
            except (OSError, ValueError) as e:
                print(f"  ! audit summary: {e}", flush=True)
            print(f"audit {gate} {d8}: {summary['suspects']} suspects in {summary['journeys']} journeys "
                  f"{summary['by_kind']}{' (partial: day still open)' if summary['partial'] else ''}", flush=True)


def new_frames():
    """Approved frames outside the reference set — what the next run would train on."""
    import train
    ref = train.reference()
    return sum(1 for p in (DATASET / "approved" / "labels").glob("*.txt") if p.stem not in ref)


def retention_policy():
    import dataset_retention
    try:
        return dataset_retention.cached_policy(DATASET)
    except ValueError:
        raise


def _current_source_ids():
    labels = {p.stem for p in (DATASET / "approved" / "labels").glob("*.txt")}
    images = {p.stem for p in (DATASET / "approved" / "images").glob("*.jpg")}
    return labels & images


def retention_training_ids():
    """Return newly observed detector and usable attribute source IDs.

    Archive manifests are verified by classifier_crops; a malformed archive therefore
    aborts the pass instead of silently changing the training corpus.
    """
    import train
    import train_attrs
    from classifier_crops import verified_archive
    import dataset_retention

    refs = set(train.reference())
    policy = dataset_retention.cached_policy(DATASET)
    live = lambda sid: not policy or not dataset_retention.expired(sid, policy)
    detector = {sid for sid in _current_source_ids() - refs if live(sid)}
    current = _current_source_ids()
    current_presence = {p.stem for kind in ("images", "labels", "attrs")
                        for p in (DATASET / "approved" / kind).glob("*") if p.is_file()}
    try:
        heads = train_attrs.vocab()
    except SystemExit:
        heads = {}
    attrs = set()
    archive_manifests = []
    for js in (DATASET / "approved" / "attrs").glob("*.json"):
        lbl = DATASET / "approved" / "labels" / f"{js.stem}.txt"
        if js.stem in refs or js.stem not in current or not live(js.stem) or not lbl.is_file():
            continue
        try:
            values = json.loads(js.read_text())
            boxes = [line.split() for line in lbl.read_text().splitlines() if line.split()]
            if any(str(i).lstrip("-").isdigit() and 0 <= int(i) < len(boxes)
                   and isinstance(a, dict)
                   and any(a.get(h) in vals for h, vals in heads.items())
                   for i, a in values.items()):
                attrs.add(js.stem)
        except (OSError, ValueError, TypeError):
            continue
    archive = DATASET / "classifier-crops"
    if archive.is_symlink():
        raise ValueError(f"archive root is symlink: {archive}")
    if archive.is_dir():
        for d in sorted(archive.iterdir()):
            if d.is_symlink() or not d.is_dir():
                raise ValueError(f"invalid archive entry: {d}")
            m = verified_archive(DATASET, d.name)
            archive_manifests.append((d, m))
            if m["reference"]:
                refs.add(d.name)
    for d, m in archive_manifests:
        if d.name in current_presence or d.name in refs:
            continue
        if any(any(s["attrs"].get(h) in vals for h, vals in heads.items()) for s in m["samples"]):
            attrs.add(d.name)
    attrs -= refs
    return detector, attrs


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
    try:
        names = (DATASET / "classes.txt").read_text().split()
    except OSError:
        names = []
    out = DATASET / "pending" / "suggest"
    out.mkdir(parents=True, exist_ok=True)
    import dataset_retention
    written = 0
    for stem in stems:
        js = out / f"{stem}.json"
        if js.exists():        # suggested already, or a re-ingest of the same stem
            continue
        try:
            with dataset_retention.DATASET_LOCK:
                policy = dataset_retention.cached_policy(DATASET)
                if policy and dataset_retention.expired(stem, policy):
                    continue
            img = Image.open(DATASET / "pending" / "images" / f"{stem}.jpg").convert("RGB")
            lines = (DATASET / "pending" / "labels" / f"{stem}.txt").read_text().splitlines()
            got = {}
            # The key is the box's index as app.py counts them, which is its position
            # among the well-formed lines — skipping a short row here and keeping it
            # there would shift every suggestion after it onto the wrong vehicle.
            for i, box in enumerate(b for b in map(str.split, lines) if len(b) == 5):
                crop = train_attrs.crop_box(img, box[1:5])   # same padding the heads trained on
                if crop is not None:
                    # The class keeps the heads honest: no three-axle motorcycles suggested.
                    cls = names[int(box[0])] if box[0].isdigit() and int(box[0]) < len(names) else None
                    got[str(i)] = classify(crop, cls)
            if got:
                with dataset_retention.DATASET_LOCK:
                    policy = dataset_retention.cached_policy(DATASET)
                    if policy and dataset_retention.expired(stem, policy):
                        continue
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
    if classify_behind():
        print(f"{now()} ingest: yielding — classify has segment(s) to do", flush=True)
        return
    with Lock():
        s = load_state()
        ds, cl, bucket = r2()
        cfg = ingest_video.config()
        settings = curation_settings(cl, bucket)
        # config.yaml's own switch and the curation server's Pause button, either stops a
        # gate — paused() already matches a bucket prefix or its gate id.
        cfg["curation_paused"] = sorted(set(cfg.get("curation_paused") or []) | set(settings["paused"]))
        raw = list(recording_keys(cl, bucket))
        keys = paused(raw, cfg)
        if raw and not keys:      # empty because paused, not because nothing was recorded
            print(f"{now()} ingest: curation paused for {', '.join(cfg['curation_paused'])}",
                  flush=True)
            return
        todo = pick(keys, s["ingested"])
        print(f"{now()} ingest: {len(todo)} new segment(s) across the cameras", flush=True)
        if not todo:
            return
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
        pruned, ok = prune_pending(cl, bucket, settings["cap"])
        if pruned:
            print(f"{now()} ingest: pruned {pruned} local pending sample(s) over cap", flush=True)
        if not ok:
            return
        sent, _ = ds.push(cl, bucket, names=PENDING)
        print(f"{now()} ingest: {s['last_ingest']['samples']} samples written, {sent} files pushed", flush=True)


def miss_moments(docs, cameras):
    """{partner camera: sorted epochs} where a lone counted journey's vehicle should also
    have been seen: MISS_PAD before its first sighting and after its last. Heavy trucks are
    skipped: a lone one is mostly a queue-matching gap, not a detector miss."""
    import detect
    from journeys import HEAVY
    partner = {c["name"]: c["handoff"]["camera"] for c in cameras if c.get("handoff")}
    out = {}
    for d in docs:
        p = partner.get(d["camera"])
        if "+" in d["camera"] or d.get("evidence") != "line" or not d.get("direction") or not p \
                or d.get("class") in HEAVY:
            continue
        out.setdefault(p, []).extend((min(m["t0"] for m in d["members"]) - detect.MISS_PAD,
                                      max(m["t1"] for m in d["members"]) + detect.MISS_PAD))
    return {c: sorted(v) for c, v in out.items()}


def gate_miss_moments(cl, bucket, gate, files, cameras):
    """miss_moments over the journeys of every day `files` start in; {} (and a note) on any failure."""
    try:
        docs = []
        for day in sorted({f.stem[:8] for f in files}):
            try:
                body = cl.get_object(Bucket=bucket, Key=f"{JOURNEYS}{gate}/{day}/journeys.jsonl")["Body"].read()
            except cl.exceptions.NoSuchKey:
                continue
            docs += [json.loads(ln) for ln in body.decode().splitlines() if ln.strip()]
        return miss_moments(docs, cameras)
    except Exception as e:
        print(f"  ! miss hunt: {e}", flush=True)
        return {}


def hunt_pass():
    """Frames of the classes the curated set is short of, from the last HUNT_HOURS.

    The sampling passes see a slice of the footage and the classify pass sees all of it
    but slowly (it tracks every frame). This decodes at the ingest rate and keeps nothing
    but wanted-class frames, so two days of footage are swept in hours and the queue's
    bus, plant and abnormal-load frames are days old at most, not a week.
    """
    import detect
    import ingest_video
    from datetime import timedelta
    if not night_slot() and classify_behind():
        print(f"{now()} hunt: yielding — classify has segment(s) to do", flush=True)
        return
    with Lock():
        s = load_state()
        wanted = hunting()
        if not wanted and not detect.CONGESTED_BOXES:
            return
        ds, cl, bucket = r2()
        cfg = ingest_video.config()
        settings = curation_settings(cl, bucket)
        cfg["curation_paused"] = sorted(set(cfg.get("curation_paused") or []) | set(settings["paused"]))
        since = (datetime.now(ZoneInfo(TZ)) - timedelta(hours=HUNT_HOURS)).strftime("%Y%m%d-%H%M%S")
        raw = list(recording_keys(cl, bucket))
        keys = paused(raw, cfg)
        if raw and not keys:      # empty because paused, not because nothing was recorded
            print(f"{now()} hunt: curation paused for {', '.join(cfg['curation_paused'])}",
                  flush=True)
            return
        todo = pick_hunt(keys, s.get("hunted", []), since)
        print(f"{now()} hunt: {len(todo)} segment(s) since {since} to sweep", flush=True)
        if not todo:
            return
        if CHAMPION.is_file():
            cfg["detect_weights"] = str(CHAMPION)
        cfg["capture_wanted"], cfg["capture_only_wanted"] = wanted, True
        cfg["capture_congested"] = detect.CONGESTED_BOXES
        files = []
        for key in todo:
            dest = local_path(key)
            dest.parent.mkdir(parents=True, exist_ok=True)
            cl.download_file(bucket, key, str(dest))
            files.append(dest)
        stems, written, dense, missed = [], 0, 0, 0
        try:
            by_gate = {}
            for f in files:
                by_gate.setdefault(gate_of(f.relative_to(VIDEOS).parts[0]), []).append(f)
            for gate, fs in by_gate.items():
                moments = gate_miss_moments(cl, bucket, gate, fs, cfg.get("cameras") or [])
                sink = ingest_video.ingest(fs, {**cfg, "toll_gate_id": gate, "capture_miss": moments})
                stems += sink.stems
                written += sum(sink.written.values())
                dense += sink.dense
                missed += sink.missed
        finally:
            for f in files:
                f.unlink(missing_ok=True)
        if ATTRS_CHAMPION.is_file():
            suggest(stems)
        s["hunted"] = (s.get("hunted", []) + todo)[-REMEMBER:]
        s["last_hunt"] = {"at": now(), "segments": len(todo), "samples": written, "congested": dense, "missed": missed,
                         "wanted": wanted}
        save_state(s)
        pruned, ok = prune_pending(cl, bucket, settings["cap"])
        if pruned:
            print(f"{now()} hunt: pruned {pruned} local pending sample(s) over cap", flush=True)
        if not ok:
            return
        sent, _ = ds.push(cl, bucket, names=PENDING)
        print(f"{now()} hunt: {written} samples written ({dense} congested, {missed} missed), {sent} files pushed", flush=True)


def published_manifests(cl, bucket, keys, since):
    """Which of `keys` (recording keys) already have an events manifest in R2 — the bucket,
    not local state, is the authority on "classified": local state is easier to lose than
    the bucket, and re-classifying an already-published segment mints new tracklet ids
    (detect.py's obs-<sha(gate,source_key)>-<ordinal>, where ordinal depends on that run's
    detection sequence) that a frozen final/ batch never recognises — re-freezing vehicles
    that were already counted.

    publish() names a segment's manifest after the segment's OWN key, filed under the day(s)
    its tracklets landed in — which can be the segment's own day or the next one if it
    straddles midnight — so both day prefixes are listed, once per pass."""
    from datetime import timedelta
    days = set()
    for k in keys:
        stem = Path(k).stem
        if stem < since:
            continue
        gate, day = gate_of(k.split("/")[0]), stem[:8]
        nxt = (datetime.strptime(day, "%Y%m%d") + timedelta(days=1)).strftime("%Y%m%d")
        days |= {(gate, day), (gate, nxt)}
    names = {k.rsplit("/", 1)[-1] for gate, day in days
             for k in bucket_keys(cl, bucket, f"{EVENTS}{gate}/{day}/", "/")}
    return {k for k in keys
            if f"{Path(k).stem}-{hashlib.sha256(k.encode()).hexdigest()[:32]}.jsonl" in names}


def publish(cl, bucket, out, gate, source_key, day):
    """Send one segment's events and crops to fieldkit-events/<gate>/<YYYYMMDD>/, the
    layout the RDA importer reads, and its tracklets and theirs to fieldkit-tracklets/
    the same way. -> events published."""
    # A quiet segment still publishes an empty file: that is what marks it done, and it
    # tells the importer the gate was watched and idle rather than never processed.
    manifest = f"{Path(source_key).stem}-{hashlib.sha256(source_key.encode()).hexdigest()[:32]}"
    (out / f"{day}.jsonl").touch()
    total = 0
    # Tracklets first, so a published events manifest implies its tracklets are there. A
    # segment can straddle midnight: two day files of each.
    for js in sorted((out / "tracklets").glob("*.jsonl")) + sorted(out.glob("*.jsonl")):
        tracklet = js.parent != out
        docs = [for_upload(json.loads(l), gate)
                for l in js.read_text().splitlines() if l.strip()]
        lines = [json.dumps(doc) for doc in docs]
        where = f"{TRACKLETS if tracklet else EVENTS}{gate}/{js.stem.replace('-', '')}"
        crops = sorted({Path(rel).name for doc in docs
                        for rel in [*doc["crops"].values(), (doc.get("plate") or {}).get("crop")] if rel})
        here = js.parent / "crops" / js.stem
        missing = [name for name in crops if not (here / name).is_file()]
        if missing:           # all checked before any upload: no manifest cites a crop that is not up
            raise FileNotFoundError(f"crop missing: {here / missing[0]}")
        # In parallel: each upload waits ~0.8 s on the office link and a day segment has 300-500
        # crops — one at a time, that was most of the segment's time in the pass.
        import dataset_sync
        dataset_sync.transfer(lambda name: cl.upload_file(str(here / name), bucket, f"{where}/crops/{name}"),
                              [(name,) for name in crops], "crops up")
        cl.put_object(Bucket=bucket, Key=f"{where}/{manifest}.jsonl",
                      Body="".join(l + "\n" for l in lines).encode(),
                      ContentType="application/json")
        total += 0 if tracklet else len(lines)
    return total


FINAL_MARGIN = 300    # seconds of slack a journey's latest member must clear a camera's
                      # horizon by before freezing it: covers LAG/STITCH_GAP in journeys.py
                      # plus a vehicle queueing up to ~5 min in the handoff zone.
PARKED_S = 1800       # ponytail: a vehicle sitting in a handoff zone longer than this is
                      # parked, not queueing — ignored by the watermark so it can't stall
                      # every OTHER journey's freeze; a real fix would count it separately.


def finalize(provisional, cutoff):
    """Which of the day's provisional journeys clear FINAL_MARGIN against `cutoff` (min
    horizon) AND against the time watermark `b`: the earliest start of any OTHER still-open
    journey of the day (excluding ones parked over PARKED_S, which mustn't hold the whole
    day hostage). Freezing journey-by-journey let an early greedy link decision go
    permanent: a long chain `d` queued in the zone past the cutoff steals lone `c`'s rightful
    partner `r`; `c` freezes on its own; `d` later grows enough to reclaim `r`, which then
    freezes too — one vehicle, two final journeys. The watermark holds `c` back for exactly
    as long as `d` (or anything else still open and unparked) could still rewrite it."""
    max_t1 = {j["id"]: max(m["t1"] for m in j["members"]) for j in provisional}
    min_t0 = {j["id"]: min(m["t0"] for m in j["members"]) for j in provisional}
    open_j = [j for j in provisional if max_t1[j["id"]] + FINAL_MARGIN > cutoff and j["dwell_s"] <= PARKED_S]
    b = min((min_t0[j["id"]] for j in open_j), default=float("inf"))
    watermark = min(cutoff, b)
    return [j for j in provisional if max_t1[j["id"]] + FINAL_MARGIN <= watermark]


def cleared(built, provisional, cutoff):
    """Which of `provisional` (the journeys owned by the day being rebuilt) may freeze,
    given EVERY journey `built` from the same tracklets (including ones owned by a
    neighbouring day, at the midnight boundary) — the watermark in finalize() must see all
    of them: a still-open journey `d` whose own ts lands on the neighbouring day can hold
    back a same-day `c` exactly as an open same-day journey would, or `c` freezes while `d`
    is still free to grow and steal `c`'s rightful partner."""
    ids = {j["id"] for j in finalize(built, cutoff)}
    return [j for j in provisional if j["id"] in ids]


def journeys_pass(cl, bucket, touched, cameras, tz, keys=(), classified=(), since="", classified_now=()):
    """Rebuild each touched (gate, day)'s fieldkit-journeys/<gate>/<YYYYMMDD>/journeys.jsonl
    from ALL its tracklets not already claimed by a frozen final/ batch, plus the WHOLE of
    the neighbouring days' (a vehicle can cross midnight, and a queued chain can span several
    minutes either side of it — a short sliver isn't enough), one line per vehicle across a
    camera pair, with attrs from the day's events. journeys.jsonl is informational only (status/
    eval) and gets overwritten every pass; exactly-once import comes from final/, an
    append-only set of batches, each written once and never touched again — a tracklet
    belongs to at most one final journey, ever, so re-linking it on a later pass (once its
    partner camera catches up) can never double-import it. Only cameras with `handoff`
    config pair up; without any there is nothing to link. -> (journeys written, still-open
    (gate, day) pairs — those with provisional journeys left for classify_pass to revisit;
    checked (gate, YYYY-MM-DD) days whose frozen/LATE FOOTAGE diagnostic completed).

    A provisional journey freezes only once BOTH: (a) its latest member clears FINAL_MARGIN
    against every camera `gate` owns (horizon()), not just the cameras it happens to touch,
    since an as-yet-uncaught-up partner could still hand it a new link, AND (b) it started
    before every other still-open journey built from the SAME tracklets (the time watermark
    in finalize()) — checked over every journey `journeys.build` produced here, not just the
    ones this day owns: an open chain `d` whose own ts happens to land on the neighbouring
    day (built from these same boundary tracklets) still has to hold a same-day `c` back, or
    `c` freezes while `d` is free to grow and steal `c`'s rightful partner — one vehicle, two
    final journeys, on two different days. A journey parked over PARKED_S is excluded from
    the watermark so it can't stall every other journey's freeze forever.
    Final batches are put BEFORE journeys.jsonl, so a reader racing the two puts sees either
    the old journeys.jsonl (safe: it doesn't yet know the batch happened) or the new one
    alongside it — never a batch with no record of what it froze.

    ponytail: the whole day's tracklets AND both neighbours' are re-read every pass — 3x the
    reads, cheap next to classify's own downloads. Crops are cited by bucket key, never
    copied."""
    if not any(c.get("handoff") for c in cameras):
        return 0, set(), set(touched)
    from datetime import timedelta
    import journeys

    _cache = {}          # a day's docs read once per journeys_pass call, not once per (day
                          # it's touched) x (day it's a neighbour of) — same day, same body.

    def docs(prefix):             # every manifest of the day; crops/ is never listed
        if prefix not in _cache:
            _cache[prefix] = r2_jsonl(cl, bucket, prefix)
        return iter(_cache[prefix])

    def frozen_ids(where):
        return {m["id"] for j in docs(f"{JOURNEYS}{where}final/") for m in j["members"]}

    def side(day8):                 # a day's raw tracklets, whole (a queued truck's chain
        w = f"{gate}/{day8}/"       # can span several minutes either side of midnight)
        return list(docs(TRACKLETS + w)), w

    total, still_open, checked_days = 0, set(), set()
    for gate, day in sorted(touched):
        try:
            yyyymmdd = day.replace("-", "")
            where = f"{gate}/{yyyymmdd}/"
            d = datetime.strptime(yyyymmdd, "%Y%m%d")
            prev8 = (d - timedelta(days=1)).strftime("%Y%m%d")
            next8 = (d + timedelta(days=1)).strftime("%Y%m%d")
            own_raw, _ = side(yyyymmdd)
            prev_raw, prev_w = side(prev8)
            next_raw, next_w = side(next8)
            # A vehicle's tracklet can be frozen under a NEIGHBOURING day's final/ batch (its
            # journey's ts landed there) even though the tracklet file itself lives under this
            # day — so the exclusion set is the union of all three days' frozen ids, applied to
            # all three days' tracklets alike, never each day's own final/ against only its own.
            fids = frozen_ids(where) | frozen_ids(prev_w) | frozen_ids(next_w)
            tracklets = [for_upload(t, gate, f"{TRACKLETS}{w}crops/")
                         for raw, w in ((own_raw, where), (prev_raw, prev_w), (next_raw, next_w))
                         for t in raw if t["id"] not in fids]
            events = {e["id"]: e for e in docs(EVENTS + where)}
            events.update({e["id"]: e for e in docs(EVENTS + prev_w)})
            events.update({e["id"]: e for e in docs(EVENTS + next_w)})
            # with_dropped: lone chains the direction/lone_edge filter removed are not journeys, but
            # as open chains they still hold the freeze watermark back (they can yet link to a partner).
            everything = journeys.build(tracklets, cameras, tz, events, with_dropped=True)
            built = [j for j in everything if not j.get("dropped")]
            # A boundary tracklet can pull in a journey that really belongs to the neighbouring
            # day (it's rebuilt there too, from its own side of the same boundary) — keep only
            # the ones whose own ts (Lusaka) lands on THIS day, or a vehicle crossing midnight
            # would be double-built rather than merely double-read.
            on_day = (lambda j: datetime.fromisoformat(j["ts"]).astimezone(ZoneInfo(TZ))
                      .strftime("%Y%m%d") == yyyymmdd)
            provisional = [j for j in built if on_day(j)]
            frozen = list(docs(f"{JOURNEYS}{where}final/"))
            if frozen:
                # Sanity check, not routine: everything classified BEFORE a freeze's cutoff
                # should already have been in it — a segment classified just now (this pass,
                # not ever) surfacing behind that point means footage arrived late, and the
                # journeys it belongs to may already have a final counterpart that never saw
                # it. Loud, since it's the exact shape of the double-count freezes prevent.
                import ingest_video as _iv
                frozen_ts = max(datetime.fromisoformat(j["ts"]).timestamp() for j in frozen)
                for k in classified_now:      # only what THIS pass classified — every OLD
                                               # classified segment is behind frozen_ts by
                                               # construction, and would false-alarm on every day
                    if (gate_of(k.split("/")[0]) == gate and Path(k).stem[:8] == yyyymmdd
                            and _iv.cam_and_start(Path(k))[1] < frozen_ts - FINAL_MARGIN):
                        alert("late_footage", gate, f"  ! LATE FOOTAGE — possible duplicate journeys: {k} "
                              f"classified behind {gate}/{yyyymmdd}'s latest frozen journey ts")

            checked_days.add((gate, day))
            h, abandoned = horizon(keys, classified, gate, yyyymmdd, since, cameras)
            # min(h.values()): a journey freezes only once EVERY camera the gate owns is past it
            # by FINAL_MARGIN — an uncaught-up partner could still hand a lone chain a new link.
            # An empty h (no cameras at all) or any -inf (a camera not yet known caught up) both
            # make this comparison fail for every journey, as they must.
            cutoff = min(h.values(), default=float("-inf"))
            # The watermark (finalize()'s `b`) must see EVERY built journey, not just the ones
            # owned by this day — an open chain `d` whose OWN ts lands on the neighbouring day
            # (built from the same boundary tracklets) still has to hold back a same-day `c`,
            # or `c` freezes while `d` is still free to grow and steal `c`'s rightful partner.
            # Only journeys actually owned by this day are ever candidates to freeze, though.
            newly_final = cleared(everything, provisional, cutoff)
            if newly_final:
                existing = set(bucket_keys(cl, bucket, f"{JOURNEYS}{where}final/"))
                ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                key, n = f"{JOURNEYS}{where}final/{ts}.jsonl", 2
                while key in existing:            # never overwrite an existing batch
                    key, n = f"{JOURNEYS}{where}final/{ts}-{n}.jsonl", n + 1
                cl.put_object(Bucket=bucket, Key=key,
                              Body="".join(json.dumps({**j, "gate": gate}) + "\n"
                                           for j in newly_final).encode(),
                              ContentType="application/json")

            js = frozen + provisional
            cl.put_object(Bucket=bucket, Key=f"{JOURNEYS}{where}journeys.jsonl",
                          Body="".join(json.dumps({**j, "gate": gate}) + "\n" for j in js).encode(),
                          ContentType="application/json")
            print(f"    {len(provisional)} journey(s), {len(newly_final)} final, "
                  f"{abandoned} abandoned segment(s) -> {JOURNEYS}{where}", flush=True)
            total += len(provisional)
            if len(provisional) > len(newly_final):
                still_open.add((gate, day))
        except Exception as e:    # one bad key or JSON line must not stop every other day
            print(f"  ! journeys {gate} {day}: {e}", flush=True)
            still_open.add((gate, day))     # leave it in open_days: retry next pass
    return total, still_open, checked_days


def classify_pass():
    """Vehicle events from the footage the gates mirror to the bucket.

    The live pipeline already turns frames into counted vehicles; this runs that same
    pipeline over the recordings instead of the wire, so a gate box that only records
    still produces events. Ten to twenty minutes behind live, which the portal is happy
    with — and the alternative is a second detector to keep in step with the first.
    """
    import concurrent.futures
    import time
    import tempfile

    import detect
    import ingest_video

    if not CHAMPION.is_file():
        print(f"{now()} classify: no detector at {CHAMPION} — nothing to classify", flush=True)
        return
    with Lock(), concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        s = load_state()
        why = appearance_stale()
        if why:
            alert("appearance_off", next(iter(GATES.values())),
                  f"  ! appearance off: {why} — refits after the next train pass")
        ds, cl, bucket = r2()
        base, tz = ingest_video.config(), ZoneInfo(TZ)
        # hunt is the curation sweep; classify only counts — no capture_wanted/dataset_dir,
        # so detect.Detector's _dataset_init skips capture and this pass never captures
        # frames or pushes to curation.
        since = classify_since(datetime.now(ZoneInfo(TZ)), base, s.get("classify_floor", ""))
        s["classify_floor"] = since
        save_state(s)
        VIDEOS.mkdir(parents=True, exist_ok=True)
        # cam_and_start parses a segment's stem in the HOST's own local tz, not Lusaka's —
        # every horizon computed from a segment start is wrong if this machine isn't set to
        # Africa/Lusaka (+02:00, no DST). Loud, not fatal: the office Mac IS that host today.
        if time.localtime().tm_gmtoff != 7200:
            print(f"{now()} classify: WARNING host tz offset is {time.localtime().tm_gmtoff}s, "
                  f"not Lusaka's +02:00 — segment starts (and thus every journey horizon) "
                  f"will be computed in the wrong clock", flush=True)

        def download(key):
            dest = local_path(key)
            dest.parent.mkdir(parents=True, exist_ok=True)
            cl.download_file(bucket, key, str(dest))
            return dest

        done = events = left = 0
        touched, failed, keys, all_todo, rendered = set(), set(), [], [], set()
        open_days = {tuple(p) for p in s.get("open_days", [])}
        # R2, not local state, is the authority on "classified" (published_manifests'
        # docstring): merge in anything the bucket already has that local state forgot,
        # before pick_classify can queue it for a costly and unsafe re-run. Once per pass —
        # a single listing of the window's day prefixes, not every round.
        keys = list(recording_keys(cl, bucket))
        fresh = published_manifests(cl, bucket, keys, since) - set(s.get("classified", []))
        if fresh:
            s["classified"] = (s.get("classified", []) + sorted(fresh))[-REMEMBER_CLASSIFIED:]
            # Recovered manifests are proof a day had (or has) provisional journeys to
            # revisit too — same save_state as s["classified"], so a crash right after can't
            # separate "classified" from "someone should still watch this day for a freeze".
            open_days |= {(gate_of(k.split("/")[0]), f"{s8[:4]}-{s8[4:6]}-{s8[6:8]}")
                          for k in fresh for s8 in [Path(k).stem[:8]]}
            s["open_days"] = sorted(list(p) for p in open_days)
            save_state(s)
        classified_now = set()
        last_journeys = time.monotonic()

        def run_journeys():
            nonlocal open_days, touched, last_journeys
            try:
                _, still_open, checked_days = journeys_pass(
                    cl, bucket, touched | open_days, base.get("cameras") or [], tz,
                    keys, s.get("classified", []), since, classified_now)
                classified_now.difference_update({
                    k for k in set(classified_now)
                    if (gate_of(k.split("/")[0]),
                        f"{Path(k).stem[:4]}-{Path(k).stem[4:6]}-{Path(k).stem[6:8]}") in checked_days
                })
                open_days = prune_open_days(open_days | touched, still_open, datetime.now(ZoneInfo(TZ)).date())
                s["open_days"] = sorted(list(p) for p in open_days)
                save_state(s)
                touched.clear()
            except Exception as e:       # journeys ride beside events; they never cost a classify
                print(f"  ! journeys: {e}", flush=True)
            try:                         # health no older than a journeys cadence, not a whole pass
                fold_alerts(s, ALERTS)
                ALERTS.clear()
                save_state(s)
                publish_health(s, cl, bucket, keys, since)
            except Exception as e:
                print(f"  ! health: {e}", flush=True)
            last_journeys = time.monotonic()

        deadline = time.monotonic() + CLASSIFY_BUDGET
        expired = False
        try:
            while not expired:
                keys = list(recording_keys(cl, bucket))   # re-listed each round: picks up arrivals
                all_todo = pick_classify(keys, s.get("classified", []), since, per_pass=None)
                stuck = len(failed & set(all_todo))
                left = len(all_todo)
                s["classify_backlog"] = {"at": now(), "left": left - stuck, "failed": stuck}
                save_state(s)
                live = live_lane(keys, s.get("classified", []), since, time.time(), failed)
                todo = list(dict.fromkeys(live + [k for k in all_todo if k not in failed]))[:CLASSIFY_PER_PASS]
                if not todo:
                    break
                print(f"{now()} classify: {len(todo)} segment(s), {left} left in the window", flush=True)
                # Downloads run ~85-90s of a ~210s segment over the office link: the next
                # segment's download overlaps this one's detection instead of stalling after it.
                pending = pool.submit(download, todo[0])
                for i, key in enumerate(todo):
                    if time.monotonic() > deadline:
                        # Checked per segment, not per round: a round is ~12 segments, and the
                        # budget must not run 45 min over its own limit. The in-flight prefetch
                        # is awaited and discarded rather than left downloaded on disk.
                        try:
                            pending.result().unlink(missing_ok=True)
                        except Exception:
                            pass
                        expired = True
                        break
                    prefix, cam_name, seg = key.split("/")
                    gate = gate_of(prefix)
                    cfg = {**base, "detect_weights": str(CHAMPION), "toll_gate_id": gate}
                    if ATTRS_CHAMPION.is_file():
                        cfg["attr_weights"] = str(ATTRS_CHAMPION)
                    if PLATE.is_file():
                        cfg["plate_weights"] = str(PLATE)
                    # Heading rides on the camera's config entry; without one there is no
                    # direction on these events, which is right — it is never guessed.
                    cam = next((c for c in (base.get("cameras") or []) if c.get("name") == cam_name),
                               {"name": cam_name})
                    print(f"  v {key}", flush=True)
                    try:
                        video = pending.result()
                    except Exception as e:       # a download failure is this segment's error
                        failed.add(key)
                        print(f"  ! {key}: {e}", flush=True)
                        video = None
                    # Submitted regardless of this segment's outcome: the next download must
                    # not wait on this one's detection, or the overlap is lost to a failure.
                    if i + 1 < len(todo):
                        pending = pool.submit(download, todo[i + 1])
                    if video is None:
                        continue
                    out = clip = None
                    try:
                        out = Path(tempfile.mkdtemp())
                        # Only footage recent enough to still matter as a "live view" is worth
                        # the encode time — a backlog must classify at full speed regardless.
                        seg_start = ingest_video.cam_and_start(video)[1]
                        live = seg_start > time.time() - LIVE_HOURS * 3600
                        clip = out / "annotated.mp4" if live else None
                        t0 = time.monotonic()
                        # The recorder's own filename is the footage clock: keep it, or
                        # cam_and_start falls back to mtime and every event is stamped
                        # with the download.
                        day, _, render_ok = detect.classify_segment(video, cam, cfg, tz, out, source_key=key,
                                                                    render_to=clip, gate=gate)
                        classify_s = time.monotonic() - t0 if live else 0
                        n = publish(cl, bucket, out, gate, key, day)
                        new_days = {(gate, js.stem) for js in (out / "tracklets").glob("*.jsonl")}
                        touched |= new_days
                        open_days |= new_days
                        classified_now.add(key)
                        # Recorded only once published: a segment that died mid-pass is retried
                        # next tick rather than lost, and re-running it rewrites the same keys.
                        # open_days saved in the SAME call, not only after journeys_pass succeeds
                        # later: a crash between here and there must not lose track of a day that
                        # now has provisional journeys to revisit.
                        s["classified"] = (s.get("classified", []) + [key])[-REMEMBER_CLASSIFIED:]
                        s["open_days"] = sorted(list(p) for p in open_days)
                        save_state(s)
                        done, events = done + 1, events + n
                        # The clip is a bonus, uploaded only once the segment itself is safely
                        # classified: a failed upload must never fail (or retry) the segment.
                        if render_ok:
                            try:
                                cl.upload_file(str(clip), bucket,
                                               f"{ANNOTATED}{gate}/{cam_name}/{Path(key).stem}.mp4",
                                               ExtraArgs={"ContentType": "video/mp4"})
                                rendered.add((gate, cam_name))
                            except Exception as e:
                                print(f"  ! annotated upload {key}: {e}", flush=True)
                        print(f"    {n} event(s) -> {EVENTS}{gate}/{day.replace('-', '')}/"
                              + (f" ({classify_s:.0f}s classify incl. render)" if live else ""), flush=True)
                    except Exception as e:       # one bad segment must not strand the rest
                        failed.add(key)
                        print(f"  ! {key}: {e}", flush=True)
                    finally:
                        video.unlink(missing_ok=True)   # never leave a prefetched file behind
                        if out is not None:
                            shutil.rmtree(out, ignore_errors=True)
                # Not every round — journeys_pass re-reads whole days, so calling it on
                # every ~12-segment round multiplied that cost for no benefit. But not only
                # in `finally` either: with CLASSIFY_BUDGET at 3h, that alone could leave the
                # RDA dashboard up to 3h stale when it should lag by tens of minutes.
                if time.monotonic() - last_journeys >= JOURNEYS_EVERY_S:
                    run_journeys()
        finally:
            # Always once more here: touched/open_days accumulate round by round precisely
            # so a crash or an uncaught exception in the loop still gets a chance to freeze
            # what's already classified, instead of losing it until the next pass notices.
            run_journeys()
            # Recounted on EVERY exit: a raise above would otherwise leave the last round's
            # count (up to CLASSIFY_PER_PASS too high) for ~2 h, and train/ingest yield to it.
            # A segment that keeps failing must not starve hunt/ingest/train for 48h: it stays
            # in `left` for status, but not in the count classify_behind() acts on — it will
            # be retried next pass regardless, since it is never added to s["classified"].
            # Guarded so a failure here can't mask the exception that got us here.
            try:
                all_todo = pick_classify(keys, s.get("classified", []), since, per_pass=None)
                stuck = len(failed & set(all_todo))
                left = len(all_todo)
                s["classify_backlog"] = {"at": now(), "left": left - stuck, "failed": stuck}
                s["last_classify"] = {"at": now(), "segments": done, "events": events}
                save_state(s)       # the recount lands before anything optional can fail
            except Exception as e:
                print(f"  ! recount: {e}", flush=True)
            try:
                fold_alerts(s, ALERTS)
                ALERTS.clear()
                save_state(s)
                publish_health(s, cl, bucket, keys, since)
            except Exception as e:
                print(f"  ! health: {e}", flush=True)
        for (gate, day), cams in coverage_manifests(keys, s.get("classified", []), since).items():
            cl.put_object(Bucket=bucket, Key=f"{COVERAGE}{gate}/{day}.json",
                          Body=json.dumps({"gate": gate, "day": day, "updated": now(),
                                          "cameras": cams}).encode(),
                          ContentType="application/json")
        for gate, cam_name in rendered:
            prune_annotated(cl, bucket, gate, cam_name)
        print(f"{now()} classify: {done} segment(s), {events} event(s) published, "
              f"{left} left in the window", flush=True)


def prune_annotated(cl, bucket, gate, cam_name):
    """Delete this camera's annotated clips older than ANNOTATED_KEEP_DAYS — listed by
    its own gate/cam prefix only, never the whole bucket."""
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=ANNOTATED_KEEP_DAYS)).strftime("%Y%m%d-%H%M%S")
    for key in bucket_keys(cl, bucket, f"{ANNOTATED}{gate}/{cam_name}/"):
        if Path(key).stem < cutoff:
            cl.delete_object(Bucket=bucket, Key=key)


def night_slot():
    """Lusaka night with a backlog one classify pass drains: train and hunt may take the GPU.
    By day any backlog wins; without this slot hunt never ran (classify always has a few left)."""
    return datetime.now(ZoneInfo(TZ)).hour in NIGHT and \
        (load_state().get("classify_backlog") or {}).get("left", 0) <= NIGHT_LEFT


def train_pass(force=False):
    import train as trainer
    if not force and not night_slot() and classify_behind():
        print(f"{now()} train: yielding — classify has segment(s) to do", flush=True)
        return
    # A forced run must not exit "busy" while classify drains its budget (up to
    # CLASSIFY_BUDGET) — it queues behind the drain instead of failing.
    with Lock(wait=CLASSIFY_BUDGET + 3600 if force else WAIT):
        s = load_state()
        ds, cl, bucket = r2()
        ds.pull(cl, bucket, names=ds.CONFIG)
        active = retention_policy()
        if active:
            # Read-side only: archive is durable training input, never a pushed ledger.
            ds.pull(cl, bucket, names=ds.LEDGERS + ("classifier-crops",))
            detector_ids, attrs_ids = retention_training_ids()
            old_detector = s.get("retention_detector_source_ids")
            old_attrs = s.get("retention_attrs_source_ids")
            if old_detector is None or old_attrs is None:
                s["retention_detector_source_ids"] = sorted(detector_ids)
                s["retention_attrs_source_ids"] = sorted(attrs_ids)
                save_state(s)
                print(f"{now()} train: retention snapshots initialized ({len(detector_ids)} detector, "
                      f"{len(attrs_ids)} attrs)", flush=True)
                return
            new_detector = detector_ids - set(old_detector)
            new_attrs = attrs_ids - set(old_attrs)
            detector_due = force or len(new_detector) >= THRESHOLD
            attrs_due = force or len(new_attrs) >= THRESHOLD
        else:
            detector_ids = attrs_ids = None
            new_detector = new_attrs = set()
            ds.pull(cl, bucket, names=ds.LEDGERS)
            detector_due = force or should_train(new_frames(), s.get("trained_frames", 0))
            attrs_due = detector_due
        count = new_frames()
        if not detector_due and not attrs_due:
            print(f"{now()} train: {count} new frames, {s.get('trained_frames', 0)} at the last run "
                  f"— {THRESHOLD - (count - s.get('trained_frames', 0))} more to go", flush=True)
            return
        if detector_due:
            print(f"{now()} train: {count} new frames — training", flush=True)
            r = subprocess.run([sys.executable, str(ROOT / "train.py")], cwd=ROOT)
            if r.returncode:
                if not active:
                    sys.exit(f"train.py failed ({r.returncode}); champion untouched")
                print(f"{now()} train.py failed ({r.returncode}); detector snapshot unchanged", flush=True)
            else:
                run = max(trainer.RUNS.glob("*/"), key=lambda p: p.stat().st_mtime)
                best = run / "weights" / "best.pt"
                ev, incumbent = run_eval(run), champion_on_split()
                s["trained_frames"], s["last_train"] = count, {"at": now(), "run": run.name, "eval": ev,
                                                               "champion_on_same_split": incumbent}
                if promote(ev, incumbent):
                    crown(s, run, count, ev, cl, bucket)
                else:
                    print(f"{now()} train: {run.name} scored {ev['map50'] if ev else 'n/a'} on the new frames' "
                          f"val split, champion {incumbent['map50']} on the same — champion stays", flush=True)
                if active:
                    s["retention_detector_source_ids"] = sorted(set(old_detector) | detector_ids)
        if attrs_due:
            attrs_ok = attrs_pass(s, cl, bucket)
            if active and attrs_ok:
                s["retention_attrs_source_ids"] = sorted(set(old_attrs) | attrs_ids)
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
        return False
    run = max(train_attrs.RUNS.glob("*/"), key=lambda p: p.stat().st_mtime)
    rep = attrs_report(run)
    s["last_attrs"] = {"at": now(), "run": run.name, "mean_acc": rep and rep["mean_acc"]}
    if promote_attrs(rep, s.get("attrs_champion")):
        crown_attrs(s, run, rep, cl, bucket)
    else:
        print(f"{now()} attrs: {run.name} scored {rep['mean_acc'] if rep else 'n/a'} mean val "
              f"accuracy, attrs champion stays at {s['attrs_champion']['mean_acc']}", flush=True)
    return True


def attrs_report(run):
    try:
        return json.loads((run / "report.json").read_text())
    except (OSError, ValueError):
        return None


def run_eval(run):
    """The run on its own val split — the frames promotion is decided on."""
    try:
        return json.loads((run / "val-eval.json").read_text())
    except (OSError, ValueError):
        return None


def champion_on_split():
    """The champion scored on the split the last train.py built (train.py score), or None
    when there is no champion — the run's own val-eval was measured on the same frames."""
    import train
    if not CHAMPION.is_file():
        return None
    r = subprocess.run([sys.executable, str(ROOT / "train.py"), "score", str(CHAMPION)], cwd=ROOT)
    if r.returncode:
        print(f"{now()} train: could not score the champion ({r.returncode}); it keeps its slot", flush=True)
        return {"map50": float("inf")}      # an unscored champion is never demoted by default
    try:
        return json.loads((train.RUN / "score.json").read_text())
    except (OSError, ValueError):
        return {"map50": float("inf")}


def crown(s, run, frames, ev, cl, bucket):
    """Make this run the champion: the pre-labeller here, and models/champion.pt in the
    bucket for whoever deploys it to a gate."""
    best = run / "weights" / "best.pt"
    shutil.copy2(best, CHAMPION)
    s["champion"] = {"run": run.name, "frames": frames, "at": now(),
                     **{k: (ev or {}).get(k) for k in ("map50", "map50_95")}}
    for key in (f"{MODELS}{run.name}/best.pt", f"{MODELS}champion.pt"):
        cl.upload_file(str(best), bucket, key)
    if (run / "reference-eval.json").is_file():      # absent when no reference frames were held out
        cl.upload_file(str(run / "reference-eval.json"), bucket, f"{MODELS}{run.name}/reference-eval.json")
    cl.put_object(Bucket=bucket, Key=f"{MODELS}champion.json", Body=json.dumps(s["champion"]).encode())
    # Every approved frame outside the reference went into this run: record the list, so
    # the consoles' "new since the last training" starts from zero — and publish it where
    # the config pull already looks.
    ref = set()
    try:
        ref = {ln.strip() for ln in (DATASET / "reference.txt").read_text().splitlines() if ln.strip()}
    except OSError:
        pass
    done = sorted(p.stem for p in (DATASET / "approved" / "labels").glob("*.txt")
                  if p.stem not in ref)
    (DATASET / "trained.txt").write_text("\n".join(done) + "\n")
    import dataset_sync
    cl.upload_file(str(DATASET / "trained.txt"), bucket, dataset_sync.PREFIX + "trained.txt")
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
    # the appearance PCA refit runs after the lock is released: refresh_appearance()


def appearance_stale():
    """None when fresh (or there is no attrs model); else why appearance codes are off."""
    if not ATTRS_CHAMPION.is_file():
        return None
    try:
        import numpy as np
        with np.load(DATASET / "appearance-pca.npz") as z:
            have = str(z["champion"])
    except FileNotFoundError:
        return "no appearance-pca.npz"
    except Exception as e:
        return f"appearance-pca.npz unreadable ({e})"
    want = hashlib.sha256(ATTRS_CHAMPION.read_bytes()).hexdigest()[:12]
    return None if have == want else f"appearance-pca.npz fitted on {have}, champion is {want}"


def refresh_appearance():
    """Refit the appearance PCA if stale. Called after the lock is released — the fit
    fetches crops and embeds them, minutes of work. Best-effort; every train retries."""
    if appearance_stale() is None:
        return
    try:
        r = subprocess.run([sys.executable, str(ROOT / "appearance_pca.py"), "--weights",
                            str(ATTRS_CHAMPION), "--gate", next(iter(GATES.values()))],
                           cwd=ROOT, timeout=1800, capture_output=True, text=True)
        err = r.returncode and ((r.stderr or r.stdout).strip()[-300:] or f"exit {r.returncode}")
    except Exception as e:
        err = str(e)
    if err:
        print(f"  ! appearance PCA refit failed: {err}", flush=True)


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
    import time
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
    print(f"lock:          {(LOCK.read_text().strip() if LOCK.exists() else '') or 'free'}")
    keys, since = [], ""
    try:
        import ingest_video
        _, cl, bucket = r2()
        now_dt = datetime.now(ZoneInfo(TZ))
        since = classify_since(now_dt, ingest_video.config(), s.get("classify_floor", ""))
        keys = list(recording_keys(cl, bucket))
        cov = coverage(keys, s.get("classified", []), since)
        default_since = classify_since(now_dt, {})
        label = (f"since {since}" if since != default_since else f"last {CLASSIFY_HOURS}h")
        print(f"coverage ({label}):")
        for hour in sorted(cov):
            parts = ", ".join(f"{cam} {c}/{r}" for cam, (c, r) in sorted(cov[hour].items()))
            print(f"  {hour}  {parts}")
    except (Exception, SystemExit) as e:
        print(f"coverage:      unavailable ({e})")
    print("health:")
    print(f"  backlog:     {s.get('classify_backlog')}")
    try:
        for gate, cams in camera_lag(keys, s.get("classified", []), since).items():
            print(f"  lag {gate}:  " + ", ".join(
                f"{c} {'never classified' if k is None else f'{(r - k) / 60:.0f} min'}, oldest unclassified "
                f"{'none' if o is None else f'{(time.time() - o) / 60:.0f} min'}"
                for c, (r, k, o) in sorted(cams.items())))
    except Exception as e:
        print(f"  lag:         unavailable ({e})")
    fold_alerts(s, [])
    by_kind = {}
    for a in s["alerts"]:
        by_kind.setdefault(a["kind"], []).append(a)
    for kind, items in sorted(by_kind.items()):
        print(f"  {kind}: {len(items)} in 24h")
        for a in items[:3]:
            print(f"    {a['at']} {a['gate']} {a['detail'].strip()}")
    if not by_kind:
        print("  no alerts in the last 24h")


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


def curation_check():
    """prune_pending() must delete exactly what curation_cap.evictions() would, ranked
    over LOCAL stems union the BUCKET's — a sample that alone looks safe locally (under
    cap) must still be pruned when the bucket already holds `cap` newer ones for its
    gate, or a push would resurrect what the curation server just evicted. Never touches
    an external import (no capture timestamp to evict by). A failed bucket listing prunes
    nothing and tells the caller to skip its push. curation_settings() must fall back
    safely when the bucket has no file or a bad client."""
    import contextlib
    import io
    import tempfile
    from unittest.mock import patch
    import curation_cap

    me = sys.modules[__name__]

    class FakeCl:
        def __init__(self, listing=None, body=None, fail_list=False, fail_get=False):
            self.listing, self.body = listing or {}, body
            self.fail_list, self.fail_get = fail_list, fail_get

        def get_paginator(self, _):
            listing, fail = self.listing, self.fail_list

            class P:
                def paginate(self, Bucket, Prefix="", **_kw):
                    if fail:
                        raise RuntimeError("listing failed")
                    yield {"Contents": [{"Key": k} for k in listing if k.startswith(Prefix)]}
            return P()

        def get_object(self, Bucket, Key):
            if self.fail_get:
                raise RuntimeError("no such key")
            return {"Body": io.BytesIO(self.body)}

    def write_local(tmp, stems):
        for part in curation_cap.PARTS:
            (tmp / "pending" / part).mkdir(parents=True, exist_ok=True)
        for sid in stems:
            for part in curation_cap.PARTS:
                (tmp / "pending" / part / f"{sid}.{curation_cap.EXT[part]}").write_text("x")

    # No bucket samples: ranked over local alone, same as a plain local-only cap.
    tmp = Path(tempfile.mkdtemp())
    write_local(tmp, [f"A-{d}" for d in ("20260101-000000", "20260102-000000", "20260103-000000")] +
                ["external-deadbeef"])
    with patch.object(me, "DATASET", tmp):
        assert prune_pending(FakeCl(), "bucket", cap=None) == (0, True), "cap unset: nothing pruned"
        pruned, ok = prune_pending(FakeCl(), "bucket", cap=2)
    assert (pruned, ok) == (1, True), (pruned, ok)
    assert all(not (tmp / "pending" / part / f"A-20260101-000000.{curation_cap.EXT[part]}").is_file()
               for part in curation_cap.PARTS), "oldest not pruned across every part"
    assert (tmp / "pending" / "images" / "A-20260102-000000.jpg").is_file(), "newest kept"
    assert (tmp / "pending" / "images" / "external-deadbeef.jpg").is_file(), "external never evicted"

    # The bucket already holds cap newer samples for A; the one local sample is older
    # than all of them and must be pruned even though it is alone (under cap) locally.
    tmp2 = Path(tempfile.mkdtemp())
    write_local(tmp2, ["A-20260101-000000"])
    remote = {f"curation/pending/images/A-{d}.jpg": 1
              for d in ("20260102-000000", "20260103-000000", "20260104-000000")}
    with patch.object(me, "DATASET", tmp2):
        pruned, ok = prune_pending(FakeCl(listing=remote), "bucket", cap=2)
        assert (pruned, ok) == (1, True), (pruned, ok)
        assert not (tmp2 / "pending" / "images" / "A-20260101-000000.jpg").is_file(), \
            "resurrection: local-only ranking would have kept this"
        # A listing failure must prune nothing and tell the caller to skip its push.
        write_local(tmp2, ["A-20260105-000000"])
        with contextlib.redirect_stdout(io.StringIO()):
            pruned, ok = prune_pending(FakeCl(fail_list=True), "bucket", cap=2)
        assert (pruned, ok) == (0, False), (pruned, ok)
        assert (tmp2 / "pending" / "images" / "A-20260105-000000.jpg").is_file(), \
            "a failed listing must not prune"

    with contextlib.redirect_stdout(io.StringIO()):
        got = curation_settings(FakeCl(body=b"cap: 500\npaused: [RDA-TG-KTB]\n"), "bucket")
        assert got == {"cap": 500, "paused": ["RDA-TG-KTB"]}, got
        assert curation_settings(FakeCl(fail_get=True), "bucket") == {"cap": None, "paused": []}, \
            "any fetch error is the safe default"


def lock_check():
    """The lock is the only thing between two GPU jobs. A dead holder must be taken over;
    a live one must be waited for; and the take must be atomic."""
    import tempfile
    from unittest.mock import patch
    me = sys.modules[__name__]
    tmp = Path(tempfile.mkdtemp()) / "loop.lock"
    with patch.object(me, "LOCK", tmp), patch.object(me, "WAIT", 0):
        tmp.write_text("999999999 ['ghost'] since never")   # left by a dead process
        with Lock():
            assert tmp.read_text().startswith(str(os.getpid())), "dead process's lock not taken"
            try:
                with Lock():
                    raise AssertionError("a live lock was granted twice")
            except SystemExit as e:
                assert "busy" in str(e), e
        assert tmp.read_text() == "", "lock not released"
        with Lock():
            pass


def publish_check():
    """The bucket layout the RDA importer reads is a contract: gate, day, one jsonl per
    segment, crops beside it. Nothing downstream is ours, so this is where a rename or a
    stray path separator has to be caught."""
    import tempfile

    class FakeS3:
        def __init__(self, fail_crop=False):
            self.put, self.sent = {}, []
            self.fail_crop = fail_crop

        def put_object(self, Bucket, Key, Body, ContentType=None):
            self.put[Key] = Body

        def upload_file(self, path, Bucket, Key):
            if self.fail_crop:
                raise OSError("upload failed")
            self.sent.append(Key)

    out = Path(tempfile.mkdtemp())
    (out / "crops" / "2026-08-19").mkdir(parents=True)
    (out / "crops" / "2026-08-19" / "c-123-best.jpg").write_bytes(b"jpeg")
    (out / "2026-08-19.jsonl").write_text(
        json.dumps({"id": "c-123", "class": "e-heavy",
                    "crops": {"best": "crops/2026-08-19/c-123-best.jpg"}}) + "\n")
    cl = FakeS3()
    source = "RDA-TG-KTB/north/20260819-151108.mkv"
    manifest = f"20260819-151108-{hashlib.sha256(source.encode()).hexdigest()[:32]}"
    assert publish(cl, "buck", out, "RDA-TG-KTB", source, "2026-08-19") == 1
    key = f"fieldkit-events/RDA-TG-KTB/20260819/{manifest}.jsonl"
    assert list(cl.put) == [key], cl.put
    doc = json.loads(cl.put[key].decode())
    assert doc["gate"] == "RDA-TG-KTB" and doc["crops"] == {"best": "crops/c-123-best.jpg"}, doc
    assert cl.sent == ["fieldkit-events/RDA-TG-KTB/20260819/crops/c-123-best.jpg"], cl.sent

    # Tracklets go first, under their own prefix, every crop (the plate's too) flattened beside them.
    tk, rel = Path(tempfile.mkdtemp()), "tracklets/crops/2026-08-19/t-1-"
    (tk / "tracklets" / "crops" / "2026-08-19").mkdir(parents=True)
    for tag in ("top", "best", "plate"):
        (tk / f"{rel}{tag}.jpg").write_bytes(b"jpeg")
    (tk / "tracklets" / "2026-08-19.jsonl").write_text(json.dumps(
        {"id": "t-1", "crops": {"top": rel + "top.jpg", "best": rel + "best.jpg"},
         "plate": {"conf": 0.3, "crop": rel + "plate.jpg"}}) + "\n")
    cl, where = FakeS3(), "fieldkit-tracklets/G/20260819/"
    assert publish(cl, "buck", tk, "G", source, "2026-08-19") == 0, "tracklets are not events"
    assert list(cl.put) == [f"{where}{manifest}.jsonl",
                            f"fieldkit-events/G/20260819/{manifest}.jsonl"], cl.put
    doc = json.loads(cl.put[f"{where}{manifest}.jsonl"].decode())
    assert doc == {"id": "t-1", "gate": "G", "crops": {"top": "crops/t-1-top.jpg", "best": "crops/t-1-best.jpg"},
                   "plate": {"conf": 0.3, "crop": "crops/t-1-plate.jpg"}}, doc
    assert sorted(cl.sent) == [f"{where}crops/t-1-{tag}.jpg" for tag in ("best", "plate", "top")], cl.sent
    (tk / f"{rel}plate.jpg").unlink()
    cl = FakeS3()
    try:
        publish(cl, "buck", tk, "G", source, "2026-08-19")
        raise AssertionError("a tracklet missing its plate crop published")
    except FileNotFoundError:
        pass
    assert cl.put == {}, cl.put

    # This publication exposes no manifest containing a missing or failed crop reference.
    for missing, client in ((True, FakeS3()), (False, FakeS3(fail_crop=True))):
        broken = Path(tempfile.mkdtemp())
        (broken / "crops" / "2026-08-19").mkdir(parents=True)
        if not missing:
            (broken / "crops" / "2026-08-19" / "x.jpg").write_bytes(b"jpeg")
        (broken / "2026-08-19.jsonl").write_text(json.dumps(
            {"id": "x", "crops": {"best": "crops/2026-08-19/x.jpg"}}) + "\n")
        try:
            publish(client, "buck", broken, "G", "G/north/same.mkv", "2026-08-19")
            raise AssertionError("broken evidence published")
        except OSError:
            pass
        assert client.put == {}, client.put

    # A segment with no vehicles still publishes, or it would be classified again forever.
    quiet, cl = Path(tempfile.mkdtemp()), FakeS3()
    quiet_source = "RDA-TG-KTB/north/20260819-152108.mkv"
    assert publish(cl, "buck", quiet, "RDA-TG-KTB", quiet_source, "2026-08-19") == 0
    quiet_name = f"20260819-152108-{hashlib.sha256(quiet_source.encode()).hexdigest()[:32]}"
    assert cl.put == {f"fieldkit-events/RDA-TG-KTB/20260819/{quiet_name}.jsonl": b""}, cl.put

    midnight, cl = Path(tempfile.mkdtemp()), FakeS3()
    (midnight / "2026-08-19.jsonl").write_text(json.dumps({"id": "a"}) + "\n")
    (midnight / "2026-08-20.jsonl").write_text(json.dumps({"id": "b"}) + "\n")
    midnight_source = "G/north/cross-midnight.mkv"
    assert publish(cl, "buck", midnight, "G", midnight_source, "2026-08-19") == 2
    midnight_name = f"cross-midnight-{hashlib.sha256(midnight_source.encode()).hexdigest()[:32]}"
    assert set(cl.put) == {f"fieldkit-events/G/20260819/{midnight_name}.jsonl",
                          f"fieldkit-events/G/20260820/{midnight_name}.jsonl"}, cl.put

    # Flat, camera-specific keys retain both cameras; retrying one source overwrites only itself.
    cl = FakeS3()
    for source in ("G/north/20260819-151108.mkv", "G/south/20260819-151108.mkv",
                   "G/north/20260819-151108.mkv"):
        publish(cl, "buck", Path(tempfile.mkdtemp()), "G", source, "2026-08-19")
    assert len(cl.put) == 2, cl.put


def listing_check():
    """A pass lists the gates' recording folders, never curation, evidence or models."""
    class Tops:
        keys = ["site1/cam3/20260923-101636.mkv", "RDA-TG-X/north/20260923-101636.mkv",
                "curation/pending/images/x.jpg", "curation-paired/v1/c.json", "models/champion.pt",
                "fieldkit-events/G/20260923/m.jsonl", "classifier-crops-history/2026-09-09/c.jpg"]
        listed = []

        def get_paginator(self, _):
            return self

        def paginate(self, Bucket, Prefix="", Delimiter=None):
            ks = [k for k in self.keys if k.startswith(Prefix)]
            if Delimiter:
                return [{"CommonPrefixes": [{"Prefix": p} for p in sorted({k.split("/")[0] + "/" for k in ks})]}]
            self.listed.append(Prefix)
            return [{"Contents": [{"Key": k} for k in ks]}]

    t = Tops()
    assert sorted(recording_keys(t, "b")) == ["RDA-TG-X/north/20260923-101636.mkv",
                                              "site1/cam3/20260923-101636.mkv"], t.listed
    assert sorted(t.listed) == ["RDA-TG-X/", "site1/"], t.listed


def journeys_check():
    """A day rebuilt from the bucket alone: tracklets from two paired cameras become one
    journey citing its crops by key, with attrs from the day's events."""
    import io

    class FakeS3:
        def __init__(self, objs):
            self.objs, self.put, self.listed = objs, {}, []

        def get_paginator(self, _):
            return self

        def paginate(self, Bucket, Prefix, Delimiter=None):
            keys = [k for k in self.objs if k.startswith(Prefix)
                    and not (Delimiter and Delimiter in k[len(Prefix):])]
            self.listed += keys
            return [{"Contents": [{"Key": k} for k in keys]}]

        def get_object(self, Bucket, Key):
            return {"Body": io.BytesIO(self.objs[Key])}

        def put_object(self, Bucket, Key, Body, ContentType=None):
            self.put[Key] = Body

    def tracklet(cam, samples):       # (t, centre x, centre y): a 0.2-wide box per sample
        path = [[t, x - 0.1, y - 0.1, x + 0.1, y + 0.1] for t, x, y in samples]
        return json.dumps({
            "id": f"obs-{cam}", "camera": cam, "t0": path[0][0], "t1": path[-1][0], "hits": len(path),
            "class": "e-heavy", "votes": {"e-heavy": len(path)}, "conf": {"e-heavy": 0.8},
            "counted": True, "ghost_of": None, "direction": "northbound", "path": path,
            "crops": {"top": f"crops/obs-{cam}-top.jpg", "best": f"crops/obs-{cam}-best.jpg"},
            "plate": {"conf": 0.3, "crop": f"crops/obs-{cam}-plate.jpg"}}).encode() + b"\n"

    # Real 2026-08-19 epochs, not tiny synthetic ones: journeys_pass now keeps only journeys
    # whose own `ts` lands on the touched day (the midnight fix), so a journey's members
    # must actually fall within it.
    BASE = datetime(2026, 8, 19, 10, 0, tzinfo=timezone.utc).timestamp()

    def tracklet(cam, samples):       # (t, centre x, centre y): a 0.2-wide box per sample
        path = [[BASE + t, x - 0.1, y - 0.1, x + 0.1, y + 0.1] for t, x, y in samples]
        return json.dumps({
            "id": f"obs-{cam}", "camera": cam, "t0": path[0][0], "t1": path[-1][0], "hits": len(path),
            "class": "e-heavy", "votes": {"e-heavy": len(path)}, "conf": {"e-heavy": 0.8},
            "counted": True, "ghost_of": None, "direction": "northbound", "path": path,
            "crops": {"top": f"crops/obs-{cam}-top.jpg", "best": f"crops/obs-{cam}-best.jpg"},
            "plate": {"conf": 0.3, "crop": f"crops/obs-{cam}-plate.jpg"}}).encode() + b"\n"

    # Northbound: cam3 watches it leave bottom-right at t=100, cam4 sees it enter bottom-left 0.4 s later.
    day = "fieldkit-tracklets/G/20260819/"
    objs = {day + "a.jsonl": tracklet("cam3", [(96 + k, 0.5 + 0.1 * k, 0.5 + 0.075 * k) for k in range(5)]),
            day + "b.jsonl": tracklet("cam4", [(100.4 + k, 0.1 + 0.1 * k, 0.7 - 0.1 * k) for k in range(5)]),
            day + "crops/obs-cam3-top.jpg": b"jpeg",      # beside the manifests, never listed
            "fieldkit-events/G/20260819/e.jsonl": json.dumps(
                {"id": "obs-cam4", "class": "e-heavy", "hits": 5, "attrs": {"axles": "5"}}).encode()}
    cams = [{"name": "cam3", "heading": "south", "handoff": {"camera": "cam4", "zone": [0.80, 0.62, 0.20, 0.38]}},
            {"name": "cam4", "heading": "north", "handoff": {"camera": "cam3", "zone": [0.0, 0.55, 0.22, 0.30]}}]
    cl = FakeS3(objs)
    assert journeys_pass(cl, "buck", {("G", "2026-08-19")}, cams, timezone.utc) == \
        (1, {("G", "2026-08-19")}, {("G", "2026-08-19")}), \
        "unfrozen (no keys/classified given: horizon never clears): still open and checked"
    assert set(cl.put) == {"fieldkit-journeys/G/20260819/journeys.jsonl"}, cl.put
    assert not any("/crops/" in k for k in cl.listed), cl.listed
    [j] = map(json.loads, cl.put["fieldkit-journeys/G/20260819/journeys.jsonl"].decode().splitlines())
    assert j["gate"] == "G" and j["link"] and j["attrs"] == {"axles": "5"}, j
    assert j["crops"] and all(v.startswith(day + "crops/") for v in j["crops"].values()), j["crops"]
    cl = FakeS3(objs)
    assert journeys_pass(cl, "buck", {("G", "2026-08-19")}, [{"name": "cam3"}, {"name": "cam4"}],
                         timezone.utc) == (0, set(), {("G", "2026-08-19")}), \
        "no handoff, nothing to diagnose"

    # r2_jsonl: bodies cached by (key, ETag): re-read costs 0 downloads, a changed ETag exactly 1.
    import tempfile
    class ETagS3:
        def __init__(self): self.o, self.gets = {"p/a.jsonl": (b'{"n":1}\n', "e1"), "p/b.jsonl": (b'{"n":2}\n', "e2")}, 0
        def get_paginator(self, _): return self
        def paginate(self, Bucket, Prefix, Delimiter):
            return [{"Contents": [{"Key": k, "ETag": f'"{e}"', "Size": len(b)} for k, (b, e) in self.o.items()]}]
        def get_object(self, Bucket, Key):
            self.gets += 1
            return {"Body": io.BytesIO(self.o[Key][0])}
    global R2_CACHE
    keep, R2_CACHE = R2_CACHE, Path(tempfile.mkdtemp())
    try:
        ec = ETagS3()
        d1 = r2_jsonl(ec, "buck", "p/")
        assert d1 == [{"n": 1}, {"n": 2}] and ec.gets == 2, (d1, ec.gets)
        assert r2_jsonl(ec, "buck", "p/") == d1 and ec.gets == 2, "second read must be all cache hits"
        ec.o["p/b.jsonl"] = (b'{"n":3}\n', "e3")
        assert r2_jsonl(ec, "buck", "p/") == [{"n": 1}, {"n": 3}] and ec.gets == 3, ec.gets
        short = next(f for f in R2_CACHE.iterdir() if f.read_bytes() == b'{"n":3}\n')
        short.write_bytes(b'{"n"')                     # a power cut left it short under its final name
        assert r2_jsonl(ec, "buck", "p/") == [{"n": 1}, {"n": 3}] and ec.gets == 4, "a short cache file is refetched"
    finally:
        shutil.rmtree(R2_CACHE, ignore_errors=True)
        R2_CACHE = keep

    # Finalization: with both cameras' one segment of the day classified, horizon() reads a
    # real 2026 epoch well past the tracklets' — so the linked journey clears FINAL_MARGIN
    # and freezes into final/ once. A second pass, seeing that batch, must exclude its
    # tracklets and neither re-emit it nor write a second batch.
    day_keys = ["G/cam3/20260819-130000.mkv", "G/cam4/20260819-130000.mkv"]   # after BASE's ~12:01 CAT
    cl = FakeS3(dict(objs))
    n1, open1, checked1 = journeys_pass(cl, "buck", {("G", "2026-08-19")}, cams, timezone.utc,
                              keys=day_keys, classified=day_keys, since="20260101-000000")
    assert n1 == 1 and open1 == set() and checked1 == {("G", "2026-08-19")}, (n1, open1, checked1)
    [final_key] = [k for k in cl.put if "/final/" in k]
    [frozen] = map(json.loads, cl.put[final_key].decode().splitlines())
    assert frozen["link"] and frozen["gate"] == "G", frozen
    cl.objs.update(cl.put)         # the bucket now holds pass 1's writes
    cl.put, cl.listed = {}, []
    n2, open2, checked2 = journeys_pass(cl, "buck", {("G", "2026-08-19")}, cams, timezone.utc,
                              keys=day_keys, classified=day_keys, since="20260101-000000")
    assert (n2, open2, checked2) == (0, set(), {("G", "2026-08-19")}), \
        "the pair's tracklets are already frozen: nothing left to build"
    assert not any("/final/" in k for k in cl.put), "already frozen: no second batch"
    [j2] = map(json.loads, cl.put["fieldkit-journeys/G/20260819/journeys.jsonl"].decode().splitlines())
    assert j2["id"] == frozen["id"], "journeys.jsonl still reports the frozen journey, informationally"

    # Midnight: cam3's leg ends 23:59:58 on day D (2026-08-19), cam4's begins 00:00:01 on
    # D+1 (2026-08-20) — filed under different day dirs by detect.py. One pass per day, each
    # loading a 60s sliver of its neighbour, must build exactly one journey, frozen once
    # (from whichever day's pass runs with the tracklets already excluded from the other).
    # Same handoff geometry as the proven-linking pair above (centre offsets only — the
    # zone-crossing shape is what makes _links() pair them), just time-shifted so cam3's
    # last sample lands on d_end and cam4's first on d_start2.
    d_end = datetime(2026, 8, 19, 23, 59, 58, tzinfo=ZoneInfo(TZ)).astimezone(timezone.utc).timestamp()
    d_start2 = datetime(2026, 8, 20, 0, 0, 1, tzinfo=ZoneInfo(TZ)).astimezone(timezone.utc).timestamp()
    shift3, shift4 = d_end - 100 - BASE, d_start2 - 100.4 - BASE   # tracklet() adds BASE back
    mid_objs = {
        "fieldkit-tracklets/G/20260819/a.jsonl":
            tracklet("cam3", [(96 + k + shift3, 0.5 + 0.1 * k, 0.5 + 0.075 * k) for k in range(5)]).replace(
                b'"obs-cam3"', b'"obs-cam3-mid"'),
        "fieldkit-tracklets/G/20260820/b.jsonl":
            tracklet("cam4", [(100.4 + k + shift4, 0.1 + 0.1 * k, 0.7 - 0.1 * k) for k in range(5)]).replace(
                b'"obs-cam4"', b'"obs-cam4-mid"')}
    cl = FakeS3(mid_objs)
    n_d, _, _ = journeys_pass(cl, "buck", {("G", "2026-08-19")}, cams, timezone.utc)
    [jd] = map(json.loads, cl.put["fieldkit-journeys/G/20260819/journeys.jsonl"].decode().splitlines())
    assert n_d == 1 and jd["link"], "day D's pass sees cam4's sliver across the boundary and links them"
    cl.objs.update(cl.put)
    cl.put = {}
    n_d1, _, _ = journeys_pass(cl, "buck", {("G", "2026-08-20")}, cams, timezone.utc)
    d1_lines = cl.put["fieldkit-journeys/G/20260820/journeys.jsonl"].decode().splitlines()
    assert n_d1 == 0 and not d1_lines, \
        "day D+1's own pass must not also build the boundary vehicle: it belongs to D's ts"

    # Freeze the midnight journey in D's pass first, then run D+1's pass: no new final
    # journey should appear for either leg — the vehicle's tracklets are already claimed by
    # D's final/ batch, and D+1's own pass excludes them via that same batch.
    later_keys = ["G/cam3/20260819-130000.mkv", "G/cam3/20260820-130000.mkv", "G/cam4/20260820-130000.mkv"]
    cl2 = FakeS3(dict(mid_objs))
    n_fd, open_fd, checked_fd = journeys_pass(cl2, "buck", {("G", "2026-08-19")}, cams, timezone.utc,
                                  keys=later_keys, classified=later_keys, since="20260101-000000")
    assert n_fd == 1 and open_fd == set() and checked_fd == {("G", "2026-08-19")}, \
        (n_fd, open_fd, checked_fd)
    [ffinal] = [k for k in cl2.put if "/final/" in k]
    cl2.objs.update(cl2.put)
    cl2.put = {}
    n_fd1, open_fd1, checked_fd1 = journeys_pass(cl2, "buck", {("G", "2026-08-20")}, cams, timezone.utc,
                                    keys=later_keys, classified=later_keys, since="20260101-000000")
    assert n_fd1 == 0 and open_fd1 == set() and checked_fd1 == {("G", "2026-08-20")} \
        and not any("/final/" in k for k in cl2.put), \
        "D+1's pass must not mint a second final batch for the vehicle D already froze"


def selfcheck():
    from datetime import timedelta
    cams = [{"name": "cam3", "handoff": {"camera": "cam4", "zone": []}},
            {"name": "cam4", "handoff": {"camera": "cam3", "zone": []}}, {"name": "cam5"}]
    lone = lambda cam="cam4", ev="line", dr="north", cls="a-car": {
        "camera": cam, "evidence": ev, "direction": dr, "class": cls,
        "members": [{"t0": 100.0, "t1": 104.0}, {"t0": 101.0, "t1": 106.0}]}
    assert miss_moments([lone()], cams) == {"cam3": [97.0, 109.0]}
    assert miss_moments([lone("cam3+cam4"), lone(ev="handoff"), lone(ev="edge"), lone(dr=None),
                         lone("cam5"), lone(cls="e-heavy")], cams) == {}
    class NoKey(Exception):
        pass

    class JourneysS3:
        class exceptions:
            NoSuchKey = NoKey

        def get_object(self, Bucket, Key):
            if Key != f"{JOURNEYS}G/20261008/journeys.jsonl":
                raise NoKey(Key)
            import io
            return {"Body": io.BytesIO((json.dumps(lone()) + "\n").encode())}
    got = gate_miss_moments(JourneysS3(), "b", "G", [Path("20261008-103635.mkv"), Path("20261009-000000.mkv")], cams)
    assert got == {"cam3": [97.0, 109.0]}, got
    assert gate_miss_moments(object(), "b", "G", [Path("20261008-103635.mkv")], cams) == {}
    classify_now = datetime(2026, 9, 27, 12)
    classify_default = "20260925-120000"
    assert classify_since(classify_now, {}) == classify_default
    assert classify_since(classify_now + timedelta(hours=1), {}) == "20260925-130000"
    assert classify_since(classify_now, {"classify_from": "20260926-000000"}) == "20260926-000000"
    assert classify_since(classify_now, {"classify_from": "20260924-000000"}) == classify_default
    assert classify_since(classify_now, {"classify_from": "2026092-000000"}) == classify_default
    assert classify_since(classify_now, {"classify_from": None}) == classify_default
    assert classify_since(classify_now, {"classify_from": "20260927-130000"}) == classify_default
    assert classify_since(classify_now.replace(tzinfo=ZoneInfo(TZ)),
                          {"classify_from": "20260927-130000"}) == classify_default
    floor = classify_since(classify_now, {"classify_from": "20260926-120000"})
    assert classify_since(classify_now, {"classify_from": "20260925-120000"}, floor) == floor
    assert classify_since(classify_now, {"classify_from": "bad"}, floor) == floor
    assert classify_since(classify_now, {}, floor) == floor
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
    assert not promote({"map50": 0.9}, {"map50": float("inf")}), "an unscored champion keeps its slot"
    assert promote_attrs({"mean_acc": 0.5}, None) and promote_attrs({"mean_acc": 0.1}, {"mean_acc": None})
    assert promote_attrs({"mean_acc": 0.88}, {"mean_acc": 0.87})
    assert not promote_attrs({"mean_acc": 0.87}, {"mean_acc": 0.87}), "a tie is not an improvement"
    assert not promote_attrs(None, {"mean_acc": 0.87}), "no report, no promotion"
    assert local_path("site1/cam3/20260827-100000.mkv").parent.name == "cam3", \
        "the camera must be the parent directory, or every camera samples as one"
    assert wanted_classes({"a": 5, "b": 300, "c": 299, "d": 0}, target=300) == ["a", "c", "d"]
    assert wanted_classes({"a": 5}, target=0) == [], "a floor of 0 hunts nothing"
    assert wanted_classes({"a": 5, "b": 9, "c": 1, "d": 7}, target=300, top=2) == ["a", "c"], \
        "only the thinnest few are hunted at once"
    assert gate_of("site1") == "RDA-TG-KTB", "the Katuba box still calls itself site1"
    assert gate_of("RDA-TG-KTB") == "RDA-TG-KTB", "a gate already named for itself"
    # Oldest unclassified first, across cameras, since the cutoff — newest-first left
    # permanent holes when another pass held the lock through the newest hour.
    got = pick_classify(keys, classified=["site1/cam3/20260827-100000.mkv"],
                        since="20260101-000000", per_pass=2)
    assert got == ["RDA-TG-KTB/north/20260827-100000.mkv",
                   "site1/cam3/20260827-101000.mkv"], got
    assert pick_classify(keys, classified=[], since="20260101-000000")[0].endswith("100000.mkv"), \
        "oldest first"
    assert pick_classify(keys, classified=keys, since="20260101-000000") == [], \
        "everything classified: nothing to do"
    assert pick_classify(keys, classified=[], since="20990101-000000") == [], \
        "since excludes older segments"
    assert pick_classify(keys, classified=[], since="20260101-000000", per_pass=None) == \
        sorted((k for segs in cameras(keys).values() for k in segs), key=lambda k: (Path(k).stem, k)), \
        "per_pass=None: the whole window, unbounded"
    # Live clips get the first slots; the oldest backlog keeps all remaining slots.
    lane_now = datetime(2026, 9, 27, 12).timestamp()  # same local clock as cam_and_start
    lane_since = "20260925-120000"
    old = [f"site1/cam3/20260926-00{i:02d}00.mkv" for i in range(CLASSIFY_PER_PASS)]
    fresh = ["site1/cam4/20260927-112000.mkv", "site1/cam3/20260927-110000.mkv"]

    def round_picks(ks, classified=(), failed=()):
        live = live_lane(ks, classified, lane_since, lane_now, failed)
        drain = pick_classify(ks, classified, lane_since, per_pass=None)
        return list(dict.fromkeys(live + [k for k in drain if k not in failed]))[:CLASSIFY_PER_PASS]

    assert round_picks(old + fresh) == fresh + old[:CLASSIFY_PER_PASS - len(fresh)], \
        "fresh cameras newest first, then oldest backlog, within the total cap"
    assert live_lane(old + fresh, fresh[:1], lane_since, lane_now) == fresh[1:], \
        "already classified fresh footage is not picked again"
    assert round_picks(old + fresh, failed=fresh[:1]) == fresh[1:] + old[:CLASSIFY_PER_PASS - 1], \
        "failed live footage is skipped by both lanes"
    assert round_picks(old) == pick_classify(old, [], lane_since), "no live footage: same oldest-first drain"
    assert round_picks(fresh) == fresh, "live picks are not duplicated in the drain"
    earlier = "site1/cam3/20260927-105000.mkv"
    assert live_lane(fresh + [earlier], [], lane_since, lane_now, limit=3) == fresh + [earlier], \
        "round-robin continues to each camera's next newest segment"
    assert live_lane(fresh + [earlier], fresh, lane_since, lane_now) == [earlier], \
        "pick the newest unclassified segment, even if a later one is already done"
    assert live_lane([earlier, fresh[1]], [], lane_since, lane_now, failed=fresh) == [earlier], \
        "a failed newest segment does not hide the next eligible one"
    assert live_lane(fresh, [], "20260927-120000", lane_now) == [], "since still applies"
    assert live_lane(["site1/cam3/20260927-060000.mkv"], [], lane_since, lane_now) == [], \
        "exactly LIVE_HOURS old is outside the render window"
    many = [f"gate{i}/cam/20260927-110000.mkv" for i in range(CLASSIFY_PER_PASS + 1)]
    crowded = round_picks(old + many)
    assert crowded == sorted(many, reverse=True)[:2] + old[:CLASSIFY_PER_PASS - 2], \
        "default live lane is capped at two, then oldest backlog fills the rest"
    lanes = ["site1/camA/20260927-115000.mkv", "site1/camA/20260927-114000.mkv",
             "site1/camA/20260927-113000.mkv", "site1/camB/20260927-112000.mkv",
             "site1/camB/20260927-111000.mkv", "site1/camB/20260927-110000.mkv"]
    expected_lanes = [lanes[0], lanes[3], lanes[1], lanes[4], lanes[2], lanes[5]]
    assert live_lane(lanes, [], lane_since, lane_now) == expected_lanes[:2], \
        "default live lane caps at two round-robin picks"
    assert live_lane(lanes, [], lane_since, lane_now, limit=6) == expected_lanes, \
        "cameras take turns through all three newest clips each"
    assert live_lane(lanes, [], lane_since, lane_now, limit=3) == expected_lanes[:3]
    assert live_lane(lanes, [], lane_since, lane_now, limit=0) == []
    edge = ["site1/cam3/20260927-063001.mkv", "site1/cam3/20260927-063000.mkv",
            "site1/cam3/20260927-062959.mkv", "site1/cam3/20260927-120001.mkv"]
    assert live_lane(edge, [], lane_since, lane_now) == [edge[0]], \
        "30-minute margin is strict; older and future clips are ineligible"
    import tempfile
    with tempfile.TemporaryDirectory(prefix="selfloop-bad-key-") as tmp:
        prefix = Path(tmp).name
    bad, sibling = f"{prefix}/cam3/invalid.mkv", f"{prefix}/cam3/20260927-110000.mkv"
    assert len(bad.split("/")) == 3 and not Path(prefix).exists()
    assert live_lane([bad, sibling], [], lane_since, lane_now) == [sibling], \
        "an unparseable key must not strand a valid sibling in its camera"
    assert paused(keys, {"curation_paused": ["site1"]}) == \
        [k for k in keys if not k.startswith("site1/")], "paused gate's keys dropped"
    assert paused(keys, {"curation_paused": ["RDA-TG-KTB"]}) == \
        [k for k in keys if not k.startswith(("site1/", "RDA-TG-KTB/"))], \
        "paused by gate id: site1 is RDA-TG-KTB too"
    assert paused(keys, {}) == keys, "nothing paused by default"
    cov = coverage(keys, classified=["site1/cam3/20260827-100000.mkv"], since="20260101-000000")
    assert cov == {"20260827-10": {"site1/cam3": (1, 3), "RDA-TG-KTB/north": (0, 1)}}, cov
    assert coverage(keys, classified=[], since="20990101-000000") == {}, "since excludes the whole hour"
    man = coverage_manifests(keys, classified=["site1/cam3/20260827-100000.mkv"], since="20260101-000000")
    assert set(man) == {("RDA-TG-KTB", "20260827")}, "site1 and RDA-TG-KTB are the same gate"
    cams = man[("RDA-TG-KTB", "20260827")]
    assert cams["cam3"]["recorded"] == ["20260827-100000", "20260827-101000", "20260827-102000"] \
        and cams["cam3"]["classified"] == ["20260827-100000"], cams["cam3"]
    assert cams["north"] == {"recorded": ["20260827-100000"], "classified": []}, cams["north"]
    # since cuts mid-day (only 102000 is >= it) but the day it selects is still filled whole,
    # or the oldest day in a window reads as fully covered when it is really mostly missing.
    mid = coverage_manifests(keys, classified=["site1/cam3/20260827-100000.mkv"], since="20260827-101500")
    assert mid[("RDA-TG-KTB", "20260827")]["cam3"]["recorded"] == \
        ["20260827-100000", "20260827-101000", "20260827-102000"], "day filled whole, not clipped by since"
    def epoch(stem):     # cam_and_start's own (naive, system-local) clock
        return datetime.strptime(stem, "%Y%m%d-%H%M%S").timestamp()

    hcams = [{"name": "cam3", "handoff": {"camera": "cam4"}}, {"name": "cam4", "handoff": {"camera": "cam3"}}]
    # cam3: two classified segments then a reconnect-shortened one, unclassified.
    # cam4: recorded once, a month before cam3's newest — dead, not merely behind.
    h_keys = ["site1/cam3/20260827-100000.mkv", "site1/cam3/20260827-101000.mkv",
              "site1/cam3/20260827-102017.mkv", "site1/cam4/20260101-000000.mkv"]
    h_done = ["site1/cam3/20260827-100000.mkv", "site1/cam3/20260827-101000.mkv"]
    h, ab = horizon(h_keys, h_done, "RDA-TG-KTB", "20260827", since="20260101-000000", cam_cfg=hcams)
    assert h["cam3"] == epoch("20260827-102017"), \
        "unclassified segment's own (non-aligned) start, never the previous one + 600"
    assert h["cam4"] == float("inf"), "silent for months next to cam3's newest: dead, not blocking"
    assert ab == 0
    # The previous day's last few minutes spill into this day's handoff window; left
    # unclassified, it blocks the day exactly as one of the day's own segments would.
    spilled = h_keys + ["site1/cam3/20260826-235500.mkv"]
    h2, _ = horizon(spilled, h_done, "RDA-TG-KTB", "20260827", since="20260101-000000", cam_cfg=hcams)
    assert h2["cam3"] == epoch("20260826-235500"), "an unclassified spillover segment blocks the day"
    # A segment too old to ever be classified (before `since`) is abandoned, not a block:
    # the camera's horizon falls back to its latest recorded segment instead.
    stale = ["site1/cam3/20260827-050000.mkv", "site1/cam3/20260827-100000.mkv"]
    h3, ab3 = horizon(stale, classified=["site1/cam3/20260827-100000.mkv"],
                      gate="RDA-TG-KTB", day="20260827", since="20260827-060000", cam_cfg=hcams)
    assert h3["cam3"] == epoch("20260827-100000") and ab3 == 1, (h3, ab3)
    # Two bucket prefixes for the same gate+camera (site1 and its RDA-TG-KTB alias) merge —
    # cam4 has nothing at all under either, and is thus dead too (never blocks by absence).
    merged = ["site1/cam3/20260827-100000.mkv", "RDA-TG-KTB/cam3/20260827-103000.mkv"]
    hm, _ = horizon(merged, classified=merged, gate="RDA-TG-KTB", day="20260827",
                    since="20260101-000000", cam_cfg=hcams)
    assert hm["cam3"] == epoch("20260827-103000") and hm["cam4"] == float("inf"), hm
    # A > GAP_S gap between two segments: recent (the segment after it is < DEAD_AFTER_S old
    # as of `now`) blocks at the segment BEFORE the gap's own start — never +600, since a
    # reconnect can cut that segment short and the missing footage may start well earlier.
    # The same gap, once `now` has moved far past it, is a real outage instead: no block.
    gapped = ["site1/cam3/20260827-100000.mkv", "site1/cam3/20260827-102500.mkv"]   # 900s gap
    h5, _ = horizon(gapped, classified=gapped, gate="RDA-TG-KTB", day="20260827",
                    since="20260101-000000", cam_cfg=hcams, now=epoch("20260827-102500") + 60)
    assert h5["cam3"] == epoch("20260827-100000"), "a recent gap blocks at the segment before it"
    h6, _ = horizon(gapped, classified=gapped, gate="RDA-TG-KTB", day="20260827",
                    since="20260101-000000", cam_cfg=hcams)   # now defaults to the real clock
    assert h6["cam3"] == epoch("20260827-102500"), "an old gap doesn't block: latest recorded wins"

    # c/d/r: c is a lone chain that would clear the cutoff on its own; d is still open
    # (its own max t1 hasn't cleared the cutoff yet) and started before c did. Freezing c
    # while d is still open and could grow to reclaim c's would-be partner is exactly the
    # double-count the watermark exists to prevent — so c must not freeze yet.
    c = {"id": "c", "members": [{"t0": 100, "t1": 110}], "dwell_s": 10}
    d = {"id": "d", "members": [{"t0": 90, "t1": 999_800}], "dwell_s": 5}    # 999_800+300 > cutoff: still open
    assert finalize([c, d], cutoff=1_000_000) == [], "c held back: d, which started earlier, is still open"
    # Once d itself clears (or is dropped), c is free to freeze.
    assert finalize([c], cutoff=1_000_000) == [c], "with d gone, nothing holds c back"
    # A journey parked past PARKED_S is still open by the margin test, but excluded from the
    # watermark: it can't hold c back the way d did, even though it hasn't cleared itself.
    d_parked = {"id": "dp", "members": [{"t0": 90, "t1": 999_800}], "dwell_s": PARKED_S + 1}
    assert finalize([c, d_parked], cutoff=1_000_000) == [c], "a parked journey can't stall the watermark"
    # cleared(): d's ts lands on the NEIGHBOURING day (it's not in `provisional`, only in
    # `built`) but must still hold c back — the regression the midnight change introduced,
    # where the watermark only saw the day's own journeys and missed a boundary d entirely.
    assert cleared(built=[c, d], provisional=[c], cutoff=1_000_000) == [], \
        "d not owned by this day, but still open: c held back all the same"
    assert cleared(built=[c], provisional=[c], cutoff=1_000_000) == [c], "no d at all: c freezes"

    today0819 = datetime(2026, 8, 26).date()
    assert prune_open_days(set(), {("G", "2026-08-19")}, today0819) == set(), \
        "not a candidate: nothing to keep"
    assert prune_open_days({("G", "2026-08-19")}, {("G", "2026-08-19")}, today0819) == \
        {("G", "2026-08-19")}, "7 days old and still open: kept"
    assert prune_open_days({("G", "2026-08-19")}, set(), today0819) == set(), \
        "journeys_pass reports nothing provisional left: frozen, stop watching"
    assert prune_open_days({("G", "2026-08-19")}, {("G", "2026-08-19")},
                           datetime(2026, 8, 27).date()) == set(), \
        "8 days old with journeys still open: dropped (loudly) — lost counts"
    assert not classify_behind({"classify_backlog": {"at": now(), "left": 0}}), "nothing left"
    assert classify_behind({"classify_backlog": {"at": now(), "left": 3}}), "fresh backlog"
    from datetime import timedelta
    stale_at = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert not classify_behind({"classify_backlog": {"at": stale_at, "left": 3}}), "stale backlog"
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
    # Live-render gate: only a segment started within LIVE_HOURS is worth encoding.
    now_ts = datetime.now(timezone.utc).timestamp()
    assert now_ts - (now_ts - 3600) < LIVE_HOURS * 3600, "1h old: still live"
    assert not (now_ts - (now_ts - (LIVE_HOURS + 1) * 3600) < LIVE_HOURS * 3600), \
        f"{LIVE_HOURS + 1}h old: backlog, not live"
    # Retention: only clips past ANNOTATED_KEEP_DAYS are selected for deletion.
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=ANNOTATED_KEEP_DAYS)).strftime("%Y%m%d-%H%M%S")
    fresh = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y%m%d-%H%M%S")
    stale = (datetime.now(timezone.utc) - timedelta(days=ANNOTATED_KEEP_DAYS + 1)).strftime("%Y%m%d-%H%M%S")
    assert fresh >= cutoff and stale < cutoff, "the stems the prune keeps vs. drops"
    lock_check()
    publish_check()
    listing_check()
    journeys_check()
    suggest_check()
    curation_check()
    lag = camera_lag(["site1/cam/20260927-110000.mkv", "site1/cam/20260927-111000.mkv",
                      "site1/cam/20260927-112000.mkv", "site1/other/20260927-110000.mkv"],
                     ["site1/cam/20260927-110000.mkv", "site1/cam/20260927-111000.mkv"])["RDA-TG-KTB"]
    assert lag["cam"][0] - lag["cam"][1] == 600 and lag["other"][1] is None
    assert lag["cam"][2] == lag["cam"][0] and lag["other"][2] == lag["other"][0]   # oldest hole, not newest
    print("selfloop self-check ok: cameras found under any gate prefix, newest unsampled segments "
          "picked per camera, training triggers on the cumulative threshold, promotion needs a "
          "strictly better reference score (detector) or mean val accuracy (attributes) unless "
          "there is no comparable champion, unclassified segments drained oldest first "
          "under their gate's id")


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
        refresh_appearance()
    elif a[0] == "audit":
        audit_pass(a[1] if len(a) > 1 else None)
    elif a[0] == "status":
        status()
    elif a[0] == "adopt" and len(a) == 2:
        adopt(a[1])
    elif a[0] == "adopt-attrs" and len(a) == 2:
        adopt_attrs(a[1])
        refresh_appearance()
    else:
        sys.exit(__doc__)
