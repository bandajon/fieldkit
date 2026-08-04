#!/bin/sh
# Build a self-contained installer zip: source + offline wheels for every
# target platform. Share the zip; field machines need no GitHub, no internet.
# Targets assume Python 3.12 on the field machine (see INSTALL.txt).
set -e
cd "$(dirname "$0")"
V=$(date +%Y%m%d)
OUT="dist/fieldkit-$V"
rm -rf dist && mkdir -p "$OUT/wheels"

git archive HEAD | tar -x -C "$OUT"          # tracked files only — never config.yaml

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

First boot creates config.yaml automatically. Then: Scan LAN -> Activate ->
Set IP with a cam slot -> the camera configures itself. See README.md.
EOF

(cd dist && zip -qr "fieldkit-$V.zip" "fieldkit-$V")
echo "dist/fieldkit-$V.zip"
