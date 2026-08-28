# YOLO26 vs v2 (2026-08-28)

Same 1,058-frame reference set, same hash-based val split (`train.split_of`, 116 frames),
`train.py baseline` for every run, scored by `train.py eval`, CPU/MPS speed by `train.py bench`
at imgsz 1280 (the gate boxes run on CPU). Runs live in `dataset/train_runs/` on the office
MacBook Pro; raw numbers in `dataset/compare-yolo26.json` and each run's `val-eval.json` /
`reference-eval.json`.

| run | model | recipe | val mAP50 / 50-95 | CPU ms/frame | MPS ms/frame |
|---|---|---|---|---|---|
| 20260827T191819Z (v2) | yolov8n | defaults | 0.760 / 0.505 | 75.9 | 38.7 |
| 20260828T032640Z (A) | yolo26n | defaults (`auto` → AdamW lr 0.00071) | 0.710 / 0.463 | 91.4 | 26.6 |
| 20260828T055801Z (B) | yolo26n | AdamW lr0 0.001, mosaic 0.5, 100 ep | **0.780 / 0.535** | 84.9 | 26.8 |

Reference-set scores (0.951 / 0.758 / 0.895) are training-set scores for all three and are
not comparable — they only show v2 memorised its training frames hardest.

**Champion: run B** (`selfloop.py adopt 20260828T055801Z`). +2 mAP50 / +3 mAP50-95 on the
held-out split, NMS-free end-to-end inference (no NMS tuning on the gate boxes, and the same
model exports to Hailo via Ultralytics 8.4 `export(format="hailo")`). Cost: ~12 % slower per
frame on CPU than v8n — 85 ms is still well under the 5 fps budget.

What mattered from the research: YOLO26's defaults are tuned for COCO-scale data; on a
1k-frame set the `auto` optimiser's low learning rate and full mosaic under-fit (run A lost to
v2). A modest fixed lr with half mosaic (run B) is what gets the architecture's gain out.
The loop's `FIELDKIT_TRAIN_ARGS` carries that recipe forward.

Left for a human: deploy `dataset/champion.pt` to the Katuba box (see SITE_INSTALL.md).
