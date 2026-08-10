// Render smoke check for the ops UI: node static/ops/selfcheck.mjs
// The render helpers are pure string builders, so a fixture fleet doc is the whole test rig.
import assert from 'node:assert/strict';
import { mark, state } from './store.js';
import { fleetView, expanded, row } from './fleet.js';
import { hiveView, cameraView } from './hive.js';
import { liveUrl } from './render.js';
import { forecast, summary, gate, tray, dismiss, reopen } from './actions.js';
import { sortAlerts, alertRank } from './pages.js';

const NOW = 1770000000;

const cam = (name, o = {}) => ({
  name, state: 'RECORDING', until: null, minutes: 120, restarts: 0,
  coverage_pct: 99.1, gaps: [], snapshot_age: 4, log: ['one', 'two', 'three', 'four'], ...o,
});

const node = (hive, name, o = {}) => ({
  key: `${hive}/${name}`, hive, node: name, state: 'ok', issues: [], sort: 9,
  confirmed: true, hb_only: false, last_seen: NOW - 31, age_s: 31, seq: 5,
  clock_drift: null, link: { rtt_ms: null, miss_pct: 0.0 }, ips: ['100.84.2.11'],
  versions: {}, version_diverged: false,
  disks: [{ path: '/srv/recordings', free_gb: 614, total_gb: 1000, hours_left: 61.4 }],
  worst_hours: 61.4, write_gb_h: 10, offload: {}, cameras: [cam('cam4')], ticket: null, ...o,
});

const kalambo = {
  hive: 'kalambo', state: 'revoked', sort: 0, online: 2,
  nodes: [
    node('kalambo', 'fk-old-07', { state: 'revoked', sort: 0, issues: ['revoked'] }),
    node('kalambo', 'fk-kal-02', {
      state: 'offline', sort: 1, issues: ['offline'], last_seen: NOW - 2520,
      cameras: [cam('cam1'), cam('cam2', { snapshot_age: null })],
    }),
    node('kalambo', 'fk-kal-03', {
      state: 'warn', sort: 3, issues: ['heartbeat only'], hb_only: true,
      cameras: [cam('cam7', { snapshot_age: 58 })],
    }),
    node('kalambo', 'fk-bb-01', {
      state: 'warn', sort: 4, issues: ['disk low'], worst_hours: 4.2,
      disks: [
        { path: '/srv/recordings', free_gb: 42, total_gb: 1000, hours_left: 4.2 },
      ],
      offload: { enabled: true, uploaded: 411, last_file: 'x/20260808T1340_cam4.mkv', last_error: null },
      cameras: [
        cam('cam5', { until: NOW + 14280 }),
        cam('cam6', { coverage_pct: 88.4, gaps: [[NOW - 40000, NOW - 33000]] }),
        cam('cam9', { snapshot_age: null }),
      ],
    }),
    node('kalambo', '<script>x</script>', { state: 'warn', sort: 5, issues: ['clock drift'], clock_drift: 4.2 }),
  ],
};

const chingola = {
  hive: 'chingola', state: 'ok', sort: 9, online: 2,
  nodes: [node('chingola', 'fk-chi-01'), node('chingola', 'fk-chi-02')],
};

const doc = {
  now: NOW, hives: [kalambo, chingola], tickets: [],
  rollup: { nodes: 7, online: 5, offline: 1, cams: 8, recording: 8, alerts_firing: 2, alerts_pending: 1, tickets_active: 1 },
  alerts: { firing: 2, pending: 1, cleared: 0 },
};

mark(doc);                                       // first frame: baseline for the offload delta
kalambo.nodes[3].offload.uploaded = 412;
mark(doc);                                       // second frame: the counter moved -> UPLOADING

const html = fleetView(doc, NOW);

// all-green hive folds to its header row, and folded hives fetch no thumbs
assert.match(html, /CHINGOLA/);
assert.match(html, /2 NODES · 2 CAMS · 2 ONLINE/);
assert.match(html, /ALL HEALTHY/);
assert.ok(!html.includes('chingola/fk-chi-01/cam4'), 'folded hive must render no thumbs');

// offline row: text only, red, no thumb fetches, last-known numbers kept
assert.match(html, /OFFLINE 42 MIN — NO THUMBS/);
assert.match(html, /LAST STATE 2 CAMS REC/);
assert.ok(!html.includes('kalambo/fk-kal-02/cam1'), 'offline node must fetch no thumbs');

// hb-only row: amber reason, snapshots still shown with age chips
assert.match(html, /HB ONLY — NO MEDIA PATH/);
assert.match(html, /data-snap="kalambo\/fk-kal-03\/cam7"/);
assert.match(html, /58 S · HB/);

// revoked pins to the top of its hive
assert.ok(html.indexOf('REVOKED') < html.indexOf('fk-kal-02'), 'revoked row must sort first');

// timer chip + MIXED, per-drive lines, offload
assert.match(html, /⏱ STOPS \d\d?:\d\d · 3:58 LEFT/);
assert.match(html, /MIXED/);
assert.match(html, /4\.2 H LEFT · 42\/1000 GB — recordings/);
assert.match(html, /▲ UPLOADING 20260808T1340_cam4\.mkv/);
assert.match(html, /CLOCK ±4\.2s/);
assert.match(html, /RTT —/);

// remote names are escaped, never injected
assert.ok(!html.includes('<script>x</script>'), 'node names must be escaped');
assert.match(html, /&lt;script&gt;x&lt;\/script&gt;/);

// expanding the healthy hive turns its thumbs on
expanded.add('chingola');
assert.match(fleetView(doc, NOW), /data-snap="chingola\/fk-chi-01\/cam4"/);
expanded.delete('chingola');

// a heartbeat-only node carries no record payload, so an empty camera list is the
// normal case and must still read as HB ONLY, not "no cameras reported"
assert.match(row({ ...kalambo.nodes[2], cameras: [] }, NOW, true), /HB ONLY — NO MEDIA PATH/);

// a camera with no cached snapshot gets a labelled placeholder, never a broken image
assert.match(html, /data-snap="kalambo\/fk-bb-01\/cam9"[\s\S]{0,80}NO SNAP/);
// a folded row falls back to its worst signal in words
assert.match(row(kalambo.nodes[1], NOW, false), /OFFLINE 42 MIN/);
assert.match(fleetView({ ...doc, rollup: { ...doc.rollup, nodes: 0 } }, NOW), /NO NODES ENROLLED/);

// hive page: full cards with coverage, timer, and a 3-line log tail
const noop = () => {};
const hive = hiveView(doc, 'kalambo', NOW);
assert.match(hive, /class="thumb[^"]* big"/);
assert.match(hive, /href="\/h\/kalambo\/fk-bb-01\/cam5"/);
assert.match(hive, /two\nthree\nfour/);
assert.ok(!hive.includes('one\ntwo\nthree\nfour'), 'log tail is the last 3 lines');
assert.match(hive, /88\.4%/);
// cam6's ~2 h gap lands as red hour-cells in a 24-cell bar, with named hours
assert.equal((hive.match(/class="gap"/g) || []).length >= 2, true, 'gap hours must go red');
assert.match(hive, /title="12 h ago · \d+ min missing"/);
assert.equal((cameraView(doc, 'kalambo', 'fk-bb-01', 'cam6', NOW, () => {})
  .match(/<i class="[^"]*" title="/g) || []).length, 24, 'coverage renders 24 hour cells');
assert.match(hive, /UNTIL STOPPED/);
assert.match(hiveView(doc, 'nope', NOW), /NO HIVE NOPE/);

// the live URL prefers the tailnet address, falls back to the first, and encodes the camera
assert.equal(liveUrl({ ips: ['192.168.8.4', '100.84.2.11'] }, 'cam4'),
             'http://100.84.2.11:1984/stream.html?src=cam4');
assert.equal(liveUrl({ ips: ['192.168.8.4'] }, 'cam4'),
             'http://192.168.8.4:1984/stream.html?src=cam4');
assert.equal(liveUrl({ ips: ['100.1.1.1'] }, 'front door/1'),
             'http://100.1.1.1:1984/stream.html?src=front%20door%2F1');
assert.equal(liveUrl({ ips: [] }, 'cam4'), '');       // no address = nothing to dial
assert.equal(liveUrl({}, 'cam4'), '');

// camera focus: one live iframe straight at the node's go2rtc, siblings say what a switch costs
const focus = cameraView(doc, 'kalambo', 'fk-bb-01', 'cam5', NOW, noop);
assert.match(focus, /data-live="http:\/\/100\.84\.2\.11:1984\/stream\.html\?src=cam5"/);
assert.match(focus, /LIVE VIA NODE GO2RTC — blank means go2rtc is off/);
// the snapshot tier sits under the iframe and works whether or not go2rtc answers
assert.match(focus, /SNAPSHOT · 2 S[\s\S]{0,120}data-snap="kalambo\/fk-bb-01\/cam5"/);
assert.match(focus, /OTHER CAMS — fk-bb-01/);
assert.match(focus, /SWITCH \(CLOSES THIS STREAM\)/);
assert.match(focus, /data-stop="kalambo\/fk-bb-01"\s+data-cam="cam5"/);
assert.match(focus, /ONE LIVE STREAM AT A TIME — RECORDING BANDWIDTH ON THE NODE ALWAYS WINS/);
// an offline or media-less node never dials: reason inline, heartbeat frame instead
const dead = cameraView(doc, 'kalambo', 'fk-kal-02', 'cam1', NOW, noop);
assert.ok(!dead.includes('data-live'), 'offline node must not open a stream');
assert.match(dead, /NODE OFFLINE — NO LIVE STREAM/);
assert.match(dead, /data-snap="kalambo\/fk-kal-02\/cam1"/);
const hb = cameraView(doc, 'kalambo', 'fk-kal-03', 'cam7', NOW, noop);
assert.ok(!hb.includes('data-live'), 'hb-only node has no media path to dial');
assert.match(hb, /HB ONLY — NO MEDIA PATH/);
assert.match(cameraView(doc, 'kalambo', 'fk-bb-01', 'nope', NOW, noop), /not reporting/);

// command scope is counted off the live doc, and a revoked node is never a target
state.doc = doc;
assert.deepEqual(forecast({ hive: 'chingola' }), { nodes: 2, connected: 2, offline: 0, cams: 2 });
assert.equal(forecast({}).nodes, 6);                       // 7 minus the revoked one
assert.equal(forecast({}).offline, 1);
assert.equal(forecast({ hive: 'kalambo', node: 'fk-bb-01' }).cams, 3);
assert.equal(forecast({ hive: 'kalambo', node: 'fk-bb-01' }, ['cam5']).cams, 1);

// the summary restates the scope, and a duration restates the stop time
assert.equal(summary('start', { hive: 'chingola' }, null, null), '2 nodes · 2 cameras · until stopped');
assert.equal(summary('start', { hive: 'chingola' }, null, 4, NOW), `2 nodes · 2 cameras · 4 h — stops ${
  new Date((NOW + 14400) * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false })}`);
assert.match(summary('stop', { hive: 'chingola' }, null, null), /stop writing footage$/);

// STOP ALL gates fleet-scope stops only, and wants the words exactly
assert.equal(gate('fleet', 'stop', ''), false);
assert.equal(gate('fleet', 'stop', 'stop all'), false);
assert.equal(gate('fleet', 'stop', 'STOP ALL'), true);
assert.equal(gate('hive', 'stop', ''), true);               // hive scope is never gated
assert.equal(gate('fleet', 'start', ''), true);             // a wrong start only costs footage

// tray rows: every terminal state reads as itself, and superseded is not a failure
const t1 = { id: '0142', action: 'start', scope: { hive: 'kalambo' }, rows: {
  'k/a': { node: 'fk-a', state: 'acked', lag: 9, error: '' },
  'k/b': { node: 'fk-b', state: 'offline', lag: null, error: '' },
  'k/c': { node: 'fk-c', state: 'superseded', lag: null, error: '' },
  'k/d': { node: 'fk-d', state: 'failed', lag: 3, error: 'no such camera' },
  'k/e': { node: 'fk-e', state: 'escalated', lag: null, error: '' },
  'k/f': { node: 'fk-f', state: 'waiting', lag: null, error: '' } } };
const open = tray({ ...doc, tickets: [t1] });
assert.match(open, /CMD 0142 · START REC · KALAMBO[\s\S]*<b>1\/6<\/b>/);
assert.match(open, /ACKED \+9 S/);
assert.match(open, /OFFLINE — STORED/);
assert.match(open, /FAILED no such camera/);
assert.match(open, /ESCALATED/);
assert.match(open, /WAITING/);
assert.match(open, /<span class="dot d-idle"><\/span>fk-c\s*<span class="dim">SUPERSEDED/);
// finished tickets collapse to a receipt that says what actually acked
assert.match(tray({ ...doc, tickets: [] }), /CMD 0142 · START · KALAMBO · 1\/6 acked · 1 stored/);
dismiss('0142');
assert.equal(tray({ ...doc, tickets: [] }), '');
reopen();
assert.match(tray({ ...doc, tickets: [] }), /CMD 0142/);

// alerts: disk_critical above everything, then firing, pending, cleared
const A = (cond, state, last) => ({ cond, state, last, hive: 'h', node: 'n', text: cond });
assert.deepEqual(
  sortAlerts([A('coverage_gap', 'CLEARED', 9), A('clock_drift', 'PENDING', 8),
              A('node_silent', 'FIRING', 7), A('disk_critical', 'FIRING', 1)])
    .map(a => a.cond),
  ['disk_critical', 'node_silent', 'clock_drift', 'coverage_gap']);
// a cleared disk_critical has stopped being urgent and sorts with the other clears
assert.equal(alertRank(A('disk_critical', 'CLEARED', 1)), 3);

console.log('ok  ops ui render smoke');
