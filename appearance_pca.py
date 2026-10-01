"""Refit the appearance PCA after an attrs retrain (the codes are only comparable within
one champion's feature space — detect.appearance_pca refuses a mismatched file).

  python3 appearance_pca.py --weights dataset/attrs-champion.pt --crops DIR [--out ...]
  python3 appearance_pca.py --weights ... --gate <gate>     # newest crops from R2 today
  python3 appearance_pca.py --self-check
"""
import argparse
import hashlib
import io
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

MIN_CROPS = 200
NEWEST = 1500


DIMS = 32                # journeys decodes exactly 32 fp16 values


def fit(X, dims=DIMS):
    """-> (mu, P (dims x D), explained variance fraction)."""
    mu = X.mean(0)
    _, s, vt = np.linalg.svd(X - mu, full_matrices=False)
    return mu, vt[:dims], float((s[:dims] ** 2).sum() / (s ** 2).sum())


def r2_crops(gate):
    """Newest NEWEST best crops under one gate/day prefix — never lists the bucket.
    Today (Lusaka) first, yesterday's too when today has too few to fit on."""
    import dataset_sync
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    o = dataset_sync.creds()
    cl = dataset_sync.client(o)
    today = datetime.now(ZoneInfo("Africa/Lusaka")).date()
    objs = []
    for day in (today, today - timedelta(days=1)):
        prefix = f"fieldkit-tracklets/{gate}/{day:%Y%m%d}/crops/"
        objs += [x for page in cl.get_paginator("list_objects_v2").paginate(Bucket=o["bucket"], Prefix=prefix)
                 for x in page.get("Contents", []) if x["Key"].endswith("-best.jpg")]
        if len(objs) >= MIN_CROPS:
            break
    objs.sort(key=lambda x: x["LastModified"], reverse=True)

    def get(x):
        for _ in range(2):                      # one retry; a stuck crop costs itself, not the run
            try:
                return cl.get_object(Bucket=o["bucket"], Key=x["Key"])["Body"].read()
            except Exception:
                pass
    with ThreadPoolExecutor(8) as ex:
        got = list(ex.map(get, objs[:NEWEST]))
    blobs = [b for b in got if b]
    print(f"fetched {len(blobs)}, skipped {len(got) - len(blobs)}")
    if len(blobs) < 300:
        sys.exit(f"only {len(blobs)} crops fetched from R2 — need at least 300")
    return blobs


def self_check():
    X = np.random.default_rng(0).normal(size=(200, 576))
    mu, P, ev = fit(X, 32)
    z = (X - mu) @ P.T
    z /= np.linalg.norm(z, axis=1, keepdims=True)
    assert mu.shape == (576,) and P.shape == (32, 576) and 0 < ev < 1, (mu.shape, P.shape, ev)
    assert np.allclose(np.linalg.norm(z, axis=1), 1) and np.allclose(P @ P.T, np.eye(32), atol=1e-6)
    import tempfile
    import detect
    with tempfile.TemporaryDirectory() as td:        # the file detect will accept, tag and all
        w = Path(td) / "attrs-champion.pt"
        w.write_bytes(b"w")
        tag = hashlib.sha256(b"w").hexdigest()[:12]
        np.savez(Path(td) / "appearance-pca.npz", mu=mu, P=P, champion=tag)
        assert detect.appearance_pca(w, tag)[2] == tag
    print("appearance_pca self-check ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weights")
    ap.add_argument("--crops")
    ap.add_argument("--gate")
    ap.add_argument("--out")
    ap.add_argument("--self-check", action="store_true")
    a = ap.parse_args()
    if a.self_check:
        return self_check()
    if not a.weights or not (a.crops or a.gate):
        ap.error("--weights and one of --crops / --gate are required")
    from PIL import Image
    import detect
    blobs = ([p.read_bytes() for p in sorted(Path(a.crops).glob("*.jpg"))] if a.crops
             else r2_crops(a.gate))
    classify = detect.attr_classifier(a.weights)
    embed = classify.embed
    X = np.stack([embed(Image.open(io.BytesIO(b)).convert("RGB")) for b in blobs])
    if len(X) <= DIMS:
        sys.exit(f"{len(X)} crops is too few for {DIMS} dims")
    mu, P, ev = fit(X, DIMS)
    tag = classify.tag
    out = Path(a.out) if a.out else Path(a.weights).with_name("appearance-pca.npz")
    np.savez(out, mu=mu, P=P, champion=tag)
    print(f"{len(X)} crops, explained variance {ev:.3f}, champion {tag} -> {out}")


if __name__ == "__main__":
    main()
