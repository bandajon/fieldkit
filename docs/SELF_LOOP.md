# The self-improving loop

Runs on the always-on training machine (the office MacBook Pro, `100.83.203.125`).

```
gate boxes mirror recordings ─► bucket ─► selfloop ingest (every 4 h)
        sample newest segments, champion model pre-labels ─► pending ─► bucket ─► curation tool
curators approve ─► bucket ─► selfloop train (checked hourly)
        ≥ 1,000 new frames outside the reference set ─► train.py ─► score on the reference
        ─► better than the champion? ─► champion.pt (pre-labels the next ingest) + models/ in the bucket
```

- `python selfloop.py status` — where it stands; `loop-ingest.log`, `loop-train.log` — what it did.
- `./run_training.sh` — train now regardless of the threshold; same lock, same promotion.
- `python selfloop.py adopt <run>` — crown a run trained by hand.
- Change the cadence: edit `THRESHOLD` / `PER_CAM` in `selfloop.py`, intervals in
  `install_loop.sh`, then `./install_loop.sh` again.

What it deliberately does not do: touch a toll gate. `models/champion.pt` in the bucket
is the offer; deploying it to a site box (`detect_weights`, restart) stays a human step,
because a pre-labeller that got worse costs curators minutes and a gate detector that got
worse costs revenue.
