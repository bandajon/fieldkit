# CountKit — UI design prompt
## Feed this document to Claude Design (claude.ai/design). It is self-contained: everything needed to design the app is in here.

---

## 1. What you are designing

**CountKit** is a local-first web console that turns verified traffic-camera
footage into a traffic-engineering deliverable: per-approach, per-class vehicle
counts in 15-minute bins, plus turning-movement matrices, exported as Excel and
PDF. It runs on an NVIDIA Jetson Orin Nano at `http://<host>:8090` and is the
companion to **FieldKit** (the capture console at `:8080`) — same operator, same
design family, next step of the same workflow: FieldKit records the junction,
CountKit counts it.

**Deliver:** a complete visual design for one single-page app with four tabs —
**Label | Analyze | Counts | Report** — plus a persistent status rail. Design
every screen state listed per tab, at two breakpoints: phone portrait (~390px)
and laptop (~1280px).

### Users & context
- One or two operators from a small traffic-survey firm (Dexterity, Zambia).
  Technically capable field engineers, not designers or data scientists.
- **A site is 2–4 cameras covering one junction** (2–3 today, 4 later). No
  single camera sees everything: a vehicle routinely ENTERS in one camera's
  view and EXITS in another's. The design must treat the SITE, not the
  camera, as the unit of counting — per-camera screens exist only inside
  Label and Analyze.
- Used **at base / in an office**, on a laptop or phone browsing to the Jetson —
  *not* at the junction in sunlight (that's FieldKit's job). Sessions are long:
  labeling takes minutes of precision work, analysis jobs run for hours.
- The output goes to a paying client (a junction study). Credibility and
  auditability matter more than flash: every number must be traceable, QA
  problems must be loud, and nothing may look hand-editable.
- **Zambia drives on the left.** The left turn is the near-side (easy) turn.
  Do not mirror US templates; junction diagrams must draw lane/arrow order for
  left-hand traffic. Timezone is Africa/Lusaka (UTC+2, no DST).

### Hard engineering constraints (shape the design; do not fight them)
- One static HTML page, vanilla JS, no build step, no component framework.
  Design must be achievable with hand-written CSS and small JS.
- Server-rendered data over simple JSON polling + SSE streams. No websockets-
  heavy live collaboration, no drag-and-drop libraries.
- One analysis job runs at a time (the Jetson is small); jobs take hours and
  must survive the phone sleeping or the page reloading.
- No auth, no user accounts. No hand-editing of counts anywhere — QA flags
  instead (hand-edits destroy the deliverable's warranty argument).

---

## 2. Design language (inherit from FieldKit — this is the family identity)

CountKit must look like FieldKit's sibling. FieldKit's system, extracted:

**Palette (dark, utilitarian):**
- Background `#111417`; panels/cards `#191d21`; log/code wells `#0b0e10`
- Text `#e8eaec`; dimmed text `#8a939b`; hairline borders `rgba(255,255,255,.14)`
- State colors — **used ONLY for state, never decoration**: ok-green `#4a9e6f`,
  bad-red `#c25454`, warn-amber `#c29a4a`
- `color-scheme: dark` (native inputs, pickers, scrollbars follow)
- CountKit is used indoors, so the dark family theme is correct here — do not
  switch to a light theme for "outdoor legibility"; that constraint belongs to
  FieldKit, not CountKit. Keep text contrast ≥ 7:1 for data, ≥ 4.5:1 for
  secondary text anyway.

**CountKit's one permitted extension:** data visualization needs more than
state colors. Add a small **categorical ramp for vehicle classes** and
**per-approach accent hues** used exclusively inside charts, sparklines, and
the junction diagram — never on buttons, chrome, or backgrounds. Derive them
muted/desaturated so they sit in the dark UI (think 60–70% saturation), and
always pair color with a text label; color is never the only channel.

**Typography:**
- Prose: `"Avenir Next", Inter, system-ui, sans-serif`, 16px/1.45 base
- **All machine data is monospace with tabular numerals** — counts, bins,
  timestamps, filenames, camera names, log lines. Columns of numbers must align.
- Section eyebrows: 11px mono, uppercase, letter-spaced, dimmed
- Big numerals for headline QA figures (pairing rate, totals) — at-a-glance

**Components & idioms (reuse, don't reinvent):**
- **State = an 8px colored dot beside an uppercase mono label** (`● VERIFIED`,
  `● RUNNING`, `● FAILED`). No filled pills, no badges, no toasts.
- Buttons: ghost style, 1px hairline border, 4px radius, **min 48px tall**;
  primary action inverts (near-white fill, dark text); destructive gets red
  border + red text. Focus rings visible (2px outline).
- Cards: panel background, hairline border, 4px radius, 16px padding.
- Inline result lines (`.res` idiom): outcome text appears *under the control
  that caused it*, tinted good/warn/bad — not in modals or toasts.
- Tab bar: full-width buttons, 52px tall, uppercase, underline for active tab;
  wraps to 2×2 on phones.
- Sticky **status rail** on every tab: tiny uppercase labels over mono values.
  CountKit's rail: `host · ingest disk free · GPU (temp/util or "idle") ·
  active job (or "—") · clock (Africa/Lusaka)`.
- Log panes: near-black well, 13px mono, dim text, auto-scroll that pauses
  when the user scrolls up.
- Native `confirm()` for the rare destructive action; no custom modal system.
- `prefers-reduced-motion` honored; no decorative animation anywhere.

---

## 3. Domain conventions the design must respect

These are traffic-engineering norms; engineers will judge the tool by them.

- **Approaches** are named by travel direction (NB/SB/EB/WB) *and* road name.
  Every labeled arm carries both: compass tag + free text ("Great East Rd (EB)").
  Non-orthogonal junctions fall back to arm names alone.
- **Movements** are entry→exit pairs. The canonical visuals are (a) the
  **origin–destination matrix** — rows entry arms, columns exit arms, diagonal
  U-turns — and (b) the **classic junction arrow diagram**: plan view, north
  arrow, four arms; on each approach three side-by-side arrows (left-hook,
  straight, right-hook — left arrow nearest the kerb for left-hand traffic),
  each carrying its peak-hour count, often as "total (heavy)" e.g. `412 (37)`;
  an info box with junction name, date, peak interval, total entering volume,
  PHF, % heavy.
- **Bins** are 15 minutes, aligned to clock quarters (:00/:15/:30/:45), local
  Africa/Lusaka time.
- **Peak hour** = highest four *consecutive* bins (may start at :15/:30/:45).
  **PHF** = hourly volume ÷ (4 × busiest 15-min), shown to 2 decimals. AM and
  PM peaks reported separately.
- **Vehicle classes** are simple visual classes, configurable (typical set:
  car, minibus, bus, light truck, heavy truck, motorcycle, bicycle) — never a
  fixed hardcoded list. A **PCU (passenger-car-unit) toggle** converts counts
  using per-class factors.
- **Movements are paired site-wide, in two tiers.** Tier 1 (confident): the
  same tracker ID crosses an entry line and an exit line in ONE camera. Tier 2
  (inferred): a leftover entry in one camera is matched to a leftover exit in
  another by vehicle class + transit-time window on clock-corrected
  timestamps — no visual re-identification. Every movement carries its tier;
  inferred counts are never silently blended with confident ones.
- **Arm ownership**: each arm's entry line and exit line is owned by exactly
  ONE camera at the site, even where views overlap — otherwise one physical
  vehicle is counted twice. Ownership is a site-level property the UI must
  make visible and enforceable.
- **QA metrics are first-class citizens**: pairing rate split into
  same-camera and cross-camera-inferred shares (combined below 85%, or
  inferred share above ~30%, flags the site), coverage gaps (missing footage
  vs the survey window), and unset clock offsets — which multi-camera pairing
  makes even more critical, since a wrong offset silently mis-pairs vehicles
  across cameras. These must be prominent, not buried.
- **Probe congestion data (fusion — v1 is corroboration only).** Each site can
  optionally carry a licensed floating-car dataset (TomTom-shaped; provider
  and dataset path set in settings, key in config — never in the UI) with,
  per approach per 15-min bin: delay vs free-flow, speed, **probe sample
  size, and confidence**. Three iron rules the design must enforce visually:
  (1) probe data is CORROBORATION, never ground truth — it always renders as
  a clearly secondary series against measured counts, never merged into
  them; (2) **every probe-derived figure carries its sample size** — Lusaka
  probe penetration is unquantified, and a delay curve built on 3 vehicles
  must look exactly as thin as it is; (3) provider attribution is part of the
  chrome wherever probe data appears. Google data is display-only if ever
  shown and is never stored — the design must not assume a Google layer.

---

## 4. The four tabs — screens and states

### Tab 1 — LABEL (calibration editor; precision work, the tool's craft moment)

Purpose: draw ENTRY and EXIT gate lines over a camera's reference frame so the
analysis pipeline knows where each approach is.

**Layout:** site + camera picker (dropdowns, mono) → large reference-frame
canvas (first frame of newest footage, or uploaded photo) → gate list panel.
On a laptop: canvas left (~70%), gate side-panel right. On a phone: canvas
full-width, gate panel below, drawing controls bottom-anchored in thumb reach.

**Drawing interaction (design the affordances for this exact model):**
- Tap/click adds a vertex; a live "rubber band" segment follows the pointer.
- Explicit on-screen **Done** button finishes the line (double-click also
  works on desktop; Enter too). **Undo point** button removes the last vertex;
  Esc cancels. No hidden gestures — every action has a visible button.
- One-finger tap draws; two-finger pinch/pan zooms the frame (precision on a
  1080p frame demands zoom); also an explicit Draw/Pan mode toggle.
- After finishing: tapping a gate selects it — vertices become drag handles
  (rendered ~24px, hit area ≥44px), segments show midpoint "+" to insert a
  vertex.
- **Every gate renders a direction indicator**: chevrons perpendicular to the
  line showing which crossing direction counts as "in", with a **Flip
  direction** button on the selected gate. This is the single most
  error-prone concept — make it unmistakable.
- Gates are color-coded by kind — entry vs exit (two distinct hues from the
  chart palette) — each with a name chip pinned at its midpoint.

**Gate properties panel (for the selected gate):** kind (ENTRY/EXIT), arm name
free text, compass tag (N/S/E/W or none), direction (shown + flip). Delete
gate (with confirm).

**Versioning is visible:** calibrations are never overwritten. Header shows
`calibration v3 · active · saved 2026-08-07 14:12` with a version dropdown to
view (read-only) older versions and a **Save as new version** primary action.
A camera re-aimed mid-survey gets a new version from that day forward — the
UI copy should say so.

**Site arm map (the multi-camera keystone — design this prominently):** above
or beside the per-camera canvas, a compact site-level panel listing every arm
of the junction with its ENTRY owner and EXIT owner camera (e.g. "Great East
Rd (E) — entry: cam1 · exit: cam2"). It is assembled automatically from the
per-camera calibrations and judges the site as a whole:
- an arm with no entry owner or no exit owner → amber "arm unowned — its
  movements can't be counted";
- an arm owned by TWO cameras → red "double-owned — vehicles here count
  twice; remove one line";
- a camera whose calibration has entry lines but no exit lines → amber note
  "volumes only from this view — exits live on other cameras" (legitimate in
  multi-camera sites, but said out loud).
The map is the operator's answer to "is this junction fully covered?" before
any analysis runs.

**States to design:** empty (no footage yet for this camera → prompt to upload
a reference photo); frame loaded, no gates; drawing in progress; gate
selected/editing; saved confirmation (inline `.res` line, not a toast); arm
map healthy / unowned-arm / double-owned-arm.

### Tab 2 — ANALYZE (job queue; long-running, glanceable)

Purpose: queue verified site-days for the detection/tracking pipeline; watch
progress; recover from failures.

**Top:** a browser of available site-days from the ingest tree. Only
`.verified` site-days are offered; each row: date, site, cameras, footage
duration, and its state dot: `● VERIFIED` (queueable), `● ANALYZED` (re-run
replaces), `● BLOCKED` red when any camera's clock offset is unset — with the
message *"cam3: time offset unset — set it in Counts → Offsets. Unset is not
zero."* A blocked row's queue button is disabled. This blocking state is a
hard product rule; design it loud.

**Queue (one job runs at a time):** per-job **cards**, newest at bottom:
- Job name (site · date), state (`● QUEUED #2` / `● RUNNING` / `● DONE` /
  `● FAILED` / `● CANCELLED`).
- Running card: progress bar + concrete units in mono —
  `frame 41 200 / 108 000 · 2.3× realtime · ~18 min left` — current phase
  text ("inference · cam2"), elapsed time, Cancel button.
- Failed card: last ~5 log lines inline (mono, dim), **Retry** button,
  attempt count.
- Done card: totals summary (events recorded) + **Review counts →** link that
  jumps to the Counts tab pre-filtered to that site-day.
- Collapsible full log pane per job (the FieldKit log-well idiom), SSE-fed,
  auto-scroll with pause-on-scroll-up.
- A thin **connection indicator** distinguishing "job failed" from "page lost
  contact with the Jetson" (field networks drop; the job itself survives).

**States:** empty queue; queued-behind ("#2 in queue"); running; failed;
done; page-reloaded-mid-job (everything restores from server state — design
nothing that only exists client-side).

### Tab 3 — COUNTS (aggregation review & QA; the trust-building screen)

Purpose: let the engineer verify the numbers before exporting. Read-only —
there is deliberately no way to edit a count.

**Selector row:** site · date · camera/approach · class filter · **PCU toggle**.

**Main views (design as switchable sub-views or stacked sections):**
1. **Bin table** — rows = 15-min bins (mono timestamps), columns = movements
   grouped by approach; right column bin total; footer rows: hourly totals,
   peak hour highlight, PHF per approach. The AM/PM **peak-hour rows get a
   subtle emphasis** (border or background lift, not color fill). Missing
   coverage renders as **hatched/struck cells**, not zeros — a zero and a gap
   must never look alike.
2. **Per-bin sparklines** — one small multiple per approach, aligned on a
   shared time axis, class-colored stacks or lines; gaps visibly broken, not
   interpolated. When the site has a probe dataset, each approach gains a
   **probe delay overlay**: a secondary line in a reserved neutral treatment
   (thin, dashed or muted — unmistakably not a counted series), broken where
   the provider has no data, with the bin's sample size on hover/tap. The
   overlay has a visible toggle and a provider attribution line.
3. **O-D matrix** — entry arms × exit arms, counts per cell, diagonal =
   U-turns, per-period (AM peak / PM peak / full day) switch. Cells whose
   count includes cross-camera-inferred pairs show the inferred share subtly
   but legibly (e.g. `412 ·6% inf` or a corner mark with the split on
   hover/tap) — a client-facing number must never hide its confidence tier.

**QA panel (always visible, top or side):** big numerals —
- **Pairing rate for the site** (e.g. `91.4%`), state-dotted (ok ≥85%), split
  into its two tiers right in the numeral block: `84.1% same-camera ·
  7.3% inferred` — inferred above ~30% gets its own warn state with copy
  "heavy cross-camera inference — check arm ownership and offsets". Per-camera
  breakdown one tap away.
- **Coverage**: `11.5 h of 12 h` with a thin timeline strip showing gaps.
- **Offsets**: per-camera clock offset chips (`cam1 +0 s · cam3 −21 600 s`),
  red `● UNSET` for missing ones, each opening the offset editor.
- **Probe** (only when a dataset is configured): median sample size per bin
  and % of survey bins with probe data (`n̄ 14/bin · 92% bins`) — warn state
  when off-peak bins run single-digit, with copy "thin probe sample —
  corroboration only, per research memo".

**Offset editor (small inline panel or section, reachable from here and from
Analyze's blocked message):** per camera: current offset in seconds (mono
input), copy explaining *"seconds ADDED to file timestamps to get true
Africa/Lusaka time — a camera left on China time needs −21 600"*, Save writes
it into the footage manifest. Unset shows red; zero must be an explicit entry.

### Tab 4 — REPORT (the deliverable; hand-off confidence)

Purpose: assemble and export the client deliverable per site.

- **Per-site export card:** site name, date range covered, cameras, QA
  roll-up (pairing rates, coverage) restated next to the export button —
  the engineer confirms health at the moment of export. Branding line
  (company name + logo from config) previewed.
- **Excel** and **PDF** buttons (primary actions), plus a "bundle" line
  showing the output path and a `● SHA256 MANIFEST` custody note — exports
  are hashed and manifested exactly like the ingest footage; the design
  should make that custody discipline visible (a small mono line, not a
  ceremony).
- **Corroboration sheet/page (only when probe data exists):** per approach,
  measured volume bins (bars) against the provider delay curve (secondary
  line), with a mandatory footer block: provider attribution, license line,
  and per-approach probe sample-size summary. Copy frames it as
  corroboration of survey timing/peaks — never as a second count. This is
  the one place probe data reaches the client deliverable in v1.
- **PDF page preview:** one page per site — header with junction name/dates/
  branding, the **junction arrow diagram** (spec in §3: north arrow, four
  arms, three count-carrying arrows per approach in left-hand-traffic order,
  info box with peak interval, total entering, PHF, % heavy), a peak-hour
  movement matrix table, headline volumes. Design this page as a monochrome-
  first line drawing that also works printed — approach accent hues optional
  on screen, never required to read it.
- **States:** ready; export running (progress line); done (paths + hashes,
  `open folder`-style mono paths); site not exportable (QA blockers listed —
  e.g. unset offset, unanalyzed days — with links to fix).

---

## 5. What good looks like

- An engineer opens Counts and knows in five seconds whether the data is
  trustworthy — pairing rate (and how much of it is inferred), coverage,
  offsets, before any numbers.
- The Label tab's arm map answers "is this 3-camera junction fully covered,
  with nothing counted twice?" at a glance, before hours of analysis run.
- A phone that slept through a 3-hour analysis job shows the truth two
  seconds after waking.
- The Label tab feels like a precision instrument: zoomed drawing is easy,
  direction chevrons make "which way counts" impossible to get wrong.
- The PDF page could be handed to a municipal client tomorrow.
- Someone who has used FieldKit needs zero minutes to learn the chrome —
  same rail, same tabs, same dots, same buttons.

## 6. Out of scope
No auth, no multi-user, no live video, no model-training UI, no count
editing, no notifications/toasts/modals beyond native `confirm()`, no light
theme, no icon library (text labels + the state-dot system carry meaning).
Probe-data boundaries (per the licensing research): no LOS computation UI
(stage 2), no volume-from-probe extrapolation or calibration screens
(stage 3 — needs a negotiated derivative license), no live API polling UI
(probe data arrives as an ingested licensed dataset), and no Google traffic
layer anywhere data is stored.

## 7. Addendum — evidence crops (verification imagery)

Every line-crossing event may carry a small **evidence crop** — the vehicle's
bounding box at the moment of crossing, ~10–25 KB JPEG. Crops exist to make
counts *auditable*, especially cross-camera inferred pairs. Design rules:

- **Verification drawer (Counts).** Tapping any O-D cell, bin count, or the
  inferred-share figure opens a drawer of sampled movements, each showing its
  crops **side by side**: entry + exit for a same-camera pair; entry from
  camera A + exit from camera B for an inferred pair — the human check for
  "is this the same vehicle?". Inferred pairs sort first; each shows its
  tier, timestamps, Δt, and class.
- **View-only, always.** A reviewer can **flag** a pair (lands in QA notes
  and beside the pairing-rate figure: `3 flagged`) but can never accept,
  reject, or edit a count — the no-hand-edits warranty rule outranks
  everything. The drawer must not even look editable.
- **Crop provenance states**: local (default), `from CDN` (dimmed mono tag —
  local copy aged out, fetched from the R2-backed CDN), and missing
  (labelled placeholder — analysis predates crops or the write failed;
  never a broken-image icon).
- **Analyze**: running/done job cards add a crops counter (`41 812 crops ·
  612 MB`). **Settings** gain an R2/CDN block (bucket, custom domain) —
  credentials in config, never shown in the UI.
- **Report**: an optional "verification sample" appendix page — a small grid
  of movement crop-pairs with timestamps — off by default.
- **Privacy posture**: crops contain readable plates. Surfaces showing them
  carry a discreet `INTERNAL QA` mono marker unless the report appendix is
  explicitly enabled.

## 8. Deliverables requested from this design session
1. Full-page designs for all four tabs at 390px and 1280px, covering the
   states listed per tab.
2. The junction arrow diagram as a reusable component (screen + print).
3. The Label tab's drawing interaction states (idle / drawing / selected /
   editing vertices) as a sequence.
4. A small component sheet: state dots, job card, bin table cell states
   (normal / peak / gap-hatched), QA numeral block, gate chip, sparkline.
5. Exact hex/size annotations consistent with the design language in §2.
6. The Counts verification drawer: crop-pair layout (same-camera and
   inferred), flag affordance, and the three crop provenance states.
