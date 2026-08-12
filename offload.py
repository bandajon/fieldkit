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

from hive import enrolled
from recorder import SEGMENT_SECONDS

# ffmpeg only ever appends to the newest segment, so anything older than one segment
# plus a margin is closed and safe to ship.
FINISHED = SEGMENT_SECONDS + 60
SWEEP_SECONDS = 60
CRED_KEYS = ("account_id", "access_key_id", "secret_access_key")
AWAITING = "offload enabled — awaiting credentials from console"


def sha256_b64(path):
    """R2 verifies this server-side and rejects the PUT if the bytes disagree."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return base64.b64encode(h.digest()).decode()


class Offload:
    def __init__(self, cfg, rec_root, console_creds=None):
        self.cfg = cfg              # the live CONFIG dict: config edits land without a reload
        # Creds the ops console pushed (hive.py writes this same dict). Separate from
        # cfg because cfg gets dumped back to config.yaml; a pushed secret must not.
        self.console_creds = {} if console_creds is None else console_creds
        self.rec_root = Path(rec_root)
        self.client = None
        self.client_creds = None    # the triple self.client was built with
        self.uploaded = 0
        self.last_file = ""
        self.last_error = ""

    def opts(self):
        """Console creds under config.yaml: the operator wins every field they set.

        Unset means absent, null or empty string — but `enabled: false` and
        `min_free_gb: 0` are real answers and survive the merge.
        """
        merged = dict(self.console_creds)
        merged.update((k, v) for k, v in (self.cfg.get("offload") or {}).items()
                      if v is not None and v != "")
        return merged

    def cred_status(self, o=None):
        """Why offload cannot run yet, or "". Config alone — no disk, no client build,
        so the Status tab says "waiting on the console" before the drive ever fills."""
        o = self.opts() if o is None else o
        missing = [k for k in CRED_KEYS if not o.get(k)]
        if not (o.get("enabled") and missing):
            return ""
        return (AWAITING if enrolled(self.cfg)
                else "offload enabled but config is missing: " + ", ".join(missing))

    def info(self):
        return {"enabled": bool(self.opts().get("enabled")), "uploaded": self.uploaded,
                "last_file": self.last_file, "last_error": self.last_error,
                "status": self.cred_status()}

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
        creds = tuple(o.get(k) or "" for k in CRED_KEYS)
        if self.client is not None and creds == self.client_creds:
            return self.client
        if not all(creds):
            self.last_error = self.cred_status(o)
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
        self.client_creds = creds     # rotated creds rebuild rather than reuse this
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

    CREDS = {"account_id": "a", "access_key_id": "k", "secret_access_key": "s"}
    TRIPLE = ("a", "k", "s")        # what a client built from CREDS is tagged with
    CFG = {"offload": {"enabled": True, "bucket": "b", "min_free_gb": 10, **CREDS}}

    # Full drive: ships every closed segment, oldest first, and deletes only after the PUT.
    root, d = tree()
    o = Offload(CFG, root)
    o.client, o.client_creds, o.free_gb = FakeClient(), TRIPLE, lambda: 0.0
    o.sweep()
    assert o.client.puts == ["site1/cam1/seg-a.mkv", "site1/cam1/seg-b.mkv"], o.client.puts
    assert not (d / "seg-a.mkv").exists() and not (d / "seg-b.mkv").exists()
    assert (d / "seg-live.mkv").exists(), "uploaded a segment ffmpeg is still writing"
    assert o.uploaded == 2 and not o.last_error, o.info()

    # Stops the moment the floor is met again — it frees space, it does not drain the disk.
    root, d = tree()
    o = Offload(CFG, root)
    seq = [0.0, 99.0]                  # empty at entry, healthy after one delete
    o.client, o.client_creds = FakeClient(), TRIPLE
    o.free_gb = lambda: seq.pop(0) if seq else 99.0
    o.sweep()
    assert o.client.puts == ["site1/cam1/seg-a.mkv"], o.client.puts
    assert (d / "seg-b.mkv").exists(), "kept deleting past the floor"

    # A failed upload keeps the footage and surfaces the error.
    root, d = tree()
    o = Offload(CFG, root)
    o.client, o.client_creds, o.free_gb = FakeClient(fail=True), TRIPLE, lambda: 0.0
    o.sweep()
    assert sorted(f.name for f in d.glob("*.mkv")) == ["seg-a.mkv", "seg-b.mkv", "seg-live.mkv"]
    assert o.uploaded == 0 and "boom" in o.last_error, o.info()

    # Above the floor: nothing is read, nothing is sent.
    root, d = tree()
    o = Offload(CFG, root)
    o.client, o.client_creds, o.free_gb = FakeClient(fail=True), TRIPLE, lambda: 99.0
    o.sweep()
    assert o.client.puts == [] and not o.last_error, o.info()

    # The merge: console creds fill the blanks, every field config.yaml sets wins.
    console = {"account_id": "acc", "access_key_id": "AK", "secret_access_key": "SK",
               "bucket": "console-bucket"}
    m = Offload({"offload": {"enabled": False, "bucket": "operator-bucket",
                             "account_id": ""}}, root, console_creds=console).opts()
    assert m["bucket"] == "operator-bucket", m         # operator wins
    assert m["access_key_id"] == "AK", m               # console fills what is unset
    assert m["account_id"] == "acc", m                 # "" in config.yaml is not a value
    assert m["enabled"] is False, m                    # but a real False is, and survives
    # An empty `offload:` block parses as None; console creds still apply.
    assert Offload({"offload": None}, root, console_creds=console).opts() == console

    # Rotation: replacing the overlay must not keep uploading with the revoked key.
    rotating = dict(CREDS)
    rot = Offload({"offload": {"enabled": True, "min_free_gb": 10}}, root,
                  console_creds=rotating)
    stale = FakeClient()
    rot.client, rot.client_creds = stale, TRIPLE
    assert rot._get_client(rot.opts()) is stale, "rebuilt a client on unchanged creds"
    rotating["access_key_id"] = "rotated"              # console pushed a new key
    assert rot._get_client(rot.opts()) is not stale, "reused a client built on old creds"

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
    assert half.info()["status"] == half.last_error, half.info()   # same words, no disk IO

    # Same blanks on a node enrolled with a console: it is waiting, not misconfigured —
    # and it says so on the Status tab straight away, not only once the drive fills.
    OPS = {"url": "ws://console/ingest", "token": "tok", "hive": "kalambo"}
    waiting = Offload({"offload": {"enabled": True}, "ops": OPS}, root)
    assert waiting.info()["status"] == AWAITING, waiting.info()
    assert not waiting.info()["last_error"], "nothing has failed yet"
    waiting.free_gb = lambda: 0.0
    waiting.sweep()
    assert waiting.last_error == AWAITING, waiting.last_error
    # Creds in hand: nothing to report, disk untouched either way.
    assert not Offload({"offload": {"enabled": True}, "ops": OPS}, root,
                       console_creds=console).info()["status"]
    assert not Offload({"offload": {"enabled": False}, "ops": OPS}, root).info()["status"]
    # A half-filled ops block is not enrolled: back to the plain missing-keys message.
    lone = Offload({"offload": {"enabled": True}, "ops": dict(OPS, token="")}, root)
    lone.free_gb = lambda: 0.0
    lone.sweep()
    assert "missing" in lone.last_error, lone.last_error

    print("offload self-check ok: oldest-first, verified, deletes only after upload")
