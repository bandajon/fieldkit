#!/bin/sh
# Build a self-contained installer zip: source + offline wheels for every
# target platform AND every Python a field machine plausibly has (only
# pydantic-core/PyYAML are version-locked; the rest are shared pure wheels).
# Windows: 3.12-3.14 (python.org installers). Ubuntu 22.04/Jetson: 3.10;
# 24.04: 3.12.
set -e
cd "$(dirname "$0")"
V=$(date +%Y%m%d-%H%M)   # minutes matter: same-day rebuilds must not look identical
OUT="dist/fieldkit-$V"
rm -rf dist && mkdir -p "$OUT/wheels"

git archive HEAD | tar -x -C "$OUT"          # tracked files only — never the WORKING config.yaml
# Pre-seed config.yaml FROM THE EXAMPLE (placeholder creds only) so a novice
# never sees a missing-config moment. The real config.yaml is untracked and
# can never reach the zip via git archive.
cp "$OUT/config.example.yaml" "$OUT/config.yaml"

wheelset() {  # platform, python versions...
  P=$1; shift
  for PYV in "$@"; do
    pip download -r requirements.txt -d "$OUT/wheels/$P" \
      --platform "$P" --only-binary=:all: --python-version "$PYV" -q
  done
}
wheelset win_amd64            3.12 3.13 3.14
wheelset manylinux2014_x86_64 3.10 3.11 3.12
wheelset manylinux2014_aarch64 3.10 3.12

# pip evaluates environment markers on THIS machine (macOS), so deps guarded by
# platform_system == "Windows" never download for the win target. Add them by hand.
# Known: click -> colorama. Re-check when requirements change:
#   pip download --dry-run on a real Windows box, or grep Requires-Dist metadata.
pip download colorama -d "$OUT/wheels/win_amd64" \
  --platform win_amd64 --only-binary=:all: --python-version 3.12 -q

cat > "$OUT/INSTALL.txt" <<'EOF'
FieldKit offline install
========================

WINDOWS - the whole install, in order (each prompt named):
  1. Install Python from python.org if the machine has none (3.12, 3.13 and
     3.14 all work). Tick BOTH boxes: "Add python.exe to PATH" AND
     "Install for all users" (Customize install).
  2. Right-click the downloaded ZIP -> Properties -> tick "Unblock" -> OK.
     (Skipping this makes Windows silently distrust every extracted file.)
  3. Right-click the ZIP -> "Extract All..." - do NOT run anything from the
     zip preview window.
  4. In the extracted folder, double-click  setup-windows.bat
     -> click "Run" on the security warning (if shown)
     -> click "Yes" on the blue Administrator prompt.
     A second window opens and does everything: dependencies, firewall,
     camera network, and starts the app. Keep that window open.
  5. ffmpeg (needed for RECORDING): download from ffmpeg.org, add to PATH.
     Setup warns you if it is missing; the UI works without it.
  Re-running setup-windows.bat any time is safe - it fixes itself.

OTHER PLATFORMS:
1. Install Python 3.12 and ffmpeg:
   - Ubuntu/Jetson: sudo apt install python3 python3-pip ffmpeg
2. Unzip this folder anywhere.
3. Install dependencies from the bundled wheels (pick your platform):
   - Ubuntu mini-PC:   pip3 install --no-index --find-links wheels/manylinux2014_x86_64 -r requirements.txt
   - Jetson Orin Nano: pip3 install --no-index --find-links wheels/manylinux2014_aarch64 -r requirements.txt
4. Run it:            python3 app.py     (Windows: python app.py)
5. On a phone on the same WiFi, open http://<this machine's IP>:8080
   (the IP is shown in the strip at the top of the screen).

Connecting the laptop to the field switch
-----------------------------------------
Field switches have no DHCP server. Plug in and the laptop self-assigns a
169.254.x.x address; cameras may still show up in the scan, but every action
against them fails because this machine is not on their network.

FieldKit fixes this for you: a scan row on a network you are not on shows a
"Join camera network" button. Grant the privilege once so it works silently:

  macOS:   echo "$USER ALL=(root) NOPASSWD: /usr/sbin/ipconfig" | sudo tee /etc/sudoers.d/fieldkit
  Ubuntu:  echo "$USER ALL=(root) NOPASSWD: $(command -v ip)" | sudo tee /etc/sudoers.d/fieldkit
  Windows: start FieldKit from a terminal opened with "Run as administrator"

Without it the button prints the exact command to run. To do it by hand:

  macOS:   sudo ipconfig set en7 MANUAL 192.168.1.2 255.255.255.0
           (en7 = the adapter; `networksetup -listallhardwareports` names it.
           Or System Settings > Network > the adapter > Details > TCP/IP >
           Configure IPv4: Manually)
  Windows: Settings > Network > Change adapter options > adapter > Properties
           > Internet Protocol Version 4 > Use the following IP address:
           192.168.1.2 / 255.255.255.0
  Ubuntu:  sudo ip addr add 192.168.1.2/24 dev eth0

LEAVE THE GATEWAY EMPTY. With no gateway on the Ethernet port the internet
keeps working over WiFi and only camera traffic uses the cable.

Factory-fresh Hikvision cameras sit at 192.168.1.64, so 192.168.1.x/24 is the
right network to join for first contact.

First boot creates config.yaml automatically. Then: Scan LAN -> Activate ->
Set IP with a cam slot -> the camera configures itself. See README.md.
EOF

(cd dist && zip -qr "fieldkit-$V.zip" "fieldkit-$V")
echo "dist/fieldkit-$V.zip"
