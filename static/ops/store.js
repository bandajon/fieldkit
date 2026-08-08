// The fleet document and the one live connection that keeps it fresh.
// GET /api/fleet paints the first frame; GET /api/stream (SSE) supplies every one after.

export const state = { doc: null, at: 0, live: false };

const RETRY_MAX = 30000;
let onchange = () => {};
let retry = 1000;
let es = null;

// Per node, the last offload numbers we saw — "uploading" is a delta, not a field.
const seen = new Map();

export function mark(doc) {
  for (const h of doc.hives) {
    for (const n of h.nodes) {
      for (const c of n.cameras) {
        // Absolute epoch of the frame, so its age stays right between SSE frames.
        c.snap_at = c.snapshot_age == null ? null : doc.now - c.snapshot_age;
      }
      const o = n.offload || {};
      if (!o.enabled) continue;
      const was = seen.get(n.key);
      const since = !was || o.last_file !== was.file ? doc.now : was.since;
      // Uploading = the counter moved this frame, or the current file is under 2 min old.
      n.uploading = !!was && (o.uploaded > was.uploaded || doc.now - since < 120);
      seen.set(n.key, { uploaded: o.uploaded || 0, file: o.last_file || null, since });
    }
  }
  return doc;
}

function accept(doc) {
  state.doc = mark(doc);
  state.at = Date.now() / 1000;
  onchange();
}

/** Console-side epoch, drifting forward from the last frame so ages tick between them. */
export const clock = () =>
  state.doc ? state.doc.now + (Date.now() / 1000 - state.at) : Date.now() / 1000;

function open() {
  es = new EventSource('/api/stream');
  es.onmessage = e => {
    retry = 1000;
    state.live = true;
    accept(JSON.parse(e.data));
  };
  es.onerror = () => {
    es.close();
    state.live = false;
    onchange();
    // Every stream opens with a whole fleet doc, so reconnecting IS the full refetch.
    retry = Math.min(retry * 2, RETRY_MAX);
    setTimeout(open, retry);
  };
}

export async function connect(cb) {
  onchange = cb;
  try {
    accept(await (await fetch('/api/fleet')).json());
  } catch {
    // No first paint; the stream's opening frame covers it.
  }
  open();
}
