#!/usr/bin/env python3
"""FieldKit — on-site capture & extraction console. Run: python app.py"""

import ipaddress
import shutil
import socket
from datetime import datetime
from pathlib import Path

import uvicorn
import yaml
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

import camera
import recorder

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"

CONFIG = {}


def load_config():
    """Re-read config.yaml into the module-level CONFIG dict."""
    CONFIG.clear()
    CONFIG.update(yaml.safe_load(CONFIG_PATH.read_text()) or {})
    return CONFIG


def save_config(cfg):
    """Validate-by-parse then write; keeps CONFIG in sync."""
    text = yaml.safe_dump(cfg, sort_keys=False)
    yaml.safe_load(text)
    CONFIG_PATH.write_text(text)
    return load_config()


def local_ips():
    """Non-loopback IPv4s of this host, stdlib only."""
    ips = set()
    for _, _, _, _, addr in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
        ips.add(addr[0])
    # getaddrinfo alone misses the LAN address on macOS/Jetson; ask the routing table too.
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # no packets sent, just picks the default route
        ips.add(s.getsockname()[0])
    except OSError:
        pass
    finally:
        s.close()
    return sorted(ip for ip in ips if not ip.startswith("127."))


def record_dir():
    d = ROOT / CONFIG.get("record_dir", "./recordings")
    d.mkdir(parents=True, exist_ok=True)
    return d


load_config()
REC = recorder.Recorder(CONFIG.get("cameras", []), record_dir(), CONFIG.get("site", "site1"))

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
    }


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


def cam_creds(ip):
    """Configured credentials for this IP, else the discovery defaults."""
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
    return {"cameras": camera.scan(cidrs, cam_creds), "cidrs": cidrs}


@app.post("/api/camera/activate")
def camera_activate(body: dict = Body(default={})):
    if not body.get("password"):
        raise HTTPException(400, "password required")
    return camera.activate(valid_ip(body.get("ip")), body["password"])


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
def camera_snapshot(ip: str, user: str = "", password: str = ""):
    valid_ip(ip)
    if not user:
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


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
