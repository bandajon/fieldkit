#!/usr/bin/env python3
"""Explicit, resumable retention maintenance for local curation datasets."""
import argparse
import hashlib
import json
import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
import threading
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path

import dataset_retention as policy

DATASET = Path("/data/dataset")
PREFIX = "curation/"
RECEIPTS = "retention-receipts.jsonl"
PLAN = "retention-plan.json"
STATUS = "retention-last-run.json"
_CYCLE_LOCK = threading.Lock()
RAW_PARTS = {"images": ".jpg", "labels": ".txt", "attrs": ".json"}


def _sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _etag(path):
    h = hashlib.md5()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _authority():
    if os.environ.get("FIELDKIT_RETENTION_AUTHORITY") != "1":
        raise PermissionError("retention authority activation required")


@contextmanager
def dataset_lock(root, filename="loop.lock"):
    """Nonblocking shared process lock; unsupported locking fails closed."""
    path = Path(root) / filename
    if path.is_symlink():
        yield False
        return
    f = path.open("a+")
    try:
        if os.name == "nt":
            import msvcrt
            try: f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError: yield False; return
        else:
            try:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (ImportError, OSError): yield False; return
        yield True
    finally:
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        f.close()


def serving_lease(root):
    """Dedicated app/maintenance lease; it never contends with the training lock."""
    return dataset_lock(root, "serving.lock")


def _objects(cl, bucket, prefix):
    out = {}
    for page in cl.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []): out[obj["Key"]] = obj
    return out


def _remote_bytes(cl, bucket, key):
    if hasattr(cl, "get_object"):
        return cl.get_object(Bucket=bucket, Key=key)["Body"].read()
    import tempfile
    with tempfile.NamedTemporaryFile() as f:
        cl.download_file(bucket, key, f.name); f.seek(0); return f.read()


def activate(root, cl, bucket, prefix=PREFIX, now=None):
    """Publish a monotonic policy and confirm the exact bytes through a fresh read."""
    _authority(); root = Path(root).resolve(); now = now or datetime.now(timezone.utc)
    p = root / policy.POLICY_NAME
    if p.is_symlink(): raise IOError("retention policy is symlink")
    local = policy.validate_policy(json.loads(p.read_text())) if p.exists() else None
    remote_key = prefix + policy.POLICY_NAME
    listed = _objects(cl, bucket, prefix)
    if p.exists() and remote_key not in listed: raise IOError("published retention policy missing remotely")
    remote = policy.validate_policy(json.loads(_remote_bytes(cl, bucket, remote_key))) if remote_key in listed else None
    if local and remote:
        import dataset_sync
        if dataset_sync._policy_regressed(local, remote):
            raise ValueError("retention policy regressed")
    old = remote or local
    known = list(_inventory(root)[0])
    for key in listed:
        if key.startswith(prefix):
            try:
                sid = policy.sample_id(key[len(prefix):])
            except ValueError:
                continue
            if sid: known.append(sid)
    unknown = sorted({sid for sid in known if policy._known(sid) is None})
    new = policy.advance_policy(old, now, unknown)
    raw = (json.dumps(new, indent=2, sort_keys=True) + "\n").encode()
    key = prefix + policy.POLICY_NAME
    cl.put_object(Bucket=bucket, Key=key, Body=raw, Metadata={"sha256": hashlib.sha256(raw).hexdigest()})
    if _remote_bytes(cl, bucket, key) != raw: raise IOError("retention policy readback mismatch")
    tmp = p.with_name(f".{p.name}.tmp-{os.getpid()}"); tmp.write_bytes(raw); os.replace(tmp, p)
    return new


def _inventory(root):
    root = Path(root).resolve(); groups = {}; unknown = []
    for tree in ("pending", "holding", "approved", "gold", "archive"):
        base = root / tree
        if not base.exists(): continue
        if base.is_symlink():
            unknown.append(base.relative_to(root).as_posix()); continue
        for p in base.rglob("*"):
            if p.is_symlink():
                unknown.append(p.relative_to(root).as_posix())
                continue
            if not p.is_file():
                continue
            rel = p.relative_to(root).as_posix()
            try: sid = policy.sample_id(rel)
            except ValueError: unknown.append(rel); continue
            if sid: groups.setdefault(sid, []).append((rel, p))
    return groups, unknown


def _receipt(root, record):
    if (Path(root) / RECEIPTS).is_symlink(): raise IOError("retention receipts is symlink")
    with (Path(root) / RECEIPTS).open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n"); f.flush(); os.fsync(f.fileno())


def _receipt_batch(root, records):
    if (Path(root) / RECEIPTS).is_symlink(): raise IOError("retention receipts is symlink")
    with (Path(root) / RECEIPTS).open("a", encoding="utf-8") as f:
        f.writelines(json.dumps(record, sort_keys=True) + "\n" for record in records)
        f.flush(); os.fsync(f.fileno())


def _save_plan(root, report):
    if (Path(root) / PLAN).is_symlink(): raise IOError("retention plan is symlink")
    raw = (json.dumps(report, sort_keys=True, indent=2) + "\n").encode()
    (Path(root) / PLAN).write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def _archive(root, sid):
    import classifier_crops
    classifier_crops.export_sample(root, sid)
    return classifier_crops.verified_archive(root, sid)


def _archive_checked(root, sid, saved_local, complete):
    """Reuse only a verified partial archive whose raw proof and metadata still match."""
    import classifier_crops
    m = classifier_crops.verified_archive(root, sid)
    if complete:
        return False
    expected = m["source_sha256"]
    for kind, ext in RAW_PARTS.items():
        rel = f"approved/{kind}/{sid}{ext}"
        snap = saved_local.get(rel)
        path = Path(root) / rel
        if snap is None or not path.exists():
            continue
        current = _sha(path)
        manifest_kind = "image" if kind == "images" else kind
        if current != snap["sha256"] or current != expected.get(manifest_kind):
            raise IOError(f"archive source changed: {sid}")
    names = (Path(root) / "classes.txt").read_text().splitlines()
    for sample in m["samples"]:
        cid = sample["class_id"]
        if cid >= len(names) or sample["class_name"] != names[cid]:
            raise IOError(f"archive classes changed: {sid}")
    import classifier_crops
    current_ref = sid in classifier_crops._references(root)
    if bool(m["reference"]) != current_ref:
        raise IOError(f"archive reference state changed: {sid}")
    return True


def _archive_files(root, sid):
    base = Path(root) / "classifier-crops" / sid
    if base.is_symlink(): raise IOError(f"archive path is symlink: {sid}")
    out = []
    for p in sorted(base.rglob("*")):
        if p.is_symlink(): raise IOError(f"archive path is symlink: {p}")
        if p.is_file(): out.append((p.relative_to(root).as_posix(), p))
    return out


def _raw_files(root, sid):
    out = []
    for tree in ("pending", "holding", "approved"):
        for kind, ext in RAW_PARTS.items():
            p = Path(root) / tree / kind / f"{sid}{ext}"
            if p.exists():
                if p.is_symlink(): raise IOError(f"raw source is symlink: {p}")
                if p.is_file(): out.append((p.relative_to(root).as_posix(), p))
    for p in ((Path(root) / "pending" / "suggest" / f"{sid}.json"),):
        if p.exists():
            if p.is_symlink(): raise IOError(f"raw source is symlink: {p}")
            if p.is_file(): out.append((p.relative_to(root).as_posix(), p))
    return out


def _backup(cl, bucket, prefix, root, sid):
    files = _archive_files(root, sid)
    if not files: raise ValueError(f"archive missing: {sid}")
    def sync(item):
        rel, path = item; raw = path.read_bytes(); key = prefix + rel
        try: remote = _remote_bytes(cl, bucket, key)
        except Exception: remote = None
        digest = hashlib.sha256(raw).hexdigest()
        if remote == raw:
            return key, digest
        if remote != raw:
            cl.put_object(Bucket=bucket, Key=key, Body=raw, Metadata={"sha256": hashlib.sha256(raw).hexdigest()})
        if _remote_bytes(cl, bucket, key) != raw: raise IOError(f"archive readback mismatch: {key}")
        return key, digest
    with ThreadPoolExecutor(max_workers=8) as pool: return dict(pool.map(sync, files))


def _verify_remote(cl, bucket, prefix, root, sid, listed):
    files = _archive_files(root, sid)
    def verify(item):
        rel, path = item; key = prefix + rel; raw = path.read_bytes()
        if key not in listed or listed[key].get("Size") != len(raw) or _remote_bytes(cl, bucket, key) != raw:
            raise IOError(f"archive proof missing or changed: {key}")
        return key, hashlib.sha256(raw).hexdigest()
    with ThreadPoolExecutor(max_workers=8) as pool: return dict(pool.map(verify, files))


def _delete_cloud(cl, bucket, keys, root=None, sizes=None):
    deleted = []
    for i in range(0, len(keys), 1000):
        batch = keys[i:i + 1000]
        try: result = cl.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in batch]})
        except Exception as e:
            result = {"Errors": [{"Key": k, "Message": str(e)} for k in batch]}
        batch_deleted = {x["Key"] for x in result.get("Deleted", [])}
        batch_absent = {x["Key"] for x in result.get("Errors", []) if x.get("Code") in {"NoSuchKey", "NotFound", "404"}}
        deleted.extend(batch_deleted | batch_absent)
        if root is not None:
            receipts = []
            for k in batch:
                state = ("success" if k in batch_deleted else "absent" if k in batch_absent else
                         "error" if any(x.get("Key") == k for x in result.get("Errors", [])) else "omitted")
                obj = next((x for x in result.get("Deleted", []) if x.get("Key") == k), {})
                size = (sizes or {}).get(k) if state == "success" else None
                receipts.append({"cloud_key": k, "size": size, "actual_deleted_bytes": size if state == "success" else 0,
                                 "delete": state, "result": state, "batch": i // 1000,
                                 "detail": next((x for x in result.get("Errors", []) if x["Key"] == k), None)})
            _receipt_batch(root, receipts)
    return deleted


def run(root=DATASET, cl=None, bucket=None, prefix=PREFIX, mode="plan", now=None, plan_path=None, expected_sha=None, internal_locked=False):
    root = Path(root).resolve(); groups, unknown = _inventory(root); p = policy.cached_policy(root)
    if not p: raise ValueError("published local retention policy required")
    now = now or datetime.now(timezone.utc)
    authority = os.environ.get("FIELDKIT_RETENTION_AUTHORITY") == "1"
    cloud = _objects(cl, bucket, prefix) if cl else {}
    for key in cloud:
        rel = key[len(prefix):] if key.startswith(prefix) else ""
        try: sid = policy.sample_id(rel)
        except ValueError: continue
        if sid and not any(r == rel for r, _ in groups.get(sid, ())): groups.setdefault(sid, []).append((rel, None))
    candidates = sorted(sid for sid in groups if policy.expired(sid, p, now.isoformat()))
    report = {"mode": mode, "expired": candidates, "unknown_paths": unknown, "actions": [],
              "root": str(root), "bucket": bucket, "prefix": prefix,
              "policy_sha256": _sha(root / policy.POLICY_NAME),
              "classes_sha256": _sha(root / "classes.txt") if (root / "classes.txt").exists() else None,
              "cloud_baseline": {k: {x: v for x, v in obj.items() if x in {"ETag", "Size"}} for k, obj in cloud.items()}}
    selected_local = {}
    selected_cloud = {}
    for sid in candidates:
        for rel, path in groups[sid]:
            if path is not None and rel.startswith(("pending/", "holding/", "approved/")):
                selected_local[rel] = {"sha256": _sha(path), "size": path.stat().st_size}
            if rel.startswith(("pending/", "holding/", "approved/")) and prefix + rel in cloud:
                obj = cloud[prefix + rel]; selected_cloud[prefix + rel] = {"ETag": obj.get("ETag"), "Size": obj.get("Size")}
    report["snapshot"] = {"local": selected_local, "cloud": selected_cloud}
    if mode == "plan": report["plan_sha256"] = _save_plan(root, report); return report
    if mode != "apply": raise ValueError("mode must be plan or apply")
    if not plan_path or not expected_sha: raise ValueError("apply requires --plan and --sha256")
    plan_raw = Path(plan_path).read_bytes()
    saved = json.loads(plan_raw)
    if hashlib.sha256(plan_raw).hexdigest() != expected_sha:
        raise ValueError("retention plan changed or is stale")
    if any(saved.get(k) != report.get(k) for k in ("root", "bucket", "prefix", "policy_sha256")):
        raise ValueError("retention plan context changed")
    if authority and cl is None: raise ValueError("authority apply needs cloud client")
    candidates = [sid for sid in saved.get("expired", []) if sid in groups and policy.expired(sid, p, now.isoformat())]
    with (nullcontext(True) if internal_locked else dataset_lock(root)) as locked:
        if not locked: report["busy"] = True; return report
        fresh = _objects(cl, bucket, prefix) if authority else {}
        if authority:
            local_policy = (root / policy.POLICY_NAME).read_bytes()
            if prefix + policy.POLICY_NAME not in fresh or _remote_bytes(cl, bucket, prefix + policy.POLICY_NAME) != local_policy:
                report["policy_unconfirmed"] = True; return report
        saved_local_all = saved.get("snapshot", {}).get("local", {})
        saved_cloud_all = saved.get("snapshot", {}).get("cloud", {})
        saved_local_by_sid, saved_cloud_by_sid = {}, {}
        for rel, snap in saved_local_all.items():
            sid = policy.sample_id(rel)
            if sid: saved_local_by_sid.setdefault(sid, {})[rel] = snap
        for key, snap in saved_cloud_all.items():
            sid = policy.sample_id(key[len(prefix):]) if key.startswith(prefix) else None
            if sid: saved_cloud_by_sid.setdefault(sid, {})[key] = snap
        cloud_keys, cloud_key_set, pending = [], set(), []
        for sid in candidates:
            paths = groups[sid]
            approved = [(r, pth) for r, pth in paths if r.startswith("approved/")]
            lineage = any(r.startswith(("approved/", "gold/", "archive/")) for r, _ in paths)
            raw_local = [(r, pth) for r, pth in paths if pth is not None and r.startswith(("pending/", "holding/", "approved/"))]
            raw_cloud = [r for r, _ in paths if r.startswith(("pending/", "holding/", "approved/")) and prefix + r in cloud]
            if not raw_local and not raw_cloud: continue
            try:
                saved_local = saved_local_by_sid.get(sid, {})
                current_local = {r: {"sha256": _sha(pth), "size": pth.stat().st_size} for r, pth in raw_local}
                if set(current_local) != {r for r in saved_local if r in current_local or r in {x[0] for x in raw_local}} or any(current_local.get(r) != saved_local.get(r) for r in current_local):
                    raise IOError(f"local source changed: {sid}")
                saved_cloud = saved_cloud_by_sid.get(sid, {})
                current_cloud_keys = {prefix + r for r in raw_cloud}
                if authority and current_cloud_keys != {k for k in saved_cloud if k in current_cloud_keys or k[len(prefix):] in raw_cloud}:
                    raise IOError(f"cloud source changed: {sid}")
                source_hashes = {r: _sha(pth) for r, pth in approved}
                if lineage and not approved: raise IOError(f"archive lineage incomplete: {sid}")
                if approved:
                    complete = (Path(root, "approved/images", f"{sid}.jpg").is_file()
                                and Path(root, "approved/labels", f"{sid}.txt").is_file())
                    reusable = False
                    manifest = Path(root) / "classifier-crops" / sid / "manifest.json"
                    if manifest.is_file() and not manifest.is_symlink():
                        try: reusable = _archive_checked(root, sid, saved_local, complete)
                        except Exception: reusable = False
                    if not reusable:
                        with policy.DATASET_LOCK: _archive(root, sid)
                if approved and cl is None: raise IOError("remote archive proof unavailable")
                proof = _backup(cl, bucket, prefix, root, sid) if authority and approved else (_verify_remote(cl, bucket, prefix, root, sid, cloud) if approved else {})
                if any(_sha(pth) != source_hashes[r] for r, pth in approved): raise IOError("source changed")
                if saved.get("classes_sha256") != (_sha(root / "classes.txt") if (root / "classes.txt").exists() else None): raise IOError("classes changed")
                if authority:
                    for r in raw_cloud:
                        key = prefix + r; obj = fresh.get(key); base = report["cloud_baseline"].get(key)
                        if obj is None or base is None or obj.get("Size") != base.get("Size") or obj.get("ETag", "").strip('"') != base.get("ETag", "").strip('"'): raise IOError(f"source cloud object changed: {key}")
                    for r in raw_cloud:
                        key = prefix + r
                        if key not in cloud_key_set: cloud_keys.append(key); cloud_key_set.add(key)
                pending.append((sid, raw_local, proof))
            except Exception as e: report["actions"].append({"sid": sid, "preserved": True, "error": str(e)})
        with policy.DATASET_LOCK:
            checked_pending = []
            for sid, raw_local, proof in pending:
                classes_ok = saved.get("classes_sha256") == (_sha(root / "classes.txt") if (root / "classes.txt").exists() else None)
                current_raw = _raw_files(root, sid)
                expected_raw = saved_local_by_sid.get(sid, {})
                sources_ok = ({r for r, _ in current_raw} <= set(expected_raw)
                              and all(_sha(pth) == expected_raw[r].get("sha256") for r, pth in current_raw))
                if not classes_ok or not sources_ok:
                    report["actions"].append({"sid": sid, "preserved": True, "error": "classes changed" if not classes_ok else "source changed"})
                    continue
                checked_pending.append((sid, raw_local, proof))
            pending = checked_pending
            allowed_cloud = {prefix + r[0] for sid, raw_local, _ in pending for r in groups[sid] if r[0].startswith(("pending/", "holding/", "approved/"))}
            cloud_keys = [key for key in cloud_keys if key in allowed_cloud]
            if authority and cloud_keys:
                fresh = _objects(cl, bucket, prefix)
                for key in cloud_keys:
                    obj = fresh.get(key); base = saved_cloud_all.get(key)
                    if obj is None or base is None or obj.get("Size") != base.get("Size") or obj.get("ETag", "").strip('"') != (base.get("ETag") or "").strip('"'):
                        raise IOError(f"source cloud object changed: {key}")
            cloud_sizes = {k: saved_cloud_all.get(k, {}).get("Size") for k in cloud_keys}
            deleted_cloud = set(_delete_cloud(cl, bucket, cloud_keys, root, cloud_sizes) if authority and cloud_keys else [])
            for sid, raw_local, proof in pending:
                local_deleted = [r for r, _ in raw_local if not authority or prefix + r in deleted_cloud or prefix + r not in cloud]
                for r, pth in raw_local:
                    if r in local_deleted: pth.unlink()
                cloud_deleted = [r for r in (x[0] for x in groups[sid]) if prefix + r in deleted_cloud]
                rec = {"sid": sid, "deleted": local_deleted, "local_deleted": local_deleted, "cloud_deleted": cloud_deleted, "archive": proof}
                _receipt(root, rec); report["actions"].append(rec)
    return report


def maintenance_once(root, cl, bucket, prefix=PREFIX):
    root = Path(root).resolve()
    import dataset_sync
    authority = os.environ.get("FIELDKIT_RETENTION_AUTHORITY") == "1"
    if not (root / policy.POLICY_NAME).exists() and authority: return {"disabled": True}
    if not _CYCLE_LOCK.acquire(blocking=False): return {"busy": True}
    try:
        with dataset_lock(root) as locked:
            if not locked: return {"busy": True}
            if authority:
                with policy.DATASET_LOCK:
                    activate(root, cl, bucket, prefix)
            else:
                listing = dataset_sync.remote(cl, bucket, prefix)
                if dataset_sync.load_policy(cl, bucket, prefix, root, listing) is None:
                    return {"disabled": True}
                dataset_sync.pull(cl, bucket, prefix, root, names=("classifier-crops",))
            policy.DATASET_LOCK.acquire()
            try:
                plan = run(root, cl=cl, bucket=bucket, prefix=prefix, mode="plan", internal_locked=True)
                out = run(root, cl=cl, bucket=bucket, prefix=prefix, mode="apply", plan_path=root / PLAN, expected_sha=plan["plan_sha256"], internal_locked=True)
            finally:
                policy.DATASET_LOCK.release()
        status = {"disabled": False, "plan": plan, "apply": out}
        if not (root / STATUS).is_symlink(): (root / STATUS).write_text(json.dumps(status, sort_keys=True))
        return status
    finally: _CYCLE_LOCK.release()


def main(argv=None):
    ap = argparse.ArgumentParser(); sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("activate"); a.add_argument("--dataset", default=str(DATASET))
    r = sub.add_parser("run"); r.add_argument("mode", choices=("plan", "apply")); r.add_argument("--dataset", default=str(DATASET)); r.add_argument("--plan"); r.add_argument("--sha256"); r.add_argument("--quiesced", action="store_true")
    o = sub.add_parser("once"); o.add_argument("--dataset", default=str(DATASET)); o.add_argument("--quiesced", action="store_true")
    ns = ap.parse_args(argv)
    if ns.cmd == "activate":
        import dataset_sync
        if not _CYCLE_LOCK.acquire(blocking=False): raise SystemExit("busy")
        try:
            with serving_lease(ns.dataset) as serving:
                if not serving: raise SystemExit("busy")
                with dataset_lock(ns.dataset) as locked:
                    if not locked: raise SystemExit("busy")
                    o = dataset_sync.creds()
                    print(json.dumps(activate(ns.dataset, dataset_sync.client(o), o["bucket"]), indent=2))
        finally: _CYCLE_LOCK.release()
    elif ns.cmd == "run":
        if ns.mode == "apply" and not ns.quiesced: raise SystemExit("apply requires --quiesced")
        import dataset_sync
        lease = serving_lease(ns.dataset) if ns.mode == "apply" else nullcontext(True)
        with lease as serving:
            if not serving: raise SystemExit("busy")
            o = dataset_sync.creds()
            cl = dataset_sync.client(o)
            print(json.dumps(run(ns.dataset, cl=cl, bucket=o["bucket"], mode=ns.mode, plan_path=ns.plan, expected_sha=ns.sha256), indent=2))
    else:
        if not ns.quiesced: raise SystemExit("once requires --quiesced")
        import dataset_sync
        with serving_lease(ns.dataset) as serving:
            if not serving: raise SystemExit("busy")
            o = dataset_sync.creds()
            print(json.dumps(maintenance_once(ns.dataset, dataset_sync.client(o), o["bucket"]), indent=2))


if __name__ == "__main__": main()
