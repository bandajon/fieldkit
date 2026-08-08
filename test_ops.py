#!/usr/bin/env python3
"""Plain stdlib test runner for the ops console: python3 test_ops.py

State lives in a throwaway OPS_STATE dir and the clock is injected, so nothing here
touches a real deployment and nothing sleeps waiting for a heartbeat interval.
"""

import importlib
import json
import os
import queue
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

STATE = Path(tempfile.mkdtemp(prefix="ops_state_test_"))
os.environ["OPS_STATE"] = str(STATE)

import ops                                    # noqa: E402
from fastapi.testclient import TestClient     # noqa: E402

# Real epoch, not a fixed one: the cold-start replay keeps 24 h and would drop rows
# stamped in 2023.
T0 = time.time()
CLOCK = [T0]
ops.now = lambda: CLOCK[0]

FAILS = []


def check(name, fn):
    try:
        fn()
        print(f"ok   {name}")
    except Exception as e:
        FAILS.append(name)
        print(f"FAIL {name}: {type(e).__name__}: {e}")


def wait(pred, what, timeout=5):
    """Heartbeats cross a socket and land on a worker thread — poll, never assume."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


def beat(node, cams=None, disks=None, offload=None, coverage=None, seq=1,
         hive="kalambo", drift=0.0, token=""):
    b = {"type": "heartbeat", "token": token, "hive": hive, "node": node, "seq": seq,
         "sent": CLOCK[0] - drift, "ips": ["10.0.0.5"],
         "versions": {"fieldkit": "abc1234", "python": "3.12.1"},
         "coverage": coverage or {}, "log": [f"{node} log line"]}
    if cams is not None:
        b["record"] = {"cameras": cams, "disks": disks or [],
                       "offload": offload or {"enabled": False, "last_error": ""}}
    return b


def push(node, **kw):
    """Apply a heartbeat directly, no socket — for the state-machine tests."""
    b = beat(node, **kw)
    ops.on_heartbeat(b, CLOCK[0])
    return b


def rec(state="RECORDING"):
    return {"state": state, "until": None, "minutes": 12.0, "restarts": 0, "log": []}


def node_of(fleet, key):
    for h in fleet["hives"]:
        for n in h["nodes"]:
            if n["key"] == key:
                return n
    raise AssertionError(f"{key} not in fleet: {[h['hive'] for h in fleet['hives']]}")


client = TestClient(ops.app)


# --- enrollment ---------------------------------------------------------------

def token_round_trip():
    """Create → a heartbeat on that token is accepted → revoke → the next one is not."""
    created = client.post("/api/tokens", json={"hive": "kalambo"}).json()
    raw, tid = created["token"], created["id"]
    assert raw and "sha256" not in created.get("token", ""), created
    assert ops.TOKENS[tid]["sha256"] != raw, "raw token must never be stored"

    with client.websocket_connect("/ingest") as ws:
        ws.send_json(beat("gate", cams={"cam1": rec()}, token=raw))
        wait(lambda: "kalambo/gate" in ops.CONNS, "node to register")
    n = node_of(client.get("/api/fleet").json(), "kalambo/gate")
    assert n["state"] == "ok" and n["confirmed"] is True, n
    assert [c["name"] for c in n["cameras"]] == ["cam1"], n["cameras"]
    assert n["cameras"][0]["state"] == "RECORDING", n["cameras"]

    client.post(f"/api/tokens/{tid}/revoke")
    with client.websocket_connect("/ingest") as ws:
        ws.send_json(beat("gate", cams={"cam1": rec()}, token=raw))
        assert ws.receive_json() == {"type": "rejected", "reason": "revoked token"}
    # A revoked node must stay visible and say why, not silently drop off the board.
    wait(lambda: ops.NODES["kalambo/gate"]["revoked"], "node marked revoked")
    assert node_of(client.get("/api/fleet").json(), "kalambo/gate")["state"] == "revoked"


def wrong_hive_rejected():
    raw = client.post("/api/tokens", json={"hive": "kalambo"}).json()["token"]
    with client.websocket_connect("/ingest") as ws:
        ws.send_json(beat("stray", hive="othersite", cams={}, token=raw))
        assert "not enrolled for hive" in ws.receive_json()["reason"]


def unknown_token_rejected():
    with client.websocket_connect("/ingest") as ws:
        ws.send_json(beat("nobody", cams={}, token="not-a-real-token"))
        assert ws.receive_json() == {"type": "rejected", "reason": "unknown token"}


# --- health -------------------------------------------------------------------

def offline_after_three_misses():
    push("quiet", cams={"cam1": rec()})
    assert node_of(client.get("/api/fleet").json(), "kalambo/quiet")["state"] == "ok"
    CLOCK[0] = T0 + 2 * ops.HB_INTERVAL + 1        # two missed beats is still just late
    assert node_of(client.get("/api/fleet").json(), "kalambo/quiet")["state"] == "ok"
    CLOCK[0] = T0 + 3 * ops.HB_INTERVAL + 1
    n = node_of(client.get("/api/fleet").json(), "kalambo/quiet")
    assert n["state"] == "offline" and n["issues"][0] == "offline", n
    CLOCK[0] = T0


def drift_and_disks():
    disks = [{"path": "/rec", "free_gb": 100.0, "total_gb": 500.0}]
    push("drifty", cams={"cam1": rec()}, disks=disks, drift=9.0)
    CLOCK[0] = T0 + 3600
    disks = [{"path": "/rec", "free_gb": 20.0, "total_gb": 500.0}]   # 80 GB/h
    push("drifty", cams={"cam1": rec()}, disks=disks, drift=9.0, seq=2)
    n = node_of(client.get("/api/fleet").json(), "kalambo/drifty")
    assert n["write_gb_h"] == 80.0, n["write_gb_h"]
    assert n["disks"][0]["hours_left"] == 0.2, n["disks"]
    assert n["clock_drift"] == 9.0 and "clock drift" in n["issues"], n
    assert "disk low" in n["issues"], n["issues"]
    CLOCK[0] = T0
    # Below the drift threshold the UI gets null, so it renders nothing at all.
    push("steady", cams={"cam1": rec()}, drift=1.0)
    assert node_of(client.get("/api/fleet").json(), "kalambo/steady")["clock_drift"] is None


def hb_only_and_sort():
    push("bare")                    # heartbeat with no record payload at all
    n = node_of(client.get("/api/fleet").json(), "kalambo/bare")
    assert n["hb_only"] is True and n["issues"] == ["heartbeat only"], n
    # Worst node first, inside the hive and across hives.
    ranks = [x["sort"] for h in client.get("/api/fleet").json()["hives"] for x in h["nodes"]]
    assert ranks == sorted(ranks), ranks


# --- commands & tickets -------------------------------------------------------

def command_ack_lag():
    raw = client.post("/api/tokens", json={"hive": "kalambo"}).json()["token"]
    with client.websocket_connect("/ingest") as ws:
        ws.send_json(beat("live1", cams={"cam1": rec()}, token=raw))
        wait(lambda: "kalambo/live1" in ops.CONNS, "live1 to connect")
        t = client.post("/api/command", json={"scope": {"node": "live1"},
                                              "action": "start", "hours": 4}).json()
        assert t["rows"]["kalambo/live1"]["state"] == "waiting", t
        frame = ws.receive_json()
        assert frame == {"type": "command", "cmd_id": t["id"], "action": "start",
                         "cams": None, "hours": 4.0}, frame
        CLOCK[0] = T0 + 2.5
        ws.send_json({"type": "ack", "cmd_id": t["id"], "ok": True,
                      "applied": ["cam1"], "error": ""})
        wait(lambda: ops.TICKETS[t["id"]]["state"] == "done", "ticket to complete")
    row = client.get("/api/tickets").json()["finished"][0]["rows"]["kalambo/live1"]
    assert row["state"] == "acked" and row["lag"] == 2.5, row
    assert row["applied"] == ["cam1"], row
    assert "kalambo/live1" not in ops.DESIRED, ops.DESIRED
    CLOCK[0] = T0


def command_bad_input():
    for body, want in (({"action": "burn"}, 400), ({"action": "start", "hours": 9999}, 400),
                       ({"action": "start", "cams": "cam1"}, 400),
                       ({"action": "stop", "scope": {"hive": "nosuch"}}, 404)):
        assert client.post("/api/command", json=body).status_code == want, body


def desired_survives_offline():
    """An offline node's command waits in desired.json and lands on reconnect."""
    raw = client.post("/api/tokens", json={"hive": "kalambo"}).json()["token"]
    push("roamer", cams={"cam1": rec()})            # enrolled, but not connected
    t = client.post("/api/command", json={"scope": {"node": "roamer"},
                                          "action": "stop"}).json()
    assert t["rows"]["kalambo/roamer"]["state"] == "offline", t
    assert ops.DESIRED["kalambo/roamer"]["cmd_id"] == t["id"]
    assert ops._read_json("desired.json", {})["kalambo/roamer"]["action"] == "stop"

    with client.websocket_connect("/ingest") as ws:
        ws.send_json(beat("roamer", cams={"cam1": rec()}, seq=2, token=raw))
        assert ws.receive_json()["cmd_id"] == t["id"], "stored command not re-sent"
        wait(lambda: ops.TICKETS[t["id"]]["rows"]["kalambo/roamer"]["state"] == "waiting",
             "row to un-park")


class _FakeLoop:
    """Stands in for a node's event loop: runs the queued send inline."""

    def call_soon_threadsafe(self, fn, *a):
        fn(*a)


def escalation_raises_alert():
    push("stuck", cams={"cam1": rec()})
    # Connected, but it will never ack.
    ops.CONNS["kalambo/stuck"] = {"loop": _FakeLoop(), "q": queue.Queue()}
    try:
        tid = client.post("/api/command", json={"scope": {"node": "stuck"},
                                                "action": "start"}).json()["id"]
        rows = ops.TICKETS[tid]["rows"]        # the live ticket, not the JSON copy
        assert rows["kalambo/stuck"]["state"] == "waiting", rows
        CLOCK[0] = T0 + ops.ESCALATE_AFTER + 1
        ops.escalate(ops.now())
        assert rows["kalambo/stuck"]["state"] == "escalated", rows
        assert ops.ESCALATED["kalambo/stuck"] == tid
        assert "cmd_unacked" in ops.conditions(
            ops.node_view(ops.NODES["kalambo/stuck"], ops.now()))
    finally:
        del ops.CONNS["kalambo/stuck"]
        ops.ESCALATED.pop("kalambo/stuck", None)
        ops.DESIRED.pop("kalambo/stuck", None)
        CLOCK[0] = T0


def overlapping_commands_retry_the_newest():
    """Stop right after start: the retry must carry the stop, and a late ack for the
    superseded start must not erase it."""
    push("busy", cams={"cam1": rec()})
    sent = queue.Queue()
    ops.CONNS["kalambo/busy"] = {"loop": _FakeLoop(), "q": sent}
    try:
        post = lambda a: client.post("/api/command",  # noqa: E731
                                     json={"scope": {"node": "busy"}, "action": a}).json()["id"]
        first, second = post("start"), post("stop")
        assert ops.DESIRED["kalambo/busy"]["cmd_id"] == second, ops.DESIRED
        ops.on_ack("kalambo/busy", {"cmd_id": first, "ok": True, "applied": ["cam1"]})
        assert ops.DESIRED.get("kalambo/busy", {}).get("cmd_id") == second, \
            "a stale ack erased the newer undelivered command"
        while not sent.empty():
            sent.get_nowait()
        CLOCK[0] = T0 + ops.ESCALATE_AFTER + 1
        ops.escalate(ops.now())
        frames = []
        while not sent.empty():
            frames.append(sent.get_nowait())
        assert [f["cmd_id"] for f in frames] == [second], frames
        assert [f["action"] for f in frames] == ["stop"], frames
    finally:
        for d in (ops.CONNS, ops.ESCALATED, ops.DESIRED):
            d.pop("kalambo/busy", None)
        CLOCK[0] = T0


def superseded_ticket_retires_quietly():
    """The command a newer one replaced must not sit active forever, nor escalate into
    a cmd_unacked alert for something the operator deliberately overrode."""
    push("relay", cams={"cam1": rec()})
    ops.CONNS["kalambo/relay"] = {"loop": _FakeLoop(), "q": queue.Queue()}
    try:
        post = lambda a: client.post("/api/command",  # noqa: E731
                                     json={"scope": {"node": "relay"}, "action": a}).json()["id"]
        first, second = post("start"), post("stop")
        old = ops.TICKETS[first]
        assert old["rows"]["kalambo/relay"]["state"] == "superseded", old["rows"]
        assert old["state"] == "done", old["state"]      # its only row is terminal now
        active = [t["id"] for t in client.get("/api/fleet").json()["tickets"]]
        assert second in active and first not in active, active   # other tests own the rest

        CLOCK[0] = T0 + ops.ESCALATE_AFTER + 1
        ops.escalate(ops.now())
        assert old["rows"]["kalambo/relay"]["state"] == "superseded", "superseded row escalated"
        assert ops.ESCALATED.get("kalambo/relay") == second, ops.ESCALATED
        ops.on_ack("kalambo/relay", {"cmd_id": second, "ok": True, "applied": ["cam1"]})
        assert ops.TICKETS[second]["state"] == "done"
        assert "kalambo/relay" not in ops.ESCALATED
        assert "cmd_unacked" not in ops.conditions(
            ops.node_view(ops.NODES["kalambo/relay"], ops.now()))
    finally:
        for d in (ops.CONNS, ops.ESCALATED, ops.DESIRED):
            d.pop("kalambo/relay", None)
        CLOCK[0] = T0


# --- alerts -------------------------------------------------------------------

def alert_pending_firing_cleared():
    key = "kalambo/sick/cam_not_recording"
    for i in range(3):
        CLOCK[0] = T0 + i * 10
        push("sick", cams={"cam1": rec("STOPPED")}, seq=i + 1)
        ops.tick()
        a = ops.ALERTS[key]
        assert a["n"] == i + 1, a
        assert a["state"] == ("FIRING" if i == 2 else "PENDING"), (i, a)
    for i in range(2):
        CLOCK[0] = T0 + 40 + i * 10
        push("sick", cams={"cam1": rec()}, seq=4 + i)
        ops.tick()
        want = "CLEARED" if i == 1 else "FIRING"     # one good round is not enough
        assert ops.ALERTS[key]["state"] == want, (i, ops.ALERTS[key])
    assert ops.ALERTS[key]["cleared"] == CLOCK[0]
    kinds = [r["kind"] for r in client.get("/api/audit").json()["audit"]]
    assert "alert_fire" in kinds and "alert_clear" in kinds, kinds
    CLOCK[0] = T0


def pending_alert_that_resolves_leaves_nothing():
    """Two rounds of a blip must not leave a CLEARED row nobody ever saw fire."""
    push("blip", cams={"cam1": rec("RECONNECTING")})
    ops.tick()
    assert ops.ALERTS["kalambo/blip/cam_not_recording"]["state"] == "PENDING"
    for i in range(2):
        push("blip", cams={"cam1": rec()}, seq=2 + i)
        ops.tick()
    assert "kalambo/blip/cam_not_recording" not in ops.ALERTS, ops.ALERTS


def disk_critical_suppresses_components():
    args = dict(cams={"cam1": rec()},
                offload={"enabled": True, "last_error": "R2 403", "uploaded": 3})
    push("full", disks=[{"path": "/rec", "free_gb": 100.0, "total_gb": 500.0}], **args)
    CLOCK[0] = T0 + 3600
    push("full", disks=[{"path": "/rec", "free_gb": 20.0, "total_gb": 500.0}],
         seq=2, **args)
    conds = ops.conditions(ops.node_view(ops.NODES["kalambo/full"], ops.now()))
    assert "disk_critical" in conds, conds
    assert "disk_low" not in conds and "offload_failing" not in conds, conds

    # Same disk, offload switched off: the two component alerts come back.
    args["offload"] = {"enabled": False, "last_error": "R2 403"}
    push("full", disks=[{"path": "/rec", "free_gb": 15.0, "total_gb": 500.0}],
         seq=3, **args)
    conds = ops.conditions(ops.node_view(ops.NODES["kalambo/full"], ops.now()))
    assert "disk_critical" not in conds, conds
    assert "disk_low" in conds and "offload_failing" in conds, conds
    CLOCK[0] = T0


def coverage_gap_alert():
    push("gappy", cams={"cam1": rec()},
         coverage={"cam1": {"pct": 91.2, "gaps": [[T0 - 900, T0 - 300]]}})
    n = node_of(client.get("/api/fleet").json(), "kalambo/gappy")
    assert n["cameras"][0]["coverage_pct"] == 91.2 and len(n["cameras"][0]["gaps"]) == 1
    assert "coverage_gap" in ops.conditions(
        ops.node_view(ops.NODES["kalambo/gappy"], ops.now()))


# --- roster, audit, misc ------------------------------------------------------

def roster_remove_and_forget():
    raw = client.post("/api/tokens", json={"hive": "otherhive"}).json()
    push("temp", hive="otherhive", cams={"cam1": rec()})
    assert client.post("/api/roster/otherhive/temp/forget").status_code == 400, \
        "forget must refuse a node that was never revoked"
    assert client.post("/api/roster/otherhive/temp/remove").json()["ok"] is True
    assert "otherhive/temp" not in ops.NODES
    assert client.post("/api/roster/otherhive/temp/remove").status_code == 404

    push("temp2", hive="otherhive", cams={"cam1": rec()})
    ops.NODES["otherhive/temp2"]["revoked"] = True
    assert client.post("/api/roster/otherhive/temp2/forget").json()["ok"] is True
    assert "otherhive/temp2" not in ops.NODES
    assert raw["hive"] == "otherhive"


def rotate_replaces_token():
    a = client.post("/api/tokens", json={"hive": "rot"}).json()
    b = client.post(f"/api/tokens/{a['id']}/rotate").json()
    assert ops.TOKENS[a["id"]]["state"] == "revoked" and b["state"] == "active"
    assert b["token"] != a["token"] and b["replaces"] == a["id"]
    masked = {t["id"]: t for t in client.get("/api/tokens").json()["tokens"]}
    assert "sha256" not in masked[a["id"]] and masked[a["id"]]["mask"].startswith("…")


def audit_covers_commands_and_tokens():
    rows = client.get("/api/audit?limit=500").json()["audit"]
    kinds = {r["kind"] for r in rows}
    for want in ("token_create", "token_revoke", "token_rotate", "command", "ack",
                 "roster_remove", "roster_forget", "rejected"):
        assert want in kinds, f"{want} missing from audit: {sorted(kinds)}"
    assert rows[0]["t"] >= rows[-1]["t"], "audit must be newest first"


def snapshot_and_logs():
    import base64
    jpeg = b"\xff\xd8\xff" + b"x" * 40
    b = beat("snapper", cams={"cam1": rec()})
    b["snapshots"] = {"cam1": base64.b64encode(jpeg).decode()}
    ops.on_heartbeat(b, CLOCK[0])
    r = client.get("/api/snapshot/kalambo/snapper/cam1")
    assert r.status_code == 200 and r.content == jpeg
    assert float(r.headers["X-Snapshot-Age"]) >= 0
    assert client.get("/api/snapshot/kalambo/snapper/nope").status_code == 404
    # Snapshots must never reach the replay log — that file would become a photo album.
    lines = (STATE / "heartbeats.jsonl").read_text()
    assert "snapshots" not in lines, "snapshot bytes leaked into heartbeats.jsonl"
    assert client.get("/api/logs/kalambo/snapper").json()["node"] == ["snapper log line"]
    assert client.get("/api/logs/kalambo/ghost").status_code == 404


def fleet_rollup_and_stream():
    f = client.get("/api/fleet").json()
    r = f["rollup"]
    assert r["nodes"] == len(ops.NODES) and r["cams"] >= 1, r
    assert r["online"] + r["offline"] <= r["nodes"], r
    assert set(f["alerts"]) == {"firing", "pending", "cleared"}, f["alerts"]
    # The SSE route is a thin wrapper around this fan-out; driving it over HTTP would
    # mean holding an open stream, which is not what a test should own.
    q = queue.Queue(maxsize=10)
    ops.SUBSCRIBERS.append(q)
    try:
        ops.publish()
        assert json.loads(q.get_nowait())["rollup"]["nodes"] == r["nodes"]
    finally:
        ops.SUBSCRIBERS.remove(q)


# --- cold start ---------------------------------------------------------------

def cold_start_replay_is_unconfirmed():
    """Reload the module against the same state dir: the roster comes back dimmed."""
    live = {k: v["confirmed"] for k, v in ops.NODES.items()}
    assert any(live.values()), "nothing was confirmed before the restart"
    importlib.reload(ops)
    ops.now = lambda: CLOCK[0]
    assert ops.NODES, "replay rebuilt nothing"
    assert set(ops.NODES) >= {"kalambo/quiet", "kalambo/sick"}, sorted(ops.NODES)
    assert not any(n["confirmed"] for n in ops.NODES.values()), "replayed rows must be dim"
    assert ops.NODES["kalambo/sick"]["record"].get("cameras"), "camera state lost in replay"
    assert ops.TOKENS, "tokens must survive a restart"
    # Removed nodes stay removed — their heartbeats were purged with them.
    assert "otherhive/temp" not in ops.NODES
    ops.on_heartbeat(beat("quiet", cams={"cam1": rec()}, seq=9), CLOCK[0])
    assert ops.NODES["kalambo/quiet"]["confirmed"] is True, "a live beat must confirm"


for name, fn in list(globals().items()):
    if callable(fn) and not name.startswith("_") and fn.__module__ == "__main__" \
            and name not in ("check", "wait", "beat", "push", "rec", "node_of"):
        check(name, fn)

shutil.rmtree(STATE, ignore_errors=True)
print(f"\n{len(FAILS)} failed" if FAILS else "\nall ops checks passed")
sys.exit(1 if FAILS else 0)
