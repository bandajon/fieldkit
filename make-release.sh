#!/bin/sh
# Build a self-contained installer zip: source + offline wheels for every
# target platform. Share the zip; field machines need no GitHub, no internet.
# Targets assume Python 3.12 on the field machine (see INSTALL.txt).
set -e
cd "$(dirname "$0")"
V=$(date +%Y%m%d)
OUT="dist/fieldkit-$V"
rm -rf dist && mkdir -p "$OUT/wheels"

git archive HEAD | tar -x -C "$OUT"          # tracked files only — never the WORKING config.yaml
# Pre-seed config.yaml FROM THE EXAMPLE (placeholder creds only) so a novice
# never sees a missing-config moment. The real config.yaml is untracked and
# can never reach the zip via git archive.
cp "$OUT/config.example.yaml" "$OUT/config.yaml"

for P in win_amd64 manylinux2014_x86_64 manylinux2014_aarch64; do
  pip download -r requirements.txt -d "$OUT/wheels/$P" \
    --platform "$P" --only-binary=:all: --python-version 3.12 -q
done

cat > "$OUT/INSTALL.txt" <<'EOF'
FieldKit offline install
========================
1. Install Python 3.12 and ffmpeg on this machine:
   - Windows:      python.org installer (tick "Add to PATH"), ffmpeg from ffmpeg.org, add to PATH
   - Ubuntu/Jetson: sudo apt install python3 python3-pip ffmpeg
2. Unzip this folder anywhere.
3. Install dependencies from the bundled wheels (pick your platform):
   - Windows laptop:   pip install --no-index --find-links wheels/win_amd64 -r requirements.txt
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
