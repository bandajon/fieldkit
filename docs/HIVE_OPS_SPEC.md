# Hive Ops — FieldKit fleet console + node agent
## Build spec distilled from the approved Claude Design project "FieldKit Ops console design"
## (Design Spec v1 2026-08-08, Hive Console prototype, Wireframes 1a+1d+1g). Same repo as FieldKit.

## What this is
Two halves, one repo, one wire protocol:

1. **Node agent** (runs inside the existing FieldKit app on every node): phones
   home to the console over a WebSocket, heartbeats status, reconciles
   desired-state commands, serves log tails. Enabled only when `ops:` is
   configured in config.yaml — a node without it behaves exactly as today.
2. **Hive console** (`python hive.py`, default port 8090, configurable): the
   FIELDKIT OPS web UI supervising the whole fleet. Light "blueprint" theme —
   deliberately NOT the dark node UI, so an operator always knows which
   console they're on.

The console can run on any machine on the tailnet; nothing depends on where.

## Wire protocol (WebSocket, console is the server at /ws)
- Node connects with `{token, host, hive, site, versions}`; console accepts
  (enrolled or valid token) or rejects (recorded in the rejected-connections
  ledger; rejected nodes appear nowhere else).
- **Heartbeat every 45 s** node→console: recorder state per camera
  (RECORDING / RECONNECTING+since / STOPPED), per-camera 24 h segment summary
  (from 600 s segment arithmetic — 6 segments per hour-cell), disk free in
  HOURS at current write rate (never percent), clock offset vs console,
  restart counts, go2rtc state, versions. Console marks a node **offline
  after 3 missed heartbeats**.
- **Snapshot piggyback**: a small JPEG (~15–25 kB, sub-stream frame) rides on
  every Nth heartbeat (tier 1). Tier 2/3 fetch snapshots on demand straight
  from the node's FieldKit HTTP API over the tailnet — the console NEVER
  relays media, and the main stream never leaves the node.
- **Commands console→node** are desired state, not RPC: `SET RECORDING=ON`
  (scope: node/hive/fleet). Node reconciles on receipt (or next reconnect),
  then acks. Offline nodes get the command queued server-side; it applies on
  reconnect — never silently partial, no timeout ever fakes success.
- **Log tail**: console requests last 200 lines of recorder/go2rtc logs over
  the control channel; read-only, no shell access.

## Console storage
Append-only JSONL files: heartbeats/segment summaries (24 h replayed into
memory on restart), audit log (every operator action; nothing editable),
enrollment (tokens, enrolled nodes, rejected attempts). No database.

## Console UI (static/hive.html — buildless, hash routing, back = up)
| Screen | URL | Tier |
|---|---|---|
| Fleet | `#/fleet` | 1 — cached/heartbeat snapshots, 30–60 s |
| Hive | `#/hive/:id` | 2 — snapshot polling ~5 s, no live video |
| Camera | `#/hive/:id/node/:host/cam/:cam` | 3 — the fleet's ONE live stream |
| Enrollment | `#/enroll` | — |
| Audit | `#/audit` | — |

Naming: **Hive → Node (hostname) → Camera**. Hive = prettified `hive:` tag
from node config ("hive:sunrise-sub" → "HIVE B — SUNRISE SUBSTATION"); `site`
is secondary metadata, never a nav level. Overlays (confirm, command dock,
log drawer) are not routes.

**Fleet view** answers "what is worst right now": severity per node
(offline 100, disk<6 h 60, cam reconnecting 50, heartbeat-only 40, stopped
cam 30, drift 20, gap 15, version divergence 10); hives sort by worst node,
nodes within hive; healthy fleets read boring. Node = comfortable row (1a):
dot+hostname+hb age, chips only when noteworthy (nominal = hairline outline,
breach = solid fill), cameras as chips with name, state word, coverage
micro-bar. Alerts rail (flap-suppressed: fires after 3 heartbeats persisting,
clears after 2 clean; cleared alerts drop to audit). Header everywhere:
online/hb-only/offline counters + START/STOP ALL REC.

**Coverage bar (1g)**: 24 one-hour cells — steel recorded, red gap, hatched
unknown (node silent — NEVER rendered as fine). Camera view adds the
number-first header (1i): big %, "worst gap 1 h 40 m · ended 2 h ago".

**Status encodings**: solid green dot online; solid amber heartbeat-only;
hollow red offline (hollow = absence, never a live reading). Camera
RECONNECTING = amber pulsing dot + solid tag with duration + growing red run
in coverage + alert. Stale snapshot = dimmed + mono age stamp. Cold start =
centered "WAITING FOR HEARTBEATS" card with listener address + Enrollment
link.

**Camera view (tier 3)**: WebRTC sub-stream via the node's go2rtc, browser →
node direct over the tailnet. One live stream fleet-wide: selecting a sibling
camera hands the stream over; leaving releases it; no play affordance exists
at tiers 1–2. Node fact sheet rides along (status, disk-h, drift, recorder,
versions, TAIL LOGS). Heartbeat-only node → explainer instead of video, all
control-plane actions still work.

**Command flow (1d)**: confirm dialog states scope, target counts, and
reachability ("1 node offline — will queue, applies on reconnect"); CTA text
is the semantics verbatim: `SET RECORDING = ON`. Then a **persistent command
dock** pinned bottom-right, surviving navigation: COMMANDED n · ACKED n ·
QUEUED n · PENDING n, proportional bar, one row per node with live ack state.
Dismissed only by the operator; completion writes the audit entry.

**Enrollment**: per-hive tokens (a token only joins nodes into its own hive)
+ org-wide scope; generate shows the full token ONCE with the exact
config.yaml lines to paste; table shows masked value, scope, created,
enrolled count, status, REVOKE (stops future enrollments only). Rejected
connections panel (token fragment, source IP, count, last attempt) — the one
place a stranger/revoked install leaves a trace. Enrolled-nodes table with
individual removal. Tokens gate the control plane; the tailnet gates media.

**Audit**: append-only table (time UTC, operator = tailnet device identity in
v1, action, scope, result).

## Visual language (console only — node UI stays dark)
Light `#f2f2f3` ground, ink `#1d1f20`, hairlines `rgba(29,31,32,.16)`, square
corners, corner registration marks on major surfaces. Steel `#5980a6` =
structure/nav/"recorded" only. Functional: green `#2f7d5b` confirmed good,
amber `#b07818` degraded/queued, red `#b6423a` absence/failure + REC pulse.
Solid fill = breach, hairline outline = nominal, hollow = absence. Type:
Barlow Condensed headings over Barlow body (vendored woff2 or condensed
system fallback — NO runtime CDN fetch; field LAN has no internet),
monospace for all machine truth. Quiet mono tier line at the bottom of every
level. Phone: same DOM one column, sticky hive headers, targets ≥ 44 px.

## Node-side config (config.yaml additions)
```yaml
hive: sunrise-sub          # hive tag; prettified by the console
ops:
  url: wss://hive.tailnet:8090/ws
  token: "..."             # from Enrollment, shown once
```

## Hard rules carried over from FieldKit
Live = go2rtc sub-streams (/102) only; never relay or transcode; never
auto-download binaries; credentials only from config.yaml; graceful stops.
Recording bandwidth on the node always wins over viewing.

## Acceptance
1. Node with `ops:` config appears on the console at first heartbeat; cold
   start screen until then; node without `ops:` is unaffected.
2. Kill a node → hollow-red offline after 3 missed beats, cameras UNKNOWN,
   coverage hatches; reconnect → recovers, queued command applies + acks.
3. Camera stuck RECONNECTING shows dot+tag+duration, red coverage run, and
   an alert after 3 heartbeats; alert clears after 2 clean ones → audit.
4. START HIVE REC on a hive with one offline node: dock shows acked/queued
   correctly, never silently partial; audit entry written on completion.
5. Enrollment: generate token → paste into node config → node enrolls into
   its hive only; revoke → reconnect attempt lands in rejected ledger.
6. Tier rules hold: no live affordance at tiers 1–2; tier 3 plays WebRTC
   from the node's go2rtc; switching cameras hands over; leaving releases.
7. Console restart replays 24 h from JSONL; UI states recover.
8. Phone layout: one column, sticky hive headers, ≥ 44 px targets.
