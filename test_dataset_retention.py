import io
import json
import tempfile
from pathlib import Path
from datetime import datetime, timezone

import dataset_retention as r
import dataset_sync as s


def policy(now="2026-09-10T00:00:00+00:00"):
    return r.advance_policy(None, now, ["unknown"])


class Fake:
    def __init__(self, objects): self.objects, self.uploaded = dict(objects), []
    def get_paginator(self, _): return self
    def paginate(self, Bucket, Prefix):
        yield {"Contents": [{"Key": k, "Size": len(v)} for k, v in self.objects.items() if k.startswith(Prefix)]}
    def get_object(self, Bucket, Key): return {"Body": io.BytesIO(self.objects[Key])}
    def upload_file(self, path, Bucket, Key): self.uploaded.append(Key); self.objects[Key] = Path(path).read_bytes()
    def download_file(self, Bucket, Key, path): Path(path).write_bytes(self.objects[Key])


def main():
    p = policy()
    assert r.expired("gate-cam-20260903-000000", p) is False
    assert r.expired("gate-cam-20260902-215959", p) is True
    older = r.advance_policy({**p, "expires_before": "2026-09-03T00:00:00+00:00"}, "2026-09-04T00:00:00+00:00")
    assert older["expires_before"] == p["expires_before"] and older["updated_at"] >= p["updated_at"]
    assert r.expired("unknown", p) is False
    later = r.advance_policy(p, "2026-09-17T00:00:00+00:00", ["unknown"])
    assert r.expired("unknown", later) is True and later["unknown_admitted_at"] == p["unknown_admitted_at"]
    added = r.advance_policy(p, "2026-09-10T00:00:00+00:00", ["new-id"])
    assert added["unknown_admitted_at"]["unknown"] == p["unknown_admitted_at"]["unknown"]
    assert r.sample_id("pending/images/gate-cam-20260903-000000.jpg")
    assert r.sample_id("classifier-crops/x/sha.png") is None
    assert r.sample_id("archive/v3-partial-20260824/images/gate-cam-20200101-000000.jpg") == "gate-cam-20200101-000000"
    assert s.always("classifier-crops/x/manifest.json")
    for bad in ("../x.jpg", "pending/images/a/b.jpg", "pending\\images\\x.jpg"):
        try: r.sample_id(bad); raise AssertionError(bad)
        except ValueError: pass
    root = Path(tempfile.mkdtemp())
    (root / "pending/images").mkdir(parents=True)
    (root / "pending/images/gate-cam-20260902-215959.jpg").write_bytes(b"old")
    (root / "classes.txt").write_text("car\n")
    symlink_root = Path(tempfile.mkdtemp()); (symlink_root / "pending").symlink_to(root / "pending", target_is_directory=True)
    try: s.safe_dest(symlink_root, "pending/images/x.jpg"); raise AssertionError("symlink accepted")
    except ValueError: pass
    remote = {"curation/retention-policy.json": json.dumps(p).encode(), "curation/classes.txt": b"x"}
    cl = Fake(remote)
    assert s.push(cl, "b", "curation/", root, names=("pending/images", "classes.txt"), force=True)[0] == 1
    assert all("20260902" not in k for k in cl.uploaded) and "curation/classes.txt" in cl.uploaded
    kept = Path(tempfile.mkdtemp()); (kept / "pending/images").mkdir(parents=True)
    (kept / "pending/images/gate-cam-20260903-000000.jpg").write_bytes(b"new")
    existing = Fake({"curation/retention-policy.json": json.dumps(p).encode(), "curation/pending/images/gate-cam-20260903-000000.jpg": b"old"})
    s.push(existing, "b", "curation/", kept, names=("pending/images",), force=True)
    assert existing.objects["curation/pending/images/gate-cam-20260903-000000.jpg"] == b"new"
    bad = Path(tempfile.mkdtemp()); (bad / "curators.yaml").symlink_to(root / "classes.txt")
    try: s.push(Fake({}), "b", "curation/", bad, names=("curators.yaml",), force=True); raise AssertionError("config symlink uploaded")
    except ValueError: pass
    dst = Path(tempfile.mkdtemp())
    assert s.pull(cl, "b", "curation/", dst)[0] == 1
    assert (dst / "retention-policy.json").read_text()
    (dst / "retention-policy.json").write_text("{}")
    try: s.pull(Fake({}), "b", "curation/", dst); raise AssertionError("missing authority accepted")
    except ValueError: pass
    print("dataset retention self-check ok")


if __name__ == "__main__": main()
