# Running a team curation week

The curation instance is FieldKit with `FIELDKIT_MODE=curation`: the Label tab
only, every call token-gated, no camera or recorder API. Curators need a URL, a
handle and a token — nothing installed, phones included.

    https://curation-production.up.railway.app

The operator's Mac stays the hub: it owns the models, the training runs and the
payroll numbers. Work flows between the two through the R2 bucket, and the
instance syncs itself every two minutes in both directions.

## Roster

`dataset/curators.yaml` — `handle: token`, one per curator. Handles are
lowercase (`^[a-z0-9][a-z0-9_-]{0,23}$`); tokens are whatever you invent, 12+
characters. Hand each person their pair privately.

    python dataset_sync.py push        # roster reaches the instance in <=2 min

Reviewer powers need BOTH the handle in the instance's `REVIEWERS` env var and
a valid token:

    railway variables --service curation --set "REVIEWERS=jonah,supervisor2"

## Assignments

Steer a curator toward a class with a quota — their queue leads with frames
containing it, and their tab shows `assignment: c-bus · 37/200`:

    python assign.py curator01 c-bus 200
    python assign.py --clear curator01
    python assign.py --list            # local table + live progress per curator

Unassigned curators get newest-first work, so nobody is ever idle.

## Feeding the queue

Frames are made where the detector lives (this Mac), never in the cloud
container — it deliberately ships without torch.

    python ingest_video.py pull katuba/katuba-north   # mirrored site footage
    python ingest_video.py ~/clips/                   # any local video
    python ingest_video.py incoming                   # videos uploaded from the UI
    python dataset_sync.py push                       # publish the new samples

Reviewers can upload footage straight from the curation site ("upload video");
it lands in R2 `incoming/` for the command above to process.

## Quality and payment

Two independent measurements, both running in the background of ordinary work:

- **Gold plants** — verified frames with deliberate errors, served disguised.
  Catching the error scores; rubber-stamping scores zero. Seed them from your
  own approvals: `python make_gold.py 30 --from-who jonah`.
- **Sampled review** — open the site with `#review` on the URL to correct real
  submissions. The diff scores that curator and your version becomes the record.

    python dataset_sync.py pull --ledgers   # harvest approvals + audit + scores
    python payroll_report.py 7              # table + CSV: volume and both accuracies

Accuracies print "insufficient sample" under three measurements — small-sample
honesty matters when the number decides a payment.

## Daily rhythm

    morning:  python ingest_video.py pull <site>/<cam> && python dataset_sync.py push
    anytime:  #review on the site, a few samples per curator
    evening:  python dataset_sync.py pull --ledgers
    friday:   python payroll_report.py 7

## When the week ends

The instance is a public endpoint protected by tokens alone. Take it down:

    railway down --service curation

Everything is already on this Mac if the ledgers were pulled — that is what the
two-minute sync exists to guarantee.
