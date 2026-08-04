#!/usr/bin/env python3
"""Plain stdlib test runner: python3 test_fieldkit.py — exits non-zero on failure."""

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
    for section in ("nvrs", "cameras"):
        for entry in cfg.get(section) or []:
            keys |= set(entry)
    for ch in (cfg.get("nvrs") or [{}])[0].get("channels") or []:
        keys |= set(ch)
    keys |= set(cfg.get("camera_defaults") or {})
    missing = sorted(k for k in keys if not re.search(rf"\b{re.escape(k)}\b", readme))
    assert not missing, f"undocumented config keys: {missing}"


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


check("camera.py self-check", self_check("camera.py"))
check("recorder.py self-check", self_check("recorder.py"))
check("live.py self-check", self_check("live.py"))
check("sadp reports inactive camera", sadp_inactive)
check("mac normalisation", macs)
check("go2rtc absent without a binary", live_absent)
check("camera name validation", camera_names)
check("recorder.add_camera", recorder_add_camera)
check("README documents every config key", readme_documents_config)

print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'all passed'}")
sys.exit(1 if FAILS else 0)
