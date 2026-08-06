#!/usr/bin/env python3
"""Give this host an address on the camera's /24 — field switches have no DHCP."""

import ipaddress
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from itertools import islice

DARWIN = sys.platform == "darwin"
WINDOWS = sys.platform == "win32"
WIRED = re.compile(r"^(en|eth|enp|ens|eno)\d", re.I)   # Jetson Orin: enP8p1s0
LOOPBACK = ("lo", "lo0")


def _run(argv):
    return subprocess.run(argv, capture_output=True, text=True, timeout=10).stdout


def _ifconfig():
    ifaces, cur = [], None
    for line in _run(["ifconfig", "-a"]).splitlines():
        m = re.match(r"^(\w+):\s", line)
        if m:
            cur = {"name": m.group(1), "ips": [], "up": False}
            ifaces.append(cur)
        elif cur is not None:
            m = re.match(r"\s+inet (\d+\.\d+\.\d+\.\d+)", line)
            if m:
                cur["ips"].append(m.group(1))
            elif "status: active" in line:
                cur["up"] = True
    return ifaces


def _iproute():
    ifaces = {}
    for line in _run(["ip", "-o", "link"]).splitlines():
        m = re.match(r"\d+:\s+([^:@]+)[:@].*?<([^>]*)>", line)
        if m:
            flags = m.group(2).split(",")
            # NO-CARRIER = admin-up but no cable; offering it would just fail later.
            ifaces[m.group(1)] = {"name": m.group(1), "ips": [],
                                  "up": "UP" in flags and "NO-CARRIER" not in flags}
    for line in _run(["ip", "-o", "-4", "addr"]).splitlines():
        p = line.split()
        if len(p) > 3 and p[1] in ifaces:
            ifaces[p[1]]["ips"].append(p[3].split("/")[0])
    return list(ifaces.values())


def _netsh():
    # ponytail: netsh only lists configured interfaces, so up=True for all of them.
    ifaces, cur = [], None
    for line in _run(["netsh", "interface", "ipv4", "show", "addresses"]).splitlines():
        m = re.match(r'Configuration for interface "(.+)"', line.strip())
        if m:
            cur = {"name": m.group(1), "ips": [], "up": True}
            ifaces.append(cur)
        elif cur is not None:
            m = re.search(r"IP Address:\s+(\d+\.\d+\.\d+\.\d+)", line)
            if m:
                cur["ips"].append(m.group(1))
    return ifaces


def list_ifaces():
    fn = _ifconfig if DARWIN else _netsh if WINDOWS else _iproute
    try:
        ifaces = fn()
    except (OSError, subprocess.SubprocessError):
        return []
    for i in ifaces:
        # A cable into a DHCP-less switch self-assigns 169.254.x and nothing else.
        i["link_local"] = bool(i["ips"]) and all(x.startswith("169.254.") for x in i["ips"])
    return [i for i in ifaces if i["name"] not in LOOPBACK]


def pick_iface(exclude_ip="", ifaces=None):
    """The self-assigned interface if there is one, else a wired one that is not
    carrying the default route (taking that over would kill the operator's internet)."""
    ifs = [i for i in (list_ifaces() if ifaces is None else ifaces)
           if i["up"] and exclude_ip not in i["ips"]]
    # A wired port with no address at all is the Linux field-switch signature
    # (macOS/Windows self-assign 169.254.x there; Linux leaves the port bare).
    return next((i for i in ifs if i["link_local"]),
                next((i for i in ifs if WIRED.match(i["name"]) and not i["ips"]),
                     next((i for i in ifs if WIRED.match(i["name"])), None)))


MAC = re.compile(r"\b([0-9a-fA-F]{1,2}[:-]){5}[0-9a-fA-F]{1,2}\b")


def parse_arp(text):
    """Pure: arp / ip-neigh output → True when a real MAC is present (address taken).
    ponytail: one regex for all three platforms — each either prints a MAC or doesn't."""
    low = text.lower()
    if "incomplete" in low or "no entry" in low or "no arp entries" in low:
        return False
    if re.search(r"\b(failed|permanent_failed)\b", low):
        return False
    return bool(MAC.search(text))


def _in_use(ip, timeout=0.5):
    """ponytail: ARP, not TCP:80 — a laptop with its firewall up answers nothing on
    :80 but cannot hide from layer 2. Two laptops grabbing .2 on one switch is the
    conflict this prevents."""
    ms = max(int(timeout * 1000), 1)
    ping = (["ping", "-n", "1", "-w", str(ms), ip] if WINDOWS else
            ["ping", "-c", "1", "-W", str(ms if DARWIN else max(int(timeout), 1)), ip])
    show = (["arp", "-a", ip] if WINDOWS else
            ["arp", "-n", ip] if DARWIN else ["ip", "neigh", "show", ip])
    try:
        subprocess.run(ping, capture_output=True, timeout=3)   # populate the neighbour table
        return parse_arp(subprocess.run(show, capture_output=True, text=True,
                                        timeout=3).stdout)
    except (OSError, subprocess.SubprocessError):
        return False


def free_addr(net, in_use=_in_use):
    return next((str(h) for h in islice(net.hosts(), 1, 10) if not in_use(str(h))), None)


def _argv(iface, addr, net):
    if DARWIN:
        return ["ipconfig", "set", iface, "MANUAL", addr, str(net.netmask)]
    if WINDOWS:
        return ["netsh", "interface", "ipv4", "add", "address",
                f"name={iface}", f"addr={addr}", f"mask={net.netmask}"]
    return ["ip", "addr", "add", f"{addr}/{net.prefixlen}", "dev", iface]


def _manual(argv):
    if WINDOWS:
        return " ".join(f'"{a}"' if " " in a else a for a in argv)
    return "sudo " + shlex.join(argv)


def _grant():
    if WINDOWS:
        return "Start FieldKit from a terminal opened with 'Run as administrator'."
    tool = "ipconfig" if DARWIN else "ip"
    path = shutil.which(tool) or f"/usr/sbin/{tool}"
    return f'echo "$USER ALL=(root) NOPASSWD: {path}" | sudo tee /etc/sudoers.d/fieldkit'


def _elevated(argv):
    if not WINDOWS and os.geteuid() != 0:
        return ["sudo", "-n"] + argv       # -n: fail fast instead of prompting a TTY
    return argv


def join(cidr, exclude_ip=""):
    """Add a free host address in `cidr` to a spare interface. → {ok, iface, ip, cmd}
    or {ok: False, error, cmd, grant}."""
    net = ipaddress.ip_network(cidr, strict=False)
    if not net.is_private:
        return {"ok": False, "error": f"{net} is not a private network — refusing"}
    iface = pick_iface(exclude_ip)
    if iface is None:
        return {"ok": False, "error": "no spare wired interface — is the Ethernet cable in?"}
    addr = free_addr(net)
    if addr is None:
        return {"ok": False, "error": f"no free address in {net} (tried .2 through .9)"}

    argv = _argv(iface["name"], addr, net)
    fail = {"ok": False, "cmd": _manual(argv), "grant": _grant()}
    try:
        p = subprocess.run(_elevated(argv), capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        return {**fail, "error": str(e)}
    if p.returncode != 0:
        return {**fail, "error": (p.stderr or p.stdout or "command failed").strip()[:300]}
    # macOS ipconfig set is asynchronous (configd applies it after the command
    # returns) — poll instead of checking once and losing the race.
    deadline = time.time() + 6
    while time.time() < deadline:
        if addr in [x for i in list_ifaces() for x in i["ips"]]:
            return {"ok": True, "iface": iface["name"], "ip": addr, "cmd": _manual(argv)}
        time.sleep(0.5)
    return {**fail, "error": f"command reported success but {addr} did not come up within 6s"}


LINK_LOCAL = "169.254.100.0/24"   # mid-range: RFC 3927 reserves the .0.x and .255.x ends


def bootstrap(exclude_ip=""):
    """Linux never self-assigns on a DHCP-less switch the way macOS/Windows do: an up
    wired port with no IPv4 can't even egress the SADP probe, so discovery finds
    nothing and auto-join never triggers. Park a link-local address on such a port.
    → join() result, or None when there is nothing to do."""
    if DARWIN or WINDOWS:
        return None
    i = pick_iface(exclude_ip)
    if i is None or i["ips"]:
        return None
    return join(LINK_LOCAL, exclude_ip)


if __name__ == "__main__":
    for i in list_ifaces():
        if i["ips"]:
            print(f"{i['name']:10} up={i['up']:<5} link_local={i['link_local']:<5} {i['ips']}")
