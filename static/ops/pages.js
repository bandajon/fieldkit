// The peer screens: alerts, audit, enrolment, and the remote log tail.
import { esc, dot, dur, hhmm, frame, cached, modal, post } from './render.js';

const COND = {
  node_silent: 'NODE SILENT',
  cam_not_recording: 'CAMERA NOT RECORDING',
  disk_critical: 'DISK CRITICAL DESPITE OFFLOAD',
  disk_low: 'DISK LOW',
  offload_failing: 'OFFLOAD FAILING',
  clock_drift: 'CLOCK DRIFT',
  coverage_gap: 'COVERAGE GAP',
  cmd_unacked: 'COMMAND UNACKED',
};

const wait = what => `<div class="empty">LOADING ${esc(what)}…</div>`;

const STATE_RANK = { FIRING: 1, PENDING: 2, CLEARED: 3 };

/** disk_critical pins above everything: safety valve down while the drive fills. */
export const alertRank = a => a.cond === 'disk_critical' && a.state !== 'CLEARED'
  ? 0 : STATE_RANK[a.state] || 9;

export const sortAlerts = rows =>
  [...rows].sort((x, y) => alertRank(x) - alertRank(y) || y.last - x.last);

export function alertsView(now, redraw) {
  const d = cached('/api/alerts', redraw);
  if (!d) return wait('alerts');
  if (!d.alerts.length) return '<div class="empty">NO ALERTS<br><span class="dim">nothing has met a firing condition</span></div>';
  return sortAlerts(d.alerts).map(a => {
    const cleared = a.state === 'CLEARED';
    const head = cleared ? `CLEARED · ${COND[a.cond] || a.cond.toUpperCase()}`
      : a.state === 'PENDING' ? `PENDING ${a.n}/${d.persist} · ${COND[a.cond] || a.cond.toUpperCase()}`
      : `FIRING · ${COND[a.cond] || a.cond.toUpperCase()}`;
    const kind = cleared ? 'idle' : a.state === 'PENDING' ? 'warn' : 'error';
    const foot = cleared
      ? `condition gone ${dur(now - a.cleared)} ago — kept 24 h, then the audit log only`
      : a.state === 'PENDING'
        ? `fires only if it persists ${d.persist - a.n} more heartbeat(s) — flap suppression, no webhook yet`
        : 'webhook posted on fire · clears itself after two clean heartbeats';
    return frame(`<div class="alert">
      <div class="alert-hd ${kind === 'error' ? 'bad' : kind === 'warn' ? 'warn' : 'dim'}">${dot(
        kind === 'idle' ? 'idle' : kind)}${esc(head)}
        <span class="dim">${cleared ? hhmm(a.cleared) : dur(now - a.first)}</span></div>
      <div class="alert-txt"><a href="/h/${encodeURIComponent(a.hive)}" data-link>${
        esc(a.hive)} › ${esc(a.node)}</a> — ${esc(a.text)}</div>
      <div class="dim alert-foot">${esc(foot)}</div></div>`,
      `alert-card ${kind === 'error' ? 't-bad' : ''} ${cleared ? 'faded' : ''}`);
  }).join('');
}

export function auditView(redraw) {
  const d = cached('/api/audit?limit=200', redraw);
  if (!d) return wait('audit log');
  // ponytail: the server records {t, kind, ...detail} with no origin device — the mock's
  // ORIGIN column has nothing to fill it, so DETAIL carries the rest verbatim.
  const rows = d.audit.map(r => {
    const { t, kind, ...rest } = r;
    return `<tr><td>${hhmm(t)}</td><td><span class="tag tag-neutral">${
      esc(kind.replace(/_/g, ' ').toUpperCase())}</span></td>
      <td>${esc(Object.entries(rest).map(([k, v]) =>
        `${k}=${typeof v === 'object' ? JSON.stringify(v) : v}`).join(' · '))}</td></tr>`;
  }).join('');
  return `<div class="pagehd"><h5>AUDIT LOG</h5><span class="dim">append-only JSONL · newest first</span>
    <a class="btn btn-ghost" href="/api/audit?limit=200" download="audit.json">Export</a></div>
    <table class="table mono"><thead><tr><th>TIME</th><th>ACTION</th><th>DETAIL</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

export function enrollView(redraw) {
  const d = cached('/api/tokens', redraw);
  if (!d) return wait('enrolment');
  const cards = d.tokens.map(t => frame(`<div class="tok">
    <div class="tok-hd"><h5>${esc(t.hive.toUpperCase())}</h5>
      <span class="dim">${(d.roster[t.hive] || []).length} nodes enrolled</span>
      <span class="${t.state === 'revoked' ? 'bad' : 'ok'}">${esc(t.state.toUpperCase())}</span></div>
    <div class="tok-id">${esc(t.id)} · ${esc(t.mask)}<span class="dim"> · created ${hhmm(t.created)}
      · shown in full once, at creation</span></div>
    <div class="tok-act"><button class="btn btn-ghost" data-rotate="${esc(t.id)}">Rotate token</button>
      <button class="btn btn-ghost btn-danger" data-revoke="${esc(t.id)}">Revoke</button></div></div>`, 'tokcard'))
    .join('');
  const roster = Object.entries(d.roster).flatMap(([hive, ns]) => ns.map(n => `<tr>
    <td>${dot(n.revoked ? 'revoked' : n.connected ? 'ok' : 'warn')}${esc(n.node)}</td>
    <td>${esc(hive)}</td>
    <td class="${n.revoked ? 'bad' : n.connected ? 'ok' : 'dim'}">${
      n.revoked ? 'REVOKED' : n.connected ? 'CONNECTED' : 'NOT CONNECTED'}</td>
    <td>${hhmm(n.last_seen)}${n.confirmed ? '' : ' <span class="dim">unconfirmed</span>'}</td>
    <td><button class="btn btn-ghost" data-${n.revoked ? 'forget' : 'remove'}="${esc(n.key)}">${
      n.revoked ? 'FORGET' : 'REMOVE'}</button></td></tr>`)).join('');
  return `<div class="pagehd"><h5>HIVE TOKENS</h5>
      <input class="input hivename" data-hivename placeholder="new hive name">
      <button class="btn btn-primary" data-newtoken>Create token</button></div>
    <div class="tokgrid">${cards || '<div class="empty">NO TOKENS YET</div>'}</div>
    <div class="pagehd"><h5>ENROLLED NODES</h5><span class="dim">REMOVE de-lists a node (it
      re-enrols on its next heartbeat); FORGET erases a revoked one. Both are audit rows.</span></div>
    <table class="table mono"><thead><tr><th>NODE</th><th>HIVE</th><th>LINK</th><th>LAST HB</th><th></th></tr></thead>
    <tbody>${roster || '<tr><td colspan="5" class="dim">no nodes enrolled</td></tr>'}</tbody></table>`;
}

/** The raw token exists outside the node's config.yaml exactly here, exactly once. */
function showToken(r, note) {
  modal(`<div class="dialog"><h4 class="dialog-title">TOKEN — ${esc(r.hive.toUpperCase())}</h4>
    <div class="dialog-body"><p class="say">${esc(note)}</p>
      <div class="tok-raw">${esc(r.token)}</div>
      <p class="say dim">shown once — paste into the node's config.yaml <b>ops:</b> block</p></div>
    <div class="dialog-actions"><button class="btn btn-primary" data-close>Done</button></div></div>`);
}

/** Yes/no on the design system rather than window.confirm, because the copy matters. */
export function ask(title, body, danger) {
  return new Promise(res => {
    const d = modal(`<div class="dialog"><h4 class="dialog-title ${danger ? 'bad' : ''}">${
      esc(title)}</h4><div class="dialog-body"><p class="say">${body}</p></div>
      <div class="dialog-actions">
        <button class="btn ${danger ? 'btn-danger' : 'btn-primary'}" data-yes>${esc(title)}</button>
        <button class="btn btn-ghost" data-close>Cancel</button></div></div>`);
    d.addEventListener('click', e => {
      if (e.target.closest('[data-yes]')) { res(true); d.close(); }
      else if (e.target.closest('[data-close]')) d.close();
    });
    d.addEventListener('close', () => res(false), { once: true });
  });
}

/** Enrolment buttons — every one of these is a write, so they answer here, not in the router. */
export async function enrollClick(t, redraw) {
  try {
    if (t.dataset.newtoken !== undefined) {
      const hive = t.closest('.pagehd').querySelector('[data-hivename]').value.trim();
      if (!hive) return;
      showToken(await post('/api/tokens', { hive }),
                'A node enrols by pasting this into its config and heartbeating.');
    } else if (t.dataset.rotate) {
      if (!await ask('Rotate token', 'The old token stops enrolling <b>new</b> nodes. '
        + 'Nodes already enrolled keep working.')) return;
      showToken(await post(`/api/tokens/${t.dataset.rotate}/rotate`), 'Replacement token.');
    } else if (t.dataset.revoke) {
      if (!await ask('Revoke token', 'Heartbeats on this token are rejected. Nodes using it '
        + 'render <b>REVOKED</b>, they do not vanish.', true)) return;
      await post(`/api/tokens/${t.dataset.revoke}/revoke`);
    } else if (t.dataset.remove) {
      if (!await ask('Remove node', 'De-lists it. The token still works, so it re-enrols '
        + 'on its next heartbeat.')) return;
      await post(`/api/roster/${t.dataset.remove}/remove`);
    } else if (t.dataset.forget) {
      if (!await ask('Forget node', 'Erases a revoked node from the roster, history included.',
        true)) return;
      await post(`/api/roster/${t.dataset.forget}/forget`);
    } else {
      return;
    }
  } catch (e) {
    modal(`<div class="dialog"><h4 class="dialog-title bad">FAILED</h4>
      <div class="dialog-body"><p class="say">${esc(e.message || e)}</p></div>
      <div class="dialog-actions"><button class="btn btn-ghost" data-close>Close</button></div></div>`);
  }
  redraw(true);
}

/** Remote log tail — read-only, over the control channel, no ssh. */
export function logTail(hive, node) {
  const d = modal(`<div class="log-sheet">${wait('log')}</div>`, 'wide');
  const key = `${encodeURIComponent(hive)}/${encodeURIComponent(node)}`;
  let which = 'node';
  const paint = data => {
    const tabs = ['node', ...Object.keys(data.cameras)];
    const lines = (which === 'node' ? data.node : data.cameras[which] || []).slice(-200);
    d.innerHTML = `<div class="log-sheet">
      <div class="log-hd"><h5>LOG TAIL — ${esc(node)}</h5>
        <div class="seg2">${tabs.map(t => `<button data-tab="${esc(t)}" class="${
          t === which ? 'on' : ''}">${esc(t.toUpperCase())}</button>`).join('')}</div>
        <span class="dim">last ${lines.length} lines · over the control channel · no ssh</span>
        <button class="btn btn-ghost" data-close>Close</button></div>
      <pre class="log-body">${esc(lines.join('\n')) || '(empty)'}</pre>
      <div class="log-foot">READ-ONLY.</div></div>`;
  };
  fetch(`/api/logs/${key}`).then(r => r.json()).then(data => {
    paint(data);
    d.addEventListener('click', e => {
      const t = e.target.closest('button');
      if (!t) return;
      if (t.dataset.close !== undefined) return d.close();
      if (t.dataset.tab) { which = t.dataset.tab; paint(data); }
    });
  }).catch(e => { d.innerHTML = `<div class="log-sheet"><div class="empty">${esc(e)}</div></div>`; });
  return d;
}
