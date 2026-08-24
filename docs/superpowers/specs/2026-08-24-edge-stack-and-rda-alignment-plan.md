# Edge stack + RDA alignment — master plan

2026-08-24 · approved: bus class added now; both security fixes in scope.
Two workstreams converging on one enriched payload contract
(/lauretta/v1 v3.1) spoken by Lauretta devices AND FieldKit-as-edge-unit.

## Ground truth established by the review

- RDA backend fees are CORRECT (migration 022, effective 2026-08-21:
  20/40/200/300/3000 + historical rates by detection_timestamp). The
  integration guide (15/25/40/60/100) and two frontend tables
  (10/25/40/60/80 and 20/35/75/150/300) are stale.
- Bus/K50 has no home in the 5-class system (022's own comment).
- Defects: unknown vehicle_class silently maps to light (K40); a per-site
  token can post as any site (site_id from payload only); heartbeat is
  unauthenticated. class_confidence/headway columns are dead (never fed).
- Frontend hardcodes the class vocabulary in ~12 files, 6 palettes/lists,
  6 spellings; GET /vehicle-classes already serves correct data + colors.

## Workstream R — RDA (repo ~/rda)

R1 Hygiene: (a) frontend classification registry — one module, fed from
GET /vehicle-classes, replacing every hardcoded palette/label/fee table;
(b) ingestion rejects unknown vehicle_class with 400 naming the valid set
(replaces the silent →light default); (c) guide fee table corrected.
R2 Bus: additive enum value 'bus', class_code 6, toll_rate 50, mapper
"Bus"→bus, axle bounds 2-3; registry renders it with no further frontend
work. Category letters A-F ship as registry display metadata
(small=A light=B bus=C medium_heavy=D heavy=E abnormal=F) — presentation,
not a 9-table enum rename.
R3 Payload v3.1 (optional fields, nullable columns, validated vocab):
class_confidence (0-1, revives the dead column), axle_config ("1+2+3"
pattern), trailer_count (0-3), cargo_type (FieldKit cargo vocab),
vehicle_type (FieldKit type vocab). Dashboards: surface in axle-analysis +
vehicle-characteristics.
R4 Security: per-site token binds site_id (mismatch → 403; env flag
LAURETTA_SITE_BINDING=warn|enforce for rollout grace), heartbeat requires
bearer auth (same tokens).
R5 LAURETTA_INTEGRATION_GUIDE v3.0: corrected fees + effective-dating
note, 6 classes + letters, v3.1 field spec with vocabularies + examples,
rejection semantics, security changes with grace period, changelog.

## Workstream F — FieldKit as edge unit (repo ~/fieldkit)

F1 Vehicle events + evidence: per counted track persist an event
(SQLite events.db per site: ts, camera, class/letter, attrs incl axles +
config + cargo + trailers, wheel-derived axles flag, crop paths) and
three crops: first-seen (approach aspect), best (largest), last-seen
(departure aspect) under evidence/<date>/<cam>/. Retention cap by GB.
F2 Direction + speed: direction from track trajectory + per-camera
heading map in config (katuba-north: approach=northbound etc.); speed
from two per-camera reference lines (config: line y-positions + metres
apart) timed at track level; stored on the event, honest ±15-20% until
surveyed. Both feed counts breakdown + events.
F3 ANPR: 'plate' detector class (Label-tab drawn, like wheel); OCR stage
on plate crops (open-source ONNX OCR), Zambian format regex validation,
plate + confidence on the event. Phase-gated on labels + model v2.
F4 RDA uplink: uplink module batches events → POST /lauretta/v1/vehicles
(+ heartbeat 1/min, images endpoints) with per-site bearer token from
config (ops console can push it like R2 creds). Class mapping A-F →
RDA names (a-*→Small... e-plant→Heavy, f-abnormal→Abnormal, c-bus→Bus).
Buffer-and-retry per the guide's own recommendations.
F5 Hailo: detector (+plates +wheels) compiled to .hef; stack deployed on
the container Linux boxes; site tokens issued from the RDA /edge page.

## Order

Wave 1 (now): R1, R2, F1. Wave 2: R3+R5 together, F2. Wave 3: R4, F4.
Wave 4: F3, F5. Each wave verified + committed before the next.
