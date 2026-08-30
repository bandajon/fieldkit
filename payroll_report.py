#!/usr/bin/env python3
"""Per-curator labelling metrics for a pay period.

    python payroll_report.py        # last 7 days
    python payroll_report.py 14

Reads dataset/audit.jsonl + dataset/scores.jsonl, prints a table and writes
dataset/payroll-<today>.csv. It ships METRICS, not money: what a point of quality
is worth is the operator's call. A payment formula would look like

    # pay = RATE_ZMW_PER_APPROVAL * approvals * max(quality, QUALITY_FLOOR)
    # RATE_ZMW_PER_APPROVAL, QUALITY_FLOOR = 0.75, 0.5

and belongs in whatever the operator already uses for payroll.
"""

import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATASET = ROOT / "dataset"
MIN_SCORES = 3        # same honesty rule as the Progress panel
COLUMNS = ("curator", "approvals", "discards", "held", "released", "days", "active_hours",
           "per_hour", "golds_seen", "gold_accuracy", "reviews", "review_accuracy",
           "corrections", "quality")
IDLE_GAP = 600        # seconds between two decisions past which the curator was away


def pace(stamps):
    """Hours actually spent deciding, from the decisions' own timestamps: consecutive
    decisions closer than IDLE_GAP are one sitting, anything longer is a break. The
    figure to hold against hours invoiced. -> {days, active_hours, per_hour}
    ponytail: a curator who fires one decision every nine minutes counts as active
    all day; lower IDLE_GAP if that game ever gets played."""
    ts = sorted(t for t in (parse_ts(x) for x in stamps) if t)
    active = sum((b - a).total_seconds() for a, b in zip(ts, ts[1:])
                 if (b - a).total_seconds() <= IDLE_GAP)
    hours = round(active / 3600, 2)
    return {"days": len({t.date() for t in ts}), "active_hours": hours,
            "per_hour": round(len(ts) / hours) if hours >= 0.25 else None}


def parse_ts(v):
    try:
        return datetime.fromisoformat(str(v))
    except ValueError:
        return None


def blank():
    """held: approvals that landed in the holding pen instead of the training set.
    released: how many of those a reviewer has since cleared. The gap is work nobody
    has proved yet — pay for it at your own risk."""
    return {"approvals": 0, "discards": 0, "held": 0, "released": 0, "gold": [], "review": [],
            "corrections": {}, "stamps": []}


def rows(name, since):
    """Log lines newer than `since`; a torn line is skipped, a missing file is empty."""
    out = []
    try:
        lines = (DATASET / name).read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict) and str(r.get("ts", "")) >= since:   # iso-utc sorts as text
            out.append(r)
    return out


def mean(vals):
    return round(sum(vals) / len(vals), 3) if len(vals) >= MIN_SCORES else None


def show(v):
    return "insufficient sample" if v is None else f"{v:.3f}"


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 7
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    people = {}
    for r in rows("audit.jsonl", since):
        act = r.get("action")
        if act == "review" and r.get("released"):
            # A release credits the CURATOR whose held work cleared, not the reviewer.
            people.setdefault(r.get("curator") or "anon", blank())["released"] += 1
            continue
        if act not in ("approve", "discard"):
            continue          # a reviewer's own audit lines are not curation work
        if r.get("note"):
            continue          # a script's decisions (burst thinning) are nobody's hours
        p = people.setdefault(r.get("who") or "anon", blank())
        p["approvals" if act == "approve" else "discards"] += 1
        p["stamps"].append(r.get("ts"))
        if r.get("held"):
            p["held"] += 1
    for r in rows("scores.jsonl", since):
        who, kind = r.get("who") or "anon", r.get("kind")
        if kind in ("gold", "review") and isinstance(r.get("score"), (int, float)):
            people.setdefault(who, blank())
            people[who][kind].append(r["score"])
            for c in r.get("corrections") or []:       # per class: what to improve on
                cc = people[who]["corrections"]
                cc[c.get("class")] = cc.get(c.get("class"), 0) + 1

    table = []
    for who, p in sorted(people.items()):
        gold, review = mean(p["gold"]), mean(p["review"])
        both = [v for v in (gold, review) if v is not None]
        quality = round(sum(both) / len(both), 3) if both else None   # equal weight when both exist
        table.append({"curator": who, "approvals": p["approvals"], "discards": p["discards"],
                      "held": p["held"], "released": p["released"], **pace(p["stamps"]),
                      "golds_seen": len(p["gold"]), "gold_accuracy": gold,
                      "reviews": len(p["review"]), "review_accuracy": review,
                      "corrections": " ".join(f"{k}:{v}" for k, v in
                                              sorted(p["corrections"].items(), key=lambda kv: -kv[1])),
                      "quality": quality})
    if not table:
        sys.exit(f"no labelling logged in the last {days} day(s)")

    print(f"last {days} day(s), since {since}\n")
    print(f"{'curator':<16}{'appr':>6}{'disc':>6}{'held':>6}{'rel':>6}{'days':>5}{'hours':>7}{'/h':>5}"
          f"{'golds':>7}{'gold acc':>22}{'reviews':>9}{'review acc':>22}{'quality':>22}")
    for t in table:
        print(f"{t['curator']:<16}{t['approvals']:>6}{t['discards']:>6}{t['held']:>6}"
              f"{t['released']:>6}{t['days']:>5}{t['active_hours']:>7}{show(t['per_hour']) if t['per_hour'] is None else t['per_hour']:>5}"
              f"{t['golds_seen']:>7}"
              f"{show(t['gold_accuracy']):>22}{t['reviews']:>9}"
              f"{show(t['review_accuracy']):>22}{show(t['quality']):>22}"
              + (f"   corrections {t['corrections']}" if t['corrections'] else ""))

    out = DATASET / f"payroll-{datetime.now(timezone.utc).date().isoformat()}.csv"
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(table)
    print(f"\n{out}")


if __name__ == "__main__":
    main()
