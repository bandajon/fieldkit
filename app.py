#!/usr/bin/env python3
"""FieldKit — on-site capture & extraction console. Run: python app.py"""

import atexit
import ipaddress
import json
import os
import queue
import shutil
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import uvicorn
import yaml
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

import camera
import live
import nvr_pull
import recorder

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"

CONFIG = {}


def load_config():
    """Re-read config.yaml into the module-level CONFIG dict."""
    cfg = yaml.safe_load(CONFIG_PATH.read_text()) or {}   # parse first: a bad file must
    for k in ("cameras", "nvrs"):                         # not leave CONFIG empty, and an
        cfg[k] = cfg.get(k) or []                         # empty `cameras:` key is None
    CONFIG.clear()
    CONFIG.update(cfg)
    return CONFIG


def local_ips():
    """Non-loopback IPv4s of this host, stdlib only."""
    ips = set()
    try:
        for *_, addr in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(addr[0])
    except OSError:
        pass          # unresolvable hostname is common on a field LAN; not fatal
    # getaddrinfo alone misses the LAN address on macOS/Jetson; ask the routing table too.
    primary = ""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # no packets sent, just picks the default route
        primary = s.getsockname()[0]
        ips.add(primary)
    except OSError:
        pass
    finally:
        s.close()
    rest = sorted(ip for ip in ips if not ip.startswith("127.") and ip != primary)
    # Default route first: the UI derives the Set-IP /24 from ips[0], and a sorted list
    # would put a Tailscale 100.x address ahead of the site's 192.168.x.
    return ([primary] if primary else []) + rest


def record_dir():
    d = ROOT / CONFIG.get("record_dir", "./recordings")
    d.mkdir(parents=True, exist_ok=True)
    return d


load_config()
REC = recorder.Recorder(CONFIG.get("cameras", []), record_dir(), CONFIG.get("site", "site1"))
LIVE = live.Live(CONFIG.get("cameras", []), CONFIG.get("go2rtc_binary", ""), ROOT / "go2rtc.yaml")
LIVE.start()   # no-op unless a binary is configured and present


@atexit.register
def _shutdown():
    """Children spawned in their own process group survive Ctrl-C; an orphaned ffmpeg
    would then fight the restarted app for the same segment paths."""
    REC.stop()
    LIVE.stop()

app = FastAPI(title="FieldKit")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/status")
def status():
    return {
        "hostname": socket.gethostname(),
        "ips": local_ips(),
        "disk_free_gb": round(shutil.disk_usage(record_dir()).free / 1e9, 1),
        "time": datetime.now().isoformat(timespec="seconds"),
        "go2rtc": LIVE.info(),
    }


@app.get("/api/cameras")
def cameras_list():
    return [{"name": c["name"], "ip": c["ip"]} for c in CONFIG.get("cameras", [])]


@app.post("/api/live/start")
def live_start():
    return {"state": LIVE.start()}


@app.post("/api/live/stop")
def live_stop():
    return {"state": LIVE.stop()}


@app.get("/api/record/status")
def record_status():
    du = shutil.disk_usage(record_dir())
    return {"cameras": REC.status(),
            "disk_free_gb": round(du.free / 1e9, 1),
            "disk_total_gb": round(du.total / 1e9, 1)}


@app.post("/api/record/start")
def record_start(body: dict = Body(default={})):
    REC.start(body.get("cams") or None)   # empty/missing = every configured camera
    return record_status()


@app.post("/api/record/stop")
def record_stop(body: dict = Body(default={})):
    REC.stop(body.get("cams") or None)
    return record_status()


# Credentials set by an activation this process performed. In-memory only: a restart
# forgets them, which is correct — config.yaml is the durable home for camera creds.
ACTIVATED = {}


def cam_creds(ip):
    """Credentials just set by us, else configured for this IP, else the defaults."""
    if ip in ACTIVATED:
        return ACTIVATED[ip]
    for c in CONFIG.get("cameras", []):
        if c.get("ip") == ip:
            return c.get("user", ""), c.get("password", "")
    d = CONFIG.get("camera_defaults") or {}
    return d.get("user", "admin"), d.get("password", "")


def valid_ip(ip):
    """These land in URLs and device config — never trust the caller."""
    try:
        ipaddress.ip_address(ip)
    except (ValueError, TypeError):
        raise HTTPException(400, f"not an IP address: {ip!r}")
    return ip


@app.post("/api/camera/scan")
def camera_scan(body: dict = Body(default={})):
    cidrs = body.get("cidrs") or [f"{ip}/24" for ip in local_ips()]
    for c in cidrs:
        try:
            net = ipaddress.ip_network(c, strict=False)
        except (ValueError, TypeError):
            raise HTTPException(400, f"not a network: {c!r}")
        if net.prefixlen < 22:               # a /8 sweep is 16M hosts, not a field scan
            raise HTTPException(400, f"{c} is too large to sweep — use /22 or smaller")
    return {"cameras": camera.scan(cidrs, cam_creds), "cidrs": cidrs}


@app.post("/api/camera/activate")
def camera_activate(body: dict = Body(default={})):
    if not body.get("password"):
        raise HTTPException(400, "password required")
    ip = valid_ip(body.get("ip"))
    r = camera.activate(ip, body["password"])
    if r.get("ok"):
        # Preview/Set IP/Test RTSP must work straight after activation, before the
        # operator has added this camera to config.yaml.
        ACTIVATED[ip] = ("admin", body["password"])
    return r


@app.post("/api/camera/set_ip")
def camera_set_ip(body: dict = Body(default={})):
    ip = valid_ip(body.get("ip"))
    user, password = cam_creds(ip)
    try:
        return camera.set_static_ip(ip, body.get("user") or user,
                                    body.get("password") or password,
                                    valid_ip(body.get("address")),
                                    body.get("mask") or "255.255.255.0",
                                    body.get("gateway") or "")
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/camera/snapshot")
def camera_snapshot(ip: str):
    # credentials resolve server-side only — query-string creds would land in access logs
    valid_ip(ip)
    user, password = cam_creds(ip)
    data, err = camera.snapshot(ip, user, password)
    if err:
        raise HTTPException(502, err)
    return Response(content=data, media_type="image/jpeg")


@app.post("/api/camera/test_rtsp")
def camera_test_rtsp(body: dict = Body(default={})):
    ip = valid_ip(body.get("ip"))
    user, password = cam_creds(ip)
    return camera.test_rtsp(ip, body.get("user") or user, body.get("password") or password)


# --- NVR pull, wrapped around the existing nvr_pull.py ---

# Daemon threads, not a pool: a non-daemon worker mid multi-GB pull would block
# interpreter exit. The semaphore keeps the same 4-at-a-time limit.
NVR_SLOTS = threading.Semaphore(4)
JOBS = {}            # site -> {"state": RUNNING|VERIFIED|ATTENTION, "started": ts}
SUBSCRIBERS = []     # one queue.Queue per open SSE client


def nvr_log(site, msg):
    """Replaces nvr_pull.log, so every pull line fans out to the UI."""
    line = {"site": site, "msg": msg, "t": datetime.now().strftime("%H:%M:%S")}
    print(f"[{line['t']}] [{site}] {msg}", flush=True)
    for q in list(SUBSCRIBERS):
        try:
            q.put_nowait(line)
        except queue.Full:
            pass     # ponytail: drop lines for a stalled client, never block the pull


nvr_pull.log = nvr_log   # every internal call resolves through module globals


def out_root():
    return ROOT / CONFIG.get("out_dir", "./ingest")


def tcp_open(host, port=80, timeout=1.0):
    try:
        socket.create_connection((host, port), timeout).close()
        return True
    except OSError:
        return False


def valid_date(d):
    """Lands in a filesystem path — reject anything that isn't a plain date."""
    try:
        return datetime.strptime(d, "%Y-%m-%d").strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        raise HTTPException(400, f"date must be YYYY-MM-DD, got {d!r}")


def valid_hhmm(t):
    """Lands in the ISAPI search XML."""
    try:
        return datetime.strptime(t, "%H:%M").strftime("%H:%M")
    except (ValueError, TypeError):
        raise HTTPException(400, f"time must be HH:MM, got {t!r}")


def nvr_by_name():
    return {n["name"]: n for n in CONFIG.get("nvrs", [])}


@app.get("/api/nvr/list")
def nvr_list(date: str = ""):
    date = valid_date(date or datetime.now().strftime("%Y-%m-%d"))
    nvrs = CONFIG.get("nvrs", [])
    with ThreadPoolExecutor(max_workers=max(len(nvrs), 1)) as ex:
        reach = list(ex.map(lambda n: tcp_open(n["host"]), nvrs))
    out = []
    for n, up in zip(nvrs, reach):
        d = out_root() / date / n["name"]
        out.append({"name": n["name"], "host": n["host"], "reachable": up,
                    "channels": len(n.get("channels", [])),
                    "state": JOBS.get(n["name"], {}).get("state", ""),
                    "verified": (d / ".verified").exists(),
                    "manifest": (d / "manifest.json").exists()})
    return {"nvrs": out, "date": date}


def run_pull(nvr, date, start, end, dry):
    site = nvr["name"]
    with NVR_SLOTS:
        try:
            ok = nvr_pull.pull_site(nvr, date, start, end, out_root(),
                                    bool(CONFIG.get("remux_mkv", True)), dry)
        except Exception as e:
            nvr_log(site, f"PULL FAILED: {e}")
            ok = False
    JOBS[site] = {"state": "VERIFIED" if ok else "ATTENTION",
                  "started": JOBS.get(site, {}).get("started", time.time())}
    nvr_log(site, "VERIFIED" if ok else "ATTENTION NEEDED")


@app.post("/api/nvr/pull")
def nvr_pull_start(body: dict = Body(default={})):
    date = valid_date(body.get("date") or datetime.now().strftime("%Y-%m-%d"))
    start = valid_hhmm(body.get("start") or "05:00")
    end = valid_hhmm(body.get("end") or "21:30")
    by_name = nvr_by_name()
    names = body.get("sites") or list(by_name)
    unknown = [s for s in names if s not in by_name]
    if unknown:
        raise HTTPException(400, f"unknown site(s): {', '.join(unknown)}")
    started = []
    for s in names:
        if JOBS.get(s, {}).get("state") == "RUNNING":
            continue
        JOBS[s] = {"state": "RUNNING", "started": time.time()}
        threading.Thread(target=run_pull, daemon=True,
                         args=(by_name[s], date, start, end, bool(body.get("dry")))).start()
        started.append(s)
    return {"started": started, "date": date, "start": start, "end": end}


@app.get("/api/nvr/logstream")
def nvr_logstream():
    q = queue.Queue(maxsize=500)
    SUBSCRIBERS.append(q)

    def gen():
        try:
            while True:
                try:
                    yield f"data: {json.dumps(q.get(timeout=15))}\n\n"
                except queue.Empty:
                    yield ": ping\n\n"    # keep idle proxies from closing the stream
        finally:
            if q in SUBSCRIBERS:
                SUBSCRIBERS.remove(q)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/nvr/manifest")
def nvr_manifest(site: str, date: str):
    if site not in nvr_by_name():          # config names only — no path traversal
        raise HTTPException(404, f"unknown site {site!r}")
    p = out_root() / valid_date(date) / site / "manifest.json"
    if not p.exists():
        raise HTTPException(404, "no manifest for that site-day yet")
    return FileResponse(p, media_type="application/json")


@app.get("/api/config")
def config_get():
    return {"text": CONFIG_PATH.read_text()}


@app.post("/api/config")
def config_post(body: dict = Body(default={})):
    text = body.get("text") or ""
    try:
        cfg = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise HTTPException(400, f"YAML parse error: {e}")
    if not isinstance(cfg, dict):
        raise HTTPException(400, "config must be a mapping of keys")
    for k in ("nvrs", "cameras"):     # absent is fine — a recording-only node has no nvrs
        if cfg.get(k) is not None and not isinstance(cfg[k], list):
            raise HTTPException(400, f"'{k}' must be a list if present")
    # Write the operator's text verbatim: a safe_dump round-trip would strip every comment.
    CONFIG_PATH.write_text(text)
    load_config()
    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
