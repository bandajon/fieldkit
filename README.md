# FieldKit

On-site capture console for Hikvision gear. One local web app, one screen,
two jobs:

1. **First-contact camera setup** — find factory-fresh or already-configured
   cameras on the LAN, activate the new ones, give each a static IP, and aim
   them using a live snapshot preview.
2. **Record** — robust ffmpeg stream-copy recording of any subset of cameras,
   on whatever machine runs the app (Windows laptop, Ubuntu mini-PC, Jetson
   Orin Nano).

It binds `0.0.0.0:8080`, so a phone on the same field WiFi can drive it. No
accounts, no auth — it is a field-LAN tool. Camera credentials live in
`config.yaml` and are never hardcoded.

---

## Quickstart

```
pip install -r requirements.txt
python app.py
```

Then open `http://<the machine's LAN IP>:8080`. The status strip at the top of
every tab shows the hostname and the bound IPs, so you can read off exactly
what to type on a phone.

`ffmpeg` and `ffprobe` must be on `PATH` — recording and the RTSP test shell
out to them.

On first run FieldKit copies `config.example.yaml` to `config.yaml` for you.
There is nothing to edit before you start.

### Setting up cameras without touching YAML

The normal field flow needs no text editor and no restart:

1. **Scan LAN** on the Cameras tab.
2. **Activate** any row showing INACTIVE — type a password, press it. FieldKit
   remembers that password for the rest of the session, so the next two steps
   just work.
3. Pick a **cam slot** (`cam1` = `.111`, `cam2` = `.112`, …) and press
   **Set IP**. The camera reboots onto its new address and is written into
   `config.yaml` automatically; the result line reads
   `now 192.168.1.113 · added to config as cam3`.

That camera is now recordable on the Record tab and visible on Monitor
immediately — no restart. For a camera that already has the address you want,
**Add to config** on its row does the same thing without changing its IP; the
button only appears for cameras that are not in the config yet.

Removing a camera is the **Remove** button on its Record-tab row — no editor,
no restart. It refuses while that camera is recording (stop it first), and
recordings already on disk are kept.

`config.yaml` is **not** tracked in git, so the passwords you type in the field
never land in a commit. `config.example.yaml` is the tracked, commented
reference — read that one to learn the schema.

Once a camera is on the network, set its image/exposure parameters per
[`docs/CAMERA_TUNING.md`](docs/CAMERA_TUNING.md) — wrong defaults (1/6 s
shutter floor, gain 100) turn night-time vehicles into undetectable light
streaks.

### Field networking checklist

Learned the hard way. Check these before blaming the software:

- **One USB-Ethernet adapter per laptop.** Never share one between machines.
  Test each adapter on its own laptop before you travel — Windows often needs
  the dongle's driver, and the installer usually ships as a fake USB drive that
  appears in File Explorer when you plug the adapter in.
- **Two laptops on one switch must not both claim the same address.** FieldKit
  picks a free one by ARP, so it will not collide with a machine that is already
  there. If you set addresses by hand, give each laptop a different one —
  Windows disables a conflicting adapter and then every command fails with
  "transmit failed. General failure".
- **Budget the PoE.** A ColorVu camera can draw ~15 W at boot. Small unmanaged
  switches brown out at two or three cameras: the symptom is cameras that answer
  ARP and SADP but flap or boot-loop the moment you load a page or a stream. If
  cameras keep restarting, count the watts before you debug anything else.
- **Cameras must have static IPs.** A camera left on DHCP strands itself on a
  field switch with no DHCP server — it will come up on a self-assigned address
  nobody can predict. Set the static address while you still have the camera on
  a network that works.
- **FieldKit joins the camera network for you.** Scan LAN notices when a camera
  is on a network this machine has no address on, and adds one automatically.
  On Linux/Jetson it first parks a link-local address on a bare wired port
  (Linux does not self-assign the way macOS/Windows do), so a fresh switch
  works from the first scan. If the scan reports it could not join, run the
  one-time sudoers command it shows, then scan again. The manual commands
  below remain as the fallback.

### Connecting the laptop to the field switch

Field switches have no DHCP server. Plug in and the laptop self-assigns a
`169.254.x.x` address; the scan may still find cameras over SADP, but every HTTP
action against them fails because this machine has no address on their network.

**FieldKit offers to fix this for you.** Any scan row on a network you are not
on shows a warning and a **Join camera network** button; tapping it adds a free
address (`.2`, `.3` or `.4`) to the Ethernet interface. It never touches the
interface carrying your default route, so internet over WiFi keeps working.

The button needs privileges. Grant them once and it works silently from then on:

```
macOS     echo "$USER ALL=(root) NOPASSWD: /usr/sbin/ipconfig set *" | sudo tee /etc/sudoers.d/fieldkit
Ubuntu    echo "$USER ALL=(root) NOPASSWD: $(command -v ip) addr add *" | sudo tee /etc/sudoers.d/fieldkit
Windows   already granted — setup-windows.bat installs FieldKit as an
          always-on task with the needed rights
```

(`setup-linux.sh` writes the scoped Ubuntu grant for you. The scope matters:
`NOPASSWD` on the bare tool would also allow `ip netns exec`, a root shell.)

Without the grant the button still tells you exactly what to run — it prints the
one-time line above and the manual command below. Doing it by hand:

```
macOS     sudo ipconfig set en7 MANUAL 192.168.1.2 255.255.255.0
Windows   adapter Properties > Internet Protocol Version 4 > Use the following
          IP address: 192.168.1.2 / 255.255.255.0
Ubuntu    sudo ip addr add 192.168.1.2/24 dev eth0
```

On macOS, `networksetup -listallhardwareports` tells you which `enN` the
adapter is; the GUI path is System Settings > Network > the adapter > Details >
TCP/IP > Configure IPv4: Manually.

**Leave the gateway empty** when doing it by hand. With no gateway on the
Ethernet port your internet keeps working over WiFi and only camera traffic uses
the cable — which is also
the case FieldKit's SADP probe handles by sending out every interface rather
than just the default route.

Factory-fresh Hikvision cameras sit at `192.168.1.64`, so `192.168.1.x/24` is
the right network to join for first contact.

## Installing on your device

Get the code onto the machine first — either unzip a release
(`./make-release.sh` builds one, with offline wheels for every platform) or
`git clone` when the machine has internet. Then follow your device's section.
When you update later (`git pull` or a new zip), just run the same setup
script again — all of them are idempotent and fix themselves.

### Raspberry Pi (4/5)

1. Flash a **64-bit** Raspberry Pi OS image (Raspberry Pi Imager: "Raspberry
   Pi OS (64-bit)"). 32-bit images cannot work — nobody publishes armv7l
   wheels for the dependency chain.
2. In the FieldKit folder, run:

   ```
   ./setup-linux.sh
   ```

   That is the whole install: ffmpeg, a venv with the dependencies (offline
   from the bundled wheels when run out of a release zip; Bookworm+ refuses
   bare `pip3 install` into the system Python), the sudoers grant so Scan LAN
   joins camera networks silently, and a systemd service so FieldKit starts
   now **and on every boot** — power-cycle the Pi and it comes back
   recording-ready, headless.
3. Open `http://<the pi's IP>:8080` from a phone on the same network (the
   script prints the address). Logs: `journalctl -u fieldkit -f`.

Pi realities: recording is ffmpeg stream-copy (no re-encode), so a Pi 4
handles several cameras — the bottleneck is the SD card; prefer an SSD or a
high-endurance card for `record_dir`. USB Ethernet dongles get Debian's
`enx<mac>` interface names and are detected for camera networks. For live
video, take the **linux_arm64** go2rtc build.

### Jetson Orin Nano

1. JetPack already includes Python 3 — nothing to flash or add.
2. In the FieldKit folder, run:

   ```
   ./setup-linux.sh
   ```

   Same script and same result as the Pi: ffmpeg, venv, network privilege,
   and start-on-boot via systemd.
3. Open `http://<the jetson's IP>:8080`. Logs: `journalctl -u fieldkit -f`.

For live video, take the **linux_arm64** go2rtc build, not amd64.

The same script covers an Ubuntu/Debian mini-PC. Manual install on any Linux
without systemd: `sudo apt install ffmpeg && python3 -m venv .venv &&
.venv/bin/pip install -r requirements.txt && .venv/bin/python app.py`.

### Windows laptop

1. Install Python from python.org if the machine has none (3.12–3.14 all
   work). Tick **both** boxes: "Add python.exe to PATH" and "Install for all
   users" (under Customize install).
2. If you downloaded a release ZIP: right-click it → Properties → tick
   **Unblock** → OK (skipping this makes Windows silently distrust every
   extracted file), then right-click → **Extract All…**.
3. In the extracted folder, double-click **`setup-windows.bat`** → "Run" on
   the security warning → "Yes" on the blue Administrator prompt. It does
   everything: dependencies (offline from the bundled wheels), the firewall
   rule so a phone can reach port 8080, the camera-network address — on the
   built-in Ethernet port or a USB dongle, whichever is connected — and
   installs FieldKit as an **always-on task**: it is running when setup
   finishes and comes back by itself after every reboot, with the rights to
   join camera networks silently. The setup window can then be closed.
4. ffmpeg (needed for recording): `winget install Gyan.FFmpeg` or download
   from ffmpeg.org and add to PATH. Setup warns if it is missing; the UI
   works without it.
5. Open `http://<the laptop's IP>:8080` — the IP is in the strip at the top
   of the screen.

Windows dongle realities: most new laptops have no Ethernet port, so a USB
dongle **is** the camera port. Test the dongle before you travel (Windows
often needs its driver, shipped as a fake USB drive that appears when you
plug it in), always use the same USB port (the parked address follows the
adapter), and plug the dongle in *before* running setup or scanning. Runtime
scans detect wired adapters the same way setup does — physical, connected,
not wireless — so a dongle and a built-in port behave identically.

Recording works the same on NTFS. Stopping a recording sends
`CTRL_BREAK_EVENT` instead of `SIGINT` (the process is spawned with
`CREATE_NEW_PROCESS_GROUP` so the signal can be delivered at all); either way
ffmpeg finalises the segment it is writing before exiting. Nothing is ever
force-killed first. To stop the always-on task itself:
`schtasks /End /TN FieldKit` (stop recordings from the UI first).

---

## Configuration reference (`config.yaml`)

One file, created from `config.example.yaml` on first boot and untracked
thereafter.

| Key | Read by | Meaning |
| --- | --- | --- |
| `cameras` | FieldKit | Cameras recorded directly over RTSP. |
| `cameras[].name` | FieldKit | Short name. Used as the recording subdirectory and as the go2rtc stream name. |
| `cameras[].ip` | FieldKit | Camera address. |
| `cameras[].user` / `cameras[].password` | FieldKit | RTSP and ISAPI credentials for that camera. |
| `site` | FieldKit | This node's site name. Recordings land in `<record_dir>/<site>/<cam>/`. |
| `toll_gate_id` | FieldKit | RDA gate id (e.g. `RDA-TG-KTB`). Keys everything written to R2 — recordings as `<gate>/<cam>/<segment>.mkv`, sample ids as `<gate>-<cam>-<time>`. Empty falls back to `site`; local disk is unaffected either way. |
| `record_dir` | FieldKit | Root for ffmpeg segment recordings. Created on demand. |
| `go2rtc_binary` | FieldKit | Absolute path to the go2rtc binary. Empty means the Monitor tab stays on snapshots. FieldKit never downloads it. |
| `camera_defaults` | FieldKit | Credentials the discovery sweep tries against cameras that are not yet in `cameras`. |
| `detect_backend` | FieldKit | Which detector runs the Monitor overlay: `cpu` (ultralytics, the default) or `hailo` for the site AI servers. An unknown value fails loudly rather than falling back — a silent swap would change what the counts mean. |
| `offload` | FieldKit | Optional Cloudflare R2 upload when the recording drive runs low. Off unless `offload.enabled` is true — see [Cloud offload](#cloud-offload-optional). |
| `ops` | FieldKit | Optional ops console enrolment: `url`, `token`, `hive`. Absent or empty means the node never phones home — see [Joining a hive](#joining-a-hive-optional). |

Relative paths resolve against the app directory, not the shell's working
directory, so `python app.py` behaves the same from anywhere.

You can edit this file from the **Record** tab (Edit config.yaml). It is
validated before it is written and your comments are preserved. Cameras you
add — by hand or via Set IP / Add to config — take effect immediately.
Camera removals made by editing the file need a restart (the Record tab's
**Remove** button removes live); changing `site`, `record_dir` or
`go2rtc_binary` needs a restart.

---

## The tabs

### Cameras

**Scan LAN** runs two discovery passes at once and merges the results by MAC:

- a SADP broadcast (UDP to `239.255.255.250:37020`), which finds Hikvision
  devices even when they are on the wrong subnet or not yet activated;
- a TCP sweep of your `/24` hitting `GET /ISAPI/System/deviceInfo` with digest
  auth, which catches already-configured cameras SADP misses.

Each row shows the IP, MAC, model, whether the unit is activated, and whether
it answers ISAPI. `ACT ?` means the sweep got a 401 — that proves nothing about
activation either way, because a factory-fresh camera rejects that request too.

Per row:

- **Activate** — for factory-fresh units (they ship inactive at `192.168.1.64`).
  Type the password and press it. If the camera demands the encrypted
  activation scheme, FieldKit tells you so rather than guessing: activate that
  one unit with the vendor SADP tool, then come back.
- **Set IP** — pick a `cam1…cam8` slot and the field fills in the site plan
  address (`cam1` = `.111`, `cam2` = `.112`, … in this server's `/24`), or type
  one. FieldKit writes the static address, reboots the camera if it asks to be
  rebooted, and — when a slot is chosen — adds the camera to `config.yaml`
  under that name so it is recordable at once.
- **Add to config** — appears only for cameras not yet configured. Adds the row
  at its current address, using the slot name if one is picked, else the next
  free `camN`.
- **Preview** — a snapshot panel that refreshes about 1.5×/second for aiming.
  Snapshots are proxied through the server because a browser cannot do digest
  auth against the camera. **Full-res shot** opens a single full-size frame in
  a new tab for a framing check.
- **Test RTSP** — a server-side ffprobe of the main stream. Give it up to about
  8 seconds: a camera that is slow to hand over a keyframe is common, and
  waiting beats a false failure. Reports the codec and resolution, or the exact
  error.

### Record

The checklist of cameras from `config.yaml`, each with live state:

- **RECORDING** — connected and writing segments.
- **RECONNECTING** — trying to connect, or reconnecting after a drop. This is
  also the state a camera sits in when its cable is out.
- **STOPPED** — not wanted.

A camera counts as RECORDING only once ffmpeg has actually created a segment
file, not merely started — a process stuck dialling a dead camera stays
RECONNECTING, which is what you want to see at a junction.

Start All / Stop All, or toggle cameras one at a time. Each row shows minutes
elapsed and megabytes written this session, and there is a disk-free bar above
the list. Recording is `-c copy` of the **main** stream in 600-second segments
with wall-clock filenames (`20260804-031816.mkv`), so nothing is re-encoded and
timestamps survive. If ffmpeg dies while a camera is still wanted, it is
restarted with a backoff that widens from 2 s to 30 s.

Megabytes can read 0.0 for the first minute of a healthy recording — Matroska
flushes in clusters, so nothing is durable on disk until the first one lands.

Sessions survive restarts: live sessions are noted in `record_state.json`
(untracked), and on boot FieldKit re-arms whatever was recording when the
process died — including the remaining time of a timed run. A timer that
expired while the node was powered off stays stopped. Combined with the
boot-on-power setup scripts, a field node that loses power resumes recording
by itself when power returns.

### Monitor

Every configured camera as a tile in a responsive grid — multi-column on a
laptop, single column on a phone.

- **Snapshots** (always available) polls each tile at 1 fps through the same
  server-side proxy the preview uses. Polling pauses when the tab is hidden or
  you switch to another tab, so it does not burn battery or bandwidth in your
  pocket.
- **Live** (needs go2rtc, see below) embeds the go2rtc player per camera. If the
  sidecar itself stops, the whole grid drops back to snapshots automatically.
  A single camera whose stream fails shows a **black tile** — the browser gives
  the page no usable error for an embedded player, so switch back to Snapshots
  to confirm whether that camera is reachable at all.

---

## Live video (go2rtc sidecar)

FieldKit implements no streaming itself. It manages [go2rtc](
https://github.com/AlexxIT/go2rtc) — a single static binary with no
dependencies — and embeds its player.

1. Download the build for your platform from the go2rtc releases page:
   `go2rtc_linux_amd64`, `go2rtc_linux_arm64` (Jetson, Raspberry Pi 64-bit), or
   `go2rtc_win64.exe`. **Do this before you go to site.** FieldKit will never
   download anything in the field.
2. Make it executable (`chmod +x`) and put it somewhere stable.
3. Set `go2rtc_binary: /path/to/go2rtc` in `config.yaml` and restart the app.

FieldKit then generates `go2rtc.yaml` from your camera list, starts the
process, supervises it with the same backoff the recorder uses, and shows its
state in the status strip (`absent` / `starting` / `running` / `reconnecting` /
`stopped`). The Live toggle stays disabled until the state is `running`; if
go2rtc dies while you are watching, the grid drops back to snapshots without
breaking the page.

**Sub-stream discipline is a hard rule.** The generated config points only at
`/Streaming/Channels/102`, never `/101`. Live view is therefore the 640×360
aiming stream — it is for pointing cameras and watching a junction, not for
evidence. Recording quality is completely unaffected by any of this.

`go2rtc.yaml` is generated, contains your camera passwords, and is gitignored.
Do not commit it.

---

## Cloud offload (optional)

A node that records for days can fill its drive. With `offload.enabled: true`
FieldKit watches free space on the recording drive and, once it drops below
`min_free_gb`, uploads the **oldest finished segments** to a Cloudflare R2
bucket and deletes each local copy — stopping the moment the drive is back
above the floor. It frees space, it does not drain the disk.

Three rules make it safe to leave on:

- Only **finished** segments move. The segment ffmpeg is currently writing is
  never touched.
- Every upload carries a sha256 that **R2 verifies server-side**. A corrupt or
  truncated upload is rejected by Cloudflare, not by us.
- The local file is deleted **only after** that upload succeeds. Any failure —
  network, credentials, checksum — keeps the footage and stops the pass.

Setup:

1. Create the bucket: `npx wrangler r2 bucket create fieldkit-recordings`.
2. In the Cloudflare dashboard, **R2 → Manage R2 API Tokens**, mint an **Object
   Read & Write** token scoped to that bucket. Note the account ID, access key
   ID and secret.
3. Fill the `offload` block in `config.yaml` and set `enabled: true`.
4. `pip install boto3` — it is not in `requirements.txt`. Enabled without it,
   FieldKit logs `offload enabled but boto3 missing` and stays idle.

State (`enabled`, files uploaded this session, last file, last error) is
reported by `/api/record/status`.

**Self-hosting?** Leave `enabled: false`, the default. Nothing ever leaves the
node, no credentials are needed, and the uploader thread does nothing at all.

---

## Joining a hive (optional)

Several nodes can report to one ops console. Fill the `ops` block with the
console's ingest URL (`ws://<console>:8090/ingest`), an enrolment `token` from
its Enroll page, and the `hive` tag this node belongs to. The node then keeps an
outbound WebSocket open — the console never dials in — sending a heartbeat every
30 s with recording state, 24 h coverage, and a camera still every second beat,
and applying start/stop commands through the same recorder the Record tab uses.

Leave `ops` out (the default) and none of that runs: no thread, no socket, no
traffic. The protocol is `docs/HIVE_PROTOCOL.md`; the node's own status is on
`/api/status` under `hive`.

### Running the ops console

```
python ops.py        # FIELDKIT HIVE OPS on 0.0.0.0:8090
```

One instance per organization, on a machine your team's tailnet can reach —
there is no auth on the console itself, the tailnet is the boundary, and the
enrolment tokens are what keep strangers' nodes out. First boot copies
`ops_config.example.yaml` to `ops_config.yaml` (port, optional alert
`webhook_url`, `disk_amber_hours`) and creates `ops_state/` for tokens,
desired commands, the audit trail and 24 h of replayable heartbeats — plain
JSON/JSONL, no database.

Flow: open `/enroll`, create a token for a hive, paste it into each node's
`ops` block. The fleet ledger at `/` sorts worst-first and folds all-green
hives; click a hive for its nodes and cameras, a camera for full focus (live
video is your browser talking straight to that node's go2rtc over the
tailnet — the console never touches video). Start/stop commands ride tickets
that show commanded-vs-acked per node; nodes offline at command time apply
the stored command when they reconnect. `python3 test_e2e.py` smoke-tests
the whole loop against a real node client.

---

## Remote access via Tailscale

The app already binds `0.0.0.0`, so remote monitoring is a deployment choice,
not a feature. Put the node host on your tailnet and reach it from anywhere
with any internet at the node (4G router, phone hotspot).

```
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Then open `http://<tailscale-ip>:8080` from your laptop or phone.

To reach the **camera web UIs** on the site LAN the same way, advertise the
site subnet from the node host and approve the route in the Tailscale admin
console:

```
sudo tailscale up --advertise-routes=192.168.1.0/24
```

Live video works over Tailscale too: WebRTC connects peer-to-peer where it can
and falls back to a DERP relay where it cannot, so there is nothing extra to
configure. Expect the relay path to be softer on a poor uplink — the aiming
stream is small, which is exactly why it is the sub-stream.

---

## Deployment

**Local-first is the real deployment.** FieldKit belongs on the machine at the
node: `pip install -r requirements.txt && python app.py`. There is no
installer and no container, on purpose.

The GitHub repository auto-deploys to Railway as a **UI demo and staging
environment only**. Everything that matters needs the field LAN: SADP discovery
is a broadcast, the TCP sweep scans a local `/24`, and recording needs a route
to the cameras. None of that works from a cloud
host — the cloud instance is for looking at the interface, nothing else. The
app honours the `PORT` environment variable so that deploy works; locally it
defaults to 8080.

---

## Testing

```
python3 test_fieldkit.py
```

Runs each module's built-in self-check plus direct assertions: SADP parsing of
an inactive camera (spec acceptance #2), MAC normalisation across the formats
SADP and ISAPI use, go2rtc reporting `absent` with no binary configured, and a
cross-check that every key in `config.example.yaml` is documented in this README.

Each module is independently runnable and asserts its own risky logic:

```
python3 camera.py      # SADP parsing, MAC normalisation, discovery merge
python3 recorder.py    # spawns a real ffmpeg at a dead address, asserts supervision
python3 live.py        # go2rtc config generation, sub-stream discipline
```

There is no test framework and no hardware in the loop. The spec's acceptance
checklist is the definition of done; these checks cover the logic that can be
verified without cameras on the bench.

---

## Layout

```
app.py             FastAPI app, all routes
camera.py          SADP discovery, TCP sweep, activation, static IP, snapshot, RTSP test
recorder.py        ffmpeg process supervisor
live.py            go2rtc sidecar manager
test_fieldkit.py   stdlib test runner
config.example.yaml  tracked, commented reference schema
config.yaml        live config (untracked; auto-created on first boot)
static/index.html  the entire UI, one file, vanilla JS
```
