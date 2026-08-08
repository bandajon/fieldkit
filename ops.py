#!/usr/bin/env python3
"""FIELDKIT HIVE OPS — fleet console for FieldKit nodes. Run: python ops.py

Nodes dial in over WS /ingest and keep the socket open (docs/HIVE_PROTOCOL.md).
This process holds fleet state in memory, persists what must survive a restart to
ops_state/, and serves the HTTP API the ops UI reads.
"""

import asyncio
import base64
import hashlib
import json
import os
import queue
import re
import secrets
import shutil
import threading
import time
from collections import Counter, deque
from pathlib import Path

import requests
import uvicorn
import yaml
from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent
STATE = Path(os.environ.get("OPS_STATE") or ROOT / "ops_state")
CONFIG_PATH = ROOT / "ops_config.yaml"
EXAMPLE_PATH = ROOT / "ops_config.example.yaml"
STATIC = ROOT / "static" / "ops"

HB_INTERVAL = 30            # protocol: node heartbeats every 30 s
OFFLINE_AFTER = 3 * HB_INTERVAL
DRIFT_S = 3                 # one-way delay makes anything tighter noise
MISS_PCT = 5.0
RTT_MS = 500
ESCALATE_AFTER = 300        # unacked while connected past this = someone must look
PERSIST_TICKS = 3           # ticks a condition must hold before the alert fires
CLEAR_TICKS = 2
RETAIN_S = 86400            # cleared alerts, and replayable heartbeats
NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")   # hive/node land in dict keys and URLs

# Worst first. A node's dot and its sort position both come from issues[0].
SEVERITY = ["revoked", "offline", "not recording", "heartbeat only",
            "disk low", "clock drift", "link degraded"]

now = time.time             # module-level so tests can inject a clock

CONFIG = {}
LOCK = threading.RLock()
NODES = {}          # "hive/node" -> node record (see blank_node)
TOKENS = {}         # token id -> {id, hive, sha256, created, state}
DESIRED = {}        # "hive/node" -> command frame, until that node acks it
TICKETS = {}        # ticket id -> ticket
ALERTS = {}         # "hive/node/cond" -> alert
SNAPS = {}          # "hive/node/cam" -> {"jpeg": bytes, "ts": float}
CONNS = {}          # "hive/node" -> {"loop": event loop, "q": asyncio.Queue}
SUBSCRIBERS = []    # one queue.Queue per open SSE client
AUDIT = deque(maxlen=2000)
ESCALATED = {}      # "hive/node" -> ticket id it has not acked in time
SEQ = [0]           # ticket counter


# --- persistence (plain files; no database, deliberately) ---

def _read_json(name, default):
    try:
        return json.loads((STATE / name).read_text())
    except (OSError, ValueError):
        return default


def _write_json(name, obj):
    (STATE / name).write_text(json.dumps(obj, indent=1))


def _append(name, row):
    with (STATE / name).open("a") as f:
        f.write(json.dumps(row) + "\n")


def audit(kind, **detail):
    row = {"t": now(), "kind": kind, **detail}
    AUDIT.appendleft(row)
    _append("audit.jsonl", row)
    print(f"audit {kind} {detail}", flush=True)
    return row


def notify(text):
    """Alert fire/clear webhook. Best effort — a dead webhook must not break alerting."""
    url = CONFIG.get("webhook_url") or ""
    if not url:
        return

    def post():
        try:
            requests.post(url, json={"text": text}, timeout=3)
        except requests.RequestException as e:
            print(f"webhook failed: {e}", flush=True)

    threading.Thread(target=post, daemon=True).start()


def publish():
    """SSE fan-out. ponytail: whole fleet per event, no diffing — a hive is tens of rows."""
    if not SUBSCRIBERS:
        return
    data = json.dumps(fleet())
    for q in list(SUBSCRIBERS):
        try:
            q.put_nowait(data)
        except queue.Full:
            pass        # a stalled browser must never block ingest


# --- tokens & enrollment ---

def _sha(raw):
    return hashlib.sha256((raw or "").encode()).hexdigest()


def create_token(hive):
    """Returns (id, raw). The raw value is shown once and never stored."""
    raw = secrets.token_urlsafe(24)
    n = 1
    while f"{n:03d}" in TOKENS:
        n += 1
    tid = f"{n:03d}"
    TOKENS[tid] = {"id": tid, "hive": hive, "sha256": _sha(raw),
                   "created": now(), "state": "active"}
    _write_json("tokens.json", TOKENS)
    audit("token_create", token=tid, hive=hive)
    return tid, raw


def check_token(hb):
    """(record, "") on success, (None, reason) on rejection."""
    got = _sha(hb.get("token"))
    rec = next((t for t in TOKENS.values() if secrets.compare_digest(t["sha256"], got)), None)
    if not rec:
        return None, "unknown token"
    if rec["state"] != "active":
        return None, "revoked token"
    if rec["hive"] != hb.get("hive"):
        return None, f"token is not enrolled for hive {hb.get('hive')!r}"
    return rec, ""


# --- heartbeats ---

def blank_node(hive, node):
    return {"hive": hive, "node": node, "confirmed": False, "revoked": False,
            "last_seen": 0.0, "seq": 0, "seq_first": None, "beats": 0,
            "clock_drift": 0.0, "ips": [], "versions": {}, "record": {},
            "coverage": {}, "log": [], "disks": [], "write_gb_h": None,
            "rtt_ms": None, "free_at": None}


def _write_rate(n, disks, ts):
    """Hours-left needs a fill rate, and the only one visible from here is free space
    shrinking between heartbeats. Offload frees space; those deltas count as zero."""
    cur = {d.get("path"): d.get("free_gb") for d in disks
           if d.get("path") and d.get("free_gb") is not None}
    prev = n.get("free_at")
    if prev and cur:
        dt = (ts - prev[0]) / 3600.0
        used = sum(max(0.0, prev[1][p] - f) for p, f in cur.items() if p in prev[1])
        if dt > 0:
            r = used / dt
            n["write_gb_h"] = round(r if n["write_gb_h"] is None
                                    else 0.7 * n["write_gb_h"] + 0.3 * r, 3)
    if cur:
        n["free_at"] = (ts, cur)


def on_heartbeat(hb, recv, confirmed=True, persist=True):
    """Fold one heartbeat into fleet state. Returns the node key."""
    key = f"{hb.get('hive') or ''}/{hb.get('node') or ''}"
    with LOCK:
        n = NODES.setdefault(key, blank_node(hb.get("hive") or "", hb.get("node") or ""))
        seq = int(hb.get("seq") or 0)
        if n["seq_first"] is None or seq < n["seq"]:   # first sight, or the node restarted
            n["seq_first"], n["beats"] = seq, 0
        n["beats"] += 1
        n["seq"] = seq
        n["last_seen"] = recv
        n["confirmed"] = n["confirmed"] or confirmed
        n["revoked"] = False
        n["clock_drift"] = round(recv - float(hb.get("sent") or recv), 2)
        n["ips"] = hb.get("ips") or []
        n["versions"] = hb.get("versions") or {}
        n["record"] = hb.get("record") or {}
        n["coverage"] = hb.get("coverage") or {}
        n["log"] = hb.get("log") or []
        disks = n["record"].get("disks") or []
        _write_rate(n, disks, recv)
        n["disks"] = disks
        for cam, b64 in (hb.get("snapshots") or {}).items():
            try:    # snapshots stay in memory — the one thing not worth surviving a restart
                SNAPS[f"{key}/{cam}"] = {"jpeg": base64.b64decode(b64), "ts": recv}
            except (ValueError, TypeError):
                pass
    if persist:
        row = {k: v for k, v in hb.items() if k not in ("snapshots", "token")}
        row["recv"] = recv
        _append("heartbeats.jsonl", row)
    return key


def replay():
    """Cold start: rebuild the last 24 h from heartbeats.jsonl, unconfirmed, and prune the
    file in the same pass. Replayed rows render dimmed until a live heartbeat lands."""
    p = STATE / "heartbeats.jsonl"
    if not p.exists():
        return 0
    cutoff, rows = now() - RETAIN_S, []
    for line in p.read_text().splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("recv", 0) >= cutoff:
            rows.append(r)
    for r in rows:
        on_heartbeat(r, r["recv"], confirmed=False, persist=False)
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return len(rows)


# --- health ---

def _hours_left(free_gb, rate):
    return round(free_gb / rate, 1) if rate and rate > 0 and free_gb is not None else None


def node_view(n, ts, modal_version=""):
    """One node in the shape the UI wants — see fleet() for the full contract."""
    key = f"{n['hive']}/{n['node']}"
    cams = []
    for name, c in (n["record"].get("cameras") or {}).items():
        cov = n["coverage"].get(name) or {}
        snap = SNAPS.get(f"{key}/{name}")
        cams.append({"name": name, "state": c.get("state", "?"), "until": c.get("until"),
                     "minutes": c.get("minutes", 0), "restarts": c.get("restarts", 0),
                     "coverage_pct": cov.get("pct"), "gaps": cov.get("gaps") or [],
                     "snapshot_age": round(ts - snap["ts"], 1) if snap else None,
                     "log": c.get("log") or []})
    cams.sort(key=lambda c: (c["state"] == "RECORDING", c["name"]))
    disks = [dict(d, hours_left=_hours_left(d.get("free_gb"), n["write_gb_h"]))
             for d in n["disks"]]
    hours = [d["hours_left"] for d in disks if d["hours_left"] is not None]
    worst = min(hours) if hours else None
    span = n["seq"] - (n["seq_first"] or 0) + 1
    miss = round(max(0.0, 100 * (1 - n["beats"] / span)), 1) if span > 0 else 0.0
    age = ts - n["last_seen"]
    version = n["versions"].get("fieldkit", "")

    issues = []
    if n["revoked"]:
        issues.append("revoked")
    if age > OFFLINE_AFTER:
        issues.append("offline")
    if any(c["state"] != "RECORDING" for c in cams):
        issues.append("not recording")
    if not n["record"]:
        issues.append("heartbeat only")
    if worst is not None and worst < CONFIG.get("disk_amber_hours", 6):
        issues.append("disk low")
    if abs(n["clock_drift"]) > DRIFT_S:
        issues.append("clock drift")
    if (n["rtt_ms"] or 0) > RTT_MS or miss > MISS_PCT:
        issues.append("link degraded")
    issues.sort(key=SEVERITY.index)

    state = ("revoked" if n["revoked"] else "offline" if age > OFFLINE_AFTER
             else "error" if "not recording" in issues else "warn" if issues else "ok")
    return {
        "key": key, "hive": n["hive"], "node": n["node"],
        "state": state, "issues": issues,
        "sort": SEVERITY.index(issues[0]) if issues else len(SEVERITY),
        "confirmed": n["confirmed"], "hb_only": not n["record"],
        "last_seen": n["last_seen"], "age_s": round(age, 1), "seq": n["seq"],
        # Rendered only when it matters: one-way delay makes small numbers meaningless.
        "clock_drift": n["clock_drift"] if abs(n["clock_drift"]) > DRIFT_S else None,
        # ponytail: rtt_ms stays null — the protocol has no console→node ping, so link
        # health rides on missed beats. Add a ping frame on both sides if RTT is wanted.
        "link": {"rtt_ms": n["rtt_ms"], "miss_pct": miss},
        "ips": n["ips"], "versions": n["versions"],
        "version_diverged": bool(version and modal_version and version != modal_version),
        "disks": disks, "worst_hours": worst, "write_gb_h": n["write_gb_h"],
        "offload": n["record"].get("offload") or {},
        "cameras": cams,
        "ticket": ESCALATED.get(key),
    }


def fleet():
    """The UI's single input — GET /api/fleet, and the payload of every SSE frame.

    {
      "now": epoch,
      "rollup": {"nodes", "online", "offline", "cams", "recording",
                 "alerts_firing", "alerts_pending", "tickets_active"},
      "hives": [{"hive", "state", "sort", "online", "nodes": [node, ...]}],  worst first
      "tickets": [ticket, ...],       active only; GET /api/tickets adds the finished ones
      "alerts": {"firing", "pending", "cleared"}
    }

    node = {
      "key": "hive/node", "hive", "node",
      "state": ok|warn|error|offline|revoked,          # the dot
      "issues": ["offline", "not recording", ...],     # worst first
      "sort": severity index (lower = worse),
      "confirmed": bool,        # false = replayed from disk, render dimmed
      "hb_only": bool,          # heartbeat carried no record payload at all
      "last_seen": epoch, "age_s": float, "seq": int,
      "clock_drift": float|null,                       # null unless |drift| > 3 s
      "link": {"rtt_ms": float|null, "miss_pct": float},
      "ips": [...], "versions": {...}, "version_diverged": bool,
      "disks": [{"path", "free_gb", "total_gb", "hours_left": float|null}],
      "worst_hours": float|null, "write_gb_h": float|null,
      "offload": {"enabled", "uploaded", "last_file", "last_error"},
      "cameras": [{"name", "state", "until", "minutes", "restarts", "coverage_pct",
                   "gaps": [[start, end], ...], "snapshot_age": seconds|null,
                   "log": [...]}],                     # worst first
      "ticket": "0142"|null,    # a command this node has not acked in time
    }

    ticket = {"id", "action", "scope", "cams", "hours", "issued", "state": active|done,
              "rows": {"hive/node": {"node", "hive", "state": waiting|acked|failed|
                                     offline|escalated, "lag": seconds|null,
                                     "error", "applied": [...]}}}
    """
    ts = now()
    with LOCK:
        seen = Counter(n["versions"].get("fieldkit", "") for n in NODES.values()
                       if n["versions"].get("fieldkit"))
        modal = seen.most_common(1)[0][0] if seen else ""
        views = [node_view(n, ts, modal) for n in NODES.values()]
        alerts = Counter(a["state"] for a in ALERTS.values())
        tickets = [t for t in TICKETS.values() if t["state"] == "active"]
    hives = {}
    for v in views:
        hives.setdefault(v["hive"], []).append(v)
    rows = []
    for hive, ns in hives.items():
        ns.sort(key=lambda v: (v["sort"], v["node"]))
        rows.append({"hive": hive, "nodes": ns, "sort": ns[0]["sort"], "state": ns[0]["state"],
                     "online": sum(1 for v in ns if v["state"] not in ("offline", "revoked"))})
    rows.sort(key=lambda r: (r["sort"], r["hive"]))
    cams = [c for v in views for c in v["cameras"]]
    return {
        "now": ts,
        "rollup": {"nodes": len(views),
                   "online": sum(1 for v in views if v["state"] not in ("offline", "revoked")),
                   "offline": sum(1 for v in views if v["state"] == "offline"),
                   "cams": len(cams),
                   "recording": sum(1 for c in cams if c["state"] == "RECORDING"),
                   "alerts_firing": alerts["FIRING"], "alerts_pending": alerts["PENDING"],
                   "tickets_active": len(tickets)},
        "hives": rows, "tickets": tickets,
        "alerts": {"firing": alerts["FIRING"], "pending": alerts["PENDING"],
                   "cleared": alerts["CLEARED"]},
    }


# --- alerts ---

def conditions(v):
    """cond -> operator-readable text, for one node view."""
    c = {}
    if v["state"] == "offline":
        c["node_silent"] = f"{v['key']}: silent for {int(v['age_s'])}s"
    bad = [x["name"] for x in v["cameras"] if x["state"] != "RECORDING"]
    if bad:
        c["cam_not_recording"] = f"{v['key']}: not recording — {', '.join(bad)}"
    off, worst = v["offload"], v["worst_hours"]
    low = worst is not None and worst < CONFIG.get("disk_amber_hours", 6)
    if low and off.get("enabled"):
        # Offload is running and the drive is STILL draining. This one is red, and it
        # already says everything the disk and offload alerts would have said apart.
        c["disk_critical"] = f"{v['key']}: {worst} h left with offload running"
    else:
        if low:
            c["disk_low"] = f"{v['key']}: worst drive {worst} h left"
        if off.get("last_error"):
            c["offload_failing"] = f"{v['key']}: offload failing — {off['last_error']}"
    if v["clock_drift"]:
        c["clock_drift"] = f"{v['key']}: clock off by {v['clock_drift']}s"
    gaps = sum(len(x["gaps"]) for x in v["cameras"])
    if gaps:
        c["coverage_gap"] = f"{v['key']}: {gaps} coverage gap(s) in the last 24 h"
    if v["ticket"]:
        c["cmd_unacked"] = f"{v['key']}: command {v['ticket']} unacked for 5 min"
    return c


def tick():
    """One evaluation round. Alert counters advance per tick, so a PENDING n/3 means three
    rounds of the same condition. Called from the sweeper and after every heartbeat."""
    ts = now()
    with LOCK:
        escalate(ts)
        live = set()
        for v in [node_view(n, ts) for n in list(NODES.values())]:
            for cond, text in conditions(v).items():
                key = f"{v['key']}/{cond}"
                live.add(key)
                a = ALERTS.get(key)
                if a is None or a["state"] == "CLEARED":
                    a = ALERTS[key] = {"key": key, "hive": v["hive"], "node": v["node"],
                                       "cond": cond, "state": "PENDING", "n": 0, "gone": 0,
                                       "first": ts, "cleared": None}
                a["text"], a["last"], a["gone"] = text, ts, 0
                if a["state"] == "PENDING":
                    a["n"] += 1
                    if a["n"] >= PERSIST_TICKS:
                        a["state"] = "FIRING"
                        audit("alert_fire", alert=key, text=text)
                        notify(f"FIRING — {text}")
        for a in list(ALERTS.values()):
            if a["key"] in live:
                continue
            if a["state"] == "CLEARED":
                if ts - a["cleared"] > RETAIN_S:
                    del ALERTS[a["key"]]
                continue
            a["gone"] += 1
            if a["gone"] < CLEAR_TICKS:
                continue
            if a["state"] == "PENDING":
                del ALERTS[a["key"]]      # never fired, so nothing to clear or announce
                continue
            a["state"], a["cleared"] = "CLEARED", ts
            audit("alert_clear", alert=a["key"], text=a["text"])
            notify(f"CLEARED — {a['text']}")
    publish()


def sweeper():
    while True:
        time.sleep(HB_INTERVAL)
        try:
            tick()
        except Exception as e:        # a bad round must not end alerting for the fleet
            print(f"sweeper: {type(e).__name__}: {e}", flush=True)


# --- commands & tickets ---

TERMINAL = ("acked", "failed", "superseded")   # row states that will never change again


def send(key, frame):
    """Queue a frame for a connected node. False if it isn't connected."""
    c = CONNS.get(key)
    if not c:
        return False
    c["loop"].call_soon_threadsafe(c["q"].put_nowait, frame)
    return True


def _finish(t):
    """Retire a ticket once no row can move again, keeping the last 20."""
    if any(r["state"] not in TERMINAL for r in t["rows"].values()):
        return
    t["state"], t["finished"] = "done", now()
    done = [x for x in TICKETS.values() if x["state"] == "done"]
    for old in sorted(done, key=lambda x: x["finished"])[:-20]:
        del TICKETS[old["id"]]


def supersede(key, tid):
    """A newer command for this node means the older one will never be acked — retire
    that row instead of leaving it to escalate into an alert nobody can action."""
    for t in list(TICKETS.values()):        # _finish may delete from TICKETS
        if t["state"] != "active" or t["id"] == tid:
            continue
        row = t["rows"].get(key)
        if row and row["state"] not in TERMINAL:
            row["state"] = "superseded"
            ESCALATED.pop(key, None)
            _finish(t)


def escalate(ts):
    """Unacked-while-connected past 5 min is a person's problem, not a retry's."""
    for t in TICKETS.values():
        if t["state"] != "active":
            continue
        for key, row in t["rows"].items():
            if row["state"] == "waiting" and ts - t["issued"] > ESCALATE_AFTER:
                row["state"] = "escalated"
                ESCALATED[key] = t["id"]
    # Re-send from DESIRED, not the ticket frame: two overlapping commands to one node
    # must retry the newest, or a stale start can undo the stop that followed it.
    for key, frame in DESIRED.items():
        if CONNS.get(key):
            send(key, frame)            # until acked; the node side is idempotent


def issue(action, scope, cams, hours):
    """Create a ticket, fan out to connected nodes, store desired state for the rest."""
    with LOCK:
        targets = [k for k, n in NODES.items()
                   if (not scope.get("hive") or n["hive"] == scope["hive"])
                   and (not scope.get("node") or n["node"] == scope["node"])
                   and not n["revoked"]]
        if not targets:
            raise HTTPException(404, "no enrolled nodes match that scope")
        SEQ[0] += 1
        tid = f"{SEQ[0]:04d}"
        frame = {"type": "command", "cmd_id": tid, "action": action,
                 "cams": cams, "hours": hours}
        rows = {}
        for k in targets:
            supersede(k, tid)           # this frame replaces whatever was pending
            DESIRED[k] = frame          # cleared on ack, so a reconnect re-delivers it
            n = NODES[k]
            rows[k] = {"hive": n["hive"], "node": n["node"], "lag": None,
                       "error": "", "applied": [],
                       "state": "waiting" if send(k, frame) else "offline"}
        _write_json("desired.json", DESIRED)
        t = TICKETS[tid] = {"id": tid, "action": action, "scope": scope, "cams": cams,
                            "hours": hours, "issued": now(), "state": "active",
                            "frame": frame, "rows": rows}
    audit("command", ticket=tid, action=action, scope=scope, cams=cams, hours=hours,
          nodes=sorted(rows))
    publish()
    return t


def deliver(key):
    """Reconnect: re-send whatever this node never acked, and un-park its ticket row."""
    frame = DESIRED.get(key)
    if not frame or not send(key, frame):
        return
    row = (TICKETS.get(frame["cmd_id"]) or {"rows": {}})["rows"].get(key)
    if row and row["state"] == "offline":
        row["state"] = "waiting"


def on_ack(key, msg):
    with LOCK:
        t = TICKETS.get(msg.get("cmd_id") or "")
        row = t and t["rows"].get(key)
        if not row:
            return
        row["state"] = "acked" if msg.get("ok") else "failed"
        row["lag"] = round(now() - t["issued"], 2)
        row["error"] = msg.get("error") or ""
        row["applied"] = msg.get("applied") or []
        # Only the ack for the CURRENT desired command clears it — an ack for a
        # superseded ticket must not erase a newer, still-undelivered command.
        if (DESIRED.get(key) or {}).get("cmd_id") == msg.get("cmd_id"):
            DESIRED.pop(key, None)
        _write_json("desired.json", DESIRED)
        ESCALATED.pop(key, None)
        _finish(t)
    audit("ack", ticket=t["id"], node=key, ok=bool(msg.get("ok")), lag=row["lag"],
          error=row["error"])
    publish()


def forget(key, purge_history):
    with LOCK:
        NODES.pop(key, None)
        DESIRED.pop(key, None)
        ESCALATED.pop(key, None)
        _write_json("desired.json", DESIRED)
        for k in [k for k in SNAPS if k.startswith(key + "/")]:
            del SNAPS[k]
        for k in [k for k in ALERTS if k.startswith(key + "/")]:
            del ALERTS[k]
    if not purge_history:
        return
    # Otherwise the next cold start replays it straight back onto the board.
    p = STATE / "heartbeats.jsonl"
    if p.exists():
        keep = [ln for ln in p.read_text().splitlines()
                if not ln.startswith("{") or _key_of(ln) != key]
        p.write_text("".join(ln + "\n" for ln in keep))


def _key_of(line):
    try:
        r = json.loads(line)
        return f"{r.get('hive') or ''}/{r.get('node') or ''}"
    except ValueError:
        return ""


# --- app ---

def boot():
    STATE.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():        # first boot, same bootstrap as app.py's config.yaml
        shutil.copyfile(EXAMPLE_PATH, CONFIG_PATH)
        print(f"created {CONFIG_PATH.name} from {EXAMPLE_PATH.name}", flush=True)
    CONFIG.clear()
    CONFIG.update(yaml.safe_load(CONFIG_PATH.read_text()) or {})
    TOKENS.update(_read_json("tokens.json", {}))
    DESIRED.update(_read_json("desired.json", {}))
    p = STATE / "audit.jsonl"
    if p.exists():
        for line in p.read_text().splitlines()[-AUDIT.maxlen:]:
            try:
                AUDIT.appendleft(json.loads(line))
            except ValueError:
                pass
    beats = replay()
    print(f"ops: {len(TOKENS)} token(s), {len(NODES)} node(s) from {beats} replayed "
          f"heartbeat(s)", flush=True)


boot()
app = FastAPI(title="FIELDKIT HIVE OPS")


@app.websocket("/ingest")
async def ingest(ws: WebSocket):
    await ws.accept()
    out, key, pump = asyncio.Queue(), None, None
    try:
        while True:
            msg = await ws.receive_json()
            kind = msg.get("type")
            if kind == "ack":
                if key:
                    await asyncio.to_thread(on_ack, key, msg)
                continue
            if kind != "heartbeat":
                continue
            rec, why = check_token(msg)
            hive, node = msg.get("hive") or "", msg.get("node") or ""
            if not (NAME.match(hive) and NAME.match(node)):
                rec, why = None, "hive and node must be 1-64 chars of letters, digits, . _ -"
            if not rec:
                k = f"{hive}/{node}"
                if k in NODES:      # an enrolled node going dark must say why, not vanish
                    NODES[k]["revoked"] = True
                    publish()
                await ws.send_json({"type": "rejected", "reason": why})
                audit("rejected", node=k, reason=why)
                break
            key = f"{hive}/{node}"
            CONNS[key] = {"loop": asyncio.get_running_loop(), "q": out}
            if pump is None:
                pump = asyncio.create_task(_pump(ws, out))
            await asyncio.to_thread(on_heartbeat, msg, now())
            await asyncio.to_thread(deliver, key)   # the node's apply is idempotent
            await asyncio.to_thread(publish)
            # Alerts advance on the sweeper's clock, not here: a chatty node must not
            # reach FIRING faster than a quiet one.
    except (WebSocketDisconnect, RuntimeError, ValueError):
        pass
    finally:
        if pump:
            pump.cancel()
        if key and CONNS.get(key, {}).get("q") is out:
            del CONNS[key]
        publish()


async def _pump(ws, q):
    """Commands are issued from HTTP worker threads; this task owns the socket's sends."""
    while True:
        await ws.send_json(await q.get())


@app.get("/api/fleet")
def api_fleet():
    return fleet()


@app.get("/api/stream")
def api_stream():
    q = queue.Queue(maxsize=100)
    SUBSCRIBERS.append(q)
    q.put_nowait(json.dumps(fleet()))

    def gen():
        try:
            while True:
                try:
                    yield f"data: {q.get(timeout=15)}\n\n"
                except queue.Empty:
                    yield ": ping\n\n"     # keep idle proxies from closing the stream
        finally:
            if q in SUBSCRIBERS:
                SUBSCRIBERS.remove(q)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/snapshot/{hive}/{node}/{cam}")
def api_snapshot(hive: str, node: str, cam: str):
    s = SNAPS.get(f"{hive}/{node}/{cam}")
    if not s:
        raise HTTPException(404, "no snapshot cached for that camera yet")
    return Response(content=s["jpeg"], media_type="image/jpeg",
                    headers={"X-Snapshot-Age": str(round(now() - s["ts"], 1))})


@app.post("/api/command")
def api_command(body: dict = Body(default={})):
    action = body.get("action")
    if action not in ("start", "stop"):
        raise HTTPException(400, "action must be 'start' or 'stop'")
    hours = body.get("hours")
    if hours is not None:
        try:
            hours = float(hours)
            if not 0 < hours <= 720:
                raise ValueError
        except (TypeError, ValueError):
            raise HTTPException(400, "hours must be a number between 0 and 720")
    cams = body.get("cams") or None
    if cams is not None and not (isinstance(cams, list) and all(isinstance(c, str) for c in cams)):
        raise HTTPException(400, "cams must be a list of camera names, or null for all")
    scope = body.get("scope") or {}
    if not isinstance(scope, dict):
        raise HTTPException(400, "scope must be an object with optional hive and node")
    return issue(action, {k: scope[k] for k in ("hive", "node") if scope.get(k)}, cams, hours)


@app.get("/api/tickets")
def api_tickets():
    with LOCK:
        ts = sorted(TICKETS.values(), key=lambda t: t["id"], reverse=True)
    return {"active": [t for t in ts if t["state"] == "active"],
            "finished": [t for t in ts if t["state"] != "active"][:20]}


@app.post("/api/tokens")
def api_token_create(body: dict = Body(default={})):
    hive = body.get("hive") or ""
    if not NAME.match(hive):
        raise HTTPException(400, "hive must be 1-64 chars of letters, digits, . _ or -")
    tid, raw = create_token(hive)
    # The only time the raw value exists outside the node's config.yaml.
    return {**TOKENS[tid], "token": raw}


def _token(tid):
    if tid not in TOKENS:
        raise HTTPException(404, f"no token {tid!r}")
    return TOKENS[tid]


@app.post("/api/tokens/{tid}/revoke")
def api_token_revoke(tid: str):
    t = _token(tid)
    t["state"] = "revoked"
    _write_json("tokens.json", TOKENS)
    audit("token_revoke", token=tid, hive=t["hive"])
    publish()
    return t


@app.post("/api/tokens/{tid}/rotate")
def api_token_rotate(tid: str):
    old = _token(tid)
    old["state"] = "revoked"
    new, raw = create_token(old["hive"])
    _write_json("tokens.json", TOKENS)
    audit("token_rotate", token=tid, replaced_by=new, hive=old["hive"])
    publish()
    return {**TOKENS[new], "token": raw, "replaces": tid}


@app.get("/api/tokens")
def api_tokens():
    roster = {}
    with LOCK:
        for k, n in NODES.items():
            roster.setdefault(n["hive"], []).append(
                {"node": n["node"], "key": k, "revoked": n["revoked"],
                 "confirmed": n["confirmed"], "last_seen": n["last_seen"],
                 "connected": k in CONNS})
    return {"tokens": [{**{x: t[x] for x in ("id", "hive", "created", "state")},
                        "mask": "…" + t["sha256"][:6]} for t in TOKENS.values()],
            "roster": roster}


@app.post("/api/roster/{hive}/{node}/remove")
def api_roster_remove(hive: str, node: str):
    """De-list. The token is still active, so the node re-enrols on its next heartbeat."""
    key = f"{hive}/{node}"
    if key not in NODES:
        raise HTTPException(404, f"no node {key!r} in the roster")
    forget(key, purge_history=True)
    audit("roster_remove", node=key)
    publish()
    return {"ok": True, "removed": key}


@app.post("/api/roster/{hive}/{node}/forget")
def api_roster_forget(hive: str, node: str):
    """Erase a node whose token was revoked — history included."""
    key = f"{hive}/{node}"
    n = NODES.get(key)
    if not n:
        raise HTTPException(404, f"no node {key!r} in the roster")
    if not n["revoked"]:
        raise HTTPException(400, "only a REVOKED node can be forgotten — revoke its token first")
    forget(key, purge_history=True)
    audit("roster_forget", node=key)
    publish()
    return {"ok": True, "forgotten": key}


@app.get("/api/alerts")
def api_alerts():
    with LOCK:
        rows = sorted(ALERTS.values(), key=lambda a: (a["state"] == "CLEARED", -a["last"]))
    return {"alerts": rows, "persist": PERSIST_TICKS}


@app.get("/api/audit")
def api_audit(limit: int = 200):
    return {"audit": list(AUDIT)[:max(1, min(limit, AUDIT.maxlen))]}


@app.get("/api/logs/{hive}/{node}")
def api_logs(hive: str, node: str):
    n = NODES.get(f"{hive}/{node}")
    if not n:
        raise HTTPException(404, "unknown node")
    cams = {c: v.get("log") or [] for c, v in (n["record"].get("cameras") or {}).items()}
    return {"node": n["log"], "cameras": cams, "last_seen": n["last_seen"]}


@app.get("/")
def index():
    p = STATIC / "index.html"
    if p.exists():
        return FileResponse(p)
    return {"app": "FIELDKIT HIVE OPS", "ui": "not built yet", "api": "/api/fleet"}


# The UI routes on real paths, so a reload or a pasted link must reach index.html.
# These sit above the StaticFiles mount, which would otherwise 404 them.
@app.get("/h/{path:path}")
@app.get("/alerts")
@app.get("/audit")
@app.get("/enroll")
def spa(path: str = ""):
    return index()


app.mount("/", StaticFiles(directory=STATIC), name="ops")


if __name__ == "__main__":
    threading.Thread(target=sweeper, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0",
                port=int(os.environ.get("PORT") or CONFIG.get("port") or 8090))
