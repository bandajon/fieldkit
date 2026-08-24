#!/usr/bin/env python3
"""FieldKit — on-site capture & extraction console. Run: python app.py"""

import atexit
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import threading
from datetime import datetime
from pathlib import Path

import uvicorn
import yaml
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
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
REC = recorder.Recorder(CONFIG.get("cameras", []), record_dir(), CONFIG.get("site", "site1"),
                        state_path=ROOT / "record_state.json")
LIVE = live.Live(CONFIG.get("cameras", []), CONFIG.get("go2rtc_binary", ""), ROOT / "go2rtc.yaml")
LIVE.start()   # no-op unless a binary is configured and present
# A reboot or crash must not end a session on an unattended node: re-arm whatever
# was recording when the process died (expired timers stay stopped).
resumed = REC.resume()
if resumed:
    print(f"resumed recording after restart: {', '.join(resumed)}", flush=True)
# Creds the ops console pushes: Hive writes this dict, Offload reads it. Deliberately
# NOT in CONFIG — CONFIG is dumped back to config.yaml on every camera edit, and
# load_config() clears it; either would put a pushed secret on disk or lose it.
CONSOLE_CREDS = {}
OFFLOAD = offload.Offload(CONFIG, record_dir(),   # holds CONFIG: config edits land live
                          console_creds=CONSOLE_CREDS)
OFFLOAD.start()   # no-op unless offload.enabled is set
# lambda: record_status is a route defined further down, and the heartbeat sends its
# body verbatim — the console sees exactly what the local Record tab does.
HIVE = hive.Hive(CONFIG, REC, lambda: record_status(), camera.snapshot, local_ips,
                 console_creds=CONSOLE_CREDS)
HIVE.start()   # no-op unless config.yaml has an ops: block
def detect_snapshot(ip, user, password):
    """cam_creds chain + short timeout: one dead camera must not stall the whole
    detection pass (camera.snapshot defaults to 8 s), and config-supplied IPs are
    untrusted (POST /api/config has no auth on the field LAN)."""
    try:
        ipaddress.ip_address(ip)
    except (ValueError, TypeError):
        return None, f"not an IP address: {ip!r}"
    return camera.snapshot(ip, *cam_creds(ip), timeout=3)


DETECT = detect.Detector(CONFIG.get("cameras", []), detect_snapshot, CONFIG,
                         creds_fn=lambda ip: cam_creds(ip), dataset_dir=DATASET,
                         counts_dir=ROOT / "counts",   # one JSON per day: the durable tallies
                         events_dir=ROOT / "events")   # per-vehicle records + evidence crops
DETECT.start()   # no-op unless the optional detection deps are installed


@atexit.register
def _shutdown():
    """Children spawned in their own process group survive Ctrl-C; an orphaned ffmpeg
    would then fight the restarted app for the same segment paths."""
    REC.shutdown()   # not stop(): a restart must not erase the sessions resume() re-arms
    LIVE.stop()
    DETECT.stop()

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


def label_files():
    """Every label file in both trees. ponytail: full scan per class edit — the
    dataset caps at ~500 pending samples; index them if that ever changes."""
    for tree in ("pending", "approved"):
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
    return {"frames": frames, "boxes": boxes}


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


@app.get("/api/dataset/samples")
def dataset_samples():
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
    return {"classes": dataset_classes(), "attributes": attr_vocab(),
            "attr_constraints": attr_meta("constraints"), "attr_defaults": attr_meta("defaults"),
            "attr_implies": attr_meta("implies"), "approved": approved_counts(),
            # the list is capped for payload size; pending_total is the real pile
            "pending_total": len(pending), "pending": [s for _, s in pending[:100]]}


@app.get("/api/dataset/image")
def dataset_image(id: str):
    img, _ = sample_paths(valid_sample_id(id))
    if not img.is_file():
        raise HTTPException(404, f"no pending sample {id!r}")
    return FileResponse(img, media_type="image/jpeg")


@app.post("/api/dataset/label")
def dataset_label(body: dict = Body(default={})):
    sid = valid_sample_id(body.get("id"))
    action = body.get("action")
    if action == "unapprove":
        src_img, src_lbl = sample_paths(sid, "approved")
        if not src_img.is_file():
            raise HTTPException(404, f"{sid} is not in the approved set")
        img, lbl = sample_paths(sid)
        img.parent.mkdir(parents=True, exist_ok=True)
        lbl.parent.mkdir(parents=True, exist_ok=True)
        src_img.rename(img)
        lbl.write_text(src_lbl.read_text() if src_lbl.is_file() else "")
        src_lbl.unlink(missing_ok=True)
        src_attrs, attrs_dst = attrs_path(sid, "approved"), attrs_path(sid)
        attrs = read_attrs(src_attrs)
        if attrs:
            attrs_dst.parent.mkdir(parents=True, exist_ok=True)
            attrs_dst.write_text(json.dumps(attrs))
        src_attrs.unlink(missing_ok=True)
        suggest_path(sid).unlink(missing_ok=True)   # the sample was reviewed once already
        for p in (img, lbl):
            os.utime(p, None)   # front of the review queue, last in line for eviction
        return {"ok": True, "id": sid, "boxes": read_boxes(lbl), "attrs": attrs}
    if action not in ("approve", "discard"):
        raise HTTPException(400, "action must be 'approve', 'discard' or 'unapprove'")
    img, lbl = sample_paths(sid)
    if not img.is_file():
        raise HTTPException(404, f"no pending sample {sid!r}")
    if action == "discard":
        img.unlink(missing_ok=True)
        lbl.unlink(missing_ok=True)
        attrs_path(sid).unlink(missing_ok=True)
        suggest_path(sid).unlink(missing_ok=True)
        return {"ok": True}
    lines, nclasses = [], len(dataset_classes())
    for b in body.get("boxes") or []:   # an empty list is legal: every box removed = a negative sample
        try:
            cls = int(b["cls"])
            coords = [float(b[k]) for k in ("cx", "cy", "w", "h")]
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, f"malformed box: {b!r}")
        if not 0 <= cls < nclasses or not all(0 <= v <= 1 for v in coords):
            raise HTTPException(400, f"box out of range: {b!r}")
        lines.append(" ".join([str(cls)] + [f"{v:.6f}" for v in coords]))
    attrs = valid_attrs(body.get("attrs"), len(lines))
    dst_img, dst_lbl = sample_paths(sid, "approved")
    dst_img.parent.mkdir(parents=True, exist_ok=True)
    dst_lbl.parent.mkdir(parents=True, exist_ok=True)
    dst_lbl.write_text("".join(l + "\n" for l in lines))   # the operator's edit wins over the model's guess
    dst_attrs = attrs_path(sid, "approved")
    if attrs:
        dst_attrs.parent.mkdir(parents=True, exist_ok=True)
        dst_attrs.write_text(json.dumps(attrs))
    else:
        dst_attrs.unlink(missing_ok=True)   # re-approved with the attributes cleared
    img.rename(dst_img)
    lbl.unlink(missing_ok=True)
    attrs_path(sid).unlink(missing_ok=True)
    suggest_path(sid).unlink(missing_ok=True)
    return {"ok": True}


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


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
