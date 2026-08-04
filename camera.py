#!/usr/bin/env python3
"""Hikvision discovery (SADP + TCP sweep), activation, addressing, preview."""

import ipaddress
import json
import re
import socket
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote
from xml.sax.saxutils import escape

import requests
from requests.auth import HTTPDigestAuth

SADP_GROUP = "239.255.255.250"
SADP_PORT = 37020
PROBE = ('<?xml version="1.0" encoding="utf-8"?>'
         '<Probe><Uuid>{uuid}</Uuid><Types>inquiry</Types></Probe>')

ACTIVATE_BODY = "<ActivateInfo><password>{pw}</password></ActivateInfo>"

IFACE_BODY = """<?xml version="1.0" encoding="UTF-8"?>
<NetworkInterface xmlns="http://www.hikvision.com/ver20/XMLSchema">
  <id>1</id>
  <IPAddress>
    <ipVersion>v4</ipVersion>
    <addressingType>static</addressingType>
    <ipAddress>{address}</ipAddress>
    <subnetMask>{mask}</subnetMask>
    <DefaultGateway><ipAddress>{gateway}</ipAddress></DefaultGateway>
  </IPAddress>
</NetworkInterface>"""


def _tag(xml, name):
    m = re.search(rf"<{name}>(.*?)</{name}>", xml, re.S | re.I)
    return m.group(1).strip() if m else ""


def norm_mac(mac):
    """Lowercase colon-separated: SADP uses dashes, ISAPI uses colons."""
    h = re.sub(r"[^0-9a-fA-F]", "", mac or "").lower()
    return ":".join(h[i:i + 2] for i in range(0, 12, 2)) if len(h) == 12 else (mac or "").lower()


def parse_sadp_reply(xml):
    """Pure: one SADP <Probematch> XML → normalised dict."""
    return {
        "ip": _tag(xml, "IPv4Address"),
        "mac": norm_mac(_tag(xml, "MAC")),
        "model": _tag(xml, "DeviceDescription"),
        "activated": _tag(xml, "Activated").lower() in ("true", "1", "yes"),
        "serial": _tag(xml, "DeviceSN"),
        "source": "sadp",
    }


def _sadp_socket():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    try:
        # Devices answer to the group as well as to us, so join it and listen there.
        s.bind(("", SADP_PORT))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                     socket.inet_aton(SADP_GROUP) + socket.inet_aton("0.0.0.0"))
    except OSError:
        s.bind(("", 0))          # port busy: unicast replies only
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    s.settimeout(0.5)
    return s


def sadp_scan(timeout=3.0):
    """Broadcast a SADP inquiry, collect replies for `timeout` seconds. → {mac: dict}"""
    s = _sadp_socket()
    out = {}
    try:
        try:
            s.sendto(PROBE.format(uuid=uuid.uuid4()).encode(), (SADP_GROUP, SADP_PORT))
        except OSError:
            return out       # no multicast route: let the TCP sweep carry the scan
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, _ = s.recvfrom(8192)
            except socket.timeout:
                continue
            except OSError:
                break
            d = parse_sadp_reply(data.decode("utf-8", "replace"))
            if d["mac"] and d["ip"]:
                out[d["mac"]] = d
    finally:
        s.close()
    return out


def probe_device(ip, user, password, timeout=1.0):
    """GET /ISAPI/System/deviceInfo on one host. → dict or None."""
    try:
        r = requests.get(f"http://{ip}/ISAPI/System/deviceInfo",
                         auth=HTTPDigestAuth(user, password), timeout=(1.0, timeout))
    except requests.RequestException:
        return None
    base = {"ip": ip, "reachable": True, "source": "sweep"}
    if r.status_code == 401:
        # activated=None, not True: a factory-fresh camera rejects deviceInfo too, so a
        # 401 proves nothing either way. None is falsy, so the merge keeps SADP's answer.
        return {**base, "mac": "", "model": "", "activated": None,
                "note": "credentials rejected"}
    if r.status_code != 200 or "DeviceInfo" not in r.text:
        return None
    return {**base, "activated": True, "mac": norm_mac(_tag(r.text, "macAddress")),
            "model": _tag(r.text, "model"), "serial": _tag(r.text, "serialNumber")}


def tcp_sweep(cidrs, cred_lookup, workers=64):
    """Probe every host of each /24. cred_lookup(ip) -> (user, password)."""
    hosts = []
    for c in dict.fromkeys(cidrs):
        hosts.extend(str(h) for h in ipaddress.ip_network(c, strict=False).hosts())
    out = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for d in ex.map(lambda ip: probe_device(ip, *cred_lookup(ip)), hosts):
            if d:
                out[d["mac"] or d["ip"]] = d
    return out


def scan(cidrs, cred_lookup, sadp_timeout=3.0):
    """SADP and sweep in parallel, merged on normalised MAC, falling back to IP."""
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_sadp = ex.submit(sadp_scan, sadp_timeout)
        f_sweep = ex.submit(tcp_sweep, cidrs, cred_lookup)
        found, swept = f_sadp.result(), f_sweep.result()
    # A 401 row carries no MAC, so match it to the SADP row by address or the same
    # camera lands in the table twice.
    by_ip = {d["ip"]: k for k, d in found.items() if d.get("ip")}
    for key, d in swept.items():
        target = key if key in found else by_ip.get(d["ip"])
        if target:
            found[target].update({k: v for k, v in d.items() if v})
            found[target].update(source="sadp+sweep", reachable=True)
        else:
            found[key] = d
    for d in found.values():
        d.setdefault("reachable", False)
    return sorted(found.values(), key=lambda d: _ipkey(d["ip"]))


def _ipkey(ip):
    try:
        return int(ipaddress.ip_address(ip))
    except ValueError:
        return 0


def activate(ip, password, timeout=10):
    """Plain-XML activation. Encrypted/RSA variant is out of scope per spec."""
    try:
        r = requests.put(f"http://{ip}/ISAPI/System/activate",
                         data=ACTIVATE_BODY.format(pw=escape(password)),
                         headers={"Content-Type": "application/xml"}, timeout=timeout)
    except requests.RequestException as e:
        return {"ok": False, "error": str(e)}
    if r.status_code in (400, 401):
        try:
            caps = requests.get(f"http://{ip}/ISAPI/System/security/capabilities",
                                timeout=timeout).text[:400]
        except requests.RequestException:
            caps = ""
        return {"ok": False, "status": r.status_code, "capabilities": caps,
                "error": "activation rejected — password too weak (need 8+ chars, mixed) "
                         "or device needs encrypted activation (use SADP tool)"}
    return {"ok": r.status_code == 200, "status": r.status_code, "response": r.text[:300]}


def set_static_ip(ip, user, password, address, mask="255.255.255.0", gateway="", timeout=10):
    for v in (address, mask, gateway or address):
        ipaddress.ip_address(v)          # all three land in the device XML — reject junk
    body = IFACE_BODY.format(address=address, mask=mask,
                             gateway=gateway or address.rsplit(".", 1)[0] + ".1")
    try:
        r = requests.put(f"http://{ip}/ISAPI/System/Network/interfaces/1", data=body,
                         auth=HTTPDigestAuth(user, password),
                         headers={"Content-Type": "application/xml"}, timeout=timeout)
    except requests.RequestException as e:
        return {"ok": False, "error": str(e)}
    out = {"ok": r.status_code == 200, "status": r.status_code, "response": r.text[:300]}
    if out["ok"] and "reboot" in r.text.lower():
        try:
            requests.put(f"http://{ip}/ISAPI/System/reboot",
                         auth=HTTPDigestAuth(user, password), timeout=timeout)
            out["rebooted"] = True
        except requests.RequestException as e:
            out["rebooted"] = False
            out["reboot_error"] = str(e)
    return out


def test_rtsp(ip, user, password, timeout=8):
    """Server-side ffprobe of the main stream. Never probes /102."""
    url = (f"rtsp://{quote(user, safe='')}:{quote(password, safe='')}"
           f"@{ip}:554/Streaming/Channels/101")
    try:
        p = subprocess.run(["ffprobe", "-rtsp_transport", "tcp", "-print_format", "json",
                            "-show_streams", "-v", "error", url],
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"no response in {timeout}s"}
    if p.returncode != 0:
        return {"ok": False, "error": (p.stderr or "ffprobe failed").strip()[:300]}
    v = next((s for s in json.loads(p.stdout or "{}").get("streams", [])
              if s.get("codec_type") == "video"), None)
    if not v:
        return {"ok": False, "error": "no video stream"}
    return {"ok": True, "codec": v.get("codec_name"), "width": v.get("width"),
            "height": v.get("height"), "fps": v.get("avg_frame_rate")}


def snapshot(ip, user, password, channel=101, timeout=8):
    """JPEG still for aiming. → (bytes, None) or (None, error)."""
    try:
        r = requests.get(f"http://{ip}/ISAPI/Streaming/channels/{channel}/picture",
                         auth=HTTPDigestAuth(user, password), timeout=timeout)
    except requests.RequestException as e:
        return None, str(e)
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"
    return r.content, None


if __name__ == "__main__":
    SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
    <Probematch><Uuid>x</Uuid><Types>inquiry</Types>
      <DeviceType>1</DeviceType>
      <DeviceDescription>DS-2CD2143G0-I</DeviceDescription>
      <DeviceSN>DS-2CD2143G0-I20200101AAWR123456</DeviceSN>
      <CommandPort>8000</CommandPort>
      <MAC>c4-2f-90-aa-bb-cc</MAC>
      <IPv4Address>192.168.1.64</IPv4Address>
      <IPv4SubnetMask>255.255.255.0</IPv4SubnetMask>
      <Activated>false</Activated>
    </Probematch>"""
    d = parse_sadp_reply(SAMPLE)
    assert d["ip"] == "192.168.1.64", d
    assert d["mac"] == "c4:2f:90:aa:bb:cc", d
    assert d["model"] == "DS-2CD2143G0-I", d
    assert d["activated"] is False, d
    assert parse_sadp_reply(SAMPLE.replace("false", "true"))["activated"] is True
    assert norm_mac("C42F90AABBCC") == "c4:2f:90:aa:bb:cc"
    assert parse_sadp_reply("<Probematch/>") == {
        "ip": "", "mac": "", "model": "", "activated": False, "serial": "", "source": "sadp"}

    DEVINFO = ("<DeviceInfo><model>DS-2CD2143G0-I</model>"
               "<macAddress>c4:2f:90:aa:bb:cc</macAddress></DeviceInfo>")
    assert _tag(DEVINFO, "model") == "DS-2CD2143G0-I"
    assert norm_mac(_tag(DEVINFO, "macAddress")) == "c4:2f:90:aa:bb:cc"

    # A 401 must report activation as unknown — the real probe, not a hand-copied row.
    from unittest.mock import patch
    fake401 = type("R", (), {"status_code": 401, "text": ""})()
    with patch.object(requests, "get", return_value=fake401):
        row401 = probe_device("192.168.1.64", "admin", "")
    assert row401["activated"] is None, row401

    def merge_with(sweep_row):
        globals()["sadp_scan"] = lambda t=0: {"c4:2f:90:aa:bb:cc": parse_sadp_reply(SAMPLE)}
        globals()["tcp_sweep"] = lambda c, f, workers=64: {
            sweep_row["mac"] or sweep_row["ip"]: sweep_row}
        return scan(["192.168.1.0/24"], lambda ip: ("admin", ""))

    # Factory-fresh camera: SADP says inactive, sweep 401s. One row, still INACTIVE,
    # so the operator keeps the Activate path (spec acceptance #2).
    rows = merge_with(row401)
    assert len(rows) == 1, rows
    assert rows[0]["activated"] is False, rows[0]
    assert rows[0]["model"] == "DS-2CD2143G0-I", rows[0]   # 401 row must not blank the model
    assert rows[0]["reachable"] and rows[0]["source"] == "sadp+sweep", rows[0]
    print("merge, sweep 401 :", {k: rows[0][k] for k in ("ip", "mac", "model", "activated")})

    # Authenticated 200 proves activation, so it wins over a stale SADP "false".
    rows = merge_with({"ip": "192.168.1.64", "mac": "c4:2f:90:aa:bb:cc", "activated": True,
                       "model": "DS-2CD2143G0-I", "reachable": True, "source": "sweep"})
    assert len(rows) == 1 and rows[0]["activated"] is True, rows
    print("merge, sweep 200 :", {k: rows[0][k] for k in ("ip", "mac", "model", "activated")})
    print("camera self-check ok:", d)
