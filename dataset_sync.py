#!/usr/bin/env python3
"""Seed a cloud curation instance from the local dataset, and harvest its output.

  python dataset_sync.py push [--prefix curation-test/]   local dataset -> bucket
  python dataset_sync.py pull [--ledgers]                 bucket -> local dataset
  python dataset_sync.py                                  self-check

Credentials come from OFFLOAD_ACCOUNT_ID / OFFLOAD_ACCESS_KEY_ID /
OFFLOAD_SECRET_ACCESS_KEY / OFFLOAD_BUCKET when set — Railway has no config.yaml —
otherwise from config.yaml's offload block, the same R2 bucket offload.py uploads to.
--prefix redirects both directions at once, for trying things somewhere harmless.

ponytail: sync without delete, deliberately. Push seeds, pull harvests, and neither
side ever removes the other's files; real two-way sync needs tombstones and conflict
rules, which is a different program nobody has asked for.
"""

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
        if have.get(prefix + rel) == f.stat().st_size:
            skipped += 1
        else:
            todo.append((f, prefix + rel))
    # Thousands of small files over a home uplink: latency-bound, so parallel
    # transfers are the whole speed-up. boto3 clients are thread-safe for this.
    sent = transfer(lambda f, key: cl.upload_file(str(f), bucket, key), todo, "pushed")
    print(f"push: {sent} uploaded, {skipped} already there ({prefix} on {bucket})")
    return sent, skipped


def pull(cl, bucket, prefix=PREFIX, root=DATASET, names=None):
    """Download the curation instance's work. names limits it to the daily harvest."""
    root = Path(root)
    todo, skipped = [], 0
    for key, size in sorted(remote(cl, bucket, prefix).items()):
        rel = key[len(prefix):]
        if not rel or (names and not rel.startswith(tuple(names))):
            continue
        dest = root / rel
        if dest.exists() and dest.stat().st_size == size:
            skipped += 1
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        todo.append((dest, key))
    got = transfer(lambda dest, key: cl.download_file(bucket, key, str(dest)), todo, "pulled")
    print(f"pull: {got} downloaded, {skipped} already local (-> {root})")
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
    assert push(cl, "buck", "curation/", src) == (0, 3), "same size, same file, no re-upload"
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
    assert pull(cl, "buck", "curation/", dst) == (0, 7), "already local, left alone"

    # --ledgers: the fast daily harvest skips the pending pile it seeded itself.
    day = Path(tempfile.mkdtemp())
    assert pull(cl, "buck", "curation/", day, LEDGERS) == (4, 0)
    assert not (day / "pending").exists(), "ledger harvest pulled the seed back down"
    assert (day / "audit.jsonl").exists() and (day / "approved" / "labels" / "a.txt").exists()

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

    print("dataset_sync self-check ok: push skips unchanged, pull harvests, --ledgers "
          "filters, neither side deletes, env creds win")


if __name__ == "__main__":
    args = sys.argv[1:]
    prefix = PREFIX
    if "--prefix" in args:
        i = args.index("--prefix")
        prefix = args.pop(i + 1).rstrip("/") + "/"
        args.pop(i)
    ledgers = "--ledgers" in args
    args = [a for a in args if not a.startswith("-")]
    if not args:
        selfcheck()
    elif args[0] == "push":
        o = creds()
        push(client(o), o["bucket"], prefix)
    elif args[0] == "pull":
        o = creds()
        pull(client(o), o["bucket"], prefix, names=LEDGERS if ledgers else None)
    else:
        sys.exit(__doc__)
