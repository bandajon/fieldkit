#!/usr/bin/env python3
"""Seed a cloud curation instance from the local dataset, and harvest its output.

  python dataset_sync.py push [--prefix curation-test/]   local dataset -> bucket
  python dataset_sync.py pull [--ledgers|--config]        bucket -> local dataset
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
        "classes.txt", "attributes.yaml", "curators.yaml", "assignments.yaml")
# ...the small part of that a node must have IN HAND before it can serve anything...
CONFIG = ("curators.yaml", "classes.txt", "attributes.yaml", "assignments.yaml", "gold")
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
    return boto3.client(
        "s3", endpoint_url=f"https://{o['account_id']}.r2.cloudflarestorage.com",
        aws_access_key_id=o["access_key_id"],
        aws_secret_access_key=o["secret_access_key"], region_name="auto")


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
    these are write-once samples, never edited in place."""
    have = remote(cl, bucket, prefix)
    todo, skipped = [], 0
    for rel, f in walk(Path(root), names):
        if have.get(prefix + rel) == f.stat().st_size and not always(rel):
            skipped += 1
        else:
            todo.append((f, prefix + rel))
    # Thousands of small files over a home uplink: latency-bound, so parallel
    # transfers are the whole speed-up. boto3 clients are thread-safe for this.
    sent = transfer(lambda f, key: cl.upload_file(str(f), bucket, key), todo, "pushed")
    print(f"push: {sent} uploaded, {skipped} already there ({prefix} on {bucket})")
    return sent, skipped


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


def consumed(root):
    """Sample ids this node has already dealt with, from its own audit ledger.

    A pending sample that was approved or discarded is GONE from pending locally, but
    its original copy still sits in the bucket — so a plain pull downloads it straight
    back and the labeller meets their own finished work again. The ledger is the record
    of what happened to each sample and it syncs both ways, so both nodes agree. An
    unapprove puts the sample back in the queue, hence last-action-wins rather than
    "ever approved"."""
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
    return {i for i, a in last.items() if a in ("approve", "discard")}


def sweep_consumed(root):
    """Drop pending copies of samples this node already finished. A pull that ran before
    the skip above existed left duplicates behind: the same frame sitting in pending AND
    in approved, so it comes round again and gets labelled twice.

    Deletion is deliberate but narrow — an approval must have its approved copy on disk
    before its pending copy goes, so a half-finished move can never destroy the only copy.
    A discard is already an instruction to destroy it."""
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
        if action == "approve" and not (root / "approved" / "labels" / f"{sid}.txt").exists():
            continue                       # the approved copy is not there: leave it alone
        if action not in ("approve", "discard"):
            continue
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
    for key, size in sorted(remote(cl, bucket, prefix).items()):
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
    swept = sweep_consumed(root)
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

    # An earlier pull left a duplicate behind; the sweep clears it, but only because the
    # approved copy is really there.
    (back / "pending" / "images" / "s1.jpg").write_bytes(b"aaa")
    assert sweep_consumed(back) == 1
    (back / "approved" / "labels" / "s1.txt").unlink()
    (back / "pending" / "images" / "s1.jpg").write_bytes(b"aaa")
    assert sweep_consumed(back) == 0, "no approved copy: the pending one is all there is"

    print("dataset_sync self-check ok: push skips unchanged, pull harvests, --ledgers and "
          "--config filter, ledgers merge instead of overwriting, neither side deletes, env "
          "creds win")


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
    else:
        sys.exit(__doc__)
