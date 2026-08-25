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

print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'all passed'}")
sys.exit(1 if FAILS else 0)
