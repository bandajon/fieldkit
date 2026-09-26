#!/usr/bin/env python3
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import dataset_retention as policy
import retention_maintenance as rm


class FakeS3:
    def __init__(self): self.objects = {}; self.fail_delete = False
    def get_paginator(self, _): return self
    def paginate(self, Bucket, Prefix):
        yield {"Contents": [{"Key": k, "Size": len(v)} for k, v in self.objects.items() if k.startswith(Prefix)]}
    def put_object(self, Bucket, Key, Body, **_): self.objects[Key] = Body
    def get_object(self, Bucket, Key):
        from io import BytesIO
        return {"Body": BytesIO(self.objects[Key])}
    def delete_objects(self, Bucket, Delete):
        if self.fail_delete: return {"Errors": [{"Key": Delete["Objects"][0]["Key"]}]}
        out = []
        for x in Delete["Objects"]: self.objects.pop(x["Key"], None); out.append(x)
        return {"Deleted": out}


def main():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td); (d / "approved/images").mkdir(parents=True); (d / "approved/labels").mkdir(); (d / "approved/attrs").mkdir(); (d / "classes.txt").write_text("car\n")
        (d / "approved/images/old-20260901-000000.jpg").write_bytes(b"x")
        (d / "approved/labels/old-20260901-000000.txt").write_text("0 .5 .5 .2 .2\n")
        (d / "approved/attrs/old-20260901-000000.json").write_text('{"0":{"type":"van"}}')
        raw = policy.advance_policy(None, "2026-09-09T00:00:00+00:00"); (d / policy.POLICY_NAME).write_text(json.dumps(raw))
        plan = rm.run(d, mode="plan"); assert plan["expired"] == ["old-20260901-000000"]
        assert rm.run(d, mode="apply", plan_path=d / rm.PLAN, expected_sha=plan["plan_sha256"])["actions"][0]["preserved"]
        assert (d / "approved/images/old-20260901-000000.jpg").exists()
        # Unknown IDs are admitted at authority time and do not expire early.
        p = policy.advance_policy(raw, "2026-09-09T00:00:00+00:00", ["mystery"]); assert not policy.expired("mystery", p)
        assert policy.sample_id("models/champion.pt") is None and policy.sample_id("classifier-crops/x/a.png") is None
        # Lock contention skips the run and symlinks are reported without traversal.
        (d / "approved/images/link.jpg").symlink_to(d / "approved/images/old-20260901-000000.jpg")
        with patch.object(rm, "dataset_lock") as lock:
            lock.return_value.__enter__.return_value = False
            assert rm.run(d, mode="apply", plan_path=d / rm.PLAN, expected_sha=plan["plan_sha256"])["busy"]
        # Authority archive failure and a changed cloud source both preserve raw files.
        cl = FakeS3(); cl.objects["curation/retention-policy.json"] = (d / policy.POLICY_NAME).read_bytes()
        with patch.dict(os.environ, {"FIELDKIT_RETENTION_AUTHORITY": "1"}):
            authority_plan = rm.run(d, cl=cl, bucket="b", mode="plan")
        with patch.dict(os.environ, {"FIELDKIT_RETENTION_AUTHORITY": "1"}), patch.object(rm, "_archive", side_effect=IOError("backup failed")):
            out = rm.run(d, cl=cl, bucket="b", mode="apply", plan_path=d / rm.PLAN, expected_sha=authority_plan["plan_sha256"])
            assert out["actions"][0]["preserved"] and (d / "approved/images/old-20260901-000000.jpg").exists()
        with patch.dict(os.environ, {"FIELDKIT_RETENTION_AUTHORITY": "1"}), patch.object(rm, "_archive"), patch.object(rm, "_backup", return_value={}):
            cl.objects["curation/approved/images/old-20260901-000000.jpg"] = b"changed"
            out = rm.run(d, cl=cl, bucket="b", mode="apply", plan_path=d / rm.PLAN, expected_sha=authority_plan["plan_sha256"])
            assert out["actions"][0]["preserved"]
        # A source mutation during archive proof must prevent any cloud delete for that SID.
        pending = d / "pending/images/old-20260901-000000.jpg"; pending.parent.mkdir(parents=True, exist_ok=True); pending.write_bytes(b"p")
        cl.objects["curation/pending/images/old-20260901-000000.jpg"] = b"p"
        with patch.dict(os.environ, {"FIELDKIT_RETENTION_AUTHORITY": "1"}):
            mutation_plan = rm.run(d, cl=cl, bucket="b", mode="plan")
        cl.calls = []; original_delete = cl.delete_objects
        def tracked_delete(*args, **kwargs): cl.calls.append(kwargs.get("Delete") or args[-1]); return original_delete(*args, **kwargs)
        cl.delete_objects = tracked_delete
        calls_before = len(cl.calls)
        def mutate_backup(*_args, **_kwargs): pending.write_bytes(b"mutated"); return {}
        with patch.dict(os.environ, {"FIELDKIT_RETENTION_AUTHORITY": "1"}), patch.object(rm, "_archive"), patch.object(rm, "_backup", side_effect=mutate_backup):
            rm.run(d, cl=cl, bucket="b", mode="apply", plan_path=d / rm.PLAN, expected_sha=mutation_plan["plan_sha256"])
        assert len(cl.calls) == calls_before
        cl.fail_delete = True
        assert rm._delete_cloud(cl, "b", ["a"]) == []
        # Cross-SID deletion batches at the S3 limit and persists mixed responses.
        class BatchS3(FakeS3):
            def __init__(self): super().__init__(); self.calls = []
            def delete_objects(self, Bucket, Delete):
                self.calls.append(Delete["Objects"])
                return {"Deleted": Delete["Objects"][:1], "Errors": [{"Key": Delete["Objects"][1]["Key"], "Code": "fail"}]} if len(self.calls) == 1 else {"Deleted": Delete["Objects"]}
        batch = BatchS3(); keys = [f"curation/pending/images/s{i}.jpg" for i in range(1002)]
        deleted = rm._delete_cloud(batch, "b", keys, d)
        assert len(batch.calls) == 2 and [len(x) for x in batch.calls] == [1000, 2] and len(deleted) == 3
        receipts = (d / rm.RECEIPTS).read_text(); assert '"delete": "error"' in receipts and '"delete": "success"' in receipts
        later = policy.advance_policy(raw, "2026-09-17T00:00:00+00:00", ["mystery"])
        assert later["expires_before"] >= raw["expires_before"]
        # The real run loop plans cloud-only raw SIDs and deletes them in S3-sized batches.
        class RunS3(FakeS3):
            def __init__(self): super().__init__(); self.calls = []
            def delete_objects(self, Bucket, Delete):
                self.calls.append(Delete["Objects"]); return {"Deleted": Delete["Objects"]}
        cloud = RunS3(); cloud.objects["curation/retention-policy.json"] = (d / policy.POLICY_NAME).read_bytes()
        for i in range(1002): cloud.objects[f"curation/pending/images/cloud{i}-20260901-000000.jpg"] = b"x"
        with patch.dict(os.environ, {"FIELDKIT_RETENTION_AUTHORITY": "1"}):
            planned = rm.run(d, cl=cloud, bucket="b", mode="plan")
            applied = rm.run(d, cl=cloud, bucket="b", mode="apply", plan_path=d / rm.PLAN, expected_sha=planned["plan_sha256"])
        assert len(cloud.calls) == 2 and [len(x) for x in cloud.calls] == [1000, 2]
        assert len([a for a in applied["actions"] if a.get("cloud_deleted")]) == 1002
        print("ok — policy, exclusions, no-follow inventory, archive gate, and busy lock checks")


if __name__ == "__main__": main()
