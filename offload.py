#!/usr/bin/env python3
"""Optional Cloudflare R2 offload: upload the oldest finished segments, then delete them.

Idle unless config.yaml sets offload.enabled — a self-hosting node never talks to a cloud.
"""

import base64
import hashlib
import shutil
import threading
import time
from pathlib import Path

from recorder import SEGMENT_SECONDS

# ffmpeg only ever appends to the newest segment, so anything older than one segment
# plus a margin is closed and safe to ship.
FINISHED = SEGMENT_SECONDS + 60
SWEEP_SECONDS = 60


def sha256_b64(path):
    """R2 verifies this server-side and rejects the PUT if the bytes disagree."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return base64.b64encode(h.digest()).decode()


class Offload:
    def __init__(self, cfg, rec_root):
        self.cfg = cfg              # the live CONFIG dict: config edits land without a reload
        self.rec_root = Path(rec_root)
        self.client = None
        self.uploaded = 0
        self.last_file = ""
        self.last_error = ""

    def opts(self):
        return self.cfg.get("offload") or {}

    def info(self):
        return {"enabled": bool(self.opts().get("enabled")), "uploaded": self.uploaded,
                "last_file": self.last_file, "last_error": self.last_error}

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                self.sweep()
            except Exception as e:          # a bad sweep must not kill the thread
                self.last_error = f"{type(e).__name__}: {e}"
                print(f"offload: {self.last_error}", flush=True)
            time.sleep(SWEEP_SECONDS)

    def free_gb(self):
        return shutil.disk_usage(self.rec_root).free / 1e9

    def finished(self):
        """Closed segments under <record_dir>/<site>/<cam>/, oldest first."""
        cutoff = time.time() - FINISHED
        files = [f for f in self.rec_root.glob("*/*/*.mkv")
                 if f.stat().st_mtime < cutoff and f.stat().st_size]
        return sorted(files, key=lambda f: f.stat().st_mtime)

    def _get_client(self, o):
        if self.client is not None:
            return self.client
        missing = [k for k in ("account_id", "access_key_id", "secret_access_key")
                   if not o.get(k)]
        if missing:
            # An enrolled node's blanks are the console's job to fill (offload_creds,
            # first beat of each session) — say "waiting", not "you forgot".
            ops = self.cfg.get("ops") or {}
            self.last_error = (
                "offload enabled — awaiting credentials from console"
                if all(ops.get(k) for k in ("url", "token", "hive"))
                else "offload enabled but config is missing: " + ", ".join(missing))
            return None
        try:
            import boto3
            from botocore.config import Config
        except ImportError:
            self.last_error = "offload enabled but boto3 missing — pip install boto3"
            print(f"offload: {self.last_error}", flush=True)
            return None
        self.client = boto3.client(
            "s3", endpoint_url=f"https://{o['account_id']}.r2.cloudflarestorage.com",
            aws_access_key_id=o["access_key_id"],
            aws_secret_access_key=o["secret_access_key"], region_name="auto",
            # boto3 >= 1.36 adds its own CRC32 by default, which this S3-compatible
            # endpoint rejects; send only the sha256 we compute.
            config=Config(request_checksum_calculation="when_required",
                          response_checksum_validation="when_required"))
        return self.client

    def sweep(self):
        """One pass: upload oldest-first until the drive is back above the floor."""
        o = self.opts()
        if not o.get("enabled"):
            return
        floor = float(o.get("min_free_gb", 20))
        if self.free_gb() >= floor:
            return
        client = self._get_client(o)
        if client is None:
            return
        bucket = o.get("bucket") or "fieldkit-recordings"
        for f in self.finished():
            key = "/".join(f.parts[-3:])              # <site>/<cam>/<segment>.mkv
            try:
                with open(f, "rb") as body:
                    client.put_object(Bucket=bucket, Key=key, Body=body,
                                      ChecksumSHA256=sha256_b64(f))
            except Exception as e:
                # Any failure at all — network, credentials, checksum mismatch — keeps
                # the local copy. Footage is only ever deleted after a verified upload.
                self.last_error = f"{key}: {e}"
                print(f"offload: {self.last_error}", flush=True)
                return
            f.unlink()
            self.uploaded += 1
            self.last_file, self.last_error = key, ""
            print(f"offload: uploaded {key}", flush=True)
            if self.free_gb() >= floor:
                return


if __name__ == "__main__":
    import os
    import tempfile

    class FakeClient:
        """Records puts and checks the checksum the way R2 does."""
        def __init__(self, fail=False):
            self.puts, self.fail = [], fail

        def put_object(self, **kw):
            if self.fail:
                raise RuntimeError("boom")
            body = kw["Body"].read()
            assert kw["ChecksumSHA256"] == base64.b64encode(
                hashlib.sha256(body).digest()).decode(), "checksum did not match the body"
            self.puts.append(kw["Key"])

    def tree():
        """Two closed segments (older one first) plus the one ffmpeg is still writing."""
        root = Path(tempfile.mkdtemp())
        d = root / "site1" / "cam1"
        d.mkdir(parents=True)
        old = time.time() - FINISHED - 600
        for i, name in enumerate(("seg-a.mkv", "seg-b.mkv")):
            (d / name).write_bytes(b"x" * (10 + i))
            os.utime(d / name, (old + i * 600, old + i * 600))
        (d / "seg-live.mkv").write_bytes(b"live")
        return root, d

    CFG = {"offload": {"enabled": True, "bucket": "b", "min_free_gb": 10}}

    # Full drive: ships every closed segment, oldest first, and deletes only after the PUT.
    root, d = tree()
    o = Offload(CFG, root)
    o.client, o.free_gb = FakeClient(), lambda: 0.0
    o.sweep()
    assert o.client.puts == ["site1/cam1/seg-a.mkv", "site1/cam1/seg-b.mkv"], o.client.puts
    assert not (d / "seg-a.mkv").exists() and not (d / "seg-b.mkv").exists()
    assert (d / "seg-live.mkv").exists(), "uploaded a segment ffmpeg is still writing"
    assert o.uploaded == 2 and not o.last_error, o.info()

    # Stops the moment the floor is met again — it frees space, it does not drain the disk.
    root, d = tree()
    o = Offload(CFG, root)
    seq = [0.0, 99.0]                  # empty at entry, healthy after one delete
    o.client, o.free_gb = FakeClient(), lambda: seq.pop(0) if seq else 99.0
    o.sweep()
    assert o.client.puts == ["site1/cam1/seg-a.mkv"], o.client.puts
    assert (d / "seg-b.mkv").exists(), "kept deleting past the floor"

    # A failed upload keeps the footage and surfaces the error.
    root, d = tree()
    o = Offload(CFG, root)
    o.client, o.free_gb = FakeClient(fail=True), lambda: 0.0
    o.sweep()
    assert sorted(f.name for f in d.glob("*.mkv")) == ["seg-a.mkv", "seg-b.mkv", "seg-live.mkv"]
    assert o.uploaded == 0 and "boom" in o.last_error, o.info()

    # Above the floor: nothing is read, nothing is sent.
    root, d = tree()
    o = Offload(CFG, root)
    o.client, o.free_gb = FakeClient(fail=True), lambda: 99.0
    o.sweep()
    assert o.client.puts == [] and not o.last_error, o.info()

    # The every-node path: no offload block at all must be a silent no-op.
    plain = Offload({}, root)
    plain.sweep()
    assert plain.info()["enabled"] is False, plain.info()

    # Enabled but unconfigured: explain it, never crash the thread.
    half = Offload({"offload": {"enabled": True}}, root)
    half.free_gb = lambda: 0.0
    half.sweep()
    assert "account_id" in half.last_error, half.last_error
    assert "missing" in half.last_error, half.last_error

    # Same blanks on a node enrolled with a console: it is waiting, not misconfigured.
    OPS = {"url": "ws://console/ingest", "token": "tok", "hive": "kalambo"}
    waiting = Offload({"offload": {"enabled": True}, "ops": OPS}, root)
    waiting.free_gb = lambda: 0.0
    waiting.sweep()
    assert waiting.last_error == "offload enabled — awaiting credentials from console", \
        waiting.last_error
    # A half-filled ops block is not enrolled: back to the plain missing-keys message.
    lone = Offload({"offload": {"enabled": True}, "ops": dict(OPS, token="")}, root)
    lone.free_gb = lambda: 0.0
    lone.sweep()
    assert "missing" in lone.last_error, lone.last_error

    print("offload self-check ok: oldest-first, verified, deletes only after upload")
