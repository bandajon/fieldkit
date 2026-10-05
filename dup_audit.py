"""Daily duplicate audit: counted journeys that are probably one vehicle counted twice.
A review queue, not a verdict: 61% of flags were true duplicates on the labelled sample."""
import base64
import json
import math
import struct
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta, timezone

# Measured on Katuba 5 Oct 15:00-15:30 (295 journeys, every suspect labelled by two reviewers).
LEFTOVER_S = 8.0       # pairs > 8 s apart were mostly different vehicles on the labelled sample
LEFTOVER_LOOK = 0.8    # ...and so were pairs whose look cosine was < 0.8
NO_LOOK_S = 10.0       # without a code only the same class within 10 s held up
FRAGMENT_S = 5.0       # a track that resumes > 5 s after the other ended was a new vehicle
FRAGMENT_DIST = 0.2    # ...and one that resumes > 0.2 frame away was too
FRAGMENT_LOOK = 0.7    # fragments of one vehicle look alike at the lower bar: same camera, same lighting
WINDOW_S = 60          # journeys whose track spans are further apart than this are never compared
SMALL, HEAVY = {"a-small"}, {"d-medium", "e-heavy", "f-abnormal"}


def clash(a, b):
    return (a in SMALL and b in HEAVY) or (b in SMALL and a in HEAVY)


def cos(a, b):
    c = [struct.unpack("<32e", base64.b64decode(t["app"])) if t.get("app") and not t.get("mixed") else None
         for t in (a, b)]
    return None if None in c else sum(x * y for x, y in zip(*c))


def epoch(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def centre(p):
    return (p[1] + p[3]) / 2, (p[2] + p[4]) / 2


def suspects(journeys, tracklets_by_id):
    js = sorted(journeys, key=lambda j: j["ts"])
    mem = [[tracklets_by_id[m["id"]] for m in j["members"] if m["id"] in tracklets_by_id] for j in js]
    # ponytail: a journey's ts is its first member's, but members span minutes, so window on the member span
    span = [(min(t["t0"] for t in m), max(t["t1"] for t in m)) if m else (epoch(j["ts"]),) * 2 for j, m in zip(js, mem)]
    ts = [epoch(j["ts"]) for j in js]
    longest = max((e - b for b, e in span), default=0)
    crop = lambda j: next((j["crops"][k] for k in ("best", "front", "rear") if (j.get("crops") or {}).get(k)), None)
    out, seen = [], set()
    for i, a in enumerate(js):
        if "+" in a["camera"] or not mem[i]:
            continue
        ta = max(mem[i], key=lambda t: t["hits"])
        first = min(mem[i], key=lambda t: t["t0"])
        for k in range(bisect_left(ts, ts[i] - WINDOW_S - longest), bisect_right(ts, ts[i] + WINDOW_S + longest)):
            b = js[k]
            if k == i or span[k][0] > span[i][1] + WINDOW_S or span[k][1] < span[i][0] - WINDOW_S or b["direction"] != a["direction"] or clash(a["class"], b["class"]):
                continue
            pair = frozenset((a["id"], b["id"]))
            if pair in seen:
                continue
            for tb in mem[k]:
                if tb["camera"] != ta["camera"]:
                    dt = min(abs(tb["t0"] - ta["t1"]), abs(ta["t0"] - tb["t1"]), abs(tb["t0"] - ta["t0"]))
                    c = cos(ta, tb)
                    if dt <= LEFTOVER_S and c is not None and c >= LEFTOVER_LOOK:
                        hit = ("leftover", "dt", dt, c)
                    elif c is None and "+" not in b["camera"] and a["class"] == b["class"] and dt <= NO_LOOK_S:
                        hit = ("no_look", "dt", dt, None)
                    else:
                        continue
                else:
                    gap = first["t0"] - tb["t1"]
                    if not (0 <= gap <= FRAGMENT_S and tb.get("path") and first.get("path")):
                        continue
                    c = cos(tb, first)
                    if c is None or c < FRAGMENT_LOOK or \
                            math.dist(centre(tb["path"][-1]), centre(first["path"][0])) > FRAGMENT_DIST:
                        continue
                    hit = ("fragment", "gap", gap, c)
                seen.add(pair)
                out.append({"kind": hit[0], "a": a["id"], "b": b["id"], hit[1]: round(hit[2], 1),
                            "look": None if hit[3] is None else round(hit[3], 2),
                            "a_ts": a["ts"], "b_ts": b["ts"], "a_camera": a["camera"], "b_camera": b["camera"],
                            "a_class": a["class"], "b_class": b["class"], "a_crop": crop(a), "b_crop": crop(b)})
                break
    return sorted(out, key=lambda s: s["a_ts"])


def run(cl, bucket, gate, day8):
    import selfloop
    day = datetime.strptime(day8, "%Y%m%d")
    near = [(day + timedelta(d)).strftime("%Y%m%d") for d in (0, -1, 1)]
    journeys = selfloop.r2_jsonl(cl, bucket, f"{selfloop.JOURNEYS}{gate}/{day8}/final/")
    tracklets = {t["id"]: t for d in near for t in selfloop.r2_jsonl(cl, bucket, f"{selfloop.TRACKLETS}{gate}/{d}/")}
    found = suspects(journeys, tracklets)
    by_kind = {k: sum(s["kind"] == k for s in found) for k in {s["kind"] for s in found}}
    at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    doc = {"gate": gate, "day": day8, "generated": at, "journeys": len(journeys), "suspects": found, "by_kind": by_kind}
    cl.put_object(Bucket=bucket, Key=f"fieldkit-audit/{gate}/{day8}/suspects.json",
                  Body=json.dumps(doc).encode(), ContentType="application/json")
    return {"day": day8, "journeys": len(journeys), "suspects": len(found), "by_kind": by_kind, "at": at}


if __name__ == "__main__":
    code = lambda v: base64.b64encode(struct.pack("<32e", *([v] + [0] * 31))).decode()
    tr = lambda i, cam, t0, t1, app=None, path=None: {"id": i, "camera": cam, "t0": t0, "t1": t1, "hits": 10,
                                                      "app": app, "path": path or [[t0, .5, .5, .5, .5], [t1, .5, .5, .5, .5]]}
    jn = lambda i, ts, cam, mem, cls="b-car": {"id": i, "ts": ts, "camera": cam, "class": cls, "direction": "in",
                                              "members": [{"id": m} for m in mem], "crops": {"best": i + ".jpg"}}
    def check(js, trs):
        return suspects(js, {t["id"]: t for t in trs})
    l = [tr("a", "cam3", 100, 105, code(1)), tr("b", "cam4", 103, 108, code(1))]
    pair = lambda cb="b-car", b0=103: ([jn("A", "2026-10-05T15:00:00Z", "cam3", ["a"]),
                                        jn("B", "2026-10-05T15:00:03Z", "cam4", ["b"], cb)], b0)
    js, _ = pair()
    s = check(js, l)
    assert [(x["kind"], x["a"], x["b"], x["dt"]) for x in s] == [("leftover", "A", "B", 2)] and s[0]["a_crop"] == "A.jpg", s
    assert not check(js, [l[0], tr("b", "cam4", 118, 123, code(1))])                       # 15 s apart
    js, _ = pair("e-heavy")
    js[0]["class"] = "a-small"
    assert not check(js, l)                                                                 # class clash
    js, _ = pair()
    assert [x["kind"] for x in check(js, [tr("a", "cam3", 100, 105), tr("b", "cam4", 103, 108)])] == ["no_look"]
    js = [jn("A", "2026-10-05T15:00:10Z", "cam3", ["a"]), jn("B", "2026-10-05T15:00:00Z", "cam3", ["b"])]
    s = check(js, [tr("a", "cam3", 103, 110, code(1)), tr("b", "cam3", 95, 100, code(1))])
    assert [(x["kind"], x["gap"]) for x in s] == [("fragment", 3.0)] and (s[0]["a"], s[0]["b"]) == ("A", "B"), s
    js = [jn("L", "2026-10-05T15:00:00Z", "cam3+cam4", ["a", "b"])]
    assert not check(js, l)                                                                 # own members never paired
    print("dup_audit self-check ok: leftover, no-look and fragment flagged; late, clashing and own-member pairs not")
