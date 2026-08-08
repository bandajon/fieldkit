// The fleet ledger: hive blocks worst-first, node rows inside them.
import {
  esc, dot, dur, hhmm, frame, empty, signal, link, drives, offload, coverage, thumb, timers,
} from './render.js';

/** Hives the operator has clicked open. Only all-green hives fold in the first place. */
export const expanded = new Set();

export const folds = h => h.state === 'ok';
export const isOpen = h => !folds(h) || expanded.has(h.hive);

const worstCam = n => n.cameras.reduce(
  (a, c) => a == null || (c.coverage_pct ?? 101) < (a.coverage_pct ?? 101) ? c : a, null);

function camsCell(n, now) {
  if (n.state === 'revoked') return '<span class="bad">REVOKED — HEARTBEATS REJECTED</span>';
  if (n.state === 'offline') {
    const was = n.cameras.filter(c => c.state === 'RECORDING').length;
    return `<span class="bad">OFFLINE ${dur(now - n.last_seen)} — NO THUMBS</span>
      <span class="dim">LAST STATE ${was} CAMS REC · ${hhmm(n.last_seen)}</span>`;
  }
  const thumbs = `<span class="thumbs">${
    n.cameras.map(c => thumb(n.key, c, now, n.hb_only)).join('')}</span>`;
  // Checked before the empty-camera case: a heartbeat-only node carries no record
  // payload at all, so its camera list is always empty — the reason is the row.
  if (n.hb_only) return `<span class="warn">HB ONLY — NO MEDIA PATH</span>${thumbs}`;
  if (!n.cameras.length) return '<span class="dim">NO CAMERAS REPORTED</span>';
  const bad = n.cameras.filter(c => c.state !== 'RECORDING');
  const rec = n.cameras.length - bad.length;
  const parts = bad.map(c =>
    `<span class="bad">${dot('error')} ${esc(c.name)} ${esc(c.state)} ${dur((c.minutes || 0) * 60)}</span>`);
  if (rec) parts.push(`<span>${dot('ok')} ${rec} CAM${rec > 1 ? 'S' : ''} REC</span>`);
  const t = timers(n, now);
  return `<span>${parts.join(' · ')}${t ? ' · ' + t : ''}</span>${thumbs}`;
}

export function row(n, now, open) {
  const down = n.state === 'offline' || n.state === 'revoked';
  const low = n.issues.includes('disk low');
  const tint = down || (low && (n.offload || {}).last_error) ? ' t-bad' : low ? ' t-warn' : '';
  const cam = worstCam(n);
  return `<div class="cols row${tint}${n.confirmed ? '' : ' unconfirmed'}">
    <a class="host" href="/h/${encodeURIComponent(n.hive)}" data-link>${dot(n.state)}${esc(n.node)}</a>
    <span class="stack phone-hide">${open ? camsCell(n, now) : signal(n, now)}</span>
    <span class="stack phone-hide">${cam ? coverage(cam, now, down) : '<span class="dim">—</span>'}</span>
    <span class="stack phone-hide">${drives(n)}</span>
    <span class="stack phone-hide">${offload(n)}</span>
    <span class="stack phone-hide">${link(n, now)}</span>
    <span class="signal">${signal(n, now)}</span>
  </div>`;
}

function block(h, now) {
  const open = isOpen(h);
  const cams = h.nodes.reduce((a, n) => a + n.cameras.length, 0);
  const problems = h.nodes.filter(n => n.issues.length).length;
  const summary = problems
    ? `<span class="bad">${dot(h.state)} ${problems} PROBLEM${problems > 1 ? 'S' : ''}</span>`
    : `<span class="ok">${dot('ok')} ALL HEALTHY</span>`;
  // A div, not a button: the hive actions are buttons and cannot nest inside one.
  const head = `<div class="hive-hd" role="button" tabindex="0" data-hive="${esc(h.hive)}">
      <h4>${esc(h.hive.toUpperCase())}</h4>
      <span class="meta">${h.nodes.length} NODES · ${cams} CAMS · ${h.online} ONLINE</span>
      ${summary}
      <a class="more" href="/h/${encodeURIComponent(h.hive)}" data-link>OPEN HIVE ▸</a>
      <span class="hd-act"><button class="btn btn-ghost" data-start="${esc(h.hive)}">Start hive</button>
        <button class="btn btn-ghost btn-danger" data-stophive="${esc(h.hive)}">Stop hive</button>
        <span class="more fold">${open ? 'FOLD ▾' : 'EXPAND ▸'}</span></span>
    </div>`;
  const body = open ? `<div class="cols head phone-hide"><span>NODE</span><span>CAMERAS · THUMBS 10 S</span
      ><span>COVERAGE 24H</span><span>DRIVES · HOURS LEFT</span><span>OFFLOAD</span><span>LAST HB</span></div>
    ${h.nodes.map(n => row(n, now, true)).join('')}` : '';
  return frame(head + body, `hive${open ? ' open' : ''}`);
}

export function fleetView(doc, now) {
  if (!doc.rollup.nodes) return empty();
  return doc.hives.map(h => block(h, now)).join('');
}
