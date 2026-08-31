#!/bin/sh
# Train by hand, outliving the SSH session that launched it. The loop (selfloop.py) does
# this on its own once enough is new; this is for "now, regardless".
#   ./run_training.sh          train now in a detached screen (pull first, promote if better)
#   ./run_training.sh watch    tail the live log
#   ./run_training.sh status   is it running, and where does the loop stand
cd "$(dirname "$0")"
PY=$([ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)   # this Mac trains from the system python
case "$1" in
  watch)  tail -f training.log ;;
  status) screen -ls | grep -q fieldkit-train && echo "training: RUNNING (screen fieldkit-train)" || echo "training: not running"
          $PY selfloop.py status ;;
  *)      screen -ls | grep -q fieldkit-train && { echo "already running — ./run_training.sh watch"; exit 1; }
          screen -dmS fieldkit-train sh -c "caffeinate -i $PY selfloop.py train --now > training.log 2>&1"
          echo "started in screen fieldkit-train — ./run_training.sh watch" ;;
esac
