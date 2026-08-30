#!/usr/bin/env python3
"""Seed a cloud curation instance from the local dataset, and harvest its output.

  python dataset_sync.py push [--prefix curation-test/]   local dataset -> bucket
  python dataset_sync.py pull [--ledgers|--config]        bucket -> local dataset
  python dataset_sync.py models                           bucket models/champion*.pt -> dataset/
  python dataset_sync.py                                  self-check

Credentials come from OFFLOAD_ACCOUNT_ID / OFFLOAD_ACCESS_KEY_ID /
OFFLOAD_SECRET_ACCESS_KEY / OFFLOAD_BUCKET when set — Railway has no config.yaml —
otherwise from config.yaml's offload block, the same R2 bucket offload.py uploads to.
--prefix redirects both directions at once, for trying things somewhere harmless.
--config fetches only what a curation node must have in hand before it starts: it
refuses to boot without curators.yaml, so that one file is worth waiting for at
startup while the samples stream in behind it.

ponytail: sync without delete, deliberately. Push seeds, pull harvests, and neither
side ever removes the other's files; real two-way sync needs tombstones and conflict
rules, which is a different program nobody has asked for.
"""

import json
import os
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
DATASET = ROOT / "dataset"
PREFIX = "curation/"
PROGRESS = 200
# What a curation instance needs to start work...
PUSH = ("pending/images", "pending/labels", "pending/attrs", "pending/suggest", "gold",
        "classes.txt", "attributes.yaml", "curators.yaml", "assignments.yaml", "trusted.yaml",
        "reference.txt", "external.json")
# ...the small part of that a node must have IN HAND before it can serve anything...
CONFIG = ("curators.yaml", "classes.txt", "attributes.yaml", "assignments.yaml",
          "trusted.yaml", "gold", "reference.txt", "external.json")
# ...and what it produces: the daily harvest.
LEDGERS = ("approved/images", "approved/labels", "approved/attrs",
           "audit.jsonl", "scores.jsonl", "gold-served.jsonl")


def creds():
    """Env first: a cloud instance has variables, not a config file."""
    cfg = {}
    path = ROOT / "config.yaml"
    if path.exists():
        cfg = (yaml.safe_load(path.read_text()) or {}).get("offload") or {}
    return {"account_id": os.environ.get("OFFLOAD_ACCOUNT_ID") or cfg.get("account_id"),
            "access_key_id": os.environ.get("OFFLOAD_ACCESS_KEY_ID") or cfg.get("access_key_id"),
            "secret_access_key": (os.environ.get("OFFLOAD_SECRET_ACCESS_KEY")
                                  or cfg.get("secret_access_key")),
            "bucket": (os.environ.get("OFFLOAD_BUCKET") or cfg.get("bucket")
                       or "fieldkit-recordings")}


def client(o):
    """The same R2 client offload.py builds."""
    missing = [k for k in ("account_id", "access_key_id", "secret_access_key") if not o.get(k)]
    if missing:
        sys.exit("no R2 credentials — set OFFLOAD_* or config.yaml offload; missing "
                 + ", ".join(missing))
    try:
        import boto3
    except ImportError:
        sys.exit("dataset_sync needs boto3 — pip install boto3")
    from botocore.config import Config
    # Same guard offload.py carries: newer botocore checksums every request and response
    # by default, and R2 rejects the first on uploads and fails the second on large
    # (multipart) downloads — a 100 MB segment came back FlexibleChecksumError.
    return boto3.client(
        "s3", endpoint_url=f"https://{o['account_id']}.r2.cloudflarestorage.com",
        aws_access_key_id=o["access_key_id"],
        aws_secret_access_key=o["secret_access_key"], region_name="auto",
        config=Config(request_checksum_calculation="when_required",
                      response_checksum_validation="when_required"))


def remote(cl, bucket, prefix):
    """{key: size} under the prefix — one paginated list beats a HEAD per file."""
    out = {}
    for page in cl.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            out[obj["Key"]] = obj["Size"]
    return out


def walk(root, names):
    """(key-relative path, file) for each named file or directory that exists."""
    for n in names:
        p = root / n
        if p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file():
                    yield f.relative_to(root).as_posix(), f
        elif p.is_file():
            yield n, p


WORKERS = 8


def transfer(fn, items, verb):
    """Run fn(local, key) over items on a small thread pool; -> count done."""
    from concurrent.futures import ThreadPoolExecutor
    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for _ in ex.map(lambda it: fn(*it), items):
            done += 1
            if done % PROGRESS == 0:
                print(f"  {verb} {done}...", flush=True)
    return done


def push(cl, bucket, prefix=PREFIX, root=DATASET, names=PUSH):
    """Upload what curation needs. Same name and same size is assumed the same file:
    these are write-once samples, never edited in place. Pending samples go further —
    see once(): an id already in the bucket is left exactly as it is, so two nodes
    watching one camera contribute one frame between them rather than two."""
    have = remote(cl, bucket, prefix)
    todo, skipped = [], 0
    for rel, f in walk(Path(root), names):
        key = prefix + rel
        if always(rel) or key not in have:
            todo.append((f, key))
        elif once(rel) or have[key] == f.stat().st_size:
            skipped += 1
        else:
            todo.append((f, key))
    # Thousands of small files over a home uplink: latency-bound, so parallel
    # transfers are the whole speed-up. boto3 clients are thread-safe for this.
    sent = transfer(lambda f, key: cl.upload_file(str(f), bucket, key), todo, "pushed")
    print(f"push: {sent} uploaded, {skipped} already there ({prefix} on {bucket})")
    return sent, skipped


def once(rel):
    """Pending samples are written once and never replaced, whoever holds the newer copy.

    Two nodes watching one camera now mint the same id for the same moment (see
    detect.sample_stem), so their frames meet on one key — near-identical pictures of the
    same vehicle, a second or two apart. Either is a fine sample and the first one there
    is the copy every curator sees. Uploading the other over it would swap the image out
    from under whoever is mid-label, leaving their boxes describing a frame that is gone.
    So for pending, the key existing at all is reason enough to leave it be."""
    return rel.startswith("pending/")


def always(rel):
    """Config is compared by name and size like everything else, and a one-character edit
    — "2" to "3" in a default — keeps the size identical, so the change would never move.
    These files are a few KB: send them every pass and let content decide, not length."""
    return rel.split("/", 1)[0] in CONFIG or rel in CONFIG


def have(dest, size):
    """Is the local copy already good? Same size is the same file: these are write-once
    samples, and a ledger of the same size holds the same lines. A ledger of a DIFFERENT
    size is fetched and merged, not replaced — see merge_jsonl."""
    return dest.exists() and dest.stat().st_size == size


def merge_jsonl(dest, extra):
    """Fold `extra` into the append-only ledger at `dest`, keeping every line either
    side had. Both sides write these: the curation instance records the team's work
    while the operator's node records its own, so replacing one with the other drops a
    week of somebody's history — and payroll is computed off exactly these files.

    Exact-string dedup, because a byte-identical line IS the same record written once.
    That makes ledger sync commutative — the union is the same whoever syncs first, so
    both sides may push and pull whenever they like."""
    lines, last = [], ""
    for line in dict.fromkeys(dest.read_text().splitlines() + extra.read_text().splitlines()):
        try:
            r = json.loads(line)
            last = (r.get("ts") if isinstance(r, dict) else None) or last
        except ValueError:
            pass          # a torn line inherits the stamp above it, so it stays put
        lines.append((last, line))
    lines.sort(key=lambda p: p[0])      # stable: one timestamp keeps the order read in
    tmp = dest.with_name(dest.name + ".merging")
    tmp.write_text("".join(f"{line}\n" for _, line in lines))
    os.replace(tmp, dest)               # a torn ledger would be worse than a stale one


def fetch(cl, bucket, dest, key):
    """One object down. An existing ledger is merged; everything else is write-once."""
    if dest.suffix == ".jsonl" and dest.exists():
        extra = dest.with_name(dest.name + ".incoming")
        cl.download_file(bucket, key, str(extra))
        try:
            merge_jsonl(dest, extra)
        finally:
            extra.unlink(missing_ok=True)
    else:
        cl.download_file(bucket, key, str(dest))


FINISHED = ("approve", "discard", "review")   # last-action-wins: unapprove/reject reopen


def consumed(root):
    """Sample ids this node has already dealt with, from its own audit ledger.

    A pending sample that was approved or discarded is GONE from pending locally, but
    its original copy still sits in the bucket — so a plain pull downloads it straight
    back and the labeller meets their own finished work again. The ledger is the record
    of what happened to each sample and it syncs both ways, so both nodes agree. An
    unapprove puts the sample back in the queue, hence last-action-wins rather than
    "ever approved". A review is an approval re-checked — the frame is still filed, so
    it must not come back either."""
    last = {}
    try:
        lines = (Path(root) / "audit.jsonl").read_text().splitlines()
    except OSError:
        return set()
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("id") and r.get("action"):
            last[r["id"]] = r["action"]
    return {i for i, a in last.items() if a in FINISHED}


def sweep_consumed(root, in_bucket=()):
    """Drop pending copies of samples that are finished — here or on any other node.

    The ledger merges both ways, so an approval made on the curation instance reaches the
    operator's laptop within a sync; but the FILES of a held approval sit in that
    instance's holding/ tree, which never syncs, and the old rule ("only sweep once the
    approved copy is on disk") left the laptop's pending copy alive. Every probationary
    curator's frame therefore came round again for whoever labelled locally.

    Deletion stays deliberate: a pending copy goes only when some other copy is known to
    exist — approved/ or holding/ here, or the original still in the bucket (`in_bucket`,
    the pending stems the last listing saw). A discard is already an order to destroy."""
    root = Path(root)
    last = {}
    try:
        for line in (root / "audit.jsonl").read_text().splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("id") and r.get("action"):
                last[r["id"]] = r["action"]
    except OSError:
        return 0
    gone = 0
    for sid, action in last.items():
        if action not in FINISHED:
            continue
        if action != "discard" and sid not in in_bucket and not any(
                (root / tree / "labels" / f"{sid}.txt").exists()
                for tree in ("approved", "holding")):
            continue                       # no other copy anywhere we can see: leave it
        for sub, ext in (("images", ".jpg"), ("labels", ".txt"),
                         ("attrs", ".json"), ("suggest", ".json")):
            f = root / "pending" / sub / f"{sid}{ext}"
            if f.exists():
                f.unlink()
                gone += 1
    return gone


def pull(cl, bucket, prefix=PREFIX, root=DATASET, names=None):
    """Download the curation instance's work. names limits it to the daily harvest."""
    root = Path(root)
    done = consumed(root)
    todo, skipped = [], 0
    listing = remote(cl, bucket, prefix)
    for key, size in sorted(listing.items()):
        rel = key[len(prefix):]
        if not rel or (names and not rel.startswith(tuple(names))):
            continue
        if rel.startswith("pending/") and Path(rel).stem in done:
            skipped += 1                  # already labelled: do not hand it back
            continue
        dest = root / rel
        if have(dest, size) and not always(rel):
            skipped += 1
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        todo.append((dest, key))
    got = transfer(lambda dest, key: fetch(cl, bucket, dest, key), todo, "pulled")
    swept = sweep_consumed(root, {Path(k).stem for k in listing
                                  if k.startswith(prefix + "pending/images/")})
    print(f"pull: {got} downloaded, {skipped} already local"
          + (f", {swept} finished copies cleared" if swept else "") + f" (-> {root})")
    return got, skipped


def selfcheck():
    import tempfile

    class FakeS3:
        """Enough S3 for both directions: list, upload, download."""

        def __init__(self, objects=None):
            self.objects = dict(objects or {})
            self.uploaded = []

        def get_paginator(self, _op):
            return self

        def paginate(self, Bucket, Prefix):
            yield {"Contents": [{"Key": k, "Size": len(v)} for k, v in self.objects.items()
                                if k.startswith(Prefix)]}

        def upload_file(self, path, Bucket, Key):
            self.objects[Key] = Path(path).read_bytes()
            self.uploaded.append(Key)

        def download_file(self, Bucket, Key, path):
            Path(path).write_bytes(self.objects[Key])

    src = Path(tempfile.mkdtemp())
    (src / "pending" / "images").mkdir(parents=True)
    (src / "pending" / "images" / "a.jpg").write_bytes(b"aaa")
    (src / "pending" / "images" / "b.jpg").write_bytes(b"bb")
    (src / "classes.txt").write_text("car\n")
    (src / "archive").mkdir()                    # not in PUSH: curation never needs it
    (src / "archive" / "old.jpg").write_bytes(b"x")

    cl = FakeS3()
    assert push(cl, "buck", "curation/", src) == (3, 0)
    assert sorted(cl.objects) == ["curation/classes.txt", "curation/pending/images/a.jpg",
                                  "curation/pending/images/b.jpg"], sorted(cl.objects)
    # Samples are write-once, so same name and size means skip; config is re-sent every
    # pass because a one-character edit does not change its length.
    assert push(cl, "buck", "curation/", src) == (1, 2), "samples skip, config always moves"
    (src / "classes.txt").write_text("van\n")        # same size, different content
    push(cl, "buck", "curation/", src)
    assert cl.objects["curation/classes.txt"] == b"van\n", "a same-size config edit must propagate"
    (src / "classes.txt").write_text("car\ntruck\n")
    assert push(cl, "buck", "curation/", src) == (1, 2), "a changed file goes again"

    # Harvest: the curation instance's output lands under a fresh local dataset.
    cl.objects.update({"curation/approved/images/a.jpg": b"aaa",
                       "curation/approved/labels/a.txt": b"0 0.5 0.5 0.1 0.1\n",
                       "curation/audit.jsonl": b"{}\n",
                       "curation/scores.jsonl": b"{}\n"})
    dst = Path(tempfile.mkdtemp())
    got, _ = pull(cl, "buck", "curation/", dst)
    assert got == 7, got                          # everything under the prefix
    assert (dst / "approved" / "images" / "a.jpg").read_bytes() == b"aaa"
    assert (dst / "pending" / "images" / "b.jpg").exists()
    # Same asymmetry on the way back: samples already here are left alone, config is
    # re-read every pass so an edit made on the other node cannot hide behind its length.
    assert pull(cl, "buck", "curation/", dst) == (1, 6), "samples left alone, config re-read"

    # --ledgers: the fast daily harvest skips the pending pile it seeded itself.
    day = Path(tempfile.mkdtemp())
    assert pull(cl, "buck", "curation/", day, LEDGERS) == (4, 0)
    assert not (day / "pending").exists(), "ledger harvest pulled the seed back down"
    assert (day / "audit.jsonl").exists() and (day / "approved" / "labels" / "a.txt").exists()

    # --config: the handful of files a node needs before it can answer at all.
    cfg = Path(tempfile.mkdtemp())
    assert pull(cl, "buck", "curation/", cfg, CONFIG) == (1, 0)
    assert (cfg / "classes.txt").exists() and not (cfg / "pending").exists()

    # An append-only ledger is never overwritten by a shorter remote copy: the node
    # appends between its push and its pull, and those lines are the team's work.
    (day / "audit.jsonl").write_text('{}\n{"kind": "approve"}\n')
    grown = (day / "audit.jsonl").stat().st_size
    pull(cl, "buck", "curation/", day, LEDGERS)
    assert (day / "audit.jsonl").stat().st_size == grown, "pull truncated a growing ledger"

    # Both sides append to a ledger, so pull merges it: local B,A and remote B,C come
    # back as the union, by ts, with the duplicate B kept once and the torn line still
    # sitting where it was found.
    a = '{"ts": "2026-08-24T09:00:00+00:00", "who": "curator01", "action": "approve"}'
    b = '{"ts": "2026-08-25T09:00:00+00:00", "who": "curator02", "action": "approve"}'
    c = '{"ts": "2026-08-26T09:00:00+00:00", "who": "jonah", "action": "review"}'
    torn = '{"ts": "2026-08-25T09:00:01+00:00", "who": "curat'
    cl.objects["curation/audit.jsonl"] = f"{b}\n{c}\n".encode()
    (day / "audit.jsonl").write_text(f"{b}\n{torn}\n{a}\n")
    pull(cl, "buck", "curation/", day, LEDGERS)
    assert (day / "audit.jsonl").read_text() == f"{a}\n{b}\n{torn}\n{c}\n", (
        (day / "audit.jsonl").read_text())
    assert not list(day.glob("*.incoming")), "the download side-file must not survive"

    # A non-ledger is still replaced wholesale — merging is for append-only files only.
    (day / "classes.txt").write_text("car\n")
    pull(cl, "buck", "curation/", day, CONFIG)
    assert (day / "classes.txt").read_text() == "car\ntruck\n", "a non-ledger must replace"

    # Nothing is ever removed: a file only one side has survives both directions.
    (dst / "pending" / "images" / "local-only.jpg").write_bytes(b"keep")
    pull(cl, "buck", "curation/", dst)
    assert (dst / "pending" / "images" / "local-only.jpg").exists(), "pull deleted a local file"

    # Env vars win over config.yaml — the cloud side has no config file at all.
    keep = {k: os.environ.get(k) for k in ("OFFLOAD_ACCOUNT_ID", "OFFLOAD_BUCKET")}
    os.environ["OFFLOAD_ACCOUNT_ID"] = "env-account"
    os.environ["OFFLOAD_BUCKET"] = "env-bucket"
    try:
        o = creds()
        assert o["account_id"] == "env-account" and o["bucket"] == "env-bucket", o
    finally:
        for k, v in keep.items():
            os.environ.pop(k, None) if v is None else os.environ.update({k: v})
    assert creds()["bucket"], "a bucket name is always resolvable"

    # A finished sample must never come back round. This is the bug that had one
    # labeller approving the same frames twice: pending copies live on in the bucket.
    back = Path(tempfile.mkdtemp())
    cl2 = FakeS3({"curation/pending/images/s1.jpg": b"aaa",
                  "curation/pending/labels/s1.txt": b"0 0.5 0.5 0.1 0.1\n",
                  "curation/pending/images/s2.jpg": b"bbb",
                  "curation/pending/labels/s2.txt": b"0 0.5 0.5 0.1 0.1\n"})
    (back / "audit.jsonl").parent.mkdir(parents=True, exist_ok=True)
    (back / "audit.jsonl").write_text(
        '{"ts": "t", "who": "a", "id": "s1", "action": "approve"}\n')
    (back / "approved" / "labels").mkdir(parents=True)
    (back / "approved" / "labels" / "s1.txt").write_text("0 0.5 0.5 0.1 0.1\n")
    pull(cl2, "buck", "curation/", back)
    assert not (back / "pending" / "images" / "s1.jpg").exists(), "approved frame came back"
    assert (back / "pending" / "images" / "s2.jpg").exists(), "untouched frame must arrive"
    # A reviewer re-checking that approval (search -> edit -> save) writes a "review" line
    # after the "approve": still finished, the frame must not come round again.
    with (back / "audit.jsonl").open("a") as f:
        f.write('{"ts": "t", "who": "r", "id": "s1", "action": "review"}\n')
    assert "s1" in consumed(back), "a reviewed approval is still finished"
    pull(cl2, "buck", "curation/", back)
    assert not (back / "pending" / "images" / "s1.jpg").exists(), "reviewed frame came back"

    # An earlier pull left a duplicate behind; the sweep clears it, but only because the
    # approved copy is really there.
    (back / "pending" / "images" / "s1.jpg").write_bytes(b"aaa")
    assert sweep_consumed(back) == 1
    (back / "approved" / "labels" / "s1.txt").unlink()
    (back / "pending" / "images" / "s1.jpg").write_bytes(b"aaa")
    assert sweep_consumed(back) == 0, "no approved copy: the pending one is all there is"

    # s2 is approved on the OTHER node by a curator on probation: its files went to that
    # node's holding/, which never syncs, so nothing of it lands here but the audit line.
    # The bucket still holds the original, so this copy is not the last one — it goes,
    # and s2 stops coming round for whoever labels on this node.
    (back / "audit.jsonl").write_text(
        '{"ts": "t", "who": "a", "id": "s1", "action": "approve"}\n'
        '{"ts": "t", "who": "curator03", "id": "s2", "action": "approve", "held": true}\n')
    assert (back / "pending" / "images" / "s2.jpg").exists()
    pull(cl2, "buck", "curation/", back)
    assert not (back / "pending" / "images" / "s2.jpg").exists(), "held elsewhere, still queued here"
    assert not (back / "pending" / "labels" / "s2.txt").exists()
    # ...and a held approval made HERE counts the same way, holding/ being a real copy.
    (back / "holding" / "labels").mkdir(parents=True)
    (back / "holding" / "labels" / "s2.txt").write_text("0 0.5 0.5 0.1 0.1\n")
    (back / "pending" / "images" / "s2.jpg").write_bytes(b"bbb")
    assert sweep_consumed(back) == 1, "a holding copy is proof enough"

    # Two nodes, one camera. The box at the gate and a laptop re-ingesting that same
    # footage see the same vehicle two seconds apart on clocks that never agreed.
    import detect
    box_id = detect.sample_stem("RDA-TG-KTB", "katuba-north", 1787000702.4)
    mac_id = detect.sample_stem("RDA-TG-KTB", "katuba-north", 1787000700.0)
    assert box_id == mac_id, (box_id, mac_id)     # one moment, one id, one curation task

    two, at_gate, at_desk = FakeS3(), Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
    for where, frame in ((at_gate, b"live off the sub-stream"), (at_desk, b"decoded")):
        (where / "pending" / "images").mkdir(parents=True)
        (where / "pending" / "images" / f"{box_id}.jpg").write_bytes(frame)
    assert push(two, "buck", "curation/", at_gate) == (1, 0)
    # Different bytes AND a different length, so the size check alone would send it again
    # and a curator mid-label would watch the picture change under their boxes.
    assert push(two, "buck", "curation/", at_desk) == (0, 1), "second node overwrote the first"
    assert two.objects[f"curation/pending/images/{box_id}.jpg"] == b"live off the sub-stream"

    print("dataset_sync self-check ok: push skips unchanged, pull harvests, --ledgers and "
          "--config filter, ledgers merge instead of overwriting, neither side deletes, env "
          "creds win")


def models(cl, bucket, root=DATASET):
    """The crowned weights (selfloop adopt publishes them under models/) into dataset/,
    where config.yaml's detect_weights / attr_weights point on a console. Same size and
    name = same file, like push/pull; the .json beside each says which run it is."""
    got = []
    for name in ("champion.pt", "champion.json", "attrs-champion.pt", "attrs-champion.json"):
        key = "models/" + name
        try:
            size = cl.head_object(Bucket=bucket, Key=key)["ContentLength"]
        except Exception:
            continue                      # nothing crowned yet
        dest = root / name
        if dest.exists() and dest.stat().st_size == size:
            continue
        cl.download_file(bucket, key, str(dest))
        got.append(name)
    print(f"models: {', '.join(got) or 'up to date'}")
    return got


if __name__ == "__main__":
    args = sys.argv[1:]
    prefix = PREFIX
    if "--prefix" in args:
        i = args.index("--prefix")
        prefix = args.pop(i + 1).rstrip("/") + "/"
        args.pop(i)
    names = LEDGERS if "--ledgers" in args else CONFIG if "--config" in args else None
    args = [a for a in args if not a.startswith("-")]
    if not args:
        selfcheck()
    elif args[0] == "push":
        o = creds()
        push(client(o), o["bucket"], prefix)
    elif args[0] == "pull":
        o = creds()
        pull(client(o), o["bucket"], prefix, names=names)
    elif args[0] == "models":
        o = creds()
        models(client(o), o["bucket"])
    else:
        sys.exit(__doc__)
