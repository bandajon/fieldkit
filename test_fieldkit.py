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
            assert s(who="curator01")["total"] == 1, "a curator still narrows"
            assert s(target="e-heavy", who="curator01")["total"] == 0
            try:
                s(target="zzz")
                raise AssertionError("an unknown class must still be refused")
            except app.HTTPException as e:
                assert e.status_code == 400, e.detail
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
    for cls, dd in defs.items():
        for head, v in dd.items():
            allowed = (cons.get(cls) or {}).get(head, vocab.get(head, []))
            assert v in allowed, f"{cls}: default {head}={v!r} is not one of {allowed}"
    z = defs.get("z-emergency") or {}
    for head in vocab:
        if (cons.get("z-emergency") or {}).get(head, vocab[head]):     # visible means required
            assert head in z, f"z-emergency: no default for {head}, so it is not one tap"
    if not shutil.which("node"):
        print("  (node not installed: JS rules not run)")
        return
    src = (ROOT / "static" / "index.html").read_text()

    def fn(name):
        i = src.index(f"function {name}(")
        return src[i:src.index("\n}\n", i) + 3]
    js = "const labClasses=%s, labAttrs=%s, labConstraints=%s, labDefaults=%s, labImplies=%s, labRestricts=%s;\n" % (
        json.dumps([l.strip() for l in (app.DATASET / "classes.txt").read_text().splitlines() if l.strip()]),
        json.dumps(vocab), json.dumps(cons), json.dumps(defs), json.dumps(cfg.get("implies") or {}),
        json.dumps(cfg.get("restricts") or {}))
    js += src[src.index("const labLocked"):src.index("\n", src.index("const labLocked"))] + "\n"
    js += "\n".join(fn(n) for n in ("labVocab", "labFill", "labDefault", "labGap"))
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
console.log('  js rules ok');
"""
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    print(r.stdout.strip())


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
check("search with no class lists the pen", search_lists_the_pen)
check("claims survive a restart", claims_survive_restart)
check("site rename", site_rename)
check("label strip rules (one-tap emergency, implies)", label_strip_rules)

print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'all passed'}")
sys.exit(1 if FAILS else 0)
