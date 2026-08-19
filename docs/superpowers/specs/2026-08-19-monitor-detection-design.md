# Monitor detection overlay — design

2026-08-19 · approved approach: A (server-side detection loop, annotated JPEG)

## Goal

The Monitor tab shows bounding boxes + class labels on each camera tile, plus a
fixed bottom-right counter with cumulative daily totals per vehicle class.
Runs on the dev Mac (CPU) and unchanged on the site servers (Intel i5 +
Hailo-8) with only the inference backend swapped. DeepStream is out: it is
NVIDIA-only and neither target has an NVIDIA GPU.

## Architecture

New module `detect.py`, one background thread:

1. Every ~1 s per configured camera, fetch a snapshot via the existing
   `camera.snapshot(ip, user, password)`.
2. Run `infer(jpeg_bytes) -> [Detection(cls, conf, box_xyxy)]` — the single
   pluggable backend function:
   - **cpu** (default): `ultralytics` YOLOv8n, auto-downloads weights, runs on
     CPU/MPS. Dev + fallback.
   - **hailo** (phase: site deploy): HailoRT Python API with a precompiled
     YOLOv8 `.hef` from the Hailo model zoo. Selected via `config.yaml` key
     `detect_backend: hailo` + `hef_path`.
3. Filter to COCO vehicle classes: car, truck, bus, motorcycle.
   Heavy machinery is deferred — needs a custom-trained model compiled to
   `.hef` (Hailo Dataflow Compiler); phase 2.
4. Update the per-camera IoU tracker and daily totals (below).
5. Draw boxes + `class conf` labels into the JPEG with Pillow; keep the latest
   annotated frame + detections in memory per camera. Nothing on disk.

The loop runs regardless of whether a browser is open, so daily totals accrue
unattended. Cameras that fail to snapshot are skipped that tick (existing
snapshot error path already tolerates this).

## Counting

Greedy IoU matcher per camera: match current detections to live tracks
(IoU ≥ 0.3, same class); a detection unmatched for 2 consecutive frames
becomes a new track and increments `totals[cls]`. Tracks unseen for 5 s die.
Totals reset at local midnight. Totals are in-memory only (restart = reset);
overlay-only scope, no persistence.

Known ceiling (`ponytail:` comment in code): at 1 fps, a vehicle that parks,
gets occluded, or moves fast can be double-counted or missed. Upgrade path if
counts ever matter more: feed the tracker from the RTSP sub-stream (`/102`) at
~5 fps server-side; UI unchanged.

## API (app.py)

- `GET /api/detect/frame?name=<cam>` → latest annotated JPEG (404 until first
  frame). Same shape as the existing snapshot proxy.
- `GET /api/detect/counts` → `{"totals": {"truck": 31, ...}, "date": "2026-08-19"}`
  plus per-camera currently-visible counts.

## UI (static/index.html)

- Monitor snapshot tiles point their `<img>` at `/api/detect/frame?name=…`
  (fall back to the raw snapshot URL while detection has no frame yet).
- Fixed bottom-right counter widget on the Monitor tab, polled with the same
  tick as snapshots: one line per class, e.g. `trucks 31 · cars 12`.
- Live (go2rtc) mode is untouched — boxes are snapshots-only by design.

## Dependencies

`requirements.txt`: + `ultralytics`, `pillow`. Hailo runtime is installed on
the site servers only (system package, not pip), imported lazily so the Mac
never needs it.

## Error handling

- Detector import/model-load failure → Monitor falls back to plain snapshots;
  `/api/status` gains a `detect` field with the error string (mirrors the
  go2rtc "absent → hint" pattern).
- Inference slower than the tick (weak CPU) → the loop simply runs at
  whatever rate it manages; no queue, latest frame wins.

## Testing

`detect.py` self-check (`python detect.py`): feed the tracker synthetic box
sequences and assert counts (new vehicle counted once, parked vehicle not
recounted while tracked, midnight reset). Inference is mocked — no model
download in the check. Acceptance on hardware: boxes visible on both cams,
counter increments as trucks pass.
