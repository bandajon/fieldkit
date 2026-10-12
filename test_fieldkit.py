#!/usr/bin/env python3
"""Plain stdlib test runner: python3 test_fieldkit.py — exits non-zero on failure."""

import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import camera          # noqa: E402
import live            # noqa: E402

FAILS = []


def check(name, fn):
    try:
        fn()
        print(f"ok   {name}")
    except Exception as e:
        FAILS.append(name)
        print(f"FAIL {name}: {e}")


def self_check(module):
    """Each module ships an assert-based __main__ self-check; run it as-is."""
    def run():
        r = subprocess.run([sys.executable, str(ROOT / module)], capture_output=True, text=True)
        assert r.returncode == 0, (r.stdout + r.stderr)[-400:]
    return run


# Spec acceptance #2: an inactive camera must be reported as inactive.
INACTIVE = """<Probematch><DeviceDescription>DS-2CD2143G0-I</DeviceDescription>
  <MAC>c4-2f-90-aa-bb-cc</MAC><IPv4Address>192.168.1.64</IPv4Address>
  <Activated>false</Activated></Probematch>"""


def sadp_inactive():
    d = camera.parse_sadp_reply(INACTIVE)
    assert d["activated"] is False, d
    assert d["ip"] == "192.168.1.64" and d["mac"] == "c4:2f:90:aa:bb:cc", d
    assert camera.parse_sadp_reply(INACTIVE.replace("false", "true"))["activated"] is True


def macs():
    for raw in ("c4-2f-90-aa-bb-cc", "C4:2F:90:AA:BB:CC", "c42f90aabbcc", "C4 2F 90 AA BB CC"):
        assert camera.norm_mac(raw) == "c4:2f:90:aa:bb:cc", raw
    assert camera.norm_mac("") == ""


def live_absent():
    cams = [{"name": "cam1", "ip": "10.0.0.1", "user": "u", "password": "p"}]
    assert live.Live(cams, "", ROOT / "go2rtc.yaml").state() == "absent"
    assert live.Live(cams, "/no/such/go2rtc", ROOT / "go2rtc.yaml").state() == "absent"
    assert "/102" in live.sub_url(cams[0]) and "/101" not in live.sub_url(cams[0])


def readme_documents_config():
    """Every config key must appear in the README reference — no eyeballing."""
    readme = (ROOT / "README.md").read_text()
    # config.example.yaml, not config.yaml: the latter is untracked and field-edited.
    cfg = yaml.safe_load((ROOT / "config.example.yaml").read_text())
    keys = set(cfg)
    for entry in cfg.get("cameras") or []:
        keys |= set(entry)
    keys |= set(cfg.get("camera_defaults") or {})
    missing = sorted(k for k in keys if not re.search(rf"\b{re.escape(k)}\b", readme))
    assert not missing, f"undocumented config keys: {missing}"


def hostnet_pick_iface():
    """Mocked interface lists only — never touches this machine's networking."""
    import hostnet
    HOTSPOT = {"name": "en0", "ips": ["172.20.10.8"], "up": True, "link_local": False}
    SWITCH = {"name": "en8", "ips": ["169.254.31.7"], "up": True, "link_local": True}
    WIRED = {"name": "en8", "ips": [], "up": True, "link_local": False}
    DOWN = {"name": "en5", "ips": [], "up": False, "link_local": False}
    WIFI = {"name": "awdl0", "ips": [], "up": True, "link_local": False}

    # The self-assigned interface is the field-switch signature and wins outright.
    assert hostnet.pick_iface("172.20.10.8", [HOTSPOT, WIRED, SWITCH])["name"] == "en8"
    # No 169.254: fall back to a wired interface that is not the default route.
    assert hostnet.pick_iface("172.20.10.8", [HOTSPOT, DOWN, WIFI, WIRED])["name"] == "en8"
    # Never hand back the interface carrying the default route.
    assert hostnet.pick_iface("172.20.10.8", [HOTSPOT]) is None
    assert hostnet.pick_iface("172.20.10.8", [DOWN, WIFI]) is None


def hostnet_arp_parser():
    """Captured real output. A MAC means the address is taken."""
    import hostnet
    taken = [
        "? (192.168.1.2) at 0:e0:4c:61:1:9a on en8 ifscope [ethernet]",       # darwin
        "192.168.1.2 dev eth0 lladdr 04:ee:cd:5e:73:17 REACHABLE",            # linux
        "  Internet Address      Physical Address      Type\n"
        "  192.168.1.2          04-ee-cd-5e-73-17     dynamic",              # windows
    ]
    free = [
        "? (192.168.1.2) at (incomplete) on en8 ifscope [ethernet]",          # darwin
        "192.168.1.2 (192.168.1.2) -- no entry",                              # darwin
        "192.168.1.2 dev eth0 FAILED",                                        # linux
        "192.168.1.2 dev eth0  INCOMPLETE",                                   # linux
        "No ARP Entries Found.",                                              # windows
        "",
    ]
    for t in taken:
        assert hostnet.parse_arp(t) is True, t
    for t in free:
        assert hostnet.parse_arp(t) is False, t


def scan_auto_joins_once():
    """Two off-net cameras must still trigger exactly one join."""
    from unittest.mock import patch
    import app
    rows = [{"ip": "192.168.1.80"}, {"ip": "10.9.9.9"}]
    calls = []

    def fake_join(cidr, exclude_ip=""):
        calls.append(cidr)
        return {"ok": True, "ip": "192.168.1.3", "iface": "en8"}

    with patch.object(app, "local_ips", lambda: ["172.20.10.8"]), \
         patch.object(app.camera, "scan", lambda *a, **k: rows), \
         patch.object(app.hostnet, "bootstrap", lambda **k: None), \
         patch.object(app.hostnet, "join", fake_join):
        out = app.camera_scan({})
    assert out["joined"] == {"ip": "192.168.1.3", "iface": "en8"}, out
    assert calls == ["192.168.1.80/24"], calls          # exactly one, the first orphan

    # A failed join surfaces the operator's escape hatch instead of a joined key.
    with patch.object(app, "local_ips", lambda: ["172.20.10.8"]), \
         patch.object(app.camera, "scan", lambda *a, **k: rows), \
         patch.object(app.hostnet, "bootstrap", lambda **k: None), \
         patch.object(app.hostnet, "join",
                      lambda c, exclude_ip="": {"ok": False, "error": "nope",
                                                "cmd": "sudo ...", "grant": "echo ..."}):
        out = app.camera_scan({})
    assert "joined" not in out and out["join_error"]["error"] == "nope", out

    # Nothing off-net: no join attempted at all.
    with patch.object(app, "local_ips", lambda: ["192.168.1.2"]), \
         patch.object(app.camera, "scan", lambda *a, **k: [{"ip": "192.168.1.80"}]), \
         patch.object(app.hostnet, "bootstrap", lambda **k: None), \
         patch.object(app.hostnet, "join", fake_join):
        out = app.camera_scan({})
    assert "joined" not in out and "join_error" not in out, out
    assert calls == ["192.168.1.80/24"], calls


def hostnet_jetson():
    """Jetson Orin realities: enP8p1s0 naming, bare ports, NO-CARRIER, bootstrap."""
    from unittest.mock import patch
    import hostnet

    assert hostnet.WIRED.match("enP8p1s0"), "Jetson Orin port name"
    assert hostnet.WIRED.match("eth0") and not hostnet.WIRED.match("wlan0")
    assert hostnet.WIRED.match("enxb827eb123456"), "Debian/Pi USB dongle name"
    assert not hostnet.WIRED.match("enx"), "bare prefix is not an interface"

    # A bare wired port (Linux on a DHCP-less switch) beats one that has an address.
    BUSY = {"name": "eth1", "ips": ["10.0.0.5"], "up": True, "link_local": False}
    BARE = {"name": "enP8p1s0", "ips": [], "up": True, "link_local": False}
    assert hostnet.pick_iface("", [BUSY, BARE])["name"] == "enP8p1s0"

    # ip-link parsing: admin-UP with NO-CARRIER means the cable is out.
    LINK = ("1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536\n"
            "2: enP8p1s0: <NO-CARRIER,BROADCAST,MULTICAST,UP> mtu 1500\n"
            "3: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500\n")
    ADDR = "3: eth0    inet 10.0.0.5/24 brd 10.0.0.255 scope global eth0\n"
    with patch.object(hostnet, "_run",
                      lambda argv: LINK if argv[:3] == ["ip", "-o", "link"] else ADDR):
        ifs = {i["name"]: i for i in hostnet._iproute()}
    assert ifs["eth0"]["up"] and ifs["eth0"]["ips"] == ["10.0.0.5"], ifs
    assert not ifs["enP8p1s0"]["up"], ifs

    # bootstrap: only on Linux, only for a bare port, parks the link-local slice.
    calls = []
    with patch.object(hostnet, "DARWIN", False), patch.object(hostnet, "WINDOWS", False), \
         patch.object(hostnet, "pick_iface", lambda e="": BARE), \
         patch.object(hostnet, "join", lambda c, e="": calls.append(c) or {"ok": True}):
        assert hostnet.bootstrap()["ok"]
    assert calls == [hostnet.LINK_LOCAL], calls
    with patch.object(hostnet, "DARWIN", False), patch.object(hostnet, "WINDOWS", False), \
         patch.object(hostnet, "pick_iface", lambda e="": BUSY):
        assert hostnet.bootstrap() is None      # port already has an address
    with patch.object(hostnet, "DARWIN", True):
        assert hostnet.bootstrap() is None      # macOS self-assigns by itself


def hostnet_windows():
    """Captured Get-NetAdapter CSV: dongles report PhysicalMediaType Unspecified,
    so wired = physical + Up + not-wireless — never require 802.3."""
    from unittest.mock import patch
    import hostnet
    CSV = ('"Name","Status","PhysicalMediaType","InterfaceDescription"\n'
           '"Ethernet","Disconnected","802.3","Realtek PCIe GbE Family Controller"\n'
           '"Ethernet 2","Up","Unspecified","Realtek USB GbE Family Controller"\n'
           '"Wi-Fi","Up","Native 802.11","Intel(R) Wi-Fi 6 AX201 160MHz"\n'
           '"Bluetooth Network Connection","Disconnected","BlueTooth","Bluetooth Device (PAN)"\n'
           '"Ethernet 3","Up","802.3","TAP-Windows Adapter V9"\n')
    assert hostnet.parse_win_adapters(CSV) == ["Ethernet 2"], hostnet.parse_win_adapters(CSV)
    assert hostnet.parse_win_adapters("") == []

    # pick_iface falls back to the adapter layer on Windows: no 169.254 yet
    # (APIPA takes ~30 s), name never matches the WIRED regex — dongle still found.
    with patch.object(hostnet, "WINDOWS", True), \
         patch.object(hostnet, "list_ifaces", lambda: [
             {"name": "Wi-Fi", "ips": ["192.168.8.10"], "up": True, "link_local": False}]), \
         patch.object(hostnet, "_win_wired", lambda: ["Ethernet 2"]):
        assert hostnet.pick_iface("192.168.8.10")["name"] == "Ethernet 2"
        # But an APIPA adapter (already self-assigned on the switch) still wins
        # without any powershell call.
    with patch.object(hostnet, "WINDOWS", True), \
         patch.object(hostnet, "list_ifaces", lambda: [
             {"name": "Ethernet 2", "ips": ["169.254.7.9"], "up": True, "link_local": True}]), \
         patch.object(hostnet, "_win_wired", lambda: (_ for _ in ()).throw(AssertionError("not needed"))):
        assert hostnet.pick_iface("")["name"] == "Ethernet 2"


def setup_linux_script():
    """The Pi/Jetson installer must parse and keep its load-bearing steps."""
    p = ROOT / "setup-linux.sh"
    r = subprocess.run(["sh", "-n", str(p)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    text = p.read_text()
    for needle in ("python3 -m venv",              # Bookworm+ blocks bare pip (PEP 668)
                   "sudoers.d/fieldkit",           # silent Scan-LAN join
                   "ip) addr add *",               # scoped: bare ip grants a root shell (netns)
                   "visudo -cf",                   # bad sudoers.d disables sudo machine-wide
                   "systemctl enable --now",       # survives a power cycle
                   "KillSignal=SIGINT",            # ffmpeg finalises the last segment
                   "--no-index --find-links"):     # offline install from the release zip
        assert needle in text, f"setup-linux.sh lost: {needle}"
    assert os.access(p, os.X_OK), "setup-linux.sh must be executable"


def setup_windows_script():
    """The Windows installer must keep its load-bearing steps."""
    text = (ROOT / "setup-windows.ps1").read_text()
    for needle in ("Register-ScheduledTask",       # boot-on-power, like systemd on the Pi
                   "-UserId 'SYSTEM'",             # elevation for runtime netsh joins
                   "-AtStartup",
                   "ExecutionTimeLimit (New-TimeSpan)",  # default kills the app after 72 h
                   "Unblock-File",                 # Mark-of-the-Web breaks extracted zips
                   "localport=8080",               # phone UI through the firewall
                   "localport=37020"):             # SADP replies through the firewall
        assert needle in text, f"setup-windows.ps1 lost: {needle}"


def scan_skips_own_link_local():
    """The parked 169.254 slice is never swept — SADP finds self-assigned cameras."""
    import app
    assert app.scan_cidrs(["192.168.1.2", "169.254.100.2"]) == ["192.168.1.2/24"]


def local_ips_sees_all_ifaces():
    """A joined or link-local address must reach the SADP egress list on Linux."""
    from unittest.mock import patch
    import app
    with patch.object(app.hostnet, "list_ifaces",
                      lambda: [{"name": "eth0", "ips": ["169.254.100.2"], "up": True,
                                "link_local": True}]):
        assert "169.254.100.2" in app.local_ips()


def hostnet_addressing():
    import ipaddress, hostnet
    net = ipaddress.ip_network("192.168.1.0/24")
    assert hostnet.free_addr(net, in_use=lambda ip: False) == "192.168.1.2"
    assert hostnet.free_addr(net, in_use=lambda ip: ip.endswith(".2")) == "192.168.1.3"
    assert hostnet.free_addr(net, in_use=lambda ip: True) is None      # .2-.10 all taken
    assert hostnet.free_addr(net, in_use=lambda ip: not ip.endswith('.10')) == '192.168.1.10'
    # Refuses public space before running any command.
    r = hostnet.join("8.8.8.0/24")
    assert r["ok"] is False and "not a private network" in r["error"], r


def sadp_probes_every_interface():
    """Fake socket — never touches the wire. One dead interface must not stop the rest."""
    import socket as sk
    from unittest.mock import patch
    sent, current = [], [""]

    class FakeSock:
        def setsockopt(self, level, opt, val):
            if opt == sk.IP_MULTICAST_IF:
                current[0] = sk.inet_ntoa(val)

        def sendto(self, data, addr):
            assert addr == (camera.SADP_GROUP, camera.SADP_PORT), addr
            if current[0] == "10.0.0.9":
                raise OSError("no route to host")
            sent.append(current[0])

        def recvfrom(self, n):
            raise sk.timeout

        def close(self):
            pass

    with patch.object(camera, "_sadp_socket", FakeSock):
        camera.sadp_scan(timeout=0, ifaces=["192.168.1.2", "10.0.0.9", "172.16.0.5"])
    assert sent == ["192.168.1.2", "172.16.0.5"], sent

    with patch.object(camera, "_sadp_socket", FakeSock):   # no ifaces = default route
        sent.clear(); current[0] = ""
        camera.sadp_scan(timeout=0)
    assert sent == [""], sent


def probe_status_codes():
    """A factory-fresh camera answers deviceInfo with an XML 403; junk on :80 is not a camera."""
    from unittest.mock import patch
    import requests as rq

    def fake(status, ctype="application/xml", text=""):
        return type("R", (), {"status_code": status, "text": text,
                              "headers": {"Content-Type": ctype}})()

    with patch.object(rq, "get", return_value=fake(403, text="<ResponseStatus/>")):
        d = camera.probe_device("10.0.0.5", "admin", "")
        assert d["activated"] is False and d["note"] == "not activated", d
    with patch.object(rq, "get", return_value=fake(404, "text/html", "<html>nope</html>")):
        assert camera.probe_device("10.0.0.5", "admin", "") is None
    with patch.object(rq, "get", return_value=fake(403, "text/html", "<html>nginx</html>")):
        assert camera.probe_device("10.0.0.5", "admin", "") is None   # 403 but not XML


def camera_tz_strings():
    """Hikvision's tz string inverts the sign: Zambia (UTC+2) is CST-2:00:00."""
    assert camera.hik_tz(120) == "CST-2:00:00"
    assert camera.hik_tz(-300) == "CST+5:00:00"
    assert camera.hik_tz(330) == "CST-5:30:00"
    assert camera.hik_tz(0) == "CST-0:00:00"


def camera_set_time():
    """Mocked ISAPI — never touches a real camera."""
    from unittest.mock import patch
    import requests as rq
    sent = {}
    fake = type("R", (), {"status_code": 200,
                          "text": "<localTime>2026-08-05T14:26:20+02:00</localTime>"})()

    def fake_put(url, data=None, **kw):
        sent.update(url=url, data=data)
        return fake

    with patch.object(rq, "put", fake_put), patch.object(rq, "get", return_value=fake):
        r = camera.set_time("10.0.0.5", "admin", "pw")
    assert r["ok"] and r["camera_time"].startswith("2026-08-05"), r
    assert sent["url"].endswith("/ISAPI/System/time"), sent
    assert "<timeMode>manual</timeMode>" in sent["data"], sent


def camera_names():
    """The name becomes a directory under record_dir, so it must stay a plain word."""
    import app
    for bad in ("../evil", "a/b", "", "x" * 33, "cam 1", ".", "café"):
        assert not app.CAM_NAME.match(bad), bad
    for good in ("cam1", "gate-north", "CAM_9", "x" * 32):
        assert app.CAM_NAME.match(good), good


def recorder_add_camera():
    import tempfile, recorder
    r = recorder.Recorder([], tempfile.mkdtemp(), "selftest")
    r.add_camera({"name": "cam9", "ip": "10.0.0.9", "user": "u", "password": "p"})
    assert r.status()["cam9"]["state"] == "STOPPED", r.status()
    r.st["cam9"]["desired"] = True                      # pretend it is recording
    r.add_camera({"name": "cam9", "ip": "10.0.0.9", "user": "u", "password": "p"})
    assert r.st["cam9"]["desired"], "re-add wiped a live session"


def recorder_timed_stop():
    """A timed run must finalise itself: the crew starts it and drives away."""
    import tempfile, time, recorder
    r = recorder.Recorder([{"name": "t", "ip": "127.0.0.1", "user": "u", "password": "p"}],
                          tempfile.mkdtemp(), "selftest")
    r.start(["t"], hours=0.001)                    # 3.6 s
    assert r.status()["t"]["until"], r.status()["t"]
    for _ in range(24):
        if r.status()["t"]["state"] == "STOPPED":
            break
        time.sleep(0.5)
    assert r.status()["t"]["state"] == "STOPPED", r.status()["t"]
    assert r.st["t"]["desired"] is False and r.st["t"]["until"] is None, r.st["t"]
    r.start(["t"], hours=0)                        # non-positive = until Stop
    assert r.st["t"]["until"] is None
    r.stop(["t"])


def record_start_hours():
    """Bad hours must be refused at the boundary, not silently ignored."""
    import app
    from fastapi import HTTPException
    for bad in ("nope", "", 0, -1, 721, [2]):
        try:
            app.record_start({"cams": ["__nosuch__"], "hours": bad})
        except HTTPException as e:
            assert e.status_code == 400, (bad, e.status_code)
        else:
            raise AssertionError(f"accepted hours={bad!r}")
    # Valid: no camera by that name, so nothing spawns — we only want the route's shape.
    r = app.record_start({"cams": ["__nosuch__"], "hours": 2})
    assert "cameras" in r and r["disks"], r
    assert r["disks"][0]["free_gb"] > 0, r["disks"]


def offload_off_by_default():
    """Every node without an offload block runs this code path — it must be inert."""
    import tempfile, offload
    o = offload.Offload({}, tempfile.mkdtemp())
    assert not o.info()["enabled"], o.info()
    o.sweep()                                  # no block, no client, no crash
    assert not o.last_error and o.uploaded == 0, o.info()


def hive_off_by_default():
    """Every self-hosting node runs this path — it must not open a socket or a thread."""
    import tempfile, threading, hive, recorder
    rec = recorder.Recorder([], tempfile.mkdtemp(), "selftest")
    h = hive.Hive({}, rec, dict, lambda *a: (None, "x"), list)
    before = threading.active_count()
    h.start()
    assert h.info() == {"configured": False, "state": "OFF", "last_beat": 0.0, "seq": 0}
    assert threading.active_count() == before, "phoned home without an ops block"
    # A partly-filled block is not enrolment either.
    for half in ({"url": "ws://c/ingest"}, {"url": "ws://c/ingest", "token": "t"},
                 {"token": "t", "hive": "kalambo"}):
        assert not hive.Hive({"ops": half}, rec, dict, lambda *a: (None, "x"),
                             list).configured(), half


check("camera.py self-check", self_check("camera.py"))
check("hive.py self-check", self_check("hive.py"))
check("recorder.py self-check", self_check("recorder.py"))
check("live.py self-check", self_check("live.py"))
check("offload.py self-check", self_check("offload.py"))
check("offload off without a config block", offload_off_by_default)
check("hive off without a config block", hive_off_by_default)
check("sadp reports inactive camera", sadp_inactive)
check("mac normalisation", macs)
check("go2rtc absent without a binary", live_absent)
check("hostnet pick_iface", hostnet_pick_iface)
check("hostnet addressing", hostnet_addressing)
def supervisor_tokens():
    """The one security path worth a test: who may set the password, who may mint tokens."""
    import tempfile
    import app

    def refused(code, fn):
        try:
            fn()
        except app.HTTPException as e:
            assert e.status_code == code, f"wanted {code}, got {e.status_code}: {e.detail}"
            return
        raise AssertionError(f"expected {code}, call succeeded")

    keep = (app.DATASET, app.REVIEWERS, app.push_roster)
    with tempfile.TemporaryDirectory() as d:
        try:
            app.DATASET, app.REVIEWERS = Path(d), ["jonah"]
            app.push_roster = lambda: True            # no bucket in a test
            app.write_yaml("curators.yaml", {"jonah": "tok-jonah"})
            pw = "hunter2hunter2"

            # First sign-in must prove itself with the token, or the internet claims it.
            refused(401, lambda: app.dataset_login({"who": "jonah", "password": pw}, ""))
            assert app.dataset_login({"who": "jonah", "password": pw},
                                     "tok-jonah")["token"] == "tok-jonah"
            # Set: the password alone works now, a wrong one never does, and it is not stored raw.
            assert app.dataset_login({"who": "jonah", "password": pw}, "")["token"] == "tok-jonah"
            refused(401, lambda: app.dataset_login({"who": "jonah", "password": "nope-nope-nope"}, ""))
            assert pw not in (Path(d) / app.SUPERVISORS).read_text()
            # Only a supervisor, and only a real handle.
            refused(403, lambda: app.dataset_login({"who": "curator01", "password": pw}, ""))

            # Minting: a curator gets a short unguessable token, and the roster is what changed.
            out = app.curators_edit({"handle": "curator01"}, "tok-jonah")
            tok = out["curators"]["curator01"]
            assert 12 <= len(tok) <= 24, tok
            assert app.curators()["curator01"] == tok
            assert app.curators_edit({"handle": "curator02"}, "tok-jonah")["curators"]["curator02"] != tok
            # A curator cannot mint, and a supervisor cannot be deleted out from under REVIEWERS.
            refused(403, lambda: app.curators_edit({"handle": "curator03"}, tok))
            refused(400, lambda: app.curators_edit({"action": "remove", "handle": "jonah"},
                                                   "tok-jonah"))
            assert "curator01" not in app.curators_edit(
                {"action": "remove", "handle": "curator01"}, "tok-jonah")["curators"]
        finally:
            app.DATASET, app.REVIEWERS, app.push_roster = keep


def attr_value_remove():
    import json
    import tempfile
    import app
    keep = (app.DATASET, app.REVIEWERS, app.publish)
    with tempfile.TemporaryDirectory() as d:
        try:
            app.DATASET, app.REVIEWERS, app.publish = Path(d), ["jonah"], lambda name: False
            app.write_yaml("curators.yaml", {"jonah": "tok-jonah", "curator01": "tok-c1"})
            (Path(d) / "attributes.yaml").write_text(yaml.safe_dump({
                "axle-config": ["1+1", "1+2+2+2", "1222", "other"],
                "implies": {"axle-config": {"1222": {"axles": "7"}}},
                "defaults": {"e-heavy": {"axle-config": "1222"}}}, sort_keys=False))
            a = Path(d) / "approved" / "attrs"
            a.mkdir(parents=True)
            (a / "f1.json").write_text('{"0": {"axle-config": "1222", "cargo": "none"}, "1": {"axle-config": "1+1"}}')
            (a / "f2.json").write_text('{"0": {"axle-config": "1+1"}}')
            body = {"head": "axle-config", "value": "1222", "remove": True, "replace": "1+2+2+2"}
            try:
                app.dataset_attr_value(body, x_curator_token="tok-c1")
                raise AssertionError("a curator must not remove values")
            except app.HTTPException as e:
                assert e.status_code == 403, e.detail
            r = app.dataset_attr_value(body, x_curator_token="tok-jonah")
            assert r["rewritten"] == 1 and "1222" not in r["attributes"]["axle-config"], r
            f1 = json.loads((a / "f1.json").read_text())
            assert f1["0"] == {"axle-config": "1+2+2+2", "cargo": "none"} and f1["1"]["axle-config"] == "1+1", f1
            cfg = yaml.safe_load((Path(d) / "attributes.yaml").read_text())
            assert "1222" not in cfg["implies"]["axle-config"] and cfg["defaults"]["e-heavy"]["axle-config"] == "1+2+2+2", cfg
            try:
                app.dataset_attr_value({**body, "replace": "9+9"}, x_curator_token="tok-jonah")
                raise AssertionError("an unknown replacement must be refused")
            except app.HTTPException as e:
                assert e.status_code in (400, 404), e.detail
        finally:
            app.DATASET, app.REVIEWERS, app.publish = keep


def attr_restricts_are_real():
    """Every restricts rule must name a head and values the vocabulary actually has.

    A typo fails silently and expensively: labVocab filters the row down to nothing,
    hits its empty-list guard, hands back the class's whole list, and the curator sees
    all eleven cargo options with no sign the rule was ever meant to fire."""
    import app
    cfg = yaml.safe_load((app.DATASET / "attributes.yaml").read_text()) or {}
    vocab = {k: v for k, v in cfg.items() if isinstance(v, list)}
    seen = 0
    for head, values in (cfg.get("restricts") or {}).items():
        assert head in vocab, f"restricts names unknown head {head!r}"
        for value, narrows in values.items():
            assert value in vocab[head], f"restricts: {head} has no value {value!r}"
            for target, only in narrows.items():
                assert target in vocab, f"restricts {head}/{value} narrows unknown head {target!r}"
                assert target != head, f"restricts {head}/{value} narrows its own head"
                only = [only] if isinstance(only, str) else only
                assert only, f"restricts {head}/{value}/{target} is empty"
                for v in only:
                    assert v in vocab[target], \
                        f"restricts {head}/{value}/{target}: {v!r} is not a {target}"
                seen += 1
    assert seen, "no restrict rules — the tanker/cargo rule should be one of them"


def admins_vet_super_admin_releases():
    """A reviewer who is not the super admin signs a held frame off in place; only the
    super admin's review moves it to approved."""
    import tempfile
    import app
    keep = (app.DATASET, app.REVIEWERS, app.SUPERVISOR)
    with tempfile.TemporaryDirectory() as d:
        try:
            app.DATASET, app.REVIEWERS, app.SUPERVISOR = Path(d), ["jonah", "joseph"], "jonah"
            app.write_yaml("curators.yaml", {"jonah": "tok-jonah", "joseph": "tok-joseph"})
            (Path(d) / "classes.txt").write_text("a-small\ne-heavy\n")
            for sub in ("images", "labels"):
                (Path(d) / "holding" / sub).mkdir(parents=True)
            (Path(d) / "holding" / "images" / "h1.jpg").write_bytes(b"jpg")
            (Path(d) / "holding" / "labels" / "h1.txt").write_text("0 0.5 0.5 0.1 0.1\n")
            (Path(d) / "audit.jsonl").write_text('{"action":"approve","id":"h1","who":"brenda","held":true}\n')
            body = {"boxes": [{"cls": 1, "cx": .5, "cy": .5, "w": .1, "h": .1}], "attrs": {}}
            r = app.review_label("h1", "joseph", body)
            assert r["vetted"] and not r["released"], r
            assert (Path(d) / "holding" / "labels" / "h1.txt").read_text().startswith("1 "), "joseph's correction is the held record"
            assert not (Path(d) / "approved" / "labels" / "h1.txt").exists(), "still held"
            s = app.dataset_search(tree="holding", vetted=1, x_curator_token="tok-jonah")
            assert s["total"] == 1 and s["results"][0]["vetted"] == "joseph", s
            assert app.dataset_search(tree="holding", x_curator_token="tok-jonah")["total"] == 1
            rel = app.dataset_release({"vetted": 1}, x_curator_token="tok-joseph")
            assert (rel["released"], rel["vetted"]) == (0, 1), "joseph cannot release, only vet again"
            rel = app.dataset_release({"vetted": 1}, x_curator_token="tok-jonah")
            assert (rel["released"], rel["vetted"]) == (1, 0), rel
            assert (Path(d) / "approved" / "labels" / "h1.txt").is_file(), "the super admin releases"
            assert app.dataset_search(tree="holding", x_curator_token="tok-jonah")["total"] == 0
        finally:
            app.DATASET, app.REVIEWERS, app.SUPERVISOR = keep


def search_lists_the_pen():
    """No class picked = the whole tree, tallied by curator: the supervisor's view of holding."""
    import tempfile
    import app

    keep = (app.DATASET, app.REVIEWERS)
    with tempfile.TemporaryDirectory() as d:
        try:
            app.DATASET, app.REVIEWERS = Path(d), ["jonah"]
            app.write_yaml("curators.yaml", {"jonah": "tok-jonah"})
            (Path(d) / "classes.txt").write_text("a-small\ne-heavy\n")
            pen = Path(d) / "holding" / "labels"
            pen.mkdir(parents=True)
            (pen / "x1.txt").write_text("0 0.5 0.5 0.1 0.1\n")
            (pen / "x2.txt").write_text("1 0.5 0.5 0.3 0.3\n")
            (pen / "x3.txt").write_text("1 0.4 0.4 0.3 0.3\n")
            (Path(d) / "audit.jsonl").write_text(
                '{"action":"approve","id":"x1","who":"curator01","held":true}\n'
                '{"action":"approve","id":"x2","who":"curator02","held":true}\n'
                '{"action":"approve","id":"x3","who":"curator02","held":true}\n')
            s = lambda **kw: app.dataset_search(tree="holding", x_curator_token="tok-jonah", **kw)
            whole = s()
            assert whole["total"] == 3 and whole["by_curator"] == {"curator01": 1, "curator02": 2}, whole
            assert s(target="E")["total"] == 2, "a category still narrows"
            assert s(target="A,E")["total"] == 3 and s(target="a-small,e-heavy")["total"] == 3, "several terms union"
            assert s(who="curator01")["total"] == 1, "a curator still narrows"
            assert s(vetted="")["total"] == 3, "an empty vetted= (every ordinary search sends it) is not a filter"
            assert s(target="e-heavy", who="curator01")["total"] == 0
            # Attributes narrow too: the way back to every "excavator" that was a loader.
            (Path(d) / "attributes.yaml").write_text(yaml.safe_dump(
                {"type": ["excavator", "loader"], "axles": ["2", "6"]}))
            at = Path(d) / "holding" / "attrs"
            at.mkdir()
            (at / "x2.txt").with_suffix(".json").write_text('{"0": {"type": "excavator", "axles": "6"}}')
            (at / "x3.json").write_text('{"0": {"type": "loader", "axles": "6"}}')
            assert s(attr="type=excavator")["total"] == 1, "one head, one value"
            assert s(attr="type=excavator,type=loader")["total"] == 2, "same head: either"
            assert s(attr="type=loader,axles=2")["total"] == 0, "two heads: both, on one box"
            assert s(attr="axles=6", target="E")["total"] == 2 and s(attr="axles=6", who="curator01")["total"] == 0
            try:
                s(attr="type=digger")
                raise AssertionError("an unknown attribute value must be refused")
            except app.HTTPException as e:
                assert e.status_code == 400, e.detail
            try:
                s(target="zzz")
                raise AssertionError("an unknown class must still be refused")
            except app.HTTPException as e:
                assert e.status_code == 400, e.detail
            # Release all of one curator's held frames a search matches, as they labelled them.
            (Path(d) / "holding" / "images").mkdir()
            for x in ("x1", "x2", "x3"):
                (Path(d) / "holding" / "images" / f"{x}.jpg").write_bytes(b"jpg")
            r = app.dataset_release({"who": "curator02", "target": "E"}, x_curator_token="tok-jonah")
            assert (r["released"], r["total"]) == (2, 2), r
            assert s()["total"] == 1, "x1 still held"
            assert app.dataset_search(tree="approved", x_curator_token="tok-jonah")["total"] == 2, "x2, x3 moved"
            assert (Path(d) / "approved" / "attrs" / "x3.json").is_file(), "attributes travel with the frame"
            assert sum(1 for l in (Path(d) / "audit.jsonl").read_text().splitlines()
                       if '"review"' in l and '"curator02"' in l) == 2, "each release credits the curator"
            # What a review changed, keyed by the class the curator gave — their list to improve on.
            was = [{"cls": 0, "cx": .5, "cy": .5, "w": .1, "h": .1}, {"cls": 1, "cx": .2, "cy": .2, "w": .1, "h": .1}]
            now = [{"cls": 1, "cx": .5, "cy": .5, "w": .1, "h": .1}, {"cls": 0, "cx": .8, "cy": .8, "w": .1, "h": .1}]
            c = app.corrections(was, {"0": {"type": "car"}}, now, {"0": {"type": "van"}})
            assert sorted((x["class"], x["what"]) for x in c) == [
                ("a-small", "added"), ("a-small", "attr:type"), ("a-small", "class->e-heavy"), ("e-heavy", "removed")], c
            assert app.corrections(was, {}, was, {}) == [], "an untouched frame is no correction"
            assert app.quality("curator02")["corrections"] == {}, "a release as labelled corrects nothing"
        finally:
            app.DATASET, app.REVIEWERS = keep


def claims_survive_restart():
    """A slice held when the process dies is still held when it comes back."""
    import tempfile, json, time
    import app

    keep = app.DATASET
    with tempfile.TemporaryDirectory() as d:
        try:
            app.DATASET = Path(d)
            with app.CLAIMS_LOCK:
                app.CLAIMS.clear()
            assert app.hold(["x1", "x2"], "curator01") == 2
            with app.CLAIMS_LOCK:
                app.CLAIMS.clear()                  # the restart
            app.CLAIMS.update(app.load_claims())
            assert app.purge_claims() == {"x1": "curator01", "x2": "curator01"}, app.CLAIMS
            app.finish("curator01", "x1", "approve", {})
            with app.CLAIMS_LOCK:
                app.CLAIMS.clear()
            app.CLAIMS.update(app.load_claims())
            assert app.purge_claims() == {"x2": "curator01"}, "a decided frame stayed claimed"
            # An expired claim does not come back from the file.
            (Path(d) / "claims.json").write_text(json.dumps({"x9": ["curator02", time.time() - 1]}))
            assert app.load_claims() == {}
        finally:
            with app.CLAIMS_LOCK:
                app.CLAIMS.clear()
            app.DATASET = keep


def site_rename():
    """Naming the box: validated like a camera name, refused under a running recorder,
    and the recorder's own path follows the config."""
    import tempfile
    import app

    keep = (app.CONFIG, app.CONFIG_PATH, app.REC.site)
    with tempfile.TemporaryDirectory() as d:
        try:
            app.CONFIG, app.CONFIG_PATH = dict(app.CONFIG, site="site1"), Path(d) / "config.yaml"
            for bad in ("", "a b", "x" * 33, "katuba/north"):
                try:
                    app.config_site_set({"site": bad})
                    raise AssertionError(f"accepted {bad!r}")
                except app.HTTPException as e:
                    assert e.status_code == 400, e.detail
            assert app.config_site_set({"site": "katuba"}) == {"ok": True, "site": "katuba"}
            assert app.REC.site == "katuba" and app.config_site()["site"] == "katuba"
            assert "site: katuba" in app.CONFIG_PATH.read_text()
            app.REC.st["fake"] = {"desired": True}
            try:
                app.config_site_set({"site": "elsewhere"})
                raise AssertionError("renamed under a running recorder")
            except app.HTTPException as e:
                assert e.status_code == 400 and "recording" in e.detail
            finally:
                del app.REC.st["fake"]
        finally:
            app.CONFIG, app.CONFIG_PATH, app.REC.site = keep


def label_strip_rules():
    """The Label tab's attribute rules, run in node against the real yaml and the real
    functions cut out of index.html: one tap on z-emergency fills everything, a suggested
    value yields to the operator's own tap, and every class's defaults obey its constraints."""
    import shutil, subprocess, json
    import app
    cfg = yaml.safe_load((app.DATASET / "attributes.yaml").read_text()) or {}
    vocab = {k: v for k, v in cfg.items() if isinstance(v, list)}
    cons, defs = cfg.get("constraints") or {}, cfg.get("defaults") or {}
    optional = cfg.get("optional") or {}
    for cls, dd in defs.items():
        for head, v in dd.items():
            allowed = (cons.get(cls) or {}).get(head, vocab.get(head, []))
            assert v in allowed, f"{cls}: default {head}={v!r} is not one of {allowed}"
    z = defs.get("z-emergency") or {}
    for head in vocab:
        if not optional.get(head) and (cons.get("z-emergency") or {}).get(head, vocab[head]):
            assert head in z, f"z-emergency: no default for {head}, so it is not one tap"
    if not shutil.which("node"):
        print("  (node not installed: JS rules not run)")
        return
    src = (ROOT / "static" / "index.html").read_text()

    def fn(name):
        i = src.index(f"function {name}(")
        return src[i:src.index("\n}\n", i) + 3]
    js = "const labVal=false, labClasses=%s, labAttrs=%s, labConstraints=%s, labDefaults=%s, labImplies=%s, labRestricts=%s, labOptional=%s;\n" % (
        json.dumps([l.strip() for l in (app.DATASET / "classes.txt").read_text().splitlines() if l.strip()]),
        json.dumps(vocab), json.dumps(cons), json.dumps(defs), json.dumps(cfg.get("implies") or {}),
        json.dumps(cfg.get("restricts") or {}), json.dumps(optional))
    js += src[src.index("const labLocked"):src.index("\n", src.index("const labLocked"))] + "\n"
    js += "\n".join(fn(n) for n in ("labVocab", "labFill", "labDefault", "labGap", "labPayload"))
    js += r"""
const cls = n => labClasses.indexOf(n), Z = cls('z-emergency'), E = cls('e-heavy');
const box = (c, attrs = {}, touched = [], suggested = []) =>
  ({cls: c, attrs, touched: new Set(touched), suggested: new Set(suggested), del: false});
const norm = x => x && typeof x === 'object' && !Array.isArray(x)
  ? Object.fromEntries(Object.entries(x).sort()) : x;     // key order is not a difference
const eq = (got, want, msg) => { const a = JSON.stringify(norm(got)), b = JSON.stringify(norm(want));
  if (a !== b) { console.log('FAIL ' + msg + ': got ' + a + ' wanted ' + b); process.exit(1); } };
const Zdef = {type: 'ambulance', axles: '2', trailers: '0', cargo: 'none', 'axle-config': '1+1'};

let b = box(E); b.cls = Z; labDefault(b);
eq(b.attrs, Zdef, 'fresh box, one tap'); eq(labGap([b]), null, 'nothing left to ask');

b = box(E, {type: 'articulated', cargo: 'general', 'axle-config': '1+2+3', axles: '6', trailers: '1'},
        ['axles'], ['type', 'cargo', 'axle-config']);
b.cls = Z; labDefault(b);
eq(b.attrs, Zdef, 'forbidden values drop whoever set them; a value nobody chose re-defaults');
eq([...b.suggested], [], 'dropped suggestions are forgotten');

b = box(E, {cargo: 'none', type: 'tanker'}, [], ['cargo', 'type']); b.cls = Z; labDefault(b);
eq(b.attrs, Zdef, 'a suggestion the new class allows stays, one it forbids goes');
eq([...b.suggested], ['cargo'], 'and is still marked as the model\'s');

b = box(E, {axles: '3'}, ['axles']); b.cls = Z; labDefault(b);
eq(b.attrs, {...Zdef, axles: '3', 'axle-config': '1+2'}, 'a counted 3 axles survives and drives 1+2');

b = box(Z, {...Zdef, 'axle-config': '1+2'}, [], ['axle-config']);
b.attrs.axles = '3'; b.touched.add('axles'); labFill(b, labImplies.axles['3'], true);
eq(b.attrs['axle-config'], '1+2', 'axles 3 -> 1+2');
b.attrs.axles = '2'; labFill(b, labImplies.axles['2'], true);
eq(b.attrs['axle-config'], '1+1', 'axles 2 -> 1+1 even over a suggested config');
b = box(Z, {...Zdef, 'axle-config': '1+2'}, ['axle-config']);
labFill(b, labImplies.axles['2'], true);
eq(b.attrs['axle-config'], '1+2', "the operator's own config is never overridden by implies");
// Three-axle medium truck towing a two-axle container trailer, in either edit order.
for (const order of [['trailers', 'axles'], ['axles', 'trailers']]) {
  b = box(cls('d-medium')); labDefault(b);
  const wanted = {trailers: '1', axles: '5', cargo: 'container'};
  for (const head of [...order, 'cargo']) {
    const value = wanted[head];
    eq(labVocab(head, 'd-medium', null, b.attrs).some(([v, stale]) => v === value && !stale),
       true, `medium truck offers ${head}=${value}`);
    b.attrs[head] = value; b.touched.add(head);
    labFill(b, (labImplies[head] || {})[value], true);
  }
  eq(b.attrs, {type: 'rigid-truck', ...wanted, 'axle-config': '1+2+2'},
     'medium truck and trailer retain five total axles');
  eq(labVocab('axle-config', 'd-medium', null, b.attrs), [['1+2+2', false]],
     'five-axle configuration is selectable');
  eq(labGap([b]), null, 'medium truck and trailer can be submitted');
}
{
// Future heads are fixtures in memory; the live dataset stays untouched.
Object.assign(labAttrs, {colour: ['other', 'unknown'], make: ['other', 'unknown']});
Object.assign(labOptional, {colour: true, make: true});
let b = box(Z, {...Zdef});
eq(labGap([b]), null, 'missing only optional heads stays approvable');
delete b.attrs.type;
eq(labGap([b]), {i: 0, head: 'type', name: 'z-emergency'}, 'missing required head still blocks');
b.attrs.type = Zdef.type;
labOptional.colour = false;
eq(labGap([b]), {i: 0, head: 'colour', name: 'z-emergency'}, 'false optional flag still requires head');
delete labOptional.colour;
eq(labGap([b]), {i: 0, head: 'colour', name: 'z-emergency'}, 'absent optional flag still requires head');
labOptional.colour = true;
b = box(Z, {colour: 'other', make: 'unknown', type: 'ambulance'}, [], ['colour', 'make', 'type']);
eq(labPayload([b]).attrs, {0: {type: 'ambulance'}},
   'untouched optional suggestions are omitted; untouched required suggestions are sent');
eq(b.attrs, {colour: 'other', make: 'unknown', type: 'ambulance'}, 'payload leaves displayed suggestions intact');
b.touched.add('colour'); b.suggested.delete('colour');
eq(labPayload([b]).attrs, {0: {colour: 'other', type: 'ambulance'}}, 'touched optional suggestion is sent');
}

console.log('  js rules ok');
"""
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    print(r.stdout.strip())


def assign_targets():
    """A target names one class, a category, several of either, or all of them."""
    import tempfile
    import app

    keep = (app.DATASET, app.REVIEWERS, app.push_file)
    with tempfile.TemporaryDirectory() as d:
        try:
            app.DATASET, app.REVIEWERS, app.push_file = Path(d), ["jonah"], lambda n: True
            app.write_yaml("curators.yaml", {"jonah": "tok-jonah", "curator01": "tok-1"})
            (Path(d) / "classes.txt").write_text("a-small\na-motorcycle\nc-bus\ne-heavy\ne-plant\nz-emergency\n")
            tc = app.target_classes
            assert tc("A") == ["a-small", "a-motorcycle"]
            assert tc("A,C") == ["a-small", "a-motorcycle", "c-bus"]
            assert tc("e-heavy, a-small") == ["a-small", "e-heavy"], "classes.txt order, not typed order"
            assert tc("all") == ["a-small", "a-motorcycle", "c-bus", "e-heavy", "e-plant", "z-emergency"]
            assert tc("zzz") == [] and tc("") == []
            assert tc("A,zzz") == ["a-small", "a-motorcycle"], "an unknown term covers nothing"
            ok = app.assign_edit({"handle": "curator01", "class": "a,c,e-plant", "min": 50}, "tok-jonah")
            assert ok["assignments"]["curator01"]["class"] == "A,C,e-plant", ok["assignments"]
            assert ok["assignments"]["curator01"]["classes"] == ["a-small", "a-motorcycle", "c-bus", "e-plant"]
            assert app.assign_edit({"handle": "curator01", "class": "ALL"}, "tok-jonah")["assignments"]["curator01"]["class"] == "all"
            for bad in ("A,zzz", "", "zzz"):
                try:
                    app.assign_edit({"handle": "curator01", "class": bad}, "tok-jonah")
                    raise AssertionError(f"accepted {bad!r}")
                except app.HTTPException as e:
                    assert e.status_code == 400, e.detail
        finally:
            app.DATASET, app.REVIEWERS, app.push_file = keep


def training_split_honours_reference():
    """Frames in reference.txt never enter a training split and always form the benchmark tree."""
    import tempfile
    import train

    keep = (train.DATASET, train.APPROVED, train.RUN, train.REF_RUN, train.REFERENCE, train.MIN_FRAMES)
    with tempfile.TemporaryDirectory() as d:
        try:
            D = Path(d)
            train.DATASET, train.APPROVED, train.RUN = D, D / "approved", D / "run"
            train.REF_RUN, train.REFERENCE, train.MIN_FRAMES = D / "reference-run", D / "reference.txt", 2
            for sub in ("images", "labels"):
                (D / "approved" / sub).mkdir(parents=True)
            stems = [f"f{i:03d}" for i in range(30)]
            for s in stems:
                (D / "approved" / "images" / f"{s}.jpg").write_bytes(b"x")
                (D / "approved" / "labels" / f"{s}.txt").write_text("0 0.5 0.5 0.1 0.1\n")
            split = train.build(["a-small"])
            assert len(split["train"]) + len(split["val"]) == 30, "no reference: every frame trains"
            assert not (D / "reference-run").exists()
            (D / "reference.txt").write_text("\n".join(stems[:20]) + "\n")
            split = train.build(["a-small"])
            assert set(split["train"]) | set(split["val"]) == set(stems[20:]), "frozen frames leaked into the split"
            held = {p.stem for p in (D / "reference-run" / "images" / "val").glob("*.jpg")}
            assert held == set(stems[:20]) and (D / "reference-run" / "data.yaml").is_file()
            (D / "reference.txt").write_text("\n".join(stems) + "\n")
            try:
                train.build(["a-small"])
                raise AssertionError("built a split with nothing outside the reference set")
            except SystemExit as e:
                assert "reference" in str(e), e
            # baseline: the reference set itself and nothing else, whatever else is approved
            (D / "reference.txt").write_text("\n".join(stems[:20]) + "\n")
            train.BASELINE = True
            split = train.build(["a-small"])
            assert set(split["train"]) | set(split["val"]) == set(stems[:20]), "baseline is not the reference set"
            assert (D / "reference-run" / "data.yaml").is_file(), "the reference tree is still scored"
        finally:
            train.BASELINE = False
            (train.DATASET, train.APPROVED, train.RUN, train.REF_RUN, train.REFERENCE, train.MIN_FRAMES) = keep


def queue_orders_by_capture_time():
    """Newest capture first, from the stem — not from file mtime, which any label rewrite scrambles."""
    import app
    assert app.captured_at("cam3-20260821-071527") == "20260821071527"
    assert app.captured_at("RDA-TG-KTB-cam3-20260828-004512") == "20260828004512"
    older, newer = app.captured_at("cam3-20260819-172820"), app.captured_at("loop-videos-20260827-223050")
    assert newer > older, "a freshly sampled frame must sort ahead of an old one"
    assert app.captured_at("no-timestamp-here") == "0", "no name to go on, no path: last"
    import tempfile, os
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "external-abc123def456.txt"; f.write_text("")
        os.utime(f, (1787900000, 1787900000))
        from datetime import datetime, timezone
        want = datetime.fromtimestamp(1787900000, tz=timezone.utc).strftime("%Y%m%d%H%M%S")
        assert app.captured_at(f.stem, f) == want, "arrival time in the same shape"
        assert app.captured_at(f.stem, f) > app.captured_at("cam3-20260821-091402"), "a web image arriving today ranks as latest"


def approved_counts_split_new_from_frozen():
    """The counter tells the whole set apart from what the next run trains on, per class."""
    import tempfile
    import app
    keep = app.DATASET
    with tempfile.TemporaryDirectory() as d:
        try:
            app.DATASET = Path(d)
            (Path(d) / "classes.txt").write_text("a-small\ne-heavy\n")
            lab = Path(d) / "approved" / "labels"
            lab.mkdir(parents=True)
            (lab / "old1.txt").write_text("0 .5 .5 .1 .1\n1 .5 .5 .1 .1\n")
            (lab / "old2.txt").write_text("1 .5 .5 .1 .1\n")
            (lab / "new1.txt").write_text("1 .5 .5 .1 .1\n1 .4 .4 .1 .1\n")
            (Path(d) / "reference.txt").write_text("old1\nold2\n")
            c = app.approved_counts()
            assert (c["frames"], c["frozen"]) == (3, 2), c
            assert c["boxes"] == {"a-small": 1, "e-heavy": 4}, c
            assert c["new_boxes"] == {"e-heavy": 2}, "only the frames outside the reference set"
            # A crowned run's trained.txt re-baselines "new": those frames are in the model.
            (Path(d) / "trained.txt").write_text("new1\n")
            c = app.approved_counts()
            assert (c["trained"], c["frames"], c["frozen"]) == (1, 3, 2), c
            assert c["new_boxes"] == {}, "a trained frame is no longer new"
        finally:
            app.DATASET = keep


def queue_focus_filters_and_suggests():
    """A focus narrows the queue to frames pre-labelled with those classes, and the
    suggestions name the under-represented classes the queue can still supply."""
    import tempfile
    import app
    keep = (app.DATASET, app.REVIEWERS)
    with tempfile.TemporaryDirectory() as d:
        try:
            app.DATASET, app.REVIEWERS = Path(d), []
            D = Path(d)
            (D / "classes.txt").write_text("a-small\nc-bus\ne-heavy\nf-abnormal\n")
            for sub in ("pending/labels", "approved/labels"):
                (D / sub).mkdir(parents=True)
            (D / "pending" / "labels" / "p1-20260828-010000.txt").write_text("0 .5 .5 .1 .1\n")
            (D / "pending" / "labels" / "p2-20260828-010010.txt").write_text("1 .5 .5 .1 .1\n0 .4 .4 .1 .1\n")
            (D / "pending" / "labels" / "p3-20260828-010020.txt").write_text("3 .5 .5 .2 .2\n")
            (D / "approved" / "labels" / "a1.txt").write_text("0 .5 .5 .1 .1\n0 .5 .5 .1 .1\n")
            (D / "approved" / "labels" / "a2.txt").write_text("1 .5 .5 .1 .1\n")
            (D / "reference.txt").write_text("")
            r = app.dataset_samples(who="", focus="", x_curator_token="")
            assert r["pending_total"] == 3 and r["focus"] == ""
            assert r["candidates"] == {"a-small": 2, "c-bus": 1, "f-abnormal": 1}, r["candidates"]
            assert [s["class"] for s in r["focus_suggestions"]] == ["f-abnormal", "c-bus", "a-small"], r["focus_suggestions"]
            first = [s["id"] for s in r["pending"]]
            r2 = app.dataset_samples(who="", focus="", offset=1, x_curator_token="")
            assert [s["id"] for s in r2["pending"]] == first[1:] and r2["pending_total"] == 3, "offset skips, total stays"
            assert app.dataset_samples(offset=99, x_curator_token="")["pending"] == [], "past the end is empty"
            r = app.dataset_samples(who="", focus="c-bus,F", x_curator_token="")
            assert sorted(s["id"] for s in r["pending"]) == ["p2-20260828-010010", "p3-20260828-010020"], r["pending"]
            assert r["pending_total"] == 2 and r["focus"] == "c-bus,F"
            assert app.dataset_samples(who="", focus="zzz", x_curator_token="")["pending_total"] == 3, "unknown focus filters nothing"
            # a web image fetched for a class is a candidate for it even with no box yet
            (D / "pending" / "labels" / "external-abc123def456.txt").write_text("")
            (D / "external.json").write_text('{"external-abc123def456": "f-abnormal"}')
            r = app.dataset_samples(who="", focus="F", x_curator_token="")
            assert [s["id"] for s in r["pending"]][-1] == "external-abc123def456" or any(
                s["id"] == "external-abc123def456" for s in r["pending"]), r["pending"]
            assert r["candidates"]["f-abnormal"] == 2 and next(s for s in r["pending"] if s["id"].startswith("external"))["external"] == "f-abnormal"
        finally:
            app.DATASET, app.REVIEWERS = keep


def classify_suggests_class_and_attrs():
    """The curator draws a box, the crowned models say what is in it. Stubbed detection:
    what is under test is the plumbing — the vocab filter, the class-name lookup, and
    every way this can decline."""
    import tempfile
    import app

    keep = (app.DATASET, app.CONFIG, app.detect.classify_box)
    tokens = dict(app.GOLD_TOKENS)
    with tempfile.TemporaryDirectory() as d:
        try:
            app.DATASET, app.CONFIG = Path(d), {}
            app.write_yaml("curators.yaml", {"jonah": "tok-jonah"})
            (Path(d) / "classes.txt").write_text("a-small\ne-heavy\n")
            (Path(d) / "attributes.yaml").write_text(yaml.safe_dump(
                {"type": ["car", "rigid-truck"], "axles": ["2", "6"]}))
            for sub, name, data in (("images", "x1.jpg", b"jpg"),
                                    ("labels", "x1.txt", b"0 0.5 0.5 0.1 0.1\n")):
                (Path(d) / "pending" / sub).mkdir(parents=True, exist_ok=True)
                (Path(d) / "pending" / sub / name).write_bytes(data)
            champ = Path(d) / "champion.pt"
            champ.write_bytes(b"weights")
            body = {"who": "jonah", "id": "x1",
                    "box": {"cx": 0.5, "cy": 0.5, "w": 0.2, "h": 0.2}}
            call = lambda **kw: app.dataset_classify({**body, **kw}, x_curator_token="tok-jonah")

            app.detect.classify_box = lambda jpeg, cfg, box: (
                {"cls": "e-heavy", "attrs": {"type": "rigid-truck", "axles": "6",
                                             "bogus": "x", "type2": "y"}}, None)
            r = call()
            assert r == {"ok": True, "cls": 1, "name": "e-heavy",
                         "attrs": {"type": "rigid-truck", "axles": "6"}}, r

            # A stock COCO name the operator's class list has never heard of: named, unmapped.
            app.detect.classify_box = lambda jpeg, cfg, box: ({"cls": "truck", "attrs": {}}, None)
            assert call() == {"ok": True, "cls": None, "name": "truck", "attrs": {}}

            def fails(what, **kw):
                try:
                    call(**kw)
                except app.HTTPException as e:
                    return e.status_code
                raise AssertionError(f"{what} must be refused")

            # A degenerate box is legal in a label file — parse_boxes takes it — but there
            # is nothing to crop, so classify alone refuses it.
            assert app.parse_boxes({"boxes": [{"cls": 0, "cx": 0.5, "cy": 0.5,
                                               "w": 0, "h": 0.1}]})[0]["w"] == 0

            # The frame is found wherever it lives now, whatever tree the caller names.
            (Path(d) / "approved" / "images").mkdir(parents=True)
            (Path(d) / "approved" / "images" / "x2.jpg").write_bytes(b"jpg")
            assert app.sample_image("x1").name == "x1.jpg", "pending is searched"
            assert app.sample_image("x2", "pending").parent.parent.name == "approved", \
                "a stale tree falls through to the others"
            assert app.sample_image("nosuch") is None
            gold = Path(d) / "gold" / "g1"
            gold.mkdir(parents=True)
            (gold / "perturbed.txt").write_text("0 0.5 0.5 0.1 0.1\n")
            (gold / "image.jpg").write_bytes(b"jpg")
            app.GOLD_TOKENS["g-abcd1234"] = "g1"
            assert app.sample_image("g-abcd1234") == gold / "image.jpg", "gold has its own dir"

            app.detect.classify_box = lambda jpeg, cfg, box: (None, "boom")
            assert fails("a detector that errors") == 503
            app.detect.classify_box = lambda jpeg, cfg, box: ({"cls": "e-heavy", "attrs": {}}, None)
            assert fails("a zero-width box", box={"cx": 0.5, "cy": 0.5, "w": 0, "h": 0.2}) == 400
            assert fails("a box outside the frame",
                         box={"cx": 1.5, "cy": 0.5, "w": 0.2, "h": 0.2}) == 400
            assert fails("a box missing a side", box={"cx": 0.5, "cy": 0.5}) == 400
            assert fails("an unknown sample", id="nosuch") == 404
            champ.unlink()
            assert fails("a node with no champion") == 503, "no weights is 503, not a crash"
            champ.write_bytes(b"weights")
            try:
                app.dataset_classify(body, x_curator_token="tok-wrong")
                raise AssertionError("a bad token must be refused")
            except app.HTTPException as e:
                assert e.status_code == 401, e.detail
        finally:
            app.DATASET, app.CONFIG, app.detect.classify_box = keep
            app.GOLD_TOKENS.clear()
            app.GOLD_TOKENS.update(tokens)


def validation_attrs_queue():
    import json, tempfile
    from unittest.mock import patch
    import app
    from train import is_val

    with tempfile.TemporaryDirectory() as d, patch.object(app, "DATASET", Path(d)), \
            patch.object(app, "reference", return_value=set()), \
            patch.object(app, "is_active", side_effect=lambda sid: sid != expired):
        root = Path(d)
        ids = [f"v{i}" for i in range(1000) if is_val(f"v{i}")][:7]
        eligible, second, frozen, archived, expired, complete, exempt = ids
        train_sid = next(f"t{i}" for i in range(100) if not is_val(f"t{i}"))
        external = next(f"external-{i}" for i in range(100) if is_val(f"external-{i}"))
        app.write_yaml("curators.yaml", {"alice": "a", "bob": "b", "charlie": "c"})
        (root / "classes.txt").write_text("a-small\nw-wheel\n")
        app.write_yaml("attributes.yaml", {"colour": ["white", "black"], "make": ["toyota", "other"],
            "type": ["car", "van"], "optional": {"colour": True, "make": True},
            "constraints": {"w-wheel": {"colour": [], "make": []}}})
        labels = root / "approved" / "labels"
        labels.mkdir(parents=True)
        for sid in ids + [train_sid, external]:
            (labels / f"{sid}.txt").write_text("0 0.5 0.5 0.1 0.1\n")
        (labels / f"{eligible}.txt").write_text("0 0.5 0.5 0.1 0.1\n1 0.2 0.2 0.1 0.1\n")
        (labels / f"{exempt}.txt").write_text("1 0.5 0.5 0.1 0.1\n")
        (root / "reference.txt").write_text(frozen + "\n")
        archive = root / "classifier-crops" / archived
        archive.mkdir(parents=True)
        (archive / "manifest.json").write_text('{"reference": true}')
        ap = app.attrs_path(eligible, "approved")
        ap.parent.mkdir()
        ap.write_text(json.dumps({"0": {"type": "car"}, "1": {"type": "van"}}))
        app.attrs_path(complete, "approved").write_text(json.dumps({"0": {"colour": "white", "make": "toyota"}}))
        ai = root / "ai-attrs"
        ai.mkdir()
        row = {"sid": eligible, "box": 0, "bbox": [.5, .5, .1, .1], "attrs": {"colour": "white", "make": "toyota"}}
        (ai / "a.jsonl").write_text(json.dumps(row) + "\n")
        (ai / "z.jsonl").write_text(json.dumps({**row, "attrs": {"colour": "black", "make": "other"}}) + "\n" +
            json.dumps({**row, "sid": second, "bbox": [.9, .5, .1, .1]}) + "\n")
        claims = dict(app.CLAIMS)
        app.CLAIMS.clear()
        try:
            get = lambda who="alice", token="a", **kw: app.dataset_valcheck(who=who, x_curator_token=token, **kw)
            assert get(count=True)["remaining"] == 2
            assert not app.CLAIMS, "counting must not claim a frame"
            r = get()
            assert r["id"] == eligible and r["remaining"] == 2, r
            assert r["suggested"] == {"0": {"colour": "black", "make": "other"}}, r
            assert r["attr_optional"] == {"colour": False, "make": False}
            assert r["attr_ordinary_optional"] == {"colour": True, "make": True}, "direct entry must carry ordinary rules"
            assert get()["id"] == eligible, "reload keeps own claim"
            other = get("bob", "b")
            assert other["id"] == second and other["suggested"] == {}, other
            assert get("charlie", "c") == {"done": False, "remaining": 2}, "claimed frames are not a finished pool"
            # An existing human value never becomes an AI suggestion.
            suggestion_sample = {**r, "attrs": {"0": {"colour": "white"}}}
            assert app.valcheck_suggestions(suggestion_sample, r["classes"], r["attributes"], r["attr_constraints"]) == {"0": {"make": "other"}}
            original = (labels / f"{eligible}.txt").read_bytes()
            body = {"who": "alice", "id": eligible, "rev": r["rev"], "action": "valattrs", "boxes": [],
                    "attrs": {"0": {"colour": "white", "make": "toyota", "type": "van"}}}
            def reject(body, status=400, token="a"):
                try:
                    app.dataset_label(body, x_curator_token=token)
                except app.HTTPException as e:
                    assert e.status_code == status, e.detail
                else:
                    raise AssertionError("invalid valattrs accepted")
            reject(body, 401, "wrong")
            for sid in [frozen, archived, expired, complete, exempt, train_sid, external, "missing"]:
                reject({**body, "id": sid})
            for attrs in [{"0": {"colour": "pink", "make": "toyota"}}, {"2": {"colour": "white"}},
                          {"1": {"colour": "white"}}, {}, {"0": {"colour": "white"}}]:
                reject({**body, "attrs": attrs})
            reject({**body, "who": "bob"}, 409, "b")
            assert app.dataset_label(body, x_curator_token="a")["ok"]
            assert json.loads(ap.read_text()) == {"0": {"type": "car", "colour": "white", "make": "toyota"}, "1": {"type": "van"}}
            assert (labels / f"{eligible}.txt").read_bytes() == original
            assert not (root / "scores.jsonl").exists()
            audit = json.loads((root / "audit.jsonl").read_text().splitlines()[-1])
            assert audit["action"] == "review" and audit["valcheck"] is True
            assert audit["who"] == "alice", "validation audit must record the token-bound handle"
            from dataset_sync import consumed
            assert eligible in consumed(root)
            app.dataset_label({**body, "id": second, "rev": other["rev"], "who": "bob"}, x_curator_token="b")
            assert get() == {"done": True, "remaining": 0}
        finally:
            app.CLAIMS.clear()
            app.CLAIMS.update(claims)


def valcheck_rejects_changed_boxes():
    import hashlib, json, tempfile
    from unittest.mock import patch
    import app
    from train import is_val

    with tempfile.TemporaryDirectory() as d, patch.object(app, "DATASET", Path(d)), \
            patch.object(app, "reference", return_value=set()), patch.object(app, "is_active", return_value=True), \
            patch.object(app, "REVIEWERS", {"reviewer"}):
        root = Path(d)
        sid = next(f"v{i}" for i in range(100) if is_val(f"v{i}"))
        app.write_yaml("curators.yaml", {"alice": "a", "reviewer": "r"})
        (root / "classes.txt").write_text("a-small\n")
        app.write_yaml("attributes.yaml", {"colour": ["white", "black"], "make": ["toyota", "other"]})
        label = app.sample_paths(sid, "approved")[1]
        label.parent.mkdir(parents=True)
        label.write_text("0 0.25 0.5 0.1 0.1\n0 0.75 0.5 0.1 0.1\n")
        claims = dict(app.CLAIMS)
        app.CLAIMS.clear()
        try:
            frame = app.dataset_valcheck(who="alice", x_curator_token="a")
            expected_rev = hashlib.sha1(label.read_bytes()).hexdigest()[:12]
            body = {"id": sid, "who": "alice", "action": "valattrs", "rev": expected_rev,
                    "attrs": {"0": {"colour": "white", "make": "toyota"},
                              "1": {"colour": "black", "make": "other"}}}
            app.dataset_label({"id": sid, "who": "reviewer", "action": "review",
                               "boxes": frame["boxes"][::-1], "attrs": {}}, x_curator_token="r")
            before = label.read_bytes()
            before_attrs = app.attrs_path(sid, "approved").read_bytes()
            assert before_attrs == b"{}", "review must publish the empty attrs sidecar"
            audit = (root / "audit.jsonl").read_bytes()
            def reject(post):
                try:
                    app.dataset_label(post, x_curator_token="a")
                except app.HTTPException as e:
                    assert (e.status_code, e.detail) == (409, "frame changed, reload"), e.detail
                else:
                    raise AssertionError("valattrs accepted stale box indexes")
                assert label.read_bytes() == before
                assert app.attrs_path(sid, "approved").read_bytes() == before_attrs
                assert (root / "audit.jsonl").read_bytes() == audit
            reject(body)
            assert frame["rev"] == expected_rev
            fresh = app.dataset_valcheck(who="alice", x_curator_token="a")
            assert fresh["rev"] == hashlib.sha1(before).hexdigest()[:12] != expected_rev
            reject({k: v for k, v in body.items() if k != "rev"})
            assert app.dataset_label({**body, "rev": fresh["rev"]}, x_curator_token="a")["ok"]
            assert json.loads(app.attrs_path(sid, "approved").read_text()) == body["attrs"]
        finally:
            app.CLAIMS.clear()
            app.CLAIMS.update(claims)


def valcheck_browser_rules():
    src = (ROOT / "static" / "index.html").read_text()
    def fn(name):
        i = src.index(f"function {name}(")
        return src[i:src.index("\n}\n", i) + 3]
    js = """const labClasses=['a-small', 'w-wheel'], labAttrs={colour:['white','black'],make:['toyota'],type:['car']},
      labConstraints={'w-wheel':{colour:[],make:[]}}, labOptional={colour:true,make:true},labRestricts={},labImplies={};
      let labVal=true;
      const b={cls:0, cx:.5, cy:.5,w:.1,h:.1,attrs:{colour:'white',make:'toyota',type:'car'},touched:new Set(['type']),suggested:new Set(['colour','make'])};
      const assert=(x,msg)=>{if(!x)throw Error(msg)};
    """ + "\n".join(fn(n) for n in ("labVocab", "labGap", "labPayload")) + """
      assert(Object.keys(labPayload([b]).attrs).length===0,'untouched AI must not be human');
      assert(labGap([b]).head==='colour','suggestion does not answer');
      b.touched.add('colour');
      assert(labGap([b]).head==='make','each head needs a tap');
      assert(Object.keys(labPayload([b]).attrs).length===0,'optional suggestion is stripped in valcheck too');
      b.suggested.delete('colour');
      assert(JSON.stringify(labPayload([b]).attrs)==='{"0":{"colour":"white"}}','only touched colour/make');
      b.touched.add('make');b.suggested.delete('make');assert(labGap([b])===null,'both tapped');
      assert(labGap([{...b,cls:1,touched:new Set()}])===null,'class exclusions');
      labVal=false;assert(labPayload([b]).attrs[0].type==='car','ordinary payload unchanged');
    """
    handler_start = src.index("document.getElementById('l-attrs').onclick =")
    handler = src[handler_start:src.index("\n};", handler_start) + 3]
    js += """
      labVal=true;b.touched=new Set();let labSel=0;const labBoxes=[b];
      const chip={};const document={getElementById:()=>chip};const labSelect=()=>{};
    """ + handler + """
      const tap=(head,val)=>chip.onclick({target:{closest:()=>({dataset:{head,val}})}});
      const human={...b,attrs:{colour:'white',make:'toyota'},touched:new Set(),suggested:new Set()};
      labBoxes[0]=human;
      assert(Object.keys(labPayload([human]).attrs).length===0,'untouched human values must not be submitted');
      tap('colour','white');
      assert(JSON.stringify(labPayload([human]).attrs)==='{"0":{"colour":"white"}}','tapped human colour is included');
      tap('make','toyota');
      assert(JSON.stringify(labPayload([human]).attrs)==='{"0":{"colour":"white","make":"toyota"}}','tapped human values are included');
      labBoxes[0]=b;
      tap('colour','white');
      assert(b.touched.has('colour') && b.attrs.colour==='white','tap confirms the lit suggestion');
      tap('colour','black');
      assert(b.touched.has('colour') && b.attrs.colour==='black','tap corrects suggestion');
      tap('make','toyota');assert(labGap([b])===null,'all heads actively answered');
      b.attrs={};b.touched=new Set();b.suggested=new Set();
      assert(labGap([b]).head==='colour','blank box needs colour');
      tap('colour','white');tap('make','toyota');
      assert(labGap([b])===null,'blank box can be answered from scratch');
      assert(JSON.stringify(labPayload([b]).attrs)==='{"0":{"colour":"white","make":"toyota"}}','blank box sends both human answers');
    """
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def same_size_attrs_sync():
    import hashlib, tempfile
    import dataset_sync as sync
    class S3:
        def __init__(self): self.objects = {}
        def get_paginator(self, op): return self
        def paginate(self, **kw):
            yield {"Contents": [{"Key": k, "Size": len(v), "ETag": '"' + hashlib.md5(v).hexdigest() + '"'} for k,v in self.objects.items()]}
        def upload_file(self, path, bucket, key): self.objects[key] = Path(path).read_bytes()
        def download_file(self, bucket, key, path): Path(path).write_bytes(self.objects[key])
    with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
        root, dest, cl = Path(a), Path(b), S3()
        p = root / "approved" / "attrs" / "v.json"
        p.parent.mkdir(parents=True)
        p.write_text('{"0":{"colour":"white"}}')
        assert sync.push(cl, "b", "c/", root, ["approved/"]) == (1,0)
        assert sync.pull(cl, "b", "c/", dest) == (1,0)
        p.write_text('{"0":{"colour":"black"}}')
        assert sync.push(cl, "b", "c/", root, ["approved/"]) == (1,0), "same-size human correction must upload"
        assert sync.pull(cl, "b", "c/", dest) == (1,0), "same-size human correction must download"
        assert (dest / "approved" / "attrs" / "v.json").read_bytes() == p.read_bytes()
        assert sync.push(cl, "b", "c/", root, ["approved/"]) == (0,1)
        assert sync.pull(cl, "b", "c/", dest) == (0,1)



def approved_pair_and_empty_attrs_sync():
    import hashlib, json, tempfile
    from unittest.mock import patch
    from PIL import Image
    import app, dataset_sync as sync, train_attrs
    class S3:
        def __init__(self): self.objects = {}; self.on_download = lambda key: None
        def get_paginator(self, op): return self
        def paginate(self, **kw):
            yield {"Contents": [{"Key": k, "Size": len(v), "ETag": '"' + hashlib.md5(v).hexdigest() + '"'} for k,v in self.objects.items()]}
        def upload_file(self, path, bucket, key): self.objects[key] = Path(path).read_bytes()
        def download_file(self, bucket, key, path):
            Path(path).write_bytes(self.objects[key]); self.on_download(key)
    with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
        root, dest, cl = Path(a), Path(b), S3()
        with patch.object(app, "DATASET", root), patch.object(app, "is_active", return_value=True), \
                patch.object(app, "REVIEWERS", {"reviewer"}), patch.object(app, "holds", return_value=False):
            (root / "classes.txt").write_text("a-small\n")
            app.write_yaml("attributes.yaml", {"colour": ["white", "black"]})
            img, label = app.sample_paths("pair", "approved")
            img.parent.mkdir(parents=True); label.parent.mkdir(parents=True)
            Image.new("RGB", (32, 32)).save(img)
            label.write_text(app.box_lines([{"cls": 0, "cx": cx, "cy": .5, "w": .1, "h": .1} for cx in (.25, .75)]))
            attrs = app.attrs_path("pair", "approved")
            attrs.parent.mkdir(parents=True)
            attrs.write_text(json.dumps({"0": {"colour": "white"}, "1": {"colour": "black"}}))
            sync.push(cl, "b", "c/", root, ["approved/"]); sync.pull(cl, "b", "c/", dest)
            boxes = app.read_boxes(label)[::-1]
            reversed_attrs = {"0": {"colour": "black"}, "1": {"colour": "white"}}
            size = label.stat().st_size
            app.review_label("pair", "reviewer", {"boxes": boxes, "attrs": reversed_attrs})
            assert label.stat().st_size == size, "regression needs a same-size reordered label"
            sync.push(cl, "b", "c/", root, ["approved/"]); sync.pull(cl, "b", "c/", dest)
            consumer_label, consumer_attrs = dest / "approved/labels/pair.txt", dest / "approved/attrs/pair.json"
            assert app.read_boxes(consumer_label) == boxes, "consumer kept old box order after paired correction"
            assert app.read_attrs(consumer_attrs) == reversed_attrs
            # Both scheduling and replacement must protect labels on the sole writer.
            old = consumer_label.read_bytes()
            label.write_text("0 0.50 0.5 0.1 0.1\n")
            sync.pull(cl, "b", "c/", root, preserve_attrs=True)
            assert label.read_text() == "0 0.50 0.5 0.1 0.1\n", "curation pull overwrote local labels"
            label.unlink()
            cl.on_download = lambda key: label.write_text("local edit") if key.endswith("pair.txt") else None
            sync.pull(cl, "b", "c/", root, preserve_attrs=True)
            assert label.read_text() == "local edit", "download replaced a concurrently created label"
            cl.on_download = lambda key: None
            label.write_bytes(old)
            app.review_label("pair", "reviewer", {"boxes": boxes, "attrs": {}})
            assert attrs.is_file(), "cleared approved attrs must remain syncable"
            assert attrs.read_text() == "{}", "cleared approved attrs must remain syncable"
            sync.push(cl, "b", "c/", root, ["approved/"]); sync.pull(cl, "b", "c/", dest)
            assert consumer_attrs.read_text() == "{}"
            sync.pull(cl, "b", "c/", root)
            assert app.read_attrs(attrs) == {}, "pull restored cleared attributes"
            with patch.object(train_attrs, "DATASET", root), patch.object(train_attrs, "APPROVED", root / "approved"), \
                    patch.object(train_attrs, "EXTERNAL", False):
                assert train_attrs.build({"colour": ["white", "black"]}) == ([], [], [], []), "empty attrs made human training crops"
            # Ordinary re-approval also publishes the clear, including a stale destination sidecar.
            app.send_back("pair", "approved")
            attrs.write_text(json.dumps(reversed_attrs))
            sync.push(cl, "b", "c/", root, ["approved/"]); sync.pull(cl, "b", "c/", dest)
            assert app.read_attrs(consumer_attrs) == reversed_attrs
            app.dataset_label({"id": "pair", "who": "reviewer", "action": "approve", "boxes": boxes, "attrs": {}}, x_curator_token="")
            assert attrs.read_text() == "{}", "re-approval deleted its empty attrs"
            sync.push(cl, "b", "c/", root, ["approved/"]); sync.pull(cl, "b", "c/", dest)
            assert app.read_attrs(consumer_attrs) == {}


def valcheck_browser_conflict_reload():
    src = (ROOT / "static/index.html").read_text()
    def fn(name):
        i = src.index(f"function {name}(")
        return "async " + src[i:src.index("\n}\n", i) + 3]
    js = """
      const assert=(x,msg)=>{if(!x)throw Error(msg)};
      let labVal=false,labRev=false,labOpened=null,labWho='',labFocus='',labSkipped=0,labLoadSeq=0;
      let labClasses=[],labAttrs={},labConstraints={},labDefaults={},labImplies={},labRestricts={},labOptional={};
      let labQueue=[],labBoxes=[],labTotal=0,labApproved={},labAssign=null,labAuthErr=false,labIdle;
      const labOne=()=>labVal||labRev||!!labOpened;
      const curation=false,location={hash:'#valcheck'},els={};
      const document={getElementById:id=>(els[id] ||= {})};
      const notes=[],sent=[];
      const labWhoShow=()=>{},labFocusFill=()=>{},labFocusSug=()=>{},labStats=()=>{},labChips=()=>{};
      const labNote=n=>notes.push(n),labSelect=()=>{};
      const labShow=()=>{labBoxes=(labQueue[0]?.boxes||[]).map(b=>({...b,touched:new Set()}));els['l-ok'].disabled=!labQueue.length};
      const labGap=bs=>bs.find(b=>!b.touched.has('colour'))?{i:0,name:'a-small',head:'colour'}:null;
      const labPayload=boxes=>({boxes,attrs:{0:{colour:'white'}}});
      const labHdr=()=>({}),labAskTok=()=>false;
      let status=409,detail='frame changed, reload',loads=0,fail=false;
      const fetch=async(url,opts)=>{sent.push(JSON.parse(opts.body));return {status,json:async()=>({detail})}};
      const labFetch=async url=>{
        assert(url.includes('/valcheck'),'conflict must reload validation queue');loads++;
        if(fail)throw Error('offline');
        return {ok:true,status:200,json:async()=>({id:'v',rev:'rev'+loads,remaining:1,boxes:[{cls:0,cx:loads===1?.25:.75}]})};
      };
    """ + "\n".join(fn(n) for n in ("post", "labLoad", "labSend")) + """
      (async()=>{
        await labLoad();labBoxes[0].touched.add('colour');await labSend('valattrs');
        assert(notes.includes('This frame changed — reloading'),'409 was presented as a retry');
        assert(loads===2&&labQueue[0].rev==='rev2'&&labBoxes[0].cx===.75,'fresh revision and boxes were not loaded');
        await labSend('valattrs');assert(sent.length===1,'fresh boxes submitted without confirmation');
        labBoxes[0].touched.add('colour');status=500;await labSend('valattrs');
        assert(loads===2&&notes.at(-1).includes('tap again to retry'),'ordinary failure must keep sample');
        status=409;detail='frame is claimed by another curator';fail=true;await labSend('valattrs');
        assert(loads===3&&els['l-ok'].disabled,'failed conflict reload left stale sample enabled');
        const count=sent.length;await labSend('valattrs');assert(sent.length===count,'retry resent stale revision');
      })().catch(e=>{console.error(e);process.exitCode=1});
    """
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def valcheck_requires_bound_handle():
    import tempfile
    from unittest.mock import patch
    import app
    with tempfile.TemporaryDirectory() as td, patch.object(app, "DATASET", Path(td)), \
            patch.object(app, "curators", return_value={"alice": "a", "bob": "b"}), \
            patch.object(app, "ensure_active"), patch.object(app, "valcheck_reference", return_value=set()), \
            patch.object(app, "valattrs_label", side_effect=lambda sid, who, body: {"who": who}):
        for token in ("a", "b"):
            for call in (lambda: app.dataset_valcheck(who="", count=True, x_curator_token=token),
                         lambda: app.dataset_label({"id": "v1", "action": "valattrs"}, x_curator_token=token)):
                try:
                    call()
                except app.HTTPException as e:
                    assert e.status_code == 401
                else:
                    raise AssertionError("rostered valcheck/valattrs accepted an omitted handle")
        for who, token in (("alice", "b"), ("bob", "a")):
            for call in (lambda: app.dataset_valcheck(who=who, count=True, x_curator_token=token),
                         lambda: app.dataset_label({"id": "v1", "who": who, "action": "valattrs"}, x_curator_token=token)):
                try:
                    call()
                except app.HTTPException as e:
                    assert e.status_code == 401
                else:
                    raise AssertionError("token was not bound to the supplied handle")
        with patch.object(app, "curators", return_value={}):
            app.dataset_valcheck(who="", count=True, x_curator_token="")
            assert app.dataset_label({"id": "v1", "action": "valattrs"}, x_curator_token="") == {"who": ""}


def curation_sync_preserves_attrs():
    import hashlib, tempfile
    from types import SimpleNamespace
    from unittest.mock import patch
    import app, dataset_sync as sync, retention_maintenance, curation_cap
    class S3:
        def __init__(self): self.objects = {}; self.on_download = lambda key: None
        def get_paginator(self, op): return self
        def paginate(self, **kw):
            yield {"Contents": [{"Key": k, "Size": len(v), "ETag": '"' + hashlib.md5(v).hexdigest() + '"'} for k,v in self.objects.items()]}
        def upload_file(self, path, bucket, key): self.objects[key] = Path(path).read_bytes()
        def download_file(self, bucket, key, path):
            Path(path).write_bytes(self.objects[key]); self.on_download(key)
    with tempfile.TemporaryDirectory() as td:
        root, cl = Path(td), S3()
        p = root / "approved" / "attrs" / "v.json"
        p.parent.mkdir(parents=True)
        white, black = b'{"0":{"colour":"white"}}', b'{"0":{"colour":"black"}}'
        p.write_bytes(white)
        missing = p.with_name("missing.json")
        raced = p.with_name("raced.json")
        cl.objects[sync.PREFIX + "approved/attrs/missing.json"] = white
        cl.objects[sync.PREFIX + "approved/attrs/raced.json"] = white
        def pull(client, bucket, **kw):
            assert cl.objects[sync.PREFIX + "approved/attrs/v.json"] == white, "push must publish before local edit"
            p.write_bytes(black)  # curator saves between the push and pull
            return sync.pull(client, bucket, root=root, **kw)
        def arrived(key):
            if key.endswith("raced.json"): raced.write_bytes(black)  # created during download
        cl.on_download = arrived
        ds = SimpleNamespace(LEDGERS=sync.LEDGERS, PREFIX=sync.PREFIX,
            push=lambda client, bucket, **kw: sync.push(client, bucket, root=root, **kw),
            pull=pull, models=lambda *a, **kw: None)
        with patch.object(app, "DATASET", root), patch.object(retention_maintenance, "maintenance_once"), \
                patch.object(app, "curation_cap", curation_cap, create=True), \
                patch.object(curation_cap, "bucket_settings", side_effect=RuntimeError("offline test")), \
                patch.object(app, "model_cfg", return_value={"detect_weights": ""}):
            app.sync_both(ds, cl, "b")
        assert p.read_bytes() == black, "curation pull undid the interleaved same-size correction"
        assert missing.read_bytes() == white, "curation pull must still seed missing attrs"
        assert raced.read_bytes() == black, "replace must recheck attrs created during download"


def valcheck_browser_transition_and_count():
    src = (ROOT / "static" / "index.html").read_text()
    def fn(name):
        i = src.index(f"function {name}(")
        return ("async " if src[i-6:i] == "async " else "") + src[i:src.index("\n}\n", i) + 3]
    js = """
      const assert=(x,msg)=>{if(!x)throw Error(msg)};
      let labVal=false,labRev=false,labOpened=null,labWho='',labFocus='',labSkipped=0,labLoadSeq=0;
      let labClasses=[],labAttrs={},labConstraints={},labDefaults={},labImplies={},labRestricts={},labOptional={};
      let labQueue=[],labTotal=0,labApproved={},labAssign=null,labAuthErr=false,labIdle;
      const labOne=()=>labVal||labRev||!!labOpened;
      const curation=false,location={hash:'#valcheck'},els={};
      const document={getElementById:id=>(els[id] ||= {})};
      const labWhoShow=()=>{},labFocusFill=()=>{},labFocusSug=()=>{},labStats=()=>{},labChips=()=>{},labShow=()=>{},labNote=()=>{};
      const sample={id:'v1',boxes:[{cls:0,attrs:{},touched:new Set()}]};
      const meta={classes:['a-small'],attributes:{colour:['white'],make:['toyota']},attr_optional:{colour:false,make:false},
        attr_ordinary_optional:{colour:true,make:true},attr_defaults:{colour:'white'}};
      const counts=[];
      const reply=d=>({ok:true,status:200,json:async()=>d});
      const labFetch=async url=>{
        if(url.includes('count=true'))return new Promise(resolve=>counts.push(resolve));
        if(url.includes('/valcheck'))return reply({...sample,...meta,remaining:2});
        return reply(sample);
      };
    """ + "\n".join(fn(n) for n in ("labLoad", "labOpenHit", "labVocab", "labGap")) + """
      (async()=>{
        await labLoad();
        assert(labGap(sample.boxes).head==='colour','validation still requires an active answer');
        location.hash='#review';await labLoad();
        assert(labGap(sample.boxes)===null,'direct valcheck to review lost ordinary optional flags');
        assert(labDefaults.colour==='white','ordinary defaults retained');
        labOptional={};location.hash='#valcheck';await labLoad();
        await labOpenHit('v1','approved',false);
        assert(labGap(sample.boxes)===null,'direct valcheck to search lost ordinary optional flags');
        location.hash='';await labLoad();await labLoad();
        const old=counts[counts.length-2],latest=counts[counts.length-1];
        latest(reply({remaining:1}));await new Promise(r=>setImmediate(r));
        old(reply({remaining:9}));await new Promise(r=>setImmediate(r));
        assert(els['l-valcheck'].textContent==='Check colour/make (1 left)','stale background count overwrote newer count');
      })().catch(e=>{console.error(e);process.exitCode=1});
    """
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def valcheck_without_ai_and_unknown():
    import json, tempfile
    from unittest.mock import patch
    import app
    from train import is_val

    with tempfile.TemporaryDirectory() as d, patch.object(app, "DATASET", Path(d)), \
            patch.object(app, "reference", return_value=set()), patch.object(app, "is_active", return_value=True):
        root = Path(d)
        sid = next(f"v{i}" for i in range(100) if is_val(f"v{i}"))
        app.write_yaml("curators.yaml", {"alice": "a"})
        (root / "classes.txt").write_text("a-small\n")
        app.write_yaml("attributes.yaml", {"colour": ["white"], "make": ["toyota", "unknown"]})
        labels = root / "approved" / "labels"
        labels.mkdir(parents=True)
        (labels / f"{sid}.txt").write_text("0 0.5 0.5 0.1 0.1\n")
        claims = dict(app.CLAIMS)
        app.CLAIMS.clear()
        try:
            get = lambda: app.dataset_valcheck(who="alice", x_curator_token="a")
            r = get()
            assert r["id"] == sid and r["remaining"] == 1 and r["suggested"] == {}, r
            assert not (root / "ai-attrs").exists(), "blank frame must be offered without an AI row"
            ai = root / "ai-attrs"
            ai.mkdir()
            (ai / "run.jsonl").write_text(json.dumps({"sid": sid, "box": 0,
                "bbox": [.5, .5, .1, .1], "attrs": {"make": "unknown"}}) + "\n")
            assert get()["suggested"] == {"0": {"make": "unknown"}}, "agreed unknown must prefill"
            (ai / "run.jsonl").unlink()
            assert app.dataset_label({"who": "alice", "id": sid, "rev": r["rev"], "action": "valattrs",
                "attrs": {"0": {"colour": "white", "make": "toyota"}}}, x_curator_token="a")["ok"]
            assert json.loads(app.attrs_path(sid, "approved").read_text()) == {
                "0": {"colour": "white", "make": "toyota"}}, "both heads can be filled from scratch"
            assert get() == {"done": True, "remaining": 0}
        finally:
            app.CLAIMS.clear()
            app.CLAIMS.update(claims)


def valcheck_browser_load_races():
    src = (ROOT / "static" / "index.html").read_text()
    def fn(name):
        i = src.index(f"function {name}(")
        return "async " + src[i:src.index("\n}\n", i) + 3]
    js = """
      const assert=(x,msg)=>{if(!x)throw Error(msg)};
      let labVal=false,labRev=false,labOpened=null,labWho='',labFocus='',labSkipped=0,labLoadSeq=0;
      let labClasses=[],labAttrs={},labConstraints={},labDefaults={},labImplies={},labRestricts={},labOptional={};
      let labQueue=[],labTotal=0,labApproved={},labAssign=null,labAuthErr=false,labIdle;
      const labOne=()=>labVal||labRev||!!labOpened;
      const curation=false,location={hash:'#valcheck'},els={};
      const document={getElementById:id=>(els[id] ||= {scrollIntoView:()=>{}})};
      let shows=0,notes=[];
      const labWhoShow=()=>{},labFocusFill=()=>{},labFocusSug=()=>{},labStats=()=>{},labChips=()=>{};
      const labShow=()=>{shows++},labNote=msg=>notes.push(msg);
      const tick=()=>new Promise(r=>setImmediate(r));
      const data=id=>({id,boxes:[],remaining:id==='old'?9:1,pending:[{id}],pending_total:1,
        classes:[id],attributes:{make:[id]},attr_constraints:{id},attr_defaults:{id},
        attr_implies:{id},attr_restricts:{id},attr_optional:{id},approved:{id},assignment:{id}});
      const reply=d=>({ok:true,status:200,json:async()=>d});
      let delay=null,release;
      const labFetch=async url=>{
        if(url.includes('count=true'))return reply({remaining:1});
        if(!delay)return reply(data('new'));
        const {stage,status,reject}=delay;delay=null;
        const response={ok:status===200,status,json:()=>stage==='json'
          ? new Promise((resolve,fail)=>{release=()=>reject?fail(Error('old error')):resolve(data('old'))})
          : Promise.resolve(data('old'))};
        if(stage==='fetch')return new Promise((resolve,fail)=>{
          release=()=>reject?fail(Error('old error')):resolve(response);
        });
        return response;
      };
      const state=()=>JSON.stringify({labVal,labRev,labOpened,labQueue,labTotal,labClasses,labAttrs,
        labConstraints,labDefaults,labImplies,labRestricts,labOptional,labApproved,labAssign,labAuthErr,
        els,shows,notes});
    """ + "\n".join(fn(n) for n in ("labLoad", "labOpenHit")) + """
      (async()=>{
        const cases=[['load','#valcheck','load','#review'],['load','#valcheck','load','#valcheck'],
          ['load','#valcheck','hit',''],['hit','','load','#review'],['hit','','hit','']];
        for(const stage of ['fetch','json'])for(const [from,hash,to,nextHash] of cases){
          for(const status of stage==='fetch'&&from==='load'?[200,401,403]:[200]){
            for(const reject of status===200?[false,true]:[false]){
              location.hash=hash;delay={stage,status,reject};
              const old=from==='load'?labLoad():labOpenHit('old','approved',true);
              await tick();
              location.hash=nextHash;
              await (to==='load'?labLoad():labOpenHit('new','approved',false));
              await tick();const current=state();
              release();await old;await tick();
              assert(state()===current,`stale ${from} ${stage} response changed newer ${to} ${nextHash} (status ${status}, reject ${reject})`);
            }
          }
        }
      })().catch(e=>{console.error(e);process.exitCode=1});
    """
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def valcheck_browser_pending_submission():
    src = (ROOT / "static" / "index.html").read_text()
    def fn(name):
        i = src.index(f"function {name}(")
        return "async " + src[i:src.index("\n}\n", i) + 3]
    js = """
      const assert=(x,msg)=>{if(!x)throw Error(msg)};
      let labVal=false,labRev=false,labOpened=null,labWho='',labFocus='',labSkipped=0,labLoadSeq=0;
      let labClasses=[],labAttrs={},labConstraints={},labDefaults={},labImplies={},labRestricts={},labOptional={};
      let labQueue=[],labBoxes=[],labTotal=0,labApproved={},labAssign=null,labAuthErr=false,labIdle;
      const labOne=()=>labVal||labRev||!!labOpened;
      const curation=false,location={hash:'#valcheck'},els={};
      const document={getElementById:id=>(els[id] ||= {})};
      const labWhoShow=()=>{},labFocusFill=()=>{},labFocusSug=()=>{},labStats=()=>{},labChips=()=>{},labNote=()=>{};
      const labShow=()=>{labBoxes=labQueue[0]?.boxes||[];document.getElementById('l-ok').disabled=!labQueue.length};
      const labGap=()=>null,labPayload=boxes=>({boxes,attrs:{0:{make:'toyota'}}});
      const sent=[];
      const post=async(url,body)=>{sent.push(body);return {ok:false}};
      const reply=id=>({ok:true,status:200,json:async()=>({id,rev:'123456abcdef',boxes:[],remaining:1})});
      const tick=()=>new Promise(r=>setImmediate(r));
      let pending=null,release;
      const labFetch=async url=>{
        if(url.includes('count=true'))return {ok:true,json:async()=>({remaining:1})};
        if(!pending)return reply('old-valcheck');
        const {stage,fail}=pending;pending=null;
        if(stage==='fetch')return new Promise((resolve,reject)=>{
          release=()=>fail?reject(Error('offline')):resolve(reply('new-review'));
        });
        return {ok:true,status:200,json:()=>new Promise((resolve,reject)=>{
          release=()=>fail?reject(Error('bad JSON')):resolve({id:'new-review',boxes:[]});
        })};
      };
    """ + "\n".join(fn(n) for n in ("labLoad", "labSend")) + """
      (async()=>{
        await labLoad();await labSend('valattrs');
        assert(sent.length===1&&sent[0].rev==='123456abcdef','valattrs must echo the loaded frame revision');
        sent.length=0;
        for(const stage of ['fetch','json'])for(const fail of [false,true]){
          location.hash='#valcheck';await labLoad();
          assert(!els['l-ok'].disabled,'loaded validation frame must be ready');
          assert(labQueue[0].id==='old-valcheck','start with a validation frame');
          location.hash='#review';pending={stage,fail};const loading=labLoad();
          await tick();
          await labSend(labVal?'valattrs':labOne()?'review':'approve');
          assert(sent.length===0,`old frame submitted during pending ${stage} transition`);
          assert(els['l-ok'].disabled,'submit must disable during transition');
          release();await loading;
          if(fail){
            await labSend('review');
            assert(sent.length===0,'failed load left old frame submittable');
            assert(els['l-ok'].disabled,'failed load must keep submit disabled');
          }else{
            assert(labQueue[0].id==='new-review','replacement frame applied');
            assert(!els['l-ok'].disabled,'replacement frame must enable submit');
            await labSend('review');
            assert(sent.length===1&&sent[0].id==='new-review','only replacement frame may submit');
            sent.length=0;
          }
        }
      })().catch(e=>{console.error(e);process.exitCode=1});
    """
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def valcheck_browser_delayed_back_and_writers():
    src = (ROOT / "static" / "index.html").read_text()
    def fn(name):
        i = src.index(f"function {name}(")
        return ("async " if src[i-6:i] == "async " else "") + src[i:src.index("\n}\n", i) + 3]
    start = src.index("document.getElementById('l-back').onclick =")
    back = src[start:src.index("\n};", start) + 3]
    js = """
      const assert=(x,msg)=>{if(!x)throw Error(msg)};
      let labVal=false,labRev=false,labOpened=null,labWho='',labFocus='',labSkipped=0,labLoadSeq=0;
      let labClasses=['car'],labAttrs={},labConstraints={},labDefaults={},labImplies={},labRestricts={},labOptional={};
      let labQueue=[],labBoxes=[],labTotal=0,labApproved={frames:1,boxes:{car:1}},labAssign=null,labAuthErr=false,labIdle;
      let labDone=[],labBlob='',labSel=-1,labClassifySaid=false,labLastCls=0,labSearchOffset=0;
      const labOne=()=>labVal||labRev||!!labOpened;
      const curation=false,location={hash:''},els={};
      const document={getElementById:id=>(els[id] ||= {querySelector:()=>({}),scrollIntoView:()=>{}})};
      let shows=0,notes=[],sent=[],revoked=[],created=0;
      const labWhoShow=()=>{},labFocusFill=()=>{},labFocusSug=()=>{},labStats=()=>{},labChips=()=>{},setCount=()=>{};
      const labNote=msg=>notes.push(msg),labHitButtons=()=>{},boxHtml=()=>'',labSelect=()=>{};
      const labGap=()=>null,labPayload=boxes=>({boxes,attrs:{}}),labDefault=()=>{},labRepaint=()=>{},labFill=()=>{};
      const prompt=()=>'-white',confirm=()=>true;
      const URL={createObjectURL:()=>`blob:${++created}`,revokeObjectURL:u=>revoked.push(u)};
      const tick=()=>new Promise(r=>setImmediate(r));
      const data=id=>({id,boxes:[{cls:0,attrs:{colour:'white'}}],attrs:{0:{colour:'white'}},remaining:1,
        pending:[{id,boxes:[{cls:0,attrs:{colour:'white'}}]}],pending_total:1,approved:{frames:1,boxes:{car:1}}});
      const reply=d=>({ok:true,status:200,json:async()=>d,blob:async()=>d});
      let delayPost=false,delayFetch=false,delayBlob=false,release,fail=false;
      const result=()=>({ok:true,boxes:[],attrs:{},attributes:{colour:['black']},attr_implies:{},rewritten:1,published:true});
      const post=async(url,body)=>{
        sent.push(body);
        if(delayPost){delayPost=false;return new Promise((resolve,reject)=>{
          release=()=>fail?reject(Error('no pending sample')):resolve(result());
        })}
        return result();
      };
      const labFetch=async url=>{
        if(url.includes('count=true'))return reply({remaining:1});
        if(delayFetch){delayFetch=false;return new Promise((resolve,reject)=>{
          release=()=>fail?reject(Error('offline')):resolve(reply(data('old')));
        })}
        if(delayBlob){delayBlob=false;return {ok:true,blob:()=>new Promise((resolve,reject)=>{
          release=()=>fail?reject(Error('offline')):resolve('old blob');
        })}}
        return reply(data('new'));
      };
      let rows=[{dataset:{open:'old',tree:'approved'}}];
      const labHitRows=()=>rows,labSearchDrop=()=>{};
      const labSearch=()=>new Promise(resolve=>{release=()=>{rows.push({dataset:{open:'old-next',tree:'approved'}});resolve()}});
      const state=()=>JSON.stringify({labQueue,labBoxes,labTotal,labDone,labApproved,labOpened,labVal,labRev,
        labAttrs,labImplies,labBlob,els,notes,revoked,created});
    """ + "\n".join(fn(n) for n in ("labLoad", "labOpenHit", "labShow", "labSend", "labImage",
                                       "labClassify", "labAddValue", "labHitStep")) + back + """
      (async()=>{
        // Back succeeds in the queue, but a response from before any new load is inert.
        for(const mode of ['#valcheck','#review','opened',''])for(fail of [false,true]){
          location.hash='';await labLoad();await tick();labDone=['old-approval'];labShow();await tick();
          delayPost=true;const old=els['l-back'].onclick();
          if(mode==='opened')await labOpenHit('new','approved',false);
          else {location.hash=mode;await labLoad()}
          await tick();const current=state();release();await old;await tick();
          assert(state()===current,`delayed Back changed ${mode||'queue'} (fail ${fail})`);
          if(mode){
            const count=sent.length;await labSend(labVal?'valattrs':'review');
            assert(sent.length===count+1&&sent[count].id==='new','Submit used the old approval');
          }
        }
        fail=false;location.hash='';await labLoad();labDone=['old-approval'];labShow();await tick();
        await els['l-back'].onclick();assert(labQueue[0].id==='old-approval','ordinary Back must still restore');
        for(const mode of ['#valcheck','#review','opened']){
          labDone=['old-approval'];
          if(mode==='opened')await labOpenHit('new','approved',false);
          else {location.hash=mode;await labLoad()}
          await tick();const count=sent.length;
          assert(els['l-back'].hidden&&els['l-back'].disabled,'single-frame Back must be unavailable');
          await els['l-back'].onclick();assert(sent.length===count,'single-frame Back posted');
        }
        for(const loader of ['queue','opened']){
          location.hash='';await labLoad();labDone=['old-approval'];labShow();await tick();
          delayFetch=true;const loading=loader==='queue'?labLoad():labOpenHit('new','approved',false);
          await tick();const count=sent.length;
          assert(els['l-back'].disabled,'loading must disable Back');
          await els['l-back'].onclick();assert(sent.length===count,'loading Back posted');
          release();await loading;await tick();
        }
        for(const writer of ['send','classify','value','image-fetch','image-blob'])for(fail of [false,true]){
          location.hash='';await labLoad();await tick();labDone=['old-approval'];
          let old;
          if(writer==='send'){delayPost=true;old=labSend('approve')}
          if(writer==='classify'){delayPost=true;old=labClassify(labBoxes[0],0)}
          if(writer==='value'){delayPost=true;old=labAddValue('colour','car')}
          if(writer.startsWith('image')){
            delayFetch=writer==='image-fetch';delayBlob=writer==='image-blob';old=labImage({},'new');
          }
          await tick();location.hash='#valcheck';await labLoad();await tick();
          const current=state();release();await old;await tick();
          assert(state()===current,`stale ${writer} changed current frame (fail ${fail})`);
        }
        fail=false;await labOpenHit('old','approved',false);await tick();document.getElementById('l-smore').hidden=false;
        const old=labHitStep(1);await tick();location.hash='#valcheck';await labLoad();await tick();
        const current=state();release();await old;await tick();
        assert(state()===current,'stale Next opened a search frame over valcheck');
      })().catch(e=>{console.error(e);process.exitCode=1});
    """
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def routes_point_at_their_handlers():
    """A helper inserted between a decorator and its function silently steals the route
    (FastAPI then demands the helper's arguments as query fields — a live 422). Pin the
    endpoints the tools depend on to the functions that implement them."""
    import app
    got = {(m, r.path): r.endpoint.__name__ for r in app.app.routes if hasattr(r, "endpoint")
           for m in (getattr(r, "methods", None) or ())}
    for (method, path), fn in {("GET", "/api/dataset/samples"): "dataset_samples",
                               ("GET", "/api/dataset/valcheck"): "dataset_valcheck",
                               ("POST", "/api/dataset/label"): "dataset_label",
                               ("POST", "/api/dataset/classify"): "dataset_classify",
                               ("GET", "/api/dataset/search"): "dataset_search",
                               ("POST", "/api/dataset/release"): "dataset_release",
                               ("POST", "/api/dataset/assign"): "assign_edit",
                               ("POST", "/api/dataset/class"): "dataset_class",
                               ("GET", "/api/config/site"): "config_site",
                               ("POST", "/api/config/site"): "config_site_set"}.items():
        assert got.get((method, path)) == fn, f"{method} {path} -> {got.get((method, path))!r}, expected {fn}"


check("hostnet arp parser", hostnet_arp_parser)
check("scan auto-joins once", scan_auto_joins_once)
check("hostnet jetson (naming, no-carrier, bootstrap)", hostnet_jetson)
check("scan skips own link-local /24", scan_skips_own_link_local)
check("setup-linux.sh (pi/jetson installer)", setup_linux_script)
check("setup-windows.ps1 (always-on task)", setup_windows_script)
check("hostnet windows (dongle detection)", hostnet_windows)
check("local_ips sees every interface", local_ips_sees_all_ifaces)
check("sadp probes every interface", sadp_probes_every_interface)
check("probe_device status codes", probe_status_codes)
check("hikvision tz strings", camera_tz_strings)
check("camera.set_time", camera_set_time)
check("camera name validation", camera_names)
check("recorder.add_camera", recorder_add_camera)
check("recorder timed stop", recorder_timed_stop)
check("record start validates hours", record_start_hours)
check("README documents every config key", readme_documents_config)
check("supervisor password + token minting", supervisor_tokens)
check("attribute restrict rules are real", attr_restricts_are_real)
check("a reviewer can remove an attribute value and remap its frames", attr_value_remove)
check("admins vet, the super admin releases", admins_vet_super_admin_releases)
check("search with no class lists the pen", search_lists_the_pen)
check("claims survive a restart", claims_survive_restart)
check("site rename", site_rename)
check("label strip rules (one-tap emergency, implies)", label_strip_rules)
check("assignment targets (several, all)", assign_targets)
check("training split honours the reference set", training_split_honours_reference)
check("queue orders by capture time", queue_orders_by_capture_time)
check("approved counts split new from frozen", approved_counts_split_new_from_frozen)
check("queue focus filters and suggests", queue_focus_filters_and_suggests)
check("classify suggests a class and its attributes", classify_suggests_class_and_attrs)
check("validation attrs queue and merge", validation_attrs_queue)
check("valcheck rejects changed box indexes", valcheck_rejects_changed_boxes)
check("valcheck browser explicit confirmation", valcheck_browser_rules)
check("same-size approved attrs sync", same_size_attrs_sync)
check("approved label/attrs pair and empty attrs sync", approved_pair_and_empty_attrs_sync)
check("valcheck browser conflict reload", valcheck_browser_conflict_reload)
check("valcheck requires a token-bound handle", valcheck_requires_bound_handle)
check("curation sync preserves local attrs", curation_sync_preserves_attrs)
check("valcheck transitions and count ordering", valcheck_browser_transition_and_count)
check("valcheck without AI and unknown suggestion", valcheck_without_ai_and_unknown)
check("valcheck delayed main responses", valcheck_browser_load_races)
check("valcheck pending transition blocks submission", valcheck_browser_pending_submission)
check("valcheck delayed Back and async frame writers", valcheck_browser_delayed_back_and_writers)
check("routes point at their handlers", routes_point_at_their_handlers)

print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'all passed'}")
sys.exit(1 if FAILS else 0)
