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
from datetime import datetime
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

TIME_BODY = ('<Time xmlns="http://www.hikvision.com/ver20/XMLSchema">'
             '<timeMode>manual</timeMode><localTime>{local}</localTime>'
             '<timeZone>{tz}</timeZone></Time>')


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


def sadp_scan(timeout=3.0, ifaces=()):
    """Broadcast a SADP inquiry, collect replies for `timeout` seconds. → {mac: dict}

    The probe egresses EVERY interface in `ifaces`, not just the default route: a
    laptop on hotspot WiFi with Ethernet to the camera switch would otherwise send
    the probe out the hotspot and never hear a camera.
    """
    s = _sadp_socket()
    out = {}
    try:
        probe = PROBE.format(uuid=uuid.uuid4()).encode()
        sent = 0
        for ip in ifaces or [""]:            # "" = whatever the routing table picks
            try:
                if ip:
                    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                                 socket.inet_aton(ip))
                s.sendto(probe, (SADP_GROUP, SADP_PORT))
                sent += 1
            except OSError:
                continue     # this interface refuses multicast; the others may not
        if not sent:
            return out       # no multicast route at all: the TCP sweep carries the scan
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
    """GET /ISAPI/System/deviceInfo on one host. → dict or None.

    Only presents credentials when it actually has a password: an empty-password
    digest attempt still counts toward Hikvision's failed-login lockout, and the
    sweep runs against every camera on every scan.
    """
    auth = HTTPDigestAuth(user, password) if password else None
    try:
        r = requests.get(f"http://{ip}/ISAPI/System/deviceInfo",
                         auth=auth, timeout=(1.0, timeout))
    except requests.RequestException:
        return None
    base = {"ip": ip, "reachable": True, "source": "sweep"}
    if r.status_code == 401:
        # activated=None, not True: a factory-fresh camera rejects deviceInfo too, so a
        # 401 proves nothing either way. None is falsy, so the merge keeps SADP's answer.
        return {**base, "mac": "", "model": "", "activated": None,
                "note": "credentials rejected"}
    if r.status_code == 403 and "xml" in r.headers.get("Content-Type", ""):
        # An inactive Hikvision refuses deviceInfo with an XML 403 (activated ones
        # use 401 for bad creds) — this IS the factory-fresh camera we're hunting.
        return {**base, "mac": "", "model": "", "activated": False,
                "note": "not activated"}
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


def scan(cidrs, cred_lookup, sadp_timeout=3.0, ifaces=()):
    """SADP and sweep in parallel, merged on normalised MAC, falling back to IP."""
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_sadp = ex.submit(sadp_scan, sadp_timeout, ifaces)
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
                "error": "activation rejected — password too weak (need 8+ chars, mixed) or "
                         f"this firmware only activates from its own page: open http://{ip} "
                         "in a browser, set the password there, rescan, then type that "
                         "password here and Set IP"}
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


def hik_tz(offset_minutes):
    """UTC offset → Hikvision tz string. The sign is INVERTED: UTC+2 is CST-2:00:00."""
    h, m = divmod(abs(offset_minutes), 60)
    return f"CST{'-' if offset_minutes >= 0 else '+'}{h}:{m:02d}:00"


def set_time(ip, user, password, timeout=10):
    """Put the camera on this host's local time. Factory units ship on China time."""
    now = datetime.now().astimezone()
    tz = hik_tz(round(now.utcoffset().total_seconds() / 60))
    url = f"http://{ip}/ISAPI/System/time"
    auth = HTTPDigestAuth(user, password)
    try:
        r = requests.put(url, data=TIME_BODY.format(local=now.isoformat(timespec="seconds"), tz=tz),
                         auth=auth, headers={"Content-Type": "application/xml"}, timeout=timeout)
    except requests.RequestException as e:
        return {"ok": False, "error": str(e)}
    if r.status_code != 200:
        return {"ok": False, "status": r.status_code, "response": r.text[:300]}
    try:
        # Read back rather than trust the 200 — firmware that ignores a field still says OK.
        got = _tag(requests.get(url, auth=auth, timeout=timeout).text, "localTime")
    except requests.RequestException:
        got = ""
    return {"ok": True, "camera_time": got, "tz": tz}


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
        globals()["sadp_scan"] = lambda t=0, i=(): {"c4:2f:90:aa:bb:cc": parse_sadp_reply(SAMPLE)}
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

    assert hik_tz(120) == "CST-2:00:00"      # Zambia/CAT
    assert hik_tz(-300) == "CST+5:00:00"
    assert hik_tz(330) == "CST-5:30:00"
    assert hik_tz(0) == "CST-0:00:00"

    sent = {}
    fake_time = type("R", (), {"status_code": 200,
                               "text": "<localTime>2026-08-05T14:26:20+02:00</localTime>"})()

    def fake_put(url, data=None, **kw):
        sent.update(url=url, data=data)
        return fake_time

    with patch.object(requests, "put", fake_put), patch.object(requests, "get", return_value=fake_time):
        t = set_time("192.168.1.64", "admin", "pw")
    assert t["ok"] and t["camera_time"].startswith("2026-08-05"), t
    assert sent["url"].endswith("/ISAPI/System/time"), sent
    assert "<timeMode>manual</timeMode>" in sent["data"], sent
    host_tz = hik_tz(round(datetime.now().astimezone().utcoffset().total_seconds() / 60))
    assert f"<timeZone>{host_tz}</timeZone>" in sent["data"], sent
    print("set_time         :", t)
    print("camera self-check ok:", d)
