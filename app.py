#!/usr/bin/env python3
"""FieldKit — on-site capture & extraction console. Run: python app.py"""

import asyncio
import atexit
import hashlib
import hmac
import ipaddress
import json
import os
import random
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
import yaml
from fastapi import Body, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

import camera
import detect
import hive
import hostnet
import live
import offload
import recorder

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"
EXAMPLE_PATH = ROOT / "config.example.yaml"
DATASET = ROOT / "dataset"                        # detection samples awaiting operator review

CONFIG = {}
CAM_NAME = re.compile(r"^[A-Za-z0-9_-]{1,32}$")   # becomes a directory name
SAMPLE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")  # becomes a file name — no dots, no separators
SEGMENT_MAX = 660                                 # 600 s segments plus slack for a late finalise
ATTR_VALUE = re.compile(r"^[a-z0-9][a-z0-9+._-]{0,31}$")   # digits and + are legal: axle configs
WHO_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,23}$")         # labeller handle
GOLD_RATE = float(os.environ.get("GOLD_RATE", 0.05))       # how often a check sample rides along
# FIELDKIT_MODE=curation: an internet-facing labelling server — dataset endpoints only,
# every one of them token-gated, and none of the camera/recorder/cloud machinery.
CURATION = os.environ.get("FIELDKIT_MODE", "console") == "curation"
REVIEWERS = [w.strip() for w in os.environ.get("REVIEWERS", "").split(",") if w.strip()]
# Break-glass supervisor: PASSWORD in the environment beats the stored hash, so a
# forgotten password is reset by editing one variable on the host instead of deleting
# a file off a volume nobody can reach. The handle is a reviewer by definition — set
# only SUPERVISOR and you are still in, whatever REVIEWERS says.
SUPERVISOR = os.environ.get("SUPERVISOR", "").strip().lower()
SUPERVISOR_PW = os.environ.get("PASSWORD", "")
if SUPERVISOR and SUPERVISOR not in REVIEWERS:
    REVIEWERS.append(SUPERVISOR)
LOGIN_TRIES = 5           # failures before a handle is locked out
LOGIN_LOCK = 60.0         # seconds it stays locked; a password endpoint faces the internet
LOGINS = {}               # handle -> [failures, locked until]
LOGINS_LOCK = threading.Lock()
REVIEW_RATE = 0.10        # share of each curator's approvals an expert re-checks
SYNC_EVERY = 120.0        # curation node -> R2: how often the ledgers are harvested
MAX_UPLOAD = 500 * 1024**2                                 # ceiling on one uploaded video
UPLOAD_NAME = re.compile(r"^[A-Za-z0-9._-]{1,80}$")        # becomes part of an R2 key
REFRESH = {"running": False, "started_at": None, "last_result": None, "last_error": None}
REFRESH_LOCK = threading.Lock()
SAME_BOX = 0.6            # IoU at which a submitted box is "the same box" as the key's
MATCH_BOX = 0.5           # looser pairing for two independent labellings of one frame
MIN_SCORES = 3            # under this many scores, report no accuracy rather than a number
GOLD_TOKENS = {}          # disguised id -> gold id; refilled from gold-served.jsonl after a restart
CLAIM_TTL = 1800.0        # a slice someone walked away from frees itself after this
CLAIMS = {}               # sample id -> (who, expiry as wall-clock); mirrored to claims.json
CLAIMS_LOCK = threading.Lock()


def claims_path():
    return DATASET / "claims.json"


def load_claims():
    """Claims outlive the process. A redeploy mid-shift used to forget every slice, and the
    first curator to reload was handed the newest hundred frames — the very ones a
    colleague still had open — so both labelled them and the slower submit bounced."""
    try:
        raw = json.loads(claims_path().read_text())
    except (OSError, ValueError):
        return {}
    now = time.time()
    return {sid: (who, exp) for sid, (who, exp) in raw.items() if exp > now}


def save_claims():
    """Caller holds CLAIMS_LOCK. The file is only the restart safety net: if it cannot be
    written the in-memory claims still keep this process's curators apart."""
    try:
        claims_path().write_text(json.dumps(CLAIMS))
    except OSError as e:
        print(f"claims: not persisted ({e})", flush=True)


CLAIMS.update(load_claims())
AXLE_GROUPS = re.compile(r"^\d+(\+\d+)+$")                 # 1+2+3 -> steer, drive, trailer axles
# Attribute heads (spec: toll-taxonomy-attributes). Operator-editable in
# dataset/attributes.yaml — append-only per head, ids positional like classes.txt.
ATTR_DEFAULTS = {
    "type": ["car", "suv", "van", "minivan", "pickup", "minibus", "coaster", "bus",
             "rigid-truck", "articulated", "tanker", "other"],
    "axles": ["2", "3", "4", "5", "6", "7plus"],
    "cargo": ["none", "general", "container", "tanker-liquid", "mineral-transport",
              "mining-equipment", "construction-equipment", "other"],
}
DET_CLASSES = list(detect.CLASSES.values())       # dataset class ids 0-3, the order detect.py writes
CLASS_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")   # YOLO training class name


def load_config():
    """Re-read config.yaml into the module-level CONFIG dict."""
    cfg = yaml.safe_load(CONFIG_PATH.read_text()) or {}   # parse first: a bad file must not leave CONFIG empty
    cfg["cameras"] = cfg.get("cameras") or []             # an empty `cameras:` key parses as None
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
    # getaddrinfo also misses secondary addresses on Linux (/etc/hosts pins the
    # hostname to 127.0.1.1) — a joined camera-net or link-local address must count.
    for i in hostnet.list_ifaces():
        ips.update(i["ips"])
    rest = sorted(ip for ip in ips if not ip.startswith("127.") and ip != primary)
    # Default route first: the UI derives the Set-IP /24 from ips[0], and a sorted list
    # would put a Tailscale 100.x address ahead of the site's 192.168.x.
    return ([primary] if primary else []) + rest


def record_dir():
    d = ROOT / CONFIG.get("record_dir", "./recordings")
    d.mkdir(parents=True, exist_ok=True)
    return d


if not CONFIG_PATH.exists():      # first boot (and cloud deploys, where it is untracked)
    shutil.copyfile(EXAMPLE_PATH, CONFIG_PATH)
    print(f"created {CONFIG_PATH.name} from {EXAMPLE_PATH.name}", flush=True)

load_config()


def detect_snapshot(ip, user, password):
    """cam_creds chain + short timeout: one dead camera must not stall the whole
    detection pass (camera.snapshot defaults to 8 s), and config-supplied IPs are
    untrusted (POST /api/config has no auth on the field LAN)."""
    try:
        ipaddress.ip_address(ip)
    except (ValueError, TypeError):
        return None, f"not an IP address: {ip!r}"
    return camera.snapshot(ip, *cam_creds(ip), timeout=3)


# Creds the ops console pushes: Hive writes this dict, Offload reads it. Deliberately
# NOT in CONFIG — CONFIG is dumped back to config.yaml on every camera edit, and
# load_config() clears it; either would put a pushed secret on disk or lose it.
CONSOLE_CREDS = {}

# The switches the ops console may throw on a node, by name — so the hive command
# channel drives them without importing app internals. Each takes one bool (mirror and
# contribute also take None: back to config.yaml). Lambdas, not bound methods: on a
# curation node these objects are None and nothing here is ever called — and that
# same deferral is why this sits ABOVE the block that builds them: Hive captures
# the dict at construction, so this name has to exist by then. Keep it here.
CONTROLS = {
    "mirror": lambda on: OFFLOAD.set_mirror(on),
    "contribute": lambda on: OFFLOAD.set_contribute(on),
    "detect": lambda on: DETECT.start() if on else DETECT.stop(),
}

if CURATION:
    # A curation node has no cameras, no drive to fill and no console to phone: it
    # serves the Label tab and its dataset, nothing else.
    REC = LIVE = OFFLOAD = HIVE = DETECT = None
else:
    REC = recorder.Recorder(CONFIG.get("cameras", []), record_dir(), CONFIG.get("site", "site1"),
                            state_path=ROOT / "record_state.json")
    LIVE = live.Live(CONFIG.get("cameras", []), CONFIG.get("go2rtc_binary", ""),
                     ROOT / "go2rtc.yaml")
    LIVE.start()   # no-op unless a binary is configured and present
    # A reboot or crash must not end a session on an unattended node: re-arm whatever
    # was recording when the process died (expired timers stay stopped).
    resumed = REC.resume()
    if resumed:
        print(f"resumed recording after restart: {', '.join(resumed)}", flush=True)
    OFFLOAD = offload.Offload(CONFIG, record_dir(),   # holds CONFIG: config edits land live
                              console_creds=CONSOLE_CREDS)
    OFFLOAD.start()   # no-op unless offload.enabled is set
    # lambda: record_status is a route defined further down, and the heartbeat sends its
    # body verbatim — the console sees exactly what the local Record tab does.
    # CONTROLS' entries are lambdas: OFFLOAD and DETECT below them resolve when the
    # console actually throws a switch, long after this module finished loading.
    HIVE = hive.Hive(CONFIG, REC, lambda: record_status(), camera.snapshot, local_ips,
                     console_creds=CONSOLE_CREDS, controls=CONTROLS)
    HIVE.start()   # no-op unless config.yaml has an ops: block
    DETECT = detect.Detector(CONFIG.get("cameras", []), detect_snapshot, CONFIG,
                             creds_fn=lambda ip: cam_creds(ip), dataset_dir=DATASET,
                             counts_dir=ROOT / "counts",   # one JSON per day: the durable tallies
                             events_dir=ROOT / "events")   # per-vehicle records + evidence crops
    DETECT.start()   # no-op unless the optional detection deps are installed


@atexit.register
def _shutdown():
    """Children spawned in their own process group survive Ctrl-C; an orphaned ffmpeg
    would then fight the restarted app for the same segment paths."""
    if REC:
        REC.shutdown()   # not stop(): a restart must not erase the sessions resume() re-arms
        LIVE.stop()
        DETECT.stop()

app = FastAPI(title="FieldKit")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

if CURATION:
    @app.middleware("http")
    async def curation_gate(request, call_next):
        """One gate for the whole curation deployment: the camera/recorder/cloud API
        does not exist here, and every dataset call — reads included — needs a curator
        token, because this node is on the public internet."""
        path = request.url.path
        if path.startswith("/api/"):
            if not path.startswith("/api/dataset/"):
                return JSONResponse({"detail": "not found"}, status_code=404)
            token = request.headers.get("x-curator-token", "")
            # /login is the one path in without a token: it proves the caller itself.
            if path != "/api/dataset/login" and \
                    not any(hmac.compare_digest(t, token) for t in curators().values()):
                return JSONResponse({"detail": "unknown curator or bad token"}, status_code=401)
        return await call_next(request)


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
        "hive": HIVE.info(),
        "detect": DETECT.info(),
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


@app.post("/api/detect/start")
def detect_start():
    return {"state": DETECT.start()}


@app.post("/api/detect/stop")
def detect_stop():
    """Frees the GPU/CPU for training runs; Monitor falls back to raw snapshots."""
    return {"state": DETECT.stop()}


@app.post("/api/offload/mirror")
def offload_mirror(body: dict = Body(default={})):
    """Keep-a-cloud-copy switch. null hands the decision back to config.yaml."""
    OFFLOAD.set_mirror(body.get("on"))
    return OFFLOAD.info()


@app.post("/api/offload/contribute")
def offload_contribute(body: dict = Body(default={})):
    """Hand this node's captured samples to the shared labelling queue. null hands the
    decision back to config.yaml."""
    OFFLOAD.set_contribute(body.get("on"))
    return OFFLOAD.info()


@app.get("/api/record/status")
def record_status():
    du = shutil.disk_usage(record_dir())
    return {"cameras": REC.status(),
            "disk_free_gb": round(du.free / 1e9, 1),
            "disk_total_gb": round(du.total / 1e9, 1),
            # single-filesystem list kept: the ops console heartbeat renders it per-disk
            "disks": [{"path": str(record_dir()), "free_gb": round(du.free / 1e9, 1),
                       "total_gb": round(du.total / 1e9, 1)}],
            "offload": OFFLOAD.info()}


def sync_clocks(names):
    """Cameras drift between site visits, and a wrong clock ruins the footage timeline."""
    for c in CONFIG["cameras"]:
        if c["name"] in names:
            r = camera.set_time(c.get("ip", ""), c.get("user", ""), c.get("password", ""))
            how = r.get("camera_time") or "ok" if r.get("ok") else r.get("error") or r.get("status")
            print(f"time sync {c['name']} {c.get('ip', '')}: {how}", flush=True)


@app.post("/api/record/start")
def record_start(body: dict = Body(default={})):
    names = set(body.get("cams") or [c["name"] for c in CONFIG["cameras"]])
    hours = body.get("hours")             # absent = record until someone presses Stop
    if hours is not None:
        try:
            hours = float(hours)
            if not 0 < hours <= 720:
                raise ValueError
        except (TypeError, ValueError):
            raise HTTPException(400, "hours must be a number between 0 and 720")
    # Off-thread and best-effort: an unreachable camera must never delay a record start.
    threading.Thread(target=sync_clocks, args=(names,), daemon=True).start()
    REC.start(body.get("cams") or None, hours=hours)   # empty/missing cams = every camera
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


def scan_cidrs(ips):
    """Default sweep targets. Never our own link-local /24: cameras don't live in the
    slice we parked on, and SADP is what finds a self-assigned camera anyway."""
    return [f"{ip}/24" for ip in ips if not ip.startswith("169.254.")]


@app.post("/api/camera/scan")
def camera_scan(body: dict = Body(default={})):
    # Linux/Jetson: a bare wired port can't even egress the SADP probe — fix that first.
    boot = hostnet.bootstrap(exclude_ip=(local_ips() or [""])[0])
    ips = local_ips()
    cidrs = body.get("cidrs") or scan_cidrs(ips)
    for c in cidrs:
        try:
            net = ipaddress.ip_network(c, strict=False)
        except (ValueError, TypeError):
            raise HTTPException(400, f"not a network: {c!r}")
        if net.prefixlen < 22:               # a /8 sweep is 16M hosts, not a field scan
            raise HTTPException(400, f"{c} is too large to sweep — use /22 or smaller")
    # ifaces: probe every interface, not just the default route (hotspot + switch case)
    rows = camera.scan(cidrs, cam_creds, ifaces=ips)
    out = {"cameras": rows, "cidrs": cidrs}
    if boot and not boot.get("ok"):
        # Usually the missing sudoers grant — the UI shows the one-time command.
        out["join_error"] = {k: boot.get(k) for k in ("error", "cmd", "grant")}

    # Novices don't read the amber warning, so join the camera's network for them.
    # One join per scan: one switch at a time is the field reality.
    mine = {ipaddress.ip_network(f"{i}/24", strict=False) for i in ips}
    orphan = next((r["ip"] for r in rows
                   if ipaddress.ip_network(f"{r['ip']}/24", strict=False) not in mine), None)
    if orphan:
        j = hostnet.join(f"{orphan}/24", exclude_ip=ips[0] if ips else "")
        if j.get("ok"):
            out["joined"] = {"ip": j["ip"], "iface": j["iface"]}
            # Re-scan with the new address so those rows come back with ISAPI data.
            # ponytail: full re-scan (~5 s) instead of merging a partial sweep — it
            # happens once per site and the merge lives in camera.scan, not here.
            ips = local_ips()
            out["cameras"] = camera.scan(body.get("cidrs") or scan_cidrs(ips),
                                         cam_creds, ifaces=ips)
        else:
            out["join_error"] = {k: j.get(k) for k in ("error", "cmd", "grant")}
    return out


@app.post("/api/host/join_network")
def host_join_network(body: dict = Body(default={})):
    """Field switches have no DHCP; give this host an address on the camera's /24."""
    ip = valid_ip(body.get("camera_ip"))
    ips = local_ips()
    return hostnet.join(f"{ip}/24", exclude_ip=ips[0] if ips else "")


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
        # Factory cameras run on China time; activation is the moment to fix that.
        r["time_sync"] = camera.set_time(ip, "admin", body["password"])
    return r


@app.post("/api/camera/login")
def camera_login(body: dict = Body(default={})):
    """Verify credentials for an already-activated camera (e.g. set up on its own
    web page) and remember them for every later action on this ip."""
    ip = valid_ip(body.get("ip"))
    user = body.get("user") or "admin"
    if not body.get("password"):
        raise HTTPException(400, "password required")
    d = camera.probe_device(ip, user, body["password"], timeout=3)
    if not d:
        raise HTTPException(502, "no ISAPI response from that address")
    if d.get("note"):                      # credentials rejected / not activated
        raise HTTPException(401, d["note"])
    ACTIVATED[ip] = (user, body["password"])
    return {"ok": True, "model": d.get("model", ""), "mac": d.get("mac", ""),
            "serial": d.get("serial", ""),
            "time_sync": camera.set_time(ip, user, body["password"])}


@app.post("/api/camera/set_ip")
def camera_set_ip(body: dict = Body(default={})):
    ip = valid_ip(body.get("ip"))
    address = valid_ip(body.get("address"))
    if address != ip and hostnet._in_use(address):
        # Two cameras on one address answer ARP and nothing else — refuse to create that.
        raise HTTPException(400, f"{address} is already in use by another device — pick a different slot")
    user, password = cam_creds(ip)
    user, password = body.get("user") or user, body.get("password") or password
    try:
        r = camera.set_static_ip(ip, user, password, address,
                                 body.get("mask") or "255.255.255.0",
                                 body.get("gateway") or "")
    except ValueError as e:
        raise HTTPException(400, str(e))
    if r.get("ok"):
        # The camera moves to `address`; the auto-add that follows looks creds up
        # by the NEW ip, so the working pair must be reachable there too.
        ACTIVATED[address] = (user, password)
    return r


@app.post("/api/camera/set_time")
def camera_set_time(body: dict = Body(default={})):
    ip = valid_ip(body.get("ip"))
    user, password = cam_creds(ip)
    return camera.set_time(ip, body.get("user") or user, body.get("password") or password)


@app.get("/api/camera/snapshot")
def camera_snapshot(ip: str):
    # credentials resolve server-side only — query-string creds would land in access logs
    valid_ip(ip)
    user, password = cam_creds(ip)
    data, err = camera.snapshot(ip, user, password)
    if err:
        raise HTTPException(502, err)
    return Response(content=data, media_type="image/jpeg")


def cam_or_404(name):
    cam = next((c for c in CONFIG["cameras"] if c["name"] == name), None)
    if not cam:
        raise HTTPException(404, f"no camera named {name!r} in config")
    return cam


@app.get("/api/detect/frame")
def detect_frame(name: str):
    cam = cam_or_404(name)
    data = DETECT.frame(name)
    if data is None:      # detection absent or between passes — show the live camera instead
        data, err = detect_snapshot(cam["ip"], "", "")
        if err:
            raise HTTPException(502, err)
    return Response(content=data, media_type="image/jpeg")


@app.get("/api/detect/counts")
def detect_counts():
    return DETECT.counts()


def segments(name):
    """(path, start epoch, duration) per recorded segment, oldest first. Names are
    local wallclock starts (recorder contract) and mtime is the end — still moving
    on the segment being written, which is what a live scrubber wants."""
    d = record_dir() / CONFIG.get("site", "site1") / name
    out = []
    for p in d.glob("*.mkv") if d.is_dir() else []:
        try:
            start = datetime.strptime(p.stem, "%Y%m%d-%H%M%S").timestamp()
            dur = p.stat().st_mtime - start
        except (ValueError, OSError):      # not a segment name, or deleted mid-scan
            continue
        out.append((p, start, round(min(max(dur, 0), SEGMENT_MAX), 1)))
    return sorted(out, key=lambda s: s[1])


@app.get("/api/review/segments")
def review_segments(name: str):
    cam_or_404(name)
    return {"segments": [{"start": s, "duration": d} for _, s, d in segments(name)]}


@app.get("/api/review/frame")
def review_frame(name: str, t: str):
    cam_or_404(name)
    try:
        when = float(t)
    except (TypeError, ValueError):
        raise HTTPException(400, f"not a timestamp: {t!r}")
    seg = next((x for x in segments(name) if x[1] <= when < x[1] + x[2]), None)
    if not seg:
        raise HTTPException(404, "no footage at that time")
    try:
        # -ss before -i seeks by keyframe index instead of decoding from the top
        out = subprocess.run(["ffmpeg", "-ss", f"{when - seg[1]:.3f}", "-i", str(seg[0]),
                              "-frames:v", "1", "-f", "image2", "-c:v", "mjpeg", "-q:v", "3", "-"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10)
    except subprocess.TimeoutExpired:
        raise HTTPException(502, "frame extraction timed out")
    if not out.stdout:
        raise HTTPException(502, "could not extract a frame")
    data, err = detect.review_frame(out.stdout, CONFIG)
    if err == detect.PIP_HINT:   # no model on this node: scrubbing without boxes beats no scrubbing
        return Response(content=out.stdout, media_type="image/jpeg")
    if err:
        raise HTTPException(502, err)
    return Response(content=data, media_type="image/jpeg")


def valid_sample_id(sid):
    """Sample ids land in filesystem paths — a basename, never a path."""
    if not SAMPLE_ID.match(sid or ""):
        raise HTTPException(400, f"not a sample id: {sid!r}")
    return sid


def dataset_classes():
    """classes.txt wins once it exists: the operator may have added classes past
    the four detect.py ships with."""
    f = DATASET / "classes.txt"
    if f.is_file():
        return [line.strip() for line in f.read_text().splitlines() if line.strip()]
    return list(DET_CLASSES)      # a copy: callers edit this list


def write_classes(classes):
    f = DATASET / "classes.txt"
    f.parent.mkdir(parents=True, exist_ok=True)   # detection may never have run on this node
    f.write_text("".join(c + "\n" for c in classes))
    publish("classes.txt")


def label_files():
    """Every label file in every tree — held work included, or a class rename would
    leave stale ids in it. ponytail: full scan per class edit — the dataset caps at
    ~500 pending samples; index them if that ever changes."""
    for tree in ("pending", "holding", "approved"):
        d = DATASET / tree / "labels"
        if d.is_dir():
            yield from d.glob("*.txt")


def approved_counts():
    """Size of the fine-tuning set. ponytail: full scan per request, same scale
    note as label_files()."""
    names, boxes, frames = dataset_classes(), {}, 0
    d = DATASET / "approved" / "labels"
    for p in sorted(d.glob("*.txt")) if d.is_dir() else []:
        try:
            text = p.read_text()
        except OSError:
            continue
        frames += 1
        for line in text.splitlines():
            f = line.split()
            try:
                cls = int(f[0])
            except (IndexError, ValueError):
                continue
            if 0 <= cls < len(names):      # a stale id past the class list is skipped, not fatal
                boxes[names[cls]] = boxes.get(names[cls], 0) + 1
    # Frames frozen as the reference benchmark (train.reference): still approved, still
    # examples, but they train nothing — so "new since the freeze" is the number that
    # says whether another run is worth starting.
    try:
        ref = {ln.strip() for ln in (DATASET / "reference.txt").read_text().splitlines() if ln.strip()}
    except OSError:
        ref = set()
    frozen = sum(1 for p in (d.glob("*.txt") if d.is_dir() else []) if p.stem in ref)
    return {"frames": frames, "boxes": boxes, "frozen": frozen}


def sample_paths(sid, tree="pending"):
    return (DATASET / tree / "images" / f"{sid}.jpg", DATASET / tree / "labels" / f"{sid}.txt")


def attrs_path(sid, tree="pending"):
    return DATASET / tree / "attrs" / f"{sid}.json"


def suggest_path(sid):
    """VLM pre-fills (suggest_attrs.py). Pending-only: a suggestion the operator
    confirmed is in attrs, and one they didn't is not a record of anything."""
    return DATASET / "pending" / "suggest" / f"{sid}.json"


def attr_yaml():
    """The whole attributes.yaml mapping, or {} — heads, constraints, defaults, implies."""
    try:
        v = yaml.safe_load((DATASET / "attributes.yaml").read_text())
    except (yaml.YAMLError, OSError):
        return {}
    return v if isinstance(v, dict) else {}


def attr_meta(key):
    """A guidance mapping from attributes.yaml (`constraints`, `defaults`, `implies`).
    UI-only: the server validates against the full vocab, so editing these never
    invalidates labels already on disk."""
    c = attr_yaml().get(key)
    return c if isinstance(c, dict) else {}


def attr_vocab():
    """Head -> allowed values. Materialised with the spec defaults on first read;
    a corrupt file falls back to them rather than blocking labelling — and is left
    alone, so the operator's own edit is still there to fix."""
    f = DATASET / "attributes.yaml"
    defaults = {h: list(v) for h, v in ATTR_DEFAULTS.items()}
    if not f.is_file():
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(yaml.safe_dump(ATTR_DEFAULTS, sort_keys=False))
        return defaults
    try:
        v = yaml.safe_load(f.read_text())
    except (yaml.YAMLError, OSError):
        v = None
    if not isinstance(v, dict):
        return defaults
    return {h: [str(x) for x in vals] for h, vals in v.items() if isinstance(vals, list)} or defaults


def read_attrs(path):
    """Sidecar attributes, or {} — a sample without them is the normal case."""
    try:
        v = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def read_boxes(path):
    """YOLO label lines -> box dicts. A malformed line is skipped: one bad row
    must not hide the rest of the sample from the operator."""
    boxes = []
    for line in path.read_text().splitlines():
        f = line.split()
        if len(f) != 5:
            continue
        try:
            boxes.append({"cls": int(f[0]), "cx": float(f[1]), "cy": float(f[2]),
                          "w": float(f[3]), "h": float(f[4])})
        except ValueError:
            continue
    return boxes


def curators():
    """{handle: token} from dataset/curators.yaml, hand-written by the operator.

    THE CONTRACT: no file, unreadable file, or an empty one = OPEN MODE — handles are
    taken at face value exactly as before, which is what a solo or trusted node wants.
    The moment the file has one entry, every request naming a handle must prove it with
    the X-Curator-Token header (see check_token). That header is the ONE mechanism: a
    token in a query string would be copied into every access log."""
    try:
        v = yaml.safe_load((DATASET / "curators.yaml").read_text())
    except (yaml.YAMLError, OSError):
        return {}
    if not isinstance(v, dict):
        return {}
    return {str(k): str(t) for k, t in v.items() if t not in (None, "")}


TRUSTED = "trusted.yaml"   # {handle: true}; syncs like the roster, see dataset_sync.CONFIG


def trusted():
    """{handle: true} from dataset/trusted.yaml — who has proved they can label without
    a second pair of eyes. Missing, unreadable or empty = NOBODY is trusted, which is the
    right default for a node that has just been handed a new team."""
    try:
        v = yaml.safe_load((DATASET / TRUSTED).read_text())
    except (yaml.YAMLError, OSError):
        return {}
    return {str(k): True for k, t in v.items() if t} if isinstance(v, dict) else {}


def holds(who):
    """Does this handle's approval go to the holding pen instead of the training set?
    Only on a node that HAS reviewers: with nobody able to release it, held work would be
    stranded forever, so a solo or open node keeps behaving exactly as it did."""
    return bool(REVIEWERS) and who not in REVIEWERS and not trusted().get(who)


SUPERVISORS = "supervisors.yaml"   # {handle: "salt:hash"}; in no sync list, so it never leaves


def supervisors():
    try:
        v = yaml.safe_load((DATASET / SUPERVISORS).read_text())
    except (yaml.YAMLError, OSError):
        return {}
    return v if isinstance(v, dict) else {}


def pw_hash(password, salt):
    """Stored salted and stretched: the volume this lands on outlives any one deploy."""
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()


def write_yaml(name, data):
    f = DATASET / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(yaml.safe_dump(data, sort_keys=True))


def check_token(who, token):
    """401 unless `who` proved their token. An unknown handle fails exactly like a wrong
    token — same words, same comparison — so this never confirms who exists."""
    known = curators()
    if not who or not known:
        return who
    good = known.get(who) or secrets.token_hex(16)   # unknown handle: compare, never short-circuit
    if not hmac.compare_digest(good, str(token or "")):
        raise HTTPException(401, "unknown curator or bad token")
    return who


def token_who(token):
    """The handle this token belongs to, for endpoints that act on the caller instead of
    on a handle the caller names — nobody can spend someone else's identity here. Open
    mode (no roster) has no names to give: "anon". The curation middleware proves only
    that a token is *someone's*; this says whose."""
    known = curators()
    if not known:
        return "anon"
    who = next((h for h, t in known.items() if hmac.compare_digest(t, str(token or ""))), "")
    if not who:
        raise HTTPException(401, "unknown curator or bad token")
    return who


def valid_who(who):
    """A labeller handle, or "" for solo use (no claims, no audit name)."""
    if not who:
        return ""
    if not WHO_RE.match(who):
        raise HTTPException(400, "who must be lowercase: a letter or digit, then letters, "
                                 "digits, - or _ (max 24)")
    return who


def purge_claims():
    """Drop expired claims. -> {sample id: who} still held."""
    now = time.time()
    with CLAIMS_LOCK:
        expired = [s for s, (_, exp) in CLAIMS.items() if exp <= now]
        for sid in expired:
            del CLAIMS[sid]
        if expired:
            save_claims()
        return {sid: w for sid, (w, _) in CLAIMS.items()}


def hold(ids, who):
    """Claim this slice for `who` (refreshing the TTL). -> how many they now hold."""
    exp = time.time() + CLAIM_TTL
    with CLAIMS_LOCK:
        CLAIMS.update({sid: (who, exp) for sid in ids})
        save_claims()
        return sum(1 for w, _ in CLAIMS.values() if w == who)


def audit(who, sid, action, extra=None):
    """One line per labelling decision. Fire and forget: the operator's work must not
    fail because the log could not be written."""
    append_line("audit.jsonl", {"who": who, "id": sid, "action": action, **(extra or {})})


def read_lines(name):
    """Tolerant reader for the append-only jsonl logs: a torn line is skipped, a
    missing file is an empty log."""
    rows = []
    try:
        lines = (DATASET / name).read_text().splitlines()
    except OSError:
        return rows
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict):
            rows.append(r)
    return rows


def append_line(name, row):
    """Fire and forget, like audit(): scoring must never fail an operator's action."""
    try:
        with open(DATASET / name, "a") as f:
            f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                **row}) + "\n")
    except OSError:
        pass


def xyxy(b):
    return (b["cx"] - b["w"] / 2, b["cy"] - b["h"] / 2, b["cx"] + b["w"] / 2, b["cy"] + b["h"] / 2)


def match_boxes(a, b, floor):
    """Greedy IoU pairing of two box lists. -> ({i: (j, iou)}, unmatched a, unmatched b).
    ponytail: O(n²) over one frame's boxes — fine at ten boxes, revisit at a thousand."""
    scored = sorted(((detect.iou(xyxy(x), xyxy(y)), i, j)
                     for i, x in enumerate(a) for j, y in enumerate(b)), reverse=True)
    pairs, taken = {}, set()
    for v, i, j in scored:
        if v >= floor and i not in pairs and j not in taken:
            pairs[i] = (j, v)
            taken.add(j)
    return (pairs, [i for i in range(len(a)) if i not in pairs],
            [j for j in range(len(b)) if j not in taken])


def agreement(a_boxes, a_attrs, b_boxes, b_attrs):
    """0..1 overlap of two labellings of one frame: each matched pair scores class
    match, box tightness and attribute agreement; anything unmatched counts against."""
    if not a_boxes and not b_boxes:
        return 1.0
    pairs, miss_a, miss_b = match_boxes(a_boxes, b_boxes, MATCH_BOX)
    total = 0.0
    for i, (j, v) in pairs.items():
        parts = [1.0 if a_boxes[i]["cls"] == b_boxes[j]["cls"] else 0.0, v]
        heads = a_attrs.get(str(i)) or {}
        if heads:
            theirs = b_attrs.get(str(j)) or {}
            parts.append(sum(theirs.get(h) == val for h, val in heads.items()) / len(heads))
        total += sum(parts) / len(parts)
    return total / (len(pairs) + len(miss_a) + len(miss_b))


def parse_boxes(body):
    """Validated boxes from a label payload — one rule set for real samples, golds
    and reviews alike."""
    nclasses, out = len(dataset_classes()), []
    for b in body.get("boxes") or []:   # an empty list is legal: every box removed = a negative sample
        try:
            cls = int(b["cls"])
            cx, cy, w, h = (float(b[k]) for k in ("cx", "cy", "w", "h"))
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, f"malformed box: {b!r}")
        if not 0 <= cls < nclasses or not all(0 <= v <= 1 for v in (cx, cy, w, h)):
            raise HTTPException(400, f"box out of range: {b!r}")
        out.append({"cls": cls, "cx": cx, "cy": cy, "w": w, "h": h})
    return out


def box_lines(boxes):
    return "".join(" ".join([str(b["cls"])] + [f"{b[k]:.6f}" for k in ("cx", "cy", "w", "h")])
                   + "\n" for b in boxes)


def golds():
    d = DATASET / "gold"
    return sorted(p for p in d.iterdir() if (p / "perturbed.txt").is_file()) if d.is_dir() else []


def gold_for(token):
    """Gold directory behind a disguised id, or None. The served log is the memory
    that survives a restart mid-session."""
    if token not in GOLD_TOKENS:
        GOLD_TOKENS.update({r["token"]: r["gold"] for r in read_lines("gold-served.jsonl")
                            if r.get("token") and r.get("gold")})
    d = DATASET / "gold" / (GOLD_TOKENS.get(token) or "")
    return d if (d / "perturbed.txt").is_file() else None


def pick_gold(who):
    """A gold this labeller has not seen, disguised as an ordinary pending sample."""
    seen = {r.get("gold") for r in read_lines("gold-served.jsonl") if r.get("who") == who}
    fresh = [g for g in golds() if g.name not in seen]
    if not fresh:
        return None
    g = random.choice(fresh)
    token = "g-" + secrets.token_hex(4)
    GOLD_TOKENS[token] = g.name
    append_line("gold-served.jsonl", {"who": who, "token": token, "gold": g.name})
    sample = {"id": token, "boxes": read_boxes(g / "perturbed.txt")}
    attrs = read_attrs(g / "perturbed-attrs.json")
    if attrs:
        sample["attrs"] = attrs
    return sample


def score_gold(gold, boxes, attrs):
    """-> (score, plants, fixed). A planted error counts as fixed when the box is back
    where the key has it, with the key's class and the key's attributes; breaking a box
    that was already right costs a quarter each."""
    key, key_attrs = read_boxes(gold / "key.txt"), read_attrs(gold / "key-attrs.json")
    try:
        plants = json.loads((gold / "plant.json").read_text())
    except (OSError, ValueError):
        plants = []
    planted = {int(p.get("box", -1)) for p in plants if isinstance(p, dict)}
    pairs, _, _ = match_boxes(key, boxes, SAME_BOX)
    fixed = damage = 0
    for i, kb in enumerate(key):
        j = pairs.get(i)
        ok = bool(j is not None and boxes[j[0]]["cls"] == kb["cls"]
                  and all((attrs.get(str(j[0])) or {}).get(h) == v
                          for h, v in (key_attrs.get(str(i)) or {}).items()))
        if i in planted:
            fixed += ok
        elif not ok:
            damage += 1
    score = (fixed / len(plants)) if plants else 1.0
    return max(0.0, score - 0.25 * damage), len(plants), fixed


def gold_label(token, who, action, body):
    """A gold decision scores the labeller and touches no footage. The response is
    byte-identical to a real one — a check sample nobody can spot is the point."""
    gold = gold_for(token)
    if gold is None or action not in ("approve", "discard"):
        raise HTTPException(404, f"no pending sample {token!r}")
    if action == "discard":
        key = read_boxes(gold / "key.txt")
        score, plants, fixed = (0.0 if key else 1.0), len(key), 0
    else:
        boxes = parse_boxes(body)
        score, plants, fixed = score_gold(gold, boxes, valid_attrs(body.get("attrs"), len(boxes)))
    append_line("scores.jsonl", {"kind": "gold", "who": who, "gold_id": gold.name,
                                 "score": round(score, 3), "plants": plants, "fixed": fixed,
                                 "action": action})
    with CLAIMS_LOCK:
        CLAIMS.pop(token, None)
        save_claims()
    return {"ok": True}


def finish(who, sid, action, payload, extra=None):
    audit(who, sid, action, extra)
    with CLAIMS_LOCK:
        CLAIMS.pop(sid, None)     # decided: back into everyone's pool
        save_claims()
    return payload


@app.get("/api/dataset/samples")
def dataset_samples(who: str = "", x_curator_token: str = Header("")):
    who = check_token(valid_who(who), x_curator_token)
    held = purge_claims()
    labels = DATASET / "pending" / "labels"
    pending = []
    for p in sorted(labels.glob("*.txt")) if labels.is_dir() else []:
        try:
            s = {"id": p.stem, "boxes": read_boxes(p)}
            attrs = read_attrs(attrs_path(p.stem))
            if attrs:
                s["attrs"] = attrs
            suggested = read_attrs(suggest_path(p.stem))
            if suggested:
                s["suggested"] = suggested
            pending.append((p.stat().st_mtime, s))
        except OSError:      # sample reviewed away mid-listing
            continue
    pending.sort(key=lambda t: t[0], reverse=True)
    if who:      # everyone else's live slice is invisible, so two labellers never collide
        pending = [t for t in pending if held.get(t[1]["id"], who) == who]
    assigned = assignment(who) if who else None
    if assigned and assigned["classes"]:
        names = dataset_classes()
        ids = {names.index(c) for c in assigned["classes"]}
        # Stable sort: the assigned classes float up, newest-first survives inside each
        # half, and the general pool still follows so nobody runs out of work.
        pending.sort(key=lambda t: not any(b["cls"] in ids for b in t[1]["boxes"]))
    out = [s for _, s in pending[:100]]
    body = {"classes": dataset_classes(), "attributes": attr_vocab(),
            "attr_constraints": attr_meta("constraints"), "attr_defaults": attr_meta("defaults"),
            "attr_implies": attr_meta("implies"),
            "attr_restricts": attr_meta("restricts"), "approved": approved_counts(),
            "reviewer": who in REVIEWERS,
            # the list is capped for payload size; pending_total is the real pile
            "pending_total": len(pending), "pending": out}
    if who:
        if random.random() < GOLD_RATE:      # at most one check sample per response
            gold = pick_gold(who)
            if gold:
                out.append(gold)
        body["who"], body["claimed"] = who, hold([s["id"] for s in out], who)
        if assigned:
            body["assignment"] = assigned
    return body


def sample_curators(tree="approved"):
    """{sample id live in `tree`: who approved it}, newest audit line wins. Held work is
    read the same way — the approve line is what says whose frame it is."""
    live = {p.stem for p in (DATASET / tree / "labels").glob("*.txt")}
    out = {}
    for r in read_lines("audit.jsonl"):
        # Only an approval assigns credit: a review rewrites the labels but the sample
        # stays the curator's work, and their score is what it is measuring.
        if r.get("action") == "approve" and r.get("id") in live:
            out[r["id"]] = r.get("who") or "anon"
    return out


@app.get("/api/dataset/examples")
def dataset_examples(target: str = "", limit: int = 6, x_curator_token: str = Header("")):
    """What a class or a whole category actually looks like, from frames a SUPERVISOR
    approved — an unsure curator gets the standard, not somebody's guess. Any curator may
    ask: they are the audience.

    Coordinates, never crops: the curation container ships without Pillow on purpose, and
    the browser already fetches the whole frame for /api/dataset/image, so it crops there.
    Handing back boxes is what keeps this endpoint dependency-free."""
    token_who(x_curator_token)
    limit = max(1, min(limit, 24))
    names = dataset_classes()
    ids = {names.index(c) for c in target_classes(target)}
    labels = DATASET / "approved" / "labels"
    if not ids or not labels.is_dir():
        return {"target": target, "examples": []}
    by = sample_curators()      # {approved id: who approved it}, live approvals only
    try:
        rows = sorted(labels.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:             # a sample unapproved mid-listing: no examples beats an error
        rows = []
    # Stable sort, the same trick the assigned-class steering uses: supervisor-approved
    # float up, newest-first survives inside each half, and the rest top the list up —
    # one pass over unique files, so no frame can appear in both halves.
    rows.sort(key=lambda p: by.get(p.stem) not in REVIEWERS)
    out = []
    for p in rows:
        # one example per matching box: two lorries in a frame are two references
        out += [{"id": p.stem, "class": names[b["cls"]],
                 **{k: b[k] for k in ("cx", "cy", "w", "h")}}
                for b in read_boxes(p) if b["cls"] in ids]
        if len(out) >= limit:
            break
    return {"target": target, "examples": out[:limit]}


TREES = ("pending", "holding", "approved")


def valid_tree(tree):
    """A tree name lands in a filesystem path: one of ours, never a caller's."""
    if tree not in TREES:
        raise HTTPException(400, f"tree must be one of: {', '.join(TREES)}")
    return tree


def envelope(sid, tree, curator):
    """One sample as the editor wants it, wherever it currently lives."""
    return {"id": sid, "curator": curator, "tree": tree,
            "boxes": read_boxes(sample_paths(sid, tree)[1]),
            "attrs": read_attrs(attrs_path(sid, tree))}


def require_reviewer(who):
    """Reviewer powers need both: a proven handle, and REVIEWERS naming it."""
    if not REVIEWERS:
        raise HTTPException(400, "review is off on this node — set REVIEWERS to enable it")
    if who not in REVIEWERS:
        raise HTTPException(403, f"{who or 'anonymous'} is not a reviewer on this node")
    return who


def review_label(sid, who, body):
    """The reviewer's version replaces the curator's: a re-check that leaves the worse
    labels on disk would be an opinion, not QA. The same act RELEASES a held sample —
    reviewed and moved into the training set in one move, scored exactly as any review."""
    require_reviewer(who)
    tree = "holding" if sample_paths(sid, "holding")[1].is_file() else "approved"
    src_img, lbl = sample_paths(sid, tree)
    # A held label with no image beside it would become an approved label with no image:
    # a corrupt training pair, so refuse it rather than write half a sample.
    if not lbl.is_file() or (tree == "holding" and not src_img.is_file()):
        raise HTTPException(404, f"{sid} is not in the {tree} set")
    boxes = parse_boxes(body)
    attrs = valid_attrs(body.get("attrs"), len(boxes))
    curator = sample_curators(tree).get(sid, "anon")
    score = agreement(read_boxes(lbl), read_attrs(attrs_path(sid, tree)), boxes, attrs)
    append_line("scores.jsonl", {"kind": "review", "who": curator, "reviewer": who,
                                 "id": sid, "score": round(score, 3)})
    dst_img, dst_lbl = sample_paths(sid, "approved")
    dst_lbl.parent.mkdir(parents=True, exist_ok=True)
    dst_lbl.write_text(box_lines(boxes))
    dst_attrs = attrs_path(sid, "approved")
    if attrs:
        dst_attrs.parent.mkdir(parents=True, exist_ok=True)
        dst_attrs.write_text(json.dumps(attrs))
    else:
        dst_attrs.unlink(missing_ok=True)
    if tree == "holding":      # the approved copy is written first: never lose the only one
        dst_img.parent.mkdir(parents=True, exist_ok=True)
        src_img.rename(dst_img)
        lbl.unlink(missing_ok=True)
        attrs_path(sid, "holding").unlink(missing_ok=True)
    # The curator rides on the line: it is THEIR work that cleared, and progress and
    # payroll credit the release to them, not to the reviewer who signed it off.
    audit(who, sid, "review",
          {"released": True, "curator": curator} if tree == "holding" else None)
    return {"ok": True, "curator": curator, "score": round(score, 3),
            "released": tree == "holding"}


@app.get("/api/dataset/review")
def dataset_review(who: str, x_curator_token: str = Header("")):
    """Held work FIRST, oldest first: an unproven curator's frames are the queue, and
    nobody's morning should wait behind a randomly drawn re-check. Once the pen is empty,
    the approved sample most in need of a second pair of eyes: whichever curator is
    furthest below the review quota, one of their samples at random."""
    who = require_reviewer(check_token(valid_who(who), x_curator_token))
    try:      # FIFO by the moment the approval wrote the label into holding
        pen = sorted((DATASET / "holding" / "labels").glob("*.txt"),
                     key=lambda f: f.stat().st_mtime)
    except OSError:      # released mid-listing: fall through to the sampled review
        pen = []
    if pen:
        sid = pen[0].stem
        return {**envelope(sid, "holding", sample_curators("holding").get(sid, "anon")),
                "held": True}
    done = {}
    for r in read_lines("scores.jsonl"):
        if r.get("kind") == "review":
            done[r.get("who")] = done.get(r.get("who"), 0) + 1
    reviewed = {r.get("id") for r in read_lines("scores.jsonl") if r.get("kind") == "review"}
    pool, approvals = {}, {}
    for sid, curator in sample_curators().items():
        approvals[curator] = approvals.get(curator, 0) + 1
        if curator != who and sid not in reviewed:
            pool.setdefault(curator, []).append(sid)
    if not pool:
        raise HTTPException(404, "nothing to review yet")
    curator = max(pool, key=lambda c: REVIEW_RATE * approvals[c] - done.get(c, 0))
    sid = random.choice(pool[curator])
    return {**envelope(sid, "approved", curator), "held": False}


@app.get("/api/dataset/search")
def dataset_search(target: str = "", who: str = "", tree: str = "approved", limit: int = 50,
                   x_curator_token: str = Header("")):
    """Every frame holding a class or a whole toll category, newest first — the way back
    to work already filed, for a supervisor spot-checking a class or cleaning up after one
    curator. No class at all lists the whole tree: "what is in holding right now, and
    whose is it" is a question with no vehicle in it. `total` is the whole match,
    `results` the page of it, `by_curator` the whole match tallied by hand.

    ponytail: full scan of the tree per call, the same ceiling approved_counts() and
    label_files() already live with; index the labels if the dataset outgrows it."""
    require_reviewer(token_who(x_curator_token))
    tree, who = valid_tree(tree), valid_who(who)
    names = dataset_classes()
    ids = {names.index(c) for c in target_classes(target)}
    if not ids and str(target or "").strip():
        raise HTTPException(400, f"no class or category {target!r} — pick one of: "
                                 f"{', '.join(categories() + names)}")
    by = sample_curators(tree)
    try:
        rows = sorted((DATASET / tree / "labels").glob("*.txt"),
                      key=lambda f: f.stat().st_mtime, reverse=True)
    except OSError:      # a sample moved mid-listing: fewer results beats an error
        rows = []
    hits, by_curator = [], {}
    for f in rows:
        curator = by.get(f.stem, "anon")
        if who and curator != who:
            continue
        try:
            boxes = read_boxes(f)
        except OSError:
            continue
        if ids and not any(b["cls"] in ids for b in boxes):
            continue
        hits.append({"id": f.stem, "curator": curator, "tree": tree, "boxes": boxes,
                     "attrs": read_attrs(attrs_path(f.stem, tree))})
        by_curator[curator] = by_curator.get(curator, 0) + 1
    return {"results": hits[:max(1, min(limit, 200))], "total": len(hits),
            "by_curator": by_curator}


@app.get("/api/dataset/sample")
def dataset_sample(id: str, tree: str = "approved", x_curator_token: str = Header("")):
    """One sample's envelope, so a searched frame loads straight into the editor."""
    require_reviewer(token_who(x_curator_token))
    sid, tree = valid_sample_id(id), valid_tree(tree)
    if not sample_paths(sid, tree)[1].is_file():
        raise HTTPException(404, f"no {tree} sample {id!r}")
    return envelope(sid, tree, sample_curators(tree).get(sid, "anon"))


# ---- The cloud round-trip. A curation node runs on a container volume, not on a
# backup: its work exists only once it reaches R2, and new work only arrives from there.

def r2():
    """(dataset_sync, client, bucket). Imported here, not at module scope: a console node
    needs neither boto3 nor credentials to serve its Label tab."""
    import dataset_sync
    try:
        o = dataset_sync.creds()
        return dataset_sync, dataset_sync.client(o), o["bucket"]
    except SystemExit as e:      # exiting is right for the CLI, wrong inside a request
        raise HTTPException(503, str(e))


def sync_both(ds, cl, bucket):
    """Our work up, everything else down. -> (uploaded, downloaded). Both directions skip
    on same-name-same-size, so a steady state is one listing and a pile of stat calls."""
    sent, _ = ds.push(cl, bucket, names=ds.LEDGERS)
    got, _ = ds.pull(cl, bucket)
    return sent, got


def sync_loop():
    """This node's whole sync engine, and the reason boot is not allowed to block on a
    9,000-object download: the first pass streams the dataset in while the server is
    already up and answering, and every pass after that publishes the team's approvals
    and picks up whatever the operator has staged — new samples within two minutes, no
    redeploy, no manual step.

    ponytail: one full LIST of the prefix per pass — fine at thousands of files, wants a
    manifest at millions — and a manual refresh can overlap a pass, which costs duplicate
    downloads and nothing else."""
    while True:
        try:
            sync_both(*r2())
        except Exception as e:   # a failed pass is a retry in SYNC_EVERY, never a dead server
            print(f"dataset sync failed: {e}", flush=True)
        time.sleep(SYNC_EVERY)


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def refresh_now():
    """The same pass the loop runs, now instead of within two minutes."""
    try:
        sent, got = sync_both(*r2())
        REFRESH["last_result"] = {"pushed": sent, "pulled": got, "at": stamp()}
    except Exception as e:
        REFRESH["last_error"] = str(e)
    finally:
        REFRESH["running"] = False


@app.post("/api/dataset/refresh")
def dataset_refresh(x_curator_token: str = Header("")):
    """Run the periodic sync right now: the operator's lever when two minutes is too long."""
    require_reviewer(token_who(x_curator_token))
    with REFRESH_LOCK:
        if REFRESH["running"]:
            return {"running": True}      # one at a time: the second press is a no-op
        REFRESH.update(running=True, started_at=stamp(), last_error=None)
    threading.Thread(target=refresh_now, daemon=True).start()
    return dict(REFRESH)


@app.get("/api/dataset/refresh")
def dataset_refresh_state():
    return dict(REFRESH)


def login_guard(who):
    """Refuse a handle that has just failed repeatedly. In memory on purpose: a restart
    clearing the counter costs an attacker more than it costs the supervisor."""
    with LOGINS_LOCK:
        fails, until = LOGINS.get(who, (0, 0.0))
        if fails >= LOGIN_TRIES and time.monotonic() < until:
            raise HTTPException(429, f"too many attempts — wait {int(until - time.monotonic())}s")


def login_result(who, ok):
    with LOGINS_LOCK:
        if ok:
            LOGINS.pop(who, None)
        else:
            fails = LOGINS.get(who, (0, 0.0))[0] + 1
            LOGINS[who] = (fails, time.monotonic() + LOGIN_LOCK)
    if not ok:
        raise HTTPException(401, "wrong password")


@app.post("/api/dataset/login")
def dataset_login(body: dict = Body(default={}), x_curator_token: str = Header("")):
    """A supervisor trades a password for their curator token, so curators.yaml stays the
    one source of identity and the browser goes on sending the same header as everyone.

    The FIRST sign-in sets the password and must prove it is really them with the token
    they already hold — otherwise the first stranger to find this endpoint claims the
    account. That self-proving is why it is the one path the curation gate lets through."""
    who = valid_who(str(body.get("who") or "").strip().lower())
    password = str(body.get("password") or "")
    if who not in REVIEWERS:
        raise HTTPException(403, f"{who or 'anonymous'} is not a supervisor on this node")
    if len(password) < 8:
        raise HTTPException(400, "password must be at least 8 characters")
    login_guard(who)
    token = curators().get(who)
    kept = supervisors()
    if who == SUPERVISOR and SUPERVISOR_PW:
        # The environment is the reset lever: it answers before any stored hash, and it
        # works on a node where this handle has no token yet.
        login_result(who, hmac.compare_digest(SUPERVISOR_PW, password))
        if not token:
            roster = curators()
            token = roster[who] = secrets.token_urlsafe(9)
            write_yaml("curators.yaml", roster)
            push_roster()
        return {"who": who, "token": token}
    if not token:
        raise HTTPException(404, f"{who} is not in curators.yaml yet")
    if who in kept:
        salt, want = str(kept[who]).split(":", 1)
        login_result(who, hmac.compare_digest(want, pw_hash(password, salt)))
    else:
        if not hmac.compare_digest(token, str(x_curator_token or "")):
            raise HTTPException(401, "first sign-in proves itself with your curator token — "
                                     "send it once and the password replaces it after that")
        salt = secrets.token_hex(16)
        kept[who] = f"{salt}:{pw_hash(password, salt)}"
        write_yaml(SUPERVISORS, kept)
    return {"who": who, "token": token}


def push_file(name):
    """Publish one config file on its own — curators.yaml, assignments.yaml. Best effort:
    the edit is already on disk and the supervisor can press Sync now if the bucket was
    unreachable."""
    try:
        ds, cl, bucket = r2()
        ds.push(cl, bucket, names=(name,))
        return True
    except Exception:
        return False


def push_roster():
    return push_file("curators.yaml")


def publish(name):
    """The taxonomy the team labels against is the taxonomy the model trains on. The
    curation instance is the official tool, so a class or a value added there has to
    reach the bucket, or the laptop harvesting for training reads ids that mean
    something else. Only the curation instance publishes: a site box or the laptop
    editing its own copy is not an authority, and classes.txt ids are positional."""
    return push_file(name) if CURATION else False


@app.get("/api/dataset/curators")
def curators_list(x_curator_token: str = Header("")):
    require_reviewer(token_who(x_curator_token))
    return {"curators": curators(), "reviewers": REVIEWERS}


@app.post("/api/dataset/curators")
def curators_edit(body: dict = Body(default={}), x_curator_token: str = Header("")):
    """Mint or drop a curator, then publish the roster in the same breath: sync_both pulls
    curators.yaml back down every couple of minutes, so an edit that is not pushed is an
    edit that gets overwritten.

    ponytail: best-effort push, and the pull skips on same-name-same-size — a sync pass
    landing between the write and the push undoes it, and swapping a handle for another of
    the exact same length would not propagate. The UI re-reads the roster so either shows
    up; give the file a version counter if that ever stops being enough."""
    me = require_reviewer(token_who(x_curator_token))
    handle = valid_who(str(body.get("handle") or "").strip().lower())
    if not handle:
        raise HTTPException(400, "handle is required")
    roster = curators()
    if body.get("action") == "remove":
        if handle in REVIEWERS:
            raise HTTPException(400, f"{handle} is a supervisor — take them out of REVIEWERS first")
        roster.pop(handle, None)
    else:
        # 12 characters, 72 bits: short enough to read down a phone line, still unguessable.
        roster[handle] = secrets.token_urlsafe(9)
    write_yaml("curators.yaml", roster)
    audit(me, handle, "roster-" + (str(body.get("action") or "add")))
    return {"curators": roster, "reviewers": REVIEWERS, "pushed": push_roster()}


@app.get("/api/dataset/trust")
def trust_list(x_curator_token: str = Header("")):
    require_reviewer(token_who(x_curator_token))
    return {"trusted": trusted()}


@app.post("/api/dataset/trust")
def trust_edit(body: dict = Body(default={}), x_curator_token: str = Header("")):
    """Take a curator off probation, or put them back on it. Published in the same breath
    as the write, same reason as the roster: sync pulls this file back down every couple
    of minutes, so an edit that is not pushed is an edit that gets overwritten."""
    me = require_reviewer(token_who(x_curator_token))
    handle = valid_who(str(body.get("handle") or "").strip().lower())
    if handle not in curators():
        raise HTTPException(404, f"{handle or 'anonymous'} is not in curators.yaml")
    roster, ok = trusted(), bool(body.get("trusted"))
    if ok:
        roster[handle] = True
    else:
        roster.pop(handle, None)
    write_yaml(TRUSTED, roster)
    audit(me, handle, "trust" if ok else "untrust")
    return {"trusted": roster, "pushed": push_file(TRUSTED)}


@app.post("/api/dataset/upload_video")
async def dataset_upload_video(request: Request, name: str, x_curator_token: str = Header("")):
    """Field staff contribute footage with the token they already label with. Streamed to
    a temp file in chunks: a phone-sized video must never be held in memory."""
    who = token_who(x_curator_token)
    name = os.path.basename(name or "")
    if not UPLOAD_NAME.match(name):
        raise HTTPException(400, "name must be 1-80 chars of letters, digits, '.', '_' or '-'")
    _, cl, bucket = r2()
    key = f"incoming/{who}/{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{name}"
    with tempfile.NamedTemporaryFile() as tmp:       # deleted on every path out, 413 included
        n = 0
        async for chunk in request.stream():
            n += len(chunk)
            if n > MAX_UPLOAD:
                raise HTTPException(413, f"upload is larger than {MAX_UPLOAD // 1024**2} MB")
            tmp.write(chunk)
        tmp.flush()
        tmp.seek(0)
        # to_thread: a 500 MB blocking PUT on the event loop would freeze every labeller.
        await asyncio.to_thread(cl.upload_fileobj, tmp, bucket, key)
    return {"ok": True, "key": key, "bytes": n}


def assignments():
    """{handle: {"class": ..., "min": n}} from dataset/assignments.yaml, hand-edited by
    the operator like attributes.yaml. Missing or broken file = nobody is assigned."""
    try:
        v = yaml.safe_load((DATASET / "assignments.yaml").read_text())
    except (yaml.YAMLError, OSError):
        return {}
    return {k: a for k, a in v.items() if isinstance(a, dict)} if isinstance(v, dict) else {}


def target_parts(target):
    """A target is one or more comma-separated terms: "A", "e-heavy", "A,C,e-plant"."""
    return [p.strip() for p in str(target or "").split(",") if p.strip()]


def target_classes(target):
    """The classes an assignment target covers, in classes.txt order. Each term is one
    toll CATEGORY letter — every class named `<letter>-*` (a-small, a-motorcycle are
    category A) — or one exact class name; "all" is every class that has a category.
    Terms are unioned, unknown terms cover nothing."""
    names = dataset_classes()
    out = []
    for t in (p.lower() for p in target_parts(target)):
        if t == "all":
            out += [c for c in names if "-" in c]
        elif len(t) == 1:
            out += [c for c in names if c.lower().startswith(t + "-")]
        else:
            out += [c for c in names if c.lower() == t]
    return [c for c in names if c in set(out)]


def categories(names=None):
    """The category letters classes.txt actually has, A first. `wheel` has no dash and
    so belongs to none."""
    return sorted({c[0].upper() for c in (dataset_classes() if names is None else names)
                   if c[1:2] == "-"})


def assignment(who):
    """This curator's quota and how far along they are, or None. `class` is the target as
    written — a class name or a category letter — and `classes` is what it covers."""
    a = assignments().get(who) or {}
    name = str(a.get("class") or "")
    if not name:
        return None
    # The TARGET is what gets stamped and counted (see assigned_extra), so a category
    # tallies every class under it and the count stays honest when the target changes.
    done = sum(1 for r in read_lines("audit.jsonl") if r.get("who") == who
               and r.get("assigned_hit") and r.get("assigned_class") == name)
    try:
        least = int(a.get("min") or 0)
    except (TypeError, ValueError):
        least = 0
    return {"class": name, "min": least, "done": done, "classes": target_classes(name)}


def assigned_extra(who, boxes):
    """Stamp the audit line when an approval really contains the assigned class —
    counting approvals alone would pay for frames that never had one."""
    target = (assignments().get(who) or {}).get("class")
    names = dataset_classes()
    ids = {names.index(c) for c in target_classes(target)}
    if ids and any(b["cls"] in ids for b in boxes):
        return {"assigned_hit": True, "assigned_class": target}
    return None


def assign_state():
    """ponytail: assignment() rescans audit.jsonl per handle — same ceiling as
    /progress, index the log if either ever gets slow."""
    return {"assignments": {w: a for w in assignments() if (a := assignment(w))},
            "classes": dataset_classes(), "categories": categories()}


@app.get("/api/dataset/assign")
def assign_list(x_curator_token: str = Header("")):
    require_reviewer(token_who(x_curator_token))
    return assign_state()


@app.post("/api/dataset/assign")
def assign_edit(body: dict = Body(default={}), x_curator_token: str = Header("")):
    """Give a curator a class or a whole toll category, then publish assignments.yaml in
    the same breath — same reason as the roster: sync_both pulls it back down every couple
    of minutes, so an edit that is not pushed is an edit that gets overwritten."""
    me = require_reviewer(token_who(x_curator_token))
    handle = valid_who(str(body.get("handle") or "").strip().lower())
    if handle not in curators():
        raise HTTPException(404, f"{handle or 'anonymous'} is not in curators.yaml")
    entries = assignments()
    action, extra = "assign-clear", None
    if body.get("clear"):
        entries.pop(handle, None)
    else:
        # Stored as written, normalised term by term: a letter upper-cased, "all" lower.
        parts = [p.upper() if len(p) == 1 else p.lower() if p.lower() == "all" else p
                 for p in target_parts(body.get("class"))]
        bad = next((p for p in parts if not target_classes(p)), None)
        if bad is not None or not parts:
            raise HTTPException(400, f"no class or category {bad!r} — pick one of: all, "
                                     f"{', '.join(categories() + dataset_classes())}")
        target = ",".join(dict.fromkeys(parts))
        try:
            least = int(body.get("min") or 0)
        except (TypeError, ValueError):
            raise HTTPException(400, "min must be a whole number")
        if least < 0:
            raise HTTPException(400, "min must be 0 or more")
        action = "assign"
        entries[handle] = extra = {"class": target, "min": least}
    write_yaml("assignments.yaml", entries)
    audit(me, handle, action, extra)
    return {**assign_state(), "pushed": push_file("assignments.yaml")}


def quality(who):
    """Gold and review scores for one curator. Under MIN_SCORES the mean says nothing,
    so it is reported as null rather than as a number someone would act on."""
    out = {}
    for kind, count_key, acc_key in (("gold", "golds_seen", "accuracy"),
                                     ("review", "reviews", "review_accuracy")):
        vals = [r["score"] for r in read_lines("scores.jsonl")
                if r.get("kind") == kind and r.get("who") == who
                and isinstance(r.get("score"), (int, float))]
        out[count_key] = len(vals)
        out[acc_key] = round(sum(vals) / len(vals), 3) if len(vals) >= MIN_SCORES else None
    return out


@app.get("/api/dataset/progress")
def dataset_progress():
    """Who decided what, from audit.jsonl. ponytail: full scan per call — a week of ten
    labellers is a few thousand lines; index it if a season's log ever gets slow."""
    total, by_who, today = {"approve": 0, "discard": 0}, {}, {"approve": 0, "discard": 0}
    day = datetime.now(timezone.utc).date().isoformat()

    def tally(handle):
        return by_who.setdefault(handle or "anon",
                                 {"approve": 0, "discard": 0, "held": 0, "released": 0})

    try:
        lines = (DATASET / "audit.jsonl").read_text().splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue                      # a torn line must not hide the rest of the log
        if not isinstance(r, dict):
            continue
        act = r.get("action")
        if act == "review" and r.get("released"):
            # Credited to the CURATOR: it is their work that cleared, not the reviewer's.
            tally(r.get("curator"))["released"] += 1
            continue
        if act not in ("approve", "discard", "unapprove"):
            continue
        total[act] = total.get(act, 0) + 1
        mine = tally(r.get("who"))
        mine[act] = mine.get(act, 0) + 1
        if r.get("held"):                 # an approval that landed in the pen, not the set
            mine["held"] += 1
        if str(r.get("ts", "")).startswith(day):
            today[act] = today.get(act, 0) + 1
    for w, stats in by_who.items():
        stats.update(quality(w))
        if assignment(w):
            stats["assignment"] = assignment(w)
    return {"total": total, "by_who": by_who, "today": today}


@app.get("/api/dataset/image")
def dataset_image(id: str):
    sid = valid_sample_id(id)
    gold = gold_for(sid) if sid.startswith("g-") else None
    for img in ([gold / "image.jpg"] if gold else
                [sample_paths(sid, t)[0] for t in TREES]):
        if img.is_file():
            return FileResponse(img, media_type="image/jpeg")
    raise HTTPException(404, f"no pending sample {id!r}")


def send_back(sid, tree):
    """Move a sample out of a finished tree and back into pending, labels and attrs with
    it. -> {"boxes", "attrs"} as they landed. The one path back: unapprove walks an
    approved sample out, reject walks a held one out."""
    src_img, src_lbl = sample_paths(sid, tree)
    if not src_img.is_file():
        raise HTTPException(404, f"{sid} is not in the {tree} set")
    img, lbl = sample_paths(sid)
    img.parent.mkdir(parents=True, exist_ok=True)
    lbl.parent.mkdir(parents=True, exist_ok=True)
    src_img.rename(img)
    lbl.write_text(src_lbl.read_text() if src_lbl.is_file() else "")
    src_lbl.unlink(missing_ok=True)
    src_attrs, attrs_dst = attrs_path(sid, tree), attrs_path(sid)
    attrs = read_attrs(src_attrs)
    if attrs:
        attrs_dst.parent.mkdir(parents=True, exist_ok=True)
        attrs_dst.write_text(json.dumps(attrs))
    src_attrs.unlink(missing_ok=True)
    suggest_path(sid).unlink(missing_ok=True)   # the sample was labelled once already
    for f in (img, lbl):
        os.utime(f, None)   # front of the review queue, last in line for eviction
    return {"boxes": read_boxes(lbl), "attrs": attrs}


@app.post("/api/dataset/label")
def dataset_label(body: dict = Body(default={}), x_curator_token: str = Header("")):
    sid = valid_sample_id(body.get("id"))
    who = check_token(valid_who(body.get("who")), x_curator_token)
    action = body.get("action")
    if sid.startswith("g-"):
        return gold_label(sid, who, action, body)
    if action == "review":
        return review_label(sid, who, body)
    if action == "unapprove":
        return finish(who, sid, action, {"ok": True, "id": sid, **send_back(sid, "approved")})
    if action == "reject":
        # Not approve-or-discard, so dataset_sync.consumed() stops calling this sample
        # finished and it comes round again — that IS the mechanism of a reject.
        require_reviewer(who)
        curator = sample_curators("holding").get(sid, "anon")     # before the files move
        return finish(who, sid, action,
                      {"ok": True, "id": sid, "curator": curator, **send_back(sid, "holding")},
                      {"curator": curator})
    if action not in ("approve", "discard"):
        raise HTTPException(400, "action must be 'approve', 'discard', 'unapprove' or 'reject'")
    img, lbl = sample_paths(sid)
    if not img.is_file():
        raise HTTPException(404, f"no pending sample {sid!r}")
    if action == "discard":
        img.unlink(missing_ok=True)
        lbl.unlink(missing_ok=True)
        attrs_path(sid).unlink(missing_ok=True)
        suggest_path(sid).unlink(missing_ok=True)
        return finish(who, sid, action, {"ok": True})
    boxes = parse_boxes(body)
    attrs = valid_attrs(body.get("attrs"), len(boxes))
    # Unproven work goes to the holding pen, not the training set: everything that reads
    # approved/ — counts, examples, gold, payroll — then ignores it until a reviewer
    # releases it, which is the whole point of probation.
    tree = "holding" if holds(who) else "approved"
    dst_img, dst_lbl = sample_paths(sid, tree)
    dst_img.parent.mkdir(parents=True, exist_ok=True)
    dst_lbl.parent.mkdir(parents=True, exist_ok=True)
    dst_lbl.write_text(box_lines(boxes))   # the operator's edit wins over the model's guess
    dst_attrs = attrs_path(sid, tree)
    if attrs:
        dst_attrs.parent.mkdir(parents=True, exist_ok=True)
        dst_attrs.write_text(json.dumps(attrs))
    else:
        dst_attrs.unlink(missing_ok=True)   # re-approved with the attributes cleared
    img.rename(dst_img)
    lbl.unlink(missing_ok=True)
    attrs_path(sid).unlink(missing_ok=True)
    suggest_path(sid).unlink(missing_ok=True)
    extra = assigned_extra(who, boxes) or {}
    if tree == "holding":
        extra["held"] = True
    return finish(who, sid, action, {"ok": True, "held": tree == "holding"}, extra)


def valid_attrs(attrs, nboxes):
    """{"<box index>": {head: value}} — sparse per box and per head. An unknown head
    or value is a typo in the caller, never a default: the operator's judgment is the
    whole point of the sidecar."""
    if not attrs:
        return {}
    if not isinstance(attrs, dict):
        raise HTTPException(400, "attrs must map a box index to its attributes")
    vocab, out = attr_vocab(), {}
    for k, v in attrs.items():
        try:
            i = int(k)
        except (TypeError, ValueError):
            raise HTTPException(400, f"not a box index: {k!r}")
        if not 0 <= i < nboxes:
            raise HTTPException(400, f"box index {i} is outside the {nboxes} posted boxes")
        if not isinstance(v, dict):
            raise HTTPException(400, f"attributes for box {i} must be a mapping")
        for head, val in v.items():
            if head not in vocab:
                raise HTTPException(400, f"unknown attribute: {head!r}")
            if val not in vocab[head]:
                raise HTTPException(400, f"{val!r} is not a {head} value")
        if v:
            out[str(i)] = v
    return out


def valid_class_name(name):
    if not CLASS_NAME.match(name or ""):
        raise HTTPException(400, "class name must be lowercase: a letter, then letters, "
                                 "digits, - or _ (max 32)")
    return name


def samples_using(idx):
    """How many label files reference class id `idx`."""
    hits = 0
    for p in label_files():
        for line in p.read_text().splitlines():
            f = line.split()
            if f and f[0] == str(idx):
                hits += 1
                break
    return hits


def remap_class_ids(path, fn):
    """Rewrite each line's class id through `fn`; the rest of the line stays verbatim."""
    lines = []
    for line in path.read_text().splitlines():
        f = line.split(None, 1)
        try:
            cls = int(f[0])
        except (IndexError, ValueError):
            lines.append(line)          # malformed row: leave it exactly as found
            continue
        lines.append(" ".join([str(fn(cls))] + f[1:]))
    path.write_text("".join(l + "\n" for l in lines))


@app.post("/api/dataset/class")
def dataset_class(body: dict = Body(default={})):
    action = body.get("action") or "add"
    classes = dataset_classes()
    if action == "add":
        name = valid_class_name(body.get("name"))
        if name not in classes:
            classes.append(name)
            write_classes(classes)
    elif action == "rename":
        old, new = body.get("from") or "", valid_class_name(body.get("to"))
        if old not in classes:
            raise HTTPException(404, f"no class named {old!r}")
        if new in classes:
            raise HTTPException(400, f"{new} already exists — merge the two classes instead")
        # Ids are positional: renaming a line touches no label file on disk.
        classes[classes.index(old)] = new
        write_classes(classes)
    elif action == "remove":
        name = body.get("name") or ""
        if name not in classes:
            raise HTTPException(404, f"no class named {name!r}")
        idx = classes.index(name)
        if idx < len(DET_CLASSES):
            raise HTTPException(400, f"{name} is a class the detector writes itself — "
                                     "rename it to your own term instead")
        used = samples_using(idx)
        if used:
            raise HTTPException(400, f"{name} is used by {used} labelled sample(s) — "
                                     "relabel them before removing it")
        # Label files first: a crash before classes.txt is rewritten leaves every id valid.
        for p in label_files():
            remap_class_ids(p, lambda c: c - 1 if c > idx else c)
        classes.pop(idx)
        write_classes(classes)
    elif action == "merge":
        src, into = body.get("from") or "", body.get("into") or ""
        for n in (src, into):
            if n not in classes:
                raise HTTPException(404, f"no class named {n!r}")
        if src == into:
            raise HTTPException(400, f"{src} is already that class")
        lo, hi = sorted((classes.index(src), classes.index(into)))
        if hi < len(DET_CLASSES):
            raise HTTPException(400, "the four detector classes can't be merged with each other — "
                                     "the detector needs all four slots")
        # Label files first, same as remove: refs remapped with line hi still present is a valid state.
        for p in label_files():
            remap_class_ids(p, lambda c: lo if c == hi else (c - 1 if c > hi else c))
        classes.pop(hi)   # lo keeps its current name — rename it next if the operator wants the other one
        write_classes(classes)
    else:
        raise HTTPException(400, "action must be 'add', 'rename', 'remove' or 'merge'")
    return {"ok": True, "classes": classes}


@app.post("/api/dataset/attr_value")
def dataset_attr_value(body: dict = Body(default={})):
    """Append-only, like classes: sidecars already name these values, and dropping
    one would silently rewrite what an operator recorded."""
    head, value = body.get("head") or "", body.get("value") or ""
    cfg = attr_yaml()
    if not isinstance(cfg.get(head), list):
        raise HTTPException(404, f"no attribute named {head!r}")
    if not ATTR_VALUE.match(value):
        raise HTTPException(400, "value must start with a lowercase letter or digit, then "
                                 "lowercase letters, digits, + . _ or - (max 32)")
    vals = [str(x) for x in cfg[head]]
    if value not in vals:
        vals.insert(vals.index("other") if "other" in vals else len(vals), value)  # "other" stays last
        cfg[head] = vals
        implies = cfg.setdefault("implies", {}).setdefault("axle-config", {}) \
            if head == "axle-config" else {}
        if AXLE_GROUPS.match(value) and value not in implies:
            # Groups are axle counts front to back: steer + drive is the truck, each
            # further group a trailer. 9 is the ceiling the vocab tops out at.
            groups = [int(g) for g in value.split("+")]
            derived = {"axles": str(min(sum(groups), 9)),
                       "trailers": str(min(max(len(groups) - 2, 0), 2))}
            got = {h: v for h, v in derived.items()
                   if v in [str(x) for x in (cfg.get(h) or [])]}
            if got:
                implies[value] = got
        (DATASET / "attributes.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
        publish("attributes.yaml")
    return {"ok": True, "attributes": attr_vocab(), "attr_implies": attr_meta("implies")}


@app.post("/api/camera/test_rtsp")
def camera_test_rtsp(body: dict = Body(default={})):
    ip = valid_ip(body.get("ip"))
    user, password = cam_creds(ip)
    return camera.test_rtsp(ip, body.get("user") or user, body.get("password") or password)


@app.get("/api/config")
def config_get():
    return {"text": CONFIG_PATH.read_text()}


@app.post("/api/config/reset")
def config_reset():
    """Restore config.yaml from config.example.yaml and apply it live."""
    if any(c["state"] != "STOPPED" for c in REC.status().values()):
        raise HTTPException(400, "stop all recording first — reset replaces the camera list")
    for name in list(REC.cams):           # all stopped, so every removal succeeds
        REC.remove_camera(name)
    shutil.copyfile(EXAMPLE_PATH, CONFIG_PATH)
    load_config()
    apply_cameras()                        # example cameras become live in recorder+monitor
    return {"ok": True, "text": CONFIG_PATH.read_text()}


@app.post("/api/config")
def config_post(body: dict = Body(default={})):
    text = body.get("text") or ""
    try:
        cfg = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise HTTPException(400, f"YAML parse error: {e}")
    if not isinstance(cfg, dict):
        raise HTTPException(400, "config must be a mapping of keys")
    if cfg.get("cameras") is not None and not isinstance(cfg["cameras"], list):
        raise HTTPException(400, "'cameras' must be a list if present")
    # Write the operator's text verbatim: a safe_dump round-trip would strip every comment.
    CONFIG_PATH.write_text(text)
    load_config()
    apply_cameras()
    return {"ok": True}


def apply_cameras():
    """Push CONFIG's camera list into the running recorder and sidecar."""
    for c in CONFIG["cameras"]:
        REC.add_camera(c)
    LIVE.set_cameras(CONFIG["cameras"])
    DETECT.set_cameras(CONFIG["cameras"])


@app.post("/api/config/add_camera")
def config_add_camera(body: dict = Body(default={})):
    name, ip = body.get("name") or "", valid_ip(body.get("ip"))
    if not CAM_NAME.match(name):
        raise HTTPException(400, "name must be 1-32 chars of letters, digits, - or _")
    existing = next((c for c in CONFIG["cameras"] if c["name"] == name), None)
    if existing and existing["ip"] != ip:
        raise HTTPException(400, f"{name} already points at {existing['ip']}")
    if existing:
        return {"ok": True, "added": False, "name": name}   # idempotent re-add
    # The ACTIVATED cache wins here: a camera activated a moment ago is usable at once.
    user, password = cam_creds(ip)
    cam = {"name": name, "ip": ip, "user": body.get("user") or user,
           "password": body.get("password") or password}
    CONFIG["cameras"].append(cam)
    CONFIG_PATH.write_text(yaml.safe_dump(CONFIG, sort_keys=False))
    apply_cameras()
    return {"ok": True, "added": True, "name": name}


@app.get("/api/config/site")
def config_site():
    return {"site": CONFIG.get("site", "site1")}


@app.post("/api/config/site")
def config_site_set(body: dict = Body(default={})):
    """Name the site from the phone. The name is a directory and a storage prefix, so the
    camera-name rule applies; and it cannot change under a running recorder, which was
    handed its output path when it started and would carry on writing to the old one."""
    name = (body.get("site") or "").strip()
    if not CAM_NAME.match(name):
        raise HTTPException(400, "site must be 1-32 chars of letters, digits, - or _")
    if any(s.get("desired") for s in REC.st.values()):
        raise HTTPException(400, "a camera is recording — stop it first")
    CONFIG["site"] = REC.site = name
    CONFIG_PATH.write_text(yaml.safe_dump(CONFIG, sort_keys=False))
    # Segments already on disk stay under the old folder — only new ones land here.
    return {"ok": True, "site": name}


@app.post("/api/config/rename_camera")
def config_rename_camera(body: dict = Body(default={})):
    old, new = body.get("old") or "", body.get("new") or ""
    if not CAM_NAME.match(new):
        raise HTTPException(400, "name must be 1-32 chars of letters, digits, - or _")
    cam = next((c for c in CONFIG["cameras"] if c["name"] == old), None)
    if not cam:
        raise HTTPException(404, f"no camera named {old!r} in config")
    if any(c["name"] == new for c in CONFIG["cameras"]):
        raise HTTPException(400, f"{new} already exists")
    if not REC.remove_camera(old):
        raise HTTPException(400, f"{old} is recording — stop it first")
    cam["name"] = new
    CONFIG_PATH.write_text(yaml.safe_dump(CONFIG, sort_keys=False))
    apply_cameras()
    # Already-written segments stay under <record_dir>/<site>/<old>/ — only new ones move.
    return {"ok": True}


@app.post("/api/config/remove_camera")
def config_remove_camera(body: dict = Body(default={})):
    name = body.get("name") or ""
    if not any(c["name"] == name for c in CONFIG["cameras"]):
        raise HTTPException(404, f"no camera named {name!r} in config")
    if not REC.remove_camera(name):
        raise HTTPException(400, f"{name} is recording — stop it first")
    CONFIG["cameras"] = [c for c in CONFIG["cameras"] if c["name"] != name]
    CONFIG_PATH.write_text(yaml.safe_dump(CONFIG, sort_keys=False))
    apply_cameras()
    # Recordings under <record_dir>/<site>/<name>/ are deliberately left on disk.
    return {"ok": True}


# Module level, not under __main__: `uvicorn app:app` must refuse just as loudly.
if CURATION and not curators():
    sys.exit("FIELDKIT_MODE=curation needs dataset/curators.yaml with at least one "
             "handle: token — an internet-facing node must never run open")

if CURATION:
    threading.Thread(target=sync_loop, daemon=True).start()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
