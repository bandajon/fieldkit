#!/usr/bin/env python3
"""Cross-camera journeys: one record per physical vehicle across a back-to-back camera pair.

  python journeys.py build CONFIG_YAML TRACKLETS.jsonl [...]   journeys as JSON lines
  python journeys.py                                           self-check

At Katuba cam3 (faces south) and cam4 (faces north) watch the same road back to back, so
every through vehicle passes both, leaving one view through a handoff zone and entering the
other's within a couple of seconds. Counted per camera, one vehicle is two rows, and one
that a camera misses (cam3 at night, cam4 on big trucks at its near edge) only counts if
the other's count line caught it. Here each camera's tracklets (detect.py) are first
stitched into chains, one per vehicle per camera; a chain leaving one zone is linked to a
chain entering the other's; each linked pair or lone chain is a journey — counted once,
missed only if both cameras missed it, with its class fused from both views and the
front and rear crops from whichever camera saw each.
"""
import hashlib
import json
import sys
import time
from bisect import bisect_left, bisect_right
from collections import Counter
from datetime import datetime, timezone

from detect import HEADINGS, OPPOSITE, bound_of, heading_of, iou, letter_of

STITCH_GAP = 3.0     # s: longest occlusion that still continues the same vehicle in one camera
STITCH_IOU = 0.2     # the next fragment starts roughly where the last one ended
NEAR = 2.0           # a far, fast box outruns IoU between fragments; a continuation lands within
                     # this many box-sizes of where the last one was heading
MOVE = 0.03          # normalised displacement that counts as motion (detect.DIRECTION_MIN)
SPAN = 0.5           # s: velocity is measured over at least this; the kept final box can be one
                     # frame after the sample before it, and a parked vehicle's jitter then reads as motion
ZONE_SHARE = 0.25    # a box is "in" a zone when this much of its area lies inside
LEAD = 2.0           # s: an arrival may show up this long before its departure starts...
LAG = 5.0            # ...or this long after it ends: at night cam3 locks on late (4.6 s measured)
MU = 0.5             # s: typical departure -> arrival gap across the blind strip (0.2-2.2 measured)
CLASS_PEN = 1.0      # s-equivalent cost of pairing two different classes
MIN_EDGE_HITS = 5    # ~1 s at 5 fps: a zone crossing alone must last this long; night glare blips don't


def _centre(b):
    return (b[0] + b[2]) / 2, (b[1] + b[3]) / 2


def _vel(p, q):
    """Centre velocity per second from path sample p to a later one q."""
    dt = q[0] - p[0]
    (x0, y0), (x1, y1) = _centre(p[1:]), _centre(q[1:])
    return (x1 - x0) / dt, (y1 - y0) / dt


def _leaving(path):
    """Velocity as a track ended, over at least SPAN; None if it is briefer than that."""
    p0 = next((p for p in reversed(path) if path[-1][0] - p[0] >= SPAN), None)
    return p0 and _vel(p0, path[-1])


def _opposed(a, b):
    """A leaves one way and B arrives the other: a car exiting at the left edge and another
    entering there is two vehicles, while the fragments of one arriving car move alike."""
    pb = b["path"]
    b1 = next((p for p in pb if p[0] - pb[0][0] >= SPAN), None)
    va = _leaving(a["path"])
    if not va or not b1:
        return False        # too brief to have a heading: not moving
    vb = _vel(pb[0], b1)
    fast = all(abs(complex(*v)) >= MOVE / 2 for v in (va, vb))
    return fast and va[0] * vb[0] + va[1] * vb[1] < 0


def _near(a, b):
    """B picks up where A was heading, for a far box too small to overlap its last one. Same
    class only: every false merge by position on footage was a big vehicle occluding a small
    one, always a class jump. Never behind A: that is the next vehicle in the queue."""
    if a["class"] != b["class"]:
        return False
    last, first = a["path"][-1][1:], b["path"][0][1:]
    vx, vy = _leaving(a["path"]) or (0.0, 0.0)
    (ax, ay), (bx, by) = _centre(last), _centre(first)
    gap = b["t0"] - a["t1"]
    size = max(last[2] - last[0], last[3] - last[1], first[2] - first[0], first[3] - first[1])
    return ((bx - ax) * vx + (by - ay) * vy >= 0
            and abs(complex(bx - ax - vx * gap, by - ay - vy * gap)) <= NEAR * size)


def _in(box, zone):
    x, y, w, h = zone
    iw = min(box[2], x + w) - max(box[0], x)
    ih = min(box[3], y + h) - max(box[1], y)
    area = (box[2] - box[0]) * (box[3] - box[1])
    return iw > 0 and ih > 0 and area > 0 and iw * ih >= ZONE_SHARE * area


def _top(votes, conf):
    """Majority class; a tie goes to the more confident one."""
    return max(votes, key=lambda c: (votes[c], conf.get(c, 0.0)))


def _area(t):
    return max((p[3] - p[1]) * (p[4] - p[2]) for p in t["path"])


def _chains(tracklets, cameras):
    """One chain per vehicle per camera: ghosts join what they continue, then fragments
    that pick up where another left off are stitched, cheapest first, one-to-one."""
    by = {t["id"]: t for t in tracklets if t.get("path")}    # a node without images writes no path
    up = {i: i for i in by}

    def find(i):
        while up[i] != i:
            up[i] = up[up[i]]
            i = up[i]
        return i

    for t in by.values():
        if t.get("ghost_of") in by:
            up[find(t["id"])] = find(t["ghost_of"])
    per = {}
    for t in by.values():
        per.setdefault(t["camera"], []).append(t)
    cands = []
    for ts in per.values():
        # A whole day is ~20k tracklets a camera: only those starting within STITCH_GAP of a's end.
        ts.sort(key=lambda t: t["t0"])
        for a in ts:
            lo = bisect_left(ts, a["t1"], key=lambda t: t["t0"])
            for b in ts[lo:bisect_right(ts, a["t1"] + STITCH_GAP, key=lambda t: t["t0"])]:
                if (b is not a and (iou(a["path"][-1][1:], b["path"][0][1:]) >= STITCH_IOU or _near(a, b))
                        and not _opposed(a, b)):
                    cands.append((b["t0"] - a["t1"] + CLASS_PEN * (a["class"] != b["class"]),
                                  a["id"], b["id"]))
    cands.sort()
    succ, pred = set(), set()
    for _, a, b in cands:
        if a not in succ and b not in pred:
            succ.add(a)
            pred.add(b)
            up[find(b)] = find(a)
    groups = {}
    for i, t in by.items():
        groups.setdefault(find(i), []).append(t)
    zones = {c["name"]: c["handoff"]["zone"] for c in cameras if c.get("handoff")}
    chains = []
    for ms in groups.values():
        ms.sort(key=lambda t: (t["t0"], t["id"]))
        path = sorted((p for t in ms for p in t["path"]), key=lambda p: p[0])
        votes, conf = Counter(), {}
        for t in ms:
            votes.update(t["votes"])
            for c, v in t["conf"].items():
                conf[c] = max(conf.get(c, 0.0), v)
        ch = {"camera": ms[0]["camera"], "members": ms, "votes": votes, "conf": conf,
              "class": _top(votes, conf), "dep": None, "arr": None}
        zone = zones.get(ch["camera"])
        zt = [p[0] for p in path if _in(p[1:], zone)] if zone else []
        if zt:
            b0, b1 = path[0][1:], path[-1][1:]
            in0, in1 = _in(b0, zone), _in(b1, zone)
            (x0, y0), (x1, y1) = _centre(b0), _centre(b1)
            moved = abs(x1 - x0) >= MOVE or abs(y1 - y0) >= MOVE
            # Parked in the zone (in at both ends, never moved) is neither: it crossed nothing.
            ch["z"] = zt[0], zt[-1]
            ch["dep"] = zt[-1] if in1 and (not in0 or moved) else None
            ch["arr"] = zt[0] if in0 and (not in1 or moved) else None
        chains.append(ch)
    return sorted(chains, key=lambda c: (c["camera"], c["members"][0]["t0"], c["members"][0]["id"]))


def _links(chains, cameras):
    """Departures from one camera's zone to arrivals in its partner's, cheapest first; a
    chain takes part in one link at most. Overlap, not order, gates a candidate: a long
    truck fills both zones at once and can arrive seconds before it has finished leaving."""
    pairs = {frozenset((c["name"], c["handoff"]["camera"])) for c in cameras if c.get("handoff")}
    arrivals = {}
    for j, r in enumerate(chains):
        if r["arr"] is not None:
            arrivals.setdefault(r["camera"], []).append(j)

    def z0(j):
        return chains[j]["z"][0]

    # Arrivals by zone entry; the longest zone stay bounds how far back an overlap can start.
    index = {cam: (sorted(js, key=z0), max(chains[j]["z"][1] - z0(j) for j in js))
             for cam, js in arrivals.items()}
    cands = []
    for i, d in enumerate(chains):
        if d["dep"] is None:
            continue
        for cam, (js, span) in index.items():
            if cam == d["camera"] or frozenset((d["camera"], cam)) not in pairs:
                continue
            lo = bisect_left(js, d["z"][0] - LEAD - span, key=z0)
            for j in js[lo:bisect_right(js, d["z"][1] + LAG, key=z0)]:
                r = chains[j]
                if r["z"][1] >= d["z"][0] - LEAD:
                    cands.append((abs(r["arr"] - d["dep"] - MU) + CLASS_PEN * (d["class"] != r["class"]),
                                  i, j))
    cands.sort()
    used, links = set(), []
    for _, i, j in cands:
        if i not in used and j not in used:
            used |= {i, j}
            links.append((chains[i], chains[j]))
    return links


def _doc(chains, link, cfg, tz, events):
    ms = sorted((t for c in chains for t in c["members"]), key=lambda t: (t["t0"], t["id"]))
    votes, conf = Counter(), {}
    for c in chains:
        votes.update(c["votes"])
        for k, v in c["conf"].items():
            conf[k] = max(conf.get(k, 0.0), v)
    cls = _top(votes, conf)
    dirs = Counter(t["direction"] for t in ms if t.get("direction")).most_common(1)
    direction = dirs[0][0] if dirs else None
    names = sorted({t["camera"] for t in ms})
    crops, plates = {}, {}
    tops = [t for t in ms if (t.get("crops") or {}).get("top")]
    if tops:
        crops["best"] = max(tops, key=lambda t: t["conf"].get(cls, 0.0))["crops"]["top"]
    if direction in OPPOSITE:
        # A camera sees the fronts of traffic coming at it and the rears of traffic it faces with.
        for side, way in (("front", OPPOSITE[direction]), ("rear", direction)):
            seen = [t for t in ms if HEADINGS.get(heading_of(cfg.get(t["camera"], {}))) == way]
            # The rear camera's largest box is the vehicle passing beside it, still side-on;
            # the true rear is the recede crop, taken once it has shrunk away after the peak.
            for tag in ("recede", "best") if side == "rear" else ("best",):
                shots = [t for t in seen if (t.get("crops") or {}).get(tag)]
                if shots:
                    crops[side] = max(shots, key=_area)["crops"][tag]
                    break
            plated = [t for t in seen if (t.get("plate") or {}).get("crop")]
            if plated:
                p = max(plated, key=lambda t: t["plate"]["conf"])["plate"]
                crops[side + "_plate"], plates[side] = p["crop"], p["conf"]
    evs = [e for e in ((events or {}).get(t["id"]) for t in ms) if e and e.get("class") == cls]
    t = link[0]["dep"] if link else ms[0]["t0"]
    return t, {
        "id": "jny-" + hashlib.sha256("\0".join(sorted(m["id"] for m in ms)).encode()).hexdigest()[:24],
        "ts": datetime.fromtimestamp(t, timezone.utc).astimezone(tz).isoformat(timespec="seconds"),
        "cameras": names, "camera": "+".join(names),
        "class": cls, "letter": letter_of(cls), "conf": conf.get(cls),
        "direction": direction,
        "bound": next((b for n in names if (b := bound_of(cfg.get(n, {}), direction))), None),
        "attrs": max(evs, key=lambda e: e.get("hits", 0)).get("attrs") or {} if evs else {},
        "crops": crops, "plates": plates,
        "members": [{k: m[k] for k in ("id", "camera", "t0", "t1", "counted")} for m in ms],
        "link": link and {"from": link[0]["camera"], "to": link[1]["camera"],
                          "gap_s": round(link[1]["arr"] - link[0]["dep"], 2)},
        "evidence": "handoff" if link else "line" if any(m["counted"] for m in ms) else "edge",
        "hits": sum(m["hits"] for m in ms),
        "dwell_s": round(max(m["t1"] for m in ms) - ms[0]["t0"], 1)}


def _build(tracklets, cameras, tz=None, events=None):
    chains = _chains(tracklets, cameras)
    links = _links(chains, cameras)
    linked = {id(c) for pair in links for c in pair}
    # A lone chain counts if its camera counted it, or it crossed a handoff zone the partner
    # missed and stayed in view long enough to be a vehicle rather than headlight glare.
    lone = [c for c in chains if id(c) not in linked and (
            any(t["counted"] for t in c["members"])
            or (c["dep"] is not None or c["arr"] is not None)
            and sum(t["hits"] for t in c["members"]) >= MIN_EDGE_HITS)]
    cfg = {c["name"]: c for c in cameras}
    docs = [_doc(list(pair), pair, cfg, tz, events) for pair in links]
    docs += [_doc([c], None, cfg, tz, events) for c in lone]
    return [d for _, d in sorted(docs, key=lambda td: (td[0], td[1]["id"]))], chains, links


def build(tracklets, cameras, tz=None, events=None):
    """Tracklets (detect.py) of paired cameras -> counted journeys, sorted by ts then id."""
    return _build(tracklets, cameras, tz, events)[0]


_CAMS = [{"name": "cam3", "heading": "south", "handoff": {"camera": "cam4", "zone": [0.80, 0.62, 0.20, 0.38]}},
        {"name": "cam4", "heading": "north", "handoff": {"camera": "cam3", "zone": [0.0, 0.55, 0.22, 0.30]}}]


def _b(cx, cy, s=0.1):
    return [cx - s, cy - s, cx + s, cy + s]


def _t(cam, i, cls, samples, **kw):
    """A synthetic tracklet from (t, box) samples; every sample a vote for `cls`."""
    path = [[t, *b] for t, b in samples]
    return {"id": f"obs-{cam}-{i}", "camera": cam, "t0": path[0][0], "t1": path[-1][0],
            "hits": len(path), "dim": [1920, 1080], "class": cls, "votes": {cls: len(path)},
            "conf": {cls: 0.8}, "counted": False, "ghost_of": None, "direction": None, "path": path,
            "crops": {"top": f"{cam}-{i}-top.jpg", "best": f"{cam}-{i}-best.jpg"}, "plate": None} | kw


def _north(i, t, gap=0.4, cls="c-small", k3={}, k4={}):
    """A northbound pass: cam3 sees it approach and leave bottom-right at t, cam4 sees it
    arrive bottom-left `gap` later and recede."""
    return [_t("cam3", i, cls, [(t - 2, _b(0.5, 0.4)), (t - 1, _b(0.7, 0.6)), (t, _b(0.9, 0.8))],
               direction="northbound", **k3),
            _t("cam4", i, cls, [(t + gap, _b(0.1, 0.7)), (t + gap + 1, _b(0.3, 0.5)),
                                (t + gap + 2, _b(0.45, 0.35))], direction="northbound", **k4)]


def _selfcheck():
    # (a) one northbound vehicle across the handoff: one journey, front from cam3, rear from cam4.
    a = _north(1, 100.0, k3={"plate": {"conf": 0.31, "crop": "cam3-1-plate.jpg"}})
    [j] = build(a, _CAMS, tz=timezone.utc)
    assert j["link"] == {"from": "cam3", "to": "cam4", "gap_s": 0.4} and j["evidence"] == "handoff", j
    assert j["crops"]["front"] == "cam3-1-best.jpg" and j["crops"]["rear"] == "cam4-1-best.jpg", j
    assert j["crops"]["front_plate"] == "cam3-1-plate.jpg" and j["plates"] == {"front": 0.31}, j
    assert "rear_plate" not in j["crops"] and j["camera"] == "cam3+cam4" and j["hits"] == 6, j
    assert j["ts"] == "1970-01-01T00:01:40+00:00" and j["bound"] == "toward_gate", j
    # ...and once cam4 saw it recede, the rear is that crop, not the side-on largest box.
    shot = {"top": "cam4-1-top.jpg", "best": "cam4-1-best.jpg", "recede": "cam4-1-recede.jpg"}
    [j] = build(_north(1, 100.0, k4={"crops": shot}), _CAMS)
    assert j["crops"]["rear"] == "cam4-1-recede.jpg" and j["crops"]["front"] == "cam3-1-best.jpg", j
    # (b) a queue 2-4 s apart pairs in order, not crossed.
    q = _north(1, 100.0) + _north(2, 103.0) + _north(3, 105.0)
    js = build(q, _CAMS)
    assert [sorted(m["id"][-1] for m in j["members"]) for j in js] == [["1", "1"], ["2", "2"], ["3", "3"]], js
    # (c) a long truck southbound: its cam3 arrival starts 8 s before its cam4 departure ends.
    t4 = _t("cam4", 1, "e-heavy", [(100, _b(0.5, 0.4))] + [(t, _b(0.1, 0.7)) for t in range(101, 113)])
    t3 = _t("cam3", 1, "e-heavy", [(104, _b(0.9, 0.8)), (105, _b(0.9, 0.8)), (106, _b(0.88, 0.78)),
                                   (110, _b(0.5, 0.4))])
    [j] = build([t4, t3], _CAMS)
    assert j["link"] == {"from": "cam4", "to": "cam3", "gap_s": -8.0}, j
    # (d) a car leaves cam4 at the left edge, another enters there 1.5 s later: opposed, not stitched.
    out = _t("cam4", 1, "c-small", [(100, _b(0.4, 0.4)), (101, _b(0.25, 0.55)), (102, _b(0.1, 0.7))], hits=15)
    inn = _t("cam4", 2, "c-small", [(103.5, _b(0.1, 0.7)), (104.5, _b(0.25, 0.55)), (105.5, _b(0.4, 0.4))],
             hits=15)
    assert len(build([out, inn], _CAMS)) == 2
    # ...but a parked car's final box 0.2 s after the one before is jitter, not a heading.
    parked = _t("cam4", 3, "c-small", [(100, _b(0.1, 0.7)), (101, _b(0.1, 0.7)), (101.2, _b(0.105, 0.7))])
    assert _opposed(out, inn) and not _opposed(parked, _t("cam4", 4, "c-small", [
        (101.5, _b(0.1, 0.7)), (102.5, _b(0.05, 0.7))]))
    # (e) two fragments of one arriving car 0.4 s apart are one chain.
    f1 = _t("cam4", 1, "c-small", [(100, _b(0.1, 0.7)), (101, _b(0.2, 0.6))], hits=5)
    f2 = _t("cam4", 2, "c-small", [(101.4, _b(0.22, 0.58)), (102.4, _b(0.35, 0.45))], hits=5)
    [j] = build([f1, f2], _CAMS)
    assert len(j["members"]) == 2 and j["evidence"] == "edge", j
    # (f) parked in the zone, jittering, never counted: nothing.
    park = _t("cam3", 1, "c-small", [(100 + k, _b(0.9 + k % 2 * 0.005, 0.8)) for k in range(6)])
    assert build([park], _CAMS) == []
    # (g) one camera, counted on its line, never near a zone: a journey on the line's word.
    [j] = build([_t("cam3", 1, "c-small", [(100, _b(0.5, 0.2)), (101, _b(0.5, 0.4))], counted=True)], _CAMS)
    assert j["evidence"] == "line" and j["link"] is None and j["cameras"] == ["cam3"], j
    # (h) class fusion: cam4's 20 e-heavy votes outweigh cam3's 3 d-medium; best crop is cam4's.
    h = _north(1, 100.0, k3={"votes": {"d-medium": 3}, "conf": {"d-medium": 0.7}, "class": "d-medium"},
               k4={"votes": {"e-heavy": 20}, "conf": {"e-heavy": 0.9}, "class": "e-heavy"})
    ev = {"obs-cam3-1": {"class": "d-medium", "hits": 3, "attrs": {"axles": 2}},
          "obs-cam4-1": {"class": "e-heavy", "hits": 20, "attrs": {"axles": 5}}}
    [j] = build(h, _CAMS, events=ev)
    assert (j["class"], j["letter"], j["conf"]) == ("e-heavy", "E", 0.9) and j["link"], j
    assert j["crops"]["best"] == "cam4-1-top.jpg" and j["attrs"] == {"axles": 5}, j
    # (i) ghost_of joins a tracklet to the one it continues, however long the gap.
    g1 = _t("cam3", 1, "c-small", [(100, _b(0.5, 0.2)), (102, _b(0.5, 0.3))], counted=True)
    g2 = _t("cam3", 2, "c-small", [(120, _b(0.5, 0.3)), (125, _b(0.5, 0.31))], ghost_of="obs-cam3-1")
    [j] = build([g1, g2], _CAMS)
    assert [m["id"] for m in j["members"]] == ["obs-cam3-1", "obs-cam3-2"] and j["dwell_s"] == 25.0, j
    # (k) an edge crossing alone needs MIN_EDGE_HITS: a 2-frame glare blip is not a vehicle.
    blip = [(100, _b(0.1, 0.7)), (101, _b(0.3, 0.5))]
    assert build([_t("cam4", 1, "c-small", blip, hits=2)], _CAMS) == []
    assert len(build([_t("cam4", 1, "c-small", blip, hits=6)], _CAMS)) == 1
    assert build([_t("cam3", 1, "c-small", [(100, _b(0.5, 0.2))], path=[], counted=True)], _CAMS) == []
    # (l) a far, fast car: 0.05 boxes 0.08 apart 0.4 s later overlap nothing, yet are one chain;
    # (m) not across a class jump; (n) not when B starts behind where A left off.
    fast = _t("cam3", 1, "a-small", [(100, _b(0.18, 0.64, 0.025)), (101, _b(0.26, 0.64, 0.025))])
    on = [(101.4, _b(0.34, 0.64, 0.025)), (102.4, _b(0.42, 0.64, 0.025))]
    assert len(_chains([fast, _t("cam3", 2, "a-small", on)], _CAMS)) == 1
    assert len(_chains([fast, _t("cam3", 2, "d-medium", on)], _CAMS)) == 2
    back = [(101.4, _b(0.20, 0.64, 0.025)), (102.4, _b(0.28, 0.64, 0.025))]
    assert len(_chains([fast, _t("cam3", 2, "a-small", back)], _CAMS)) == 2
    # (j) a rebuild, in any input order, yields the same ids.
    assert [j["id"] for j in build(q, _CAMS)] == [j["id"] for j in build(q[::-1], _CAMS)]
    # A whole day at a gate: 10k passes, 20k tracklets, in windowed time rather than all pairs.
    start = time.perf_counter()
    assert len(build([t for i in range(10_000) for t in _north(i, 100.0 + 5 * i)], _CAMS)) == 10_000
    took = time.perf_counter() - start
    print("journeys self-check ok: handoff links front to rear, queues pair in order, long trucks "
          "link across the overlap, opposed fragments stay apart, parked vehicles never count, "
          "classes fuse by votes, ghosts join, glare blips drop, far fast fragments join, ids are stable; "
          f"10k passes (20k tracklets) in {took:.1f} s")


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        _selfcheck()
    elif a[0] == "build" and len(a) >= 3:
        from pathlib import Path
        from zoneinfo import ZoneInfo

        import yaml
        cams = yaml.safe_load(Path(a[1]).read_text())["cameras"]
        ts = [json.loads(line) for f in a[2:] for line in Path(f).read_text().splitlines() if line.strip()]
        js, chains, links = _build(ts, cams, ZoneInfo("Africa/Lusaka"))
        for j in js:
            print(json.dumps(j))
        per = dict(sorted(Counter(c["camera"] for c in chains).items()))
        print(f"chains {per}, links {len(links)}, journeys {len(js)}: {len(links)} linked, "
              f"{len(js) - len(links)} single-camera", file=sys.stderr)
    else:
        sys.exit(__doc__)
