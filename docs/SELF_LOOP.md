# The self-improving loop

Runs on the always-on training machine (the office MacBook Pro, `100.83.203.125`).

```
gate boxes mirror recordings ─► bucket ─► selfloop ingest (every 4 h)
        sample newest segments, champion model pre-labels ─► pending ─► bucket ─► curation tool
curators approve ─► bucket ─► selfloop train (checked hourly)
        ≥ 1,000 new frames outside the reference set ─► train.py ─► score on the reference
        ─► better than the champion? ─► champion.pt (pre-labels the next ingest) + models/ in the bucket
        then train_attrs.py on the same set ─► better mean val accuracy across the heads?
        ─► attrs-champion.pt (suggests attributes on the next ingest) + models/attrs/ in the bucket
bucket ─► selfloop classify (every 10 min)
        newest un-classified segments ─► the LIVE pipeline on footage time (champion +
        attrs champion + ByteTrack + the same counting) ─► one jsonl + crops per segment
        ─► fieldkit-events/<gate>/<YYYYMMDD>/ ─► the RDA importer
        + every track as a tracklet ─► fieldkit-tracklets/<gate>/<YYYYMMDD>/
        ─► journeys.py over the whole day ─► fieldkit-journeys/<gate>/<YYYYMMDD>/journeys.jsonl
```

- `python selfloop.py status` — where it stands; `loop-ingest.log`, `loop-train.log` — what it did.
- `./run_training.sh` — train now regardless of the threshold; same lock, same promotion.
- `python selfloop.py adopt <run>` — crown a run trained by hand; `adopt-attrs <run>` does
  the same for a `dataset/attr_runs/` run (the 2026-08-27 baseline was one).
- The attribute classifier is promoted on the mean of its per-head val accuracies, written
  by `train_attrs.py` to `report.json` next to `attrs.pt`. It is judged separately from the
  detector: a run that fails to train the heads leaves the detector's promotion standing.
- Ingest pre-fills `pending/suggest/<stem>.json` with the attrs champion's guesses, so
  curators correct attributes instead of typing them. A suggestion is never a record — the
  Label tab drops it the moment the sample is saved.
- `python selfloop.py classify` — vehicle events from the mirrored recordings, so a gate
  box that only records still reports. It runs the live detector, tracker and counting
  over the footage with both clocks driven by the segment's own name, so an event says
  when the vehicle passed, not when the pass ran, and re-running a segment rewrites the
  same keys instead of double-counting. Events land 10–20 minutes behind live; a quiet
  segment publishes an empty jsonl, which is what marks it done.
- Direction and `bound` on those events need `heading:` on the camera in `config.yaml`
  (see `config.example.yaml`). Without one the events are still complete, just undirected
  — a heading is never guessed from the footage.
Rarity-aware sampling: both passes capture one frame per camera per `CAPTURE_EVERY`
(10 s), which makes the queue mirror the traffic mix — cars and heavies pile up while
buses, plant and abnormal loads stay at a handful. So a frame holding a *wanted* class
is captured off the cadence instead. Wanted = fewer than `WANT_BOXES` (300) boxes in the
frames curated **since the reference freeze**, per `dataset/classes.txt`; the frozen
frames train nothing, so they say nothing about where the next run is thin. Each pass
prints the list it is hunting. Scene dedup and the pending cap still apply to wanted
classes, a wanted class is captured at most once per `RARE_EVERY` (5 s) per camera so a
rolling bus is a few samples rather than one a second, and rare frames use a 1 s id
bucket instead of the 10 s one so they cannot overwrite each other. The classify pass
carries the widest net — it decodes every mirrored segment, so it sees all 48 h of
footage rather than the slice a sampling pass lands on — and pushes what it captures,
attribute suggestions included, to the queue. It is slow, though — it tracks every
frame — so `python selfloop.py hunt` (every 10 min, `HUNT_PER_PASS` segments a pass)
sweeps the last `HUNT_HOURS` (48) of footage newest first at the ingest rate and keeps
*only* wanted-class frames: a two-day backlog clears in hours, and after that the newest
bus or plant frame in the queue is never more than a pass or two old. Only a class that
is under the floor gets hunted; once every class is past it the pass is a no-op.

- Change the cadence: edit `THRESHOLD` / `PER_CAM` / `CLASSIFY_PER_PASS` in `selfloop.py`, intervals in
  `install_loop.sh`, then `./install_loop.sh` again.

`detect.attr_classifier()` is the one loader for `attrs.pt` — live detection, the offline
classifier and ingest suggestions all go through it, and `selfloop.py`'s self-check loads a
checkpoint in the trainer's exact format so the two files cannot drift apart unnoticed.

What it deliberately does not do: touch a toll gate. `models/champion.pt` in the bucket
is the offer; deploying it to a site box (`detect_weights`, restart) stays a human step,
because a pre-labeller that got worse costs curators minutes and a gate detector that got
worse costs revenue.

## Journeys: one vehicle across a camera pair

Katuba's cam3 (faces south) and cam4 (faces north) are back to back on one road, so every
through vehicle passes both — and per-camera events count it twice, while a vehicle one
camera misses (cam3 at night, cam4 on big trucks at its near edge) is only counted if the
other's count line caught it. The classify pass therefore also writes every track, counted
or not, as a tracklet (path, class votes, surest crop per class, largest crop, receding
crop, plate crop), and `journeys.py` rebuilds the touched days into one line per vehicle:

- **Chains** — a camera's fragments of one vehicle are joined: the recount guard's
  `ghost_of`, then fragments that start where the last one ended (IoU) or where it was
  heading (same class only — a big vehicle occluding a small one is the false merge that
  rule exists to refuse), never two moving opposite ways.
- **Links** — a chain leaving one camera's handoff zone is paired with one entering the
  partner's, by zone-interval overlap and closeness to the measured ~0.5 s gap (a long
  truck fills both zones at once; at night cam3 locks on up to ~5 s late).
- **Counted** once if any chain crossed a count line, or crossed a handoff zone for at
  least a second. Class is voted across both cameras; `best` is the surest crop of that
  class; `front`/`rear` come from the camera the vehicle approaches / leaves.

Config — only cameras with `handoff` take part (Katuba, measured 2026-09-23 from tracks):

```yaml
cameras:
- {name: cam3, heading: south, handoff: {camera: cam4, zone: [0.80, 0.62, 0.20, 0.38]}}
- {name: cam4, heading: north, handoff: {camera: cam3, zone: [0.0, 0.55, 0.22, 0.30]}}
```

`zone` is `[x, y, w, h]` of the frame. `dataset/plate.pt` (optional: a one-class plate
YOLO, e.g. `morsetechlab/yolov11-license-plate-detection` `license-plate-finetune-v1s.pt`,
AGPL-3.0) adds plate crops. At 1080p a plate is ~20 px wide: the crops are evidence, not
readable, and about half are wheels or logos — a lane-facing ANPR camera is the real fix.

Measured on 2026-09-22 10:16 (10 min, daytime): 55 cross-camera links, every sampled pair
the same vehicle, 68 journeys against 26 + 45 per-camera events; night 00:46: 6 journeys
(5 linked) against 1 + 3 events. Known residuals, one each: a vehicle whose model class
flips between fragments of one camera (b-light/d-medium by day, d-medium/e-heavy at night)
is not re-joined when it is lost for longer than an overlap — two journeys. Tuning further
wants labelled pairs (the curation tool's Paired review). Evaluate any footage with
`python journeys.py build config.yaml out/*/tracklets/*.jsonl`.

Journeys are published **beside** events; the importer still reads `fieldkit-events/`.
Switching it is a prefix change there, once the counts are compared — but the day file is
rebuilt every pass (ids are deterministic, so unchanged journeys keep theirs), and crops are
cited by full bucket key rather than `crops/<name>`.

## Comparing models like for like

`python train.py baseline` trains on the reference set itself — the exact frames and hash
split v2 used — so two architectures can be compared on identical ground; `train.py eval
<run>` re-scores an old run on that split and on the reference tree; `train.py bench <run>
...` times each on CPU (what a gate box has) and MPS at the working image size. A baseline
run's reference score is inflated (it trained on those frames), which is why promotion is
not decided on it: a loop run continues from `dataset/champion.pt` (train.py `START`) on
the new curations only, and both it and the champion are then scored on that run's val
split (`train.py score dataset/champion.pt`) — new frames neither model trained on. The
run is crowned only if it beats the champion there.
`FIELDKIT_TRAIN_ARGS` passes a recipe through to ultralytics, e.g. the small-dataset one
from the YOLO26 training guide: `{"optimizer": "AdamW", "lr0": 0.001, "mosaic": 0.5}`.

