#!/usr/bin/env python3
"""FieldKit — on-site capture & extraction console. Run: python app.py"""

import shutil
import socket
from datetime import datetime
from pathlib import Path

import uvicorn
import yaml
from fastapi import Body, FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

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


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
