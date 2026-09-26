#!/usr/bin/env python3
"""Per-gate pause and hard cap on the curation queue's pending samples.

213k pending samples piled up (206k from Katuba) because nothing ever thinned the
queue. The owner wants each gate held to its newest N pending samples, oldest
evicted first, and a switch to stop a gate sending more while it's paused. The
first real enforcement deletes ~200k samples irreversibly, so the cap starts
unset (report-only: log what WOULD be evicted) until the owner turns it on via
curation.yaml. Importable from the office Mac too (which reads the same
settings file from the bucket) — no app.py imports here.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

SETTINGS = "curation.yaml"
DEFAULT_CAP = 5000   # the owner's target; used only to size the report while cap is unset
# Pre-gate-id sample names, all from the Katuba box before it got a gate id.
LEGACY = {"", "cam3", "cam4", "loop-videos", "katuba", "site1"}
LEGACY_GATE = "RDA-TG-KTB"
_STEM = re.compile(r"^(.*?)-?(\d{8}-\d{6})$")


def load_settings(root):
    """{"cap": int|None, "paused": [gate ids]} — missing/invalid file is the safe default:
    no cap, nothing paused."""
    try:
        data = yaml.safe_load((Path(root) / SETTINGS).read_text()) or {}
    except OSError:
        data = {}
    cap = data.get("cap")
    cap = cap if isinstance(cap, int) else None
    paused = data.get("paused") or []
    return {"cap": cap, "paused": [p for p in paused if isinstance(p, str)]}


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


def evictions(stems, cap, keep=()):
    """{gate: [stems to evict]}, oldest first, keeping each gate's newest `cap`. Claimed
    stems (`keep`) are never evicted and don't count against the cap either way."""
    if cap is None:
        return {}
    keep = set(keep)
    by_gate = {}
    for s in stems:
        if s in keep:
            continue
        by_gate.setdefault(gate_of(s), []).append(s)
    out = {}
    for gate, sids in by_gate.items():
        if gate in ("external", "unknown"):
            continue   # no capture timestamp to order by — nothing to evict, ever
        sids.sort()   # timestamp suffix sorts lexically -> chronological
        if len(sids) > cap:
            out[gate] = sids[:len(sids) - cap]
    return out


def report(stems, cap, keep=()):
    """{gate: {"pending": n, "over": would-be eviction count}} for the UI and report-only
    mode. `cap` may be None — then "over" is 0 everywhere, just a queue-depth report."""
    keep = set(keep)
    counts = {}
    for s in stems:
        if s in keep:
            continue
        counts[gate_of(s)] = counts.get(gate_of(s), 0) + 1
    return {gate: {"pending": n, "over": max(0, n - cap) if cap is not None else 0}
            for gate, n in counts.items()}


PARTS = ("images", "labels", "attrs", "suggest")
EXT = {"images": "jpg", "labels": "txt", "attrs": "json", "suggest": "json"}


def enforce(cl, bucket, root, prefix, cap, keep=()):
    """List pending/, evict each gate down to its newest `cap` (cap None: report only,
    delete nothing). Returns the report plus per-gate eviction counts."""
    listing = {}
    for page in cl.get_paginator("list_objects_v2").paginate(
            Bucket=bucket, Prefix=prefix + "pending/"):
        for obj in page.get("Contents", []):
            listing[obj["Key"]] = obj["Size"]
    stems = {Path(k).stem for k in listing if k.startswith(prefix + "pending/images/")}
    rep = report(stems, cap if cap is not None else DEFAULT_CAP, keep)
    to_evict = evictions(stems, cap, keep)
    if cap is None:
        return {"report": rep, "evicted": {}}
    evicted = {}
    log = Path(root) / "curation-evictions.jsonl"
    for gate, sids in to_evict.items():
        keys = [prefix + f"pending/{part}/{sid}.{EXT[part]}" for sid in sids for part in PARTS
                if prefix + f"pending/{part}/{sid}.{EXT[part]}" in listing]
        for i in range(0, len(keys), 1000):
            batch = keys[i:i + 1000]
            resp = cl.delete_objects(Bucket=bucket, Delete={
                "Objects": [{"Key": k} for k in batch], "Quiet": True})
            if resp.get("Errors"):
                raise RuntimeError(f"delete_objects errors: {resp['Errors']}")
        for sid in sids:
            for part in PARTS:
                (Path(root) / "pending" / part / f"{sid}.{EXT[part]}").unlink(missing_ok=True)
        evicted[gate] = len(sids)
        with open(log, "a") as f:
            f.write(json.dumps({
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "gate": gate, "evicted": len(sids),
                "oldest": sids[0], "newest": sids[-1]}) + "\n")
    return {"report": rep, "evicted": evicted}


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

    stems = [f"A-{d}" for d in ("20260101-000000", "20260102-000000", "20260103-000000")]
    ev = evictions(stems, cap=2)
    assert ev == {"A": ["A-20260101-000000"]}
    assert evictions(stems, cap=None) == {}
    assert evictions(stems, cap=2, keep={"A-20260101-000000"}) == {}  # kept, and doesn't count

    rep = report(stems, cap=2)
    assert rep == {"A": {"pending": 3, "over": 1}}

    # external imports and unparseable stems have no age to rank by — never evicted,
    # however far "over" any cap they'd otherwise look.
    ext = ["external-deadbeef1", "external-deadbeef2", "external-deadbeef3"]
    assert evictions(ext, cap=1) == {}
    assert evictions(["not-a-stem", "also-not-one"], cap=1) == {}

    class FakeS3:
        def __init__(self, objs):
            self.objs = dict(objs)
            self.deleted = []

        def get_paginator(self, name):
            objs = self.objs

            class P:
                def paginate(self, Bucket, Prefix):
                    yield {"Contents": [{"Key": k, "Size": v} for k, v in objs.items()
                                         if k.startswith(Prefix)]}
            return P()

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
        objs = {}
        for i, day in enumerate(("20260101-000000", "20260102-000000", "20260103-000000")):
            sid = f"A-{day}"
            for part in PARTS:
                key = f"curation/pending/{part}/{sid}.{EXT[part]}"
                objs[key] = 1
                (root / "pending" / part / f"{sid}.{EXT[part]}").write_text("x")

        s3 = FakeS3(objs)
        out = enforce(s3, "bucket", root, "curation/", cap=None)
        assert out["evicted"] == {} and s3.deleted == []
        assert out["report"]["A"]["pending"] == 3 and out["report"]["A"]["over"] == 0

        out = enforce(s3, "bucket", root, "curation/", cap=2)
        assert out["evicted"] == {"A": 1}
        assert len(s3.deleted) == 4   # one stem, 4 parts
        assert not (root / "pending" / "images" / "A-20260101-000000.jpg").exists()
        assert (root / "pending" / "images" / "A-20260102-000000.jpg").exists()
        lines = (root / "curation-evictions.jsonl").read_text().splitlines()
        assert len(lines) == 1 and json.loads(lines[0])["evicted"] == 1

    print("curation_cap: ok")


if __name__ == "__main__":
    selfcheck()
