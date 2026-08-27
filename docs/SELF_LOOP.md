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
- Change the cadence: edit `THRESHOLD` / `PER_CAM` / `CLASSIFY_PER_PASS` in `selfloop.py`, intervals in
  `install_loop.sh`, then `./install_loop.sh` again.

`detect.attr_classifier()` is the one loader for `attrs.pt` — live detection, the offline
classifier and ingest suggestions all go through it, and `selfloop.py`'s self-check loads a
checkpoint in the trainer's exact format so the two files cannot drift apart unnoticed.

What it deliberately does not do: touch a toll gate. `models/champion.pt` in the bucket
is the offer; deploying it to a site box (`detect_weights`, restart) stays a human step,
because a pre-labeller that got worse costs curators minutes and a gate detector that got
worse costs revenue.
