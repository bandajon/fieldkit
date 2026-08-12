# Hive protocol — node ⇄ ops console contract (v1)

The node opens an **outbound WebSocket** to the console (`ops.url` in
config.yaml, e.g. `ws://100.x.y.z:8090/ingest`) and keeps it open; the console
never dials a node. Reconnect with backoff (2 s → 60 s). All frames are JSON
text.

## Node → console

### heartbeat (every 30 s, and immediately on connect and after applying a command)

```json
{
  "type": "heartbeat",
  "token": "<enrollment token>",
  "hive": "<hive tag from config.yaml ops.hive>",
  "node": "<hostname>",
  "seq": 41,
  "sent": 1754820000.123,
  "ips": ["192.168.1.10", "100.64.0.7"],
  "versions": {"fieldkit": "<git sha or ''>", "python": "3.12.1"},
  "record": { ... verbatim GET /api/record/status body: cameras (state,
              until, minutes, restarts, log), disks, offload,
              disk_free_gb, disk_total_gb ... },
  "coverage": {"cam1": {"pct": 97.4, "gaps": [[1754800000, 1754800612]]}},
  "snapshots": {"cam1": "<base64 jpeg ~20KB>"},
  "log": ["last ~20 recorder/app lines"]
}
```

- `snapshots` present only every 2nd heartbeat (and on the first after
  connect); omitted otherwise. The UI's stale threshold assumes this ~60 s
  cadence — change them together. Sub-stream-sized (camera.snapshot downscaled
  is NOT required in v1 — send what `/api/camera/snapshot` returns if under
  ~60 KB, else skip that camera this round).
- `coverage` is 24 h of segment arithmetic per camera: expected 600 s
  wallclock-named segments vs present files; `gaps` = missing intervals
  (epoch pairs), capped at 50 entries, oldest dropped first.
- Console computes clock drift as `recv_time - sent` (one-way, good enough
  at ±3 s granularity) and link RTT from WS ping/pong.

### ack (response to a command)

```json
{"type": "ack", "cmd_id": "0142", "ok": true,
 "applied": ["cam1", "cam2"], "error": ""}
```

## Console → node

### command (desired state, sent when issued AND re-sent on node reconnect
if still unacked — the node result is idempotent)

```json
{"type": "command", "cmd_id": "0142", "action": "start",
 "cams": null, "hours": 4.0}
```

- `action`: `start` | `stop`. `cams: null` = all configured cameras.
- `hours` only meaningful on start; null = until stopped.
- Node applies via its own recorder (same code path as the local UI) and
  replies with `ack`, then an immediate fresh heartbeat.

### offload_creds (R2 credentials the console holds on the node's behalf)

```json
{"type": "offload_creds", "account_id": "…", "access_key_id": "…",
 "secret_access_key": "…", "bucket": "fieldkit-recordings"}
```

- Sent once per session, right after the first accepted heartbeat, and only
  when the console's `R2_ACCOUNT_ID` / `R2_ACCESS_KEY_ID` /
  `R2_SECRET_ACCESS_KEY` env vars are all set. Never audited or persisted
  console-side.
- The node keeps them in a separate in-memory overlay, never in its own config:
  each frame **replaces** the overlay wholesale, so rotating the key on the
  console reaches a node on its next connect. At read time the node merges
  overlay under config.yaml — every offload field the operator actually set
  wins. Nothing is written to disk, so a reboot drops them and reconnect
  re-delivers.
- No ack — fire-and-forget. A node running older code ignores the unknown
  type and keeps listening, so the console can push to a mixed fleet.
- **These creds are only as protected as the enrollment token.** Anyone
  holding a hive's token can connect and be handed them. A lost or stolen box
  means revoking that token on the console's Tokens page, which cuts off both
  commands and credentials; rotate the R2 key too if the box was out of your
  hands.

### rejected (console → node, then console closes the socket)

```json
{"type": "rejected", "reason": "unknown or revoked token"}
```

Node logs it, shows it on its local status API, and retries hourly (a
revoked node must not hammer).

## Console HTTP API (UI-facing, same origin)

- `GET  /api/fleet` — full state: hives → nodes → cameras, computed health,
  alerts summary, active tickets. The UI's single input.
- `GET  /api/stream` — SSE; event per state change (heartbeat applied,
  ack, alert fired/cleared). Payload = same shape as /api/fleet (small
  fleets; no diffing in v1).
- `GET  /api/snapshot/{hive}/{node}/{cam}` — last cached JPEG (age header).
- `POST /api/command` — `{scope: {hive?, node?}, action, cams?, hours?}` →
  creates ticket, fans out to matching connected nodes, stores desired
  state for offline ones. Returns the ticket.
- `GET  /api/tickets` — active + last 20 finished.
- `POST /api/tokens` — `{hive}` → creates token (full value returned ONCE).
- `POST /api/tokens/{id}/rotate` / `POST /api/tokens/{id}/revoke`
- `GET  /api/tokens` — masked list + per-hive enrolled roster.
- `POST /api/roster/{hive}/{node}/remove` (de-list, re-enrollable) /
  `.../forget` (erase a revoked node).
- `GET  /api/alerts` — firing + cleared-within-24 h, each with state
  PENDING (n/3) / FIRING / CLEARED.
- `GET  /api/audit?limit=200` — audit rows, newest first.
- `GET  /api/logs/{hive}/{node}` — last log lines from latest heartbeat.

## Console persistence (files beside ops.py, all JSONL/JSON, no DB)

- `ops_state/tokens.json` — token records (never the full value after
  creation; store sha256 of token; nodes send the raw value, console hashes
  to match).
- `ops_state/desired.json` — per-node stored desired state for offline nodes.
- `ops_state/audit.jsonl` — append-only: commands, acks, token ops, alert
  fire/clear.
- `ops_state/heartbeats.jsonl` — one line per heartbeat, WITHOUT snapshots;
  used for 24 h replay on cold start (rows render dimmed "last known,
  unconfirmed" until a live heartbeat confirms).

## Config

Node (`config.yaml`):

```yaml
ops:                # absent = node never phones home (self-contained mode)
  url: ""           # ws://<console>:8090/ingest
  token: ""         # enrollment token from the console's /enroll page
  hive: ""          # hive tag, e.g. kalambo
```

Console (`ops_config.yaml`, created from example on first boot):

```yaml
port: 8090
webhook_url: ""     # optional; alert fire/clear POSTs {text: "..."}
disk_amber_hours: 6
```
