# Installing a FieldKit node at a site

For the Linux boxes that sit with the cameras. A node records locally, and —
when it has a link — contributes what the labelling team needs and uploads
footage if you ask it to. It survives reboots, losing the link, and losing
power, without anyone standing there.

## What a node needs

    sudo apt install -y python3 python3-pip ffmpeg git
    git clone <repo> ~/fieldkit && cd ~/fieldkit
    pip install -r requirements.txt

Detection (which produces the training frames) also needs `ultralytics`; it
pulls torch, so allow time and disk. A node that only records and uploads
footage does not need it.

## config.yaml

Copy `config.example.yaml` to `config.yaml` and set:

```yaml
site: katuba              # local disk only: <record_dir>/<site>/<cam>/
toll_gate_id: RDA-TG-KTB  # the RDA gate id. Everything in the bucket is keyed by this.
cameras:
  - name: katuba-north    # MUST be globally unique: sample ids are <gate>-<camera>-<time>
    ip: 192.168.1.77
    user: admin
    password: "..."
offload:
  enabled: true
  contribute: true        # training frames: ~90 MB/day per site. Always worth it.
  mirror: false           # whole recordings: ~45 GB/day PER CAMERA. See below.
  recycle: true           # when the disk fills, drop the oldest footage
  min_free_gb: 20
  account_id: "..."       # same R2 credentials as the other sites
  access_key_id: "..."
  secret_access_key: "..."
  bucket: fieldkit-recordings
```

### `site` vs `toll_gate_id`

Two names because they answer to two different people. `site` is for whoever is
standing at the box — it names a directory on that machine's disk and nothing
else. `toll_gate_id` is for whoever opens the bucket six months from now with an
RDA gate code in hand, so every remote key carries it:

| | key |
|---|---|
| recording | `RDA-TG-KTB/katuba-north/20260826-071500.mkv` |
| training frame | `curation/pending/images/RDA-TG-KTB-katuba-north-20260826-071500.jpg` |
| pulling it back | `python ingest_video.py pull RDA-TG-KTB/katuba-north` |

Leave `toll_gate_id` empty and remote keys fall back to `site`, exactly as nodes
behaved before the key existed. Objects already uploaded under the old prefix stay
where they are — this changes new writes only, so a gate that gets its id late has
footage under both names.

The nine gate ids (`LAURETTA_INTEGRATION_GUIDE.md`): `RDA-TG-ABM` Abraham Mokola,
`RDA-TG-CHW` Chongwe, `RDA-TG-CKS` Chingola-Kasumbalesa, `RDA-TG-KFL` Kafulafuta,
`RDA-TG-KTB` Katuba, `RDA-TG-LMB` Lusaka-Mumbwa, `RDA-TG-MCS` Michael Chilufya
Sata, `RDA-TG-MYB` Manyumbi, `RDA-TG-SHM` Shimabala.

Camera names are not cosmetic. Recordings land in `<record_dir>/<site>/<camera>/`
and every training frame is named `<camera>-<timestamp>`, so two sites sharing a
camera name would collide in the bucket and in the dataset.

### Camera heading

`heading:` is the compass direction a camera *faces*. At Katuba a north camera looks
toward the toll gate and a south camera looks away from it, so the heading alone says
which way each vehicle travels — one receding up the frame goes the way the camera
points — and whether it is heading `toward_gate` or `away_from_gate`. A camera named
`katuba-north` gets it from its own name; `gate_ahead: true|false` overrides which side
the gate is on. Until a heading is set, events carry no direction at all — the software
never guesses one.

With a heading set, a vehicle is counted **once, when its centre crosses the count line**
(`count_line`, default 0.55 of the frame along the travel axis) — not when it first appears.
That is what keeps slow traffic honest: a vehicle queued short of the line is never counted
however long it sits, a second track id the tracker gives the same crawling vehicle is
recognised as a twin, and one that re-appears past the line never crosses again. Move the
line with `count_line:` if the default sits on a spot where vehicles routinely stop.

## Carry the taxonomy with you

`dataset/` is not in the repo, so a fresh clone knows nothing about your classes.
Copy three things onto each box (a USB stick or `scp` — they are small):

    dataset/classes.txt           # the class list, positional and authoritative
    dataset/attributes.yaml       # vocabularies, constraints, defaults
    dataset/train_runs/<run>/weights/best.pt   # ~6 MB, the fine-tuned model

then point at the model in config.yaml:

```yaml
detect_backend: cpu
detect_weights: /home/<user>/fieldkit/dataset/weights/best.pt
```

A box without them still works and still contributes: detection falls back to
the stock model, whose four classes land on positions 0-3 of your list — car,
motorcycle, bus and truck line up with `a-small`, `a-motorcycle`, `c-bus` and
`e-heavy` exactly, because those positions are where the taxonomy grew from. Its
proposals are simply coarser: it will never suggest `b-light` or `d-medium`, and
your curators will correct more boxes than they otherwise would.

Nothing a node captures can corrupt the shared taxonomy: a node uploads frames
and labels only, never its config.

## Mirror costs a thousand times what contribute costs

Measured from real segments: ten minutes of 1080p is ~317 MB, so mirroring one
camera is ~45 GB/day. A training frame is ~111 KB and a busy camera yields a few
hundred a day — under 90 MB/day for a whole site.

So on a metered link: **contribute always, mirror on demand.** Turn mirroring on
when you want a window of footage, take it, turn it off again — from the ops
console or the node's own settings page. `recycle: true` then keeps the disk from
filling: the oldest footage is dropped rather than shipped. That is a deliberate
choice of losing the oldest over losing the newest, and it is the right one for a
site that cannot afford to ship video.

## Run it, and keep it running

    sudo tee /etc/systemd/system/fieldkit.service >/dev/null <<'UNIT'
    [Unit]
    Description=FieldKit
    After=network-online.target
    Wants=network-online.target

    [Service]
    User=%i
    WorkingDirectory=/home/%i/fieldkit
    ExecStart=/usr/bin/python3 /home/%i/fieldkit/app.py
    Restart=always
    RestartSec=10

    [Install]
    WantedBy=multi-user.target
    UNIT
    sudo systemctl enable --now fieldkit@$USER

`Restart=always` covers a crash; `enable` covers a power cut. Recording sessions
resume by themselves — the node remembers what was running.

## Check it before you leave the site

    curl -s localhost:8080/api/status | python3 -m json.tool | head -20
    curl -s localhost:8080/api/record/status | python3 -m json.tool | head -30

Look for: cameras `RECORDING`, `detect` running (if you installed it), and under
`offload` — `contribute: true`, `pending_samples` climbing, `contributed` rising
once the link is up. Segments appear under `recordings/<site>/<camera>/`.

The link does not need to be up when you install. Uploads are attempted every
sweep and failures simply retry, so a node parked without a SIM starts shipping
the moment one arrives — nothing to restart.

## Reaching it later

Two ways, and they are complementary:

- **Ops console** — set the `ops:` block (url, token, hive) and the node phones
  home. The console shows every node and carries switches for uploads. Nodes dial
  out, so carrier NAT is not in the way.
- **Tailscale** — `curl -fsSL https://tailscale.com/install.sh | sh` then
  `sudo tailscale up`. The node's own console is then reachable at
  `http://<tailnet-ip>:8080` from anywhere, which is the quickest way to look at
  a camera or flip a switch yourself.

Never expose port 8080 to the public internet: FieldKit has no login by design.
