# Video-rate detection feed + labeling page — design

2026-08-19 · follows the Monitor detection overlay (PR #9). Mac-first: nothing
Hailo-specific is built now; the `detect_backend` seam stays for the site
deploy.

## Part A — sub-stream feed with ByteTrack

detect.py's worker stops polling snapshots and consumes the RTSP **sub-stream**
(`/Streaming/Channels/102`) at ~5 fps per camera:

- One **ffmpeg reader subprocess per camera** (ffmpeg is already a FieldKit
  requirement — no new dependency): `-rtsp_transport tcp -i <sub url>
  -vf fps=5 -f image2pipe -c:v mjpeg -q:v 3 -`. A reader thread splits stdout
  on JPEG SOI/EOI markers and keeps only the latest frame (no queue).
  Reader death → restart with backoff, recorder-style. Sub-stream only — the
  main stream stays exclusively the recorder's (hard constraint).
- The inference loop round-robins cameras over the latest frames using
  **ultralytics `model.track(persist=True, tracker="bytetrack.yaml")`** —
  one YOLO instance per camera (tracker state is per-model). Verified on real
  footage: persistent IDs, ~370 ms/frame worst case on MPS incl. warmup.
  Device: `mps` when available, else CPU; loop runs flat out, effective fps
  adapts.
- **Counting by track ID**: an ID counts once when seen COUNT_AT_HITS times;
  per-ID majority class vote with tally reassignment (unchanged semantics from
  the occlusion fix, now keyed by ByteTrack ID instead of greedy IoU).
- Snapshot polling remains only as the `/api/detect/frame` fallback when a
  camera's reader is down.
- Detector public API (frame/counts/info/set_cameras/start/stop) is unchanged;
  `__init__` grows `creds_fn` (ip → (user, password), app passes the cam_creds
  chain) for building sub-stream URLs, and `dataset_dir` for Part B.

Camera-side: sub-streams raised to their max 1280×720 via ISAPI (one-time ops
step, reversible) — 360p sub-frames would gut small-vehicle recall; at 720p,
`imgsz=1280` is ~native. Live view sharpens as a side effect.

## Part B — labeling page → fine-tuning dataset

- **Capture** (in detect.py): every 30 s per camera, when the frame has
  detections, save the raw sub-stream frame + labels to
  `dataset/pending/images/<cam>-<utc ts>.jpg` and
  `dataset/pending/labels/<same>.txt` (YOLO txt: `cls cx cy w h` normalized;
  class ids 0=car 1=motorcycle 2=bus 3=truck, written once to
  `dataset/classes.txt`). Cap: skip capture when 500 pending samples exist.
- **Review API** (app.py): `GET /api/dataset/samples` (pending ids + per-box
  labels), `GET /api/dataset/image?id=`, `POST /api/dataset/label`
  `{id, boxes, action: "approve"|"discard"}` — approve rewrites the label file
  and moves image+labels to `dataset/approved/...`; discard deletes both.
  Path safety: ids are validated basenames, never paths.
- **UI**: new **Label** tab in index.html. Pending list → image with box
  overlays (scaled divs over the `<img>`); tapping a box cycles its class
  (car → motorcycle → bus → truck → ✕ remove); Approve / Discard. Reclassify
  and remove only — drawing new boxes for missed vehicles is deferred.
- `dataset/` is gitignored. Fine-tuning itself (yolo train on the approved
  set) is a later manual/scripted step, not part of this build.

## Out of scope now (deferred)

Hailo backend (needs its own tracker — TAPPAS hailotracker or standalone
ByteTrack), vehicle-type classes beyond COCO (MIO-TCD fine-tune uses the
dataset this builds), drawing missed boxes, per-camera fps tuning.

## Testing

detect.py self-check keeps passing with tracking mocked (ID-based counting
exercised with synthetic ID sequences). Real check: run against the live
cameras, confirm ids persist, counts accrue, samples land in dataset/pending,
and a label round-trips through the API.
