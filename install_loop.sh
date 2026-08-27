#!/bin/sh
# Install the self-improving loop on this machine (macOS launchd, per-user, no sudo):
# ingest every 4 hours, a training check every hour. Re-run to change intervals.
# launchd starts jobs with a bare PATH, so the venv's bin (ffmpeg/ffprobe live there on a
# machine without Homebrew: pip install static-ffmpeg, then symlink) is put on it here.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="$HERE/.venv/bin/python"
[ -x "$PY" ] || { echo "no venv at $HERE/.venv — set the machine up first"; exit 1; }
mkdir -p ~/Library/LaunchAgents
agent() {   # label, seconds, subcommand
  cat > ~/Library/LaunchAgents/$1.plist <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$1</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/caffeinate</string><string>-i</string>
    <string>$PY</string><string>$HERE/selfloop.py</string><string>$3</string></array>
  <key>WorkingDirectory</key><string>$HERE</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>$HERE/.venv/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
  <key>StartInterval</key><integer>$2</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$HERE/loop-$3.log</string>
  <key>StandardErrorPath</key><string>$HERE/loop-$3.log</string>
</dict></plist>
EOF
  launchctl bootout gui/$(id -u)/$1 2>/dev/null || true
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/$1.plist
  echo "  $1: every $(( $2 / 3600 ))h -> selfloop.py $3 (log: loop-$3.log)"
}
agent com.fieldkit.loop-ingest 14400 ingest
agent com.fieldkit.loop-train   3600 train
