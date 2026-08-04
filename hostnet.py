#!/usr/bin/env python3
"""Give this host an address on the camera's /24 — field switches have no DHCP."""

import ipaddress
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
from itertools import islice

DARWIN = sys.platform == "darwin"
WINDOWS = sys.platform == "win32"
WIRED = re.compile(r"^(en|eth|enp|ens|eno)\d")
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
            ifaces[m.group(1)] = {"name": m.group(1), "ips": [],
                                  "up": "UP" in m.group(2).split(",")}
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
    return next((i for i in ifs if i["link_local"]),
                next((i for i in ifs if WIRED.match(i["name"])), None))


def _in_use(ip, timeout=0.5):
    """ponytail: TCP:80 only — a host that answers nothing looks free. Fine for
    .2/.3/.4 on a camera switch; move to ARP if collisions ever turn up."""
    try:
        socket.create_connection((ip, 80), timeout).close()
        return True
    except OSError:
        return False


def free_addr(net, in_use=_in_use):
    return next((str(h) for h in islice(net.hosts(), 1, 4) if not in_use(str(h))), None)


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
        return {"ok": False, "error": f"no free address in {net} (tried .2 through .4)"}

    argv = _argv(iface["name"], addr, net)
    fail = {"ok": False, "cmd": _manual(argv), "grant": _grant()}
    try:
        p = subprocess.run(_elevated(argv), capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        return {**fail, "error": str(e)}
    if p.returncode != 0:
        return {**fail, "error": (p.stderr or p.stdout or "command failed").strip()[:300]}
    if addr not in [x for i in list_ifaces() for x in i["ips"]]:
        return {**fail, "error": f"command reported success but {addr} is not up"}
    return {"ok": True, "iface": iface["name"], "ip": addr, "cmd": _manual(argv)}


if __name__ == "__main__":
    for i in list_ifaces():
        if i["ips"]:
            print(f"{i['name']:10} up={i['up']:<5} link_local={i['link_local']:<5} {i['ips']}")
