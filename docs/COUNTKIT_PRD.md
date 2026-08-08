# CountKit — Product Requirements (development)
## Junction analysis & reporting on the Jetson — companion to FieldKit
### Companions: `COUNTKIT_DESIGN_PROMPT.md` (UI design), `CONGESTION_FUSION_RESEARCH.md` (probe-data licensing/provider research). This PRD is the build-facing source of truth.

## What this is
A local-first web app that runs ON the Orin Nano (and later the RTX 5080 rig,
unchanged) and turns verified footage into the client deliverable:
per-approach, per-class vehicle counts in 15-minute bins, plus
turning-movement matrices, exported as Excel + PDF — with per-vehicle
evidence imagery making every count auditable.

Platform: Python/FastAPI + one static HTML page, same stack and conventions
as FieldKit. Runs at :8090 so both apps coexist. **Lives in its own sibling
repo** (not in fieldkit/) — it shares conventions and the ingest tree, not
code paths.

## Inputs
- The verified ingest tree produced by nvr_pull:
  `/data/ingest/<date>/<site>/<cam>/*.mkv` + manifest.json (`.verified` gate)
- Per-camera calibration files produced by the Label tab
- Per-camera time offsets: **extend the manifest schema with `time_offset_s`**
  (float, seconds to ADD to file timestamps to get true Africa/Lusaka time).
  The offset is entered in CountKit's UI and written into the existing
  manifest.json so it travels with the footage. CountKit must refuse to run
  a camera whose offset is unset rather than assume zero — unset ≠ zero.
- Optionally, a licensed probe dataset per site (see Probe fusion).

## Sites are multi-camera (core requirement)
Every site has 2–4 cameras (2–3 today, 4 later). No single camera sees the
whole junction; vehicles routinely enter in one camera's view and exit in
another's. Consequences:

- **Arm ownership:** each arm's ENTRY line and EXIT line is owned by exactly
  one camera per site, even where views overlap — otherwise vehicles double-
  count. The Label tab derives a site-level arm map from the per-camera
  calibrations and flags unowned arms (amber) and double-owned arms (red).
- **Two-tier movement pairing** (site-wide, at aggregation time — the
  per-camera pipelines are unchanged):
  - **Tier 1 (confident):** same tracker obj_id crosses an entry line and an
    exit line in one camera, within the 120 s window.
  - **Tier 2 (inferred):** leftover entries matched to leftover exits across
    cameras by vehicle class + transit-time window on clock-corrected
    timestamps, greedy, arm-topology-gated. NO visual re-identification.
  - Every movement records its tier; inferred counts are reported separately,
    never silently blended. Remaining unmatched events are the QA residual.
- **QA:** pairing rate splits into same-camera and inferred shares; combined
  <85% or inferred >~30% flags the site.

## The four tabs

### 1) Label (build first)
- Pick site+camera → reference frame (first frame of newest MKV, or uploaded
  photo). Draw ENTRY/EXIT polylines with arm name, compass tag, and a
  direction normal (which side counts as crossing "in").
- Calibration JSON per site+camera:
  `{ "image_size": [w,h], "lines": [ {"kind":"entry","name":"...","points":[[x,y],...],"dir":"AB"} ] }`
- Calibrations are versioned — never overwritten (new file + active pointer);
  a camera re-aim mid-wave needs a new calibration from that day on.
- Site arm map (see above) rendered from all cameras' active calibrations.

### 2) Analyze (pipeline runner)
- Select site-days (only `.verified` offered; any camera with an unset
  time offset BLOCKS the site-day with a clear message) → queue jobs.
- Per camera: filesrc(MKVs in order) → nvinfer (config path from settings;
  TrafficCamNet ONNX now, fine-tuned RT-DETR later — the model is CONFIG,
  not code) → nvdcf tracker → pyds probe that:
  - applies the camera's calibration lines geometrically (line-crossing from
    tracker bbox centroids — the probe owns geometry so calibrations
    hot-load; do NOT depend on nvdsanalytics config generation);
  - records events `(site, cam, obj_id, cls, line, kind, corrected_ts)`;
  - writes the evidence crop for each crossing event (see Evidence crops).
- Events land in SQLite (one DB per wave). Idempotent per site-day (re-run
  replaces events AND crops).
- Progress via SSE; jobs survive page reloads; one job at a time with a
  queue (the Orin is small).
- Build order note: stub nvinfer with a mock detector first so geometry,
  pairing, and crops are testable off-Jetson; the real DeepStream runner is
  written alongside it behind the same interface and wired on the Orin.

### 3) Counts (aggregation & QA — strictly read-only)
- Approach volumes: entry-line crossings per class per 15-min bin
  (clock-quarter bins on corrected_ts, Africa/Lusaka).
- Turning movements: two-tier pairing (above) → movement entry→exit per
  class per bin; O-D matrix (entry arms × exit arms, diagonal U-turns) per
  period, with per-cell inferred share.
- Class mapping table in settings (model label → report class) so the
  pretrained taxonomy maps now and the fine-tuned one later without code
  changes. PCU conversion factors per class, togglable.
- QA: per-bin sparklines, pairing-rate split, coverage gaps (missing
  segments vs recording window — a gap must never render like a zero),
  unset offsets, probe sample-size chip, flagged pairs count.
- **Verification drawer** (evidence crops): view-only; flags only; see below.

### 4) Report (the deliverable)
- Per site: Excel workbook (openpyxl) — metadata sheet (site, dates, camera
  positions, method statement incl. probe provider + license line when
  used); per-day 15-min classified counts per approach; turning sheet
  (movement matrix per peak period per class, inferred share footnoted);
  summary (peak-hour volumes, PHF, daily totals).
- PDF summary: one page per site — junction arrow diagram (left-hand
  traffic arrow order), headline volumes, peak hour, movement matrix.
- Optional corroboration page (probe fusion) and optional verification-
  sample appendix (crops) — both off by default.
- Export bundle: `/data/reports/<wave>/<site>/` with manifest + sha256s
  (same custody discipline as ingest). Branding from config
  (company_name/logo_path).

## Evidence crops (verification imagery)
Purpose: make every count auditable, and make tier-2 inferred pairs humanly
verifiable (entry crop from cam A beside exit crop from cam B).

- **Capture:** in the probe, at each line-crossing event, crop the frame to
  the tracker bbox, scale to ~224 px wide, JPEG q≈70 → **10–25 KB**. Encode
  and write on an async writer thread that can never stall inference; a
  failed write degrades to "crop missing", never fails the event.
- **Storage, local-first:** crops land on the Jetson under the wave's data
  dir, keyed `<site>/<date>/<cam>/<obj_id>-<line>-<ts>.jpg`; the event row
  stores the key. Budget ≈1.5 GB per busy site-day.
- **Offload:** background upload to Cloudflare R2 using FieldKit's verified
  pattern (sha256 checksum on PUT; local delete only after verified upload
  AND only under disk pressure). Served through the R2-backed CDN (custom
  domain). Credentials in config.yaml only. UI reads local first, CDN
  fallback (provenance shown).
- **Cross-camera semantics (set expectations):** "all images of a vehicle"
  means the crops of the events the pairing logic linked — there is no
  global re-ID. For an inferred pair the crops ARE the audit evidence.
  Human-confirmed pairs (flags aside) are future labeled re-ID training
  data; that upgrade is out of scope v1.
- **Verification is view-only.** Reviewers may flag a pair (stored in QA
  notes, surfaced next to pairing rate); flags never mutate counts. If
  flags reveal systemic mis-pairing, the remedy is fixing gates/offsets and
  re-running (idempotent) — never hand-editing.
- **Privacy:** crops contain readable plates (Zambia Data Protection Act
  2021 applies). Default posture: internal QA only, retention window
  configurable per wave; client-facing export of crops is an explicit
  opt-in; plate blur is a later option.

## Probe fusion (v1 = corroboration only — see research doc)
- Provider: **TomTom** (storable license, Zambia coverage). Google is never
  stored — at most a live, attributed, unstored sanity glance. HERE is the
  enterprise fallback.
- Data arrives as an ingested dataset (Traffic Stats export or the
  standalone Stage-0 poller service — NOT part of CountKit), joined on
  `(site, approach, 15-min bin)`: delay, speed vs free-flow, sample size,
  confidence.
- Every probe-derived figure displays its sample size; probe series render
  as clearly secondary; provider attribution wherever shown.
- Out of scope v1: LOS computation (stage 2), volume-from-probe calibration
  or any derivative product (stage 3 — requires a written derivative
  license), live polling UI.

## Non-goals v1
No model training, no annotation UI, no multi-user/auth, no live
processing (verified files only), **no hand-editing of counts** (QA flags
instead — hand-edits destroy the warranty argument), no visual re-ID, no
Google-data storage.

## Acceptance
1. Label a 2-camera site from reference frames in <15 min; calibration JSONs
   versioned; arm map correctly flags an unowned and a double-owned arm.
2. Analyze one verified site-day on the Orin end-to-end; events + crops in
   place; re-run is idempotent (events and crops replaced).
3. A camera with China-time offset (−21 600 s) bins correctly against a
   synced camera at the same junction (fixtures).
4. Two-camera fixture site: tier-1 pairs from same-camera IDs, tier-2 pairs
   recovered across cameras with correct movement matrix; unmatched residual
   reported; inferred share correct.
5. Counts shows pairing-rate split and flags an artificial coverage gap;
   verification drawer shows side-by-side crops for both tiers; flagging a
   pair changes QA output, never a count.
6. Unset time offset blocks analysis with a clear message.
7. Report opens in Excel with correct bins, movement matrix with inferred
   share footnote; PDF renders with left-hand-traffic arrow diagram; export
   bundle manifest + sha256s verify.
8. Crop offload: verified R2 upload, local delete only after verification
   and under disk pressure; UI falls back to CDN and labels provenance.
9. With a probe dataset configured: overlay renders with sample sizes and
   attribution; corroboration page exports; with none configured, no probe
   UI appears anywhere.
10. Same code runs on the RTX 5080 rig by changing only the nvinfer config.
