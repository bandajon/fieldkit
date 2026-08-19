#!/usr/bin/env python3
"""FieldKit — on-site capture & extraction console. Run: python app.py"""

import atexit
import ipaddress
import os
import re
import shutil
import socket
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
DET_CLASSES = list(detect.CLASSES.values())       # dataset class ids 0-3, the order detect.py writes


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
OFFLOAD = offload.Offload(CONFIG, record_dir())   # holds CONFIG: config edits land live
OFFLOAD.start()   # no-op unless offload.enabled is set
# lambda: record_status is a route defined further down, and the heartbeat sends its
# body verbatim — the console sees exactly what the local Record tab does.
HIVE = hive.Hive(CONFIG, REC, lambda: record_status(), camera.snapshot, local_ips)
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
                         creds_fn=lambda ip: cam_creds(ip), dataset_dir=DATASET)
DETECT.start()   # no-op unless the optional detection deps are installed


@atexit.register
def _shutdown():
    """Children spawned in their own process group survive Ctrl-C; an orphaned ffmpeg
    would then fight the restarted app for the same segment paths."""
    REC.stop()
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


@app.get("/api/detect/frame")
def detect_frame(name: str):
    cam = next((c for c in CONFIG["cameras"] if c["name"] == name), None)
    if not cam:
        raise HTTPException(404, f"no camera named {name!r} in config")
    data = DETECT.frame(name)
    if data is None:      # detection absent or between passes — show the live camera instead
        data, err = detect_snapshot(cam["ip"], "", "")
        if err:
            raise HTTPException(502, err)
    return Response(content=data, media_type="image/jpeg")


@app.get("/api/detect/counts")
def detect_counts():
    return DETECT.counts()


def valid_sample_id(sid):
    """Sample ids land in filesystem paths — a basename, never a path."""
    if not SAMPLE_ID.match(sid or ""):
        raise HTTPException(400, f"not a sample id: {sid!r}")
    return sid


def sample_paths(sid, tree="pending"):
    return (DATASET / tree / "images" / f"{sid}.jpg", DATASET / tree / "labels" / f"{sid}.txt")


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
            pending.append((p.stat().st_mtime, {"id": p.stem, "boxes": read_boxes(p)}))
        except OSError:      # sample reviewed away mid-listing
            continue
    pending.sort(key=lambda t: t[0], reverse=True)
    return {"classes": DET_CLASSES, "pending": [s for _, s in pending[:100]]}


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
    if action not in ("approve", "discard"):
        raise HTTPException(400, "action must be 'approve' or 'discard'")
    img, lbl = sample_paths(sid)
    if not img.is_file():
        raise HTTPException(404, f"no pending sample {sid!r}")
    if action == "discard":
        img.unlink(missing_ok=True)
        lbl.unlink(missing_ok=True)
        return {"ok": True}
    lines = []
    for b in body.get("boxes") or []:   # an empty list is legal: every box removed = a negative sample
        try:
            cls = int(b["cls"])
            coords = [float(b[k]) for k in ("cx", "cy", "w", "h")]
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, f"malformed box: {b!r}")
        if not 0 <= cls < len(DET_CLASSES) or not all(0 <= v <= 1 for v in coords):
            raise HTTPException(400, f"box out of range: {b!r}")
        lines.append(" ".join([str(cls)] + [f"{v:.6f}" for v in coords]))
    dst_img, dst_lbl = sample_paths(sid, "approved")
    dst_img.parent.mkdir(parents=True, exist_ok=True)
    dst_lbl.parent.mkdir(parents=True, exist_ok=True)
    dst_lbl.write_text("".join(l + "\n" for l in lines))   # the operator's edit wins over the model's guess
    img.rename(dst_img)
    lbl.unlink(missing_ok=True)
    return {"ok": True}


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
