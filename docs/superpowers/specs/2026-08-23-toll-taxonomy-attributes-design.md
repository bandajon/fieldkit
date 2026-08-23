# Toll taxonomy + vehicle attributes — design

2026-08-23 · approved: two-stage architecture; motorcycles stay a separate
class. Supersedes the flat 8-class taxonomy for model v2.

## Why two-stage

Multi-label detection in one YOLO model is unsupported custom surgery
(modified NMS/heads), breaks the ultralytics train pipeline and the Hailo
compile path. Industry standard (incl. DeepStream's PGIE/SGIE pattern) is a
detector for the primary class plus attribute classifiers on the box crop.

## Stage 1 — detector: toll categories

Detector classes (classes.txt, positional; renames keep ids so no label file
changes):

| id | new name    | from                          | toll |
|----|-------------|-------------------------------|------|
| 0  | a-small     | small-vehicles-passenger-cars | A 20 |
| 1  | motorcycle  | motorcycle                    | own  |
| 2  | c-bus       | buses                         | C 50 |
| 3  | e-heavy     | heavy-vehicles                | E 300|
| 4  | b-light     | light-vehicles                | B 40 |
| 5  | d-medium    | medium-heavy-vehicles         | D 200|
| 6  | f-abnormal  | abnormal-load-vehicles        | F 3000|
| 7  | minibuses   | DISSOLVES: review pass splits each box into a-small (≤16 seats, Hiace-size) or b-light (17–30, Coaster-size); class removed when empty |

Honest limit: tonnage/seat thresholds are not visible; categories are the
same visual judgment a toll clerk makes. Attributes make it auditable.

Deployed model v1 keeps emitting its baked-in old names until v2 trains —
capture stays CORRECT regardless because label files store positional ids,
which the rename does not move.

## Stage 2 — attribute classifier (crops)

One small multi-head model (torchvision mobilenet_v3_small backbone, three
linear heads; ~60 lines of torch in train_attrs.py):

- type: car, suv, van, minivan, pickup, minibus, coaster, bus, rigid-truck,
  articulated, tanker, other
- axles: 2, 3, 4, 5, 6, 7plus  (classifier bins for v1; ponytail ceiling —
  upgrade path is a wheel detector + spacing-grouping when tolling-grade
  axle accuracy is needed)
- cargo: none, general, container, tanker-liquid, mineral-transport,
  mining-equipment, construction-equipment, other

Head vocabularies live in dataset/attributes.yaml (operator-editable,
append-only per head, same positional-id rule as classes.txt).

## Labels on disk

YOLO txt files stay exactly as they are (category ids). Attributes ride in a
sidecar: dataset/{pending,approved}/attrs/<id>.json →
`{"<box index>": {"type": "articulated", "axles": "6", "cargo": "mineral-transport"}}`
— sparse: only boxes the operator enriched. approve/unapprove/discard move
or delete the sidecar with the sample. Box indices follow the label file's
line order as saved at approve time.

## Label tab

Tap-cycle still sets the category. Tapping a box also SELECTS it and shows a
compact attribute strip under the actions row: type chips, axle stepper
(2…7+), cargo chips. Chips reflect/update the selected box's sidecar entry.
Unenriched attributes stay unset — never guessed. Attribute strip only
renders when a box is selected.

## Training + live

- train.py: unchanged (categories).
- train_attrs.py: exports crops from approved samples that have attrs,
  trains the multi-head model (MPS), reports per-head accuracy, saves
  dataset/attr_runs/<ts>/attrs.pt. Refuses under ~200 enriched boxes.
- detect.py: config key `attr_weights`; when set, each NEWLY COUNTED track's
  best crop (largest box seen) is classified once; counts() gains a
  `breakdown` — per category, tallies by type/axles/cargo. Review tab frames
  append attributes to box labels ("e-heavy · articulated · 6ax · mineral").
- Hailo phase (later): both stages compile independently (detector .hef +
  classifier .hef).

## Phases (each shippable)

1. Taxonomy migration: renames via the class API; move-back review pass for
   the minibuses split; remove class 7 when empty. Ops, no code.
2. Attrs sidecar + API + Label tab attribute strip.
3. train_attrs.py + live attribute classification + counts breakdown +
   Review display.
4. Deferred: wheel-detector axle counting; tanker split inside e-heavy
   (cargo head now covers it — revisit if detector-level split still wanted);
   Hailo compile.

## Testing

Self-checks per module as house style; migration verified by class-usage
scan (no dangling ids, counts preserved); attribute round-trip via the API;
live breakdown verified against a manually attributed sample set.
