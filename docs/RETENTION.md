# Retention maintenance

Install prerequisites with the project virtualenv and configure the offload credentials used by `dataset_sync.py`.

The application holds `dataset/serving.lock` for its lifetime. A standalone cycle
uses the same nonblocking lease and safely reports `busy` while the app serves;
`--quiesced` remains a compatibility acknowledgement.

```sh
python retention_maintenance.py once --dataset /Users/admin/fieldkit/dataset --quiesced
```

To publish the initial policy, stop the app and run:

```sh
FIELDKIT_RETENTION_AUTHORITY=1 python retention_maintenance.py activate --dataset /Users/admin/fieldkit/dataset
```

`run apply` also requires `--quiesced`; it acquires the serving lease before
credentials or cloud work. The in-process callback runs every 120 seconds, while
the office job runs hourly. The seven-day cutoff advances on each sweep, with
the initial floor at `2026-09-02T22:00Z`.

`classifier-crops/` and best-evidence artifacts are permanent. Raw videos live
outside this rolling dataset policy.

The macOS launchd template discards routine stdout; `retention-last-run.json`
and `retention-receipts.jsonl` remain the durable records. When installing it
locally, use the available project virtualenv interpreter in the template.
