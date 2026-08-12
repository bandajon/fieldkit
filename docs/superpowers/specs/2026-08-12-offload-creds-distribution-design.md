# Offload credential distribution — ops console → nodes

2026-08-12. Approved design.

## Problem

FieldKit's R2 offload (`offload:` block) needs S3 credentials hand-edited into
every node's config.yaml. Nodes already hold an authenticated outbound
WebSocket to the ops console (Railway); the console should hand credentials to
nodes so offload works without per-node secret management.

## Decisions (made with Jonah)

- **Memory only.** Credentials are never written to a node's disk. A stolen
  field box leaks nothing; rotating the Railway env vars rotates the fleet on
  next reconnect. A node that boots offline records normally and offloads
  once it reaches the console again (offload needs internet anyway).
- **The node's `offload.enabled` stays the only switch.** The console supplies
  credentials; it never turns offloading on. Creds arriving while
  `enabled: false` sit unused in memory.

## Mechanism

One new console→node frame, delivered over the existing `/ingest` WebSocket
(protocol v1, additive):

```json
{"type": "offload_creds", "account_id": "…", "access_key_id": "…",
 "secret_access_key": "…", "bucket": "fieldkit-recordings"}
```

### Console (ops.py)

- Reads `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`,
  `R2_BUCKET` (default `fieldkit-recordings`) from the environment
  (Railway service variables).
- Sends the frame once per WS session, immediately after the node's first
  accepted heartbeat. Any env var missing → frame never sent; console
  behaves exactly as before.

### Node (hive.py)

- On `offload_creds`: fill **only the empty fields** of the in-memory
  `CONFIG["offload"]` (config.yaml values always win — an operator override
  is never clobbered). Never writes config.yaml. Logs one line:
  `offload creds received from console`.
- `offload.Offload` holds CONFIG by reference, so filled creds take effect
  live, no restart.
- Unknown-frame tolerance already exists on old nodes (`msg.get("type")`
  dispatch), so mixed fleets are safe.

### Operator-facing status

When `offload.enabled` is true, `ops:` is configured, and keys are empty,
offload's status line reads `awaiting credentials from console` instead of a
generic not-configured message. Bad credentials surface through offload's
existing error reporting (already carried in heartbeats).

## Failure modes

| Situation | Behaviour |
|---|---|
| Console unreachable at boot | Records normally; offload idle with the awaiting-creds status; creds arrive on reconnect |
| Env vars unset on Railway | Frame never sent; nodes behave exactly as today |
| Creds arrive, `enabled: false` | Held in memory, unused, until operator enables locally |
| Bad/rotated-away creds | Offload errors surface in status + heartbeat as today |
| Operator hand-fills config.yaml | Those values win; console fills only blanks |

## Testing

Same stdlib-assert style as both repos:

- hive.py inline tests: `offload_creds` fills only empty fields; no
  config.yaml write; frame ignored gracefully when malformed.
- ops.py test: frame emitted once after first heartbeat iff all env vars set.
- offload status copy test: awaiting-creds message appears only in the
  enabled+ops+empty-keys state.

## Deployment

- Set the four `R2_*` variables on the Railway `fieldkit` service
  (production env) — the token scoped to countkit-crops + fieldkit-recordings.
- Deploy flows through the normal Railway pipeline (push to main).

## Out of scope (YAGNI)

Per-hive buckets, credential TTL/refresh frames, console-controlled enable,
CountKit credential distribution (already configured directly).
