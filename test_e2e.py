#!/usr/bin/env python3
"""End-to-end smoke: a real hive.py client against a real ops.py server.

Covers the one path unit tests cannot: enrollment over a live WebSocket, a
command fanned out to a connected node, applied by a real Recorder, acked, and
the ticket completing. Run manually: python3 test_e2e.py (binds port 8397).
"""

import os
import socket
import subprocess
import sys
import tempfile
import time

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = 8397
BASE = f"http://127.0.0.1:{PORT}"


def wait(pred, seconds, what):
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            r = pred()
            if r:
                return r
        except requests.RequestException:
            pass
        time.sleep(0.5)
    raise AssertionError(f"timed out waiting for {what}")


def main():
    state = tempfile.mkdtemp()
    # The console is a subprocess: its R2 env is this dict, not our os.environ.
    env = dict(os.environ, OPS_STATE=state, PORT=str(PORT),
               R2_ACCOUNT_ID="e2e-acct", R2_ACCESS_KEY_ID="e2e-ak",
               R2_SECRET_ACCESS_KEY="e2e-sk", R2_BUCKET="e2e-bucket")
    server = subprocess.Popen([sys.executable, os.path.join(ROOT, "ops.py")],
                              env=env, cwd=ROOT,
                              stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        wait(lambda: requests.get(f"{BASE}/api/fleet", timeout=2).ok, 20, "server up")

        raw = requests.post(f"{BASE}/api/tokens", json={"hive": "e2e"}).json()["token"]

        import hive
        import recorder
        rec = recorder.Recorder(
            [{"name": "fake", "ip": "127.0.0.1", "user": "u", "password": "p"}],
            tempfile.mkdtemp(), "e2e")
        cfg = {"ops": {"url": f"ws://127.0.0.1:{PORT}/ingest",
                       "token": raw, "hive": "e2e"}, "cameras": []}

        def record_status():
            return {"cameras": rec.status(),
                    "disks": [{"path": "/", "free_gb": 100.0, "total_gb": 200.0}],
                    "disk_free_gb": 100.0, "disk_total_gb": 200.0,
                    "offload": {"enabled": False}}

        h = hive.Hive(cfg, rec, record_status,
                      lambda ip, u, p: (None, "no camera"), lambda: ["127.0.0.1"])
        h.start()
        me = f"e2e/{socket.gethostname()}"

        # Enrollment: the node appears in the fleet, confirmed, with its camera.
        doc = wait(lambda: (d := requests.get(f"{BASE}/api/fleet").json())
                   and d["rollup"]["nodes"] == 1 and d, 30, "node enrolled")
        node = doc["hives"][0]["nodes"][0]
        assert node["key"] == me and node["confirmed"], node
        assert [c["name"] for c in node["cameras"]] == ["fake"], node["cameras"]

        # The console pushes its R2 creds unasked, right after the first beat.
        wait(lambda: (cfg.get("offload") or {}).get("access_key_id") == "e2e-ak",
             20, "offload creds pushed")
        creds = cfg["offload"]
        assert creds["account_id"] == "e2e-acct", creds
        assert creds["secret_access_key"] == "e2e-sk", creds
        assert creds["bucket"] == "e2e-bucket", creds
        assert not creds.get("enabled"), creds        # creds never switch offload on

        # Command round trip: start lands on the REAL recorder and acks.
        t = requests.post(f"{BASE}/api/command",
                          json={"scope": {"hive": "e2e"}, "action": "start",
                                "hours": 0.01}).json()
        tick = wait(lambda: (x := requests.get(f"{BASE}/api/tickets").json())
                    and next((f for f in x["finished"] + x["active"]
                              if f["id"] == t["id"]
                              and f["rows"][me]["state"] == "acked"), None),
                    20, "start acked")
        assert tick["rows"][me]["applied"] == ["fake"], tick["rows"][me]
        assert tick["rows"][me]["lag"] < 15, tick["rows"][me]
        assert rec.st["fake"]["desired"] is True and rec.st["fake"]["until"], \
            rec.st["fake"]
        assert tick["state"] == "done", tick

        # Stop: second ticket, recorder back to not-desired.
        t2 = requests.post(f"{BASE}/api/command",
                           json={"scope": {"hive": "e2e"}, "action": "stop"}).json()
        wait(lambda: (x := requests.get(f"{BASE}/api/tickets").json())
             and next((f for f in x["finished"]
                       if f["id"] == t2["id"]
                       and f["rows"][me]["state"] == "acked"), None),
             20, "stop acked")
        assert rec.st["fake"]["desired"] is False, rec.st["fake"]

        # The trail: both commands and both acks are audit rows.
        kinds = [a["kind"] for a in
                 requests.get(f"{BASE}/api/audit").json()["audit"]]
        assert kinds.count("command") == 2 and kinds.count("ack") == 2, kinds

        rec.stop()
        print("e2e smoke ok: enroll → command → real recorder → ack → ticket done")
    finally:
        server.terminate()
        server.wait(timeout=10)


if __name__ == "__main__":
    main()
