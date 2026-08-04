# FieldKit

On-site capture and extraction console for Hikvision gear. One local web app,
one screen, three jobs:

1. **First-contact camera setup** — find factory-fresh or already-configured
   cameras on the LAN, activate the new ones, give each a static IP, and aim
   them using a live snapshot preview.
2. **Record on anything that isn't an NVR** — robust ffmpeg stream-copy
   recording of any subset of cameras, on whatever machine runs the app
   (Windows laptop, Ubuntu mini-PC, Jetson Orin Nano).
3. **Pull from multiple NVRs on a switch** — drive the existing ISAPI
   extraction in `nvr_pull.py`, watch progress live, and get a per-site
   VERIFIED badge before anyone clears a disk.

It binds `0.0.0.0:8080`, so a phone on the same field WiFi can drive it. No
accounts, no auth — it is a field-LAN tool. Camera and NVR credentials live in
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

`ffmpeg` and `ffprobe` must be on `PATH` — recording, the RTSP test, and the
NVR pull's verification step all shell out to them.

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

Removing a camera is the one thing that still needs the editor: delete it from
`config.yaml` (NVR Pull tab → Edit config.yaml) and restart the app.

`config.yaml` is **not** tracked in git, so the passwords you type in the field
never land in a commit. `config.example.yaml` is the tracked, commented
reference — read that one to learn the schema.

### Ubuntu / Debian (including a mini-PC at the node)

```
sudo apt update && sudo apt install -y python3-pip ffmpeg
pip3 install -r requirements.txt
python3 app.py
```

### Jetson Orin Nano (ARM64)

Identical to Ubuntu — the app is pure Python and ships no compiled extensions.
JetPack already includes Python 3. Install `ffmpeg` from apt as above. If you
want live video, take the **linux_arm64** go2rtc build, not amd64.

### Windows laptop

```
winget install Gyan.FFmpeg        # or: choco install ffmpeg
pip install -r requirements.txt
python app.py
```

Recording works the same on NTFS. Stopping a recording sends
`CTRL_BREAK_EVENT` instead of `SIGINT` (the process is spawned with
`CREATE_NEW_PROCESS_GROUP` so the signal can be delivered at all); either way
ffmpeg finalises the segment it is writing before exiting. Nothing is ever
force-killed first.

If Windows Firewall prompts on first run, allow private networks — otherwise
the phone on the field WiFi cannot reach port 8080.

---

## Configuration reference (`config.yaml`)

One file, created from `config.example.yaml` on first boot and untracked
thereafter. It is shared with `nvr_pull.py`, which reads the first three keys.

| Key | Read by | Meaning |
| --- | --- | --- |
| `out_dir` | nvr_pull | Root of the extraction tree: `<out_dir>/<date>/<site>/<cam>/`. |
| `remux_mkv` | nvr_pull | Remux pulled `.mp4` segments into `.mkv` with `-c copy`. No re-encode, no quality change. If the remux fails the original container is kept. |
| `nvrs` | nvr_pull, NVR tab | List of NVRs on the switch. One entry per unit. |
| `nvrs[].name` | both | Site name. Becomes a directory name and the badge label. Must be unique. |
| `nvrs[].host` | both | NVR address for ISAPI on port 80. |
| `nvrs[].user` / `nvrs[].password` | both | ISAPI digest credentials for that NVR. |
| `nvrs[].channels` | both | Which tracks to pull. |
| `channels[].id` | both | ISAPI track ID. Camera N's main stream is `N*100 + 1` — cam1 = `101`, cam2 = `201`, cam3 = `301`. |
| `channels[].label` | both | Directory name for that camera's footage inside the site folder. |
| `cameras` | FieldKit | Cameras recorded directly over RTSP, with no NVR in the path. |
| `cameras[].name` | FieldKit | Short name. Used as the recording subdirectory and as the go2rtc stream name. |
| `cameras[].ip` | FieldKit | Camera address. |
| `cameras[].user` / `cameras[].password` | FieldKit | RTSP and ISAPI credentials for that camera. |
| `site` | FieldKit | This node's site name. Recordings land in `<record_dir>/<site>/<cam>/`. |
| `record_dir` | FieldKit | Root for ffmpeg segment recordings. Created on demand. |
| `go2rtc_binary` | FieldKit | Absolute path to the go2rtc binary. Empty means the Monitor tab stays on snapshots. FieldKit never downloads it. |
| `camera_defaults` | FieldKit | Credentials the discovery sweep tries against cameras that are not yet in `cameras`. |

Relative paths resolve against the app directory, not the shell's working
directory, so `python app.py` behaves the same from anywhere.

You can edit this file from the **NVR Pull** tab (Edit config.yaml). It is
validated before it is written and your comments are preserved. Cameras you
add — by hand or via Set IP / Add to config — take effect immediately.
Removing a camera, or changing `site`, `record_dir` or `go2rtc_binary`, needs a
restart.

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

### NVR Pull

Pick a date and a time window, select NVRs (or **Pull All**), and go. Each site
pulls in its own background thread and streams its log lines into the pane
underneath. **Dry Run** lists what would be downloaded without fetching it.

Every NVR row has a reachability dot, a state badge, and — once a pull has
finished — a link to that site-day's `manifest.json`.

**The verification rule, which matters more than anything else on this tab:**

- FieldKit and `nvr_pull.py` **never delete anything from an NVR**. Ever.
- After a pull, each site-day gets a `manifest.json` listing every file with
  its size, duration, and sha256.
- A `.verified` marker file is written **only if every single check passed** —
  every channel returned segments, every file downloaded, and every file
  probed back as readable with a non-zero duration.
- **VERIFIED** (green) means that marker exists. **ATTENTION** (red) means it
  does not, and the log says why: no recordings in the window, a failed search,
  a failed download, or an unreadable file.
- An operator clears an NVR's disk **only** after the site-day shows VERIFIED
  and a human has reviewed it. No marker, no clearing — re-run instead.

Re-running a pull is safe and cheap: files that already exist are skipped.

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
   `go2rtc_linux_amd64`, `go2rtc_linux_arm64` (Jetson), or
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

## Remote access via Tailscale

The app already binds `0.0.0.0`, so remote monitoring is a deployment choice,
not a feature. Put the node host on your tailnet and reach it from anywhere
with any internet at the node (4G router, phone hotspot).

```
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Then open `http://<tailscale-ip>:8080` from your laptop or phone.

To reach the **NVR web UIs** on the site LAN the same way, advertise the site
subnet from the node host and approve the route in the Tailscale admin console:

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
is a broadcast, the TCP sweep scans a local `/24`, recording needs a route to
the cameras, and the NVR pull needs the NVRs. None of that works from a cloud
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
nvr_pull.py        existing ISAPI extraction tool, used as a library
test_fieldkit.py   stdlib test runner
config.example.yaml  tracked, commented reference schema
config.yaml        live config (untracked; auto-created on first boot)
static/index.html  the entire UI, one file, vanilla JS
```
