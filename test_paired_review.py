#!/usr/bin/env python3
import asyncio, hashlib, io, os, shutil, subprocess, sys, tempfile, threading, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
import paired_review as p

class FakeR2:
    def __init__(self, objects): self.objects = dict(objects); self.gets = 0; self.list_calls = []; self.last_modified = {}
    def get_paginator(self, _): return self
    def paginate(self, **kw):
        self.list_calls.append(dict(kw)); prefix, delim = kw.get("Prefix", ""), kw.get("Delimiter")
        keys = sorted(k for k in self.objects if k.startswith(prefix))
        if delim:
            common, direct = set(), []
            for k in keys:
                rest = k[len(prefix):]
                if delim in rest: common.add(prefix + rest[:rest.index(delim) + 1])
                else: direct.append({"Key": k, "Size": len(self.objects[k])})
            for item in direct:
                if item["Key"] in self.last_modified: item["LastModified"] = self.last_modified[item["Key"]]
            yield {"CommonPrefixes": [{"Prefix": x} for x in sorted(common)], "Contents": direct}
        else:
            contents = [{"Key": k, "Size": len(self.objects[k])} for k in keys]
            for item in contents:
                if item["Key"] in self.last_modified: item["LastModified"] = self.last_modified[item["Key"]]
            yield {"Contents": contents}
    def head_object(self, **kw):
        key = kw["Key"]
        if key not in self.objects:
            error = RuntimeError("not found"); error.response = {"Error": {"Code": "NoSuchKey"}}; raise error
        return {"ContentLength": len(self.objects[key]), "ETag": '"etag-' + str(len(self.objects[key])) + '"'}
    def get_object(self, **kw):
        self.gets += 1
        if kw["Key"] not in self.objects:
            error = RuntimeError("not found"); error.response = {"Error": {"Code": "NoSuchKey"}}; raise error
        return {"Body": io.BytesIO(self.objects[kw["Key"]])}
    def put_object(self, **kw):
        body = kw["Body"].read() if hasattr(kw["Body"], "read") else kw["Body"]
        self.objects[kw["Key"]] = body
        return {"ETag": '"etag-' + str(len(body)) + '"'}
    def delete_object(self, **kw): self.objects.pop(kw["Key"], None); return {}

class PartialBody:
    def __init__(self): self.done = False
    def read(self, _):
        if self.done: raise OSError("connection dropped")
        self.done = True; return b"partial"
    def close(self): pass

class PartialR2(FakeR2):
    def get_object(self, **kw): return {"Body": PartialBody()}

class FailingPutR2(FakeR2):
    def put_object(self, **kw):
        if kw["Key"].endswith("example.json"): raise RuntimeError("provider unavailable")
        return super().put_object(**kw)

def expect(fn, typ):
    try: fn()
    except typ: return
    raise AssertionError(f"expected {typ.__name__}")

def main():
    ka = "site1/cam3/20260903-104834.mkv"; kb = "site1/cam4/20260903-104837.mkv"; kc = "site1/cam5/20260903-104840.mkv"
    objects = {ka: b"a" * 10, kb: b"b" * 10, kc: b"c" * 10,
               "site2/cam1/20260905-104834.mkv": b"d", "site1/cam3/thumb.jpg": b"x",
               "classifier-crops-history/x/" + ("deep/" * 3) + "image.jpg": b"x"}
    cloud = FakeR2(objects)
    with tempfile.TemporaryDirectory() as td:
        cat = p.catalog(cloud, "bucket")
        assert {x["site"] for x in cat["recordings"]} == {"site1", "site2"}
        assert cat["dates"] == ["2026-09-03", "2026-09-05"]
        assert p.catalog(cloud, "bucket", "site1", "2026-09-03")["recordings"]
        assert not any(c.get("Prefix", "").startswith("classifier-crops-history") and not c.get("Delimiter") for c in cloud.list_calls)
        old_probe, old_frame = p._probe, p._frame
        p._probe = lambda path: (1.5, 20.0, 640, 360)
        p._frame = lambda path, elapsed: (b"\xff\xd8jpeg", 1.5 + elapsed)
        try:
            a0 = p._epoch("20260903-104834")
            for delta in (-2, 0, 2):
                out = p.frames(cloud, "bucket", Path(td), ka, kb, a0 + 10, delta)
                assert out["frames"][1]["requested_ts"] == a0 + 10 + delta
            assert p.frames(cloud, "bucket", Path(td), ka, kb, None, 0)["ts"] == a0 + 3
            p.frames(cloud, "bucket", Path(td), ka, kb, a0 + 10, 0)
            assert cloud.gets == 2
            p.frames(cloud, "bucket", Path(td), ka, kc, a0 + 10, 0)
            p.frames(cloud, "bucket", Path(td), ka, kb, a0 + 10, 0)  # B may evict, but A must not.
            errors = []
            def concurrent(other):
                try: p.frames(cloud, "bucket", Path(td), ka, other, a0 + 10, 0)
                except Exception as exc: errors.append(exc)
            threads = [threading.Thread(target=concurrent, args=(kb,)), threading.Thread(target=concurrent, args=(kc,))]
            [t.start() for t in threads]; [t.join() for t in threads]
            assert not errors
            many = [f"site1/cam{i}/20260903-1048{i:02}.mkv" for i in range(6, 12)]
            cloud.objects.update({key: b"q" * 10 for key in many})
            for key in many:
                expect(lambda key=key: p.frames(cloud, "bucket", Path(td), ka, key, a0 + 1000, 0), (ValueError, LookupError))
            assert len(list(Path(td).glob("*.mkv"))) <= 2
            expect(lambda: p.frames(cloud, "bucket", Path(td), ka, kb, a0 + 3, -2), ValueError)
            expect(lambda: p.frames(cloud, "bucket", Path(td), ka, ka, a0 + 10, 0), ValueError)
            expect(lambda: p.frames(cloud, "bucket", Path(td), "site1/../cam3/20260903-104834.mkv", kb), ValueError)
            expect(lambda: p.frames(cloud, "bucket", Path(td), ka, kb, a0, 601), ValueError)
            assert p.PTS.search(b"showinfo n:1 pts_time:999 showinfo n:0 pts_time:-1.25e+00").group(1) == b"-1.25e+00"
        finally: p._probe, p._frame = old_probe, old_frame
        oversized = FakeR2({ka: b"xx", kb: b"yy"})
        with patch.object(p, "MAX_SOURCE", 1): expect(lambda: p.frames(oversized, "b", Path(td), ka, kb), ValueError)
        changed = FakeR2({ka: b"a", kb: b"b"})
        with patch.object(changed, "head_object", lambda **kw: {"ContentLength": 2, "ETag": '"changed"'}):
            expect(lambda: p._retrieve(changed, "b", ka, Path(td)), p.RecordingUnavailable)
        partial_dir = Path(td) / "partial"
        expect(lambda: p._retrieve(PartialR2({ka: b"complete"}), "b", ka, partial_dir), p.RecordingUnavailable)
        assert not list(partial_dir.glob("*.mkv"))
        wrong = Path(td) / "wrong.mkv"; wrong.write_bytes(b"not a Matroska recording")
        expect(lambda: p._probe(wrong), p.RecordingUnavailable)
        expect(lambda: p._frame(wrong, 0), p.RecordingUnavailable)
        expect(lambda: p._key("site1/../20260903-104834.mkv"), ValueError)
        # Same-size local mutation changes mtime/inode identity and is rejected.
        p._probe = lambda path: (0.0, 20.0, 1, 1)
        def mutate(path, elapsed):
            path.write_bytes(b"z" * path.stat().st_size); os.utime(path, None); return b"\xff\xd8x", 0.0
        p._frame = mutate
        try: expect(lambda: p.frames(cloud, "bucket", Path(td), ka, kb, a0 + 10, 0), p.RecordingUnavailable)
        finally: p._probe, p._frame = old_probe, old_frame
    print("paired review core tests passed")

def stage2_test():
    ka = "site1/cam3/20260903-104834.mkv"; kb = "site1/cam4/20260903-104837.mkv"
    cloud = FakeR2({ka: b"a" * 10, kb: b"b" * 10})
    now = datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc)
    with tempfile.TemporaryDirectory() as td:
        old_probe, old_frame = p._probe, p._frame
        p._probe = lambda path: (1.5, 20.0, 640, 360)
        p._frame = lambda path, elapsed: (b"\xff\xd8jpeg", 1.5 + elapsed)
        try:
            body = {"schema_version": 1, "mode": "overlap", "camera_ids": ["cam3", "cam4"], "title": "north", "delta_s": 0, "zones": [{"x": .1, "y": .1, "width": .2, "height": .2}, {"x": .6, "y": .1, "width": .2, "height": .2}]}
            cal = p.save_calibration(cloud, "bucket", "site1", body, now)
            assert len(cal["id"]) == 64 and p.save_calibration(cloud, "bucket", "site1", body, now)["id"] == cal["id"]
            assert len(p.list_calibrations(cloud, "bucket", "site1")["calibrations"]) == 1
            jpeg_hash = hashlib.sha256(b"\xff\xd8jpeg").hexdigest()
            example_body = {"calibration_id": cal["id"], "source_keys": [ka, kb], "ts": p._epoch("20260903-104834") + 10, "interval": [0, 1], "journey_label": "truck", "verdict": "same", "views": [{"side": "a", "visibility": "full", "bbox": {"x": .1, "y": .1, "width": .2, "height": .2}, "jpeg_sha256": jpeg_hash}, {"side": "b", "visibility": "partial", "bbox": {"x": .2, "y": .2, "width": .2, "height": .2}, "jpeg_sha256": jpeg_hash}]}
            saved = p.save_example(cloud, "bucket", Path(td), "site1", "2026-09-03", example_body, "alice", now)
            expected_interval = [p._epoch("20260903-104834") + 3, p._epoch("20260903-104834") + 18.5]
            assert saved["curator"] == "alice" and saved["expires_at"] != saved["created_at"] and saved["interval"] == expected_interval
            assert p.get_example(cloud, "bucket", "site1", "2026-09-03", saved["id"], "alice", [], now)["interval"] == expected_interval
            listed = p.list_examples(cloud, "bucket", "site1", "2026-09-03", "alice", [], now)
            assert listed["examples"][0]["can_delete"] is True and listed["examples"][0]["interval"] == expected_interval
            assert p.list_examples(cloud, "bucket", "site1", "2026-09-03", "bob", ["reviewer"], now)["examples"][0]["can_delete"] is False
            assert p.list_examples(cloud, "bucket", "site1", "2026-09-03", "reviewer", ["reviewer"], now)["examples"][0]["can_delete"] is True
            assert p.get_example_frame(cloud, "bucket", "site1", "2026-09-03", saved["id"], "a", now) == b"\xff\xd8jpeg"
            stale = dict(example_body); stale["views"] = [dict(v) for v in example_body["views"]]; stale["views"][0]["jpeg_sha256"] = "0" * 64
            try: p.save_example(cloud, "bucket", Path(td), "site1", "2026-09-03", stale, "alice", now); raise AssertionError("stale hash accepted")
            except ValueError: pass
            mismatch = dict(example_body); mismatch["source_keys"] = [ka, "site1/cam9/20260903-104837.mkv"]
            try: p.save_example(cloud, "bucket", Path(td), "site1", "2026-09-03", mismatch, "alice", now); raise AssertionError("camera mismatch accepted")
            except (ValueError, p.RecordingMissing): pass
            try: p.delete_example(cloud, "bucket", "site1", "2026-09-03", saved["id"], "bob", [], now); raise AssertionError("other curator deleted")
            except PermissionError: pass
            assert p.delete_example(cloud, "bucket", "site1", "2026-09-03", saved["id"], "reviewer", ["reviewer"], now) == {"ok": True}
            assert not p.list_examples(cloud, "bucket", "site1", "2026-09-03", "alice", [], now)["examples"]
            assert ka in cloud.objects and kb in cloud.objects
            failed = FailingPutR2({ka: b"a" * 10, kb: b"b" * 10})
            failed.objects[p._calibration_key("site1", cal["id"])] = cloud.objects[p._calibration_key("site1", cal["id"])]
            try: p.save_example(failed, "bucket", Path(td), "site1", "2026-09-03", example_body, "alice", now); raise AssertionError("partial save succeeded")
            except p.RecordingUnavailable: pass
            assert not any(k.endswith("example.json") for k in failed.objects)
            expired = p.save_example(cloud, "bucket", Path(td), "site1", "2026-09-03", example_body, "alice", now)
            assert not p.list_examples(cloud, "bucket", "site1", "2026-09-03", "alice", [], now + timedelta(days=8))["examples"]
            p.cleanup(cloud, "bucket", Path(td), now + timedelta(days=8))
            assert not any(expired["id"] in k for k in cloud.objects)
            assert p.list_calibrations(cloud, "bucket", "site1")["calibrations"]
            orphan = "curation-paired/v1/examples/site1/2026-09-03/" + ("a" * 32)
            cloud.objects[orphan + "/a.jpg"] = b"orphan"; cloud.last_modified[orphan + "/a.jpg"] = now - timedelta(days=8)
            p.cleanup(cloud, "bucket", Path(td), now)
            assert orphan + "/a.jpg" not in cloud.objects
            mixed = "curation-paired/v1/examples/site1/2026-09-03/" + ("b" * 32)
            cloud.objects[mixed + "/a.jpg"] = b"old"; cloud.objects[mixed + "/stray.bin"] = b"recent"
            cloud.last_modified[mixed + "/a.jpg"] = now - timedelta(days=8); cloud.last_modified[mixed + "/stray.bin"] = now - timedelta(days=1)
            p.cleanup(cloud, "bucket", Path(td), now)
            assert mixed + "/a.jpg" in cloud.objects and mixed + "/stray.bin" in cloud.objects
        finally: p._probe, p._frame = old_probe, old_frame
    print("paired stage2 persistence tests passed")

def http_test():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for source in Path(".").glob("*.py"): shutil.copy(source, root / source.name)
        shutil.copytree("static", root / "static"); shutil.copy("config.example.yaml", root / "config.example.yaml")
        (root / "dataset").mkdir(); (root / "dataset" / "curators.yaml").write_text("tester: secret\n")
        child = r'''
import asyncio, httpx, os, sys, threading, types
for key in list(os.environ):
    if key.startswith(("OFFLOAD_", "AWS_")) or key in {"PASSWORD", "SUPERVISOR"}:
        os.environ.pop(key, None)
os.environ.update(FIELDKIT_MODE="curation", REVIEWERS="", SUPERVISOR="", PASSWORD="")
class ForbiddenBoto3(types.ModuleType):
    def __init__(self): super().__init__("boto3")
    def client(self, *args, **kwargs): raise AssertionError("real cloud client constructed")
sys.modules["boto3"] = ForbiddenBoto3()
original_start = threading.Thread.start
def start_without_sync(self):
    if getattr(getattr(self, "_target", None), "__name__", "") in ("sync_loop", "maintenance_loop"):
        return
    return original_start(self)
threading.Thread.start = start_without_sync
try:
    import app
finally:
    threading.Thread.start = original_start
class Missing(Exception):
    response = {"Error": {"Code": "NoSuchKey"}}
class R2:
    def __init__(self): self.objects = {}
    def get_paginator(self, _): return self
    def paginate(self, **kw):
        prefix = kw.get("Prefix", "")
        yield {"CommonPrefixes": [{"Prefix": "site1/"}], "Contents": [{"Key": k, "Size": len(v)} for k, v in self.objects.items() if k.startswith(prefix)]}
    def head_object(self, **kw):
        if kw["Key"] not in self.objects: raise Missing()
        return {"ContentLength": len(self.objects[kw["Key"]])}
    def get_object(self, **kw):
        if kw["Key"] not in self.objects: raise Missing()
        import io
        return {"Body": io.BytesIO(self.objects[kw["Key"]])}
    def put_object(self, **kw):
        body = kw["Body"].read() if hasattr(kw["Body"], "read") else kw["Body"]
        self.objects[kw["Key"]] = body
        return {"ok": True}
app.r2 = lambda: (None, R2(), "bucket")
async def main():
    transport = httpx.ASGITransport(app=app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        from starlette.requests import Request
        events = iter(({"type":"http.request", "body":b"x" * 65536, "more_body":True}, {"type":"http.request", "body":b"y", "more_body":False}))
        async def receive(): return next(events)
        try: await app._paired_body(Request({"type":"http", "method":"POST", "path":"/", "headers":[]}, receive))
        except app.HTTPException as exc: assert exc.status_code == 400
        else: raise AssertionError("chunked oversized body accepted")
        r = await c.get("/api/dataset/paired/recordings", headers={"x-curator-token":"secret"})
        assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
        r = await c.get("/api/dataset/paired/recordings")
        assert r.status_code == 401 and r.headers["cache-control"] == "no-store"
        assert (await c.get("/api/dataset/paired/recordings", headers={"x-curator-token":"bad"})).status_code == 401
        assert (await c.get("/api/dataset/paired/frames", params={"source_a":"bad", "source_b":"bad"})).status_code == 401
        assert (await c.get("/api/cameras", headers={"x-curator-token":"secret"})).status_code == 404
        assert (await c.get("/api/dataset/paired/frames", params={"source_a":"../bad", "source_b":"site1/cam4/20260903-104837.mkv"}, headers={"x-curator-token":"secret"})).status_code == 400
        assert (await c.post("/api/dataset/paired/site1/calibrations", content="{", headers={"x-curator-token":"secret"})).status_code == 400
        assert (await c.post("/api/dataset/paired/site1/calibrations", content="x" * 65537, headers={"x-curator-token":"secret"})).status_code == 400
        seen = {}
        app.paired_review.save_example = lambda *args: seen.update(curator=args[-1]) or {"curator": args[-1]}
        forged = {"curator": "forged", "calibration_id": "0" * 64, "source_keys": [], "ts": 0, "journey_label": "x", "verdict": "same", "views": []}
        assert (await c.post("/api/dataset/paired/site1/2026-09-03/examples", json=forged, headers={"x-curator-token":"secret"})).status_code == 200
        assert seen["curator"] == "tester"
        original_save = app._save_paired_calibration
        async def health_during_slow_save():
            import time
            app._save_paired_calibration = lambda *args: (time.sleep(.3) or {})
            started = time.monotonic()
            saving = asyncio.create_task(c.post("/api/dataset/paired/site1/calibrations", json={}, headers={"x-curator-token":"secret"}))
            await asyncio.sleep(.02)
            health = await c.get("/healthz")
            assert time.monotonic() - started < .25
            await saving
            app._save_paired_calibration = original_save
        await health_during_slow_save()
        assert (await c.get("/api/dataset/paired/frames", params={"source_a":"site1/cam3/20260903-104834.mkv", "source_b":"site1/cam4/20260903-104837.mkv", "ts":"nan"}, headers={"x-curator-token":"secret"})).status_code == 400
        missing = {"source_a":"site1/cam3/20260903-104834.mkv", "source_b":"site1/cam4/20260903-104837.mkv"}
        assert (await c.get("/api/dataset/paired/frames", params=missing, headers={"x-curator-token":"secret"})).status_code == 404
        app.paired_review.frames = lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("recordings have no common interval"))
        r = await c.get("/api/dataset/paired/frames", params=missing, headers={"x-curator-token":"secret"})
        assert r.status_code == 400 and r.headers["cache-control"] == "no-store"
        app.r2 = lambda: (_ for _ in ()).throw(RuntimeError("secret endpoint credentials"))
        r = await c.get("/api/dataset/paired/recordings", headers={"x-curator-token":"secret"})
        assert r.status_code == 503 and "secret" not in r.text and "endpoint" not in r.text
asyncio.run(main()); print("isolated curation HTTP tests passed", flush=True)
'''
        env = os.environ.copy(); env["FIELDKIT_MODE"] = "curation"; env["PYTHONPATH"] = str(root)
        subprocess.run([sys.executable, "-c", child], cwd=root, env=env, check=True)
        console = '''import asyncio, httpx, os, sys, threading, types
for key in list(os.environ):
    if key.startswith(("OFFLOAD_", "AWS_")) or key in {"PASSWORD", "SUPERVISOR"}:
        os.environ.pop(key, None)
os.environ.update(FIELDKIT_MODE="console", REVIEWERS="", SUPERVISOR="", PASSWORD="")
class ForbiddenBoto3(types.ModuleType):
    def __init__(self): super().__init__("boto3")
    def client(self, *args, **kwargs): raise AssertionError("real cloud client constructed")
sys.modules["boto3"] = ForbiddenBoto3()
original_start = threading.Thread.start
def start_without_background(self):
    return
threading.Thread.start = start_without_background
try:
    import app
finally:
    threading.Thread.start = original_start
if getattr(app, "DETECT", None): app.DETECT.stop = lambda: None
async def main():
    t = httpx.ASGITransport(app=app.app)
    async with httpx.AsyncClient(transport=t, base_url="http://test") as c:
        assert (await c.get("/api/dataset/paired/recordings")).status_code == 404
asyncio.run(main()); print("isolated console paired-route 404 passed", flush=True)
'''
        console_env = os.environ.copy(); console_env["FIELDKIT_MODE"] = "console"; console_env["PYTHONPATH"] = str(root)
        subprocess.run([sys.executable, "-c", console], cwd=root, env=console_env, check=True)

if __name__ == "__main__":
    main(); stage2_test(); http_test()
