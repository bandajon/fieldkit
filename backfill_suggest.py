#!/usr/bin/env python3
"""Fill missing requested heads in pending R2 suggestions.

    python backfill_suggest.py                         assert-based self-check
    python backfill_suggest.py run [--limit N] [--dry-run] [--champion PATH]
                                  [--heads colour,make]

Limit caps successful frame writes; in dry-run it caps frames needing a backfill.
Validation frames are skipped before downloads. Existing values (including null)
are preserved. An omitted prediction stays absent.
Requested heads withheld by the champion are skipped before any R2 access.
"""
import io
import json
import shutil
import tempfile
import time
from pathlib import Path
from datetime import datetime, timezone


def split_withheld(heads, withheld):
    withheld = withheld or []
    return ([h for h in heads if h not in withheld],
            [h for h in heads if h in withheld])


def backfill(cl, bucket, heads, vocab, classify=None, *, dry_run=False, limit=None):
    from concurrent.futures import ThreadPoolExecutor
    from tempfile import TemporaryDirectory
    from threading import Lock
    from botocore.exceptions import ClientError
    import dataset_sync
    import dataset_retention
    from train_attrs import is_validation

    if not heads:
        raise ValueError("request at least one head")
    missing = [h for h in heads if h not in vocab]
    if missing:
        raise ValueError("champion lacks requested heads: " + ", ".join(missing))
    if limit is not None and limit < 1:
        raise ValueError("--limit must be positive")
    if not dry_run and classify is None:
        raise ValueError("classifier required")

    inference_lock = Lock()
    prefix = dataset_sync.PREFIX

    def get(key):
        body = cl.get_object(Bucket=bucket, Key=prefix + key)["Body"]
        try:
            return body.read()
        finally:
            body.close()

    etags = {}
    listing = {}
    for rel in ("pending/labels/", "pending/suggest/", "approved/labels/", "holding/labels/",
                dataset_retention.POLICY_NAME):
        started = time.monotonic()
        listing.update(dataset_sync.remote(cl, bucket, prefix + rel, etags))
        print(f"listed {rel} {time.monotonic() - started:.1f}s", flush=True)
    with TemporaryDirectory(prefix="backfill-suggest-") as root:
        try:
            audit = get("audit.jsonl")
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") != "NoSuchKey":
                raise
            print("! backfill: audit ledger missing; treating consumed stems as empty", flush=True)
            audit = b""
        # A reopened frame's live boxes may not match R2's pending original.
        audited = set()
        for line in audit.splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("id"):
                audited.add(r["id"])
        policy = dataset_sync.load_policy(cl, bucket, prefix, root, listing)

    def stems(tree):
        base = prefix + tree + "/labels/"
        return {k[len(base):-4] for k in listing
                if k.startswith(base) and k.endswith(".txt") and "/" not in k[len(base):]}

    def timestamp(sid):
        known = dataset_retention._known(sid)
        admitted = policy["unknown_admitted_at"].get(sid) if policy else None
        return known or (dataset_retention._dt(admitted) if admitted else
                         datetime.min.replace(tzinfo=timezone.utc))

    curated = stems("approved") | stems("holding") | audited
    pending = sorted(stems("pending"), key=lambda sid: (timestamp(sid), sid), reverse=True)

    names = get("classes.txt").decode("utf-8").split()
    if not dry_run:
        from PIL import Image
        from train_attrs import crop_box

    stats = dict.fromkeys(("scanned", "needs", "boxes", "updated", "created", "conflicts",
                           "validation", "expired", "curated", "nothing_missing", "errors"), 0)

    def frame(sid):
        result = dict.fromkeys(stats, 0)
        result["scanned"] = 1
        try:
            if is_validation(sid):
                result["validation"] = 1
                return result
            if policy and dataset_retention.expired(sid, policy):
                result["expired"] = 1
                return result
            if sid in curated:
                result["curated"] = 1
                return result
            key = prefix + f"pending/suggest/{sid}.json"
            exists = key in listing
            try:
                lines = get(f"pending/labels/{sid}.txt").decode("utf-8").splitlines()
                suggestions = json.loads(get(f"pending/suggest/{sid}.json")) if exists else {}
            except ClientError as e:
                if (e.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404
                        or e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound")):
                    result["conflicts"] = 1
                    return result
                raise
            if not isinstance(suggestions, dict) or any(
                    not isinstance(v, dict) for v in suggestions.values()):
                raise ValueError("suggestion must map box indices to head dictionaries")
            boxes = []
            for fields in map(str.split, lines):
                if len(fields) != 5:
                    continue
                try:
                    boxes.append((int(fields[0]), *map(float, fields[1:])))
                except ValueError:
                    continue
            needed = [(str(i), box) for i, box in enumerate(boxes)
                      if any(h not in suggestions.get(str(i), {}) for h in heads)]
            if not needed:
                result["nothing_missing"] = 1
                return result
            result["needs"], result["boxes"] = 1, len(needed)
            if dry_run:
                return result
            with Image.open(io.BytesIO(get(f"pending/images/{sid}.jpg"))) as source:
                img = source.convert("RGB")
            added = False
            for i, box in needed:
                crop = crop_box(img, box[1:])
                if crop is None:
                    continue
                cls = names[box[0]] if 0 <= box[0] < len(names) else None
                with inference_lock:
                    prediction = classify(crop, cls)
                previous = suggestions.get(i, {})
                extra = {h: prediction[h] for h in heads if h not in previous
                         and h in prediction and prediction[h] is not None}
                if extra:
                    suggestions[i] = {**previous, **extra}
                    added = True
            if not added:
                result["nothing_missing"] = 1
                return result
            # Never silently downgrade an existing object's conditional write.
            condition = {"IfMatch": etags[key]} if exists else {"IfNoneMatch": "*"}
            # A stem consumed after the ledger read can still get a sidecar, but dataset_sync.pull skips pending/ for consumed stems, so it is never served.
            try:
                cl.put_object(Bucket=bucket, Key=key,
                              Body=json.dumps(suggestions, indent=1).encode("utf-8"),
                              ContentType="application/json", **condition)
            except ClientError as e:
                if (e.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 412
                        or e.response.get("Error", {}).get("Code") in ("412", "PreconditionFailed")):
                    result["conflicts"] = 1
                    return result
                raise
            result["updated" if exists else "created"] = 1
        except Exception as e:
            result["errors"] = 1
            print(f"! backfill {sid}: {e}", flush=True)
        return result

    def summary(label):
        skipped = stats["validation"] + stats["expired"] + stats["curated"] + stats["nothing_missing"]
        print(f"{label}: scanned={stats['scanned']} needs={stats['needs']} boxes={stats['boxes']} "
              f"updated={stats['updated']} created={stats['created']} conflicts={stats['conflicts']} "
              f"skipped={skipped} (validation={stats['validation']} expired={stats['expired']} curated={stats['curated']} "
              f"nothing-missing={stats['nothing_missing']}) errors={stats['errors']}", flush=True)

    # Bounded batches avoid 92k queued futures and reserve at most the remaining writes.
    cursor = 0
    with ThreadPoolExecutor(max_workers=16) as pool:
        while cursor < len(pending):
            size = 16
            if limit is not None:
                progressed = stats["needs"] if dry_run else stats["updated"] + stats["created"]
                size = min(size, limit - progressed)
                if size == 0:
                    break
            batch = pending[cursor:cursor + size]
            cursor += len(batch)
            for result in pool.map(frame, batch):
                for k, v in result.items():
                    stats[k] += v
                if stats["scanned"] % 1000 == 0:
                    summary("progress")
    summary("dry-run" if dry_run else "backfill")
    return stats


def self_check():
    assert split_withheld(["make", "colour", "type"], ["colour"]) == (["make", "type"], ["colour"])
    assert split_withheld(["colour", "make"], ["make", "colour"]) == ([], ["colour", "make"])
    assert split_withheld(["colour", "make"], None) == (["colour", "make"], [])

    import contextlib
    import hashlib
    import threading
    from tempfile import TemporaryDirectory
    from botocore.exceptions import ClientError
    from PIL import Image
    import dataset_sync
    import dataset_retention

    import sys
    from types import SimpleNamespace
    from unittest.mock import patch
    skipped = "backfill_suggest: {} withheld by champion (below its calibration floor) - skipped\n"
    for dry_run in (False, True):
        output = io.StringIO()
        with patch.object(sys, "argv", [__file__, "run"] + (["--dry-run"] if dry_run else [])), \
                patch.dict(sys.modules, {"torch": SimpleNamespace(load=lambda *a, **kw: {
                    "heads": {"colour": [], "make": []}, "withheld": ["colour", "make"]})}), \
                patch("shutil.copy2"), \
                patch.object(dataset_sync, "creds") as creds, \
                patch.object(dataset_sync, "client") as client, \
                contextlib.redirect_stdout(output):
            assert main() == 0
        assert output.getvalue() == (skipped.format("colour") + skipped.format("make") +
            "backfill_suggest: nothing to do - all requested heads withheld by champion\n"), output.getvalue()
        creds.assert_not_called()
        client.assert_not_called()

    for withheld, expected in ((["colour"], ["make"]), (None, ["colour", "make"])):
        with patch.object(sys, "argv", [__file__, "run", "--dry-run"]), \
                patch.dict(sys.modules, {"torch": SimpleNamespace(load=lambda *a, **kw: {
                    "heads": {"colour": [], "make": []}, **({"withheld": withheld} if withheld else {})})}), \
                patch("shutil.copy2"), \
                patch.object(dataset_sync, "creds", return_value={"bucket": "test"}), \
                patch.object(dataset_sync, "client"), \
                patch(__name__ + ".backfill", return_value={"errors": 0}) as fill, \
                contextlib.redirect_stdout(io.StringIO()):
            assert main() == 0
        assert fill.call_args.args[2] == expected, fill.call_args

    regressions = []
    with TemporaryDirectory(prefix="backfill-check-") as root:
        champion = Path(root) / "champion.pt"
        champion.write_bytes(b"original")
        attributes = b"constraints:\n  motorcycle:\n    make: []\n"
        (champion.parent / "attributes.yaml").write_bytes(attributes)
        reads = []

        def load_checkpoint(path, **kwargs):
            reads.append(Path(path))
            assert Path(path).read_bytes() == b"original"
            champion.write_bytes(b"promoted")
            return {"heads": {"colour": [], "make": []}}

        def classifier(path, dev):
            reads.append(Path(path))
            assert Path(path).read_bytes() == b"original"
            assert (Path(path).parent / "attributes.yaml").read_bytes() == attributes
            return lambda crop, cls: {}

        torch = SimpleNamespace(load=load_checkpoint, backends=SimpleNamespace(
            mps=SimpleNamespace(is_available=lambda: False)))
        with patch.object(sys, "argv", [__file__, "run", "--champion", str(champion)]), \
                patch.dict(sys.modules, {"torch": torch, "detect": SimpleNamespace(attr_classifier=classifier)}), \
                patch.object(dataset_sync, "creds", return_value={"bucket": "test"}), \
                patch.object(dataset_sync, "client"), \
                patch(__name__ + ".backfill", return_value={"errors": 0}), \
                contextlib.redirect_stdout(io.StringIO()):
            status = main()
        regressions.append(("checkpoint snapshot", status == 0 and len(reads) == 2
                            and reads[0] == reads[1] and reads[0] != champion
                            and not reads[0].exists()))

    prefix = dataset_sync.PREFIX
    jpg = io.BytesIO()
    Image.new("RGB", (100, 100), "white").save(jpg, format="JPEG")
    line = "0 .5 .5 .4 .4\n"
    vocab = {"colour": ["red"], "make": ["Toyota"]}

    class S3:
        def __init__(self):
            self.data, self.gets, self.puts, self.prefixes = {}, [], [], []
            self.races = set()
            self.vanished = set()
            self.lock = threading.Lock()
            self.add("classes.txt", "car bus")
            self.add("audit.jsonl", "")
            self.add("retention-policy.json", {
                "version": 1, "days": 7, "not_before": "2026-09-02T22:00:00+00:00",
                "expires_before": "2026-10-05T00:00:00+00:00",
                "updated_at": "2026-10-12T00:00:00+00:00", "unknown_admitted_at": {}})

        def add(self, key, value):
            if isinstance(value, dict): value = json.dumps(value)
            self.data[prefix + key] = value.encode() if isinstance(value, str) else value

        def frame(self, sid, labels=line, suggest=None):
            self.add(f"pending/labels/{sid}.txt", labels)
            self.add(f"pending/images/{sid}.jpg", jpg.getvalue())
            if suggest is not None: self.add(f"pending/suggest/{sid}.json", suggest)

        def etag(self, key):
            return hashlib.md5(self.data[key]).hexdigest()

        def get_paginator(self, name):
            assert name == "list_objects_v2"
            return self

        def paginate(self, Bucket, Prefix):
            self.prefixes.append(Prefix)
            rows = [{"Key": k, "Size": len(v), "ETag": '"' + self.etag(k) + '"'}
                    for k, v in sorted(self.data.items()) if k.startswith(Prefix)]
            for start in range(0, len(rows), 2):
                yield {"Contents": rows[start:start + 2]}

        def get_object(self, Bucket, Key):
            with self.lock:
                self.gets.append(Key)
                if Key in self.vanished:
                    self.data.pop(Key, None)
                    raise ClientError({"Error": {"Code": "404"},
                                       "ResponseMetadata": {"HTTPStatusCode": 404}}, "GetObject")
                if Key not in self.data:
                    raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
                return {"Body": io.BytesIO(self.data[Key])}

        def put_object(self, Bucket, Key, Body, ContentType, **condition):
            with self.lock:
                assert ContentType == "application/json"
                assert set(condition) in ({"IfMatch"}, {"IfNoneMatch"})
                if Key in self.races:
                    self.data[Key] = b'{"winner": {"type": "bus"}}'
                if (("IfMatch" in condition and (Key not in self.data or
                        condition["IfMatch"] != self.etag(Key))) or
                        ("IfNoneMatch" in condition and (condition["IfNoneMatch"] != "*" or
                                                       Key in self.data))):
                    raise ClientError({"Error": {"Code": "PreconditionFailed"},
                                       "ResponseMetadata": {"HTTPStatusCode": 412}}, "PutObject")
                self.puts.append((Key, condition))
                self.data[Key] = Body

        def suggestion(self, sid):
            return json.loads(self.data[prefix + f"pending/suggest/{sid}.json"])

        def images(self):
            return [k for k in self.gets if "/images/" in k]

    calls = []
    def classify(crop, cls):
        calls.append(cls)
        return {"colour": "red", "make": "Toyota", "type": "changed", "axles": "9"}

    def run(s3, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return backfill(s3, "test", ["colour", "make"], vocab, classify, **kwargs)

    from train_attrs import is_validation
    s3 = S3()
    concurrent_frames = [f"inference-{i}" for i in range(32)
                         if not is_validation(f"inference-{i}")][:16]
    assert len(concurrent_frames) == 16, concurrent_frames
    for sid in concurrent_frames:
        s3.frame(sid)
    active = peak = inference_calls = 0
    counter_lock = threading.Lock()

    def slow_classify(crop, cls):
        nonlocal active, peak, inference_calls
        with counter_lock:
            active += 1
            peak = max(peak, active)
            inference_calls += 1
        try:
            time.sleep(0.02)
            return {"colour": "red", "make": "Toyota"}
        finally:
            with counter_lock:
                active -= 1

    with contextlib.redirect_stdout(io.StringIO()):
        stats = backfill(s3, "test", list(vocab), vocab, slow_classify, dry_run=False)
    assert stats["needs"] == stats["created"] == len(s3.puts) == inference_calls == 16, stats
    assert stats["errors"] == active == 0, (stats, active)
    assert peak == 1, f"classify calls overlapped: peak={peak}"

    s3 = S3()
    original = {"0": {"colour": "blue", "type": "car"}, "7": {"cargo": "old"}}
    s3.frame("merge", "bad row\n" + line + "short\n1 .4 .4 .3 .3\n", original)
    s3.frame("new")
    s3.frame("approved"); s3.add("approved/labels/approved.txt", line)
    s3.frame("holding"); s3.add("holding/labels/holding.txt", line)
    s3.frame("gate-20260901-120000")
    stats = run(s3)
    assert stats["updated"] == 1 and stats["created"] == 1, stats
    assert stats["scanned"] == 5 and stats["needs"] == 2 and stats["boxes"] == 3, stats
    assert stats["expired"] == 1 and stats["curated"] == 2 and stats["errors"] == 0, stats
    assert s3.suggestion("merge") == {
        "0": {"colour": "blue", "type": "car", "make": "Toyota"},
        "1": {"colour": "red", "make": "Toyota"}, "7": {"cargo": "old"}}
    assert sorted(calls) == ["bus", "car", "car"], calls
    assert all("approved" not in k and "holding" not in k and "20260901" not in k
               for k in s3.gets), s3.gets
    assert prefix not in s3.prefixes
    assert len(s3.prefixes) == 5, s3.prefixes
    assert s3.gets.count(prefix + "audit.jsonl") == 1, s3.gets
    assert all(prefix + x in s3.prefixes for x in (
        "pending/labels/", "pending/suggest/", "approved/labels/", "holding/labels/",
        dataset_retention.POLICY_NAME))
    assert {next(iter(c)) for _, c in s3.puts} == {"IfMatch", "IfNoneMatch"}
    images, writes = len(s3.images()), len(s3.puts)
    stats = run(s3)
    assert stats["updated"] == stats["created"] == stats["needs"] == 0, stats
    assert len(s3.puts) == writes and len(s3.images()) == images

    calls.clear()
    s3 = S3()
    s3.frame("parse", "bad .5 .5 .4 .4\n0 .2 .2 .1 .1\n-1 .5 .5 .4 .4\n0 .5 .5 x .4\n")
    stats = run(s3)
    assert stats["created"] == 1 and stats["errors"] == 0, stats
    assert s3.suggestion("parse") == {
        "0": {"colour": "red", "make": "Toyota"},
        "1": {"colour": "red", "make": "Toyota"}}, s3.suggestion("parse")
    assert calls == ["car", None] and stats["errors"] == 0, (calls, stats)

    for dry_run in (False, True):
        s3 = S3(); s3.frame("val-13"); s3.frame("good")
        stats = run(s3, dry_run=dry_run)
        assert all("val-13" not in k for k in s3.gets), s3.gets
        assert stats["validation"] == 1, stats

    s3 = S3(); s3.frame("dry", suggest={"0": {"colour": "blue"}})
    stats = run(s3, dry_run=True)
    assert stats["needs"] == stats["boxes"] == 1 and not s3.puts and not s3.images(), stats
    s3 = S3(); s3.frame("missing")
    try:
        backfill(s3, "test", ["colour", "make"], {"colour": []}, classify)
    except ValueError as e:
        assert "make" in str(e)
    else:
        assert False, "missing champion head accepted"
    assert not s3.puts and not s3.gets and not s3.prefixes

    s3 = S3(); s3.frame("omit", line + "1 .5 .5 .4 .4\n")
    def omit(crop, cls):
        return {"colour": "red", "make": None, "type": "car"} if cls == "car" else {}
    with contextlib.redirect_stdout(io.StringIO()):
        stats = backfill(s3, "test", list(vocab), vocab, omit)
    assert s3.suggestion("omit") == {"0": {"colour": "red"}}

    for existing in (None, {"0": {"type": "car"}}):
        s3 = S3(); s3.frame("race", suggest=existing)
        s3.races.add(prefix + "pending/suggest/race.json")
        stats = run(s3)
        assert stats["conflicts"] == 1 and stats["errors"] == 0 and not s3.puts, stats
        assert s3.suggestion("race") == {"winner": {"type": "bus"}}

    from train_attrs import is_validation
    assert not any(is_validation(sid) for sid in ("ledger-discard", "reopened", "missing-ledger", "label-gone"))
    for dry_run in (False, True):
        s3 = S3(); s3.frame("ledger-discard")
        s3.add("audit.jsonl", '{"id":"ledger-discard","action":"approve"}\n'
               '{"id":"ledger-discard","action":"discard"}\n')
        stats = run(s3, dry_run=dry_run)
        regressions.append((f"last discard (dry_run={dry_run})",
                            stats["curated"] == 1 and stats["needs"] == stats["errors"] == 0
                            and not s3.puts and all("ledger-discard" not in k for k in s3.gets)
                            and s3.gets.count(prefix + "audit.jsonl") == 1))

        s3 = S3(); s3.frame("reopened")
        s3.add("audit.jsonl", '{"id":"reopened","action":"approve"}\n'
               '{"id":"reopened","action":"unapprove"}\n')
        stats = run(s3, dry_run=dry_run)
        regressions.append((f"unapproved stem is skipped (dry_run={dry_run})",
                            stats["curated"] == 1 and stats["needs"] == stats["errors"] == 0
                            and not s3.puts and all("reopened" not in k for k in s3.gets)
                            and s3.gets.count(prefix + "audit.jsonl") == 1))

        for action in ("review", "future-action"):
            s3 = S3(); s3.frame("ledger-discard")
            s3.add("audit.jsonl", b'broken json\n\xff\n' + json.dumps(
                {"id": "ledger-discard", "action": action}).encode() + b'\n{}\n')
            stats = run(s3, dry_run=dry_run)
            regressions.append((f"any audit action {action} (dry_run={dry_run})",
                                stats["curated"] == 1 and stats["needs"] == stats["errors"] == 0
                                and not s3.puts and all("ledger-discard" not in k for k in s3.gets)))

        s3 = S3(); s3.frame("missing-ledger")
        del s3.data[prefix + "audit.jsonl"]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            stats = backfill(s3, "test", list(vocab), vocab, classify, dry_run=dry_run)
        warning = "! backfill: audit ledger missing; treating consumed stems as empty"
        regressions.append((f"missing ledger (dry_run={dry_run})",
                            stats["curated"] == stats["errors"] == 0 and stats["needs"] == 1
                            and stats["created"] == (0 if dry_run else 1)
                            and output.getvalue().splitlines().count(warning) == 1
                            and s3.gets.count(prefix + "audit.jsonl") == 1))

        s3 = S3(); s3.frame("label-gone")
        s3.vanished.add(prefix + "pending/labels/label-gone.txt")
        stats = run(s3, dry_run=dry_run)
        regressions.append((f"deleted label (dry_run={dry_run})",
                            stats["conflicts"] == 1 and stats["errors"] == stats["needs"] == 0
                            and not s3.puts and not s3.images()
                            and prefix + "pending/suggest/label-gone.json" not in s3.gets))

    s3 = S3(); s3.frame("deleted-sidecar", suggest={"0": {"type": "car"}})
    s3.vanished.add(prefix + "pending/suggest/deleted-sidecar.json")
    stats = run(s3)
    regressions.append(("deleted sidecar", stats["conflicts"] == 1
                        and stats["errors"] == 0 and not s3.puts))
    for code in ("AccessDenied", "404"):
        s3 = S3(); s3.frame("ledger-error")
        error = ClientError({"Error": {"Code": code}}, "GetObject")
        get_object = s3.get_object

        def fail_ledger(Bucket, Key):
            if Key == prefix + "audit.jsonl":
                s3.gets.append(Key)
                raise error
            return get_object(Bucket, Key)

        with patch.object(s3, "get_object", side_effect=fail_ledger):
            try:
                run(s3)
            except ClientError as e:
                assert e is error
            else:
                assert False, "non-NoSuchKey ledger error accepted"
        assert s3.gets == [prefix + "audit.jsonl"] and not s3.puts, s3.gets

    assert all(passed for _, passed in regressions), regressions

    s3 = S3()
    for i in range(40): s3.frame(f"limit-{i}")
    stats = run(s3, limit=2)
    assert len(s3.puts) == stats["created"] == 2, stats
    s3 = S3()
    for i in range(10): s3.frame(f"dry-{i}")
    stats = run(s3, limit=2, dry_run=True)
    assert stats["scanned"] == stats["needs"] == 2 and not s3.images() and not s3.puts

    newer, older, complete = "z-20261011-000000", "a-20261010-000000", "b-20261012-000000"
    assert not any(is_validation(sid) for sid in (newer, older, complete))
    for dry_run in (True, False):
        s3 = S3()
        s3.frame(older); s3.frame(newer)
        s3.frame(complete, suggest={"0": {"colour": "x", "make": "y"}})
        s3.frame("gate-20260901-120000")
        stats = run(s3, limit=1, dry_run=dry_run)
        assert stats["needs"] == 1 and stats["scanned"] == 2, stats
        assert prefix + f"pending/labels/{newer}.txt" in s3.gets, s3.gets
        assert prefix + f"pending/labels/{older}.txt" not in s3.gets, s3.gets
        if not dry_run:
            assert [k for k, _ in s3.puts] == [prefix + f"pending/suggest/{newer}.json"], s3.puts
        else:
            assert not s3.puts and not s3.images()

    assert not is_validation("admitted-newer")
    for dry_run in (True, False):
        s3 = S3()
        s3.frame(newer); s3.frame("admitted-newer")
        policy = json.loads(s3.data[prefix + "retention-policy.json"])
        policy["unknown_admitted_at"]["admitted-newer"] = "2026-10-12T00:00:00+00:00"
        s3.add("retention-policy.json", policy)
        stats = run(s3, limit=1, dry_run=dry_run)
        assert stats["scanned"] == stats["needs"] == 1, stats
        assert prefix + "pending/labels/admitted-newer.txt" in s3.gets, s3.gets
        assert prefix + f"pending/labels/{newer}.txt" not in s3.gets, s3.gets
        assert stats["created"] == (0 if dry_run else 1), stats
        if dry_run:
            assert not s3.puts and not s3.images()
        else:
            assert [k for k, _ in s3.puts] == [prefix + "pending/suggest/admitted-newer.json"], s3.puts

    for dry_run in (True, False):
        s3 = S3()
        s3.frame("external-a-needed")
        s3.frame("external-z-expired")
        policy = json.loads(s3.data[prefix + "retention-policy.json"])
        policy["unknown_admitted_at"]["external-z-expired"] = "2026-09-01T00:00:00+00:00"
        s3.add("retention-policy.json", policy)
        s3.frame("external-y-complete", suggest={"0": {"colour": "x", "make": "y"}})
        stats = run(s3, limit=1, dry_run=dry_run)
        assert stats["needs"] == 1 and stats["expired"] == stats["nothing_missing"] == 1, stats
        assert stats["scanned"] == 3, stats
        assert all("external-z-expired" not in k for k in s3.gets), s3.gets
        assert stats["created"] == (0 if dry_run else 1), stats

    s3 = S3(); s3.frame("bad", suggest={"0": "invalid"}); s3.frame("good")
    s3.frame("degenerate", "0 .5 .5 0 0\n")
    stats = run(s3)
    assert stats["errors"] == 1 and stats["created"] == 1, stats
    assert prefix + "pending/suggest/degenerate.json" not in s3.data
    print("backfill_suggest self-check ok: requested-only merge, stable box indices, validation/curated/expired skips, "
          "missing-head refusal, omitted heads, conditional conflicts, idempotence, dry-run, newest-first outcome limits including unknown admission times, five full listings, isolated errors, degenerate crops, withheld-head filtering and early exit, audit-ledger eligibility including unapproved stems and malformed lines, missing-ledger warning and error refusal, deleted-label/sidecar conflicts, checkpoint snapshot, serialized inference across 16 frames")


def main():
    import argparse
    import sys
    if len(sys.argv) == 1:
        self_check()
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["run"])
    parser.add_argument("--heads", default="colour,make")
    parser.add_argument("--champion", type=Path,
                        default=Path(__file__).resolve().parent / "dataset/attrs-champion.pt")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    heads = list(dict.fromkeys(h.strip() for h in args.heads.split(",") if h.strip()))
    try:
        if not heads:
            raise ValueError("request at least one head")
        if args.limit is not None and args.limit < 1:
            raise ValueError("--limit must be positive")
        import torch
        with tempfile.TemporaryDirectory(prefix="backfill-champion-") as root:
            snapshot = Path(root) / args.champion.name
            shutil.copy2(args.champion, snapshot)
            attributes = args.champion.parent / "attributes.yaml"
            if attributes.exists():
                shutil.copy2(attributes, snapshot.parent / attributes.name)
            ck = torch.load(snapshot, map_location="cpu", weights_only=True)
            vocab = ck["heads"]
            missing = [h for h in heads if h not in vocab]
            if missing:
                raise ValueError("champion lacks requested heads: " + ", ".join(missing))
            heads, dropped = split_withheld(heads, ck.get("withheld") or [])
            for head in dropped:
                print(f"backfill_suggest: {head} withheld by champion (below its calibration floor) - skipped", flush=True)
            if not heads:
                print("backfill_suggest: nothing to do - all requested heads withheld by champion", flush=True)
                return 0
            classify = None
            if not args.dry_run:
                import detect
                dev = "mps" if torch.backends.mps.is_available() else "cpu"
                classify = detect.attr_classifier(str(snapshot), dev)
            import dataset_sync
            credentials = dataset_sync.creds()
            stats = backfill(dataset_sync.client(credentials), credentials["bucket"], heads, vocab,
                             classify, dry_run=args.dry_run, limit=args.limit)
            return 1 if stats["errors"] else 0
    except Exception as e:
        print(f"backfill_suggest: {e}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
