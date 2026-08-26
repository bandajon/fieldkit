// Router and boot. History API on real paths; every screen redraws from one fleet doc.
import { state, connect, clock } from './store.js';
import { nav, rollup, paintThumbs, thumbTick, paintLive, bust } from './render.js';
import { fleetView, expanded, folds } from './fleet.js';
import { hiveView, cameraView, crumb, switchClick } from './hive.js';
import { confirm, tray, dismiss, reopen } from './actions.js';
import { alertsView, auditView, enrollView, enrollClick, logTail } from './pages.js';

const $ = id => document.getElementById(id);
const dec = s => { try { return decodeURIComponent(s); } catch { return s; } };
const seg = () => location.pathname.split('/').filter(Boolean).map(dec);

const focus = () => { const s = seg(); return s[0] === 'h' && s.length >= 4; };

function view(doc, now) {
  const s = seg();
  if (s[0] === 'h' && s.length >= 4)
    return [cameraView(doc, s[1], s[2], s[3], now, draw), crumb(s[1], ` › ${s[2]} › <b>${s[3]}</b>`)];
  if (s[0] === 'h' && s[1]) return [hiveView(doc, s[1], now), crumb(s[1])];
  if (s[0] === 'alerts') return [alertsView(now, draw), ''];
  if (s[0] === 'audit') return [auditView(draw), ''];
  if (s[0] === 'enroll') return [enrollView(draw), ''];
  return [fleetView(doc, now), ''];
}

function draw(fresh) {
  const doc = state.doc;
  $('conn').hidden = state.live;
  if (!doc) return;
  if (fresh) bust();                              // a write just invalidated the cached reads
  const now = clock();
  const [html, breadcrumb] = view(doc, now);
  $('nav').innerHTML = nav(doc, location.pathname);
  $('nav').insertAdjacentHTML('beforeend', breadcrumb);
  $('rollup').innerHTML = rollup(doc);
  $('main').innerHTML = html;
  $('main').classList.toggle('dark', focus());
  $('tray').innerHTML = tray(doc);
  paintThumbs($('main'));
  paintLive($('main'));                           // no [data-live] on screen = stream torn down
}

function go(path) {
  if (path !== location.pathname) history.pushState(null, '', path);
  draw();
  scrollTo(0, 0);
}

document.addEventListener('click', e => {
  const a = e.target.closest('a[data-link]');
  if (a && a.origin === location.origin) {
    e.preventDefault();
    return go(a.pathname);
  }
  const b = e.target.closest('button');
  const d = b && b.dataset;
  // Actions are checked before the hive fold: they sit inside the hive header.
  if (d && d.start !== undefined) return confirm('start', d.start ? { hive: d.start } : {});
  if (d && d.stophive !== undefined) return confirm('stop', d.stophive ? { hive: d.stophive } : {});
  if (d && (d.stop || d.startnode)) {
    const [hive, node] = (d.stop || d.startnode).split('/');
    return confirm(d.stop ? 'stop' : 'start', { hive, node }, d.cam ? [d.cam] : null);
  }
  if (d && d.switch) return switchClick(b, draw);
  if (d && d.log) return logTail(...d.log.split('/'));
  if (d && d.dismiss) { dismiss(d.dismiss); return draw(); }
  if (d && d.tickets !== undefined) { reopen(); return draw(); }
  if (d && (d.newtoken !== undefined || d.rotate || d.revoke || d.remove || d.forget))
    return enrollClick(b, draw);

  const hd = e.target.closest('[data-hive]');
  if (hd) {
    const h = hd.dataset.hive;
    // Only all-green hives fold; a hive with problems ignores the toggle.
    if (folds(state.doc.hives.find(x => x.hive === h) || {})) {
      expanded.has(h) ? expanded.delete(h) : expanded.add(h);
    }
    draw();
  }
});

addEventListener('popstate', draw);
setInterval(draw, 30000);                                       // countdowns and ages
setInterval(() => thumbTick($('main')), 10000);                 // ledger snapshots
setInterval(() => focus() && thumbTick($('main')), 2000);       // camera focus, faster tier
setInterval(() => { $('clock').textContent = new Date().toLocaleTimeString(); }, 1000);
connect(draw);
