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
from math import isfinite
from statistics import median
from zoneinfo import ZoneInfo

from detect import (GHOST_BIRTH_DIST, HEADINGS, OPPOSITE, bound_of, ghost_birth_distance,
                    heading_of, iou, letter_of)

STITCH_GAP = 3.0     # s: ordinary same-camera fragment continuation
OCCLUDED_GAP = 6.0   # s: only with oncoming heavy cover at loss; 18 Oct tipper needs 4.4 s
OVERTAKE_GAP = 15.0  # s: hard ceiling; measured same-direction cover must support the gap
STITCH_IOU = 0.2     # the next fragment starts roughly where the last one ended
TWIN_IOU = 0.5       # detect.GUARD_IOU: one vehicle, two ids, the same box at the same instant
TWIN_COVER = 0.8     # a box this much inside another is the same vehicle seen in part (cab + whole truck)...
TWIN_TAIL_COS = 0.85  # a coach tail born on the whole vehicle may slide off its box before dying
TWIN_COS = 0.5       # ...when the class agrees and the appearance codes cosine is at least this
TWIN_HEAVY_SHARE = 0.25  # a fragment of another class is the same truck if this share of its votes is heavy
TWIN_FRAG_S = 2.0    # s: ...and it dies this soon after the whole truck's track is born (the real cab fragment: 1.0)
CONTAIN_NEAR = 1.27  # interpolated box widths: the minibus switch is 1.21 at birth, 1.263 at its end
TWIN_SNAP = 1.0      # s: a path sample stands for a moment this close to it (paths are ~1 sample/s)
NEAR = 2.0           # a far, fast box outruns IoU between fragments; a continuation lands within
                     # this many box-sizes of where the last one was heading
REVERSE = 0.2       # normalised centre displacement required on BOTH legs of a reversal
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
APP_PEN = 8.0        # gate 2493 vs hand 2493, abs 91, over 57; fixes the 11 Oct Land Cruiser crossing:
                     # true cos .89 beats .28, 2 s closer
APP_TYPICAL = 0.7    # cosine of a typical true match (Katuba median ~0.79): an uncoded candidate pays
                     # a typical-match penalty: at equal timing it beats only codes with cosine below .7
APP_VETO = 0.2       # known cosine below this is never a link except same-class pairs at night:
                     # true pairs' 1st percentile is far above; audited links under it were different vehicles
DAY_HOURS = (6, 18)  # local hours (build's tz) the appearance veto applies to any pair; outside them only across classes
HEAVY = ("d-medium", "e-heavy", "f-abnormal")
SMALL = ("a-small",)  # never links to HEAVY: 0 of 43 labelled true cross-camera pairs do, 16 of 89 wrong candidates
PARK_S = 2.0         # s: a box held this long is a parked vehicle
PARK_IOU = 0.6       # a sample overlapping the anchor box this much has not moved
REPARK_S = 15.0      # s: a new track born on the parked box this soon after it pulled away took it over
REPARK_IOU = 0.5     # overlap of that birth box with the parked box
HIJACK_STILL_S = 5.0  # s: the hijacked track sat this long before pulling away; a queue advance (next vehicle moves into the vacated spot) mimics a hijack, a true park does not
REPARK_STILL_S = 5.0  # s: the re-park track stays still this long from birth; a true parked vehicle and its stolen track both sat for seconds (2 s parked->moving still flags photos)
TRUCK_LEAD = 8.0     # s: a slow long truck's arrival may precede its departure...
TRUCK_LAG = 15.0     # ...or follow it this long
TRUCK_COS = 0.7      # look agreement a wider window demands
WORKS_PLANT_HITS = 20  # a plant (e-plant) track this solid means works are on the road, not a passing roller glimpse
WORKS_WINDOW = 900.0   # s: a plant track covers [t0 - this, t1 + this], its whole stay and this much either side
WORKS_PLANT_TRACKS = 3  # works need this many solid plant tracks in the window (a lone one can be a passing roller)...
WORKS_PLANT_LONG = 150  # ...or one this long (a roller that sat in view): 7 Oct morning max 11 hits, 9 Oct noon 62 tracks of 20+
WORKS_RESCUE_HITS = 20  # a chain works mode rescues needs this many hits: 9 Oct hand counts, 5 hits over-counted 14:00 (+5.9%), 20 gives 12:00 -5.7%, 14:00 +2.3%, combined -1.5% (under preferred)
MIN_EDGE_HITS = 5    # ~1 s at 5 fps: a zone crossing alone must last this long; night glare blips don't

RIDE_CLASSES = HEAVY  # only heavy parts may ride; covered light followers remain distinct vehicles
RIDE_COVER = 0.5     # a tractor-chain box must cover this share of its part at least once
RIDE_END_S = 2.0     # s: a towed part disappears with its tractor
RIDE_GAP = 0.25      # smaller-box heights: a towed part touches its tractor at every common instant
RIDE_SLIP = 0.25     # of the tractor's displacement: it moves with it
RIDE_LANE = 0.5      # tractor widths: in line behind it, not in the next lane
PAIR_COS = 0.85      # labelled general leftovers: 41/45 correct (91.1%)
PAIR_SMALL_COS = 0.88  # similar small cars need stronger appearance evidence
PAIR_HEAVY_COS = 0.75  # labelled heavy/bus handoffs after window filtering: 74/78 correct (94.9%)
PAIR_MARGIN = 0.05  # a near-equal second candidate is too ambiguous to pair
PAIR_WINDOW = (-8.0, 12.0)  # s: real departure-end to arrival-start window for general leftovers
PAIR_HEAVY_WINDOW = (-15.0, 20.0)  # s: heavy/bus zone departure to arrival


def _centre(b):
    return (b[0] + b[2]) / 2, (b[1] + b[3]) / 2


def _clash(a, b):
    return a in SMALL and b in HEAVY or b in SMALL and a in HEAVY


def _hijacked(tracklets, anchors=None):
    """{id: (pull-away time, re-park tracklet id)} of tracklets that sat still, then pulled away as a new
    track was born on the parked box: the track slid onto passing traffic, so its crops, attrs and
    look are the parked vehicle's, and the re-park track is the parked vehicle itself.
    If supplied, anchors receives the still box that triggered each hijack."""
    by = {}
    for t in tracklets:
        by.setdefault(t["camera"], []).append(t)
    out = {}
    for ts in by.values():
        ts.sort(key=lambda t: t["t0"])
        for a in ts:
            path = a.get("path") or []      # a node without images writes no path
            anchor = path[:1]
            for s in path[1:]:
                if iou(anchor[0][1:], s[1:]) >= PARK_IOU:
                    continue
                if s[0] - anchor[0][0] >= PARK_S:
                    lo = bisect_left(ts, s[0] - 1, key=lambda t: t["t0"])
                    b = next((b for b in ts[lo:bisect_right(ts, s[0] + REPARK_S, key=lambda t: t["t0"])]
                              if b is not a and b.get("path") and iou(anchor[0][1:], b["path"][0][1:]) >= REPARK_IOU), None)
                    if b:
                        out[a["id"]] = s[0], b["id"]
                        if anchors is not None:
                            anchors[a["id"]] = anchor[0][1:]
                    break
                anchor = [s]
    return out


def _app(ms):
    """(unit vector, model version) of the member with the most hits that has a code, else None.
    A mixed member's code may be the other vehicle's: it never speaks for the chain. (A hijacked
    one keeps its code: the flag also fires on ordinary queue advances, and linking without
    their codes measurably let more wrong pairs through - it only steers photos and attrs.)"""
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


REID_MISSED_HEAVY_COS = {"reid-v1-clip-b32-laion2b-head-r1": .5365007054807878}


def _reids(ms):
    """All clean, valid member codes, normalized after float16 storage."""
    out = []
    for t in ms:
        if t.get("mixed") or not t.get("reid") or not t.get("reid_v"):
            continue
        try:
            v = struct.unpack("<256e", base64.b64decode(t["reid"], validate=True))
            n = sum(x*x for x in v)**.5
            if n > 0 and isfinite(n):
                out.append((t["camera"], tuple(x/n for x in v), t["reid_v"]))
        except (TypeError, ValueError, struct.error):
            pass
    return out


def _reid_sim(a, b, *, passing=False):
    return max((score for ca,va,version in a for cb,vb,vb_version in b
                if version == vb_version and ca != cb and version in REID_MISSED_HEAVY_COS
                for score in [sum(x*y for x,y in zip(va,vb))]
                if not passing or score >= REID_MISSED_HEAVY_COS[version]), default=None)


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
    if a.get("direction") and b.get("direction") and OPPOSITE.get(a["direction"]) == b["direction"]:
        return True         # detect already read them as opposite ways, however slowly B started
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
    class or both heavy: every false merge by position on footage was a big vehicle occluding a small
    one, a small-heavy jump (heavy classes flicker on one truck). Never behind A: that is the next vehicle in the queue."""
    if a["class"] != b["class"] and not (a["class"] in HEAVY and b["class"] in HEAVY and abs(complex(*(_leaving(a["path"]) or (0, 0)))) >= MOVE / 2):
        return False    # a stopped truck never swallows a different one queued behind it
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


def _ati(path, t):
    """Interpolate nearby samples; sparse gaps keep _at's unreadable intervals."""
    i = bisect_left(path, t, key=lambda p: p[0])
    if 0 < i < len(path) and path[i][0] - path[i - 1][0] <= 2 * TWIN_SNAP:
        p, q = path[i - 1], path[i]
        f = (t - p[0]) / (q[0] - p[0]) if q[0] > p[0] else 0
        return [x + f * (y - x) for x, y in zip(p[1:], q[1:])]
    return _at(path, t)


def _occluded(a, others):
    """An oncoming heavy's larger box covers A when its last sample disappears."""
    last = a["path"][-1]
    box = last[1:]
    x, y = _centre(box)
    for b in others:
        if (b is a or b["camera"] != a["camera"] or b["class"] not in HEAVY
                or not a.get("direction") or b.get("direction") != OPPOSITE.get(a["direction"])):
            continue
        cover = _at(b["path"], last[0])
        if (cover and (cover[2] - cover[0]) * (cover[3] - cover[1]) > (box[2] - box[0]) * (box[3] - box[1])
                and (iou(box, cover) > 0 or cover[0] <= x <= cover[2] and cover[1] <= y <= cover[3])):
            return True
    return False


def _overtaken(a, others):
    """{occluder id: last covered time} for uninterrupted overtaking cover after A's loss."""
    last = a["path"][-1]
    loss, box = last[0], last[1:]
    va = _leaving(a["path"])
    if not a.get("direction") or not va or abs(complex(*va)) < MOVE / 2:
        return {}
    x, y = _centre(box)
    covers = {}
    for truck in others:
        if (truck["id"] == a["id"] or truck["camera"] != a["camera"]
                or truck["class"] not in HEAVY or truck.get("direction") != a["direction"]):
            continue
        path = truck["path"]
        cover = _at(path, loss)
        if (not cover or iou(box, cover) <= 0
                or (cover[2] - cover[0]) * (cover[3] - cover[1]) <= (box[2] - box[0]) * (box[3] - box[1])):
            continue
        i = bisect_right(path, loss, key=lambda p: p[0])
        before = path[:i]
        if not before or before[-1][0] < loss:
            before = [*before, [loss, *cover]]
        vb = _leaving(before)
        if not vb or vb[0] * va[0] + vb[1] * va[1] <= va[0] ** 2 + va[1] ** 2:
            continue    # the covering truck must gain on A along A's own motion
        end = None
        previous = loss
        for t, *bounds in [[loss, *cover], *path[i:]]:
            # A missing interval longer than _at can represent is not measured cover.
            if t - previous > 2 * TWIN_SNAP:
                break
            px, py = x + va[0] * (t - loss), y + va[1] * (t - loss)
            inside = bounds[0] <= px <= bounds[2] and bounds[1] <= py <= bounds[3]
            if end is None:
                if t - loss > STITCH_GAP:
                    break
            elif not inside:
                break    # later re-covering must not bridge an uncovered interval
            if inside:
                end = t
            previous = t
        if end is not None:
            covers[truck["id"]] = end
    return covers


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

    def heavy(x, y):    # x is a heavy class and y's votes carry a heavy share: a truck's cab read as light
        return x["class"] in HEAVY and sum(v for c, v in y["votes"].items() if c in HEAVY) >= TWIN_HEAVY_SHARE * sum(y["votes"].values())

    def same(p, q, born):
        if iou(p, q) >= TWIN_IOU:
            return True
        if not cover(p, q):
            return False
        if born:
            # Only a part-view fragment of another class (a truck's cab read as light) that B grows over
            # and that dies within TWIN_FRAG_S of B's birth: a same-class pair, B born small inside A
            # (a queue), or an A that drives on beside B (the next lane) stays two vehicles.
            if (a["class"] == b["class"] or _clash(a["class"], b["class"]) or a["t1"] - b["t0"] > TWIN_FRAG_S
                    or a["hits"] >= b["hits"]     # a fragment is the briefer view; a covered car built up its own
                    or (p[2] - p[0]) * (p[3] - p[1]) >= (q[2] - q[0]) * (q[3] - q[1])):
                return False
        if not alike:
            alike.append((a["class"] == b["class"] or not _clash(a["class"], b["class"]) and (heavy(a, b) or heavy(b, a)))
                         and (_similar(_app([a]), _app([b])) or 0) >= TWIN_COS)
        return alike[0]

    if (a["class"] == b["class"] == "c-bus"
            and b["t1"] < a["t1"] and b["hits"] < a["hits"]
            and any(p and iou(p, b["path"][0][1:]) >= TWIN_IOU
                    for p in (_at(a["path"], b["t0"]), _ati(a["path"], b["t0"])))
            and (_similar(_app([a]), _app([b])) or 0) >= TWIN_TAIL_COS):
        return True
    return all(p and q and same(p, q, born) for born, p, q in (
        (True, _at(a["path"], b["t0"]), b["path"][0][1:]), (False, _at(a["path"], end), _at(b["path"], end))))


def _rides(c, p):
    """Chain c is a towed or carried part of chain p on the same camera: born after it, attached to it at every
    common instant, in line with it, moving with it, and gone when it goes."""
    if any((letter_of(x["class"]) or "") < "D" for x in c["members"]):
        return False    # a broad tractor twin can also cover a separate light follower
    for x in c["members"]:
        for y in p["members"]:
            lo, hi = x["t0"], min(x["t1"], y["t1"])
            if x["t0"] < y["t0"] or hi - lo < 1 or abs(x["t1"] - y["t1"]) > RIDE_END_S or (
                    x.get("direction") and y.get("direction") and x["direction"] != y["direction"]):
                continue
            times = sorted({lo, hi} | {s[0] for s in x["path"] + y["path"] if lo <= s[0] <= hi})
            rows = [(_ati(y["path"], t), _ati(x["path"], t)) for t in times]
            if (len(rows) < 3 or any(not q or not r for q, r in rows)
                    or any(t - prev > 2 * TWIN_SNAP for prev, t in zip(times, times[1:]))):
                continue
            def apart(q, r):
                dx = max(0, max(q[0], r[0]) - min(q[2], r[2]))
                dy = max(0, max(q[1], r[1]) - min(q[3], r[3]))
                return abs(complex(dx, dy)) > RIDE_GAP * min(q[3] - q[1], r[3] - r[1])
            if any(apart(q, r) for q, r in rows):
                continue
            (q0, r0), (q1, r1) = rows[0], rows[-1]
            move = complex(*_centre(q1)) - complex(*_centre(q0))
            if abs(move) < 3 * MOVE or abs(complex(*_centre(r1)) - complex(*_centre(r0)) - move) > RIDE_SLIP * abs(move):
                continue
            off = (complex(*_centre(r0)) - complex(*_centre(q0))) / (move / abs(move))
            if off.real > 0 or abs(off.imag) > RIDE_LANE * (q0[2] - q0[0]):
                continue
            # Co-motion also fits a convoy; a whole-vehicle member must cover the part at least once.
            for z in p["members"]:
                for t in sorted({s[0] for s in x["path"] + z["path"] if max(lo, z["t0"]) <= s[0] <= min(x["t1"], z["t1"])}):
                    r, q = _ati(x["path"], t), _ati(z["path"], t)
                    if r and q:
                        area = (r[2] - r[0]) * (r[3] - r[1])
                        overlap = max(0, min(r[2], q[2]) - max(r[0], q[0])) * max(0, min(r[3], q[3]) - max(r[1], q[1]))
                        if area > 0 and overlap / area >= RIDE_COVER:
                            return True
    return False


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


def _ghosts(tracklets, history=(), cameras=None):
    """Classify raw births; only direction-compatible continuations advance root history."""
    by = {t["id"]: t for t in [*history, *tracklets] if t.get("path")}    # a node without images writes no path
    up = {i: i for i in by}
    cfg = {c["name"]: c for c in cameras or ()}
    pieces = {i: _split(t, k, cfg.get(t["camera"], {})) if k is not None else [t]
              for i, t in by.items() for k in [_reversal(t) if cameras is not None else None]}
    leg_up = {p["id"]: p["id"] for ps in pieces.values() for p in ps}
    directions = {p["id"]: {p["direction"]} if p.get("direction") and p["hits"] >= MIN_EDGE_HITS else set()
                  for ps in pieces.values() for p in ps}

    def find(i, parents=up):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i

    latest = {}

    def join(a, b):
        a, b = find(a), find(b)
        if a != b:
            up[b] = a
            if b in latest and (a not in latest or (latest[b]["t1"], latest[b]["id"]) >
                                (latest[a]["t1"], latest[a]["id"])):
                latest[a] = latest[b]

    joins = []
    ended = iter(sorted(by.values(), key=lambda t: (t["t1"], t["id"])))
    last = next(ended, None)
    for t in sorted(by.values(), key=lambda t: (t["t0"], t["id"])):
        while last is not None and last["t1"] <= t["t0"]:
            latest[find(last["id"])] = last
            last = next(ended, None)
        root = by.get(t.get("ghost_of"))
        if root is not None:
            previous = latest.get(find(root["id"]))
            # No completed endpoint means the old id is still alive: retain the original join.
            if (previous is None or previous["camera"] == t["camera"] and
                    ghost_birth_distance(previous["path"][-1][1:], t["path"][0][1:]) <= GHOST_BIRTH_DIST):
                # Check the same resolved leg that _chains will join, but retain raw birth boxes.
                ps = pieces[(previous or root)["id"]]
                leg = max((p for p in ps if p["t1"] <= t["t0"]),
                          key=lambda p: (p["t1"], p["id"]), default=ps[0])
                a, b = find(leg["id"], leg_up), find(t["id"], leg_up)
                if any(OPPOSITE.get(d) in directions[b] for d in directions[a]):
                    continue
                leg_up[b] = a
                directions[a].update(directions[b])
                join(root["id"], t["id"])
                joins.append(((previous or root)["id"], t["id"]))
            elif previous is not None:
                # The recount guard suppressed a vehicle that drove in: restore its line evidence.
                by[t["id"]] = dict(t, counted=True)

    return by, joins


def _chains(tracklets, cameras, ghost_joins=None, history=()):
    """One chain per vehicle per camera: join classified ghosts and twins, then stitch
    fragments that pick up where another left off, cheapest first, one-to-one."""
    if ghost_joins is None:
        by, ghost_joins = _ghosts(tracklets)
    else:
        by = {t["id"]: t for t in tracklets if t.get("path")}
    up = {i: i for i in by}
    owned = set()
    excluded = {i: set() for i in by}
    start = {i: t["t0"] for i, t in by.items()}
    finish = {i: t["t1"] for i, t in by.items()}
    windows = {i: [] for i in by}
    legs = {i: {t["source"]["id"]: i} if t.get("source") else {} for i, t in by.items()}
    directions = {i: {t["direction"]} if t.get("direction") and t["hits"] >= MIN_EDGE_HITS else set()
                  for i, t in by.items()}

    def find(i):
        while up[i] != i:
            up[i] = up[up[i]]
            i = up[i]
        return i

    def join(a, b):
        a, b = find(a), find(b)
        if a != b:
            if any(find(i) == b for i in excluded[a]) or any(find(i) == a for i in excluded[b]):
                return False    # preserve an accepted overtake's distinct-vehicle exclusions
            if any(OPPOSITE.get(d) in directions[b] for d in directions[a]):
                return False
            if any(s in owned and legs[a][s] != legs[b][s] for s in legs[a].keys() & legs[b].keys()):
                return False    # an accepted ghost owns one resolved leg, never its sibling
            up[b] = a
            start[a] = min(start[a], start[b])
            finish[a] = max(finish[a], finish[b])
            windows[a].extend(windows[b])
            legs[a].update(legs[b])
            excluded[a].update(excluded[b])
            directions[a].update(directions[b])
        return True

    for a, b in ghost_joins:
        if a in by and b in by:
            if join(a, b) and by[a].get("source"):
                owned.add(by[a]["source"]["id"])

    per = {}
    for t in by.values():
        per.setdefault(t["camera"], []).append(t)
    for ts in per.values():
        ts.sort(key=lambda t: (t["t0"], t["id"]))
        for a in ts:
            # Forced like a ghost, any class (twins flicker), and no stitch slot used: detect's
            # recount guard only tags a twin once it crosses the count line.
            for b in ts[bisect_left(ts, a["t0"], key=lambda t: t["t0"]):
                        bisect_left(ts, a["t1"], key=lambda t: t["t0"])]:
                if b is not a and _twin(a, b):
                    join(a["id"], b["id"])
    end = {}
    for t in by.values():
        r = find(t["id"])
        end[r] = max(end.get(r, t["t1"]), t["t1"])
    evidence = {}
    for t in {t["id"]: t for t in [*history, *by.values()] if t.get("path")}.values():
        if t["class"] in HEAVY and t.get("direction"):
            evidence.setdefault(t["camera"], []).append(t)
    cands = []
    overtake_edges = {}
    for camera, ts in per.items():
        # Sweep heavy lifetimes at loss times; history supplies evidence, never candidates.
        heavy = sorted(evidence.get(camera, ()), key=lambda t: (t["t0"], t["id"]))
        born = iter(heavy)
        died = iter(sorted(heavy, key=lambda t: (t["t1"], t["id"])))
        first, last = next(born, None), next(died, None)
        active = {}
        for a in sorted(ts, key=lambda t: (t["path"][-1][0], t["id"])):
            loss = a["path"][-1][0]
            while first is not None and first["t0"] <= loss:
                active.setdefault(first["direction"], {})[first["id"]] = first
                first = next(born, None)
            while last is not None and last["t1"] < loss:
                active[last["direction"]].pop(last["id"], None)
                last = next(died, None)
            if end[find(a["id"])] > a["t1"]:
                continue    # its vehicle lived on under another id: a twin dying mid-frame left nowhere
            gap = OCCLUDED_GAP if _occluded(a, active.get(OPPOSITE.get(a.get("direction")), {}).values()) else STITCH_GAP
            ordinary_gap = gap
            overtaken = _overtaken(a, active.get(a.get("direction"), {}).values())
            occluders = {find(i) for i in overtaken if i in up}
            extended = []
            emergence = max(loss, max(overtaken.values(), default=loss) - STITCH_GAP)
            if overtaken:
                gap = max(gap, min(OVERTAKE_GAP, max(overtaken.values()) + STITCH_GAP - a["t1"]))
            lo = bisect_left(ts, a["t1"], key=lambda t: t["t0"])
            for b in ts[lo:bisect_right(ts, a["t1"] + gap, key=lambda t: t["t0"])]:
                if find(b["id"]) in occluders:
                    continue    # the covering truck is a different vehicle
                overtake_only = b["t0"] - a["t1"] > ordinary_gap
                if overtake_only and start[find(b["id"])] < emergence:
                    continue    # a vehicle already visible during cover is not A emerging
                if b["t0"] - a["t1"] > STITCH_GAP:
                    (ax, ay), (bx, by_) = _centre(a["path"][-1][1:]), _centre(b["path"][0][1:])
                    vx, vy = _leaving(a["path"]) or (0.0, 0.0)
                    if (b.get("direction") != a.get("direction") or not _near(a, b)
                            or (bx - ax) * vx + (by_ - ay) * vy <= 0):
                        continue
                if (b is not a and (iou(a["path"][-1][1:], b["path"][0][1:]) >= STITCH_IOU or _near(a, b))
                        and not _opposed(a, b)):
                    edge = (b["t0"] - a["t1"] + CLASS_PEN * (a["class"] != b["class"]), a["id"], b["id"])
                    if overtake_only:
                        extended.append(edge)
                    else:
                        cands.append(edge)
            if len({find(b) for _, _, b in extended}) == 1:
                cands.extend(extended)
                for _, aid, bid in extended:
                    overtake_edges[aid, bid] = emergence, tuple(i for i in overtaken if i in up)
    cands.sort()
    succ, pred = set(), set()
    for _, a, b in cands:
        emergence, covers = overtake_edges.get((a, b), (None, ()))
        if (emergence is not None and start[find(b)] < emergence
                or any(find(i) in (find(a), find(b)) for i in covers)):
            continue
        ra, rb = find(a), find(b)
        if ra != rb and (any(start[rb] < threshold
                             and not (by[a]["t1"] <= loss and finish[rb] <= loss)
                             for loss, threshold in windows[ra])
                         or any(start[ra] < threshold
                                and not (by[b]["t1"] <= loss and finish[ra] <= loss)
                                for loss, threshold in windows[rb])):
            continue    # only predecessors ending before loss may join the A side
        if a not in succ and b not in pred and join(a, b):
            if (a, b) in overtake_edges:
                windows[find(a)].append((by[a]["path"][-1][0], emergence))
            for i in covers:
                excluded[find(a)].add(i)
                excluded[find(i)].add(a)
            succ.add(a)
            pred.add(b)
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
        if all(t.get("parked") for t in ms):
            zone = None     # a parked vehicle is neither an arrival nor a departure
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
        elif zone:
            # Never in the zone yet moving away from it: cam3 at night locks on only mid-frame.
            zc = (zone[0] + zone[2] / 2, zone[1] + zone[3] / 2)
            first = ms[0]["path"]
            (x0, y0), (x1, y1) = _centre(first[0][1:]), _centre(first[-1][1:])
            if abs(complex(x1 - zc[0], y1 - zc[1])) - abs(complex(x0 - zc[0], y0 - zc[1])) >= MOVE:
                ch["late"] = first[0][0]
            elif ms[0]["hits"] < MIN_EDGE_HITS and abs(complex(x1 - x0, y1 - y0)) < MOVE:
                # A still prefix supplies timing only to the restricted sliver swap.
                for t in ms[1:]:
                    (x0, y0), (x1, y1) = _centre(t["path"][0][1:]), _centre(t["path"][-1][1:])
                    if (t["hits"] >= MIN_EDGE_HITS and
                            abs(complex(x1 - zc[0], y1 - zc[1])) - abs(complex(x0 - zc[0], y0 - zc[1])) >= MOVE):
                        ch["stub_late"] = t["path"][0][0]
                        break
            # Lost before the zone: use the last fragment's approach, not the whole chain's motion.
            last = max(ms, key=lambda t: t["t1"])["path"]
            (x0, y0), (x1, y1) = _centre(last[0][1:]), _centre(last[-1][1:])
            if abs(complex(x0 - zc[0], y0 - zc[1])) - abs(complex(x1 - zc[0], y1 - zc[1])) >= MOVE:
                ch["early"] = last[-1][0]
        chains.append(ch)
    return sorted(chains, key=lambda c: (c["camera"], c["members"][0]["t0"], c["members"][0]["id"]))


def _shadows(chains, works):
    # Keep shadow ownership separate from matching candidates.
    owners, per = {}, {}
    for c in chains:
        per.setdefault(c["camera"], []).append(c)
    for cs in per.values():
        shadows = [c for c in cs if all(not t.get("direction") and not t["counted"] for t in c["members"])
                   and not works(c)]
        if not shadows:
            continue
        start = lambda c: min(t["t0"] for t in c["members"])
        shadows.sort(key=start)
        for parent in cs:
            direction = next((t["direction"] for t in parent["members"]
                              if t.get("direction") and t["hits"] >= MIN_EDGE_HITS), None)
            if not direction:
                continue
            path = sorted((p for t in parent["members"] for p in t["path"]), key=lambda p: p[0])
            lo = bisect_left(shadows, path[0][0], key=start)
            for c in shadows[lo:bisect_right(shadows, path[-1][0] + .5, key=start)]:
                if (c.get("shadow") or max(t["t1"] for t in c["members"]) > path[-1][0] + .5
                        or c["class"] != parent["class"]):
                    continue
                samples = sorted((p for t in c["members"] for p in t["path"]), key=lambda p: p[0])
                if _opposed({"path": path, "direction": direction}, {"path": samples}):
                    continue
                for sample in samples:
                    i = bisect_left(path, sample[0], key=lambda p: p[0])
                    p, q = path[max(0, i - 1)], path[min(i, len(path) - 1)]
                    f = (sample[0] - p[0]) / (q[0] - p[0]) if q[0] > p[0] else 0
                    box = [x + f * (y - x) for x, y in zip(p[1:], q[1:])]
                    distance = abs(complex(*_centre(box)) - complex(*_centre(sample[1:])))
                    if distance > CONTAIN_NEAR * (box[2] - box[0]):
                        break
                else:
                    c["shadow"] = parent["members"][0]["id"]
                    owners[id(c)] = parent
    return owners


def _crosses(d, r, accepted, classes=HEAVY):
    heavy = lambda c: c["class"] in classes
    dep = lambda c: c["dep"] if c["dep"] is not None else c.get("early")
    arr = lambda c: c["arr"] if c["arr"] is not None else (
        c["late"] if c.get("late") is not None else c.get("stub_late", c["members"][0]["path"][0][0]))
    return heavy(d) and heavy(r) and dep(d) is not None and any(
        (dep(dd) - dep(d)) * (arr(aa) - arr(r)) < 0
        for dd, aa in accepted if heavy(dd) and heavy(aa) and dep(dd) is not None
        and (dd["camera"], aa["camera"]) == (d["camera"], r["camera"]))


def _links(chains, cameras, tz=None, shadows=None, absorbed=None):
    """Departures from one camera's zone to arrivals in its partner's, cheapest first; a
    chain takes part in one link at most. Overlap, not order, gates a candidate: a long
    truck fills both zones at once and can arrive seconds before it has finished leaving."""
    pairs = {frozenset((c["name"], c["handoff"]["camera"])) for c in cameras if c.get("handoff")}

    def z0(j):
        return chains[j]["z"][0] if chains[j]["arr"] is not None else chains[j]["late"]

    def z1(j):
        return chains[j]["z"][1] if chains[j]["arr"] is not None else chains[j]["late"]

    def candidates(late, used, early=False):
        arrivals = {}
        for j, r in enumerate(chains):
            if id(r) in used or early and sum(t["hits"] for t in r["members"]) < MIN_EDGE_HITS:
                continue
            if (r["arr"] is None and r.get("late") is not None
                    and sum(t["hits"] for t in r["members"]) >= MIN_EDGE_HITS) if late else r["arr"] is not None:
                arrivals.setdefault(r["camera"], []).append(j)
        # By zone entry; the longest zone stay bounds how far back an overlap can start.
        index = {cam: (sorted(js, key=z0), max(z1(j) - z0(j) for j in js))
                 for cam, js in arrivals.items()}
        cands = []
        for i, d in enumerate(chains):
            dep = d.get("early") if early else d["dep"]
            if dep is None or id(d) in used or early and sum(t["hits"] for t in d["members"]) < MIN_EDGE_HITS:
                continue
            start, end = (dep, dep) if early else d["z"]
            for cam, (js, span) in index.items():
                if cam == d["camera"] or frozenset((d["camera"], cam)) not in pairs:
                    continue
                lo = bisect_left(js, start - LEAD - (0 if early else span), key=z0)
                for j in js[lo:bisect_right(js, end + (LAG_LATE if late else LAG), key=z0)]:
                    r = chains[j]
                    if z1(j) >= start - LEAD:
                        sim = _similar(d["app"], r["app"])
                        if _clash(d["class"], r["class"]):
                            continue
                        # At night one camera sees a lit side and the other headlights: a same-class pair
                        # can look unalike and still be one vehicle (measured at dusk: 7 of 10 judged true).
                        night = not DAY_HOURS[0] <= datetime.fromtimestamp(dep, tz or timezone.utc).hour < DAY_HOURS[1]
                        if sim is not None and sim < APP_VETO and not (night and d["class"] == r["class"]):
                            continue
                        if (late or early) and (sim is None or sim < LATE_COS):
                            continue
                        t = abs(z0(j) - dep - MU) + CLASS_PEN * (d["class"] != r["class"])
                        cands.append((t, t + APP_PEN * (1 - (APP_TYPICAL if sim is None else sim)), i, j))
        return cands

    def match(late, used, early=False):
        """Match unused chains per component, preferring appearance when it preserves the link count."""
        cands = candidates(late, used, early)

        def greedy(cs, k):
            taken, out = set(), []
            for *_, i, j in sorted(cs, key=lambda c: (c[k], c[2], c[3])):
                if i not in taken and j not in taken:
                    taken |= {i, j}
                    out.append((chains[i], chains[j]))
            return out

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
    ordinary_count = len(links)
    # Late chains never compete with ordinary arrivals: only departures still unlinked may take one.
    links += match(True, {id(c) for link in links for c in link})
    # 11 Oct pickup: lost 1.8 s before arrival; use the ordinary -LEAD..LAG start window.
    # Early pass deliberately skips the lone-chain evidence filter: requiring it costs abs +3 / over +2 on the hand-count gate.
    links += match(False, {id(c) for link in links for c in link}, early=True)
    links += _trucks(chains, links, pairs)
    used = {id(c) for link in links for c in link}
    ta = lambda r: r["arr"] if r["arr"] is not None else (
        r["late"] if r.get("late") is not None else r.get("stub_late", r["members"][0]["path"][0][0]))


    # Index only unused substantial late/stub arrivals; recheck ownership after each swap.
    slivers = {}
    chain_index = {id(c): i for i, c in enumerate(chains)}
    for j, c in enumerate(chains):
        arrival = c.get("late") if c.get("late") is not None else c.get("stub_late")
        if (id(c) not in used and c["arr"] is None and arrival is not None
                and sum(t["hits"] for t in c["members"]) >= MIN_EDGE_HITS):
            slivers.setdefault(c["camera"], []).append((arrival, j))
    for arrivals in slivers.values():
        arrivals.sort()

    # Preserve the original late swap; sliver recovery is an additional fallback.
    for k in range(ordinary_count):
        d, r = links[k]
        sim = _similar(d["app"], r["app"])
        replacements = []
        if not (d["class"] == r["class"] or sim is None or sim >= LATE_COS
                or any(t.get("counted") or t.get("direction") for t in r["members"])):
            for c in candidates(True, used - {id(d)}):
                L = chains[c[3]]
                if chains[c[2]] is d and L["class"] == d["class"] and not _crosses(d, L, links):
                    replacements.append(c)
        direction = next((t["direction"] for t in d["members"] if t.get("direction")), None)
        weak = (sum(t["hits"] for t in r["members"]) < MIN_EDGE_HITS
                and not any(t.get("counted") for t in r["members"])
                and all(not t.get("direction") or t["direction"] == OPPOSITE.get(direction) for t in r["members"]))
        if not replacements and weak and sim is not None:
            # At 53 km/h the next camera sees the minibus before this zone loses it.
            for cam, arrivals in slivers.items():
                if cam == d["camera"] or frozenset((d["camera"], cam)) not in pairs:
                    continue
                lo = bisect_left(arrivals, d["dep"] - LEAD, key=lambda a: a[0])
                hi = bisect_right(arrivals, d["z"][1] + LAG_LATE, key=lambda a: a[0])
                for arrival, j in arrivals[lo:hi]:
                    L = chains[j]
                    if id(L) in used or L["class"] != d["class"]:
                        continue
                    look = _similar(d["app"], L["app"])
                    if look is None or look < .8 or look - sim < .05 or _crosses(d, L, links):
                        continue
                    t = abs(arrival - d["dep"] - MU)
                    if t > abs(ta(r) - d["dep"] - MU) + 3.0:
                        continue
                    replacements.append((t, t + APP_PEN * (1 - look), chain_index[id(d)], j))
        if replacements:
            *_, i, j = min(replacements, key=lambda c: (c[1], c[2], c[3]))
            links[k] = (d, chains[j])
            used.remove(id(r))
            used.add(id(chains[j]))
    shadows = shadows or {}
    linked_shadows = {}
    for k, (s, L) in enumerate(links):
        parent = shadows.get(id(s))
        if parent is not None:
            linked_shadows.setdefault(id(parent), []).append((s, L, k))
    removed = set()
    absorbing = set()
    # Re-point d, absorb s for freezing, and release only the decoy r.
    for k, (d, r) in enumerate(links[:]):
        if k in removed or id(d) not in linked_shadows:
            continue
        sim = _similar(d["app"], r["app"])
        if (d["class"] == r["class"]
                or any(t.get("counted") or t.get("direction") for t in r["members"])):
            continue
        replacements = []
        dep = d["dep"] if d["dep"] is not None else d["early"]
        for s, L, j in linked_shadows[id(d)]:
            if j in removed or L["class"] != d["class"]:
                continue
            parent_sim = _similar(d["app"], s["app"])
            if parent_sim is not None and parent_sim < LATE_COS:
                continue
            shadow_sim = _similar(s["app"], L["app"])
            if shadow_sim is None or shadow_sim < LATE_COS or (sim is not None and shadow_sim <= sim):
                continue
            if _crosses(d, L, (pair for i, pair in enumerate(links) if i not in removed)):
                continue
            replacements.append((abs(ta(L) - dep - MU), s["members"][0]["id"], L["members"][0]["id"], j, s, L))
        if replacements:
            *_, j, s, L = min(replacements, key=lambda c: c[:3])
            links[k] = (d, L)
            if absorbed is not None:
                absorbed[id(d)] = s
            removed.add(j)
            absorbing.add(id(d))
    links = [pair for k, pair in enumerate(links) if k not in removed]
    used = {id(c) for pair in links for c in pair}
    used |= {id(c) for c in chains if id(shadows.get(id(c))) in absorbing}
    recovered = []
    for c in chains:
        if id(c) in used or c["arr"] is not None or c["dep"] is not None:
            continue
        ms = c["members"]
        first = next((i for i, t in enumerate(ms) if t["hits"] >= MIN_EDGE_HITS and t.get("direction")), 0)
        prefix, tail = ms[:first], ms[first:]
        if (not prefix or any(t["hits"] >= MIN_EDGE_HITS or t.get("direction") for t in prefix)
                or tail[0].get("ghost_of") not in {t["id"] for t in prefix}
                or max(t["t1"] for t in prefix) >= tail[0]["t0"]):
            continue
        clean = _chains(tail, cameras)
        if len(clean) == 1 and clean[0]["arr"] is not None:
            # A weak ghost prefix can hide a real entry; repair only after established matches.
            recovered.append((c, c.get("z")))
            c["arr"], c["z"] = clean[0]["arr"], clean[0]["z"]
    if recovered:
        recovered_ids = {id(c) for c, _ in recovered}
        for d, r in match(False, used):
            if id(r) in recovered_ids and not _crosses(d, r, links):
                links.append((d, r))
        used = {id(c) for pair in links for c in pair}
        for c, z in recovered:
            if id(c) not in used:
                c["arr"] = None
                c["z"] = z
    return links


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
            accepted.setdefault((d["camera"], r["camera"]), []).append((d["dep"] if d["dep"] is not None else d["early"], ta(r)))
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
    out = []
    # One chain, one link across every DP: components go in a fixed order and skip what is taken.
    for key, (ds, rs) in sorted(comps.items(), key=lambda kv: (min(d["dep"] for d in kv[1][0].values()), kv[0][1:])):
        ds = sorted((d for d in ds.values() if id(d) not in used), key=lambda d: d["dep"])
        rs = sorted((r for r in rs.values() if id(r) not in used), key=ta)
        # ponytail: O(D*R) per component; one giant component of thousands of alike trucks ~10 s apart
        # would take seconds. Band the DP to the time window if it ever does.
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
                d, r = ds[i - 1], rs[j - 1]
                i, j = i - 1, j - 1
                acc = accepted.setdefault(key[1:], [])
                if not any((dd - d["dep"]) * (aa - ta(r)) < 0 for dd, aa in acc):  # crosses an earlier component's link
                    acc.append((d["dep"], ta(r)))
                    used |= {id(d), id(r)}
                    out.append((d, r))
    return out


def _pairs(lone, cameras, accepted=()):
    """Mutual, unambiguous appearance matches among final lone cam3/cam4 cards."""
    cfg = {c["name"]: c for c in cameras}
    handoffs = {frozenset((c["name"], c["handoff"]["camera"])) for c in cameras if c.get("handoff")}
    if frozenset(("cam3", "cam4")) not in handoffs:
        return []
    headings = {cam: HEADINGS.get(heading_of(cfg.get(cam, {}))) for cam in ("cam3", "cam4")}
    if headings["cam3"] is None or headings["cam4"] != OPPOSITE[headings["cam3"]]:
        return []
    starts = {id(c): min(t["t0"] for t in c["members"]) for c in lone}
    ends = {id(c): max(t["t1"] for t in c["members"]) for c in lone}
    dirs = {id(c): next((t["direction"] for t in c["members"] if t.get("direction")), None) for c in lone}
    heavy = lambda c: c["class"] in (*HEAVY, "c-bus")
    family = lambda c: "heavy" if c["class"] in HEAVY else "bus" if c["class"] == "c-bus" else "light"
    codes = {id(c): _reids(c["members"]) if heavy(c) else [] for c in lone}
    arrival = lambda c: c["arr"] if c["arr"] is not None else (
        c["late"] if c.get("late") is not None else c.get("stub_late"))
    times = {}
    rows = {}
    for i, c in enumerate(lone):
        direction = dirs[id(c)]
        if c["camera"] not in headings or direction not in headings.values():
            continue
        departure = direction == OPPOSITE[headings[c["camera"]]]
        t = (c["dep"] if c["dep"] is not None else c.get("early")) if departure else arrival(c)
        times[id(c)] = t if heavy(c) else ends[id(c)] if departure else starts[id(c)]
        if c["camera"] == "cam3" and times[id(c)] is not None:
            rows.setdefault((direction, family(c)), []).append((times[id(c)], i, c))
    for index in rows.values():
        index.sort()
    choices = {}
    for a in lone:
        if a["camera"] != "cam4" or times.get(id(a)) is None:
            continue
        direction = dirs[id(a)]
        index = rows.get((direction, family(a)), [])
        departure = direction == OPPOSITE[headings["cam4"]]
        low, high = PAIR_HEAVY_WINDOW if heavy(a) else PAIR_WINDOW
        t = times[id(a)]
        # Cam3 is indexed by arrival start or departure end, never by lifetime span.
        lo, hi = (t + low, t + high) if departure else (t - high, t - low)
        for _, _, b in index[bisect_left(index, lo, key=lambda row: row[0]):
                             bisect_right(index, hi, key=lambda row: row[0])]:
            d, r = (a, b) if departure else (b, a)
            gap = times[id(r)] - times[id(d)]
            cos = _similar(a["app"], b["app"])
            threshold = PAIR_HEAVY_COS if heavy(a) else max(PAIR_COS, PAIR_SMALL_COS if
                a["class"] in ("a-small", "a-motorcycle") or b["class"] in ("a-small", "a-motorcycle") else PAIR_COS)
            reid = _reid_sim(codes[id(a)], codes[id(b)], passing=True)
            scores = ([cos] if cos is not None and cos >= threshold else []) + (
                [reid] if reid is not None else [])
            if not scores:
                continue
            cos = max(scores)   # only qualifying evidence competes for mutual best
            if heavy(a) and _crosses(d, r, accepted, (*HEAVY, "c-bus")):
                continue
            for c, other in ((a, b), (b, a)):
                choices.setdefault(id(c), []).append((cos, abs(gap), id(other), d, r))
    best = {}
    for key, candidates in choices.items():
        candidates.sort(key=lambda row: (-row[0], row[1], row[2]))
        if len(candidates) == 1 or candidates[0][0] - candidates[1][0] > PAIR_MARGIN:
            best[key] = candidates[0]
    pairs = []
    for key, (_, _, other, d, r) in sorted(best.items(), key=lambda item: (
            -item[1][0], item[1][1], min(t["id"] for t in item[1][3]["members"]),
            min(t["id"] for t in item[1][4]["members"]))):
        if key == id(d) and other in best and best[other][2] == key:
            if _crosses(d, r, pairs, (*HEAVY, "c-bus")):
                continue
            d["dep"], r["arr"] = times[id(d)], times[id(r)]
            pairs.append((d, r))
    return pairs


def _member_attrs(ms, cls):
    """Attrs of the member with the most hits, preferring ones of the journey's class."""
    have = [t for t in ms if t.get("attrs")]
    have = [t for t in have if not (t.get("mixed") or t.get("hijacked"))] or have
    same = [t for t in have if t.get("class") == cls]
    return max(same or have, key=lambda t: t["hits"], default={}).get("attrs") or {}


def _doc(chains, link, cfg, tz, events, absorbed=()):
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
    pics = [t for t in ms if not (t.get("mixed") or t.get("hijacked"))]    # a switched or hijacked track's crops show another vehicle
    speeds = [t["speed_kph"] for t in pics if t.get("speed_kph") is not None]
    pics = pics if any((t.get("crops") or {}) for t in pics) else ms
    tops = [t for t in pics if (t.get("crops") or {}).get("top")]
    if tops:
        crops["best"] = max(tops, key=lambda t: t["conf"].get(cls, 0.0))["crops"]["top"]
    elif bests := [t for t in pics if (t.get("crops") or {}).get("best")]:
        crops["best"] = max(bests, key=_area)["crops"]["best"]
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
    evs = [(t, e) for t in ms if (e := (events or {}).get((t.get("source") or t)["id"])) and e.get("class") == cls]
    # A switched track's event attrs are another vehicle's: only clean members' events speak; with none,
    # _member_attrs below (which also prefers clean members) decides.
    evs = [e for t, e in evs if not (t.get("mixed") or t.get("hijacked"))]
    # Absorbed shadows freeze with their parent but contribute no journey evidence.
    member_ms = sorted([*ms, *absorbed], key=lambda t: (t["t0"], t["id"]))
    members = [{k: m[k] for k in ("id", "camera", "t0", "t1", "counted")} for m in member_ms]
    # Keep raw ids for audit lookup, but freeze and time each reversal leg independently.
    for m, member in zip(member_ms, members):
        if m.get("source"):
            member.update(id=m["source"]["id"], piece=m["id"])
    t = (link[0]["dep"] if link[0]["dep"] is not None else link[0]["early"]) if link else ms[0]["t0"]
    return t, {
        "id": "jny-" + hashlib.sha256("\0".join(sorted(m["id"] for m in member_ms)).encode()).hexdigest()[:24],
        "ts": datetime.fromtimestamp(t, timezone.utc).astimezone(tz).isoformat(timespec="seconds"),
        "cameras": names, "camera": "+".join(names),
        "class": cls, "letter": letter_of(cls), "conf": conf.get(cls),
        "direction": direction,
        "speed_kph": round(median(speeds), 1) if speeds else None,
        "bound": next((b for n in names if (b := bound_of(cfg.get(n, {}), direction))), None),
        "attrs": (max(evs, key=lambda e: e.get("hits", 0)).get("attrs") if evs else None) or _member_attrs(ms, cls),
        "crops": crops, "plates": plates,
        "members": members,
        "link": link and {"from": link[0]["camera"], "to": link[1]["camera"],
                          "gap_s": round((link[1]["arr"] if link[1]["arr"] is not None else (
                              link[1]["late"] if link[1].get("late") is not None
                              else link[1].get("stub_late", link[1]["members"][0]["path"][0][0]))) - t, 2)},
        "evidence": "handoff" if link else "line" if any(m["counted"] for m in ms) else "edge",
        "hits": sum(m["hits"] for m in ms),
        "dwell_s": round(max(m["t1"] for m in ms) - ms[0]["t0"], 1)}


def _held(t, t_off):
    """True if t sat still HIJACK_STILL_S up to (not including) its pull-away sample at t_off."""
    anchor = t["path"][0]
    last = anchor
    for p in t["path"]:
        if p[0] >= t_off:
            break
        if iou(anchor[1:], p[1:]) < PARK_IOU:
            anchor = p
        last = p
    return last[0] - anchor[0] >= HIJACK_STILL_S


def _stays(t):
    """True if t holds its birth box for REPARK_STILL_S and ends on it: one that drives off is a vehicle."""
    p0 = t["path"][0]
    # Ends where born: REPARK_IOU, not PARK_IOU - a parked box jitters (Maersk's parked truck: 0.58) -
    # and its centre within 2*MOVE: parked boxes measured drift 0.005-0.032, ones that left 0.05+,
    # and a wide truck can slide 0.13 sideways while still overlapping its birth box by 0.5.
    (x0, y0), (x1, y1) = _centre(p0[1:]), _centre(t["path"][-1][1:])
    return iou(p0[1:], t["path"][-1][1:]) >= REPARK_IOU and abs(complex(x1 - x0, y1 - y0)) < 2 * MOVE and max(p[0] for p in t["path"] if iou(p0[1:], p[1:]) >= PARK_IOU) - p0[0] >= REPARK_STILL_S


def _pulled(t, t_off):
    """Copy of hijacked t from its last still sample on: that is the passing vehicle, so its zone timing
    starts there - at the parked box, inside the zone (a wide truck's first moving box is already out)."""
    p = [p for p in t["path"] if p[0] >= max([q[0] for q in t["path"] if q[0] < t_off] or [t_off])]
    # votes/class stay whole: tracklets carry no per-frame votes
    return dict(t, hijacked=True, **({"path": p, "t0": p[0][0], "hits": max(1, round(t["hits"] * len(p) / len(t["path"])))}
                                     if len(p) >= 2 else {}))



def _reversal(t):
    """Index of a substantial turn-around, or None for jitter, short paths and straight travel."""
    path = t.get("path") or []
    if len(path) < 4:
        return None
    centres = [complex(*_centre(p[1:])) for p in path]
    k = max(range(len(path)), key=lambda i: abs(centres[i] - centres[0]))
    a, b = centres[k] - centres[0], centres[-1] - centres[k]
    return k if min(abs(a), abs(b)) >= REVERSE and (a.conjugate() * b).real < 0 else None


def _split(t, k, cam):
    """Share the turning box; keep the sole line count on the leg agreeing with net motion."""
    path = t["path"]
    centres = [complex(*_centre(p[1:])) for p in path]
    first = ((centres[k] - centres[0]).conjugate() * (centres[-1] - centres[0])).real > 0
    way = t.get("direction")
    heading = HEADINGS.get(heading_of(cam))
    pair = (cam.get("travel") or {}).get("neg"), (cam.get("travel") or {}).get("pos")
    opposite = OPPOSITE.get(way) if way and (way in pair or way in (heading, OPPOSITE.get(heading))) else None
    largest = max(range(len(path)), key=lambda i: (path[i][3] - path[i][1]) * (path[i][4] - path[i][2]))
    owner = 0 if largest <= k else 1
    hits = max(1, min(t["hits"] - 1, round(t["hits"] * (k + 1) / (len(path) + 1))))
    pieces = []
    for i, p in enumerate((path[:k + 1], path[k:])):
        # Travel-axis per-leg direction measured worse on the hand-count gate (abs +4, over +1).
        same = (i == 0) == first
        piece = dict(t, id=t["id"] if i == 0 else t["id"] + "~reverse", path=p,
                     t0=p[0][0], t1=p[-1][0], hits=hits if i == 0 else max(1, t["hits"] - hits),
                     direction=way if same else opposite, counted=t["counted"] and same,
                     speed_kph=None,
                     reverse=True, source={key: t[key] for key in ("id", "camera", "t0", "t1", "counted")})
        piece.update(crops={"best": t["crops"]["best"]} if (t.get("crops") or {}).get("best") else {}, plate=None)
        if i != owner:
            piece.update(mixed=True, app=None, crops={}, plate=None, attrs={})
        pieces.append(piece)
    return pieces


def _build(tracklets, cameras, tz=None, events=None, with_dropped=False, plant=None, frozen=(), history=()):
    if plant is None:
        plant = [(t["t0"], t["t1"], t["hits"]) for t in tracklets if t.get("class") == "e-plant"]
    cfg = {c["name"]: c for c in cameras}
    boxes = {}
    for name, cam in cfg.items():
        boxes[name] = []
        raw = cam.get("phantoms")
        if not isinstance(raw, (list, tuple)):
            continue
        for box in raw:
            try:
                box = tuple(float(v) for v in box)
                if (len(box) == 4 and all(isfinite(v) and 0 <= v <= 1 for v in box)
                        and box[0] < box[2] and box[1] < box[3]):
                    boxes[name].append(box)
            except (TypeError, ValueError, OverflowError):
                continue
    anchors = {}
    hj = _hijacked(tracklets, anchors)
    by = {t["id"]: t for t in tracklets}
    hit = {a: hj[a] for a in hj if _held(by[a], hj[a][0]) and _stays(by[hj[a][1]])}   # a parked vehicle's stolen track, not a queue advance
    parked = {b for _, b in hit.values()}
    # Fixed-camera calibration identifies empty road; track evidence alone also fits a booth queue.
    phantoms = {t["id"]: [box for box in boxes.get(t["camera"], [])
                          if iou(t["path"][0][1:], box) >= REPARK_IOU]
                for t in tracklets if t.get("path")}
    hit.update({a: hj[a] for a in hj
                if any(iou(anchors[a], box) >= REPARK_IOU for box in boxes.get(by[a]["camera"], []))})
    tracklets = [_pulled(t, hj[t["id"]][0]) if t["id"] in hit else dict(t, hijacked=True) if t["id"] in hj
                 else dict(t, parked=True) if t["id"] in parked else t for t in tracklets
                 if not any(all(iou(p[1:], box) >= REPARK_IOU for p in t["path"])
                            for box in phantoms.get(t["id"], []))]
    ghosts, ghost_joins = _ghosts(tracklets, history, cameras)
    tracklets = [ghosts.get(t["id"], t) for t in tracklets]
    pieces = {t["id"]: _split(t, k, cfg.get(t["camera"], {})) if k is not None else [t]
              for t in tracklets for k in [_reversal(t)]}
    # Resolve before frozen pieces disappear; opposition is checked on the selected legs in _chains.
    ghost_joins = [(max((p for p in pieces[a] if p["t1"] <= ghosts[b]["t0"]),
                       key=lambda p: (p["t1"], p["id"]), default=pieces[a][0])["id"], b)
                   for a, b in ghost_joins if a in pieces and b in pieces]
    tracklets = [p for ps in pieces.values() for p in ps]
    claimed = {raw for raw, _ in frozen}
    tracklets = [t for t in tracklets if (
        (t["source"]["id"], None) not in frozen and (t["source"]["id"], t["id"]) not in frozen
        if t.get("source") else t["id"] not in claimed)]
    plant = [p for p in plant if p[2] >= WORKS_PLANT_HITS]

    def works(c, rescue=True):    # plant never rescues itself, and a rescued chain needs substance
        ms = c["members"]
        if rescue and (2 * sum(t["hits"] for t in ms if t.get("class") == "e-plant") > sum(t["hits"] for t in ms)
                       or sum(t["hits"] for t in ms) < WORKS_RESCUE_HITS):
            return False
        t = min(m["t0"] for m in ms)
        near = [p for p in plant if p[0] - WORKS_WINDOW <= t <= p[1] + WORKS_WINDOW]
        return len(near) >= WORKS_PLANT_TRACKS or any(p[2] >= WORKS_PLANT_LONG for p in near)
    chains = _chains(tracklets, cameras, ghost_joins, history)
    shadows = _shadows(chains, lambda c: works(c, rescue=False))
    absorbed = {}
    links = _links(chains, cameras, tz, shadows, absorbed)
    linked = {id(c) for pair in links for c in pair}
    # A swap claims every eligible shadow still free after all link passes.
    absorbed = {parent: [c for c in chains if shadows.get(id(c)) is shadows[id(s)] and id(c) not in linked]
                for parent, s in absorbed.items()}
    linked |= {id(c) for cs in absorbed.values() for c in cs}
    # A lone chain counts if its camera counted it, or it crossed a handoff zone the partner
    # missed and stayed in view long enough to be a vehicle rather than headlight glare.
    lone = [c for c in chains if id(c) not in linked and (
            any(t["counted"] for t in c["members"])
            or (c["dep"] is not None or c["arr"] is not None)
            and sum(t["hits"] for t in c["members"]) >= MIN_EDGE_HITS)]
    # A lone chain with no direction is no passage, and a camera with lone_edge: false (cam3: the toll
    # queue's tail sits in its zone) does not count lone edge-only chains. Measured 7 Oct: +6.5% -> -1.7%
    # vs hand counts; under-counting is preferred.
    keep = lambda c: any(t.get("direction") for t in c["members"]) and (
        any(t["counted"] for t in c["members"]) or cfg.get(c["camera"], {}).get("lone_edge", True) is not False)
    # Works mode: roadworks (rollers, graders, lanes closed) mean stop-start crawl and diversions, so lone
    # chains the filter drops are real vehicles, not queue-tail duplicates. Measured 9 Oct noon: 184 -> 262
    # vs hand count 272; 7 Oct morning unchanged (no plant track there has 20+ hits).
    # An uncounted reversal leg needs a partner: a genuine lone U-turn is still one vehicle.
    accepted = lambda c: (not any(t.get("reverse") and not t["counted"] for t in c["members"])
                          or any(t["counted"] for t in c["members"])) and (keep(c) or works(c))
    dropped = [c for c in lone if not accepted(c)]
    lone = [c for c in lone if accepted(c)]
    # Only existing lone cards may become leftover handoffs; dropped chains remain dropped.
    links.extend(_pairs(lone, cameras, links))
    linked.update(id(c) for pair in links for c in pair)
    # Index member ends so rider checks stay local even on a full cached day.
    ends = {}
    for i, pair in enumerate(links):
        for p in pair:
            if p["class"] in (*HEAVY, "c-bus"):
                ends.setdefault(p["camera"], []).extend((t["t1"], i) for t in p["members"])
    for rows in ends.values():
        rows.sort()
    # Absorbed parts freeze with the tractor but supply no class, photo or count evidence.
    for c in chains:
        if (id(c) in linked or c["class"] not in RIDE_CLASSES
                or any(t["counted"] or t.get("reverse") for t in c["members"])):
            continue
        rows = ends.get(c["camera"], [])
        candidates = {i for t in c["members"] for _, i in rows[
            bisect_left(rows, t["t1"] - RIDE_END_S, key=lambda row: row[0]):
            bisect_right(rows, t["t1"] + RIDE_END_S, key=lambda row: row[0])]}
        for i in sorted(candidates):
            d, r = links[i]
            parent = d if d["camera"] == c["camera"] else r
            if _rides(c, parent):
                absorbed.setdefault(id(d), []).append(c)
                linked.add(id(c))
                break
    lone = [c for c in lone if id(c) not in linked]
    docs = [_doc(list(pair), pair, cfg, tz, events,
                 [t for c in absorbed.get(id(pair[0]), ()) for t in c["members"]]) for pair in links]
    docs += [_doc([c], None, cfg, tz, events) for c in lone]
    if with_dropped:    # lone chains the filter removed: not journeys, but a still-open one can yet link
        docs += [(ts, dict(d, dropped=True)) for ts, d in (_doc([c], None, cfg, tz, events) for c in dropped)]
    return [d for _, d in sorted(docs, key=lambda td: (td[0], td[1]["id"]))], chains, links


def build(tracklets, cameras, tz=None, events=None, with_dropped=False, plant=None, frozen=(), history=()):
    """Tracklets (detect.py) of paired cameras -> counted journeys, sorted by ts then id. with_dropped
    also returns the lone chains the direction/lone_edge filter removed, flagged "dropped": True.
    plant: [(t0, t1, hits)] of e-plant tracklets for works mode, None = derive from tracklets (a caller
    that drops frozen tracklets passes the unfiltered list, or works mode flips off for open chains).
    frozen: (raw id, piece id) pairs to exclude after splitting; None claims the whole raw tracklet.
    Any claimed leg also excludes a later unsplit copy of its raw tracklet.
    history: raw tracklets used only for ghost birth classification and heavy occluder evidence,
    including frozen tracks; never added to chain membership."""
    return _build(tracklets, cameras, tz, events, with_dropped, plant, frozen, history)[0]


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


def _selfcheck_phantoms(case):
    from selfloop import cleared

    box = _b(.845, .845, .055)
    cams = [dict(c, phantoms=[box]) if c["name"] == "cam3" else c for c in _CAMS]
    def stolen(name, born, off):
        return _t("cam3", name, "b-minibuses",
                  [(t, box) for t in range(born, off)] +
                  [(off, _b(.70, .80, .055)), (off + 4, _b(.30, .60, .03))],
                  hits=60, counted=True, direction="southbound")
    def departure(name, end):
        return _t("cam4", name, "b-minibuses",
                  [(end - 2, _b(.5, .4)), (end - 1, _b(.15, .7)), (end, _b(.02, .7))],
                  hits=30, counted=True, direction="southbound")
    a, b = stolen("first", 100, 108), stolen("second", 107, 451)
    park = _t("cam3", "phantom", "b-minibuses", [(t, box) for t in range(450, 461)],
              counted=True, direction="southbound")
    da, db = departure("first", 106), departure("second", 449)
    tracks = [a, b, park, da, db]
    if case == "unconfigured":
        _, chains, _ = _build(tracks, _CAMS)
        members = {m["id"]: m for c in chains for m in c["members"]}
        assert members[a["id"]]["path"] == a["path"], "no calibration: first relay keeps origin/main timing"
        assert members[b["id"]]["path"] == _pulled(b, 451)["path"], "ordinary held-and-stays trim retained"
        assert members[park["id"]].get("parked"), "ordinary re-park retained"
        return
    if case == "queue":
        cams = [dict(c, phantoms=[_b(.2, .2, .03)], lone_edge=False) if c["name"] == "cam3" else c for c in _CAMS]
        for gap in (1, 3):
            queue = []
            born = 100
            for i in range(4):
                off = born + 7
                queue.append(_t("cam4", f"v{i}", "a-small",
                    [(born - 4, _b(.5, .4)), (born - 2, _b(.2, .65)), (born - .5, _b(.05, .7))],
                    hits=20, counted=True, direction="southbound"))
                queue.append(_t("cam3", f"v{i}", "a-small",
                    [(born + k, _b(.9, .8, .06)) for k in range(8)] +
                    [(off + 1, _b(.7, .75, .05)), (off + 3, _b(.4, .65, .04)), (off + 6, _b(.05, .55, .02))],
                    hits=60, counted=True, direction="southbound"))
                born = off + gap
            js = build(queue, cams)
            assert len(js) == 4, ("booth queue with unrelated calibration", gap, len(js))
            assert all({m["id"] for m in j["members"]} == {f"obs-cam3-v{i}", f"obs-cam4-v{i}"}
                       for i, j in enumerate(js)), "queue vehicles keep their own partners"
        return
    js, chains, links = _build(tracks, cams)
    assert len(js) == 2 and len(links) == 2, ("calibrated phantom relay", len(js), len(links))
    assert [{m["id"] for m in j["members"]} for j in js] == [
        {a["id"], da["id"]}, {b["id"], db["id"]}], "still phantom never forms a counting journey"
    assert [r["arr"] for _, r in links] == [107, 450], "still heads excluded from handoff timing"
    assert not build([park], cams, with_dropped=True, plant=[(0, 1000, WORKS_PLANT_LONG)]), "still phantom cannot count even in works mode"
    if case == "freeze":
        final = cleared(js, js, cutoff=1000)
        assert final == js, "calibrated phantom freeze"
        claims = {(m["id"], m.get("piece")) for j in final for m in j["members"]}
        assert build(tracks, cams, frozen=claims) == [], "phantom cannot count after full freeze"
        still = dict(b, path=[p for p in b["path"] if p[0] <= 420], t1=420, counted=False, direction=None)
        early = build([a, still, da], cams)
        assert early == js[:1], "first vehicle links while phantom still holds"
        final = cleared(early, early, cutoff=420)
        assert final == js[:1], "first vehicle freezes before second arrives"
        claims = {(m["id"], m.get("piece")) for j in final for m in j["members"]}
        assert build(tracks, cams, frozen=claims) == js[1:], "phantom partial freeze stable"


def _selfcheck_phantom_regressions(case):
    box = _b(.845, .845, .055)
    if case in ("moving_prefix", "moved_onto"):
        anchor = _b(.6, .7, .055)
        birth = box
        if case == "moved_onto":
            birth, anchor = anchor, box
        a = _t("cam3", "moving-prefix", "b-minibuses",
               [(100, birth)] + [(t, anchor) for t in range(101, 108)] +
               [(108, _b(.7, .8, .055)), (112, _b(.3, .6, .03))],
               hits=60, counted=True, direction="southbound")
        b = _t("cam3", "replacement", "b-minibuses",
               [(107, anchor), (108, anchor), (113, _b(.4, .4, .03))])
        d = _t("cam4", "departure", "b-minibuses",
               [(96, _b(.5, .4)), (97, _b(.15, .7)), (98, _b(.02, .7))],
               hits=30, counted=True, direction="southbound")
        tracks = [a, b, d]
        expected = build(tracks, _CAMS)
        if case == "moving_prefix":
            assert len(expected) == 1, ("moving prefix baseline", expected)
        cams = [dict(c, phantoms=[box]) if c["name"] == "cam3" else c for c in _CAMS]
        if case == "moved_onto":
            _, chains, _ = _build(tracks, cams)
            member = next(m for c in chains for m in c["members"] if m["id"] == a["id"])
            assert member["path"] == _pulled(a, 108)["path"], "actual phantom anchor must trim despite a different birth box"
        else:
            assert build(tracks, cams) == expected, "birth phantom must not trim a different still anchor"
        return
    malformed = {
        "null": None, "number": 3, "string": "bad",
        "null_entry": [None], "number_entry": [3], "string_entry": ["bad"],
        "short": [[.1, .2, .3]], "long": [[.1, .2, .3, .4, .5]],
        "non_numeric": [[.1, .2, "bad", .4]],
        "nan": [[.1, .2, float("nan"), .4]],
        "inf": [[.1, .2, float("inf"), .4]],
        "outside": [[-.1, .2, .3, .4]],
        "reversed": [[.3, .2, .1, .4]], "flat": [[.1, .2, .3, .2]],
    }
    tracks = [_t("cam3", "real", "b-minibuses", [(100, box), (101, box)],
                 hits=30, counted=True, direction="southbound")]
    expected = build(tracks, _CAMS)
    assert len(expected) == 1, "invalid calibration baseline counts"
    cams = [dict(c, phantoms=malformed[case]) if c["name"] == "cam3" else c for c in _CAMS]
    assert build(tracks, cams) == expected, ("malformed phantom is absent", case)
    if isinstance(malformed[case], list):
        cams = [dict(c, phantoms=[*malformed[case], box]) if c["name"] == "cam3" else c for c in _CAMS]
        assert build(tracks, cams) == [], ("invalid entries do not discard valid calibration", case)


def _selfcheck_ghost_convoy():
    from selfloop import cleared
    from itertools import permutations

    root = _t("cam3", "root", "e-heavy", [(100, _b(.9, .7, .03)), (110, _b(.03, .5, .02))],
              hits=30, counted=True, direction="southbound")
    trucks = [dict(root, id=f"convoy-{i}", counted=False, ghost_of=root["id"],
                   t0=100 + 20 * i, t1=110 + 20 * i,
                   path=[[t + 20 * i, *b] for t, *b in root["path"]]) for i in range(1, 4)]
    tracks = [root, *trucks]
    cams = [dict(c, lone_edge=False) for c in _CAMS]
    assert len(_chains(tracks, cams)) == 4, "far-born convoy must not join its root"
    expected = build(tracks, cams)
    assert len(expected) == 4 and all(j["evidence"] == "line" for j in expected), "refused ghosts count as line evidence"
    assert not any(t["counted"] for t in trucks), "rebuild must not mutate stored tracklets"
    for shuffled in permutations(tracks):
        assert build(shuffled, cams) == expected, "convoy input order"
    final = cleared(expected, expected, cutoff=1000)
    assert final == expected
    claims = {(m["id"], m.get("piece")) for j in final for m in j["members"]}
    assert build(tracks, cams, frozen=claims) == [], "convoy freeze -> rebuild"

    convoy = [root, *[dict(root, id=f"convoy-{i}", counted=False, ghost_of=root["id"],
                          t0=100 + 20 * i, t1=110 + 20 * i,
                          path=[[t + 20 * i, *b] for t, *b in root["path"]]) for i in range(1, 31)]]
    journeys = build(convoy, cams)
    assert len(journeys) == 31, "long convoy counts every truck"
    final = cleared(journeys, journeys, cutoff=800)
    assert len(final) == 5, "partial convoy freeze includes the root"
    claims = {(m["id"], m.get("piece")) for j in final for m in j["members"]}
    remaining = build(convoy, cams, frozen=claims)
    assert len(remaining) == 26, ("partial freeze must retain refused ghosts' line evidence", len(remaining))
    assert remaining == [j for j in journeys if j not in final], "partial convoy freeze stable"
    assert build(convoy[1:], cams) == [], "missing root alone does not restore counting evidence"
    assert not any(t["counted"] for t in convoy[1:]), "classification never mutates stored tracklets"

    turn = _t("cam3", "convoy-turn", "e-heavy",
              [(120, _b(.97, .5, .02)), (125, _b(.8, .5, .02)),
               (130, _b(.6, .5, .02)), (135, _b(.85, .5, .02))],
              hits=30, direction="southbound", ghost_of=root["id"])
    refused = build([root, turn], cams)
    counted = build([root, dict(turn, counted=True, ghost_of=None)], cams)
    assert len(refused) == len(counted) == 2, "refused reversal carries only one line count"
    assert refused == counted, "refused ghost pieces behave like counted tracklet pieces"

    # Root ids persist through ghosts: compare against the latest continuation's end.
    stalled = dict(trucks[0], t0=120, t1=130,
                   path=[[120, *_b(.03, .5, .02)], [130, *_b(.8, .5, .02)]])
    again = dict(trucks[1], t0=140, t1=150,
                 path=[[140, *_b(.8, .5, .02)], [150, *_b(.8, .5, .02)]])
    [chain] = _chains([root, stalled, again], cams)
    assert len(chain["members"]) == 3, "stalled ghosts follow the latest resting place"
    nearby = dict(stalled, path=[[120, *_b(.43, .5, .02)], *stalled["path"][1:]])
    assert len(_chains([root, nearby], cams)) == 1, "nearby disjoint return stays a ghost"
    back = dict(again, path=[[140, *_b(.03, .5, .02)], [150, *_b(.03, .5, .02)]])
    assert len(_chains([root, stalled, back], cams)) == 2, "old root box is no longer the resting place"


def _selfcheck_ghost_reversal():
    from datetime import datetime, timezone
    from unittest.mock import patch
    import selfloop

    root = _t("cam3", "turn-root", "e-heavy",
              [(100, _b(.05, .5, .02)), (105, _b(.9, .5, .02)),
               (110, _b(.5, .5, .02)), (120, _b(.1, .5, .02))],
              hits=100, counted=True, direction="northbound")
    first = _t("cam3", "turn-first", "e-heavy",
               [(108, _b(.9, .5, .02)), (109, _b(.9, .5, .02))],
               hits=10, ghost_of=root["id"], direction="northbound")
    last = _t("cam3", "turn-last", "e-heavy",
              [(130, _b(.1, .5, .02)), (135, _b(.1, .5, .02))],
              hits=10, ghost_of=root["id"])

    def joined(tracks, ghost):
        chains = _build(tracks, _CAMS)[1]
        return {m["id"] for c in chains if any(m["id"] == ghost["id"] for m in c["members"])
                for m in c["members"]}

    assert joined([root, last], last) == {root["id"] + "~reverse", last["id"]}, \
        "final-endpoint ghost must join the second reversal piece"
    assert joined([root, first], first) == {root["id"], first["id"]}, \
        "turn-endpoint ghost must join the first reversal piece"
    slow_turn = dict(root, direction=None,
                     path=[*root["path"][:2], [106, *_b(.899, .5, .02)], *root["path"][2:]])
    assert len(_build([slow_turn], _CAMS)[1]) == 1, \
        "ordinary slow-turn siblings keep their existing stitch behavior"
    assert joined([slow_turn, last], last) == {root["id"] + "~reverse", last["id"]}, \
        "slow turn must not stitch the selected reversal leg back to its sibling"
    last = dict(last, direction="southbound", counted=True)
    tracks = [root, first, last]
    assert joined(tracks, last) == {root["id"] + "~reverse", last["id"]}, \
        "the selected piece's direction must admit the final ghost"
    opposed = dict(last, direction="northbound")
    assert joined([root, opposed], opposed) == {opposed["id"]}, \
        "the selected piece's direction must refuse an opposed ghost"

    # Exercise the storage boundary: frozen piece claims must not reassign ghosts on rebuild.
    base = datetime(2026, 8, 19, 10, tzinfo=timezone.utc).timestamp()
    tracks = [dict(t, t0=t["t0"] + base, t1=t["t1"] + base,
                   path=[[p[0] + base, *p[1:]] for p in t["path"]]) for t in tracks]
    expected = build(tracks, _CAMS, timezone.utc)
    assert len(expected) == 2, "each reversal leg has its own counted evidence"
    objects = {"fieldkit-tracklets/G/20260819/a.jsonl": tracks}

    class Bucket:
        def put_object(self, Bucket, Key, Body, **kw):
            objects[Key] = [json.loads(line) for line in Body.decode().splitlines()]

    def docs(cl, bucket, prefix):
        return [d for k, ds in objects.items() if k.startswith(prefix) for d in ds]

    ownership = lambda js: {j["id"]: {(m["id"], m.get("piece")) for m in j["members"]} for j in js}
    with patch.object(selfloop, "r2_jsonl", docs), patch.object(
            selfloop, "bucket_keys", lambda cl, bucket, prefix: [k for k in objects if k.startswith(prefix)]):
        for i, cutoff in enumerate((125, 1000, 1000)):
            with patch.object(selfloop, "horizon", return_value=(
                    {c: base + cutoff + selfloop.FINAL_MARGIN for c in ("cam3", "cam4")}, 0)):
                n, opened, _ = selfloop.journeys_pass(Bucket(), "buck", {("G", "2026-08-19")},
                                                    _CAMS, timezone.utc)
            assert n == (2, 2, 0)[i] and bool(opened) == (i == 0), ("reversal ghost freeze pass", i, n, opened)
            finals = [j for k, ds in objects.items() if "/final/" in k for j in ds]
            assert len(finals) == (0 if i == 0 else 2) and all(
                ownership(expected)[j["id"]] == ownership([j])[j["id"]] for j in finals), \
                "reversal ghosts freeze once with stable piece ownership"
            assert ownership(objects["fieldkit-journeys/G/20260819/journeys.jsonl"]) == ownership(expected), \
                "open and frozen rebuilds retain both reversal legs' piece ownership"



def _selfcheck_ghost_handoff():
    from selfloop import cleared

    sliver = _t("cam3", "sliver", "e-heavy", [(2959.6, _b(.04, .51, .02)),
                                             (2960.6, _b(.02, .51, .02))], hits=4, counted=True)
    truck = _t("cam3", "truck", "e-heavy", [(2991.4, _b(.89, .67, .05)),
                                           (2995.6, _b(.82, .67, .05)),
                                           (3012.8, _b(.02, .51, .02))],
               hits=101, direction="southbound", ghost_of=sliver["id"])
    departure = _t("cam4", "truck", "e-heavy", [(2959.2, _b(.64, .61, .05)),
                                               (2986.8, _b(.15, .67, .05)),
                                               (2989.6, _b(.02, .67, .05))],
                   hits=133, direction="southbound")
    tracks = [sliver, truck, departure]
    js, chains, links = _build(tracks, _CAMS)
    assert len(chains) == 3 and len(js) == 1 and len(links) == 1, ("far-born truck links independently", js)
    assert {m["id"] for m in js[0]["members"]} == {truck["id"], departure["id"]}, js
    assert build([sliver], _CAMS) == [], "counted 4-hit sliver without direction is no passage"
    assert js[0]["link"]["gap_s"] == 1.8, js
    final = cleared(js, js, cutoff=4000)
    assert final == js, ("ghost handoff freeze", final)
    claims = {(m["id"], m.get("piece")) for j in final for m in j["members"]}
    assert build(tracks, _CAMS, frozen=claims) == [], "ghost handoff rebuilt after freeze"

    # Born in place across the zone boundary: the weak prefix still hides an arrival.
    sliver = dict(sliver, path=[[2959.6, *_b(.78, .67, .02)], [2960.6, *_b(.78, .67, .02)]])
    truck = dict(truck, path=[[2991.4, *_b(.805, .67, .02)], *truck["path"][1:]])
    tracks = [sliver, truck, departure]
    [chain] = _chains([sliver, truck], _CAMS)
    assert chain["arr"] is None and chain["dep"] is None, "born-in-place prefix hides entry"
    assert _chains([truck], _CAMS)[0]["arr"] == 2991.4, "tail has a real arrival"
    js, _, links = _build(tracks, _CAMS)
    assert len(js) == len(links) == 1 and js[0]["link"]["gap_s"] == 1.8, "reachable weak-prefix recovery"
    assert {m["id"] for m in js[0]["members"]} == {t["id"] for t in tracks}
    final = cleared(js, js, cutoff=4000)
    assert final == js, "born-in-place handoff freeze"
    claims = {(m["id"], m.get("piece")) for j in final for m in j["members"]}
    assert build(tracks, _CAMS, frozen=claims) == [], "born-in-place handoff rebuild"

    for guard in ({"hits": MIN_EDGE_HITS}, {"direction": "southbound"}):
        assert not _build([dict(sliver, **guard), truck, departure], _CAMS)[2], (
            "substantive ghost prefix must retain its geometry", guard)
    ordinary = dict(truck, id="obs-cam3-ordinary", ghost_of=None,
                    t0=truck["t0"] + .2, t1=truck["t1"] + .2,
                    path=[[t + .2, x1, y1 + .2, x2, y2 + .2] for t, x1, y1, x2, y2 in truck["path"]])
    _, _, links = _build([*tracks, ordinary], _CAMS)
    assert len(links) == 1 and links[0][1]["members"] == [ordinary], "ordinary handoff keeps priority"
    far = dict(departure, t0=departure["t0"] - 100, t1=departure["t1"] - 100,
               path=[[p[0] - 100, *p[1:]] for p in departure["path"]])
    _, chains, links = _build([sliver, truck, far], _CAMS)
    assert not links, "recovered arrival still obeys timing window"
    assert next(c for c in chains if c["camera"] == "cam3") == _chains([sliver, truck], _CAMS)[0], (
        "unmatched ghost retains original evidence")

    later_dep = _t("cam4", "later", "e-heavy", [(2987, _b(.5, .4, .02)),
                    (2990, _b(.12, .8, .02)), (2990.5, _b(.02, .8, .02))],
                    hits=30, counted=True, direction="southbound")
    later_arr = _t("cam3", "later", "e-heavy", [(2990.8, _b(.95, .85, .02)),
                    (2991.8, _b(.7, .85, .02)), (2993, _b(.4, .8, .02))],
                    hits=30, counted=True, direction="southbound")
    _, _, links = _build([*tracks, later_dep, later_arr], _CAMS)
    assert len(links) == 1 and links[0][0]["members"] == [later_dep], "ghost recovery refuses heavy crossing"


def _selfcheck_ghost_decoy():
    from selfloop import cleared

    code = lambda x, y: {"app": base64.b64encode(struct.pack("<32e", x, y, *[0.] * 30)).decode(), "app_v": "m1"}
    a = _north("A", 36100, cls="b-minibuses", k3={"hits": 8, "counted": True, **code(1, 0)})[0]
    b = _north("B", 36095.5, cls="c-bus", k3={"hits": 8, "counted": True, **code(0, 1)})[0]
    r = dict(_north("r", 36100, cls="b-light", gap=.5, k4={"hits": 8, **code(.4, .9165)})[1], direction=None)
    late = _t("cam4", "L", "b-minibuses", [(36108, _b(.35, .45, .05)), (36110, _b(.55, .25, .05))],
              hits=8, counted=True, direction="northbound", **code(.8, -.6))
    sliver = _t("cam3", "sliver", "e-heavy", [(2959.6, _b(.04, .51, .02)), (2960.6, _b(.02, .51, .02))],
                hits=4, counted=True)
    truck = _t("cam3", "truck", "e-heavy", [(2991.4, _b(.89, .67, .05)),
               (2995.6, _b(.82, .67, .05)), (3012.8, _b(.02, .51, .02))],
               hits=101, direction="southbound", ghost_of=sliver["id"])
    dep = _t("cam4", "truck", "e-heavy", [(2959.2, _b(.64, .61, .05)),
             (2986.8, _b(.15, .67, .05)), (2989.6, _b(.02, .67, .05))], hits=133, direction="southbound")
    cams = [dict(c, lone_edge=False) for c in _CAMS]
    unrelated = [a, b, r, late]
    expected = build(unrelated, cams)
    tracks = [sliver, truck, dep, *unrelated]
    js = build(tracks, cams)
    assert [j for j in js if not any(m["id"] == truck["id"] for m in j["members"])] == expected, "distant ghost must not relink released decoy"
    final = cleared(js, js, cutoff=4000)
    assert len(final) == 1 and truck["id"] in {m["id"] for m in final[0]["members"]}, "only recovered truck freezes"
    claims = {(m["id"], m.get("piece")) for j in final for m in j["members"]}
    assert build(tracks, cams, frozen=claims) == [j for j in js if j not in final], "ghost recovery partial freeze stable"


def _selfcheck_ghost():
    a = _t("cam3", 1, "e-heavy", [(100, _b(.5, .4)), (102, _b(.6, .5))], hits=8, direction="northbound")
    b = _t("cam3", 2, "e-heavy", [(140, _b(.6, .5)), (142, _b(.5, .4))], hits=8, ghost_of=a["id"])
    for direction, n in (("southbound", 2), ("northbound", 1), (None, 1)):
        assert len(_chains([a, dict(b, direction=direction)], _CAMS)) == n, ("ghost direction", direction)
    assert len(_chains([dict(a, direction=None), dict(b, direction="southbound")], _CAMS)) == 1

    for short in ("original", "ghost"):
        aa = dict(a, hits=3) if short == "original" else a
        bb = dict(b, hits=3, direction="southbound") if short == "ghost" else dict(b, direction="southbound")
        assert len(_chains([aa, bb], _CAMS)) == 1, ("short ghost direction", short)


def _selfcheck_chain_guards(case):
    if case in ("end_escape", "works", "motion", "interior_escape"):
        a = _t("cam3", 91, "b-minibuses", [(100, _b(.33, .66, .04)), (101, _b(.46, .70, .04)),
                                           (104.4, _b(.90, .84, .04))], hits=11, counted=True, direction="northbound")
        b = _t("cam3", 92, "b-minibuses", [(103.2, _b(.84, .79, .04)), (104.6, _b(.70, .40, .04))], hits=6)
        arrival = _north(93, 104.4, gap=0, cls="b-minibuses", k4={"hits": 11})[1]
        if case == "end_escape":
            js, chains, links = _build([a, b, arrival], _CAMS)
            assert len(js) == 1 and len(chains) == 3 and len(links) == 1, (case, len(js), len(chains), len(links))
            assert not any(c.get("shadow") for c in chains), case
        elif case == "works":
            # Two non-overlapping vehicles, 1.2 parent widths apart; B counts through works rescue.
            a = _t("cam3", 91, "b-minibuses", [(100, _b(.65, .8, .04)), (104, _b(.85, .8, .04))],
                   hits=20, counted=True, direction="northbound")
            b = _t("cam3", 92, "b-minibuses", [(102, _b(.846, .8, .04)), (103, _b(.896, .8, .04))], hits=20)
            assert all(iou(_b(.65 + .05 * (p[0] - 100), .8, .04), p[1:]) == 0 for p in b["path"])
            # The second window starts after A's birth: only B qualifies for works rescue.
            for plant in ([(90, 120, WORKS_PLANT_LONG)], [(1001.5, 1020, WORKS_PLANT_LONG)]):
                js, chains, _ = _build([a, b], _CAMS, plant=plant)
                assert len(js) == 2 and len(chains) == 2, (case, plant, len(js), len(chains))
                assert not any(c.get("shadow") for c in chains), (case, plant)
                _, thin, _ = _build([a, dict(b, hits=6)], _CAMS, plant=plant)
                assert not any(c.get("shadow") for c in thin), (case, "works context below rescue hits")
        elif case == "interior_escape":
            # Birth and end qualify; the intervening sample belongs to a different path.
            a = _t("cam3", 91, "b-minibuses", [(100, _b(.5, .4, .04)), (104, _b(.7, .4, .04))],
                   hits=11, counted=True, direction="northbound")
            b = _t("cam3", 92, "b-minibuses", [(102, _b(.69, .4, .04)), (102.5, _b(.9, .4, .04)),
                                             (103, _b(.70, .4, .04))], hits=6)
            assert not _opposed(a, b)
            _, chains, _ = _build([a, b], _CAMS)
            assert len(chains) == 2 and not any(c.get("shadow") for c in chains), case
        else:
            # Every sample stays near A, but B moves the other way and is no ID switch.
            a = _t("cam3", 91, "b-minibuses", [(100, _b(.5, .4, .04)), (104, _b(.6, .4, .04))],
                   hits=11, counted=True, direction="northbound")
            b = _t("cam3", 92, "b-minibuses", [(102, _b(.64, .4, .04)), (103, _b(.62, .4, .04))], hits=6)
            assert _opposed(a, b)
            _, chains, _ = _build([a, b], _CAMS)
            assert len(chains) == 2 and not any(c.get("shadow") for c in chains), case
    elif case == "weak_ghost":
        a = _t("cam3", 91, "c-small", [(100, _b(.5, .4)), (102, _b(.5, .4))],
               hits=8, counted=True, direction="northbound")
        g = _t("cam3", 92, "c-small", [(101, _b(.5, .4)), (101.5, _b(.5, .4))],
               hits=3, ghost_of=a["id"], direction="southbound")
        c = _t("cam3", 93, "c-small", [(102.4, _b(.5, .4)), (103.4, _b(.5, .4))],
               hits=8, counted=True, direction="northbound")
        js = build([a, g, c], _CAMS)
        assert len(js) == 1 and len(js[0]["members"]) == 3, (case, len(js))
        # 12 Oct #5c (10 hits) and #5e (5 hits) both have reliable directions.
        assert len(_chains([dict(a, hits=10), dict(g, hits=5)], _CAMS)) == 2, "solid ghost remains opposed"
    else:
        assert case == "refused_slot"
        ts = [_t("cam3", i, "c-small", [(start, _b(.5, .4)), (end, _b(.5, .4))],
                 hits=5, counted=True, direction=direction)
              for i, start, end, direction in ((91, 100, 101, "southbound"), (92, 101.2, 102, None),
                                              (93, 102.2, 103, "northbound"), (94, 102.4, 103.4, "southbound"))]
        chains = _chains(ts, _CAMS)
        groups = [{m["id"] for m in c["members"]} for c in chains]
        assert {ts[i]["id"] for i in (0, 1, 3)} in groups and {ts[2]["id"]} in groups, (case, groups)


def _selfcheck_shadows(case):
    code = lambda x, y: base64.b64encode(struct.pack("<32e", x, y, *[0.0] * 30)).decode()
    if case == "shared_ghost":
        g = _t("cam3", 90, "c-small", [(90, _b(.1, .2)), (91, _b(.1, .2))], hits=3)
        a = _t("cam3", 91, "c-small", [(100, _b(.5, .4)), (102, _b(.5, .4))],
               hits=8, counted=True, direction="northbound", ghost_of=g["id"])
        b = _t("cam3", 92, "c-small", [(95, _b(.1, .2)), (96, _b(.1, .2))],
               hits=8, direction="southbound", ghost_of=g["id"])
        c = _t("cam3", 93, "c-small", [(102.4, _b(.5, .4)), (103.4, _b(.5, .4))],
               hits=8, counted=True, direction="northbound")
        js, chains, links = _build([g, a, b, c], _CAMS)
        assert len(js) == 1 and not links, (case, len(js), len(links))
        assert all(len({m["direction"] for m in ch["members"]
                        if m.get("direction") and m["hits"] >= MIN_EDGE_HITS}) <= 1 for ch in chains), case
        assert {m["id"] for m in js[0]["members"]} == {a["id"], c["id"]}, case
        assert any({m["id"] for m in ch["members"]} == {g["id"], b["id"]} for ch in chains), case
        return
    if case == "endpoint":
        a = _t("cam3", 91, "b-minibuses", [(100, _b(.84, .80, .04)), (104, _b(.88, .80, .04))],
               hits=11, counted=True, direction="northbound")
        b = _t("cam3", 92, "b-minibuses", [(103, _b(.85, .80, .04)), (104.2, _b(.86, .80, .04))], hits=6)
    else:
        assert case == "appearance"
        a = _t("cam3", 91, "b-minibuses", [(100, _b(.65, .8, .04)), (104, _b(.85, .8, .04))],
               hits=11, counted=True, direction="northbound", app=code(1, 0), app_v="m1")
        b = _t("cam3", 92, "b-minibuses", [(102, _b(.846, .8, .04)), (103, _b(.896, .8, .04))],
               hits=20, app=code(0, 1), app_v="m1")
    arrival = _north(93, 104, gap=.4, cls="b-minibuses",
                     k4={"hits": 11, "app": code(1, 0), "app_v": "m1"})[1]
    # Noon applies the appearance veto to same-class vehicles too.
    shift = 12 * 3600
    ts = [dict(t, t0=t["t0"] + shift, t1=t["t1"] + shift,
               path=[[p[0] + shift, *p[1:]] for p in t["path"]]) for t in (a, b, arrival)]
    js, chains, links = _build(ts, _CAMS, timezone.utc)
    assert len(js) == 1 and len(links) == 1, (case, len(js), len(links))
    assert {m["id"] for m in js[0]["members"]} == {a["id"], arrival["id"]}, (case, js)
    assert len(chains) == 3, (case, "parent evidence changed")
    parent = next(c for c in chains if c["members"][0]["id"] == a["id"])
    assert parent == _chains(parent["members"], _CAMS)[0], (case, "parent evidence changed")
    assert next(c for c in chains if c["members"][0]["id"] == b["id"]).get("shadow"), case


def _selfcheck_shadow_rejections(case):
    code = lambda x, y: base64.b64encode(struct.pack("<32e", x, y, *[0.0] * 30)).decode()
    a = _t("cam3", 91, "b-minibuses", [(100, _b(.65, .8, .04)), (104, _b(.85, .8, .04))],
           hits=11, counted=True, direction="northbound", app=code(1, 0), app_v="m1")
    start = 99.6 if case == "start" else 102
    end = 104.6 if case == "end" else 103
    b = _t("cam3", 92, "a-small" if case == "class" else "b-minibuses",
           [(t, _b(.65 + .05 * (min(t, 104) - 100) + .096, .8, .04)) for t in (start, end)],
           hits=20, counted=case == "counted", app=code(0, 1), app_v="m1")
    if case == "too_far":
        b["path"] = [[t, *_b(.65 + .05 * (t - 100) + .104, .8, .04)] for t in (start, end)]
    if case == "weak_parent":
        a["hits"] = MIN_EDGE_HITS - 1
    elif case == "directionless_parent":
        a["direction"] = None
    assert all(iou(_b(.65 + .05 * (min(p[0], 104) - 100), .8, .04), p[1:]) == 0 for p in b["path"]), case
    chains = _chains([a, b], _CAMS)
    assert len(chains) == 2, (case, "fixture merged")
    _shadows(chains, lambda c: False)
    shadow = next(c for c in chains if c["members"][0]["id"] == b["id"])
    assert bool(shadow.get("shadow")) == (case == "eligible"), (case, "shadow eligibility")
    if case == "start":
        arrival = _north(93, 104, gap=.4, cls="b-minibuses",
                         k4={"hits": 11, "app": code(0, 1), "app_v": "m1"})[1]
        shift = 12 * 3600
        ts = [dict(t, t0=t["t0"] + shift, t1=t["t1"] + shift,
                   path=[[p[0] + shift, *p[1:]] for p in t["path"]]) for t in (a, b, arrival)]
        cameras = [dict(c, lone_edge=False) for c in _CAMS]
        js, chains, links = _build(ts, cameras, timezone.utc)
        assert len(js) == 2 and len(chains) == 3 and len(links) == 1, (case, len(js), len(chains), len(links))
        assert {m["id"] for c in links[0] for m in c["members"]} == {b["id"], arrival["id"]}, case


def _selfcheck_shadow_competition(case):
    if case == "heavy":
        # Exact five-tracklet chain_gate_over fixture: shadow removal crosses the truck convoy.
        ts = json.loads('''[
        {"id":"obs-cam3-91","camera":"cam3","t0":43300,"t1":43304,"hits":11,"dim":[1920,1080],"class":"e-heavy","votes":{"e-heavy":2},"conf":{"e-heavy":0.8},"counted":true,"ghost_of":null,"direction":"northbound","path":[[43300,0.61,0.76,0.69,0.84],[43304,0.81,0.76,0.89,0.84]],"crops":{"top":"cam3-91-top.jpg","best":"cam3-91-best.jpg"},"plate":null,"app":"8jScOwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA==","app_v":"m1"},
        {"id":"obs-cam3-92","camera":"cam3","t0":43302,"t1":43303,"hits":20,"dim":[1920,1080],"class":"e-heavy","votes":{"e-heavy":2},"conf":{"e-heavy":0.8},"counted":false,"ghost_of":null,"direction":null,"path":[[43302,0.806,0.76,0.886,0.84],[43303,0.856,0.76,0.936,0.84]],"crops":{"top":"cam3-92-top.jpg","best":"cam3-92-best.jpg"},"plate":null,"app":"yjDpOwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA==","app_v":"m1"},
        {"id":"obs-cam3-0","camera":"cam3","t0":43302.243,"t1":43304.243,"hits":8,"dim":[1920,1080],"class":"e-heavy","votes":{"e-heavy":3},"conf":{"e-heavy":0.8},"counted":false,"ghost_of":null,"direction":"northbound","path":[[43302.243,0.4,0.3,0.6,0.5],[43303.243,0.6,0.5,0.8,0.7],[43304.243,0.8,0.7,1.0,0.9]],"crops":{"top":"cam3-0-top.jpg","best":"cam3-0-best.jpg"},"plate":null,"app":"IDDvOwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA==","app_v":"m1"},
        {"id":"obs-cam4-1","camera":"cam4","t0":43313.13,"t1":43315.13,"hits":8,"dim":[1920,1080],"class":"e-heavy","votes":{"e-heavy":3},"conf":{"e-heavy":0.8},"counted":true,"ghost_of":null,"direction":"northbound","path":[[43313.13,0.0,0.6,0.2,0.8],[43314.13,0.2,0.4,0.4,0.6],[43315.13,0.35,0.25,0.55,0.45]],"crops":{"top":"cam4-1-top.jpg","best":"cam4-1-best.jpg"},"plate":null,"app":"+TrZNwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA==","app_v":"m1"},
        {"id":"obs-cam4-2","camera":"cam4","t0":43303.603,"t1":43305.603,"hits":8,"dim":[1920,1080],"class":"e-heavy","votes":{"e-heavy":3},"conf":{"e-heavy":0.8},"counted":false,"ghost_of":null,"direction":"northbound","path":[[43303.603,0.0,0.6,0.2,0.8],[43304.603,0.2,0.4,0.4,0.6],[43305.603,0.35,0.25,0.55,0.45]],"crops":{"top":"cam4-2-top.jpg","best":"cam4-2-best.jpg"},"plate":null,"app":"MrkVOgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA==","app_v":"m1"}
        ]''')
        cameras = [dict(c, lone_edge=False) if c["name"] == "cam3" else c for c in _CAMS]
        js, chains, links = _build(ts, cameras, timezone.utc)
        assert (len(js), len(links)) == (2, 2), (case, len(js), len(links))
        return
    code = lambda x, y: base64.b64encode(struct.pack("<32e", x, y, *[0.0] * 30)).decode()
    a = _t("cam3", 91, "b-minibuses", [(43300, _b(.65, .8, .04)), (43304, _b(.85, .8, .04))],
           hits=11, counted=True, direction="northbound", app=code(1, 0), app_v="m1")
    b = _t("cam3", 92, "b-minibuses", [(43302, _b(.846, .8, .04)), (43303, _b(.896, .8, .04))],
           hits=20, app=code(0, 1), app_v="m1")
    arrival = _north(93, 43304, cls="b-minibuses",
                     k4={"hits": 11, "app": code(0, 1), "app_v": "m1"})[1]
    cameras = [dict(c, lone_edge=False) for c in _CAMS]
    js, chains, links = _build([a, b, arrival], cameras, timezone.utc)
    assert (len(js), len(links)) == (2, 1), (case, len(js), len(links))
    assert {m["id"] for c in links[0] for m in c["members"]} == {b["id"], arrival["id"]}, case
    assert next(c for c in chains if c["members"][0]["id"] == b["id"]).get("shadow"), case


def _selfcheck_ghost_history():
    from itertools import permutations

    root = _t("cam3", "history-root", "e-heavy", [(100, _b(.2, .5, .02)), (110, _b(.1, .5, .02))],
              hits=30, counted=True, direction="southbound")
    opposed = _t("cam3", "history-opposed", "e-heavy", [(120, _b(.1, .5, .02)), (130, _b(.9, .5, .02))],
                 hits=30, ghost_of=root["id"], direction="northbound")
    again = _t("cam3", "history-again", "e-heavy", [(140, _b(.1, .5, .02)), (150, _b(.05, .5, .02))],
               hits=30, ghost_of=root["id"], direction="southbound")
    cams = [dict(c, lone_edge=False) for c in _CAMS]
    expected = build([root, again], cams)
    assert len(expected) == 1 and expected[0]["evidence"] == "line"
    for shuffled in permutations([root, opposed, again]):
        assert build(shuffled, cams) == expected, "opposed ghost must not advance continuation history"


def _selfcheck_ghost_endpoint():
    root = _t("cam3", "endpoint-root", "e-heavy", [(100, _b(.2, .5, .02)), (110, _b(.1, .5, .02))],
              hits=30, counted=True, direction="southbound")
    continued = _t("cam3", "endpoint-continued", "e-heavy", [(120, _b(.1, .5, .02)), (130, _b(.9, .5, .02))],
                   hits=30, ghost_of=root["id"], direction="southbound")
    moved = _t("cam3", "endpoint-moved", "e-heavy", [(140, _b(.9, .5, .02)), (150, _b(.85, .5, .02))],
               hits=30, ghost_of=root["id"], direction="southbound")
    cams = [dict(c, lone_edge=False) for c in _CAMS]
    tracks = [root, continued, moved]
    assert _ghosts(tracks)[1] == [(root["id"], continued["id"]), (continued["id"], moved["id"])], \
        "ghost join must name the endpoint-bearing member"
    [chain] = _build(tracks, cams, frozen={(root["id"], None)})[1]
    assert {m["id"] for m in chain["members"]} == {continued["id"], moved["id"]}, \
        "open ghosts must stay joined after the root freezes"


def _selfcheck_ghost_permutations():
    from itertools import permutations

    g = _t("cam3", 90, "c-small", [(90, _b(.1, .2)), (91, _b(.1, .2))], hits=3)
    a = _t("cam3", 91, "c-small", [(100, _b(.5, .4)), (102, _b(.5, .4))],
           hits=8, counted=True, direction="northbound", ghost_of=g["id"])
    b = _t("cam3", 92, "c-small", [(95, _b(.1, .2)), (96, _b(.1, .2))],
           hits=8, counted=True, direction="southbound", ghost_of=g["id"])
    ts = [g, b, a]
    expected = build(ts, _CAMS)
    assert len(expected) == 2
    assert {m["id"] for j in expected if j["direction"] == "southbound" for m in j["members"]} == {g["id"], b["id"]}
    for shuffled in permutations(ts):
        actual = build(shuffled, _CAMS)
        assert actual == expected, ("ghost permutation", [t["id"] for t in shuffled], actual, expected)


def _selfcheck_shadow_links():
    code = base64.b64encode(struct.pack("<32e", 1, *[0.0] * 31)).decode()
    for mode in ("ordinary", "late", "early", "truck"):
        ts = _north(91, 104, cls="e-heavy", k3={"hits": 8, "app": code, "app_v": "m1"},
                    k4={"hits": 8, "app": code, "app_v": "m1"}, gap=10 if mode == "truck" else .4)
        chains = _chains(ts, _CAMS)
        d, r = chains
        if mode == "late":
            r["late"], r["arr"] = r["arr"], None
        elif mode == "early":
            d["early"], d["dep"] = d["dep"], None
        assert _links(chains, _CAMS) == [(d, r)], (mode, "fixture")
        for c in chains:
            parent = dict(c, members=[dict(c["members"][0], id="absent-parent")])
            assert _links(chains, _CAMS, shadows={id(c): parent}) == [(d, r)], (mode, c["camera"], "uncontested shadow")
        parent = dict(d, members=[dict(d["members"][0], id="parent")])
        expected = _links([d, parent, r], _CAMS)
        assert _links([d, parent, r], _CAMS, shadows={id(d): parent}) == expected, (mode, "shadow cannot filter candidates")


def _selfcheck_shadow_swap():
    code = lambda x, y: base64.b64encode(struct.pack("<32e", x, y, *[0.0] * 30)).decode()
    a = _t("cam3", 91, "b-minibuses", [(100, _b(.33, .66, .04)), (101, _b(.46, .70, .04)),
                                       (104.4, _b(.90, .84, .04))], hits=11, counted=True,
           direction="northbound", app=code(1, 0), app_v="m1")
    b = _t("cam3", 92, "b-minibuses", [(103.2, _b(.84, .79, .04)), (104.2, _b(.97, .80, .04))],
           hits=6, app=code(.873548, .486738), app_v="m1")
    arrival = _north(93, 104.4, gap=0, cls="b-minibuses",
                     k4={"hits": 11, "app": code(.52, .854166), "app_v": "m1"})[1]
    for guard in (None, "direction", "counted", "weaker_shadow"):
        shadow = dict(b, app=code(.995333, .096500)) if guard == "weaker_shadow" else b
        car = _north(94, 104.4, gap=2, cls="a-small",
                     k4={"hits": 11, "app": code(.67, .742361), "app_v": "m1"})[1]
        car["direction"] = None
        if guard in ("direction", "counted"):
            car[guard] = "northbound" if guard == "direction" else True
        # Real replay: parent/car .67, shadow/arrival .87, parent/arrival .52.
        assert abs(_similar(_app([a]), _app([car])) - .67) < .001
        assert abs(_similar(_app([shadow]), _app([arrival])) - (.60 if guard == "weaker_shadow" else .87)) < .001
        assert abs(_similar(_app([a]), _app([arrival])) - .52) < .001
        a["speed_kph"], arrival["speed_kph"] = 30, 50
        shadow = dict(shadow, speed_kph=170, conf={"b-minibuses": .99}, attrs={"colour": "shadow"})
        events = {shadow["id"]: {"class": "b-minibuses", "hits": 100, "attrs": {"colour": "shadow-event"}}}
        js, chains, links = _build([a, shadow, arrival, car], _CAMS, events=events)
        actual = [{m["id"] for c in pair for m in c["members"]} for pair in links]
        if guard:
            assert actual == [{b["id"], arrival["id"]}, {a["id"], car["id"]}], (guard, actual)
        else:
            assert actual == [{a["id"], arrival["id"]}], ("shadow decoy swap", actual)
            expected = _doc(list(links[0]), links[0], {c["name"]: c for c in _CAMS}, None, events)[1]
            assert {k: v for k, v in js[0].items() if k not in ("id", "members")} == {
                k: v for k, v in expected.items() if k not in ("id", "members")}, ("shadow changed evidence", js)
            assert js[0]["id"] != expected["id"], "absorbed shadow must change journey id"
            frozen_ids = {(m["id"], m.get("piece")) for j in js for m in j["members"]}
            rebuilt = build([a, shadow, arrival, car], _CAMS, frozen=frozen_ids)
            assert rebuilt == [], ("shadow swap after freeze", rebuilt)
            assert len(js) == 1 and {m["id"] for m in js[0]["members"]} == actual[0] | {b["id"]}, js

            from selfloop import cleared
            extra = dict(shadow, id="obs-cam3-extra", app=code(.98, -.2),
                         path=[[t, x1 - .08, y1, x2 - .08, y2] for t, x1, y1, x2, y2 in shadow["path"]])
            tracks = [a, shadow, extra, arrival, car]
            events[extra["id"]] = events[shadow["id"]]
            assert abs(_similar(_app([extra]), _app([car])) - .508) < .001
            js, _, links = _build(tracks, _CAMS, events=events)
            assert len(js) == 1 and links[0][0]["members"] == [a], ("five-tracklet swap", js)
            final = cleared(js, js, cutoff=1000)
            assert final == js, ("five-tracklet freeze", final)
            claims = {(m["id"], m.get("piece")) for j in final for m in j["members"]}
            rebuilt = build(tracks, _CAMS, events=events, frozen=claims)
            assert rebuilt == [], ("second shadow re-paired after freeze", rebuilt)
            assert {m["id"] for m in js[0]["members"]} == {a["id"], shadow["id"], extra["id"], arrival["id"]}, js
            assert {k: v for k, v in js[0].items() if k not in ("id", "members")} == {
                k: v for k, v in expected.items() if k not in ("id", "members")}, ("second shadow changed evidence", js)


def _selfcheck_swap_rejections(case):
    # Isolate the final swap from geometry: each shadow already has a linked arrival.
    def chain(cam, tid, cls, dep, arr, app, direction=None):
        dep = dep + 43200 if dep is not None else None
        arr = arr + 43200 if arr is not None else None
        return {"camera": cam, "class": cls, "dep": dep, "arr": arr,
                "z": (dep if dep is not None else arr,) * 2, "app": (app, "m1"),
                "members": [{"id": tid, "hits": 8, "counted": False, "direction": direction}]}

    cls = "e-heavy" if case == "heavy_crossing" else "b-minibuses"
    d = chain("cam3", "parent", cls, 104, None, (1, 0, 0, 0), "northbound")
    s = chain("cam3", "shadow", cls, 103, None, (.6, .8, 0, 0))
    low = case == "minimum_similarity"
    y = .325 if low else .875
    x = .21 if low else .67
    L = chain("cam4", "arrival", "c-bus" if case == "arrival_class" else cls,
              None, 103.5, (.3, y, (1 - .3 ** 2 - y ** 2) ** .5, 0), "northbound")
    r = chain("cam4", "decoy", "b-minibuses" if cls in HEAVY else "a-small",
              None, 106, (x, 0, (1 - x ** 2) ** .5, 0))
    if case == "same_class":
        r["class"] = cls
    chains = [d, s, L, r]
    if case == "heavy_crossing":
        dd = chain("cam3", "convoy-departure", cls, 103.5, None, (0, 0, 0, 1), "northbound")
        aa = chain("cam4", "convoy-arrival", cls, None, 106, (0, 0, 0, 1), "northbound")
        chains.extend([dd, aa])
    expected = _links(chains, _CAMS)
    assert (s, L) in expected and (d, r) in expected, (case, "fixture", expected)
    assert _links(chains, _CAMS, shadows={id(s): d}) == expected, (case, "swap must reject")


def _selfcheck_contradictory_swap():
    code = lambda x, y: base64.b64encode(struct.pack("<32e", x, y, *[0.0] * 30)).decode()
    a = _t("cam3", 91, "b-minibuses", [(43300, _b(.65, .8, .04)), (43304, _b(.85, .8, .04))],
           hits=11, counted=True, direction="northbound", app=code(1, 0), app_v="m1")
    b = _t("cam3", 92, "b-minibuses", [(43302, _b(.846, .8, .04)), (43303, _b(.896, .8, .04))],
           hits=20, app=code(0, 1), app_v="m1")
    arrival = _north(93, 43304, cls="b-minibuses",
                     k4={"hits": 11, "app": code(0, 1), "app_v": "m1"})[1]
    car = _north(94, 43304, gap=2, cls="a-small",
                 k4={"hits": 11, "app": code(.67, .742361), "app_v": "m1"})[1]
    car["direction"] = None
    js, chains, links = _build([a, b, arrival, car], _CAMS, timezone.utc)
    assert (len(js), len(links)) == (2, 2), ("contradictory swap", len(js), len(links))
    assert [{m["id"] for c in pair for m in c["members"]} for pair in links] == [
        {b["id"], arrival["id"]}, {a["id"], car["id"]}], "contradictory swap membership"


def _selfcheck_twin_permutations():
    from itertools import permutations

    ts = [_t("cam3", i, "c-small", [(100, _b(.5, .4)), (102, _b(.5, .4))],
             hits=8, counted=True, direction=d)
          for i, d in ((91, "northbound"), (92, None), (93, "southbound"))]
    expected = build(ts, _CAMS)
    assert len(expected) == 2
    assert {ts[0]["id"], ts[1]["id"]} in [{m["id"] for m in j["members"]} for j in expected]
    for shuffled in permutations(ts):
        assert build(shuffled, _CAMS) == expected, ("equal-time twin permutation", [t["id"] for t in shuffled])


def _selfcheck_early(case):
    from unittest.mock import patch
    code = lambda x, y: base64.b64encode(struct.pack("<32e", x, y, *[0.0] * 30)).decode()
    a = _t("cam3", 1, "a-small", [(100, _b(.1, .54)), (101, _b(.4, .65)), (102, _b(.7, .78))],
           counted=True, hits=32, direction="northbound", app=code(1, 0), app_v="m1")
    b = _north(2, 103.4, cls="a-small", k4={"app": code(.9, .43589), "app_v": "m1", "counted": True, "hits": 8})[1]
    [ch] = _chains([a], _CAMS)
    assert ch.get("early") == 102 and ch["dep"] is None and ch["arr"] is None, (case, "early fixture", ch)
    if case == "look":
        b.update(app=code(.3, .95394))
        assert len(build([a, b], _CAMS)) == 2, "early low cosine"
    elif case == "away":
        away = dict(a, path=[[100 + i, *p[1:]] for i, p in enumerate(reversed(a["path"]))])
        [ch] = _chains([away], _CAMS)
        assert ch.get("early") is None and ch.get("late") == 100, "moving away is not early"
    elif case == "priority":
        ordinary = _north(2, 103.4, cls="a-small")[0]
        js, _, ll = _build([a, ordinary, b], _CAMS)
        assert len(js) == 2 and len(ll) == 1 and ll[0][0]["members"][0]["id"] == ordinary["id"], "ordinary departure wins"
    elif case == "late":
        late = dict(b, path=[[103.8, *_b(.4, .4)], [104.8, *_b(.6, .3)]], t0=103.8, t1=104.8)
        # Preserve the ordinary linker's prohibition on two guessed edges.
        with patch(__name__ + ".PAIR_COS", 9), patch(__name__ + ".PAIR_HEAVY_COS", 9):
            assert len(build([a, late], _CAMS)) == 2, "two guessed edges never link"
        assert len(build([a, late], _CAMS)) == 1, "leftover pairing rescues two guessed edges"
        assert len(build([a, dict(late, app=code(.8, .6))], _CAMS)) == 2, "guessed-edge leftovers need pair cosine"
    elif case in ("dep_hits", "arr_hits"):
        (a if case == "dep_hits" else b)["hits"] = MIN_EDGE_HITS - 1
        # Preserve the ordinary early linker's minimum-hit floor.
        with patch(__name__ + ".PAIR_COS", 9), patch(__name__ + ".PAIR_HEAVY_COS", 9):
            assert not _build([a, b], _CAMS)[2], ("early hit floor", case)
        assert len(build([a, b], _CAMS)) == 1, ("leftover pairing rescues low-hit edge", case)
        assert len(build([a, dict(b, app=code(.8, .6))], _CAMS)) == 2, ("low-hit leftovers need pair cosine", case)
    elif case == "last_end":
        long = dict(a, t1=106, path=[a["path"][0], [106, *a["path"][-1][1:]]])
        short = _t("cam3", 3, "a-small", [(101, _b(.5, .65)), (102, _b(.4, .55))],
                   hits=8, direction="northbound", ghost_of=a["id"])
        [ch] = _chains([long, short], _CAMS)
        assert ch.get("early") == 106, "early uses latest end, not latest start"
        arrival = dict(b, t0=b["t0"] + 4, t1=b["t1"] + 4, path=[[p[0] + 4, *p[1:]] for p in b["path"]])
        [j] = build([long, short, arrival], _CAMS)
        assert j["link"]["gap_s"] == 1.8, "early time uses latest end"
        away = dict(long, path=[[100, *long["path"][-1][1:]], [106, *long["path"][0][1:]]])
        toward = dict(short, path=[[101, *short["path"][-1][1:]], [102, *short["path"][0][1:]]])
        [ch] = _chains([away, toward], _CAMS)
        assert ch.get("early") is None, "latest-ending member must approach"
    elif case == "truck_convoy":
        a = dict(a, **{"class": "e-heavy", "votes": {"e-heavy": 32}})
        b = dict(b, **{"class": "e-heavy", "votes": {"e-heavy": 8}})
        dep, arr = _north(4, 108, gap=10, cls="e-heavy",
                          k3={"counted": True, "hits": 8, "app": code(1, 0), "app_v": "m1"},
                          k4={"counted": True, "hits": 8, "app": code(1, 0), "app_v": "m1"})
        js, _, ll = _build([a, b, dep, arr], _CAMS)
        assert len(js) == 2 and len(ll) == 2, "truck convoy reads accepted early time"
    elif case == "heavy":
        js = build([dict(a, **{"class": "e-heavy", "votes": {"e-heavy": 32}}),
                    dict(b, **{"class": "e-heavy", "votes": {"e-heavy": 8}})], _CAMS)
        assert len(js) == 1 and js[0]["link"]["gap_s"] == 1.8, "early truck document"
    else:
        [j] = build([a, b], _CAMS, timezone.utc)
        assert j["link"] == {"from": "cam3", "to": "cam4", "gap_s": 1.8}, j
        assert j["ts"] == "1970-01-01T00:01:42+00:00", j



def _selfcheck_reversal(case):
    a = _t("cam3", 48, "e-heavy", [(100, _b(.3, .65)), (102, _b(.6, .7)),
            (105, _b(.9, .8)), (107, _b(.65, .7, .15)), (110, _b(.1, .55))],
            counted=True, hits=100, direction="southbound", app="look", app_v="m1")
    if case in ("small", "straight", "short"):
        xs = {"small": [.3, .35, .4, .35, .3], "straight": [.1, .3, .5, .7, .9],
              "short": [.3, .9, .1]}[case]
        a["path"] = [[100 + i, *_b(x, .7)] for i, x in enumerate(xs)]
        assert _reversal(a) is None, case
        assert len(_build([a], _CAMS)[1]) == 1, case
        return
    b = _north(30, 105, gap=4.4, cls="b-light", k4={"hits": 8, "counted": True})[1]
    c = _t("cam4", 46, "e-heavy", [(102, _b(.45, .35)), (104, _b(.1, .7))],
           hits=8, counted=True, direction="southbound")
    js, ch, ll = _build([a, b, c] if case in ("link", "freeze") else [a], _CAMS)
    pieces = sorted((t for chain in ch for t in chain["members"] if t["camera"] == "cam3"), key=lambda t: t["t0"])
    assert len(pieces) == 2, (case, "two pieces", len(pieces))
    first, second = pieces
    assert first["path"][-1] == second["path"][0] == a["path"][2]
    assert (first["direction"], second["direction"]) == ("northbound", "southbound")
    assert not first["counted"] and second["counted"] and sum(t["hits"] for t in pieces) == 100
    assert first.get("mixed") and not second.get("mixed") and _app([first]) is None
    assert second["app"] == a["app"] and second["crops"] == {"best": a["crops"]["best"]}
    assert a["t0"] == 100 and len(a["path"]) == 5 and not a.get("mixed"), "input immutable"
    if case == "link":
        assert len(ll) == len(js) == 2, (len(ll), len(js))
        assert any({a["id"], b["id"]} <= {m["id"] for m in j["members"]} for j in js)
        assert any({second["id"], c["id"]} <= {m.get("piece", m["id"]) for m in j["members"]} for j in js)
        assert [j["id"] for j in js] == [j["id"] for j in build([c, b, a], _CAMS)]
    elif case == "freeze":
        from selfloop import finalize, FINAL_MARGIN
        legs = [m for j in js for m in j["members"] if m["id"] == a["id"]]
        assert {(m["piece"], m["t0"], m["t1"]) for m in legs} == {
            (first["id"], 100, 105), (second["id"], 105, 110)}, "both raw-backed legs retain their own spans"
        assert finalize(js, 110 + FINAL_MARGIN) == [], "open partner holds both pieces"
        frozen = finalize(js, 112 + FINAL_MARGIN)
        assert len(frozen) == 2
        fids = {(m["id"], m.get("piece")) for j in frozen for m in j["members"]}
        assert build([a, b, c], _CAMS, frozen=fids) == []
        assert build([a, b, c], _CAMS, frozen={(a["id"], None), (b["id"], None), (c["id"], None)}) == [], "legacy raw claims exclude the whole tracklet"
    else:
        assert len(js) == 1, "a lone U-turn retains only the original count, including lone_edge cameras"
        assert len(build([a], _CAMS, plant=[(90, 120, 150)])) == 1, "works cannot rescue the other leg"


def _selfcheck_reversal_findings(case):
    a = _t("cam3", 48, "e-heavy", [(100, _b(.3, .65, .2)), (102, _b(.6, .7)),
            (105, _b(.9, .8)), (107, _b(.65, .7)), (110, _b(.1, .55))],
            counted=True, hits=100, direction="southbound",
            crops={"best": "first-best.jpg", "top": "second-top.jpg", "recede": "second-recede.jpg"},
            plate={"crop": "second-plate.jpg", "conf": .9})
    if case == "photos":
        pieces = _split(a, 2, _CAMS[0])
        assert pieces[0]["crops"] == {"best": "first-best.jpg"} and not pieces[0].get("plate"), "ambiguous photographs and plate cleared"
        b = _north(30, 105, gap=4.4, cls="e-heavy", k4={"hits": 8, "crops": {}, "plate": None})[1]
        j = next(j for j in build([a, b], _CAMS) if j["link"])
        assert j["crops"]["best"] == "first-best.jpg" and not j["plates"], j
        assert not any("second-" in crop for crop in j["crops"].values()), j
        return
    if case == "plant":
        a["class"], a["votes"], a["hits"], a["counted"] = "e-plant", {"e-plant": 160}, 160, False
        c = _t("cam4", 90, "e-heavy", [(100, _b(.1, .7)), (102, _b(.4, .4))], hits=25)
        expected = build([a, c], _CAMS, plant=[(100, 110, 160)])
        assert len(expected) == 1 and build([a, c], _CAMS) == expected, "raw plant evidence survives split"
        return
    fragment = _t("cam3", 47, "e-heavy", [(97, _b(.1, .55)), (99, _b(.3, .65, .2))],
                  hits=20, direction="northbound")
    cams = [dict(c, lone_edge=False) for c in _CAMS] if case == "fragment_works" else _CAMS
    plant = [(90, 120, 150)] if case == "fragment_works" else None
    js, chains, links = _build([fragment, a], cams, plant=plant)
    assert not links and any(len(c["members"]) == 2 for c in chains), "fragment genuinely stitched"
    assert len(js) == 1, "stitched uncounted fragment cannot rescue a lone U-turn leg"
    fragment["counted"] = True
    assert len(build([fragment, a], cams, plant=plant)) == 2, "independent count remains evidence"


def _selfcheck_review(case):
    a = _t("cam3", 48, "e-heavy", [(100, _b(.3, .65)), (102, _b(.6, .7)),
            (105, _b(.9, .8)), (107, _b(.65, .7, .15)), (110, _b(.1, .55))],
            counted=True, hits=100, direction="southbound", speed_kph=12.0)
    if case == "frozen_unsplit":
        from unittest.mock import patch
        frozen = {(a["id"], a["id"] + "~reverse")}
        assert len(_build([a], _CAMS, frozen=frozen)[1]) == 1, "unclaimed sibling remains available"
        with patch(__name__ + ".REVERSE", .7):
            assert _reversal(a) is None, "retuning removes the split"
            js, chains, _ = _build([a], _CAMS, frozen=frozen)
            assert not js and not chains, "claimed leg excludes a later unsplit raw tracklet"
    elif case == "speed":
        pieces = _split(a, 2, _CAMS[0])
        assert all(p.get("speed_kph") is None for p in pieces), "neither leg inherits whole-track speed"
        assert build([a], _CAMS)[0]["speed_kph"] is None
    elif case == "events":
        events = {a["id"]: {"class": "e-heavy", "hits": 100, "attrs": {"axles": 7}}}
        [j] = build([a], _CAMS, events=events)
        assert j["members"][0]["piece"] == a["id"] + "~reverse" and j["crops"], "second leg owns the photos"
        assert j["attrs"] == {"axles": 7}, "photo-owning derived leg reads the raw event"


def _selfcheck_late_priority(case):
    # 11 Oct 11:40: a truck cab overlaps a minibus departure before its own late track locks.
    def chain(cam, name, cls, look, hits, dep=None, arr=None, late=None):
        return {"camera": cam, "class": cls, "app": (look, "m1"), "dep": dep, "arr": arr,
                "late": late, "z": (dep - 1, dep) if dep is not None else (arr, arr),
                "members": [{"id": name, "hits": hits}]}

    if case in ("truck_swap_crossing", "weak_truck_swap_crossing"):
        chains = [chain("cam3", "A", "e-heavy", (1, 0, 0), 88, dep=36100),
                  chain("cam3", "B", "e-heavy", (0, 1, 0), 88, dep=36106),
                  chain("cam4", "r", "b-light", (.3, 0, .91 ** .5), 4 if case == "weak_truck_swap_crossing" else 88, arr=36100.5),
                  chain("cam4", "R2", "e-heavy", (0, 1, 0), 88, arr=36101),
                  chain("cam4", "U", "e-heavy", (1, 0, 0), 88, late=36108)]
        links = {(d["members"][0]["id"], r["members"][0]["id"]) for d, r in _links(chains, _CAMS)}
        assert links == {("A", "r"), ("B", "R2")}, ("truck swap crossing", links)
        return

    if case == "oct7_continuation":
        chains = [chain("cam3", "06", "b-minibuses", (1, 0), 27, dep=100),
                  chain("cam4", "07", "a-small", (.441, (1 - .441 ** 2) ** .5), 3, arr=100.4),
                  chain("cam4", "08", "b-minibuses", (.777, (1 - .777 ** 2) ** .5), 27, late=104)]
        chains[-1]["members"][0]["counted"] = True
        links = {(d["members"][0]["id"], r["members"][0]["id"]) for d, r in _links(chains, _CAMS)}
        assert links == {("06", "08")}, ("7 Oct 06:07:30 counted continuation replaces directionless sliver", links)
        return

    if case == "truck_swap_protection":
        chains = [chain("cam3", "A", "e-heavy", (1, 0, 0), 88, dep=36100),
                  chain("cam3", "B", "e-heavy", (.8, .6, 0), 88, dep=36095),
                  chain("cam4", "r", "b-light", (.3, -.4, .75 ** .5), 88, arr=36100.5),
                  chain("cam4", "U", "e-heavy", (1, 0, 0), 88, late=36108)]
        links = {(d["members"][0]["id"], r["members"][0]["id"]) for d, r in _links(chains, _CAMS)}
        assert links == {("A", "r"), ("B", "U")}, ("truck swap protection", links)
        return

    if case == "early_swap_protection":
        chains = [chain("cam3", "A", "b-minibuses", (1, 0, 0), 88, dep=36100),
                  dict(chain("cam3", "E", "b-light", (.3, .91 ** .5, 0), 88), early=36100),
                  chain("cam4", "r", "b-light", (.3, .91 ** .5, 0), 88, arr=36100.5),
                  chain("cam4", "b", "b-light", (0, .6 / .91 ** .5, (1 - .36 / .91) ** .5), 88, arr=36101),
                  chain("cam4", "U", "b-minibuses", (1, 0, 0), 88, late=36108)]
        links = {(d["members"][0]["id"], r["members"][0]["id"]) for d, r in _links(chains, _CAMS)}
        # E keeps its baseline link; only A swaps its weak cross-class link to unused U, releasing r.
        assert links == {("A", "U"), ("E", "b")}, ("early swap protection", links)
        return

    if case == "shared_late":
        chains = [chain("cam3", "A", "b-minibuses", (1, 0, 0, 0), 88, dep=100),
                  chain("cam3", "B", "b-minibuses", (0, 1, 0, 0), 88, dep=101),
                  chain("cam4", "a", "b-light", (.3, 0, .91 ** .5, 0), 21, arr=100.4),
                  chain("cam4", "b", "b-light", (0, .3, 0, .91 ** .5), 21, arr=101.4),
                  chain("cam4", "late", "b-minibuses", (.6, .8, 0, 0), 176, late=104)]
        links = {(d["members"][0]["id"], r["members"][0]["id"]) for d, r in _links(chains, _CAMS)}
        # A is first in ordinary assignment order and takes late; B keeps its ordinary link.
        assert links == {("A", "late"), ("B", "b")}, ("shared late arrival", links)
        return

    if case == "uncontested_replacement":
        chains = [chain("cam3", "A", "b-minibuses", (1, 0, 0), 88, dep=36100),
                  chain("cam3", "B", "b-minibuses", (0, 1, 0), 88, dep=36101),
                  chain("cam4", "a", "b-light", (.3, 0, .91 ** .5), 88, arr=36100.5),
                  chain("cam4", "U", "b-minibuses", (.6, 0, .8), 88, late=36110),
                  chain("cam4", "S", "b-minibuses", (.8, .6, 0), 88, late=36101)]
        links = {(d["members"][0]["id"], r["members"][0]["id"]) for d, r in _links(chains, _CAMS)}
        # B keeps S from the late pass; A swaps its ordinary arrival for unused U.
        assert links == {("A", "U"), ("B", "S")}, ("uncontested replacement before shared late", links)
        return

    if case == "shared_late_three":
        chains = [chain("cam3", "A", "b-minibuses", (1, 0, 0, 0), 88, dep=36100),
                  chain("cam3", "B", "b-minibuses", (0, 1, 0, 0), 88, dep=36101),
                  chain("cam3", "C", "b-light", (0, 0, 1, 0), 88, dep=36101),
                  chain("cam4", "a", "b-light", (.3, 0, .25, .8475 ** .5), 88, arr=36100.5),
                  chain("cam4", "b", "b-light", (0, .3, 0, .91 ** .5), 88, arr=36101.5),
                  chain("cam4", "L", "b-minibuses", (.5, .6, .6, .03 ** .5), 88, late=36104)]
        links = {(d["members"][0]["id"], r["members"][0]["id"]) for d, r in _links(chains, _CAMS)}
        # C already uses L after the late pass, so both ordinary links stay intact.
        assert links == {("A", "a"), ("B", "b"), ("C", "L")}, ("three departures share late arrival", links)
        return

    if case == "ordinary_count_protection":
        chains = [chain("cam3", "A", "b-light", (1, 0, 0), 88, dep=36100),
                  chain("cam3", "B", "b-light", (0, 1, 0), 88, dep=36101),
                  chain("cam4", "a", "b-light", (.3, .65, .4875 ** .5), 88, arr=36100.5),
                  chain("cam4", "b", "b-minibuses", (0, .3, .91 ** .5), 88, arr=36102),
                  chain("cam4", "U", "b-light", (0, .6, .8), 88, late=36100)]
        links = {(d["members"][0]["id"], r["members"][0]["id"]) for d, r in _links(chains, _CAMS)}
        # Preserve count-protected A→a; only B swaps its weak cross-class link for U.
        assert links == {("A", "a"), ("B", "U")}, ("ordinary count protection", links)
        return

    if case == "truck_wider_window":
        chains = [chain("cam3", "A", "e-heavy", (1, 0, 0), 88, dep=36100),
                  chain("cam3", "B", "e-heavy", (.4, .84 ** .5, 0), 88, dep=36095),
                  chain("cam3", "C", "e-heavy", (.6, -.6, .28 ** .5), 88, dep=36111),
                  chain("cam4", "c", "e-heavy", (.6, -.6, .28 ** .5), 88, arr=36112),
                  chain("cam4", "U", "e-heavy", (.71, .4959 ** .5, 0), 88, late=36108),
                  chain("cam4", "S", "e-heavy", (1, 0, 0), 88, late=36110)]
        links = {(d["members"][0]["id"], r["members"][0]["id"]) for d, r in _links(chains, _CAMS)}
        # No weak cross-class ordinary link exists; A keeps S and the truck pass gives B U.
        assert links == {("C", "c"), ("A", "S"), ("B", "U")}, ("truck wider window", links)
        return

    ordinary_cos = .61 if case == "strong_ordinary" else .368
    late_cos = .3 if case == "weak_late" else .61
    cab_class = "b-minibuses" if case == "same_class" else "b-light"
    chains = [chain("cam3", "4c", "b-minibuses", (1, 0, 0, 0), 88, dep=2414.8),
              chain("cam3", "45", "b-light", (0, 1, 0, 0), 104, dep=2416.6),
              chain("cam4", "5b", cab_class,
                    (ordinary_cos, .264, (1 - ordinary_cos ** 2 - .264 ** 2) ** .5, 0), 21, arr=2412.4),
              chain("cam4", "5d", "b-light", (0, .448, 0, (1 - .448 ** 2) ** .5), 19, arr=2415.4),
              chain("cam4", "61", "b-minibuses", (late_cos, 0, (1 - late_cos ** 2) ** .5, 0),
                    176, late=2420)]
    if case in ("released_counted", "released_direction"):
        # Evidence on any member of the released cab must preserve its ordinary link.
        chains[2]["members"].append({"id": "5b-fragment", "hits": 1,
                                    "counted": case == "released_counted",
                                    "direction": "northbound" if case == "released_direction" else None})
    links = {(d["members"][0]["id"], r["members"][0]["id"]) for d, r in _links(chains, _CAMS)}
    expected = {("4c", "61" if case == "yield" else "5b"), ("45", "5d")}
    assert links == expected, ("late priority", case, links, expected)


def _selfcheck_duplicate_cards(case):
    code = lambda cos: base64.b64encode(struct.pack("<32e", cos, (1 - cos * cos) ** .5, *[0.0] * 30)).decode()
    look = {"app": code(1), "app_v": "m1", "hits": 27, "counted": True}
    if case == "stub":
        a = _t("cam4", "stub", "b-minibuses", [(100, _b(.27, .45, .02)), (101, _b(.27, .45, .02))], hits=4)
        b = _t("cam4", "moving", "b-minibuses", [(101.2, _b(.28, .45, .02)), (103, _b(.5, .3, .02))],
               hits=11, counted=True, direction="northbound", ghost_of=a["id"])
        [ch] = _chains([a, b], _CAMS)
        assert ch.get("late") is None and ch.get("stub_late") == 101.2, "stub timing must not widen general late matching"
        d, _ = _north("stub-departure", 99.6, cls="b-minibuses", k3=look)
        assert not _build([d, a, b], _CAMS)[2], "stub continuation never enters ordinary late pass"
        for changes in ({"hits": MIN_EDGE_HITS}, {"path": [[100, *_b(.35, .45, .02)], [101, *_b(.27, .45, .02)]]}):
            [guarded] = _chains([dict(a, **changes), b], _CAMS)
            assert guarded.get("stub_late") is None, ("stub must be short and still", changes)
        assert _chains([a, dict(b, hits=MIN_EDGE_HITS - 1)], _CAMS)[0].get("stub_late") is None
        return
    if case == "oct9_general_late":
        # 9 Oct 14:05-14:10: #bc/#bd must not become a general late arrival for #b9.
        tracks = [
            _t('cam3', '000000b9', "a-small", [
                (91.0, [0.067, 0.5464, 0.0881, 0.5809]),
                (92.0, [0.0916, 0.5575, 0.1155, 0.5964]),
                (93.0, [0.1262, 0.5764, 0.1493, 0.6119]),
                (94.0, [0.1666, 0.5921, 0.1961, 0.634]),
                (95.0, [0.2209, 0.6129, 0.2558, 0.6606]),
                (96.0, [0.2957, 0.6391, 0.3381, 0.6941]),
                (97.0, [0.3937, 0.6685, 0.4505, 0.737]),
                (98.0, [0.5367, 0.7141, 0.6039, 0.792]),
                (99.0, [0.7459, 0.7768, 0.8109, 0.8463]),
                (100.0, [0.9565, 0.8084, 1.0, 0.872]),
            ],
               hits=46, counted=False, direction='northbound', app='o7gntJmxXLBZtm+lkLSvLlUxACZEr9wscjF7L/2y8KXArfkw8y6xrUexKaNoL4+oPjJAq8At0K7jJRYgDSssKQ==', app_v='d39e2864c3c7'),
            _t('cam4', '000000bc', "a-small", [
                (102.8, [0.369, 0.5286, 0.4171, 0.5796]),
                (103.0, [0.3845, 0.5266, 0.428, 0.5727]),
            ],
               hits=2, counted=True, direction=None, app='8rE+tKO2JbI7t8Kqt7IaLZMzISfqrBqrmiwtrD20OK2AMEUpvjAWsn6z4yqQLtiuiDInr7MwYKkOrIyi/yrRMQ==', app_v='d39e2864c3c7'),
            _t('cam4', '000000bd', "a-small", [
                (103.6, [0.4222, 0.5116, 0.4643, 0.5588]),
                (104.8, [0.4781, 0.4929, 0.5169, 0.5364]),
                (105.8, [0.5126, 0.4828, 0.5459, 0.5201]),
                (106.8, [0.5388, 0.4753, 0.5668, 0.5072]),
                (107.8, [0.5569, 0.4697, 0.5826, 0.4995]),
                (108.2, [0.5631, 0.4681, 0.5877, 0.4973]),
            ],
               hits=21, counted=False, direction='northbound', app='YLSrrnW0/abPuIKuSLQvLQMz9iHpq5Qqh602rNG1EbAVLHiolyx4sY6xaKmNMbGgKDFOqw0vYqpwrQmoUZM7MA==', app_v='d39e2864c3c7'),
        ]
        js, chains, links = _build(tracks, _CAMS)
        assert len(chains) == 2 and len(js) == 2 and not links, "9 Oct still prefix preserves two journeys"
        assert next(c for c in chains if c["camera"] == "cam4").get("late") is None
        return
    if case == "sliver":
        a, _ = _north("minibus", 100, cls="b-minibuses", k3=look)
        r = _t("cam4", "sliver", "a-small", [(100.4, _b(.03, .65, .02)), (102, _b(.05, .7, .02))],
               hits=4, direction="southbound", app=code(.76), app_v="m1")
        b = _t("cam4", "late", "b-minibuses", [(98, _b(.3, .5, .02)), (101, _b(.5, .3, .02))],
               direction="northbound", **(look | {"app": code(.86)}))
        tracks = [a, r, b]
        js, _, links = _build(tracks, _CAMS)
        assert len(links) == 1 and links[0][1]["members"][0]["id"] == b["id"], "opposite weak sliver yields to better late arrival"
        assert len(js) == 1 and {m["id"] for m in js[0]["members"]} == {a["id"], b["id"]}, "released sliver has no counting evidence"
        for changes in ({"hits": MIN_EDGE_HITS}, {"counted": True}, {"direction": "northbound"}):
            assert _build([a, dict(r, **changes), b], _CAMS)[2][0][1]["members"][0]["id"] == r["id"], ("sliver evidence protects link", changes)
        for cosine in (.70, .79, .80):
            worse = dict(b, app=code(cosine))
            assert _build([a, r, worse], _CAMS)[2][0][1]["members"][0]["id"] == r["id"], "replacement needs .8 cosine and .05 improvement"
        from unittest.mock import patch
        for start, swap in ((98, True), (97.9, False), (112, True), (112.1, False)):
            shifted = dict(b, t0=start, t1=start + 3, path=[[p[0] - b["t0"] + start, *p[1:]] for p in b["path"]])
            target = b["id"] if swap else r["id"]
            # Isolate the window boundaries from the separately checked timing budget.
            with patch(__name__ + ".MU", 10):
                assert _build([a, r, shifted], _CAMS)[2][0][1]["members"][0]["id"] == target, ("sliver window", start)
        prefix = _t("cam4", "still-prefix", "b-minibuses",
                    [(97, _b(.29, .51, .02)), (97.5, _b(.29, .51, .02))], hits=4)
        continuation = dict(b, ghost_of=prefix["id"])
        repaired, chains, swapped = _build([a, r, prefix, continuation], _CAMS)
        arrival = next(c for c in chains if b in c["members"] or continuation in c["members"])
        assert arrival.get("late") is None and arrival.get("stub_late") == 98
        assert len(repaired) == 1 and swapped == [(next(c for c in chains if a in c["members"]), arrival)]
        assert repaired[0]["link"]["gap_s"] == -2, "stub-backed link uses moving-member arrival timing"
        # Check the ordinary late window independently of leftover pairing.
        with patch(__name__ + ".PAIR_COS", 9), patch(__name__ + ".PAIR_HEAVY_COS", 9):
            assert not _build([a, prefix, continuation], _CAMS)[2], "stub continuation never widens ordinary late candidates"
        paired, _, leftover = _build([a, prefix, continuation], _CAMS)
        assert len(paired) == 1 and len(leftover) == 1 and {a["id"], continuation["id"]} <= {m["id"] for m in paired[0]["members"]}, "leftover pairing keeps the same minibus in one card"
        assert len(build([a, prefix, dict(continuation, app=code(.80))], _CAMS)) == 2, "stub leftovers need pair cosine"
        from selfloop import cleared
        final = cleared(js, js, cutoff=1000)
        assert final == js, "repaired minibus freezes"
        claims = {(m["id"], m.get("piece")) for j in final for m in j["members"]}
        assert build(tracks, _CAMS, frozen=claims) == [], "released sliver cannot count after freeze rebuild"
        return


def _selfcheck_sliver_guards(case):
    code = lambda cos: base64.b64encode(struct.pack("<32e", cos, (1 - cos * cos) ** .5, *[0.0] * 30)).decode()

    def chain(cam, name, cls="b-minibuses", cosine=1, hits=27, dep=None, arr=None, stub=None):
        member = {"id": name, "hits": hits, "app": code(cosine), "app_v": "m1",
                  "path": [[stub if stub is not None else 100, *_b(.3, .5, .02)]]}
        return {"camera": cam, "class": cls, "app": _app([member]), "members": [member],
                "dep": dep, "arr": arr, "late": None, "stub_late": stub,
                "z": (dep - 1, dep) if dep is not None else (arr, arr)}

    A = chain("cam3", "A", dep=100)
    r = chain("cam4", "r", cosine=.78, hits=4, arr=100.5)
    L = chain("cam4", "L", cosine=.9, stub=102)
    chains = [A, r, L]
    expected = {("A", "L")}
    if case == "ownership":
        B = chain("cam3", "B", dep=100.1)
        R2 = chain("cam4", "R2", cosine=.78, hits=4, arr=100.6)
        chains += [B, R2]
        expected = {("A", "L"), ("B", "R2")}
    elif case == "class":
        L["class"] = "d-medium"
        expected = {("A", "r")}
    elif case == "margin":
        L["app"] = _app([dict(L["members"][0], app=code(.82))])
        assert .8 < _similar(A["app"], L["app"]) < _similar(A["app"], r["app"]) + .05
        expected = {("A", "r")}
    elif case == "similarity":
        r["app"] = _app([dict(r["members"][0], app=code(.70))])
        L["app"] = _app([dict(L["members"][0], app=code(.79))])
        assert _similar(A["app"], r["app"]) + .05 < _similar(A["app"], L["app"]) < .8
        expected = {("A", "r")}
    elif case == "hits":
        L["members"][0]["hits"] = MIN_EDGE_HITS - 1
        expected = {("A", "r")}
    elif case == "timing":
        L["stub_late"] = 104
        expected = {("A", "r")}
    elif case == "stub_convoy":
        A["class"] = r["class"] = L["class"] = "e-heavy"
        L["stub_late"] = 103
        L["members"][0]["path"][0][0] = 103
        L["members"].insert(0, {"id": "still-prefix", "hits": 4,
                                 "path": [[101.5, *_b(.3, .5, .02)], [102, *_b(.3, .5, .02)]]})
        B = chain("cam3", "B", cls="e-heavy", dep=106)
        R2 = chain("cam4", "R2", cls="e-heavy", arr=102)
        B["app"] = R2["app"] = ((0, 1), "m1")
        chains += [B, R2]
        expected = {("A", "r"), ("B", "R2")}
    links = {(d["members"][0]["id"], r["members"][0]["id"]) for d, r in _links(chains, _CAMS)}
    assert links == expected, ("sliver guard", case, links, expected)


def _selfcheck_weak_arrival_perf():
    start = time.process_time()
    tracks = [t for i in range(10_000) for t in _north(
        i, 100.0 + 5 * i, k4={"hits": 4})]
    tracks = [dict(t, direction=None) if t["camera"] == "cam4" else t for t in tracks]
    assert len(build(tracks, _CAMS)) == 10_000
    took = time.process_time() - start
    assert took < 10, ("20k weak-arrival workload with scheduling margin", took)
    # Known similarities must also stay windowed when many unused late arrivals exist.
    chains = []
    for i in range(5_000):
        dep = 100 + 30 * i
        for cam, name, cls, cosine, hits, departure, arrival, late in (
                ("cam3", "d", "b-minibuses", 1, 27, dep, None, None),
                ("cam4", "r", "a-small", .76, 4, None, dep + .4, None),
                ("cam4", "L", "b-minibuses", .79, 27, None, None, dep + 4)):
            chains.append({"camera": cam, "class": cls, "app": ((cosine, (1 - cosine ** 2) ** .5), "m1"),
                           "dep": departure, "arr": arrival, "late": late,
                           "z": (dep - 1, dep) if departure is not None else (arrival, arrival),
                           "members": [{"id": f"{name}-{i}", "hits": hits}]})
    start = time.process_time()
    assert len(_links(chains, _CAMS)) == 5_000
    indexed_took = time.process_time() - start
    assert indexed_took < 10, ("15k known-similarity weak/late workload with scheduling margin", indexed_took)
    return took, indexed_took


def _selfcheck_occlusion(case):
    # 18 Oct 08:14: the northbound tipper disappears behind a southbound truck for 4.4 s.
    a = _t("cam3", "occluded", "e-heavy", [(881.8, _b(.066, .548, .025)),
           (884.8, _b(.158, .588, .035)), (885.6, _b(.193, .600, .04))],
           hits=20, counted=True, direction="northbound")
    b = _t("cam3", "reacquired", "d-medium", [(890, _b(.545, .710, .07)),
           (891, _b(.656, .735, .077)), (894.6, _b(.944, .773, .075))],
           hits=24, direction="northbound")
    truck = _t("cam3", "occluder", "e-heavy", [(879.6, _b(.87, .80, .12)),
               (885.8, _b(.272, .642, .10)), (893.2, _b(.054, .549, .03))],
               hits=64, direction="southbound")
    others, expected = [truck], True
    if case == "absent":
        others, expected = [], False
    elif case == "same_direction":
        others, expected = [dict(truck, direction="northbound")], False
    elif case == "opposed":
        b, expected = dict(b, direction="southbound"), False
    elif case == "opposed_motion":
        b = dict(b, path=[b["path"][0], [891, *_b(.40, .70, .077)], [894.6, *_b(.30, .60, .075)]])
        assert _opposed(a, b), "occlusion continuation reverses despite its direction label"
        expected = False
    elif case == "unknown_direction":
        b, expected = dict(b, direction=None), False
    elif case == "other_camera":
        others, expected = [dict(truck, camera="cam4")], False
    elif case == "small":
        others, expected = [dict(truck, **{"class": "a-small"})], False
    elif case == "no_box":
        others, expected = [dict(truck, path=[truck["path"][0], truck["path"][-1]])], False
    elif case == "no_overlap":
        others, expected = [dict(truck, path=[[p[0], *_b(.8, .2, .1)] for p in truck["path"]])], False
    elif case == "smaller_heavy":
        others, expected = [dict(truck, path=[[p[0], *_b(.193, .600, .02)] for p in truck["path"]])], False
    elif case in ("limit", "too_long"):
        shift = 1.6 if case == "limit" else 1.7
        b = dict(b, t0=b["t0"] + shift, t1=b["t1"] + shift,
                 path=[[p[0] + shift, *p[1:]] for p in b["path"]])
        expected = case == "limit"
    elif case in ("convoy", "backward", "level"):
        a = _t("cam3", "occluded", "e-heavy", [(98, _b(.20, .60, .05)),
               (99, _b(.30, .60, .05)), (100, _b(.40, .60, .05))],
               hits=15, counted=True, direction="northbound")
        if case == "level":
            a = dict(a, path=[[98, *_b(.38, .60, .05)], [99, *_b(.39, .60, .05)], a["path"][-1]])
        x = .39 if case == "backward" else .40
        b = _t("cam3", "reacquired", "e-heavy", [(105, _b(x, .60, .05)),
               (106, _b(x + .10, .60, .05)), (107, _b(x + .20, .60, .05))],
               hits=15, counted=True, direction="northbound")
        others = [_t("cam3", "occluder", "e-heavy", [(99, _b(.50, .60, .15)),
                  (100, _b(.40, .60, .15)), (101, _b(.30, .60, .15))],
                  hits=15, counted=True, direction="southbound")]
        assert _near(a, b) == (case == "level") and iou(a["path"][-1][1:], b["path"][0][1:]) >= STITCH_IOU
        assert _occluded(a, others), "convoy fixture needs occlusion evidence"
        expected = False
    chains = _chains([a, b, *others], _CAMS)
    together = any({a["id"], b["id"]} <= {t["id"] for t in c["members"]} for c in chains)
    assert together == expected, ("occlusion stitch", case, together)
    if case == "partial_freeze":
        from selfloop import cleared
        truck = dict(truck, counted=True)
        b = dict(b, t1=3000, path=[*b["path"], [3000, *b["path"][-1][1:]]])
        tracks = [a, b, truck]
        js = build(tracks, _CAMS)
        final = cleared(js, js, cutoff=1300)
        assert [{m["id"] for m in j["members"]} for j in final] == [{truck["id"]}], "only occluder freezes"
        claims = {(m["id"], m.get("piece")) for j in final for m in j["members"]}
        for open_tracks in (tracks, [a, b]):
            rebuilt = build(open_tracks, _CAMS, frozen=claims, history=tracks)
            assert len(rebuilt) == 1 and {m["id"] for m in rebuilt[0]["members"]} == {a["id"], b["id"]}, (
                "partial freeze preserves occlusion without reclaiming occluder", rebuilt)
    if case == "freeze":
        from selfloop import cleared
        arrival = _north("tipper", 894.6, gap=1.6, cls="e-heavy", k4={"counted": True})[1]
        tracks = [a, b, truck, arrival]
        js = build(tracks, _CAMS)
        tipper = next(j for j in js if any(m["id"] == a["id"] for m in j["members"]))
        assert {m["id"] for m in tipper["members"]} == {a["id"], b["id"], arrival["id"]}
        assert build(tracks[::-1], _CAMS) == js, "occlusion input order"
        final = cleared(js, js, cutoff=2000)
        assert final == js, "occlusion freezes"
        claims = {(m["id"], m.get("piece")) for j in final for m in j["members"]}
        assert build(tracks, _CAMS, frozen=claims) == [], "occlusion freeze rebuild"



def _selfcheck_overtake(case):
    # 19 Oct 12:36: overlap at loss becomes centre cover three seconds later.
    a = _t("cam3", "overtaken", "e-heavy", [
        (22.2, [.2244, .5864, .3366, .6909]),
        (23.2, [.2665, .6049, .3649, .6992]),
        (24.2, [.2903, .6102, .3908, .7086]),
        (24.4, [.2969, .6119, .3962, .7104])],
        hits=60, counted=True, direction="northbound")
    b = _t("cam3", "reacquired", "d-medium", [
        (35.8, [.4622, .6498, .5883, .774]),
        (36.8, [.4882, .6587, .613, .7813]),
        (49.4, _b(.94, .80, .06))], hits=60, direction="northbound")
    boxes = [(.1363, .5331, .2648, .6817), (.1556, .5325, .3036, .6959),
             (.1776, .5376, .3447, .7131), (.2034, .551, .3879, .7315),
             (.2276, .5566, .4381, .749), (.2531, .5639, .4913, .7694),
             (.2859, .5736, .5497, .7899), (.3193, .5821, .6129, .8119),
             (.3637, .5937, .6836, .8334), (.4086, .6083, .7526, .8522),
             (.4565, .617, .8329, .8717), (.5181, .6315, .9045, .883),
             (.5875, .651, .9711, .8914), (.6381, .6666, 1., .8943)]
    truck = _t("cam3", "overtaker", "e-heavy",
               [(23.4 + i, box) for i, box in enumerate(boxes)],
               hits=64, counted=True, direction="northbound")
    expected = case in ("road_train", "partial_freeze", "freeze", "order", "limit", "twin", "ghost", "early_vehicle", "later_visible_join",
                        "early_twin_real", "early_ghost_real", "later_early_join")
    if case == "follower":
        b = dict(b, path=[[p[0], *_b(.14 + .02 * i, .70, .06)] for i, p in enumerate(b["path"])])
    elif case == "ends_early":
        truck = dict(truck, t1=28.4, path=truck["path"][:6])
    elif case == "interrupted":
        truck = dict(truck, path=[p if i != 7 else [p[0], *_b(.1, .2, .05)]
                                 for i, p in enumerate(truck["path"])])
    elif case == "missing_samples":
        truck = dict(truck, path=truck["path"][:5] + truck["path"][9:])
    elif case == "slower":
        truck = dict(truck, path=[[23.4, *_b(.222, .614, .075)], *truck["path"][1:]])
    elif case == "never_reaches":
        truck = dict(truck, path=[p if i < 4 else [p[0], *_b(.1, .2, .05)]
                                 for i, p in enumerate(truck["path"])])
    elif case == "no_overlap":
        truck = dict(truck, path=[p if i != 1 else [p[0], *_b(.15, .60, .07)]
                                 for i, p in enumerate(truck["path"])])
    elif case in ("limit", "too_long"):
        born = a["t1"] + (15 if case == "limit" else 15.01)
        b = dict(b, t0=born, path=[[born, *b["path"][0][1:]],
                     [born + 1, *_b(.60, .73, .06)], b["path"][-1]])
        truck = dict(truck, t1=39.4, path=[*truck["path"],
                     [37.4, *_b(.70, .77, .20)], [38.4, *_b(.73, .78, .20)],
                     [39.4, *_b(.76, .79, .20)]])
    tracks = [a, b, truck]
    if case in ("predecessor", "heavy_predecessor"):
        from itertools import permutations

        truck = dict(truck, t1=28.4, path=truck["path"][:6])
        p = _t("cam3", "predecessor", "e-heavy" if case == "heavy_predecessor" else "d-medium",
               [(18.2, _b(.18, .63, .05)), (19.2, _b(.205, .637, .05))],
               hits=20, counted=True, direction="northbound")
        b = _t("cam3", "reacquired", "e-heavy",
               [(27.5, _b(.43, .69, .05)), (28.5, _b(.455, .697, .05)),
                (40, _b(.94, .80, .06))], hits=60, counted=True, direction="northbound")
        tracks = [p, a, b, truck]
        expected_groups = {frozenset((p["id"], a["id"], b["id"])), frozenset((truck["id"],))}
        baseline = None
        for ordered in permutations(tracks):
            js = build(list(ordered), _CAMS)
            groups = {frozenset(m["id"] for m in j["members"]) for j in js}
            assert groups == expected_groups, ("A-side predecessor", case, groups)
            if baseline is None:
                baseline = js
            assert js == baseline, ("A-side predecessor input order", case)
        return
    if case in ("twin", "ghost"):
        duplicate = _t("cam3", "duplicate", "e-heavy",
                       [(p[0], p[1:]) for p in truck["path"] if p[0] >= 32.4],
                       hits=20, direction="northbound")
        if case == "ghost":
            duplicate = dict(duplicate, ghost_of=truck["id"])
        tracks.append(duplicate)
    elif case in ("visible_twin", "visible_ghost"):
        c = _t("cam3", "visible_c", "e-heavy",
               [(22.4 + i, _b(.33 + .015 * i, .54, .07)) for i in range(17)],
               hits=60, counted=True, direction="northbound")
        duplicate = _t("cam3", "visible_d", "e-heavy",
                       [(p[0], p[1:]) for p in c["path"] if p[0] >= 36.4],
                       hits=20, counted=True, direction="northbound")
        if case == "visible_ghost":
            duplicate = dict(duplicate, ghost_of=c["id"])
        tracks = [a, truck, c, duplicate]
    elif case in ("early_twin", "early_ghost", "early_twin_real", "early_ghost_real"):
        c = _t("cam3", "early_c", "e-heavy",
               [(32.8 + i, _b(.54 + .05 * i, .59, .06)) for i in range(7)],
               hits=60, counted=True, direction="northbound")
        duplicate = _t("cam3", "early_d", "e-heavy",
                       [(p[0], p[1:]) for p in c["path"] if p[0] >= 36.8],
                       hits=20, counted=True, direction="northbound")
        if "ghost" in case:
            duplicate = dict(duplicate, ghost_of=c["id"])
        tracks = [a, truck, c, duplicate] + ([b] if case.endswith("_real") else [])
    elif case in ("later_visible_join", "later_early_join"):
        b = dict(b, t1=39.4, path=[*b["path"][:2],
                 [38.4, *_b(.94, .50, .06)], [39.4, *_b(.92, .52, .06)]])
        duplicate = _t("cam3", "late_twin", "d-medium",
                       [(p[0], p[1:]) for p in b["path"] if p[0] >= 38.4],
                       hits=20, counted=True, direction="northbound")
        c = _t("cam3", "earlier_vehicle", "e-heavy",
               [(23, _b(.63, .27, .05)), (24, _b(.65, .30, .05))],
               hits=20, counted=True, direction="northbound")
        cover = _t("cam3", "earlier_cover", "e-heavy",
                   [(t, _b(.60 + .025 * (t - 23), .30 + .03 * (t - 23), .18))
                    for t in range(23, 40)], hits=60, counted=True, direction="northbound")
        if case == "later_early_join":
            c = dict(c, t0=c["t0"] + 9, t1=c["t1"] + 9,
                     path=[[p[0] + 9, *p[1:]] for p in c["path"]])
            cover = dict(cover, t0=32, path=[[p[0] + 9, *p[1:]] for p in cover["path"] if p[0] < 31])
        tracks = [a, b, truck, c, cover, duplicate]
        assert any({c["id"], duplicate["id"]} <= {t["id"] for t in ch["members"]}
                   for ch in _chains([c, cover, duplicate], _CAMS)), "later join is otherwise admissible"
    elif case == "joined_occluder":
        duplicate = _t("cam3", "joined_cover", "e-heavy",
                       [(37.4, truck["path"][-1][1:]), (38.4, _b(.87, .78, .15))],
                       hits=20, counted=True, direction="northbound")
        tracks = [a, truck, duplicate]
    elif case in ("early_vehicle", "ambiguous"):
        born = 32.8 if case == "early_vehicle" else 34.8
        tracks.append(_t("cam3", "far_lane", "e-heavy",
                      [(born + i, _b(x, .59, .06)) for i, x in enumerate((.54, .59, .64))],
                      hits=20, counted=True, direction="northbound"))
    elif case == "queue":
        a = _t("cam3", "queued_a", "e-heavy", [(23, _b(.3500, .65, .05)),
                (24, _b(.3501, .65, .05))], hits=20, counted=True, direction="northbound")
        truck = _t("cam3", "queued_cover", "e-heavy",
                   [(t, _b(.34 + .001 * (t - 23), .65, .10)) for t in range(23, 37)],
                   hits=60, counted=True, direction="northbound")
        b = _t("cam3", "queued_b", "e-heavy", [(34, _b(.39, .65, .05)),
                (35, _b(.3901, .65, .05))], hits=20, counted=True, direction="northbound")
        tracks = [a, b, truck]
    chains = _chains(tracks, _CAMS)
    members = next({t["id"] for t in c["members"]} for c in chains if a in c["members"])
    assert (b["id"] in members) == expected, ("overtake stitch", case, members)
    assert truck["id"] not in members, ("overtaker is never the continuation", case)
    if case in ("twin", "ghost", "joined_occluder", "early_vehicle", "ambiguous", "queue",
                "visible_twin", "visible_ghost", "later_visible_join",
                "early_twin", "early_ghost", "early_twin_real", "early_ghost_real", "later_early_join"):
        from itertools import permutations
        for ordered in permutations(tracks):
            js = build(list(ordered), _CAMS)
            group = next({m["id"] for m in j["members"]} for j in js if any(m["id"] == a["id"] for m in j["members"]))
            assert (b["id"] in group) == expected and truck["id"] not in group, (case, group)
            allowed = {a["id"], b["id"]}
            if case in ("later_visible_join", "later_early_join"):
                allowed.add(duplicate["id"])
            assert group <= allowed, ("distinct vehicle", case, group)
            if case in ("visible_twin", "visible_ghost", "early_twin", "early_ghost",
                        "early_twin_real", "early_ghost_real"):
                assert group == ({a["id"], b["id"]} if expected else {a["id"]}) and len(js) == 3, (case, js)
                assert any({c["id"], duplicate["id"]} <= {m["id"] for m in j["members"]}
                           for j in js), ("resolved visible vehicle", case)
    if case == "order":
        from itertools import permutations
        js = build(tracks, _CAMS)
        for ordered in permutations(tracks):
            assert build(list(ordered), _CAMS) == js, "overtake input order"
    if case in ("freeze", "partial_freeze"):
        from selfloop import cleared
        if case == "partial_freeze":
            b = dict(b, t1=3000, path=[*b["path"], [3000, *b["path"][-1][1:]]])
            tracks = [a, b, truck]
        js = build(tracks, _CAMS)
        final = cleared(js, js, cutoff=1300)
        claims = {(m["id"], m.get("piece")) for j in final for m in j["members"]}
        if case == "freeze":
            assert final == js and build(tracks, _CAMS, frozen=claims) == [], "overtake freeze"
        else:
            assert [{m["id"] for m in j["members"]} for j in final] == [{truck["id"]}]
            original = json.dumps(tracks, sort_keys=True)
            for opened in (tracks, [a, b]):
                rebuilt = build(opened, _CAMS, frozen=claims, history=tracks)
                assert len(rebuilt) == 1 and {m["id"] for m in rebuilt[0]["members"]} == {a["id"], b["id"]}, rebuilt
            assert json.dumps(tracks, sort_keys=True) == original, "history is read-only"


def _selfcheck_l2(case):
    code = lambda c: base64.b64encode(struct.pack("<32e", c, (1 - c*c)**.5, *[0.] * 30)).decode()
    kw = dict(direction="southbound", app=code(1), app_v="m1")
    if case.startswith("tail"):
        a = _t("cam3", 1, "c-bus", [(100, _b(.9,.8)), (101, _b(.8,.75)),
               (102, _b(.7,.7)), (103, _b(.6,.65)), (104, _b(.5,.6)), (105, _b(.4,.55))],
               counted=True, hits=30, **kw)
        b = _t("cam3", 2, "c-bus", [(101.8, _b(.72,.71)), (103, _b(.67,.69)),
               (104, _b(.61,.67))], hits=10, **(kw | {"app": code(.91)}))
        if case == "tail_interpolated":
            a["path"] = [a["path"][0], a["path"][2], *a["path"][4:]]
            b["path"][0] = [101, *_b(.8,.75)]
            b["t0"] = 101
        cls = {"tail_minibus": "b-minibuses", "tail_car": "a-small", "tail_convoy": "e-heavy", "tail_medium": "d-medium"}.get(case)
        if cls:
            a = dict(a, **{"class": cls, "votes": {cls: 30}})
            b = dict(b, **{"class": cls, "votes": {cls: 10}})
        if case == "tail_convoy":
            b["app"] = code(.84)
        if case == "tail_medium":
            b["app"] = code(.94)
        assert _twin(a, b) == (cls is None), case
        assert len(_chains([a,b], _CAMS)) == (1 if cls is None else 2), case
        return
    if case.startswith("pair"):
        cls = "e-heavy" if "heavy" in case else "b-light"
        a = _t("cam4", 1, cls, [(100,_b(.6,.4)),(101,_b(.5,.4)),(102,_b(.4,.4))],
               counted=True, hits=30, **kw)
        gap = -20 if case == "pair_order" else 31 if case == "pair_window" else 2
        cos = .7 if case == "pair_heavy_low" else .8 if case == "pair_light_low" else .79 if case == "pair_heavy" else .9
        b = _t("cam3", 2, cls, [(102+gap,_b(.6,.4)),(103+gap,_b(.5,.4)),(104+gap,_b(.4,.4))],
               counted=True, hits=30, **(kw | {"app": code(cos)}))
        tracks = [a,b]
        if case == "pair_opposite":
            b["direction"] = "northbound"
        if case == "pair_ambiguous":
            tracks.append(dict(b, id="obs-cam3-decoy", app=code(.88),
                               path=[[t,x1,y1+.3,x2,y2+.3] for t,x1,y1,x2,y2 in b["path"]]))
        if case == "pair_family":
            b = dict(b, **{"class": "c-bus", "votes": {"c-bus": 30}})
            tracks = [a,b]
        if case == "pair_directionless":
            tracks.append(dict(b, id="obs-cam3-directionless", direction=None, app=code(.99),
                               path=[[t,x1,y1+.3,x2,y2+.3] for t,x1,y1,x2,y2 in b["path"]]))
            assert build([tracks[-1]], _CAMS) == [], "directionless chain became a lone card"
        if case == "pair_north":
            a["camera"], b["camera"] = "cam3", "cam4"
            a["direction"] = b["direction"] = "northbound"
        yes = case in ("pair", "pair_heavy", "pair_directionless", "pair_north", "pair_freeze")
        js = build(tracks, _CAMS)
        assert len(js) == (1 if yes else len(tracks)), (case, js)
        if yes:
            assert js[0]["evidence"] == "handoff" and js[0]["link"]["gap_s"] == gap, (case, js)
        if case == "pair_directionless":
            assert {m["id"] for m in js[0]["members"]} == {a["id"], b["id"]}, "directionless chain joined or blocked pair"
        if case == "pair_freeze":
            _selfcheck_l2_freeze(tracks, js)
        return
    coach = _t("cam4", 1, "c-bus", [(100+k, _b(.5-.1*k,.4+.075*k)) for k in range(5)],
               counted=True, hits=30, **kw)
    arrival = _t("cam3", 3, "c-bus", [(104.5,_b(.9,.8)),(105.5,_b(.7,.6)),(106.5,_b(.5,.4))],
                 counted=True, hits=30, **kw)
    whole = dict(coach, id="obs-cam4-whole", ghost_of=coach["id"],
                 path=[[t,x1,y1-.05,x2+.12,y2] for t,x1,y1,x2,y2 in coach["path"]])
    rider = _t("cam4", 2, "d-medium", [(101+k,_b(.55-.1*k,.435+.075*k,.04)) for k in range(5)],
               hits=20, direction="southbound", attrs={"colour": "rider"}, speed_kph=170)
    if case.startswith("broad_follower") or case == "rider_light":
        off = .09 + .01 * (int(case[-1]) if case.startswith("broad_follower") else 0)
        rider = _t("cam4", 2, "b-light", [(100+k,_b(.5-.1*k+off,.4+.075*k-.75*off,.07)) for k in range(1,5)],
                   hits=20, direction="southbound")
    if case == "rider_sparse":
        coach = dict(coach, path=[coach["path"][0],coach["path"][-1]])
        whole = dict(whole, path=[whole["path"][0],whole["path"][-1]])
    if case == "rider_uncovered":
        whole = coach
    if case == "rider_counted":
        rider["counted"] = True
    if case == "rider_reverse":
        rider["reverse"] = True
    tracks = [coach, arrival, rider] + ([whole] if whole is not coach else [])
    yes = case in ("rider", "rider_freeze")
    js = build(tracks, _CAMS)
    if case == "rider_reverse":
        assert all(m["id"] != rider["id"] for j in js for m in j["members"]), "reversal part absorbed"
        return
    assert len(js) == (1 if yes else 2), (case, js)
    if yes:
        expected = build([coach,whole,arrival], _CAMS)[0]
        assert {k:v for k,v in js[0].items() if k not in ("id","members")} == {
            k:v for k,v in expected.items() if k not in ("id","members")}, "rider changed evidence"
        assert {m["id"] for m in js[0]["members"]} == {t["id"] for t in tracks}
    if case == "rider_freeze":
        _selfcheck_l2_freeze(tracks, js)


def _selfcheck_l2e(case):
    from unittest.mock import patch
    code = lambda c: base64.b64encode(struct.pack("<32e", c, (1-c*c)**.5, *[0.] * 30)).decode()
    if case.startswith("small") or case == "general_cos":
        cls = "a-motorcycle" if case == "small_motorcycle" else "a-small" if case.startswith("small") else "b-light"
        cos = .89 if case == "small_pass" else .87
        tracks = [_t("cam4", 1, cls, [(100,_b(.6,.4)),(101,_b(.5,.4)),(102,_b(.4,.4))],
                     counted=True, hits=30, direction="southbound", app=code(1), app_v="m1"),
                  _t("cam3", 2, cls, [(104,_b(.6,.4)),(105,_b(.5,.4)),(106,_b(.4,.4))],
                     counted=True, hits=30, direction="southbound", app=code(cos), app_v="m1")]
        assert len(build(tracks, _CAMS)) == (1 if case in ("small_pass", "general_cos") else 2), case
        return
    if case.startswith("convoy"):
        tracks = []
        for i in range(4):
            t = 100 + 5*i
            # 3 s transit plus 5 s headway gives an 8 s gap for the two missing views.
            tracks += [_t("cam4", i, "e-heavy", [(t-2,_b(.45,.35)),(t-1,_b(.3,.5)),(t,_b(.1,.7))],
                          counted=True,hits=8,direction="southbound",app=code(1),app_v="m1"),
                       _t("cam3", i, "e-heavy", [(t+3,_b(.9,.8)),(t+4,_b(.7,.6)),(t+5,_b(.5,.4))],
                          counted=True,hits=8,direction="southbound",app=code(1),app_v="m1")]
        if case in ("convoy_miss_one", "convoy_miss_two"):
            tracks = [t for t in tracks if t["id"] != "obs-cam3-1"]
        if case == "convoy_miss_two":
            tracks = [t for t in tracks if t["id"] != "obs-cam4-2"]
        with patch(__name__ + ".PAIR_COS", 9), patch(__name__ + ".PAIR_HEAVY_COS", 9):
            before = build(tracks, _CAMS)
        js = build(tracks, _CAMS)
        assert len(js) == 4, (case, len(js))
        if case != "convoy_miss_two":
            assert js == before, (case, "R3 changed ordinary convoy")
        return
    # Accepted B departs later but arrives first; A must not jump over B.
    def chain(cam, ident, dep, arr, early=None, late=None):
        t = _t(cam,ident,"e-heavy",[(100,_b(.5,.4)),(110,_b(.4,.4))],direction="southbound")
        return dict(camera=cam, members=[t], **{"class":"e-heavy"}, app=None,
                    dep=dep,arr=arr,early=early,late=late)
    a,b = chain("cam4",1,100,None),chain("cam3",1,None,110)
    d,r = chain("cam4",2,105,None),chain("cam3",2,None,108)
    with patch(__name__ + "._similar", return_value=.95):
        if case == "heavy_zone_window":
            b["arr"] = 121
            assert not _pairs([a,b], _CAMS), "heavy window used endpoints instead of 21 s zone gap"
        else:
            assert not _pairs([a,b], _CAMS, [(d,r)]), "heavy leftover jumped over accepted truck"


def _selfcheck_l2_freeze(tracks, js):
    from selfloop import cleared
    other = _t("cam3", "old", "a-small", [(-500,_b(.2,.3)),(-499,_b(.3,.4))],
               counted=True, direction="northbound")
    built = build([other,*tracks], _CAMS)
    final = cleared(built, built, cutoff=350)
    assert len(final) == 1 and final[0]["members"][0]["id"] == other["id"]
    claims = {(m["id"],m.get("piece")) for j in final for m in j["members"]}
    assert build([other,*tracks], _CAMS, frozen=claims) == js, "partial freeze changed L2 card"
    final = cleared(js, js, cutoff=1000)
    assert final == js
    claims |= {(m["id"],m.get("piece")) for j in final for m in j["members"]}
    assert build([other,*tracks], _CAMS, frozen=claims) == [], "L2 card reappeared after freeze"


def _selfcheck_l2f(case):
    from unittest.mock import patch
    code = lambda c: base64.b64encode(struct.pack("<32e", c, (1-c*c)**.5, *[0.] * 30)).decode()
    kw = dict(direction="southbound", app=code(1), app_v="m1")
    if case.startswith("tail_"):
        a = _t("cam3", 1, "c-bus", [(100+k,_b(.9-.1*k,.8-.05*k)) for k in range(6)],
               counted=True, hits=30, **kw)
        b = _t("cam3", 2, "c-bus", [(102,_b(.7,.7)),(103,_b(.67,.69)),(104,_b(.61,.67))],
               hits=10, **(kw | {"app":code(.91)}))
        assert _twin(a,b), "coach control must exercise tail rescue"
        if case == "tail_cos":
            b["app"] = code(.84)
        elif case == "tail_birth":
            b["path"][0][1:] = _b(.9,.9)
        elif case == "tail_end":
            b["t1"] = b["path"][-1][0] = a["t1"]
        elif case == "tail_hits":
            b["hits"] = a["hits"]
        assert not _twin(a,b), case
        assert len(_chains([a,b], _CAMS)) == 2, case
        return
    if case.startswith("rider_"):
        y = _t("cam4", 1, "c-bus", [(100+k,_b(.5-.1*k,.4+.075*k)) for k in range(6)], **kw)
        x = _t("cam4", 2, "d-medium", [(101+k,_b(.45-.1*k,.435+.075*k,.04)) for k in range(5)], **kw)
        whole = dict(y, id="obs-cam4-whole", path=[[t,x1,y1-.05,x2+.12,y2] for t,x1,y1,x2,y2 in y["path"]])
        parent, rider = {"members":[y,whole]}, {"members":[x]}
        assert _rides(rider,parent), "attached control must ride"
        if case == "rider_gap":
            x["path"][2][1:] = _b(.55,.2,.04)
        else:
            # Endpoints remain readable; only the unobserved three-second interior rejects.
            x["path"] = [x["path"][0], *x["path"][3:]]
            y["path"] = [y["path"][0], y["path"][1], *y["path"][4:]]
            whole["path"] = [whole["path"][0],whole["path"][1],*whole["path"][4:]]
        assert not _rides(rider,parent), case
        return
    def chain(cam, ident, start, cls="e-heavy"):
        t = _t(cam,ident,cls,[(start,_b(.6,.4)),(start+1,_b(.5,.4)),(start+2,_b(.4,.4))],
               counted=True,hits=30,**kw)
        return dict(camera=cam,members=[t],**{"class":cls},app=((1.,0.),"m1"),
                    dep=start+2 if cam == "cam4" else None, arr=start if cam == "cam3" else None,
                    early=None,late=None)
    a,b = chain("cam4",1,100),chain("cam3",2,104)
    if case == "works_directionless":
        tracks = [a["members"][0],b["members"][0]]
        for direction in (None, "eastbound"):
            tracks[1]["direction"] = direction
            with patch(__name__ + ".PAIR_COS", 9), patch(__name__ + ".PAIR_HEAVY_COS", 9):
                expected = build(tracks,_CAMS,plant=[(0,200,200)])
            assert len(expected) == 2, "works must retain both lone cards"
            assert build(tracks,_CAMS,plant=[(0,200,200)]) == expected, (case,direction)
    elif case == "pair_mutual":
        c = chain("cam4",3,101)
        a["app"],c["app"] = ((.99,(1-.99**2)**.5),"m1"),((.8,.6),"m1")
        pairs = _pairs([a,c,b], _CAMS)
        assert pairs == [(a,b)], "only the arrival's best departure may pair"
    elif case == "pair_crossing":
        c,d = chain("cam4",3,101),chain("cam3",4,103)
        c["app"] = d["app"] = ((0.,1.),"m1")
        pairs = _pairs([a,b,c,d], _CAMS)
        assert len(pairs) == 1, "new leftover handoffs must not cross"
    elif case == "heavy_lower":
        b["arr"] = a["dep"] - 16
        assert not _pairs([a,b], _CAMS), "heavy zone gap below -15 s must be rejected"
    elif case == "cheap_before_crossing":
        b["app"] = ((0.,1.),"m1")
        with patch(__name__ + "._crosses", return_value=False) as crossing:
            assert not _pairs([a,b], _CAMS, [(a,b)])
            assert crossing.call_count == 0, "ineligible appearance must reject before crossing scan"


def _selfcheck_l2g(case):
    from unittest.mock import patch
    if case.startswith("ride_"):
        # A broad whole-truck box covers a smaller attached part, moving to the right.
        def member(name, born=0, end=4, off=-.08, lane=0, speed=.1, size=.04):
            return _t("cam4", name, "e-heavy",
                      [(t, _b(.3 + speed*t + off, .5 + lane, size)) for t in range(born,end+1)],
                      direction="southbound")
        y = member("tractor", off=0, size=.2)
        x = member("part", born=1)
        assert _rides({"members":[x]}, {"members":[y]}), "rider control"
        if case == "ride_letter":
            x["class"] = "b-light"
        elif case == "ride_birth":
            y = member("tractor", born=1, off=0, size=.2)
            x = member("part")
            x["path"][0][1:] = x["path"][1][1:]
        elif case == "ride_end":
            # A same-lane follower stays attached during overlap but outlives the tractor by 5 s.
            x = member("follower", born=1, end=9)
        elif case == "ride_direction":
            x["direction"] = "northbound"
        elif case == "ride_rows":
            y["path"], x["path"] = y["path"][2::2], x["path"][1::2]
            y["t0"] = x["t0"] = 2
        elif case == "ride_movement":
            y, x = member("tractor", off=0, size=.2, speed=.02), member("part", born=1, speed=.02)
        elif case == "ride_slip":
            x = member("part", born=1, speed=.07)
        elif case == "ride_behind":
            x = member("ahead", born=1, off=.08)
        elif case == "ride_lane":
            # Covered completely by a broad truck box, but in the adjacent lane.
            y["path"] = [[t,x1+.1,y1,x2-.1,y2] for t,x1,y1,x2,y2 in y["path"]]
            x = member("adjacent", born=1, lane=.15)
        assert not _rides({"members":[x]}, {"members":[y]}), case
        return

    def chain(cam, name, start, cls="e-heavy", cos=1, axis=0):
        vector = [0., 0., 0., 0.]
        vector[axis], vector[axis+1] = cos, (1-cos*cos)**.5
        t = _t(cam,name,cls,[(start,_b(.6,.4)),(start+1,_b(.5,.4)),(start+2,_b(.4,.4))],
               counted=True,hits=30,direction="southbound")
        return dict(camera=cam,members=[t],**{"class":cls},app=(tuple(vector),"m1"),
                    votes=t["votes"],conf=t["conf"],z=(start,start+2),
                    dep=start+2 if cam=="cam4" else None,arr=start if cam=="cam3" else None,
                    early=None,late=None)

    def pairs(cards, cameras=_CAMS, accepted=()):
        # Exercise the production caller as well: config and dropped-chain filtering live there.
        with patch(__name__ + "._chains", return_value=cards), patch(__name__ + "._links", return_value=list(accepted)), patch(__name__ + "._shadows", return_value={}):
            return _build([], cameras)[2]

    a,b = chain("cam4","a",100),chain("cam3","b",104)
    if case == "absorption_class":
        c = chain("cam4", "plant", 100, "e-plant")
        c["members"][0]["counted"] = False
        with patch(__name__ + "._rides", return_value=True) as riding:
            pairs([a,b,c], accepted=[(a,b)])
        assert not riding.called, "non-rider classes must not reach geometry checks"
    elif case == "pair_config":
        for cameras in ([dict(c, handoff=None) for c in _CAMS],
                        [dict(c,handoff={"camera":"cam5"}) for c in _CAMS]):
            assert not pairs([a,b],cameras), "leftover pair requires configured cam3/cam4 handoff"
    elif case == "pair_heading":
        cameras = [dict(c,heading="north" if c["name"]=="cam3" else "south") for c in _CAMS]
        a,b = chain("cam3","a",100,"b-light"),chain("cam4","b",104,"b-light")
        a["dep"],a["arr"],b["dep"],b["arr"] = 102,None,None,104
        assert pairs([a,b],cameras)==[(a,b)], "direction order must follow headings"
    elif case == "pair_same_heading":
        cameras = [dict(c,heading="south") for c in _CAMS]
        a,b = chain("cam4","a",100,"b-light"),chain("cam3","b",104,"b-light")
        assert not pairs([a,b],cameras), "configured handoff requires opposite headings"
    elif case == "pair_family":
        b["class"] = b["members"][0]["class"] = "c-bus"
        b["votes"] = b["members"][0]["votes"] = {"c-bus":30}
        assert not pairs([a,b]), "truck and bus are separate families"
    elif case == "pair_stub":
        b["arr"],b["stub_late"] = None,104
        assert pairs([a,b])==[(a,b)] and b["arr"]==104, "heavy stub arrival timing"
    elif case == "pair_dropped":
        b["members"][0]["counted"] = False
        cameras = [dict(c,lone_edge=False) for c in _CAMS]
        assert not pairs([a,b],cameras), "dropped chain must never pair"
    elif case == "pair_cos_order":
        a["app"] = ((.9,(1-.9**2)**.5,0.,0.),"m1")
        c,d = chain("cam4","c",101,axis=2),chain("cam3","d",95,axis=2)
        assert pairs([a,b,c,d])==[(c,d)], "higher cosine wins crossing new pairs"
    elif case == "pair_bus_crossing":
        a,b = chain("cam4","a",100,"c-bus"),chain("cam3","b",104,"c-bus")
        c,d = chain("cam4","c",101,"c-bus"),chain("cam3","d",103,"c-bus")
        assert pairs([a,b])==[(a,b)], "bus leftover control"
        assert pairs([a,b,c,d],accepted=[(c,d)])==[(c,d)], "bus leftover must not cross accepted bus link"
    elif case == "links_bus":
        for c in (a,b): c["class"] = "c-bus"
        c,d = chain("cam4","c",101,"c-bus"),chain("cam3","d",103,"c-bus")
        assert not _crosses(a,b,[(c,d)]), "ordinary links retain HEAVY-only crossing behaviour"
    elif case == "pair_parked_perf":
        # Counting cosines is deterministic and catches wide scans without a slow timing threshold.
        cards = [chain(cam,f"{cam}-{i}",i*40,"b-light") for cam in ("cam3","cam4") for i in range(1200)]
        parked = chain("cam3","parked",0,"b-light")
        parked["members"][0]["t1"] = 6*3600
        cards.append(parked)
        start = time.process_time()
        with patch(__name__ + "._similar", wraps=_similar) as cosine:
            pairs(cards)
        assert cosine.call_count < 5000, ("long parked card widened pairing scan",cosine.call_count)
        assert time.process_time()-start < 3, "long parked workload must stay fast"


def _selfcheck_pair_tie():
    from itertools import permutations

    def track(cam, name, start, axis):
        vector = [0.] * 32
        vector[axis] = 1.
        return _t(cam, name, "e-heavy", [(start+i, _b(.6-.1*i, .4)) for i in range(3)],
                  counted=True, hits=30, direction="southbound", app_v="m1",
                  app=base64.b64encode(struct.pack("<32e", *vector)).decode())

    # Orthogonal vehicles: each pair has cosine 1 and |gap| 4, but their handoffs cross.
    tracks = [track("cam4", "A", 100, 0), track("cam3", "A", 98, 0),
              track("cam4", "B", 98, 1), track("cam3", "B", 104, 1)]
    expected = build(tracks, _CAMS)
    assert len(expected) == 3 and any(
        {m["id"] for m in j["members"]} == {tracks[0]["id"], tracks[1]["id"]}
        for j in expected), "equal-score crossing pairs prefer stable departure/arrival member ids"
    for shuffled in permutations(tracks):
        assert build(list(shuffled), _CAMS) == expected, "input order must not change equal-score crossing pairs"
    _selfcheck_l2_freeze(tracks, expected)


def _selfcheck_reid():
    from unittest.mock import patch
    with patch.dict(REID_MISSED_HEAVY_COS, {"synthetic-v1": .5365007054807878}):
        _selfcheck_reid_cases()


def _selfcheck_reid_cases():
    from unittest.mock import patch
    from copy import deepcopy
    from itertools import permutations

    def chain(cam, name, start=100, cls="e-heavy", score=1.):
        v = [score, (1-score*score)**.5] + [0.] * 254
        t = _t(cam, name, cls, [(start, _b(.6,.4)), (start+1, _b(.5,.4)),
                               (start+2, _b(.4,.4))], counted=True, hits=30,
               direction="southbound", reid_v="synthetic-v1",
               reid=base64.b64encode(struct.pack("<256e", *v)).decode())
        return dict(camera=cam, members=[t], **{"class":cls}, app=None,
                    votes=t["votes"], conf=t["conf"], z=(start,start+2),
                    dep=start+2 if cam=="cam4" else None,
                    arr=start if cam=="cam3" else None, early=None, late=None)

    a,b = chain("cam4","a"),chain("cam3","b",104)
    assert _pairs([a,b], _CAMS) == [(a,b)], "re-ID rescues missed handoff without app"
    # Raw tracklets must carry re-ID through build, not just hand-built _pairs cards.
    for app in (False, True):
        tracks = deepcopy([a["members"][0], b["members"][0]])
        if app:
            for t, score in zip(tracks, (1., 0.)):
                t.update(app=base64.b64encode(struct.pack(
                    "<32e", score, 1-score, *[0.] * 30)).decode(), app_v="synthetic-app")
            assert _similar(*[_app([t]) for t in tracks]) < PAIR_HEAVY_COS
        without = [{k:v for k,v in t.items() if k not in ("reid", "reid_v")} for t in tracks]
        assert len(build(without, _CAMS)) == 2, "missed handoff control must require re-ID"
        expected = build(tracks, _CAMS)
        assert len(expected) == 1, "build lost re-ID missed heavy handoff"
        assert expected[0]["evidence"] == "handoff" and expected[0]["link"]["gap_s"] == 2
        assert {m["id"] for m in expected[0]["members"]} == {t["id"] for t in tracks}
        for ordered in permutations(tracks):
            assert build(list(ordered), _CAMS) == expected, "re-ID handoff input order"
        _selfcheck_l2_freeze(tracks, expected)

    convoy = [chain(cam, name, start)["members"][0]
              for cam, name, start in (("cam4", "convoy-a", 100), ("cam4", "convoy-b", 101),
                                       ("cam3", "convoy-c", 104), ("cam3", "convoy-d", 105))]
    expected = build(convoy, _CAMS)
    assert len(expected) == 4 and all(len(j["members"]) == 1 and not j["link"] for j in expected), \
        "identical re-ID codes must not collapse four ambiguous convoy trucks"
    assert {m["id"] for j in expected for m in j["members"]} == {t["id"] for t in convoy}
    for ordered in permutations(convoy):
        assert build(list(ordered), _CAMS) == expected, "re-ID convoy input order"

    for cls, threshold in (("e-heavy", .5365007054807878), ("c-bus", .5365007054807878)):
        for delta, want in ((-.002, False), (.002, True)):
            x,y = chain("cam4","x",cls=cls),chain("cam3","y",104,cls,threshold+delta)
            assert bool(_pairs([x,y], _CAMS)) == want, (cls,delta)
            for c in (x,y):
                c["members"][0]["reid_v"] = "reid-v1-clip-b32-laion2b-head-r1"
            assert bool(_pairs([x,y], _CAMS)) == want, ("current version",cls,delta)
    for field,value in (("reid_v","other"),("reid_v",None),("reid","bad"),("mixed",True)):
        x,y=deepcopy(a),deepcopy(b)
        y["members"][0][field]=value
        assert not _pairs([x,y],_CAMS), field
    for version in (None, "", "uncalibrated"):
        x,y=deepcopy(a),deepcopy(b)
        for c in (x,y): c["members"][0]["reid_v"]=version
        assert not _pairs([x,y],_CAMS), "both codes must have a calibrated version"
        if not version:
            assert not _reids(x["members"]), "missing/empty version must not decode"
    with patch.dict(REID_MISSED_HEAVY_COS, {"synthetic-v1": 1.}):
        assert _pairs([a,b],_CAMS), "exact threshold equality must pair"
    with patch.dict(REID_MISSED_HEAVY_COS, {"synthetic-v1": 1.000001}):
        assert not _pairs([a,b],_CAMS), "use the version's threshold"
    x,y=deepcopy(a),deepcopy(b)
    x["members"].append(dict(x["members"][0], id="lower-hit", hits=1))
    x["members"][0]["reid"] = chain("cam4","orthogonal",score=0)["members"][0]["reid"]
    assert _pairs([x,y],_CAMS), "MAX includes lower-hit members"
    for change in ("family","direction","window","config","heading"):
        x,y=deepcopy(a),deepcopy(b); cams=deepcopy(_CAMS)
        if change=="family": y["class"]="c-bus"
        if change=="direction":
            y["members"][0]["direction"]="northbound"
            y["dep"],y["arr"]=y["arr"],None
        if change=="window": y["arr"]=1000
        if change=="config": cams=[{k:v for k,v in c.items() if k!="handoff"} for c in cams]
        if change=="heading": cams[0]["heading"]="unknown"
        assert not _pairs([x,y],cams), change
    c=chain("cam3","competitor",105,score=.98)
    assert not _pairs([a,b,c],_CAMS), "strict margin rejects near ties"
    c=chain("cam4","competitor",101,score=.9)
    assert _pairs([a,b,c],_CAMS)==[(a,b)], "mutual best keeps strongest"
    # A below-threshold app score must not inflate a qualifying re-ID candidate.
    x=chain("cam4","score-parent")
    y=chain("cam3","weaker",104,score=.55)
    z=chain("cam3","stronger",105,score=.62)
    x["app"]=((1.,0.),"app")
    y["app"]=((.69,(1-.69**2)**.5),"app")
    z["app"]=((0.,1.),"app")
    assert _pairs([x,y,z],_CAMS)==[(x,z)], "only qualifying scores determine mutual-best margin"
    c,d=chain("cam4","c",101),chain("cam3","d",103)
    assert not _pairs([a,b],_CAMS,[(c,d)]), "accepted convoy order survives re-ID"

    for cls in ("a-small", "a-motorcycle", "b-light"):
        x,y=chain("cam4","x",cls=cls),chain("cam3","y",104,cls)
        assert not _pairs([x,y],_CAMS), "re-ID only rescues heavy/bus handoffs"
    codes=_reids(a["members"])
    assert _reid_sim(codes,codes) is None, "same-camera codes cannot support a handoff"
    weaker = _reids(chain("cam4", "weaker-member", score=.6)["members"])
    for members in (codes + weaker, weaker + codes):
        assert _reid_sim(members, _reids(b["members"])) == 1., "MAX over every compatible member"
    bad=dict(a["members"][0],reid=base64.b64encode(struct.pack("<256e", *([float("nan")]*256))).decode())
    assert not _reids([bad]), "nonfinite vectors abstain"
    bad["reid"]=base64.b64encode(bytes(512)).decode()
    assert not _reids([bad]), "zero vectors abstain"
    # Similarity is cosine even when half-precision storage changes the norm.
    scaled=dict(a["members"][0],reid=base64.b64encode(struct.pack("<256e", *([2.]+[0.]*255))).decode())
    assert abs(_reid_sim(_reids([scaled]),_reids(b["members"]))-1)<1e-9


def _selfcheck():
    _selfcheck_reid()
    _selfcheck_pair_tie()
    for case in ("ride_letter", "absorption_class", "ride_birth", "ride_end", "ride_direction", "ride_rows", "ride_movement", "ride_slip", "ride_behind", "ride_lane",
                 "pair_config", "pair_heading", "pair_same_heading", "pair_family", "pair_stub", "pair_dropped", "pair_cos_order", "pair_bus_crossing", "links_bus", "pair_parked_perf"):
        _selfcheck_l2g(case)
    for case in ("tail_cos", "tail_birth", "tail_end", "tail_hits", "rider_gap", "rider_continuity",
                 "works_directionless", "pair_mutual", "pair_crossing", "heavy_lower", "cheap_before_crossing"):
        _selfcheck_l2f(case)
    from unittest.mock import patch
    for case in ("small_low", "small_motorcycle", "small_pass", "general_cos",
                 "convoy_all", "convoy_miss_one", "convoy_miss_two", "crossing", "heavy_zone_window"):
        _selfcheck_l2e(case)
    for case in ("tail_medium", "tail", "tail_interpolated", "tail_minibus", "tail_car", "tail_convoy",
                 "rider", "rider_light", "broad_follower0", "broad_follower1", "broad_follower2", "broad_follower3",
                 "rider_sparse", "rider_uncovered", "rider_counted", "rider_reverse", "rider_freeze",
                 "pair", "pair_ambiguous", "pair_order", "pair_opposite", "pair_light_low", "pair_heavy_low",
                 "pair_window", "pair_family", "pair_heavy", "pair_directionless", "pair_north", "pair_freeze"):
        _selfcheck_l2(case)
    for case in ("road_train", "follower", "ends_early", "slower", "never_reaches", "interrupted",
                 "missing_samples", "no_overlap", "limit", "too_long", "partial_freeze", "order", "freeze",
                 "twin", "ghost", "joined_occluder", "early_vehicle", "ambiguous", "queue",
                 "visible_twin", "visible_ghost", "later_visible_join",
                 "early_twin", "early_ghost", "early_twin_real", "early_ghost_real", "later_early_join",
                 "predecessor", "heavy_predecessor"):
        _selfcheck_overtake(case)
    for case in ("tipper", "absent", "same_direction", "opposed", "opposed_motion", "unknown_direction", "other_camera", "small",
                 "no_box", "no_overlap", "smaller_heavy", "limit", "too_long", "freeze", "convoy", "backward", "level", "partial_freeze"):
        _selfcheck_occlusion(case)
    for case in ("ownership", "class", "margin", "similarity", "hits", "timing", "stub_convoy"):
        _selfcheck_sliver_guards(case)
    for case in ("stub", "sliver", "oct9_general_late"):
        _selfcheck_duplicate_cards(case)
    for case in ("null", "number", "string", "null_entry", "number_entry", "string_entry",
                 "short", "long", "non_numeric", "nan", "inf", "outside", "reversed", "flat", "moving_prefix", "moved_onto"):
        _selfcheck_phantom_regressions(case)
    for case in ("relay", "unconfigured", "queue", "freeze"):
        _selfcheck_phantoms(case)
    _selfcheck_ghost_decoy()
    _selfcheck_ghost_convoy()
    _selfcheck_ghost_reversal()
    _selfcheck_ghost_handoff()
    _selfcheck_contradictory_swap()
    _selfcheck_twin_permutations()
    for case in ("heavy_crossing", "minimum_similarity", "arrival_class", "same_class"):
        _selfcheck_swap_rejections(case)
    _selfcheck_shadow_swap()
    for case in ("heavy", "contradicting_appearance"):
        _selfcheck_shadow_competition(case)
    _selfcheck_ghost_permutations()
    _selfcheck_ghost_history()
    _selfcheck_ghost_endpoint()
    for case in ("start", "class", "end", "counted", "weak_parent", "directionless_parent", "too_far", "eligible"):
        _selfcheck_shadow_rejections(case)
    _selfcheck_shadow_links()
    for case in ("endpoint", "appearance", "shared_ghost"):
        _selfcheck_shadows(case)
    for case in ("end_escape", "works", "motion", "interior_escape", "weak_ghost", "refused_slot"):
        _selfcheck_chain_guards(case)
    for case in ("yield", "same_class", "weak_late", "strong_ordinary", "shared_late", "shared_late_three",
                 "uncontested_replacement", "ordinary_count_protection", "truck_wider_window",
                 "truck_swap_protection", "early_swap_protection", "truck_swap_crossing", "weak_truck_swap_crossing", "oct7_continuation",
                 "released_counted", "released_direction"):
        _selfcheck_late_priority(case)
    for case in ("frozen_unsplit", "speed", "events"):
        _selfcheck_review(case)
    for case in ("photos", "plant", "fragment", "fragment_works"):
        _selfcheck_reversal_findings(case)
    for case in ("link", "small", "straight", "short", "uturn", "freeze"):
        _selfcheck_reversal(case)
    _selfcheck_ghost()
    for case in ("link", "look", "away", "priority", "late", "heavy", "dep_hits", "arr_hits", "last_end", "truck_convoy"):
        _selfcheck_early(case)
    # (a) one northbound vehicle across the handoff: one journey, front from cam3, rear from cam4.
    a = _north(1, 100.0, k3={"plate": {"conf": 0.31, "crop": "cam3-1-plate.jpg"}})
    [j] = build(a, _CAMS, tz=timezone.utc)
    assert j["link"] == {"from": "cam3", "to": "cam4", "gap_s": 0.4} and j["evidence"] == "handoff", j
    assert j["crops"]["front"] == "cam3-1-best.jpg" and j["crops"]["rear"] == "cam4-1-best.jpg", j
    assert j["crops"]["front_plate"] == "cam3-1-plate.jpg" and j["plates"] == {"front": 0.31}, j
    assert "rear_plate" not in j["crops"] and j["camera"] == "cam3+cam4" and j["hits"] == 6, j
    assert j["ts"] == "1970-01-01T00:01:40+00:00" and j["bound"] == "toward_gate", j
    # Speed uses only clean members; old files and null speeds contribute nothing.
    legacy_id = "jny-c6f23aeb221245706e98c7e6"
    assert j["id"] == legacy_id, j
    assert j["speed_kph"] is None, "old tracklets have no speed key"
    [j] = build(_north(1, 100.0, k3={"speed_kph": 40.2}, k4={"speed_kph": 60.4}), _CAMS)
    assert j["speed_kph"] == 50.3 and j["id"] == legacy_id, j
    for flag in ("mixed", "hijacked"):
        [j] = build(_north(1, 100.0, k3={"speed_kph": 150.0, flag: True}, k4={"speed_kph": 60.4}), _CAMS)
        assert j["speed_kph"] == 60.4 and j["id"] == legacy_id, (flag, j)
        [j] = build(_north(1, 100.0, k3={"speed_kph": 150.0, flag: True}, k4={"speed_kph": None}), _CAMS)
        assert j["speed_kph"] is None, (flag, j)
    [j] = build(_north(1, 100.0, k3={"speed_kph": None}, k4={"speed_kph": 60.4}), _CAMS)
    assert j["speed_kph"] == 60.4, j
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
    # (b4) codes that disagree (cosine < APP_VETO) never link by day, even when timing says they should.
    lusaka = ZoneInfo("Africa/Lusaka")
    dt = datetime(2026, 10, 6, 12, 0, tzinfo=lusaka).timestamp()
    pair = _north(1, dt, k3={"app": code(1), "app_v": "m1", "counted": True},
                  k4={"app": code(-1), "app_v": "m1", "counted": True})
    assert len(build(pair, _CAMS, lusaka)) == 2
    # (b4n) ...but at night (here 20:00 Lusaka) a same-class pair is not vetoed: lit side vs headlights.
    nt = datetime(2026, 10, 6, 20, 0, tzinfo=lusaka).timestamp()
    pairn = _north(1, nt, k3={"app": code(1), "app_v": "m1", "counted": True}, k4={"app": code(-1), "app_v": "m1", "counted": True})
    assert len(build(pairn, _CAMS, lusaka)) == 1
    # (LOOK-LATE) A moving first fragment can link late; a stationary first fragment cannot.
    for moving, want in ((True, True), (False, False)):
        la = _t("cam3", 81, "c-small", [(dt + 8, _b(.5, .4)), (dt + 9, _b(.4 if moving else .5, .4))],
                app=code(1), app_v="m1", hits=6)
        lb = _t("cam3", 82, "c-small", [(dt + 10, _b(.4 if moving else .5, .4)), (dt + 11, _b(.3, .3))],
                app=code(1), app_v="m1", hits=4)
        ld = _t("cam4", 81, "c-small", [(dt - 2, _b(.45, .35)), (dt - 1, _b(.3, .5)), (dt, _b(.1, .7))],
                app=code(1), app_v="m1", counted=True, direction="southbound")
        _, lc, ll = _build([la, lb, ld], _CAMS, lusaka)
        assert len(lc) == 2 and bool(ll) == want, "LOOK-LATE"
        assert ("late" in next(c for c in lc if c["camera"] == "cam3")) == want, "LOOK-LATE"
    # (LOOK-LINK) A .89 look beats a .28 candidate 2 s closer; without codes timing wins.
    for coded, want in ((True, "obs-cam4-81"), (False, "obs-cam4-82")):
        def look(cosine):
            return {"app": code(cosine, (1 - cosine ** 2) ** .5), "app_v": "m1"} if coded else {}
        lp = _north(81, dt, gap=2.5, k3=look(1), k4=look(.89))
        lp += _north(82, dt, gap=.5, k4=look(.28))[1:]
        _, _, ll = _build(lp, _CAMS, lusaka)
        assert len(ll) == 1 and ll[0][1]["members"][0]["id"] == want, ("LOOK-LINK", coded)
    pair[0]["mixed"] = pair[1]["mixed"] = True        # ...unless a code may belong to a switched track
    assert len(build(pair, _CAMS)) == 1
    # (b5) a mixed member's crops lose to a clean member's.
    shot = lambda c: {"top": c + "-top.jpg", "best": c + "-best.jpg"}
    [j] = build(_north(1, 100.0, k3={"crops": shot("cam3-1"), "mixed": True}, k4={"crops": shot("cam4-1")}), _CAMS)
    assert j["crops"]["best"] == "cam4-1-top.jpg" and "front" not in j["crops"] and j["crops"]["rear"] == "cam4-1-best.jpg", j
    # (V1) a small car never links to a truck, however well timing and codes agree; two trucks do.
    k = lambda c: {"app": code(1), "app_v": "m1", "counted": True, "direction": "northbound"}
    for c3, c4, n in (("e-heavy", "a-small", 2), ("e-heavy", "e-heavy", 1)):
        [d] = [_t("cam4", 1, c4, [(100, _b(.45, .35)), (101, _b(.3, .5)), (102, _b(.1, .7))], hits=8, **k(0))]
        r = _t("cam3", 1, c3, [(102.5, _b(.9, .8)), (103.5, _b(.7, .6)), (104.5, _b(.5, .4))], hits=8, **k(0))
        assert len(build([d, r], _CAMS)) == n, (c3, c4)
    # (H1) A sits at P for 5 s, moves off; B is born on P 3 s later: A is hijacked, and its crops lose.
    P = _b(.5, .4)
    A = _t("cam3", 1, "e-heavy", [(100 + k, P) for k in range(6)] + [(106, _b(.7, .6))], direction="northbound")
    B = _t("cam3", 2, "e-heavy", [(109, P), (110, P)])
    assert _hijacked([A, B]).keys() == {"obs-cam3-1"}
    C = _t("cam3", 3, "e-heavy", [(106.5, _b(.7, .6)), (107.5, _b(.9, .8))])
    [j] = [j for j in build([A, B, C], _CAMS) if len(j["members"]) == 3] or build([A, B, C], _CAMS)[:1]
    assert j["crops"]["best"] in ("cam3-2-top.jpg", "cam3-3-top.jpg") and "hijacked" not in A, j
    # (H2) B born far from P: no. (H3) A never still for 2 s: no.
    assert not _hijacked([A, _t("cam3", 2, "e-heavy", [(109, _b(.1, .1)), (110, _b(.1, .1))])])
    assert not _hijacked([_t("cam3", 1, "e-heavy", [(100 + k, _b(.3 + .05 * k, .4)) for k in range(8)]), B])
    assert not _hijacked([{k: v for k, v in A.items() if k != "path"}, B]), "a node without images writes no path"
    # (H4) Maersk: P sits in cam4's zone, then drives off (it is M, counted); R is born on the parked box
    # and stays (a 0.04 drift): M is one journey with cam3's, R is no vehicle and takes no link.
    M = _north(1, 104.0)[0]
    P = _t("cam4", 1, "c-small", [(90 + k, _b(.1, .7)) for k in range(14)]
           + [(104, _b(.2, .6)), (105, _b(.3, .5)), (106, _b(.45, .35))], counted=True, direction="northbound")
    R = _t("cam4", 2, "c-small", [(108 + k, _b(.1, .7)) for k in range(6)] + [(114, _b(.14, .7))], direction="southbound")
    js = build([M, P, R], _CAMS)
    assert [sorted(m["id"] for m in j["members"]) for j in js] == [["obs-cam3-1", "obs-cam4-1"]], js
    # (H5) queue advance: A sits 3 s and moves off, B born on its spot moves off 2 s later: no trim, B not parked.
    A = _t("cam4", 1, "c-small", [(100 + k, _b(.1, .7)) for k in range(4)] + [(104, _b(.2, .6)), (105, _b(.3, .5))])
    B = _t("cam4", 2, "c-small", [(106 + k, _b(.1, .7)) for k in range(3)] + [(109, _b(.2, .6)), (110, _b(.3, .5))])
    assert _hijacked([A, B]).keys() == {"obs-cam4-1"}
    _, ch, _ = _build([A, B], _CAMS)
    assert [len(m["path"]) for c in ch for m in c["members"]] == [6, 5] and not any(m.get("parked") for c in ch for m in c["members"])
    assert any(c["arr"] == 106 for c in ch)
    # (H6) the re-park track drives off later (north: counted; south: into cam3) or B is a queue at a gate
    # (A stops 8 s and leaves, B first seen on the spot stops 8 s and leaves): photo flag only, as on main.
    for cnt in (False, True):
        R = _t("cam4", 2, "c-small", [(108 + k, _b(.1, .7)) for k in range(30)]
               + [(138, _b(.2, .6)), (139, _b(.3, .5)), (140, _b(.45, .35))], counted=cnt, direction="northbound")
        assert len(build([M, P, R], _CAMS)) == 2, cnt
        R = _t("cam4", 2, "c-small", [(108 + k, _b(.1, .7)) for k in range(30)] + [(138, _b(.08, .75)), (139, _b(.05, .78))], counted=cnt, direction="southbound")
        S = _t("cam3", 5, "c-small", [(139.5, _b(.9, .8)), (140.5, _b(.7, .6)), (141.5, _b(.5, .4))], counted=cnt)
        assert len(build([M, P, R, S], _CAMS)) == 2, cnt
    A = _t("cam4", 1, "c-small", [(100 + k, _b(.1, .7)) for k in range(9)] + [(109, _b(.2, .6)), (110, _b(.3, .5)), (111, _b(.45, .35))])
    B = _t("cam4", 2, "c-small", [(112 + k, _b(.1, .7)) for k in range(9)] + [(121, _b(.2, .6)), (122, _b(.3, .5)), (123, _b(.45, .35))])
    _, ch, _ = _build([A, B], _CAMS)
    assert not any(m.get("parked") for c in ch for m in c["members"]) and sum(c["arr"] is not None for c in ch) == 2
    # (m3) a stopped e-heavy truck never swallows the d-medium one queued 1.5 s behind it.
    st = _t("cam3", 1, "e-heavy", [(100 + k, _b(.5, .4)) for k in range(5)])
    assert len(_chains([st, _t("cam3", 2, "d-medium", [(105.5, _b(.5, .62)), (106.5, _b(.5, .62))])], _CAMS)) == 2
    # (O) minibuses passing at cam3's right edge: northbound A ends at x .93, southbound B starts at .98
    # 0.5 s later barely moving: two chains, whatever B's speed.
    na = _t("cam3", 1, "c-small", [(98, _b(.6, .5)), (99, _b(.78, .65)), (100, _b(.93, .8))], direction="northbound")
    sb = _t("cam3", 2, "c-small", [(100.5, _b(.98, .8)), (101.5, _b(.98, .8)), (103, _b(.7, .6))], direction="southbound")
    assert len(_chains([na, sb], _CAMS)) == 2
    assert len(_chains([na, dict(sb, direction=None)], _CAMS)) == 1
    # (W) a tanker's headlit cab (A, read light but with heavy votes) is covered by the whole truck B born over it: one
    # chain; with no heavy votes it is a car beside the truck: two.
    ca = [[.84, .70, .99, .89], [.70, .69, .84, .87], [.54, .67, .66, .82]]
    cb = [[.66, .68, .95, .88], [.50, .65, .80, .86], [.37, .62, .64, .82]]
    for votes, n in (({"e-heavy": 4, "b-light": 8}, 1), ({"b-light": 12}, 2)):
        cabs = _t("cam3", 1, "b-light", list(zip((48, 49, 50), ca)), votes=votes, hits=12, **k(1))   # the real counts
        assert len(_chains([cabs, _t("cam3", 2, "e-heavy", list(zip((49.2, 50.2, 51.2), cb)), hits=37, **k(1))], _CAMS)) == n, votes
    # ...and a covered vehicle that built up more sightings than the truck is no fragment.
    cabs = _t("cam3", 1, "b-light", list(zip((48, 49, 50), ca)), votes={"e-heavy": 4, "b-light": 8}, hits=40, **k(1))
    assert len(_chains([cabs, _t("cam3", 2, "e-heavy", list(zip((49.2, 50.2, 51.2), cb)), hits=37, **k(1))], _CAMS)) == 2
    # (E) attrs come from clean members' events: the mixed member's bigger event is another vehicle's.
    ev = {"obs-cam3-1": {"class": "c-small", "hits": 9, "attrs": {"axles": 3}}, "obs-cam4-1": {"class": "c-small", "hits": 2, "attrs": {"axles": 6}}}
    [j] = build(_north(1, 100.0, k3={"mixed": True}), _CAMS, events=ev)
    assert j["attrs"] == {"axles": 6}, j
    # (c) a long truck southbound: its cam3 arrival starts 8 s before its cam4 departure ends.
    t4 = _t("cam4", 1, "e-heavy", [(100, _b(0.5, 0.4))] + [(t, _b(0.1, 0.7)) for t in range(101, 113)])
    t3 = _t("cam3", 1, "e-heavy", [(104, _b(0.9, 0.8)), (105, _b(0.9, 0.8)), (106, _b(0.88, 0.78)),
                                   (110, _b(0.5, 0.4))])
    [j] = build([t4, t3], _CAMS)
    assert j["link"] == {"from": "cam4", "to": "cam3", "gap_s": -8.0}, j
    # (d) a car leaves cam4 at the left edge, another enters there 1.5 s later: opposed, not stitched.
    out = _t("cam4", 1, "c-small", [(100, _b(0.4, 0.4)), (101, _b(0.25, 0.55)), (102, _b(0.1, 0.7))], hits=15, direction="southbound")
    inn = _t("cam4", 2, "c-small", [(103.5, _b(0.1, 0.7)), (104.5, _b(0.25, 0.55)), (105.5, _b(0.4, 0.4))],
             hits=15, direction="northbound")
    assert len(build([out, inn], _CAMS)) == 2
    # ...but a parked car's final box 0.2 s after the one before is jitter, not a heading.
    parked = _t("cam4", 3, "c-small", [(100, _b(0.1, 0.7)), (101, _b(0.1, 0.7)), (101.2, _b(0.105, 0.7))])
    assert _opposed(out, inn) and not _opposed(parked, _t("cam4", 4, "c-small", [
        (101.5, _b(0.1, 0.7)), (102.5, _b(0.05, 0.7))]))
    # (e) two fragments of one arriving car 0.4 s apart are one chain.
    f1 = _t("cam4", 1, "c-small", [(100, _b(0.1, 0.7)), (101, _b(0.2, 0.6))], hits=5, direction="northbound")
    f2 = _t("cam4", 2, "c-small", [(101.4, _b(0.22, 0.58)), (102.4, _b(0.35, 0.45))], hits=5)
    [j] = build([f1, f2], _CAMS)
    assert len(j["members"]) == 2 and j["evidence"] == "edge", j
    # A directionless fragment must not bridge opposite ways, by stitching or by twins.
    for twin in (False, True):
        times = ((100, 102), (101, 104), (103, 105)) if twin else ((100, 101), (101.5, 102), (102.2, 103))
        fragments = [_t("cam4", i, "c-small", [(t, _b(.1, .7)) for t in span], hits=5, direction=direction)
                     for i, span, direction in zip(range(3), times, ("southbound", None, "northbound"))]
        chains = _chains(fragments, _CAMS)
        assert len(chains) == 2 and all(len({t["direction"] for t in c["members"] if t["direction"]}) <= 1
                                       for c in chains), ("directionless bridge", "twins" if twin else "stitch")
    # A sparse-path ID switch must not steal the minibus link and send A to a later car.
    a = _t("cam3", 91, "b-minibuses", [(100, _b(.33, .66, .04)), (101, _b(.46, .70, .04)),
                                       (104.4, _b(.90, .84, .04))], hits=11, counted=True, direction="northbound",
           app=code(1, 0), app_v="m1")
    b = _t("cam3", 92, "b-minibuses", [(103.2, _b(.84, .79, .04)), (104.2, _b(.97, .80, .04))],
           hits=6, app=code(1, 0), app_v="m1")
    arrival = _north(93, 104.4, gap=0, cls="b-minibuses",
                     k4={"hits": 11, "app": code(1, 0), "app_v": "m1"})[1]
    car = _north(94, 104.4, gap=2, cls="a-small",
                 k4={"hits": 12, "counted": False, "app": code(.3, .954), "app_v": "m1"})[1]
    car["direction"] = None
    assert _similar(_app([a]), _app([car])) < LATE_COS
    js = build([a, b, arrival, car], _CAMS)
    j = next(j for j in js if any(m["id"] == a["id"] for m in j["members"]))
    assert {m["id"] for m in j["members"]} == {a["id"], b["id"], arrival["id"]}, ("shadow link decoy", j)
    assert len(js) == 1, ("shadow absorbed and car released", js)
    # (f) parked in the zone, jittering, never counted: nothing.
    park = _t("cam3", 1, "c-small", [(100 + k, _b(0.9 + k % 2 * 0.005, 0.8)) for k in range(6)], direction="southbound")
    assert build([park], _CAMS) == []
    # (g) one camera, counted on its line, never near a zone: a journey on the line's word.
    [j] = build([_t("cam3", 1, "c-small", [(100, _b(0.5, 0.2)), (101, _b(0.5, 0.4))], counted=True, direction="northbound")], _CAMS)
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
    g1 = _t("cam3", 1, "c-small", [(100, _b(0.5, 0.2)), (102, _b(0.5, 0.3))], counted=True, direction="northbound")
    g2 = _t("cam3", 2, "c-small", [(120, _b(0.5, 0.3)), (125, _b(0.5, 0.31))], ghost_of="obs-cam3-1")
    [j] = build([g1, g2], _CAMS)
    assert [m["id"] for m in j["members"]] == ["obs-cam3-1", "obs-cam3-2"] and j["dwell_s"] == 25.0, j
    # (k) an edge crossing alone needs MIN_EDGE_HITS: a 2-frame glare blip is not a vehicle.
    blip = [(100, _b(0.1, 0.7)), (101, _b(0.3, 0.5))]
    assert build([_t("cam4", 1, "c-small", blip, hits=2, direction="northbound")], _CAMS) == []
    assert len(build([_t("cam4", 1, "c-small", blip, hits=6, direction="northbound")], _CAMS)) == 1
    assert build([_t("cam3", 1, "c-small", [(100, _b(0.5, 0.2))], path=[], counted=True)], _CAMS) == []
    # (l) a far, fast car: 0.05 boxes 0.08 apart 0.4 s later overlap nothing, yet are one chain;
    # (m) not across a class jump; (n) not when B starts behind where A left off.
    fast = _t("cam3", 1, "a-small", [(100, _b(0.18, 0.64, 0.025)), (101, _b(0.26, 0.64, 0.025))])
    on = [(101.4, _b(0.34, 0.64, 0.025)), (102.4, _b(0.42, 0.64, 0.025))]
    assert len(_chains([fast, _t("cam3", 2, "a-small", on)], _CAMS)) == 1
    assert len(_chains([fast, _t("cam3", 2, "d-medium", on)], _CAMS)) == 2
    # (m2) ...except between two heavy classes: a truck's class flickering, not a size jump.
    for c1, c2, n in (("e-heavy", "d-medium", 1), ("a-small", "e-heavy", 2)):
        a = _t("cam3", 1, c1, [(100, _b(0.5, 0.4)), (101, _b(0.5, 0.5))])
        assert len(_chains([a, _t("cam3", 2, c2, [(102.5, _b(0.5, 0.7)), (103.5, _b(0.5, 0.8))])], _CAMS)) == n, (c1, c2)
    back = [(101.4, _b(0.20, 0.64, 0.025)), (102.4, _b(0.28, 0.64, 0.025))]
    assert len(_chains([fast, _t("cam3", 2, "a-small", back)], _CAMS)) == 2
    # (o) ByteTrack twins: two ids on one crawling vehicle at once are one journey;
    # (p) but two that touch where the second starts and have parted by the end are two.
    crawl = [(t, _b(0.1 + 0.02 * (t - 100), 0.7 - 0.02 * (t - 100))) for t in range(100, 111)]
    first = _t("cam4", 1, "c-small", crawl, counted=True, direction="northbound")
    assert len(build([first, _t("cam4", 2, "d-medium", crawl[3:], hits=8)], _CAMS)) == 1
    apart = [(t, _b(0.16 + 0.05 * (t - 103), 0.64)) for t in range(103, 111)]
    assert len(build([first, _t("cam4", 2, "c-small", apart, hits=8, direction="northbound")], _CAMS)) == 2
    # (q) a twin that dies mid-frame is no stitch anchor: the next vehicle starting on its
    # last box, same class and way, stays its own journey.
    after = [(105.5 + k, _b(0.2 + 0.05 * k, 0.6 - 0.05 * k)) for k in range(4)]
    assert len(build([first, _t("cam4", 2, "c-small", crawl[3:6]),
                      _t("cam4", 3, "c-small", after, counted=True, direction="northbound")], _CAMS)) == 2
    # (L) cam3 first sees a southbound vehicle 8 s after it left cam4, mid-frame and moving away
    # from its zone: it links on matching codes (L1) only; unlike codes (L2) or none (L3) leave two.
    for c3, c4, n in ((code(1), code(1), 1), (code(1), code(-1), 2), (None, None, 2)):
        k = lambda c: {"app": c, "app_v": "m1"} if c else {}
        out = _t("cam4", 1, "c-small", [(100, _b(0.45, 0.35)), (101, _b(0.3, 0.5)), (102, _b(0.1, 0.7))],
                 counted=True, direction="southbound", **k(c4))
        late = _t("cam3", 1, "c-small", [(110, _b(0.5, 0.4)), (111, _b(0.3, 0.3))], counted=True, hits=6, direction="southbound", **k(c3))
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
    # (L5) late appearance re-pairing must keep timing's two links, not strand two counted chains.
    late_pair = []
    for i, t, look in ((1, 100, (.5, 3 ** .5 / 2)), (2, 104, (1, 0))):
        late_pair.append(_t("cam4", i, "c-small", [(t - 3, _b(.45, .35)),
                         (t - 2, _b(.15, .65)), (t, _b(.05, .75))],
                            counted=True, hits=6, direction="southbound", app=code(*look), app_v="m1"))
    for i, t, look in ((1, 101, (1, 0)), (2, 110, (.8, -.6))):
        late_pair.append(_t("cam3", i, "c-small", [(t, _b(.5, .4)), (t + 1, _b(.3, .3))],
                            counted=True, hits=6, direction="southbound", app=code(*look), app_v="m1"))
    js, ch, ll = _build(late_pair, _CAMS, timezone.utc)
    assert len(ch) == 4 and [c["z"] for c in ch if c["camera"] == "cam4"] == [(98, 100), (102, 104)], "L5 fixture"
    assert [c.get("late") for c in ch if c["camera"] == "cam3"] == [101, 110], "L5 fixture"
    assert len(js) == 2 and len(ll) == 2, ("L5", APP_PEN, len(js), len(ll))
    # (C) a slow truck's cam3 arrival 10 s after its cam4 departure (past LAG): linked only on like codes (C1);
    # a convoy never crosses (C2); cars are not widened (C3).
    k = lambda c: {"app": code(*c), "app_v": "m1"}
    dep4 = lambda i, t, cls, c: _t("cam4", i, cls, [(t, _b(.45, .35)), (t + 1, _b(.3, .5)), (t + 2, _b(.1, .7))],
                                   counted=True, hits=8, direction="southbound", **k(c))
    arr3 = lambda i, t, cls, c: _t("cam3", i, cls, [(t, _b(.9, .8)), (t + 1, _b(.7, .6)), (t + 2, _b(.5, .4))],
                                   counted=True, hits=8, direction="southbound", **k(c))
    for c, n in (((1,), 1), ((0, 1), 2), ((.5, .866), 2), ((.8, .6), 1)):
        assert len(build([dep4(1, 100, "e-heavy", (1,)), arr3(1, 112, "e-heavy", c)], _CAMS)) == n, c
    # Preserve the ordinary linker's class-specific candidate window.
    with patch(__name__ + ".PAIR_COS", 9), patch(__name__ + ".PAIR_HEAVY_COS", 9):
        assert len(build([dep4(1, 100, "c-small", (1,)), arr3(1, 112, "c-small", (1,))], _CAMS)) == 2
    assert len(build([dep4(1, 100, "c-small", (1,)), arr3(1, 112, "c-small", (1,))], _CAMS)) == 1
    assert len(build([dep4(1, 100, "c-small", (1,)), arr3(1, 112, "c-small", (.8, .6))], _CAMS)) == 2
    # A leaves first, B 3 s later; B's own arrival B' (0.5 s after B leaves) is linked by pass 1; A's only
    # arrival A' lands 10 s after A, after B' (0.5 s beyond B's 105 + 2 s exit): linking A->A' would invert.
    A, B = dep4(1, 100, "e-heavy", (1,)), dep4(2, 103, "e-heavy", (0, 1))
    Bp, Ap = arr3(2, 105.5, "e-heavy", (0, 1)), arr3(1, 112, "e-heavy", (1,))
    # Preserve the ordinary linker's prohibition on inverted arrival order.
    with patch(__name__ + ".PAIR_COS", 9), patch(__name__ + ".PAIR_HEAVY_COS", 9):
        assert ids(build([A, B, Ap, Bp], _CAMS)) == [["obs-cam3-1"], ["obs-cam3-2", "obs-cam4-2"], ["obs-cam4-1"]]
    assert ids(build([A, B, Ap, Bp], _CAMS)) == [["obs-cam3-1"], ["obs-cam3-2", "obs-cam4-2"], ["obs-cam4-1"]], "heavy leftovers cannot invert accepted convoy order"
    assert ids(build([A, B, dict(Ap, app=code(.7, 0, .51 ** .5)), Bp], _CAMS)) == [
        ["obs-cam3-1"], ["obs-cam3-2", "obs-cam4-2"], ["obs-cam4-1"]], "inverted leftovers need heavy pair cosine"
    # Slow convoy: A exits 102, B 106; A' enters 113, B' 115, all alike. Cheapest-first would pair B->A'
    # and strand both; the order-preserving selection links A+A' and B+B'.
    A, B, Ap, Bp = dep4(1, 100, "e-heavy", (1,)), dep4(2, 104, "e-heavy", (1,)), \
        arr3(1, 113, "e-heavy", (1,)), arr3(2, 115, "e-heavy", (1,))
    assert ids(build([A, B, Ap, Bp], _CAMS)) == [["obs-cam3-1", "obs-cam4-1"], ["obs-cam3-2", "obs-cam4-2"]]
    # A chain that moves inside its zone (dep and arr) is in one journey, not one per direction.
    k3 = dep4(1, 90, "e-heavy", (1,))
    k3.update(camera="cam3", id="obs-cam3-1", path=[[90, *_b(.5, .4)], [91, *_b(.7, .6)], [92, *_b(.9, .8)]])
    mid = _t("cam4", 9, "e-heavy", [(100, _b(.05, .6)), (101, _b(.1, .7)), (102, _b(.15, .8))], counted=True, hits=8, direction="southbound", **k((1,)))
    js = build([k3, mid, arr3(2, 110, "e-heavy", (1,))], _CAMS)
    assert sum("obs-cam4-9" in (m["id"] for m in j["members"]) for j in js) == 1, ids(js)
    # (j) a rebuild, in any input order, yields the same ids.
    assert [j["id"] for j in build(q, _CAMS)] == [j["id"] for j in build(q[::-1], _CAMS)]
    # (D) lone chains: no direction is no passage; lone_edge: false drops an uncounted edge-only one (not a
    # counted one); a linked pair needs no direction on both members.
    nd = _t("cam3", 1, "c-small", [(100, _b(0.5, 0.2)), (101, _b(0.5, 0.4))], counted=True)
    assert build([nd], _CAMS) == [] and len(build([dict(nd, direction="northbound")], _CAMS)) == 1
    edge = _t("cam3", 1, "c-small", [(100, _b(0.5, 0.4)), (101, _b(0.7, 0.6)), (102, _b(0.9, 0.8))], hits=8, direction="northbound")
    nolone = [dict(c, lone_edge=False) if c["name"] == "cam3" else c for c in _CAMS]
    assert len(build([edge], _CAMS)) == 1 and build([edge], nolone) == []
    assert len(build([dict(edge, counted=True)], nolone)) == 1
    assert [d.get("dropped") for d in build([nd], _CAMS, with_dropped=True)] == [True] and build([nd], _CAMS) == []
    # (W) works mode: 3 plant tracks of 20+ hits (or one of 150+) within 900 s of a chain keep the lone
    # chains the filter drops; plant never rescues itself, a blip is no rescue, a long stay covers its length.
    pln = lambda i, t0, hits, t1=None, cls="e-plant": _t("cam3", 90 + i, cls, [(t0, _b(.5, .5)), (t1 or t0 + 1, _b(.5, .6))], hits=hits)
    three = lambda t0, hits=25: [pln(i, t0 - i, hits) for i in range(3)]
    nd = dict(nd, hits=25)
    kept = lambda ts, cams=_CAMS: any(nd["id"] in [m["id"] for m in j["members"]] for j in build([nd] + ts, cams))
    assert not kept([]) and kept(three(-200.0))
    assert not kept(three(-200.0, 10)) and not kept(three(-1900.0))
    assert not kept([pln(0, -200.0, 25)]) and not kept(three(-200.0)[:2]), "one or two plant tracks are a passing roller"
    assert kept([pln(0, -200.0, 150)]) and not kept([pln(0, -200.0, 149)])
    assert not kept([pln(0, -1900.0, 200)]) and kept([pln(0, -1900.0, 200, t1=-100.0)]), "stay covers t1 + W"
    edge = dict(edge, hits=25)
    assert build([edge], nolone) == [] and len(build([edge] + three(-200.0), nolone)) == 1
    thin = dict(nd, hits=19)
    assert build([thin] + three(-200.0), _CAMS) == [], "a 19-hit chain is not rescued"
    pc = _t("cam3", 1, "e-plant", [(100, _b(0.5, 0.2)), (101, _b(0.5, 0.4))], counted=True, hits=30)
    assert build([pc] + three(-200.0), _CAMS) == [], "an e-plant chain is not rescued by its own presence"
    assert len(build([nd], _CAMS, plant=[(-200.0, -199.0, 150)])) == 1, "plant passed in (frozen tracklets) counts"
    pr = _north(1, 100.0)
    assert len(build([dict(pr[0], direction=None), pr[1]], _CAMS)) == 1
    # A whole day at a gate: 10k passes, 20k tracklets, in windowed time rather than all pairs.
    start = time.process_time()
    assert len(build([t for i in range(10_000) for t in _north(i, 100.0 + 5 * i)], _CAMS)) == 10_000
    took = time.process_time() - start
    start = time.process_time()
    assert len(build([t for i in range(10_000) for t in _north(
        i, 100.0 + 5 * i, cls="b-minibuses", k4={"class": "b-light"})], _CAMS)) == 10_000
    cross_took = time.process_time() - start
    weak_took, indexed_took = _selfcheck_weak_arrival_perf()
    print("journeys self-check ok: handoff links front to rear, queues pair in order, long trucks "
          "link across the overlap, opposed fragments stay apart, parked vehicles never count, "
          "classes fuse by votes, weak ghosts do not veto continuations, opposite roots never bridge, "
          "refused joins leave slots free, eligible shadow swaps repair cross-class links without changing parent evidence, "
          "weak ghost prefixes recover unused handoffs and freeze together, calibrated phantoms trim relay heads and freeze stably, shared weak ghosts cannot poison roots, works skips shadows, glare blips drop, far fast fragments join, twins merge and anchor nothing, ids are stable, "
          "lone chains need a direction and honour lone_edge unless plant marks roadworks; "
          f"10k passes (20k tracklets) in {took:.1f} s; cross-class in {cross_took:.1f} s; weak arrivals in {weak_took:.1f} s; known-similarity weak/late in {indexed_took:.1f} s")


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
