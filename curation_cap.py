#!/usr/bin/env python3
"""Per-gate pause and hard cap on the curation queue's pending samples.

213k pending samples piled up (206k from Katuba) because nothing ever thinned the
queue. The owner wants each gate held to its newest N pending samples, oldest
evicted first, and a switch to stop a gate sending more while it's paused. The
first real enforcement deletes ~200k samples irreversibly, so the cap starts
unset (report-only: log what WOULD be evicted) until the owner turns it on via
curation.yaml. Importable from the office Mac too (which reads the same
settings file from the bucket) — no app.py imports here.

The bucket is the one authority for curation.yaml: a node's local copy can be
mid-revert from a pull that ran earlier in the same sync pass, and trusting it
would let a stopped cap re-arm itself. enforce() always re-reads the bucket
copy right before it decides anything.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

SETTINGS = "curation.yaml"
DEFAULT_CAP = 5000   # the owner's target; used only to size the report while cap is unset
CAP_MIN, CAP_MAX = 100, 100000
# Pre-gate-id sample names, all from the Katuba box before it got a gate id.
LEGACY = {"", "cam3", "cam4", "loop-videos", "katuba", "site1"}
LEGACY_GATE = "RDA-TG-KTB"
_STEM = re.compile(r"^(.*?)-?(\d{8}-\d{6})$")


def _parse_settings(data):
    """A mapping straight off untrusted yaml (local file, or the bucket) -> the one
    settings shape the rest of this module trusts: cap is a real int in range or None,
    paused is a de-duplicated, sorted list of strings, and anything else — a list where
    a mapping was expected, a float cap, a bool (bool is an int in Python) — is the safe
    default rather than a crash or a silently-wrong value."""
    if not isinstance(data, dict):
        return {"cap": None, "paused": []}
    cap = data.get("cap")
    if isinstance(cap, bool) or not isinstance(cap, int) or not (CAP_MIN <= cap <= CAP_MAX):
        cap = None
    paused = data.get("paused")
    paused = sorted({p for p in paused if isinstance(p, str)}) if isinstance(paused, list) else []
    return {"cap": cap, "paused": paused}


def load_settings(root):
    """The local copy: {"cap": int|None, "paused": [gate ids]}. Missing, unreadable, or
    invalid (bad yaml, wrong shape) -> the safe default, never an exception."""
    try:
        data = yaml.safe_load((Path(root) / SETTINGS).read_text()) or {}
    except Exception:
        data = {}
    return _parse_settings(data)


def bucket_settings(cl, bucket, key):
    """The authoritative settings, read from the bucket itself, right now. Never the
    local copy — see the module docstring."""
    try:
        raw = cl.get_object(Bucket=bucket, Key=key)["Body"].read()
        data = yaml.safe_load(raw) or {}
    except Exception:
        data = {}
    return _parse_settings(data)


def gate_of(stem):
    """Gate id a pending sample stem belongs to. "external" for the curated imports
    listed in external.json (no capture timestamp to order by — never a camera gate).
    "unknown" for anything else that doesn't parse — never dropped silently, so it
    still shows up somewhere to fix by hand."""
    if stem.startswith("external-"):
        return "external"
    m = _STEM.match(stem)
    if not m:
        return "unknown"
    prefix = m.group(1)
    prefix = re.sub(r"-(cam[^-]*|north|south)$", "", prefix)
    return LEGACY_GATE if prefix in LEGACY else (prefix or LEGACY_GATE)


def _ts(stem):
    """The timestamp part alone — sorting by the whole stem mixes prefixes ("cam3-..."
    legacy vs "RDA-TG-KTB-cam3-..." current) that share a gate but not a string order,
    so it must never stand in for chronological order."""
    m = _STEM.match(stem)
    return m.group(2) if m else stem


def _keepset(keep):
    """`keep` is a set/collection snapshot, or a callable re-read live (see enforce:
    a slice claimed mid-listing must not be evicted, so the eviction loop re-checks)."""
    return keep() if callable(keep) else set(keep)


def evictions(stems, cap, keep=()):
    """{gate: [stems to evict]}, oldest first by capture time, keeping each gate's
    newest `cap`. Claimed stems (`keep`) are never evicted and don't count against the
    cap either way. "external" and "unknown" have no capture timestamp to rank by, so
    they are never evicted."""
    if cap is None:
        return {}
    keep = _keepset(keep)
    by_gate = {}
    for s in stems:
        if s in keep:
            continue
        by_gate.setdefault(gate_of(s), []).append(s)
    out = {}
    for gate, sids in by_gate.items():
        if gate in ("external", "unknown"):
            continue
        sids.sort(key=lambda s: (_ts(s), s))   # ties by name: every node must agree which goes
        if len(sids) > cap:
            out[gate] = sids[:len(sids) - cap]
    return out


def report(stems, cap, keep=()):
    """{gate: {"pending": n, "over": would-be eviction count}} for the UI and report-only
    mode. `cap` may be None — then "over" is 0 everywhere, just a queue-depth report."""
    keep = _keepset(keep)
    counts = {}
    for s in stems:
        if s in keep:
            continue
        counts[gate_of(s)] = counts.get(gate_of(s), 0) + 1
    return {gate: {"pending": n, "over": max(0, n - cap) if cap is not None else 0}
            for gate, n in counts.items()}


PARTS = ("images", "labels", "attrs", "suggest")
EXT = {"images": "jpg", "labels": "txt", "attrs": "json", "suggest": "json"}
STEMS_PER_BATCH = 250   # 4 keys/stem, delete_objects tops out at 1000 keys


def enforce(cl, bucket, root, prefix, keep=(), consumed=()):
    """List pending/, evict each gate down to its newest cap — the cap the BUCKET's
    curation.yaml says right now, not any value the caller might pass. cap unset there:
    report only, delete nothing. `keep` and `consumed` may be callables (see _keepset);
    `consumed` (dataset_sync.consumed(root)) are already-decided samples, excluded from
    both the count and the ranking — they're finished work, not queue.

    Deletes happen in batches of stems: each batch re-reads `keep` (a slice claimed
    since the listing started must survive), and a batch that deletes cleanly is
    unlinked locally and logged before the next batch runs — a mid-run failure leaves
    every earlier batch's bucket state, local state and log in agreement."""
    settings = bucket_settings(cl, bucket, prefix + SETTINGS)
    cap = settings["cap"]

    listing = {}
    for page in cl.get_paginator("list_objects_v2").paginate(
            Bucket=bucket, Prefix=prefix + "pending/"):
        for obj in page.get("Contents", []):
            listing[obj["Key"]] = obj["Size"]
    all_stems = {Path(k).stem for k in listing if k.startswith(prefix + "pending/images/")}
    consumed = _keepset(consumed)
    stems = all_stems - consumed
    keep_now = _keepset(keep)
    rep = report(stems, cap if cap is not None else DEFAULT_CAP, keep_now)
    result = {"report": rep, "evicted": {}, "settings": settings}
    if cap is None:
        return result
    to_evict = evictions(stems, cap, keep_now)

    log = Path(root) / "curation-evictions.jsonl"
    for gate, sids in to_evict.items():
        for i in range(0, len(sids), STEMS_PER_BATCH):
            chunk = [s for s in sids[i:i + STEMS_PER_BATCH] if s not in _keepset(keep)]
            if not chunk:
                continue
            keys = [prefix + f"pending/{part}/{sid}.{EXT[part]}" for sid in chunk for part in PARTS
                    if prefix + f"pending/{part}/{sid}.{EXT[part]}" in listing]
            resp = cl.delete_objects(Bucket=bucket, Delete={
                "Objects": [{"Key": k} for k in keys], "Quiet": True})
            errors = {e["Key"] for e in resp.get("Errors", [])}
            failed = {sid for sid in chunk for part in PARTS
                      if prefix + f"pending/{part}/{sid}.{EXT[part]}" in errors}
            succeeded = [s for s in chunk if s not in failed]
            for sid in succeeded:
                for part in PARTS:
                    (Path(root) / "pending" / part / f"{sid}.{EXT[part]}").unlink(missing_ok=True)
            if succeeded:
                result["evicted"][gate] = result["evicted"].get(gate, 0) + len(succeeded)
                with open(log, "a") as f:
                    f.write(json.dumps({
                        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "gate": gate, "evicted": len(succeeded),
                        "oldest": succeeded[0], "newest": succeeded[-1]}) + "\n")
            if errors:
                raise RuntimeError(f"delete_objects errors: {resp['Errors']}")
    return result


def selfcheck():
    assert gate_of("RDA-TG-KTB-cam3-20260926-071842") == "RDA-TG-KTB"
    assert gate_of("Ben-Bella-cam1-20260806-144054") == "Ben-Bella"
    assert gate_of("Kafue-Roundabout-cam2-20260101-000000") == "Kafue-Roundabout"
    assert gate_of("cam3-20260819-164645") == LEGACY_GATE
    assert gate_of("cam4-20260819-164645") == LEGACY_GATE
    assert gate_of("loop-videos-20260827-215000") == LEGACY_GATE
    assert gate_of("katuba-20260826-113644") == LEGACY_GATE
    assert gate_of("20260826-113644") == LEGACY_GATE
    assert gate_of("not-a-stem") == "unknown"
    assert gate_of("Gate-A-north-20260101-000000") == "Gate-A"
    assert gate_of("external-deadbeef1234") == "external"

    # Legacy Katuba names and current RDA-TG-KTB-cam* stems share a gate but not a
    # string prefix — sorting by the whole stem would mix them out of time order.
    # Ben-Bella-cam1 vs Ben-Bella-cam2 on the same day is the same trap on a smaller
    # scale. Both must rank strictly by the timestamp part.
    mixed = ["RDA-TG-KTB-cam3-20260103-000000", "cam4-20260101-000000",
             "loop-videos-20260102-000000", "katuba-20260104-000000"]
    ev = evictions(mixed, cap=2)
    assert ev == {"RDA-TG-KTB": ["cam4-20260101-000000", "loop-videos-20260102-000000"]}

    cams = ["Ben-Bella-cam2-20260101-100000", "Ben-Bella-cam1-20260101-090000",
            "Ben-Bella-cam1-20260101-110000"]
    assert evictions(cams, cap=2) == {"Ben-Bella": ["Ben-Bella-cam1-20260101-090000"]}

    stems = [f"A-{d}" for d in ("20260101-000000", "20260102-000000", "20260103-000000")]
    ev = evictions(stems, cap=2)
    assert ev == {"A": ["A-20260101-000000"]}
    assert evictions(stems, cap=None) == {}
    assert evictions(stems, cap=2, keep={"A-20260101-000000"}) == {}  # kept, and doesn't count
    assert evictions(stems, cap=2, keep=lambda: {"A-20260101-000000"}) == {}   # callable keep

    rep = report(stems, cap=2)
    assert rep == {"A": {"pending": 3, "over": 1}}

    # external imports and unparseable stems have no age to rank by — never evicted,
    # however far "over" any cap they'd otherwise look.
    ext = ["external-deadbeef1", "external-deadbeef2", "external-deadbeef3"]
    assert evictions(ext, cap=1) == {}
    assert evictions(["not-a-stem", "also-not-one"], cap=1) == {}

    # Settings parsing: bad shapes fall back to the safe default, never raise.
    assert _parse_settings({"cap": 5000, "paused": ["A", 3, "B"]}) == {"cap": 5000, "paused": ["A", "B"]}
    assert _parse_settings({"cap": True}) == {"cap": None, "paused": []}     # bool is not an int here
    assert _parse_settings({"cap": 50}) == {"cap": None, "paused": []}      # below CAP_MIN
    assert _parse_settings({"cap": 5000.0}) == {"cap": None, "paused": []}  # not a real int
    assert _parse_settings("not a mapping") == {"cap": None, "paused": []}
    assert _parse_settings(None) == {"cap": None, "paused": []}
    assert _parse_settings({"paused": "A"}) == {"cap": None, "paused": []}  # not a list

    class FakeS3:
        def __init__(self, objs, settings_key=None, settings_yaml=""):
            self.objs = dict(objs)
            self.deleted = []
            self.settings_key = settings_key
            self.settings_yaml = settings_yaml

        def get_paginator(self, name):
            objs = self.objs

            class P:
                def paginate(self, Bucket, Prefix):
                    yield {"Contents": [{"Key": k, "Size": v} for k, v in objs.items()
                                         if k.startswith(Prefix)]}
            return P()

        def get_object(self, Bucket, Key):
            if Key != self.settings_key:
                raise KeyError(Key)
            import io
            return {"Body": io.BytesIO(self.settings_yaml.encode())}

        def delete_objects(self, Bucket, Delete):
            keys = [o["Key"] for o in Delete["Objects"]]
            self.deleted.extend(keys)
            for k in keys:
                self.objs.pop(k, None)
            return {}

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for d in PARTS:
            (root / "pending" / d).mkdir(parents=True)
        # CAP_MIN is 100, so the delete pipeline needs >100 samples to see an eviction:
        # 103 one-day-apart stems, oldest three are the ones over a cap of 100.
        from datetime import timedelta
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        days = [(base + timedelta(days=i)).strftime("%Y%m%d-%H%M%S") for i in range(103)]
        objs = {}
        for day in days:
            sid = f"A-{day}"
            for part in PARTS:
                key = f"curation/pending/{part}/{sid}.{EXT[part]}"
                objs[key] = 1
                (root / "pending" / part / f"{sid}.{EXT[part]}").write_text("x")
        oldest3 = sorted(days)[:3]

        settings_key = "curation/" + SETTINGS

        # No settings object in the bucket at all -> cap None -> report only.
        s3 = FakeS3(objs, settings_key=settings_key, settings_yaml="")
        out = enforce(s3, "bucket", root, "curation/")
        assert out["evicted"] == {} and s3.deleted == []
        assert out["report"]["A"]["pending"] == 103
        assert out["settings"] == {"cap": None, "paused": []}

        # Bucket says cap 100 — authoritative, whatever a local file might say.
        (root / SETTINGS).write_text(yaml.safe_dump({"cap": 999999}))   # local: bogus/out of range
        s3 = FakeS3(objs, settings_key=settings_key, settings_yaml=yaml.safe_dump({"cap": 100}))
        out = enforce(s3, "bucket", root, "curation/")
        assert out["settings"]["cap"] == 100
        assert out["evicted"] == {"A": 3}
        assert len(s3.deleted) == 12   # three stems, 4 parts
        for day in oldest3:
            assert not (root / "pending" / "images" / f"A-{day}.jpg").exists()
        survivor = sorted(days)[3]
        assert (root / "pending" / "images" / f"A-{survivor}.jpg").exists()
        lines = (root / "curation-evictions.jsonl").read_text().splitlines()
        assert len(lines) == 1 and json.loads(lines[0])["evicted"] == 3

        # A claim minted after the listing started survives even though it's the
        # oldest of what's left — the per-batch re-check catches it, not just the
        # snapshot taken before the listing.
        objs2 = {k: v for k, v in objs.items() if not any(d in k for d in oldest3)}
        s3b = FakeS3(objs2, settings_key=settings_key, settings_yaml=yaml.safe_dump({"cap": 100}))
        held = {f"A-{survivor}"}
        out = enforce(s3b, "bucket", root, "curation/", keep=lambda: held)
        assert out["evicted"] == {}
        assert (root / "pending" / "images" / f"A-{survivor}.jpg").exists()

        # consumed (already approved/discarded per the audit ledger) drops out of both
        # the count and the ranking entirely.
        s3c = FakeS3(objs2, settings_key=settings_key, settings_yaml=yaml.safe_dump({"cap": 100000}))
        out = enforce(s3c, "bucket", root, "curation/", consumed={f"A-{d}" for d in days})
        assert out["report"] == {}

    print("curation_cap: ok")


if __name__ == "__main__":
    selfcheck()
