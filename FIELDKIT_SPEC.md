# FieldKit — Dexterity on-site capture & extraction console
## Build specification for Claude Code — target: working tool in ≤3 hours

## What this is
A single local-first web app that makes Dexterity 100% recorder-agnostic in the
field. It must do three jobs from one screen:

1. **First-contact camera setup** — discover factory-fresh or configured
   Hikvision cameras on the LAN, activate new ones (set password), assign each
   a static IP, and show a live-ish preview for aiming.
2. **Record on anything that isn't an NVR** — start/stop robust RTSP
   stream-copy recordings (ffmpeg) for any subset of cameras, on whatever
   machine runs the app (Windows laptop, Ubuntu mini-PC, Jetson Orin Nano).
3. **Pull from multiple NVRs on a switch** — drive the existing
   `nvr_pull.py` ISAPI extraction as a library: select NVRs, date, window;
   watch progress; see per-site VERIFIED status.

## Platform decision (already made — do not revisit)
Local web app: **Python 3.10+ / FastAPI / uvicorn**, one `static/index.html`
with vanilla JS (no npm, no build step). Serves on `0.0.0.0:8080` so a phone
on the field WiFi can drive it. Runs identically on x86 and ARM.

## Repository layout
```
fieldkit/
  app.py               # FastAPI app + all routes
  camera.py            # SADP discovery, ISAPI activation/IP/preview helpers
  recorder.py          # ffmpeg process manager
  nvr_pull.py          # EXISTS ALREADY — import as module, do not rewrite
  config.yaml          # sites/NVRs (same schema as nvr_pull) + tool settings
  static/index.html    # entire UI, single file, tabs: Cameras | Record | NVR Pull
  requirements.txt     # fastapi, uvicorn, requests, pyyaml
  README.md            # run instructions incl. Windows + Nano notes
```

## Hikvision specifics (implement exactly)
- **Discovery**: broadcast SADP probe — UDP to 239.255.255.250:37020, payload
  `<?xml version="1.0" encoding="utf-8"?><Probe><Uuid>{uuid4}</Uuid><Types>inquiry</Types></Probe>`
  — collect replies for ~3 s; parse DeviceDescription/IPv4Address/MAC/
  Activated fields from the XML responses. ALSO run a parallel TCP sweep of the
  local /24 probing port 80 `GET /ISAPI/System/deviceInfo` (digest auth with
  known creds) to find already-configured cameras SADP might miss. Merge
  results into one table keyed by MAC.
- **Activation** (factory-fresh cameras ship "inactive", default IP
  192.168.1.64): `PUT http://{ip}/ISAPI/System/activate` — body per Hikvision
  ISAPI: password sent per the device's activation schema. Implement the
  plain-XML variant first (`<ActivateInfo><password>...</password></ActivateInfo>`);
  if the device answers 401/400 with a security-capabilities challenge, fetch
  `GET /ISAPI/System/security/capabilities` and report "needs encrypted
  activation — activate this unit via SADP tool" rather than implementing RSA
  in v1. Log outcome per camera.
- **Static IP assignment**: `PUT /ISAPI/System/Network/interfaces/1` with the
  IPAddress XML (address, subnetMask, DefaultGateway). Follow with reboot call
  if the device requires it (`PUT /ISAPI/System/reboot`).
- **Preview for aiming**: poll `GET /ISAPI/Streaming/channels/101/picture`
  (JPEG snapshot) every 700 ms into an `<img>` — do NOT attempt RTSP in the
  browser. Provide a full-res single-shot button for framing checks.
- **RTSP URL for recording**: `rtsp://{user}:{pass}@{ip}:554/Streaming/Channels/101`
  (main stream). Sub stream = /102 — never record it.

## Recorder engine (recorder.py)
- Per camera: spawn
  `ffmpeg -rtsp_transport tcp -use_wallclock_as_timestamps 1 -i {rtsp}
   -c copy -f segment -segment_time 600 -reset_timestamps 1 -strftime 1
   {out}/{site}/{cam}/%Y%m%d-%H%M%S.mkv`
- Supervise: restart with backoff if ffmpeg exits while "recording" is the
  desired state; surface per-camera state (RECORDING / RECONNECTING / STOPPED),
  bytes written, minutes captured, disk free.
- Recordings root from config; must work on NTFS (Windows laptop) and ext4.
- Graceful stop = SIGINT/CTRL_BREAK so the last segment finalises; never
  SIGKILL first.

## NVR pull tab
- Import `nvr_pull` and run its per-site pull in a background thread pool via
  the same functions (do not shell out); stream log lines to the UI over a
  WebSocket or SSE endpoint; show per-site VERIFIED / ATTENTION badge from the
  return status and link the manifest.json.
- Config edit: textarea that round-trips config.yaml with validation.

## UI (static/index.html — keep it plain and thumb-friendly)
- Three tabs. Big touch targets (phone use at a junction, in sunlight —
  high contrast, no tiny icons).
- **Cameras tab**: Scan button → table (IP, MAC, model, Activated?, reachable?);
  per-row actions: Activate (password field), Set IP (next-free suggestion from
  a site plan dropdown: cam1=…111, cam2=…112 pattern), Preview (opens polling
  snapshot panel), Test RTSP (server-side 3-s probe, reports codec/resolution).
- **Record tab**: checklist of configured cameras with live state, one
  Start All / Stop All + per-camera toggles, disk-free bar, per-camera minutes
  + size counters.
- **NVR Pull tab**: NVR list from config with reachability dot, date + window
  pickers, Pull Selected / Pull All, live log pane, VERIFIED badges.
- Status strip on every tab: hostname, IP the server is bound to (so the
  operator knows what to type on the phone), disk free, clock.

## Non-goals for v1 (do not build)
- No user accounts/auth on the tool itself (field LAN only) — but bind
  camera/NVR credentials from config, never hardcode.
- No self-built streaming/transcoding — live video comes ONLY from the managed go2rtc sidecar (sub-streams); no HLS, no custom WebRTC.
- No editing of NVR recording schedules.
- No packaging/installers — `pip install -r requirements.txt && python app.py`.

## Acceptance checklist (run before calling it done)
1. `python app.py` on Ubuntu and the UI loads from a phone on the same LAN.
2. Scan finds a configured Hikvision camera AND reports an inactive one
   correctly (mock the SADP reply in a unit test if no hardware attached).
3. Activate + Set IP round-trip succeeds against a real camera (or the ISAPI
   calls are demonstrably correct against recorded fixtures).
4. Preview panel refreshes at <1 s cadence without browser memory growth.
5. Start Record on 2 cameras → files appear in segment tree, wall-clock
   timestamps verified with ffprobe; kill camera link → state shows
   RECONNECTING and recording resumes when link returns.
6. NVR Pull tab runs a real pull of a 10-minute window and shows VERIFIED.
7. With a go2rtc binary present, Live mode shows moving video for 2 cameras
   in the browser (laptop and phone), and killing go2rtc flips the grid back
   to snapshots without breaking the page.
8. Everything above works with the server on the Jetson (ARM) unchanged.

## Build order for the 3 hours
1. (30 min) FastAPI skeleton + config load + static UI shell with tabs
2. (45 min) recorder.py + Record tab (this is the "any platform" insurance —
   highest value, build it before discovery)
3. (45 min) camera.py discovery + preview + Test RTSP
4. (30 min) activation + static-IP actions
5. (30 min) NVR Pull tab wiring around existing nvr_pull.py
6. (30 min) live.py go2rtc sidecar + Monitor tab Live toggle
7. (remaining) acceptance checklist + README

Note: with Tier 3 in scope the realistic build is 3–3.5 hours. If the clock
wins, ship with the Live toggle greyed out — the sidecar design means it can
land later without touching anything else.

## Monitoring (added v1.1)
**Tier 1 (in scope for the 3-hour build):** the snapshot-polling preview IS
junction monitoring. Add one small thing beyond the per-camera preview: a
**Monitor tab** (or a "wall" toggle on the Cameras tab) showing all configured
cameras as a grid of polling snapshots (1 fps, pause when tab hidden). Grid
must degrade gracefully on a phone (single column).

**Tier 2 (deployment config, zero code):** the app already binds 0.0.0.0, so
remote monitoring = Tailscale on the host + any internet source (4G router /
phone hotspot) at the node. README must include a short "Remote access via
Tailscale" section: install, `tailscale up`, open http://<tailscale-ip>:8080.
Note that NVR web UIs on the same LAN become reachable the same way via
`tailscale up --advertise-routes=<site-subnet>` on the node host.

**Tier 3 (IN SCOPE — v1.2): smooth live video via a go2rtc sidecar.**
FieldKit does not implement any streaming itself — it manages go2rtc
(single static binary, no dependencies, builds exist for linux-amd64,
linux-arm64 and windows) and embeds its player:

- `live.py`: locate the go2rtc binary (path in config.yaml, key
  `go2rtc_binary`; if absent, Monitor tab shows snapshots only and a hint —
  NEVER auto-download in the field). Generate `go2rtc.yaml` from the
  configured camera list using SUB-streams only:
  `streams: { cam1: rtsp://user:pass@ip:554/Streaming/Channels/102, ... }`
  Start/supervise the process like a recorder (restart with backoff), expose
  state in the status strip.
- Monitor tab gains a **Snapshots | Live** toggle. Live mode embeds go2rtc's
  WebRTC player per camera (iframe to
  `http://{host}:1984/stream.html?src={cam}` or its video-stream.js module),
  same responsive grid. Falls back to snapshot tile per-camera if its stream
  errors.
- Sub-stream discipline is a hard rule: never point go2rtc at /101. Set an
  expectation note in the UI: live view is the 640×360 aiming stream, not
  evidence quality — recording quality is unaffected.
- Remote (Tier 2) combines transparently: WebRTC over Tailscale works
  peer-to-peer or via DERP relay; document in README.
