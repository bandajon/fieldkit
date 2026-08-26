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
site: chunga              # MUST be unique across sites: it is the storage path
cameras:
  - name: chunga-north    # MUST be globally unique: sample ids are <camera>-<time>
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

Camera names are not cosmetic. Recordings land in `<record_dir>/<site>/<camera>/`
and every training frame is named `<camera>-<timestamp>`, so two sites sharing a
camera name would collide in the bucket and in the dataset.

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
