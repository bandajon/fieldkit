# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

FieldKit — a local-first web console for on-site Hikvision camera setup, RTSP recording, and NVR footage extraction. **The full build specification is `FIELDKIT_SPEC.md` — read it before doing anything.** Most of the app described there does not exist yet; the repo currently contains only the spec and `nvr_pull.py`.

## Hard constraints (already decided — do not revisit)

- Stack: Python 3.10+ / FastAPI / uvicorn, single `static/index.html` with vanilla JS. No npm, no build step, no installers. Binds `0.0.0.0:8080`. Must run unchanged on x86 (Windows/Ubuntu) and ARM (Jetson Orin Nano).
- `nvr_pull.py` **exists and works — import it as a library, never rewrite it or shell out to it.** Its contract: pulls Hikvision NVR footage via ISAPI into `<out_dir>/<date>/<site>/<cam>/…`, writes `manifest.json` (sha256 + durations) and a `.verified` marker only when every check passes. It never deletes anything from an NVR.
- Recording: ffmpeg stream-copy (`-c copy`) of the RTSP **main** stream `/Streaming/Channels/101`, 600 s segments, wallclock timestamps. Graceful stop is SIGINT/CTRL_BREAK so the last segment finalises — never SIGKILL first.
- Live video comes only from a managed go2rtc sidecar on **sub-streams** (`/102`) — FieldKit implements no streaming/transcoding itself. Never point go2rtc at `/101`; never auto-download the binary.
- Browser preview is JPEG snapshot polling (`GET /ISAPI/Streaming/channels/101/picture`), not RTSP.
- No auth on the tool (field-LAN only), but camera/NVR credentials come from `config.yaml` — never hardcode them. `config.yaml` shares the schema `nvr_pull.py` reads (`nvrs:` list with name/host/user/password/channels, `out_dir`, `remux_mkv`).

## Running

```
pip install -r requirements.txt   # fastapi, uvicorn, requests, pyyaml
python app.py                     # once app.py exists

# The existing extraction tool works standalone today:
python3 nvr_pull.py --config config.yaml --date 2026-08-04 --start 05:00 --end 21:30 [--site NAME] [--dry-run]
```

ffmpeg/ffprobe must be on PATH. There is no test framework configured; the spec's acceptance checklist (spec §Acceptance) is the definition of done, and unit tests may mock SADP replies when no hardware is attached.

## Architecture (target layout from the spec)

- `app.py` — FastAPI app + all routes
- `camera.py` — SADP UDP discovery (broadcast to 239.255.255.250:37020) merged with a TCP /24 sweep of `/ISAPI/System/deviceInfo`; ISAPI activation and static-IP assignment
- `recorder.py` — ffmpeg process supervisor (restart with backoff while "recording" is desired; states RECORDING / RECONNECTING / STOPPED)
- `live.py` — go2rtc sidecar manager (binary path from `config.yaml` key `go2rtc_binary`; absent → snapshots-only with a hint)
- `nvr_pull.py` — existing; NVR Pull tab runs `pull_site()` in a background thread pool and streams `log()` output to the UI (SSE/WebSocket)
- `static/index.html` — entire UI, tabs: Cameras | Record | NVR Pull | Monitor; thumb-friendly, high contrast for phone use in sunlight
