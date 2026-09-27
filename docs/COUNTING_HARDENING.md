# FieldKit counting hardening — handover

Katuba toll gate (`RDA-TG-KTB`), cameras `cam3` + `cam4`. Written 2026-09-27 as a handover to
Codex. Opus 5.5 reviews every PR that comes out of this work.

## 1. What success means (measured, not asserted)

| # | Metric | Definition | Target | Baseline (2026-09-25, a fully classified day) |
|---|---|---|---|---|
| R | **Recall** | Journeys counted ÷ true vehicles, from the ground-truth set (§3). Reported per stratum: day/night × peak/off-peak × class. | **≥ 95% in every stratum, at all hours** | Unmeasured. 11,372 journeys that day; the operator's August average is ~7,400/day (230k/month) |
| D | **Double counts** | Journeys that duplicate another journey of the same physical vehicle, as a share of journeys. From the ground-truth set plus a blind visual audit. | **≤ 1%** | Unmeasured. A partial audit on 09-23 found ~91% of cross-camera links correct, and that single-camera journeys often duplicate a neighbour |
| B | **Both-camera images** | Counted journeys with a crop from EACH camera (front/rear/best whose tracklet belongs to different cameras) ÷ counted journeys. Computable from `journeys.jsonl` alone. | **≥ 90%** | **57.3%** (6,518 of 11,372). 59.9% are linked (`camera == "cam3+cam4"`). Day 57.6% linked, night 64.5% |

R and D fight each other: every rule that merges more fragments lifts D-safety and risks R, and
vice versa. **A change is accepted only if it moves one metric without regressing another** on the
fixed evaluation set.

Split of 09-25 by evidence: `handoff` 6,812 (linked), `line` 2,545 (one camera, crossed its count line), `edge`
2,015 (one camera, zone crossing ≥ `MIN_EDGE_HITS`, never on a line). Single-camera journeys: cam3 2,419, cam4 2,141.
Working hypothesis (unverified): most of any over-count sits in `edge`, and most of the B gap is linkable
pairs that the handoff matcher misses. Test both; don't assume either.

## 2. The pipeline and where each piece lives

```
gate node zambia8 (records only, no detector)          office MacBook (M1 Max), selfloop.py under launchd
  recorder.py: ffmpeg -c copy, 600 s segments    -->     classify_pass(): oldest-first, last 48 h, prefetch
  R2 fieldkit-recordings/site1/cam{3,4}/                 detect.classify_segment(): YOLO + ByteTrack @ 5 fps, 1280
  YYYYMMDD-HHMMSS.mkv (local Lusaka time)                   -> fieldkit-events/<gate>/<day>/*.jsonl  (per camera, counted-on-line)
                                                            -> fieldkit-tracklets/<gate>/<day>/*.jsonl + crops/  (every track)
                                                         journeys_pass(): journeys.build(day's unfrozen tracklets)
                                                            -> fieldkit-journeys/<gate>/<day>/journeys.jsonl        (informational: frozen + provisional)
                                                            -> fieldkit-journeys/<gate>/<day>/final/<ts>.jsonl      (append-only; see §5)
                                                         coverage -> fieldkit-coverage/<gate>/<day>.json (recorded vs classified stems)
RDA dashboard-api importer (Go, every 5 min) reads final/*.jsonl -> public.vehicles source='fieldkit'
```

- `detect.py`: detection, tracking, per-camera counting (`COUNT_LINE`, `COUNT_AT_HITS`, `RECOUNT_GUARD`, `GUARD_IOU`),
  tracklets (`TRACKLET_CROP_HITS`, `PATH_EVERY`, `PATH_MAX`), crops (best/top/recede), plates (`PLATE_*`).
- `journeys.py`: the cross-camera count. Stitches fragments within one camera (`STITCH_GAP`, `STITCH_IOU`, `NEAR`,
  twin merge `TWIN_IOU`/`TWIN_SNAP`, motion guard `MOVE`/`SPAN`), then links cam3↔cam4 through handoff zones
  (`ZONE_SHARE`, `LEAD`, `LAG`, `MU`, `CLASS_PEN`). A lone chain counts if a member was line-counted, or if it crossed a
  zone with ≥ `MIN_EDGE_HITS`. Its self-check (`python3 journeys.py`) documents the cases (a)–(g).
- `selfloop.py`: orchestration, the per-day rebuild, `horizon`, coverage manifests. Self-check: `python3 selfloop.py`.
- Camera geometry: handoff zones in the office Mac's `config.yaml` — cam3 `[0.80, 0.62, 0.20, 0.38]` (bottom-right),
  cam4 `[0.0, 0.55, 0.22, 0.30]` (bottom-left). Northbound traffic: cam3 sees fronts, cam4 sees rears. There is a blind strip between
  them (measured gap 0.2–2.2 s). Camera clocks are synced hourly (a crontab on zambia8). The recording timeline is authoritative.
- `docs/CAMERA_PAIRING.md` covers pairing across cameras. The curation service's "Paired review" tool (`paired_review.py`) runs live
  but is NOT on `main` — it is uncommitted work in `/Users/admin/fieldkit`; ask before building on it.
- Background: `~/fieldkit-audit/review/design-final-full.md` (the full regime review, 2026-09-2x) and `~/fieldkit-audit/20260923/`
  (precision-audit sheets and crops).

## 3. Work package 0 — measure before you tune (required first)

There is no ground truth yet, and no change can be judged without it. Build it before touching the counting code:

1. **An evaluation set:** ≥ 24 paired 10-minute windows (cam3 + cam4, same start), stratified by day/night, peak/off-peak and
   weekday/weekend, taken from days that are already classified. Freeze the list in `eval/windows.json`.
2. **Ground truth per window:** every physical vehicle, with its entry time on each camera it appears in, its class, and whether it
   is visible in cam3, cam4 or both. Labelled by a person with the Paired review tool, or with a small purpose-built
   labeller. The user will arrange labelling; make the tool fast.
3. **`eval/score.py`:** runs detect + journeys (or reads the published journeys) on those windows, matches journeys to truth, and prints
   R, D and B per stratum. Deterministic. This command goes in every PR body, with its before/after output.
4. B also has a whole-day version that needs no labels: run it on every classified day and publish it beside the coverage manifest.
5. Operator check: the manager's daily totals, when they arrive, are a sanity bound, not the metric.

## 4. Known failure modes (evidence so far — each needs measuring)

- **Split journeys:** class flips between fragments (b-light/d-medium, d-medium/e-heavy at night) can turn one vehicle into two
  journeys (the class penalty only softens pairing). Singles often duplicate a neighbouring journey (from the 09-23 audit).
- **Missed links:** 40% of journeys are single-camera. Candidates: a vehicle that stops at the booth longer than `LAG`; zones that
  are too tight; late lock-on at night (4.6 s measured on cam3); ByteTrack id switches near the zone; trucks occluding each other.
- **Night:** glare blips (the `MIN_EDGE_HITS` guard). At night the edge share and the FieldKit/Lauretta ratio (5–14×) are both highest.
- **Midnight:** a vehicle crossing 00:00 splits in two (the neighbouring day's tracklets are not loaded).
- **Attributes:** only 61% of journeys carry `attrs` (type/axles), because attrs come only from line-counted events of the matching
  class. Filling them from the journey's best crop is a wanted follow-up.
- **Plates:** crops are ~20 px and unreadable, and about half are wheels or logos. They are evidence only; do not build on OCR.
- **Coverage:** classification must keep up (`selfloop.py status`). A partially classified day is not a count.

## 5. Contracts you must not break

- **The RDA importer** (`/Users/admin/rda` dashboard-api `internal/services/fieldkit_import.go`, being deployed now) reads:
  `id, ts, camera, class, letter, conf, bound, attrs, crops{best,front,rear,front_plate,rear_plate}, plates{front,rear},
  members[{id,camera,t0,t1}], gate`.
  - Crop values must be full bucket keys under `fieldkit-tracklets/`.
  - Journey ids must stay **deterministic** (sha256 of sorted member tracklet ids). The importer imports each id once.
- **`fieldkit-journeys/<gate>/<day>/final/*.jsonl`** is the importer's no-double-count guarantee, not `journeys.jsonl`. Each batch is
  immutable and append-only (a new timestamped file per pass that freezes anything, never a rewrite of an old one); a tracklet belongs
  to at most one final journey, ever. `journeys_pass()` only builds provisional journeys from tracklets NOT already claimed by a final
  batch, so re-linking one later (once its partner camera catches up) can never re-import it. A provisional journey freezes once
  `max(member t1) + FINAL_MARGIN (300 s) ≤ horizon()` for every camera the gate owns, not just the ones it happens to touch — an
  as-yet-uncaught-up partner could still hand a lone chain a new link. `journeys.jsonl` (frozen + provisional) is informational only —
  status/eval, never the importer.
- **Changing ids of journeys that were already imported** creates duplicates in RDA. That happens if you change the member set of a
  final journey, the id scheme, or the tracklet ids of a day whose final batches have already been imported. The importer logs
  `FieldKit journeys changed id after import`. A deliberate re-count of past days needs a coordinated RDA-side replace (ask; don't
  improvise).

## 6. Rules

- **Branches and reviews:** branch per work package off `main` in a worktree. Keep PRs small, with a measured before/after (`eval/score.py`) and the self-checks
  (`python3 journeys.py`, `python3 selfloop.py`, `python3 detect.py`, `for t in test_*.py; do python3 $t; done` — plain scripts, no pytest) in the body.
  Opus 5.5 reviews each PR; address the findings before merge.
- **Keep the invariants:** `CLAUDE.md` / `FIELDKIT_SPEC.md`. Never hardcode or print credentials. Camera creds and R2 keys come from `config.yaml` or
  the env. An RTSP URL contains a password.
- **Machines and deploys:**
  - **Never touch Lauretta on zambia8** (`zambia-rda.service`, `~/lauretta`, `/dev/hailo*`), and don't restart gate services.
    There is no sudo.
  - **The office Mac** runs a working-tree swap of `~/fieldkit`. Deploying there, and any production DB or R2 write outside
    FieldKit's own prefixes, needs the user's explicit OK.
  - **`/Users/admin/fieldkit`** on this Mac holds other people's uncommitted work. Work in your own worktree.
- **Data access:** `import dataset_sync as ds; o = ds.creds(); cl = ds.client(o)` — bucket `fieldkit-recordings`. Recordings go through a key listing
  (`selfloop.recording_keys`); never list the whole bucket casually (it holds ~700k objects).
- **Compute:** the GPU on the office Mac is shared with the live classify pass. Batch experiments must not starve it (see `selfloop.Lock`).
