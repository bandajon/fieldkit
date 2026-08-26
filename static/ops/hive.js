// Hive drill-down — the same nodes, one tier closer: full camera cards.
import {
  esc, dot, dur, frame, stub, drives, offload, coverage, thumb, timers, link, hhmm, hm,
  liveUrl, cached, modal, post,
} from './render.js';
import { ask } from './pages.js';

export const crumb = (hive, rest = '') =>
  `<span class="band-crumb"><a href="/" data-link>FLEET</a> › <b>${esc(hive.toUpperCase())}</b>${rest}</span>`;

// The two node-wide switches, in the console's existing chip idiom — lit is on. Each
// label carries what the switch COSTS, not just what it does: that is the decision an
// operator is actually making, and they make it from a phone without a price list.
const SWITCH = {
  mirror: ['UPLOAD RECORDINGS (HEAVY)', 'roughly 45 GB per camera per day, on a metered link'],
  contribute: ['CONTRIBUTE FRAMES (LIGHT)', 'under 90 MB per day for the whole site'],
};

const UPLOAD_COST = 'Every finished segment is copied to the cloud as it closes — roughly '
  + '<b>45 GB per camera per day</b> on a metered link. Contributing training frames is '
  + 'under 90 MB a day for a whole site; this is not that.';

/** Only a switch the node actually reported renders — a state we do not know is not
 *  drawn as OFF. Offline nodes keep theirs: the command waits and lands on reconnect. */
function switches(n) {
  const o = n.offload || {};
  const chips = Object.entries(SWITCH)
    .filter(([k]) => typeof o[k] === 'boolean')
    .map(([k, [label, cost]]) => `<button data-switch="${esc(n.key)}" data-what="${k}"
      data-on="${o[k]}" class="${o[k] ? 'on' : ''}" title="${esc(cost)}">${label} · ${
      o[k] ? 'ON' : 'OFF'}</button>`).join('');
  return chips ? `<span class="chips">${chips}</span>` : '';
}

/** Only the expensive direction asks: turning uploads ON spends real money on someone
 *  else's metered link. Turning them off, or contributing frames, gets no ceremony. */
export const needsConfirm = (what, on) => what === 'mirror' && on;

/**
 * Flip one switch. The chip does not move until the node's next heartbeat says it did —
 * the ticket tray carries the pending command, exactly as it does for start/stop.
 */
export async function switchClick(t, redraw) {
  const [hive, node] = t.dataset.switch.split('/');
  const on = t.dataset.on !== 'true';
  if (needsConfirm(t.dataset.what, on) && !await ask('Turn on uploads', UPLOAD_COST, true))
    return;
  try {
    await post('/api/command', { action: t.dataset.what, scope: { hive, node }, on });
  } catch (e) {
    modal(`<div class="dialog"><h4 class="dialog-title bad">FAILED</h4>
      <div class="dialog-body"><p class="say">${esc(e.message || e)}</p></div>
      <div class="dialog-actions"><button class="btn btn-ghost" data-close>Close</button></div></div>`);
  }
  redraw(true);
}

function card(n, cam, now) {
  const t = cam.until
    ? `<span class="chip">⏱ STOPS ${hhmm(cam.until)} · ${hm(cam.until - now)} LEFT</span>`
    : '<span class="dim">UNTIL STOPPED</span>';
  const log = (cam.log || []).slice(-3).join('\n');
  return `<a class="card-cam" href="/h/${encodeURIComponent(n.hive)}/${encodeURIComponent(n.node)}/${
      encodeURIComponent(cam.name)}" data-link>
    ${thumb(n.key, cam, now, n.hb_only, true)}
    <span class="name">${dot(cam.state === 'RECORDING' ? 'ok' : 'error')}${esc(cam.name)} · ${esc(cam.state)}${
      cam.state !== 'RECORDING' ? ' ' + dur((cam.minutes || 0) * 60) : ''}</span>
    ${t}
    ${coverage(cam, now)}
    <p class="logtail">${esc(log)}</p>
  </a>`;
}

function node(n, now) {
  const head = `<div class="node-hd">
    <span class="host">${dot(n.state)}${esc(n.node)}</span>
    <span class="dim">${esc((n.ips || []).join(' · ')) || 'no address reported'}</span>
    <span class="stack">${drives(n)}</span>
    <span class="stack">${offload(n)}</span>
    <span class="stack">${link(n, now)}</span>
    <span class="hd-act">${switches(n)}
      <button class="btn btn-ghost" data-log="${esc(n.key)}">Log tail</button>
      <button class="btn btn-ghost" data-startnode="${esc(n.key)}">Start node</button>
      <button class="btn btn-ghost btn-danger" data-stop="${esc(n.key)}">Stop node</button></span>
  </div>`;
  if (n.state === 'offline' || n.state === 'revoked') {
    const label = n.state === 'revoked' ? 'REVOKED — HEARTBEATS REJECTED'
      : `OFFLINE ${dur(now - n.last_seen)} · last hb ${hhmm(n.last_seen)}`;
    return frame(head + `<div class="node-hd"><span class="bad">${label}</span>
      <span class="dim">last state: ${n.cameras.filter(c => c.state === 'RECORDING').length
        } cams recording — no thumbs fetched</span></div>`, 'node');
  }
  const warn = n.hb_only
    ? `<div class="node-hd"><span class="warn">HB ONLY — NO MEDIA PATH</span>
       <span class="dim">control channel up, media path down — no live video, no on-demand
       snapshots. Heartbeat snapshots only.</span></div>`
    : '';
  const cards = n.cameras.length
    ? `<div class="cards">${n.cameras.map(c => card(n, c, now)).join('')}</div>`
    : '<div class="node-hd"><span class="dim">NO CAMERAS REPORTED</span></div>';
  const t = timers(n, now);
  return frame(head + warn + cards + (t ? `<div class="node-hd">${t}</div>` : ''), 'node');
}

export function hiveView(doc, name, now) {
  const h = doc.hives.find(x => x.hive === name);
  if (!h) return `<div class="empty">NO HIVE ${esc(name.toUpperCase())}<br
    ><span class="dim">it has no enrolled node reporting — <a href="/" data-link>back to fleet</a></span></div>`;
  const cams = h.nodes.reduce((a, n) => a + n.cameras.length, 0);
  return `<div class="pagehd"><h3>${esc(name.toUpperCase())}</h3>
      <span class="dim mono">${h.nodes.length} nodes · ${cams} cams · ${h.online} online</span>
      <button class="btn btn-primary" data-start="${esc(name)}">Start hive recording</button>
      <button class="btn btn-ghost btn-danger" data-stophive="${esc(name)}">Stop hive</button></div>`
    + h.nodes.map(n => node(n, now)).join('');
}

export const find = (doc, hive, name) =>
  (doc.hives.find(x => x.hive === hive) || { nodes: [] }).nodes.find(n => n.node === name);

/**
 * Camera focus. Live is one iframe straight to the node's own go2rtc over the tailnet —
 * the console never proxies video. paintLive() enforces the one-stream rule.
 */
export function cameraView(doc, hive, nodeName, camName, now, redraw) {
  const n = find(doc, hive, nodeName);
  const cam = n && n.cameras.find(c => c.name === camName);
  if (!cam) return stub(`${hive} › ${nodeName} › ${camName} — not reporting`);
  const others = n.cameras.filter(c => c.name !== camName);
  const rec = cam.state === 'RECORDING';
  const down = n.state === 'offline' || n.state === 'revoked';
  const url = down || n.hb_only ? '' : liveUrl(n, camName);
  const why = down ? `NODE ${n.state.toUpperCase()} — NO LIVE STREAM, LAST HEARTBEAT FRAME BELOW`
    : n.hb_only ? 'HB ONLY — NO MEDIA PATH, HEARTBEAT SNAPSHOT BELOW'
    : url ? '' : 'NODE REPORTS NO ADDRESS — NO LIVE STREAM';
  const gaps = (cam.gaps || []).map(([a, b]) => `${hhmm(a)}–${hhmm(b)} (${dur(b - a)})`).join(' · ');
  const log = (cached(`/api/logs/${encodeURIComponent(hive)}/${encodeURIComponent(nodeName)}`,
    redraw) || {});
  const lines = ((log.cameras || {})[camName] || []).slice(-12).join('\n');

  return `<div class="stage">
    <div class="stage-img">
      ${url ? `<div class="dim livenote">LIVE VIA NODE GO2RTC — blank means go2rtc is off on the
          node or no tailnet path; snapshots below are live either way.</div>
        <div class="live" data-live="${esc(url)}"
          ><span class="stage-tag">${dot('ok')}LIVE · SUB-STREAM · ${esc(camName)}</span></div>
        <div class="snapstrip"><h6>SNAPSHOT · 2 S</h6>${thumb(n.key, cam, now, n.hb_only)}</div>`
      : `${thumb(n.key, cam, now, n.hb_only, true)}
        <span class="stage-tag">${dot(down ? 'offline' : 'warn')}${esc(why)}</span>`}
    </div>
    <div class="stage-strip">
      <div><h6>STATE</h6><span>${dot(rec ? 'ok' : 'error')}${esc(cam.state)} ${dur((cam.minutes || 0) * 60)}</span></div>
      <div><h6>RESTARTS</h6><span>${esc(cam.restarts)}</span></div>
      <div><h6>MAIN STREAM</h6><span>records on node — never streamed here</span></div>
      <div><h6>DISK</h6><span>${n.worst_hours == null ? '—' : esc(n.worst_hours) + ' h left'}</span></div>
    </div>
    <div class="stage-cov"><h6>COVERAGE — LAST 24 H</h6>${coverage(cam, now)}
      <div class="dim">${gaps ? `<span class="bad">gaps: ${esc(gaps)}</span>` : 'no gaps recorded'}</div></div>
    <div class="stage-log"><h6>LOG TAIL — ${esc(camName)}</h6>
      <pre class="log-body">${esc(lines) || '(no lines for this camera yet)'}</pre></div>
    <div class="stage-side">
      <h6>OTHER CAMS — ${esc(nodeName)}</h6>
      ${others.map(c => `<a class="card-cam" href="/h/${encodeURIComponent(hive)}/${
        encodeURIComponent(nodeName)}/${encodeURIComponent(c.name)}" data-link>
        ${thumb(n.key, c, now, n.hb_only, true)}
        <span class="name">${dot(c.state === 'RECORDING' ? 'ok' : 'error')}${esc(c.name)} —
          ${c.state === 'RECORDING' ? 'SWITCH (CLOSES THIS STREAM)' : esc(c.state)}</span></a>`).join('')
        || '<span class="dim">no other cameras on this node</span>'}
      <div class="stage-act">
        <button class="btn btn-ghost" data-log="${esc(hive + '/' + nodeName)}">Node log tail</button>
        <button class="btn btn-ghost btn-danger" data-stop="${esc(hive + '/' + nodeName)}"
          data-cam="${esc(camName)}">Stop this camera</button></div>
    </div>
    <p class="stage-foot">ONE LIVE STREAM AT A TIME — RECORDING BANDWIDTH ON THE NODE ALWAYS WINS.</p>
  </div>`;
}
