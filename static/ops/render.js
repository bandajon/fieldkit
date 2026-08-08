// Pure render helpers — HTML strings in, no DOM. Everything a node/hive/camera
// names is remote input, so it goes through esc().

export const esc = s => String(s ?? '').replace(/[&<>"]/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

export const dot = st => `<span class="dot d-${esc(st)}"></span>`;

export const tail = p => String(p ?? '').split(/[\\/]/).filter(Boolean).pop() || String(p ?? '');

/** Coarse, uppercase, one unit — the ledger never shows "2 h 14 m 6 s". */
export function dur(s) {
  s = Math.max(0, Math.round(s || 0));
  if (s < 90) return `${s} S`;
  if (s < 5400) return `${Math.round(s / 60)} MIN`;
  if (s < 172800) return `${(s / 3600).toFixed(1)} H`;
  return `${Math.round(s / 86400)} D`;
}

// Local wall clock, 24 h — an operator reads it against their own watch, and HH:MM
// never has to be re-parsed past an AM/PM.
export const hhmm = ep =>
  new Date(ep * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });

/** Countdown as H:MM. */
export function hm(sec) {
  sec = Math.max(0, Math.round(sec));
  return `${Math.floor(sec / 3600)}:${String(Math.floor(sec / 60) % 60).padStart(2, '0')}`;
}

export const camAge = (cam, now) => cam.snap_at == null ? null : now - cam.snap_at;

// Nodes send a still every 2nd 30 s heartbeat; stale = twice that cadence missed.
export const SNAP_EVERY = 60;

// --- camera bits ---

export function timers(node, now) {
  const timed = node.cameras.filter(c => c.until);
  if (!timed.length) return '';
  const soonest = Math.min(...timed.map(c => c.until));
  const open = node.cameras.some(c => !c.until && c.state === 'RECORDING');
  return `<b class="chip">⏱ STOPS ${hhmm(soonest)} · ${hm(soonest - now)} LEFT</b>`
    + (open ? ` <b class="chip warn">MIXED</b>` : '');
}

export function thumb(nodeKey, cam, now, hb, big) {
  const age = camAge(cam, now);
  const stale = age != null && age > 2 * SNAP_EVERY;
  const label = age == null ? 'NO SNAP' : dur(age) + (hb ? ' · HB' : '');
  return `<span class="thumb${stale ? ' stale' : ''}${big ? ' big' : ''}"
    data-snap="${esc(nodeKey + '/' + cam.name)}"
    ><span class="agechip${stale || hb ? ' warn' : ''}">${esc(label)}</span></span>`;
}

/** 24 discrete hour-cells (design 1g) against the window ending now. A cell reads
 * as a gap from half a segment (300 s) of missing footage in its hour; the exact
 * intervals are on the camera view, and each cell's title carries its minutes. */
export function coverage(cam, now, unknown) {
  const t0 = now - 86400;
  const miss = Array(24).fill(0);
  for (const [a, b] of cam.gaps || []) {
    const lo = Math.max(a, t0), hi = Math.min(b, now);
    for (let h = Math.max(0, Math.floor((lo - t0) / 3600));
         h < 24 && t0 + h * 3600 < hi; h++) {
      miss[h] += Math.max(0, Math.min(hi, t0 + (h + 1) * 3600) - Math.max(lo, t0 + h * 3600));
    }
  }
  const cells = miss.map((s, h) => `<i class="${s >= 300 ? 'gap' : ''}" title="${
    24 - h} h ago${s ? ` · ${Math.round(s / 60)} min missing` : ''}"></i>`).join('');
  const pct = cam.coverage_pct == null ? '—' : `${cam.coverage_pct}%`;
  return `<span class="cov${unknown ? ' unknown' : ''}"><span class="bar">${cells}</span
    ><span class="pct">${esc(pct)}</span></span>`;
}

// --- node bits ---

/** One line per filesystem — never merged, never a percentage. */
export function drives(n) {
  if (!n.disks.length) return '<span class="dim">—</span>';
  const low = n.issues.includes('disk low');
  return n.disks.map(d => {
    const worst = d.hours_left != null && d.hours_left === n.worst_hours;
    const h = d.hours_left == null ? '—' : d.hours_left;
    return `<span class="${low && worst ? 'bad' : 'dim'}">${esc(h)} H LEFT · ${
      esc(d.free_gb)}/${esc(d.total_gb)} GB — ${esc(tail(d.path))}</span>`;
  }).join('');
}

export function offload(n) {
  const o = n.offload || {};
  if (!o.enabled) return '<span class="dim">—</span>';
  if (o.last_error) return `<span class="bad">OFFLOAD FAILED — ${esc(o.last_error)}</span>`;
  if (n.uploading) return `<span class="accent">▲ UPLOADING ${esc(tail(o.last_file))}</span>`;
  return '<span class="dim">✓ OFFLOAD IDLE</span>';
}

/** The one thing a phone row shows: what is worst about this node, in words. */
export function signal(n, now) {
  if (n.state === 'revoked') return '<span class="bad">REVOKED</span>';
  if (n.state === 'offline') return `<span class="bad">OFFLINE ${dur(now - n.last_seen)}</span>`;
  if (n.hb_only) return '<span class="warn">HB ONLY — NO MEDIA PATH</span>';
  const bad = n.cameras.find(c => c.state !== 'RECORDING');
  if (bad) return `<span class="bad">${esc(bad.name)} ${esc(bad.state)} ${dur((bad.minutes || 0) * 60)}</span>`;
  if (n.issues.includes('disk low')) return `<span class="warn">${esc(n.worst_hours)} H DISK</span>`;
  if (n.issues.length) return `<span class="warn">${esc(n.issues[0].toUpperCase())}</span>`;
  return '<span class="ok">OK</span>';
}

/** Last-heartbeat cell: age plus the link signals that only matter when they are bad. */
export function link(n, now) {
  const out = [`<span class="${n.state === 'offline' ? 'bad' : 'dim'}">${dur(now - n.last_seen)}</span>`];
  if (n.clock_drift != null) out.push(`<span class="warn">CLOCK ±${esc(n.clock_drift)}s</span>`);
  if (n.link.miss_pct > 5) out.push(`<span class="warn">${esc(n.link.miss_pct)}% MISSED</span>`);
  // rtt_ms is always null in v1 — the protocol has no console→node ping.
  out.push(`<span class="dim">RTT ${n.link.rtt_ms == null ? '—' : esc(n.link.rtt_ms) + ' MS'}</span>`);
  if (!n.confirmed) out.push('<span class="dim">LAST KNOWN, UNCONFIRMED</span>');
  return out.join('');
}

export const frame = (inner, cls = '') =>
  `<div class="blueprint ${cls}"><i class="corner tl"></i><i class="corner tr"></i
    ><i class="corner bl"></i><i class="corner br"></i>${inner}</div>`;

// --- chrome ---

export function nav(doc, path) {
  const a = doc.rollup.alerts_firing + doc.rollup.alerts_pending;
  const cur = p => path === p ? ' aria-current="page"' : '';
  return `<a href="/" data-link${cur('/')}>FLEET</a>
    <a href="/alerts" data-link${cur('/alerts')}>ALERTS <span class="count${a ? '' : ' zero'}">${a}</span></a>
    <a href="/audit" data-link${cur('/audit')}>AUDIT</a>
    <a href="/enroll" data-link${cur('/enroll')}>ENROLL</a>`;
}

export function rollup(doc) {
  const r = doc.rollup;
  return `<div><h6>NODES</h6><span><b class="ok">${r.online}</b> online · <b class="bad">${
      r.offline}</b> offline · ${r.nodes} known</span></div>
    <div><h6>CAMERAS</h6><span><b class="ok">${r.recording}</b> recording / ${r.cams}</span></div>
    <div><h6>ALERTS</h6><span><a href="/alerts" data-link>${r.alerts_firing} firing · ${
      r.alerts_pending} pending</a></span></div>
    <div><h6>TICKETS</h6><span><button class="linkish" data-tickets>${
      r.tickets_active} active</button></span></div>
    <div class="strip-act"><button class="btn btn-secondary" data-start="">Start recording…</button>
      <button class="btn btn-ghost btn-danger" data-stophive="">Stop…</button></div>`;
}

export const empty = () => `<div class="empty">NO NODES ENROLLED<br
  ><span class="dim">create a token at <a href="/enroll" data-link>/enroll</a>, paste it into the
  node's config.yaml, and its next heartbeat lands here</span></div>`;

export const stub = what => `<div class="empty">${esc(what)}<br
  ><span class="dim">camera focus — task 4</span></div>`;

/** Native <dialog>: modal semantics, focus trap and Escape come free. */
export function modal(html, cls = '') {
  const d = document.createElement('dialog');
  d.className = 'sheet ' + cls;
  d.innerHTML = html;
  document.body.append(d);
  d.addEventListener('close', () => d.remove());
  d.addEventListener('click', e => {
    if (e.target === d || e.target.closest('[data-close]')) d.close();     // backdrop or Close
  });
  d.showModal();
  return d;
}

/** Read-through cache for the endpoints outside the SSE doc; refetches when stale. */
const CACHE = new Map();
export function cached(url, onload, ttl = 5000) {
  const c = CACHE.get(url) || {};
  if (!(Date.now() - (c.at || 0) < ttl)) {
    CACHE.set(url, { ...c, at: Date.now() });
    fetch(url).then(r => r.json())
      .then(data => { CACHE.set(url, { at: Date.now(), data }); onload(); })
      .catch(() => CACHE.set(url, { ...c, at: 0 }));   // let the next draw retry
  }
  return c.data;
}

/** After a write, the cached read is stale by definition. */
export const bust = () => CACHE.clear();

export async function post(url, body) {
  const r = await fetch(url, { method: 'POST', headers: { 'content-type': 'application/json' },
                               body: JSON.stringify(body || {}) });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.detail || r.statusText);
  return d;
}

// --- thumb painting (the only stateful part; keeps one <img> per camera alive
//     across re-renders so a frame never refetches or flashes) ---

const imgs = new Map();
const snapUrl = k => '/api/snapshot/' + k.split('/').map(encodeURIComponent).join('/') + '?t=' + Date.now();

export function paintThumbs(root) {
  for (const box of root.querySelectorAll('[data-snap]')) {
    const key = box.dataset.snap;
    let img = imgs.get(key);
    if (!img) {
      img = new Image();
      img.alt = '';
      img.onerror = () => img.classList.add('broken');   // hatch placeholder, never a broken icon
      img.onload = () => img.classList.remove('broken');
      img.src = snapUrl(key);
      imgs.set(key, img);
    }
    box.prepend(img);
  }
}

/** The node's own go2rtc, direct over the tailnet — the console never proxies video. */
export function liveUrl(n, cam) {
  const ip = (n.ips || []).find(x => x.startsWith('100.')) || (n.ips || [])[0];
  return ip ? `http://${ip}:1984/stream.html?src=${encodeURIComponent(cam)}` : '';
}

// Exactly one live stream exists app-wide. The element is kept across re-renders so an
// SSE frame doesn't restart the stream; any change of src, or a screen with no [data-live]
// at all (navigation away), tears it down first.
let liveEl = null, liveSrc = '';

export function paintLive(root) {
  const box = root.querySelector('[data-live]');
  const src = box ? box.dataset.live : '';
  if (src !== liveSrc) killLive();
  if (!src) return;
  if (!liveEl) {
    liveEl = document.createElement('iframe');
    liveEl.allow = 'autoplay; fullscreen';
    liveEl.src = src;
    liveSrc = src;
  }
  box.prepend(liveEl);
}

export function killLive() {
  if (liveEl) liveEl.remove();
  liveEl = null;
  liveSrc = '';
}

/** Only cameras currently on screen refresh — folded hives and offline rows render no box. */
export function thumbTick(root) {
  for (const box of root.querySelectorAll('[data-snap]')) {
    const img = imgs.get(box.dataset.snap);
    if (img) img.src = snapUrl(box.dataset.snap);
  }
}
