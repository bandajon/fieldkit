# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

FieldKit — a local-first web console for on-site Hikvision camera setup and RTSP recording. **The full build specification is `FIELDKIT_SPEC.md` — read it before doing anything.** NVR support was removed in 2026-08: FieldKit records cameras directly over RTSP only.

## Hard constraints (already decided — do not revisit)

- Stack: Python 3.10+ / FastAPI / uvicorn, single `static/index.html` with vanilla JS. No npm, no build step, no installers. Binds `0.0.0.0:8080`. Must run unchanged on x86 (Windows/Ubuntu) and ARM (Jetson Orin Nano).
- Recording: ffmpeg stream-copy (`-c copy`) of the RTSP **main** stream `/Streaming/Channels/101`, 600 s segments, wallclock timestamps. Graceful stop is SIGINT/CTRL_BREAK so the last segment finalises — never SIGKILL first.
- Live video comes only from a managed go2rtc sidecar on **sub-streams** (`/102`) — FieldKit implements no streaming/transcoding itself. Never point go2rtc at `/101`; never auto-download the binary.
- Browser preview is JPEG snapshot polling (`GET /ISAPI/Streaming/channels/101/picture`), not RTSP.
- No auth on the tool (field-LAN only), but camera credentials come from `config.yaml` — never hardcode them.

## Running

```
pip install -r requirements.txt   # fastapi, uvicorn, requests, pyyaml
python app.py
```

ffmpeg/ffprobe must be on PATH. There is no test framework configured; the spec's acceptance checklist (spec §Acceptance) is the definition of done, and unit tests may mock SADP replies when no hardware is attached.

## Architecture (target layout from the spec)

- `app.py` — FastAPI app + all routes
- `camera.py` — SADP UDP discovery (broadcast to 239.255.255.250:37020) merged with a TCP /24 sweep of `/ISAPI/System/deviceInfo`; ISAPI activation and static-IP assignment
- `recorder.py` — ffmpeg process supervisor (restart with backoff while "recording" is desired; states RECORDING / RECONNECTING / STOPPED)
- `live.py` — go2rtc sidecar manager (binary path from `config.yaml` key `go2rtc_binary`; absent → snapshots-only with a hint)
- `static/index.html` — entire UI, tabs: Cameras | Record | Monitor; thumb-friendly, high contrast for phone use in sunlight
