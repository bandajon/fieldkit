#!/bin/sh
# One-shot FieldKit setup for Raspberry Pi / Jetson / Ubuntu. Safe to re-run —
# every step is idempotent and re-running fixes a broken install.
set -e
cd "$(dirname "$0")"

if [ "$(id -u)" = 0 ]; then
    echo "Run this as your normal user, not root — it sudos where needed." >&2
    exit 1
fi

echo "[1/4] system packages (ffmpeg + venv support)"
sudo apt-get update -qq
# python3-venv: Debian Bookworm+ and Ubuntu 24.04+ refuse bare pip installs
# into the system Python (PEP 668) — a venv is the supported path everywhere.
sudo apt-get install -y -qq python3-venv ffmpeg

echo "[2/4] python dependencies"
python3 -m venv .venv
ARCH=$(uname -m)      # aarch64 on Pi 4/5 + Jetson, x86_64 on a mini-PC
if [ -d "wheels/manylinux2014_$ARCH" ]; then
    # release zip: fully offline install from the bundled wheels
    .venv/bin/pip install -q --no-index --find-links "wheels/manylinux2014_$ARCH" \
        -r requirements.txt
else
    .venv/bin/pip install -q -r requirements.txt
fi

echo "[3/4] network privilege (lets Scan LAN join camera networks silently)"
echo "$USER ALL=(root) NOPASSWD: $(command -v ip)" | sudo tee /etc/sudoers.d/fieldkit >/dev/null
sudo chmod 440 /etc/sudoers.d/fieldkit

echo "[4/4] start now and on every boot"
sudo tee /etc/systemd/system/fieldkit.service >/dev/null <<UNIT
[Unit]
Description=FieldKit camera console
After=network.target

[Service]
User=$USER
WorkingDirectory=$PWD
ExecStart=$PWD/.venv/bin/python app.py
Restart=always
RestartSec=3
# SIGINT reaches ffmpeg too (control-group kill), so the last segment finalises.
KillSignal=SIGINT
TimeoutStopSec=20

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now fieldkit
sudo systemctl restart fieldkit   # re-run after a git pull picks up the new code

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo ""
echo "FieldKit is running and will start on every boot."
echo "Open http://${IP:-this-machine}:8080 from a phone on the same network."
echo "Logs: journalctl -u fieldkit -f"
