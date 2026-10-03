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
import base64
import hashlib
import json
import struct
import sys
import time
from bisect import bisect_left, bisect_right
from collections import Counter
from datetime import datetime, timezone

from detect import HEADINGS, OPPOSITE, bound_of, heading_of, iou, letter_of

STITCH_GAP = 3.0     # s: longest occlusion that still continues the same vehicle in one camera
STITCH_IOU = 0.2     # the next fragment starts roughly where the last one ended
TWIN_IOU = 0.5       # detect.GUARD_IOU: one vehicle, two ids, the same box at the same instant
TWIN_COVER = 0.8     # a box this much inside another is the same vehicle seen in part (cab + whole truck)...
TWIN_COS = 0.5       # ...when the class agrees and the appearance codes cosine is at least this
TWIN_SNAP = 1.0      # s: a path sample stands for a moment this close to it (paths are ~1 sample/s)
NEAR = 2.0           # a far, fast box outruns IoU between fragments; a continuation lands within
                     # this many box-sizes of where the last one was heading
MOVE = 0.03          # normalised displacement that counts as motion (detect.DIRECTION_MIN)
SPAN = 0.5           # s: velocity is measured over at least this; the kept final box can be one
                     # frame after the sample before it, and a parked vehicle's jitter then reads as motion
ZONE_SHARE = 0.25    # a box is "in" a zone when this much of its area lies inside
LEAD = 2.0           # s: an arrival may show up this long before its departure starts...
LAG = 5.0            # ...or this long after it ends: at night cam3 locks on late (4.6 s measured)
LAG_LATE = 12.0      # s: a late-locked chain (first seen past the zone) may start this long after the departure ends
LATE_COS = 0.45      # a late-locked chain links only on a known appearance cosine at least this
MU = 0.5             # s: typical departure -> arrival gap across the blind strip (0.2-2.2 measured)
CLASS_PEN = 1.0      # s-equivalent cost of pairing two different classes
APP_PEN = 2.5        # s-equivalent cost per unit of (1 - cosine) between appearance codes, so 0..5 s.
                     # Measured at Katuba 2026-10-01 10:30-11:30: at 2.5 the crossed near-simultaneous
                     # pickups pair correctly and ~80% of changed links are visually right (~14% before)
APP_TYPICAL = 0.7    # cosine of a typical true match (Katuba median ~0.79): an uncoded candidate pays
                     # the penalty of a typical match, so it never beats a coded one at equal timing
APP_VETO = 0.2       # known cosine below this is never a link: true pairs' 1st percentile is far above, and
                     # every linked pair under it audited on a real Katuba hour was two different vehicles
HEAVY = ("d-medium", "e-heavy", "f-abnormal")
TRUCK_LEAD = 8.0     # s: a slow long truck's arrival may precede its departure...
TRUCK_LAG = 15.0     # ...or follow it this long
TRUCK_COS = 0.7      # look agreement a wider window demands
MIN_EDGE_HITS = 5    # ~1 s at 5 fps: a zone crossing alone must last this long; night glare blips don't


def _centre(b):
    return (b[0] + b[2]) / 2, (b[1] + b[3]) / 2


def _app(ms):
    """(unit vector, model version) of the member with the most hits that has a code, else None.
    A mixed member's code may be the other vehicle's: it never speaks for the chain."""
    t = max((t for t in ms if t.get("app") and not t.get("mixed")), key=lambda t: t["hits"], default=None)
    try:
        return t and (struct.unpack("<32e", base64.b64decode(t["app"])), t.get("app_v"))
    except (TypeError, ValueError, struct.error):  # ValueError covers binascii.Error
        return None


def _similar(a, b):
    """Cosine of two appearance codes; None unless both exist and come from the same model."""
    if a is None or b is None or a[1] != b[1]:
        return None
    return sum(x * y for x, y in zip(a[0], b[0]))


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


def _at(path, t):
    """The path's box nearest time t, or None when no sample lies within TWIN_SNAP of it."""
    i = bisect_left(path, t, key=lambda p: p[0])
    p = min(path[max(i - 1, 0):i + 1], key=lambda p: abs(p[0] - t))
    return p[1:] if abs(p[0] - t) <= TWIN_SNAP else None


def _twin(a, b):
    """B, starting while A is alive, is A under a second id: the boxes coincide where B starts
    and still where their overlap ends. Two cars in adjacent lanes touch briefly, then diverge."""
    end = min(a["t1"], b["t1"])
    alike = []  # lazy: the appearance test only runs when IoU alone fails

    def cover(p, q):
        w = min(p[2], q[2]) - max(p[0], q[0])
        h = min(p[3], q[3]) - max(p[1], q[1])
        s = min((p[2] - p[0]) * (p[3] - p[1]), (q[2] - q[0]) * (q[3] - q[1]))
        return w > 0 and h > 0 and s > 0 and w * h / s >= TWIN_COVER

    def same(p, q, born):
        if iou(p, q) >= TWIN_IOU:
            return True
        if born or not cover(p, q):
            return False
        if not alike:
            alike.append(a["class"] == b["class"] and (_similar(_app([a]), _app([b])) or 0) >= TWIN_COS)
        return alike[0]

    return all(p and q and same(p, q, born) for born, p, q in (
        (True, _at(a["path"], b["t0"]), b["path"][0][1:]), (False, _at(a["path"], end), _at(b["path"], end))))


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
    """One chain per vehicle per camera: ghosts join what they continue and twins (two ids
    on one vehicle at once) join each other, then fragments that pick up where another left
    off are stitched, cheapest first, one-to-one."""
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
    for ts in per.values():
        ts.sort(key=lambda t: t["t0"])
        for a in ts:
            # Forced like a ghost, any class (twins flicker), and no stitch slot used: detect's
            # recount guard only tags a twin once it crosses the count line.
            for b in ts[bisect_left(ts, a["t0"], key=lambda t: t["t0"]):
                        bisect_left(ts, a["t1"], key=lambda t: t["t0"])]:
                if b is not a and _twin(a, b):
                    up[find(b["id"])] = find(a["id"])
    end = {}
    for t in by.values():
        r = find(t["id"])
        end[r] = max(end.get(r, t["t1"]), t["t1"])
    cands = []
    for ts in per.values():
        # A whole day is ~20k tracklets a camera: only those starting within STITCH_GAP of a's end.
        for a in ts:
            if end[find(a["id"])] > a["t1"]:
                continue    # its vehicle lived on under another id: a twin dying mid-frame left nowhere
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
              "class": _top(votes, conf), "dep": None, "arr": None, "app": _app(ms)}
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
        elif zone and len(path) > 1:
            # Never in the zone yet moving away from it: cam3 at night locks on only mid-frame.
            zc = (zone[0] + zone[2] / 2, zone[1] + zone[3] / 2)
            (x0, y0), (x1, y1) = _centre(path[0][1:]), _centre(path[-1][1:])
            if abs(complex(x1 - zc[0], y1 - zc[1])) - abs(complex(x0 - zc[0], y0 - zc[1])) >= MOVE:
                ch["late"] = path[0][0]
        chains.append(ch)
    return sorted(chains, key=lambda c: (c["camera"], c["members"][0]["t0"], c["members"][0]["id"]))


def _links(chains, cameras):
    """Departures from one camera's zone to arrivals in its partner's, cheapest first; a
    chain takes part in one link at most. Overlap, not order, gates a candidate: a long
    truck fills both zones at once and can arrive seconds before it has finished leaving."""
    pairs = {frozenset((c["name"], c["handoff"]["camera"])) for c in cameras if c.get("handoff")}

    def z0(j):
        return chains[j]["z"][0] if chains[j]["arr"] is not None else chains[j]["late"]

    def z1(j):
        return chains[j]["z"][1] if chains[j]["arr"] is not None else chains[j]["late"]

    def match(late, used):
        """Pass 1 (late False): ordinary arrivals. Pass 2: late chains for the departures left over."""
        arrivals = {}
        for j, r in enumerate(chains):
            if (r["arr"] is None and r.get("late") is not None
                    and sum(t["hits"] for t in r["members"]) >= MIN_EDGE_HITS) if late else r["arr"] is not None:
                arrivals.setdefault(r["camera"], []).append(j)
        # By zone entry; the longest zone stay bounds how far back an overlap can start.
        index = {cam: (sorted(js, key=z0), max(z1(j) - z0(j) for j in js))
                 for cam, js in arrivals.items()}
        cands = []
        for i, d in enumerate(chains):
            if d["dep"] is None or id(d) in used:
                continue
            for cam, (js, span) in index.items():
                if cam == d["camera"] or frozenset((d["camera"], cam)) not in pairs:
                    continue
                lo = bisect_left(js, d["z"][0] - LEAD - span, key=z0)
                for j in js[lo:bisect_right(js, d["z"][1] + (LAG_LATE if late else LAG), key=z0)]:
                    r = chains[j]
                    if z1(j) >= d["z"][0] - LEAD:
                        sim = _similar(d["app"], r["app"])
                        if sim is not None and sim < APP_VETO:
                            continue
                        if late and (sim is None or sim < LATE_COS):
                            continue
                        t = abs(z0(j) - d["dep"] - MU) + CLASS_PEN * (d["class"] != r["class"])
                        cands.append((t, t + APP_PEN * (1 - (APP_TYPICAL if sim is None else sim)), i, j))

        def greedy(cs, k):
            taken, out = set(), []
            for *_, i, j in sorted(cs, key=lambda c: (c[k], c[2], c[3])):
                if i not in taken and j not in taken:
                    taken |= {i, j}
                    out.append((chains[i], chains[j]))
            return out

        if late:
            return greedy(cands, 1)
        # Appearance only re-pairs within a connected set of candidates, and only if that costs no
        # link: a re-pairing that strands a chain would turn one vehicle into two journeys.
        root = list(range(len(chains)))

        def find(x):
            while root[x] != x:
                root[x] = root[root[x]]
                x = root[x]
            return x

        for *_, i, j in cands:
            root[find(i)] = find(j)
        comps = {}
        for c in cands:
            comps.setdefault(find(c[2]), []).append(c)
        links = []
        for cs in comps.values():
            by_time = greedy(cs, 0)
            by_look = greedy(cs, 1)
            links += by_look if len(by_look) == len(by_time) else by_time
        return links

    links = match(False, set())
    # Late chains never compete with ordinary arrivals: only departures still unlinked may take one.
    links += match(True, {id(c) for link in links for c in link})
    return links + _trucks(chains, links, pairs)


def _trucks(chains, links, pairs):
    """Slow trucks miss the ordinary window: a wider one, only for trucks that look alike, only for
    chains still unlinked, and never crossing an accepted truck link (convoy order). Within a set of
    competing candidates the most links in order win, then the cheapest: a greedy would cross a convoy."""
    used = {id(c) for link in links for c in link}
    heavy = lambda c: c["class"] in HEAVY
    ta = lambda r: r["arr"] if r["arr"] is not None else r["late"]
    solid = lambda c: sum(t["hits"] for t in c["members"]) >= MIN_EDGE_HITS
    deps = [d for d in chains if d["dep"] is not None and id(d) not in used and heavy(d) and solid(d)]
    arrs = sorted((r for r in chains if id(r) not in used and heavy(r) and (
        r["arr"] is not None or (r.get("late") is not None and solid(r)))), key=ta)
    times = [ta(r) for r in arrs]
    accepted = {}  # (dep camera, arr camera) -> [(dep, arr)] of accepted truck links
    for d, r in links:
        if heavy(d) and heavy(r):
            accepted.setdefault((d["camera"], r["camera"]), []).append((d["dep"], ta(r)))
    cost, root, cands = {}, {}, []

    def find(x):
        while root.setdefault(x, x) != x:
            root[x] = root[root[x]]
            x = root[x]
        return x

    for d in deps:
        for r in arrs[bisect_left(times, d["dep"] - TRUCK_LEAD):bisect_right(times, d["dep"] + TRUCK_LAG)]:
            if r["camera"] == d["camera"] or frozenset((d["camera"], r["camera"])) not in pairs:
                continue
            sim = _similar(d["app"], r["app"])
            if (sim is not None and sim >= TRUCK_COS and not any(
                    (dd - d["dep"]) * (aa - ta(r)) < 0 for dd, aa in accepted.get((d["camera"], r["camera"]), ()))):
                cost[id(d), id(r)] = abs(ta(r) - d["dep"] - MU) + APP_PEN * (1 - sim)
                root[find(id(d))] = find(id(r))
                cands.append((d, r))
    comps = {}  # (component, dep camera, arr camera) -> (deps, arrs) by id
    for d, r in cands:
        ds, rs = comps.setdefault((find(id(d)), d["camera"], r["camera"]), ({}, {}))
        ds[id(d)], rs[id(r)] = d, r
    comps = [(list(ds.values()), list(rs.values())) for ds, rs in comps.values()]
    out = []
    for ds, rs in comps:
        ds.sort(key=lambda d: d["dep"])
        rs.sort(key=ta)
        # Monotone alignment: best[i][j] = (links, -cost) over the first i departures and j arrivals.
        best = [[(0, 0.0)] * (len(rs) + 1) for _ in range(len(ds) + 1)]
        for i, d in enumerate(ds, 1):
            for j, r in enumerate(rs, 1):
                best[i][j] = max(best[i - 1][j], best[i][j - 1])
                c = cost.get((id(d), id(r)))
                if c is not None:
                    n, k = best[i - 1][j - 1]
                    best[i][j] = max(best[i][j], (n + 1, k - c))
        i, j = len(ds), len(rs)
        while i and j:
            if best[i][j] == best[i - 1][j]:
                i -= 1
            elif best[i][j] == best[i][j - 1]:
                j -= 1
            else:
                out.append((ds[i - 1], rs[j - 1]))
                i, j = i - 1, j - 1
    return out


def _member_attrs(ms, cls):
    """Attrs of the member with the most hits, preferring ones of the journey's class."""
    have = [t for t in ms if t.get("attrs")]
    same = [t for t in have if t.get("class") == cls]
    return max(same or have, key=lambda t: t["hits"], default={}).get("attrs") or {}


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
    pics = [t for t in ms if not t.get("mixed")]    # a switched track's crops show two vehicles
    pics = pics if any((t.get("crops") or {}) for t in pics) else ms
    tops = [t for t in pics if (t.get("crops") or {}).get("top")]
    if tops:
        crops["best"] = max(tops, key=lambda t: t["conf"].get(cls, 0.0))["crops"]["top"]
    if direction in OPPOSITE:
        # A camera sees the fronts of traffic coming at it and the rears of traffic it faces with.
        for side, way in (("front", OPPOSITE[direction]), ("rear", direction)):
            seen = [t for t in pics if HEADINGS.get(heading_of(cfg.get(t["camera"], {}))) == way]
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
        "attrs": (max(evs, key=lambda e: e.get("hits", 0)).get("attrs") if evs else None) or _member_attrs(ms, cls),
        "crops": crops, "plates": plates,
        "members": [{k: m[k] for k in ("id", "camera", "t0", "t1", "counted")} for m in ms],
        "link": link and {"from": link[0]["camera"], "to": link[1]["camera"],
                          "gap_s": round((link[1]["arr"] if link[1]["arr"] is not None else link[1]["late"]) - link[0]["dep"], 2)},
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
    # (b2) two same-class vehicles 1 s apart: timing alone crosses them (B leaves 1 s after A and
    # A's arrival lands 0.5 s after B's departure); the look pairs A with its own arrival.
    code = lambda *v: base64.b64encode(struct.pack("<32e", *v, *[0.0] * (32 - len(v)))).decode()
    ids = lambda js: sorted(sorted(m["id"] for m in j["members"]) for j in js)
    right = [["obs-cam3-1", "obs-cam4-1"], ["obs-cam3-2", "obs-cam4-2"]]
    crossed = [["obs-cam3-1", "obs-cam4-2"], ["obs-cam3-2", "obs-cam4-1"]]
    for v3, v4, want in (("m1", "m1", right),       # learned codes decide
                         (None, None, crossed),     # no codes: timing as before
                         ("m1", "m2", crossed)):    # different models: ignored
        def k(c, v):
            return {"app": code(*c), "app_v": v} if v else {}
        pair = _north(1, 100.0, gap=1.5, k3=k([1], v3), k4=k([1], v4)) \
            + _north(2, 101.0, gap=1.5, k3=k([0, 1], v3), k4=k([0, 1], v4))
        assert ids(build(pair, _CAMS)) == want, (v3, v4, ids(build(pair, _CAMS)))
    assert _app([{"hits": 1, "app": "!!", "app_v": "m1"}]) is None == _app([{"hits": 1, "app": "AAAA"}])
    # (b3) appearance may never strand a chain: d2 only reaches r2, and r1's rear view looks
    # nothing like d1, so looks alone would send d1 to r2 and leave d2 and r1 lone. Timing's two links stay.
    pair = _north(1, 100.0, gap=0.5, k3={"app": code(1), "app_v": "m1"}, k4={"app": code(.25, .968), "app_v": "m1"}) \
        + _north(2, 105.0, gap=-1.0, k3={"app": code(.25, .968), "app_v": "m1"}, k4={"app": code(1), "app_v": "m1"})
    assert ids(build(pair, _CAMS)) == [["obs-cam3-1", "obs-cam4-1"], ["obs-cam3-2", "obs-cam4-2"]]
    # (b4) codes that disagree (cosine < APP_VETO) never link, even when timing says they should.
    pair = _north(1, 100.0, k3={"app": code(1), "app_v": "m1", "counted": True},
                  k4={"app": code(-1), "app_v": "m1", "counted": True})
    assert len(build(pair, _CAMS)) == 2
    pair[0]["mixed"] = pair[1]["mixed"] = True        # ...unless a code may belong to a switched track
    assert len(build(pair, _CAMS)) == 1
    # (b5) a mixed member's crops lose to a clean member's.
    shot = lambda c: {"top": c + "-top.jpg", "best": c + "-best.jpg"}
    [j] = build(_north(1, 100.0, k3={"crops": shot("cam3-1"), "mixed": True}, k4={"crops": shot("cam4-1")}), _CAMS)
    assert j["crops"]["best"] == "cam4-1-top.jpg" and "front" not in j["crops"] and j["crops"]["rear"] == "cam4-1-best.jpg", j
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
    # No events: the best same-class member's attrs; event attrs still win; ids unchanged.
    h0 = [dict(m) for m in h]
    h0[0]["attrs"], h0[1]["attrs"] = {"axles": 2}, {"axles": 5}
    h0[0]["hits"], h0[1]["hits"] = 30, 20     # cam3 has more hits but is d-medium
    [jn] = build(h0, _CAMS)
    assert jn["attrs"] == {"axles": 5} and jn["id"] == j["id"], jn
    [jn] = build(h0, _CAMS, events={"obs-cam3-1": {"class": "e-heavy", "hits": 1, "attrs": {"axles": 7}}})
    assert jn["attrs"] == {"axles": 7}, jn
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
    # (o) ByteTrack twins: two ids on one crawling vehicle at once are one journey;
    # (p) but two that touch where the second starts and have parted by the end are two.
    crawl = [(t, _b(0.1 + 0.02 * (t - 100), 0.7 - 0.02 * (t - 100))) for t in range(100, 111)]
    first = _t("cam4", 1, "c-small", crawl, counted=True)
    assert len(build([first, _t("cam4", 2, "d-medium", crawl[3:], hits=8)], _CAMS)) == 1
    apart = [(t, _b(0.16 + 0.05 * (t - 103), 0.64)) for t in range(103, 111)]
    assert len(build([first, _t("cam4", 2, "c-small", apart, hits=8)], _CAMS)) == 2
    # (q) a twin that dies mid-frame is no stitch anchor: the next vehicle starting on its
    # last box, same class and way, stays its own journey.
    after = [(105.5 + k, _b(0.2 + 0.05 * k, 0.6 - 0.05 * k)) for k in range(4)]
    assert len(build([first, _t("cam4", 2, "c-small", crawl[3:6]),
                      _t("cam4", 3, "c-small", after, counted=True)], _CAMS)) == 2
    # (L) cam3 first sees a southbound vehicle 8 s after it left cam4, mid-frame and moving away
    # from its zone: it links on matching codes (L1) only; unlike codes (L2) or none (L3) leave two.
    for c3, c4, n in ((code(1), code(1), 1), (code(1), code(-1), 2), (None, None, 2)):
        k = lambda c: {"app": c, "app_v": "m1"} if c else {}
        out = _t("cam4", 1, "c-small", [(100, _b(0.45, 0.35)), (101, _b(0.3, 0.5)), (102, _b(0.1, 0.7))],
                 counted=True, **k(c4))
        late = _t("cam3", 1, "c-small", [(110, _b(0.5, 0.4)), (111, _b(0.3, 0.3))], counted=True, hits=6, **k(c3))
        js = build([out, late], _CAMS)
        assert len(js) == n and (n == 2 or js[0]["link"]["gap_s"] == 8.0), (n, js)
    # (T) one long truck as two concurrent boxes, the small one inside the big (IoU 0.25):
    # one chain on matching class and codes (T1); two on another class or unlike codes (T2).
    k = lambda c: {"app": code(c), "app_v": "m1"}
    big = [(t, _b(0.5, 0.4, 0.2)) for t in (100, 101, 102, 103)]
    part = [(101, _b(0.5, 0.4, 0.17)), (102, _b(0.5, 0.4, 0.1)), (103, _b(0.5, 0.4))]  # born on the truck, then shrinks
    for cls, c, n in (("c-small", 1, 1), ("d-medium", 1, 2), ("c-small", -1, 2)):
        assert len(_chains([_t("cam4", 1, "c-small", big, **k(1)), _t("cam4", 2, cls, part, **k(c))], _CAMS)) == n, (cls, c)
    # (T3) a near vehicle and a far one nose to tail, alike, the far box inside the near: two.
    near = _t("cam4", 1, "d-medium", [(t, [0.25, 0.25, 0.75, 0.75]) for t in (100, 101, 102, 103)], **k(1))
    far = _t("cam4", 2, "d-medium", [(t, [0.35, 0.3, 0.55, 0.5]) for t in (101, 102, 103)], **k(1))
    assert len(_chains([near, far], _CAMS)) == 2
    # (L4) a late chain never takes a departure from its real, uncoded arrival.
    out = _t("cam4", 1, "c-small", [(100, _b(0.45, 0.35)), (101, _b(0.3, 0.5)), (102, _b(0.1, 0.7))],
             counted=True, **k(1))
    real = _t("cam3", 1, "c-small", [(103, _b(0.9, 0.8)), (104, _b(0.9, 0.8)), (105, _b(0.7, 0.6))], hits=8)
    late = _t("cam3", 2, "c-small", [(103.5, _b(0.5, 0.4)), (104.5, _b(0.3, 0.3))], hits=6, **k(1))
    [j] = build([out, real, late], _CAMS)
    assert ids([j]) == [["obs-cam3-1", "obs-cam4-1"]], j
    # (C) a slow truck's cam3 arrival 10 s after its cam4 departure (past LAG): linked only on like codes (C1);
    # a convoy never crosses (C2); cars are not widened (C3).
    k = lambda c: {"app": code(*c), "app_v": "m1"}
    dep4 = lambda i, t, cls, c: _t("cam4", i, cls, [(t, _b(.45, .35)), (t + 1, _b(.3, .5)), (t + 2, _b(.1, .7))],
                                   counted=True, hits=8, **k(c))
    arr3 = lambda i, t, cls, c: _t("cam3", i, cls, [(t, _b(.9, .8)), (t + 1, _b(.7, .6)), (t + 2, _b(.5, .4))],
                                   counted=True, hits=8, **k(c))
    for c, n in (((1,), 1), ((0, 1), 2), ((.5, .866), 2), ((.8, .6), 1)):
        assert len(build([dep4(1, 100, "e-heavy", (1,)), arr3(1, 112, "e-heavy", c)], _CAMS)) == n, c
    assert len(build([dep4(1, 100, "c-small", (1,)), arr3(1, 112, "c-small", (1,))], _CAMS)) == 2
    # A leaves first, B 3 s later; B's own arrival B' (0.5 s after B leaves) is linked by pass 1; A's only
    # arrival A' lands 10 s after A, after B' (0.5 s beyond B's 105 + 2 s exit): linking A->A' would invert.
    A, B = dep4(1, 100, "e-heavy", (1,)), dep4(2, 103, "e-heavy", (0, 1))
    Bp, Ap = arr3(2, 105.5, "e-heavy", (0, 1)), arr3(1, 112, "e-heavy", (1,))
    assert ids(build([A, B, Ap, Bp], _CAMS)) == [["obs-cam3-1"], ["obs-cam3-2", "obs-cam4-2"], ["obs-cam4-1"]]
    # Slow convoy: A exits 102, B 106; A' enters 113, B' 115, all alike. Cheapest-first would pair B->A'
    # and strand both; the order-preserving selection links A+A' and B+B'.
    A, B, Ap, Bp = dep4(1, 100, "e-heavy", (1,)), dep4(2, 104, "e-heavy", (1,)), \
        arr3(1, 113, "e-heavy", (1,)), arr3(2, 115, "e-heavy", (1,))
    assert ids(build([A, B, Ap, Bp], _CAMS)) == [["obs-cam3-1", "obs-cam4-1"], ["obs-cam3-2", "obs-cam4-2"]]
    # (j) a rebuild, in any input order, yields the same ids.
    assert [j["id"] for j in build(q, _CAMS)] == [j["id"] for j in build(q[::-1], _CAMS)]
    # A whole day at a gate: 10k passes, 20k tracklets, in windowed time rather than all pairs.
    start = time.perf_counter()
    assert len(build([t for i in range(10_000) for t in _north(i, 100.0 + 5 * i)], _CAMS)) == 10_000
    took = time.perf_counter() - start
    print("journeys self-check ok: handoff links front to rear, queues pair in order, long trucks "
          "link across the overlap, opposed fragments stay apart, parked vehicles never count, "
          "classes fuse by votes, ghosts join, glare blips drop, far fast fragments join, twins merge and anchor nothing, ids are stable; "
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
