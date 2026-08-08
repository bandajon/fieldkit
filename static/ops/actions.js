// Commands: scope + duration confirm, and the ticket tray that outlives it.
import { state } from './store.js';
import { esc, dot, dur, hhmm, modal, post } from './render.js';

const PRESETS = [['UNTIL STOPPED', null], ['2 H', 2], ['4 H', 4], ['8 H', 8]];

const all = () => (state.doc?.hives || []).flatMap(h => h.nodes);

const match = scope => all().filter(n =>
  (!scope.hive || n.hive === scope.hive) && (!scope.node || n.node === scope.node)
  && n.state !== 'revoked');

/** Counts straight off the fleet doc; connected = anything not offline or revoked. */
export function forecast(scope, cams) {
  const ns = match(scope);
  const offline = ns.filter(n => n.state === 'offline').length;
  return {
    nodes: ns.length,
    connected: ns.length - offline,
    offline,
    cams: cams ? ns.length * cams.length : ns.reduce((a, n) => a + n.cameras.length, 0),
  };
}

export function summary(action, scope, cams, hours, at = Date.now() / 1000) {
  const f = forecast(scope, cams);
  const when = action !== 'start' ? 'stop writing footage'
    : hours ? `${hours} h — stops ${hhmm(at + hours * 3600)}` : 'until stopped';
  return `${f.nodes} node${f.nodes === 1 ? '' : 's'} · ${f.cams} camera${
    f.cams === 1 ? '' : 's'} · ${when}`;
}

export const gate = (level, action, typed) =>
  !(level === 'fleet' && action === 'stop') || typed === 'STOP ALL';

export function confirm(action, scope, cams) {
  // Node scope preselects the node; a hive button starts at its hive; the fleet strip
  // has nowhere narrower to start.
  const st = { level: scope.node ? 'node' : scope.hive ? 'hive' : 'fleet',
               hours: null, custom: false, typed: '' };
  const d = modal('');

  const scoped = () => st.level === 'fleet' ? {}
    : st.level === 'hive' ? { hive: scope.hive } : scope;

  const paint = () => {
    const sc = scoped();
    const f = forecast(sc, cams);
    const ok = gate(st.level, action, st.typed);
    const opt = (level, label) => `<button data-level="${level}" class="${
      st.level === level ? 'on' : ''}">${label}</button>`;
    d.innerHTML = `<div class="dialog">
      <h4 class="dialog-title ${action === 'stop' ? 'bad' : ''}">${
        action === 'stop' ? 'STOP' : 'START'} RECORDING${cams ? ' — ' + esc(cams.join(', ')) : ''}</h4>
      ${st.level === 'fleet' && !scope.hive ? '' : `<div class="seg2">
        ${scope.node ? opt('node', 'THIS NODE · ' + esc(scope.node.toUpperCase())) : ''}
        ${scope.hive ? opt('hive', 'THIS HIVE · ' + esc(scope.hive.toUpperCase())) : ''}
        ${opt('fleet', 'ENTIRE FLEET' + (action === 'stop' ? ' ⚠' : ''))}</div>`}
      ${action === 'start' ? `<h6>DURATION</h6><div class="chips">${
        PRESETS.map(([label, h]) => `<button data-h="${h}" class="${
          !st.custom && st.hours === h ? 'on' : ''}">${label}</button>`).join('')}
        <button data-custom="1" class="${st.custom ? 'on' : ''}">CUSTOM</button>
        ${st.custom ? `<input class="input hours" type="number" min="0.5" max="720" step="0.5"
          value="${st.hours ?? 12}" data-hours>` : ''}</div>` : ''}
      <div class="dialog-body">
        <div class="say">${esc(summary(action, sc, cams, st.hours))}</div>
        <div class="say"><span class="ok">${f.connected} connected will ack within a
          heartbeat</span>${f.offline ? ` · <span class="warn">${f.offline} offline get it
          on reconnect</span>` : ''}</div>
      </div>
      ${st.level === 'fleet' && action === 'stop' ? `<div class="guard">
        <h6>FLEET SCOPE IS NEVER THE DEFAULT</h6>
        <p>A wrong start records extra footage; a wrong stop loses it. Type
          <b>STOP ALL</b> to enable the button.</p>
        <input class="input" data-type placeholder="STOP ALL" value="${esc(st.typed)}"></div>` : ''}
      <div class="dialog-actions">
        <button class="btn ${action === 'stop' ? 'btn-danger' : 'btn-primary'}" data-go
          ${ok ? '' : 'disabled'}>Command ${action} — ${f.nodes} node${f.nodes === 1 ? '' : 's'}</button>
        <button class="btn btn-ghost" data-cancel>Cancel</button></div>
      <p class="say bad" data-err></p></div>`;
  };

  d.addEventListener('click', async e => {
    const t = e.target.closest('button');
    if (!t) return;
    if (t.dataset.cancel !== undefined) return d.close();
    if (t.dataset.level) { st.level = t.dataset.level; return paint(); }
    if (t.dataset.h !== undefined) {
      st.custom = false;
      st.hours = t.dataset.h === 'null' ? null : +t.dataset.h;
      return paint();
    }
    if (t.dataset.custom !== undefined) { st.custom = true; st.hours = st.hours ?? 12; return paint(); }
    if (t.dataset.go !== undefined) {
      t.disabled = true;
      try {
        await post('/api/command', { action, scope: scoped(), cams, hours: st.hours });
        d.close();
      } catch (err) {
        d.querySelector('[data-err]').textContent = String(err.message || err);
        t.disabled = false;
      }
    }
  });
  d.addEventListener('input', e => {
    const el = e.target;
    if (el.dataset.type !== undefined) st.typed = el.value;
    else if (el.dataset.hours !== undefined) st.hours = Math.min(720, Math.max(0.5, +el.value || 0.5));
    else return;
    paint();
    // Repainting drops focus; put it back with the caret at the end. A number input
    // has no selection API at all, so only text inputs get the caret move.
    const again = d.querySelector(el.dataset.type !== undefined ? '[data-type]' : '[data-hours]');
    if (!again) return;
    again.focus();
    if (again.type !== 'number') again.setSelectionRange(again.value.length, again.value.length);
  });
  paint();
  return d;
}

// --- ticket tray ---
// Tickets ride the SSE doc (active only), so one that vanishes from the doc has finished.
// Keep the last copy to collapse it into a receipt instead of having it blink out.
const seen = new Map();
const dismissed = new Set();

const ROW = {
  acked: r => ['ok', `ACKED +${dur(r.lag || 0)}`],
  waiting: () => ['warn', 'WAITING'],
  offline: () => ['warn', 'OFFLINE — STORED'],
  escalated: () => ['error', 'ESCALATED'],
  failed: r => ['error', `FAILED ${r.error || ''}`.trim()],
  // Not a failure: a newer command replaced this one, and the row can never move again.
  superseded: () => ['idle', 'SUPERSEDED'],
};

export function tray(doc) {
  const active = new Set(doc.tickets.map(t => t.id));
  for (const t of doc.tickets) seen.set(t.id, t);
  return [...seen.values()].filter(t => !dismissed.has(t.id)).map(t => {
    const list = Object.entries(t.rows);
    const acked = list.filter(([, r]) => r.state === 'acked').length;
    const stored = list.filter(([, r]) => r.state === 'offline').length;
    const scope = (t.scope.node || t.scope.hive || 'FLEET').toUpperCase();
    if (!active.has(t.id)) {
      return `<div class="ticket done">${dot(acked === list.length ? 'ok' : 'warn')} CMD ${
        esc(t.id)} · ${esc(t.action.toUpperCase())} · ${esc(scope)} · ${acked}/${list.length} acked${
        stored ? ` · ${stored} stored` : ''}
        <button class="x" data-dismiss="${esc(t.id)}">✕</button></div>`;
    }
    return `<div class="ticket">
      <div class="ticket-hd">CMD ${esc(t.id)} · ${esc(t.action.toUpperCase())} REC · ${esc(scope)}
        <b>${acked}/${list.length}</b></div>
      <div class="ticket-bar"><i style="width:${Math.round(100 * acked / (list.length || 1))}%"></i></div>
      ${list.map(([, r]) => {
        const [kind, text] = (ROW[r.state] || (() => ['idle', r.state.toUpperCase()]))(r);
        return `<div class="ticket-row">${dot(kind)}${esc(r.node)}
          <span class="${kind === 'ok' ? 'ok' : kind === 'warn' ? 'warn'
            : kind === 'error' ? 'bad' : 'dim'}">${esc(text)}</span></div>`;
      }).join('')}
      <div class="ticket-foot"><button class="btn btn-ghost" data-dismiss="${esc(t.id)}">Dismiss</button></div>
    </div>`;
  }).join('');
}

export const dismiss = id => dismissed.add(id);
export const reopen = () => dismissed.clear();
