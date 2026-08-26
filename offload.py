#!/usr/bin/env python3
"""Optional Cloudflare R2 offload: upload the oldest finished segments, then delete them.

Idle unless config.yaml sets offload.enabled — a self-hosting node never talks to a cloud.

offload.recycle picks which footage a full drive loses. Off (the default), a segment is
only ever deleted after a verified upload, so a drive that fills with nowhere to send its
footage stops recording — loudly, on purpose. On, the oldest finished segments are deleted
unsent once the drive is below min_free_gb, and recording never stops: the node becomes a
ring buffer. That is the whole trade — lose the oldest footage, or lose the newest. A site
on a metered link cannot afford to ship ~317 MB per camera every ten minutes, so it
chooses to lose the oldest, deliberately.
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
# What a node contributes to the shared labelling queue: what detect.py captured, and
# nothing else. dataset_sync's default prefix is where the curation node already looks.
SAMPLES = ("pending/images", "pending/labels", "pending/attrs")


def marker(f):
    """`<segment>.mkv.uploaded` — an empty flag meaning "this one is in the bucket".
    finished() globs *.mkv, so markers are never mistaken for segments."""
    return f.with_name(f.name + ".uploaded")


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
        self.mirror_override = None   # None: config.yaml decides. Set from the Record tab.
        self.contributed = 0
        self.contribute_override = None

    def set_mirror(self, on):
        """Runtime toggle. None hands the decision back to config.yaml."""
        self.mirror_override = None if on is None else bool(on)

    def mirroring(self, o):
        return self.mirror_override if self.mirror_override is not None else bool(o.get("mirror"))

    def set_contribute(self, on):
        """Runtime toggle. None hands the decision back to config.yaml."""
        self.contribute_override = None if on is None else bool(on)

    def contributing(self, o):
        return (self.contribute_override if self.contribute_override is not None
                else bool(o.get("contribute")))

    def pending_samples(self):
        """Captured and not yet handed over — one label file per sample. Cheap enough
        for a status poll: detect.py caps what lives in pending."""
        import dataset_sync
        return len(list((dataset_sync.DATASET / "pending" / "labels").glob("*.txt")))

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
        if o.get("recycle") and not (self.mirroring(o) or self.contributing(o)):
            return ""     # sends nothing by design: not misconfigured, not waiting
        return (AWAITING if enrolled(self.cfg)
                else "offload enabled but config is missing: " + ", ".join(missing))

    def info(self):
        o = self.opts()
        return {"enabled": bool(o.get("enabled")), "uploaded": self.uploaded,
                "mirror": self.mirroring(o), "override": self.mirror_override,
                "mirrored": len(list(self.rec_root.glob("*/*/*.mkv.uploaded"))),
                "recycle": bool(o.get("recycle")),
                "contribute": self.contributing(o),
                "contribute_override": self.contribute_override,
                "contributed": self.contributed, "pending_samples": self.pending_samples(),
                "last_file": self.last_file, "last_error": self.last_error,
                "status": self.cred_status(o)}

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

    def storage_key(self, f):
        """Where a segment lives in the bucket: <toll gate>/<camera>/<segment>.mkv.

        The local tree is named for the operator's convenience; the bucket is named for
        the authority that has to find things in it later, which is the RDA toll gate id
        (RDA-TG-KTB and friends). Falls back to the local site name when no gate id is
        configured, which is what every node did before this existed."""
        gate = str(self.opts().get("toll_gate_id")
                   or self.cfg.get("toll_gate_id") or "").strip()
        parts = list(f.parts[-3:])                    # <site>/<cam>/<segment>.mkv
        if gate:
            parts[0] = gate
        return "/".join(parts)

    def _put(self, client, bucket, f):
        """Upload one segment. -> False with last_error set on any failure — network,
        credentials, checksum mismatch — so the local copy always survives it."""
        key = self.storage_key(f)
        try:
            with open(f, "rb") as body:
                client.put_object(Bucket=bucket, Key=key, Body=body,
                                  ChecksumSHA256=sha256_b64(f))
        except Exception as e:
            self.last_error = f"{key}: {e}"
            print(f"offload: {self.last_error}", flush=True)
            return False
        self.uploaded += 1
        self.last_file, self.last_error = key, ""
        print(f"offload: uploaded {key}", flush=True)
        return True

    def _contribute(self, client, bucket):
        """Hand this node's captured samples to the shared curation prefix. Every failure
        is swallowed: the samples stay on disk and the next sweep tries again."""
        try:
            import dataset_sync
            sent, _ = dataset_sync.push(client, bucket, names=SAMPLES,
                                        root=dataset_sync.DATASET)
        except Exception as e:
            self.last_error = f"contribute: {e}"
            print(f"offload: {self.last_error}", flush=True)
            return
        self.contributed += sent

    def sweep(self):
        """One pass: mirror everything unsent (if mirroring), upload oldest-first until
        the drive is back above the floor, then contribute what the cameras captured."""
        o = self.opts()
        if not o.get("enabled"):
            return
        floor = float(o.get("min_free_gb", 20))
        mirror, contribute = self.mirroring(o), self.contributing(o)
        recycle = bool(o.get("recycle"))
        below = self.free_gb() < floor
        if not (mirror or contribute or below):
            return
        # A recycling node may have no bucket at all — only mirror, contribute and the
        # upload-to-free-space pass need a client, and the ring buffer must turn either way.
        client = self._get_client(o) if (mirror or contribute or not recycle) else None
        if client is None and not recycle:
            return
        bucket = o.get("bucket") or "fieldkit-recordings"
        self._recordings(client, bucket, floor, mirror, below, recycle)
        # Deliberately independent of the footage passes above: a segment upload that
        # failed must not keep samples off the labelling queue, or the other way round.
        # pending_samples() first so an idle node never lists the bucket to send nothing.
        if contribute and self.pending_samples():
            self._contribute(client, bucket)

    def _recordings(self, client, bucket, floor, mirror, below, recycle=False):
        """The footage passes. Every early return means "keep it local, retry next sweep"."""
        if mirror and client is not None:
            for f in self.finished():
                if marker(f).exists():
                    continue
                if not self._put(client, bucket, f):
                    return                    # keep everything local; next sweep retries
                marker(f).touch()
            below = self.free_gb() < floor     # the mirror pass took time; re-read the drive
        if not below:
            return
        for f in self.finished():
            if marker(f).exists():            # already in the bucket: just reclaim the space
                marker(f).unlink()            # marker first: a crash here re-uploads, never orphans
                f.unlink()
                print(f"offload: freed {'/'.join(f.parts[-3:])} (mirrored)", flush=True)
            elif recycle:                     # the ring buffer: gone, and never sent
                f.unlink()
                print(f"offload: recycled {'/'.join(f.parts[-3:])} (not uploaded)", flush=True)
            elif self._put(client, bucket, f):
                f.unlink()
            else:
                return
            if self.free_gb() >= floor:
                return


if __name__ == "__main__":
    import os
    import tempfile

    class FakeClient:
        """Records puts and checks the checksum the way R2 does. The list/upload_file
        half is what dataset_sync.push drives."""
        def __init__(self, fail=False):
            self.puts, self.fail, self.objects = [], fail, {}

        def put_object(self, **kw):
            if self.fail:
                raise RuntimeError("boom")
            body = kw["Body"].read()
            assert kw["ChecksumSHA256"] == base64.b64encode(
                hashlib.sha256(body).digest()).decode(), "checksum did not match the body"
            self.puts.append(kw["Key"])

        def get_paginator(self, _op):
            return self

        def paginate(self, Bucket, Prefix):
            yield {"Contents": [{"Key": k, "Size": s} for k, s in self.objects.items()
                                if k.startswith(Prefix)]}

        def upload_file(self, path, Bucket, Key):
            if self.fail:
                raise RuntimeError("boom")
            self.objects[Key] = Path(path).stat().st_size
            self.puts.append(Key)

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

    # Mirror: every closed segment ships and STAYS, with a marker beside it.
    MIRROR = {"offload": dict(CFG["offload"], mirror=True)}
    root, d = tree()
    o = Offload(MIRROR, root)
    o.client, o.client_creds, o.free_gb = FakeClient(), TRIPLE, lambda: 99.0
    o.sweep()
    assert o.client.puts == ["site1/cam1/seg-a.mkv", "site1/cam1/seg-b.mkv"], o.client.puts
    assert (d / "seg-a.mkv").exists() and (d / "seg-b.mkv").exists(), "mirror deleted footage"
    assert (d / "seg-a.mkv.uploaded").exists() and (d / "seg-b.mkv.uploaded").exists()
    assert not (d / "seg-live.mkv.uploaded").exists(), "marked a segment still being written"
    assert o.info()["mirrored"] == 2 and o.info()["mirror"] is True, o.info()
    assert [f.name for f in o.finished()] == ["seg-a.mkv", "seg-b.mkv"], "markers listed as segments"

    o.sweep()                                    # second sweep: nothing left to send
    assert o.client.puts == ["site1/cam1/seg-a.mkv", "site1/cam1/seg-b.mkv"], o.client.puts

    # Runtime toggle: the operator's switch outranks config.yaml, both ways.
    root2, d2 = tree()
    t = Offload(CFG, root2)                      # config.yaml says mirror: false
    t.client, t.client_creds, t.free_gb = FakeClient(), TRIPLE, lambda: 99.0
    t.set_mirror(True)
    t.sweep()
    assert (d2 / "seg-a.mkv").exists() and (d2 / "seg-a.mkv.uploaded").exists(), "override ignored"
    assert t.info()["mirror"] is True and t.info()["override"] is True, t.info()
    t.set_mirror(None)                           # back to whatever the file says
    assert t.info()["mirror"] is False and t.info()["override"] is None, t.info()
    assert Offload(MIRROR, root2).info()["mirror"] is True, "config still decides unoverridden"

    # Disk pressure on a mirrored node: delete what is already safe, never re-upload it.
    seq = [0.0, 0.0, 99.0]                       # entry, after the mirror pass, after one delete
    o.free_gb = lambda: seq.pop(0) if seq else 99.0
    o.sweep()
    assert o.client.puts == ["site1/cam1/seg-a.mkv", "site1/cam1/seg-b.mkv"], o.client.puts
    assert not (d / "seg-a.mkv").exists() and not (d / "seg-a.mkv.uploaded").exists()
    assert (d / "seg-b.mkv").exists(), "kept deleting past the floor"
    assert o.uploaded == 2, "counted a delete as an upload"

    # A failed mirror upload leaves no marker — the next sweep must try again.
    root, d = tree()
    o = Offload(MIRROR, root)
    o.client, o.client_creds, o.free_gb = FakeClient(fail=True), TRIPLE, lambda: 99.0
    o.sweep()
    assert list(d.glob("*.uploaded")) == [], "marked a segment the bucket never got"
    assert (d / "seg-a.mkv").exists() and o.uploaded == 0 and "boom" in o.last_error, o.info()

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

    # ---- contribute: the node hands its own captured samples to the labelling queue.
    import dataset_sync
    samples = Path(tempfile.mkdtemp())
    (samples / "pending" / "images").mkdir(parents=True)
    (samples / "pending" / "labels").mkdir(parents=True)
    (samples / "pending" / "images" / "s1.jpg").write_bytes(b"jpeg")
    (samples / "pending" / "labels" / "s1.txt").write_text("0 0.5 0.5 0.1 0.1\n")
    (samples / "approved").mkdir()                    # not ours to send: curation owns it
    (samples / "approved" / "old.jpg").write_bytes(b"x")
    dataset_sync.DATASET = samples                    # where detect.py drops them

    # Off (the default): a healthy disk with samples waiting still sends nothing.
    root, d = tree()
    c = Offload(CFG, root)
    c.client, c.client_creds, c.free_gb = FakeClient(), TRIPLE, lambda: 99.0
    c.sweep()
    assert c.client.puts == [] and c.info()["contribute"] is False, c.info()
    assert c.info()["pending_samples"] == 1, c.info()

    # On: samples go to the curation prefix, footage does not, and the count is cumulative.
    c.set_contribute(True)
    c.sweep()
    # sorted: contribution runs on a thread pool, so arrival order is not a fact
    assert sorted(c.client.puts) == ["curation/pending/images/s1.jpg",
                                     "curation/pending/labels/s1.txt"], c.client.puts
    assert c.info()["contributed"] == 2 and c.info()["contribute_override"] is True, c.info()
    assert (samples / "pending" / "images" / "s1.jpg").exists(), "contribute deleted a sample"

    c.sweep()                                   # already in the bucket: nothing re-sent
    assert c.info()["contributed"] == 2, c.info()
    (samples / "pending" / "images" / "s2.jpg").write_bytes(b"jpeg2")
    (samples / "pending" / "labels" / "s2.txt").write_text("1 0.5 0.5 0.2 0.2\n")
    c.sweep()                                   # only the new sample moves
    assert sorted(c.client.puts[-2:]) == ["curation/pending/images/s2.jpg",
                                          "curation/pending/labels/s2.txt"], c.client.puts
    assert c.info()["contributed"] == 4, c.info()

    # config.yaml decides when the operator has not: contribute: true needs no console.
    c.set_contribute(None)
    assert c.info()["contribute"] is False and c.info()["contribute_override"] is None, c.info()
    assert Offload({"offload": dict(CFG["offload"], contribute=True)},
                   root).info()["contribute"] is True, "config key ignored"

    # A push that blows up keeps every sample and leaves the sweep alive to retry.
    bad = Offload({"offload": dict(CFG["offload"], contribute=True)}, root)
    bad.client, bad.client_creds, bad.free_gb = FakeClient(fail=True), TRIPLE, lambda: 99.0
    bad.sweep()                                 # must not raise
    assert bad.info()["contributed"] == 0 and "boom" in bad.last_error, bad.info()
    assert (samples / "pending" / "labels" / "s1.txt").exists(), "a failed push deleted a sample"
    assert bad.info()["pending_samples"] == 2, bad.info()

    # An empty pending pile never lists the bucket at all.
    for f in (samples / "pending").rglob("*.jpg"):
        f.unlink()
    for f in (samples / "pending" / "labels").glob("*.txt"):
        f.unlink()
    quiet = Offload({"offload": dict(CFG["offload"], contribute=True)}, root)
    quiet.client, quiet.client_creds, quiet.free_gb = FakeClient(), TRIPLE, lambda: 99.0
    quiet.sweep()
    assert quiet.client.puts == [] and quiet.info()["pending_samples"] == 0, quiet.info()

    # ---- recycle: on a metered link the drive is a ring buffer, not an upload queue.
    RECYCLE = {"offload": dict(CFG["offload"], recycle=True)}

    # On: below the floor the oldest goes, unsent, and the newest stays.
    root, d = tree()
    r = Offload(RECYCLE, root)
    seq = [0.0, 99.0]                            # empty at entry, healthy after one delete
    r.client, r.client_creds = FakeClient(), TRIPLE
    r.free_gb = lambda: seq.pop(0) if seq else 99.0
    r.sweep()
    assert r.client.puts == [], "recycle uploaded a segment"
    assert not (d / "seg-a.mkv").exists(), "the oldest survived a full drive"
    assert (d / "seg-b.mkv").exists(), "recycled past the floor"
    assert (d / "seg-live.mkv").exists(), "deleted a segment ffmpeg is still writing"
    assert r.uploaded == 0 and r.info()["recycle"] is True, r.info()

    # Off (the default): a full drive with a dead link keeps every frame and says why.
    root, d = tree()
    n = Offload(CFG, root)
    n.client, n.client_creds, n.free_gb = FakeClient(fail=True), TRIPLE, lambda: 0.0
    n.sweep()
    assert sorted(f.name for f in d.glob("*.mkv")) == ["seg-a.mkv", "seg-b.mkv", "seg-live.mkv"]
    assert n.info()["recycle"] is False and "boom" in n.last_error, n.info()

    # Either way a mirrored segment is reclaimed the same: marker first, never re-uploaded.
    for cfg in (MIRROR, {"offload": dict(MIRROR["offload"], recycle=True)}):
        root, d = tree()
        m = Offload(cfg, root)
        seq = [99.0, 0.0, 99.0]                  # entry, after the mirror pass, after a delete
        m.client, m.client_creds = FakeClient(), TRIPLE
        m.free_gb = lambda: seq.pop(0) if seq else 99.0
        m.sweep()
        assert m.client.puts == ["site1/cam1/seg-a.mkv", "site1/cam1/seg-b.mkv"], m.client.puts
        assert not (d / "seg-a.mkv").exists() and not (d / "seg-a.mkv.uploaded").exists()
        assert (d / "seg-b.mkv").exists() and (d / "seg-b.mkv.uploaded").exists()

    # No credentials at all is a valid recycling node — the ring buffer still has to turn,
    # and nothing is waiting on the console, so the Status tab stays quiet.
    root, d = tree()
    lte = Offload({"offload": {"enabled": True, "min_free_gb": 10, "recycle": True}}, root)
    seq = [0.0, 99.0]
    lte.free_gb = lambda: seq.pop(0) if seq else 99.0
    lte.sweep()
    assert not (d / "seg-a.mkv").exists(), "no creds, no recycling, disk fills forever"
    assert (d / "seg-b.mkv").exists() and not lte.info()["status"], lte.info()
    # Mirroring one is a different thing: it IS waiting on creds, and it must still recycle.
    both = Offload({"offload": {"enabled": True, "min_free_gb": 10, "recycle": True,
                                "mirror": True}}, root)
    seq2 = [0.0, 99.0]
    both.free_gb = lambda: seq2.pop(0) if seq2 else 99.0
    both.sweep()
    assert "missing" in both.info()["status"], both.info()
    assert not (d / "seg-b.mkv").exists(), "a mirroring node stopped recycling"

    print("offload self-check ok: oldest-first, verified, mirrors without deleting, "
          "deletes only after upload, contributes samples without losing one, "
          "recycles the oldest instead of shipping it")
