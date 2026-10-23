// Workbench Activity stays in step with the server (static/js/workbench.js):
// reconnects do not duplicate events, polling and streaming feed one run
// registry, a stalled history request cannot wedge the connection, and the
// indicator says whether the feed is live, reconnecting, stale or paused.
import assert from 'node:assert/strict';
import { after, beforeEach, mock, test } from 'node:test';

import { installDom } from '../_support/dom.mjs';
import { installFetchFake } from '../_support/fetchFake.mjs';

const dom = installDom();
const fake = installFetchFake();

const sources = [];
globalThis.EventSource = class EventSource {
  constructor(url) { this.url = url; this.readyState = 0; this.closed = false; sources.push(this); }
  close() { this.closed = true; this.readyState = 2; }
};
const openStreams = () => sources.filter((s) => !s.closed);

window.sessionModule = { getCurrentSessionId: () => 's1' };
document.body.innerHTML = `
  <div id="workbench-modal"><div class="workbench-modal-content"></div>
    <span id="wb-live"></span><button id="wb-pause">Pause feed</button>
    <div id="wb-activity"></div>
  </div>`;

mock.timers.enable({ apis: ['setTimeout', 'setInterval', 'Date'], now: 1_700_000_000_000 });
after(async () => {
  mock.timers.reset();
  fake.restore();
  await dom.restore();
});

let historyEvents = [];
let historySeq = 0;
let historyHang = 0;     // how many history requests never answer
let polledRuns = [];
fake.route('GET', '/api/auth/settings', () => ({ workbench_enabled: true }));
fake.route('GET', '/api/workbench/activity', () => {
  if (historyHang > 0) { historyHang -= 1; return new Promise(() => {}); }
  return { events: historyEvents, seq: historySeq };
});
fake.route('GET', '/api/workbench/runs', () => ({ runs: polledRuns }));

const mod = await import('../../../../static/js/workbench.js');
const { _state: state, _connect: connect, _ingest: ingest, _refreshAgentRuns: poll, feedStatus } = mod;
await mod.default.init();

// Let promise callbacks run without moving the fake clock.
const flush = async () => { for (let i = 0; i < 12; i += 1) await new Promise((r) => setImmediate(r)); };
const settle = async (ms) => { mock.timers.tick(ms); await flush(); };

const ev = (seq, extra = {}) => ({
  id: `e${seq}`, seq, ts: 1_700_000_000 + seq, session_id: 's1', run_id: 'r1', source: 'claude_code',
  kind: 'message', level: 'info', title: `event ${seq}`, data: {}, ...extra,
});
const activityText = () => document.getElementById('wb-activity').textContent;

beforeEach(async () => {
  state.paused = false;
  state.prefs.tab = 'activity';
  state.prefs.scope = 'session';
  historyEvents = []; historySeq = 0; historyHang = 0; polledRuns = [];
  await connect(true);
  await flush();
  sources.length = 0;
  state.agentRunsAt = 0;
});

test('an event delivered twice changes no list, count or badge', async () => {
  // A reconnect replays from an older position; the same (session, seq) must
  // not be added, counted or badged a second time.
  const tool = ev(1, { kind: 'tool_start', title: 'bash' });
  assert.equal(ingest(tool, { live: true }), true);
  assert.equal(ingest({ ...tool }, { live: true }), false);

  assert.equal(state.events.length, 1);
  assert.equal(state.runs.get('r1').tools, 1);
  assert.equal(state.cursors.get('s1'), 1);
});

test('two sessions may use the same seq without hiding each other', () => {
  ingest(ev(1, { session_id: 'sA', run_id: 'rA' }), { live: true });
  ingest(ev(1, { session_id: 'sB', run_id: 'rB' }), { live: true });
  assert.equal(state.events.length, 2);
  assert.equal(state.cursors.get('sA'), 1);
  assert.equal(state.cursors.get('sB'), 1);
});

test('the client reopens a closed stream from the positions it holds', async () => {
  await connect(true);
  await flush();
  ingest(ev(7), { live: true });
  const first = openStreams()[0];
  first.readyState = 2;
  first.onerror();
  await settle(1000);

  const reopened = openStreams();
  assert.equal(reopened.length, 1);
  assert.notEqual(reopened[0], first);
  assert.match(reopened[0].url, /since=7/);
});

test('a global-feed reconnect sends the per-session positions, not one number', async () => {
  state.prefs.scope = 'all';
  await connect(true);
  await flush();
  ingest(ev(3, { session_id: 'sA' }), { live: true });
  ingest(ev(9, { session_id: 'sB', run_id: 'rB' }), { live: true });
  const first = openStreams()[0];
  first.readyState = 2;
  first.onerror();
  await settle(1000);

  const url = new URL(openStreams()[0].url, 'http://localhost');
  const cursor = url.searchParams.get('cursor');
  assert.ok(cursor && cursor.startsWith('g1.'), `cursor sent: ${cursor}`);
  const body = JSON.parse(atob(cursor.slice(3).replace(/-/g, '+').replace(/_/g, '/') + '=='.slice(0, (4 - (cursor.length - 3) % 4) % 4)));
  assert.deepEqual(body.s, { sA: 3, sB: 9 });
});

test('a gap frame shows "Some activity was skipped" and refetches history', async () => {
  await connect(true);
  await flush();
  const before = fake.calls.filter((c) => c.url.pathname === '/api/workbench/activity').length;
  historyEvents = [ev(1), ev(2)];
  historySeq = 2;

  openStreams()[0].onmessage({ data: JSON.stringify({ type: 'gap', session_id: 's1', skipped: 40 }) });
  assert.match(activityText(), /Some activity was skipped/);
  await settle(1000);

  const after = fake.calls.filter((c) => c.url.pathname === '/api/workbench/activity').length;
  assert.equal(after, before + 1, 'history was fetched again');
  assert.equal(state.events.length, 2);
  assert.match(activityText(), /Caught up from history/);
});

test('a run that only polling discovers appears in Activity, start and finish', async () => {
  // The stream never delivered this run's events (the tab slept, or the stream
  // was down). Polling is the only thing that knows about it.
  polledRuns = [{ run_id: 'p1', source: 'session', session_id: 's1', title: 'Polled worker',
    status: 'running', started_at: 1_700_000_000, finished_at: null, summary: {} }];
  await poll({ force: true });
  assert.match(activityText(), /Polled worker/);
  assert.equal(document.querySelector('#wb-activity .wb-runs')?.textContent.includes('Nothing running'), false);

  polledRuns = [{ ...polledRuns[0], status: 'completed', finished_at: 1_700_000_050 }];
  await poll({ force: true });
  const recent = Array.from(document.querySelectorAll('#wb-activity .wb-runs .wb-group-h'))
    .find((h) => h.textContent.includes('Recent'));
  assert.ok(recent, 'a Recent group is rendered');
  assert.equal(state.runs.get('p1').status, 'completed');
  assert.match(activityText(), /Nothing running/);
});

test('while the feed is paused polling marks N new and leaves the list alone', async () => {
  await poll({ force: true });
  const shown = activityText();
  state.paused = true;
  polledRuns = [{ run_id: 'p2', source: 'session', session_id: 's1', title: 'Late worker',
    status: 'running', started_at: 1_700_000_100, finished_at: null, summary: {} }];

  await poll({ force: true });

  assert.equal(activityText(), shown, 'the rendered list did not move');
  assert.equal(state.pendingNew, 1);
  assert.match(document.getElementById('wb-pause').textContent, /1 new/);
  assert.equal(document.getElementById('wb-live').dataset.state, 'paused');
});

test('a history request that never answers times out and the stream still opens', async () => {
  historyHang = 1;
  const attempt = connect(true);
  await flush();
  assert.equal(openStreams().length, 0, 'still waiting on history');
  assert.equal(state.pendingKey, 's1');

  await settle(8000);
  await attempt;
  await flush();

  assert.equal(openStreams().length, 1, 'the stream opened after the timeout');
  assert.equal(state.pendingKey, null);
  // The backlog is retried with backoff until it arrives.
  historyEvents = [ev(1)];
  historySeq = 1;
  await settle(1000);
  assert.equal(state.events.length, 1);
});

test('a stalled attempt does not block a later connect', async () => {
  historyHang = 1;
  const stuck = connect(true);
  await flush();
  const later = connect(true);
  await flush();
  await later;
  assert.equal(openStreams().length, 1);
  await settle(9000);   // the stalled attempt times out after it was replaced
  await stuck;
  assert.equal(openStreams().length, 1, 'the superseded attempt opened nothing');
});

test('hiding and showing the tab leaves exactly one open stream', async () => {
  await connect(true);
  await flush();
  assert.equal(openStreams().length, 1);

  const setHidden = (hidden) => {
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden });
    document.dispatchEvent(new Event('visibilitychange'));
  };
  // Back before the grace period: the stream stayed open, nothing reopens.
  setHidden(true);
  await settle(10_000);
  setHidden(false);
  await flush();
  assert.equal(openStreams().length, 1);

  // Back after the grace period: the stream was dropped, one reopens.
  setHidden(true);
  await settle(31_000);
  assert.equal(openStreams().length, 0);
  setHidden(false);
  setHidden(true);
  setHidden(false);
  await flush();
  assert.equal(openStreams().length, 1);
  Object.defineProperty(document, 'hidden', { configurable: true, get: () => false });
});

test('coming back from a long hide keeps what is on screen and asks the server once', async () => {
  ingest(ev(5), { live: true });
  const asked = () => fake.calls.filter((c) => c.url.pathname === '/api/workbench/runs').length;
  const setHidden = (hidden) => {
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden });
    document.dispatchEvent(new Event('visibilitychange'));
  };
  setHidden(true);
  await settle(31_000);
  const before = asked();
  setHidden(false);
  await flush();

  assert.equal(state.events.length, 1, 'the registry survived');
  assert.match(openStreams()[0].url, /since=5/);
  assert.equal(asked() - before, 1);
});

test('the indicator tells live, reconnecting, stale, paused and offline apart', () => {
  const now = 1_700_000_000_000;
  const base = { lastUpdateAt: now - 1000, now, pendingNew: 0 };
  assert.equal(feedStatus({ ...base, paused: false, connection: 'live' }).state, 'live');
  assert.equal(feedStatus({ ...base, paused: false, connection: 'reconnecting' }).state, 'reconnecting');
  assert.equal(feedStatus({ ...base, paused: false, connection: 'live', lastUpdateAt: now - 120_000 }).state, 'stale');
  assert.equal(feedStatus({ ...base, paused: false, connection: 'off' }).state, 'off');
  const paused = feedStatus({ ...base, paused: true, connection: 'off', pendingNew: 2 });
  assert.equal(paused.state, 'paused');
  assert.match(paused.text, /paused.*2 new.*offline/i, 'paused and disconnected read differently');
  assert.doesNotMatch(feedStatus({ ...base, paused: false, connection: 'off' }).text, /paused/i);
  assert.match(feedStatus({ ...base, paused: false, connection: 'live' }).text, /\d{1,2}:\d{2}/, 'shows the time of the last update');
});

test('a burst of write results sends one repo-stale event per second', async () => {
  const seen = [];
  document.addEventListener('wb:repo-stale', (e) => seen.push(e.detail));
  await settle(2000);
  for (let i = 1; i <= 6; i += 1) {
    ingest(ev(100 + i, { kind: 'tool_result', title: 'file_write done', data: { tool: 'file_write' } }), { live: true });
  }
  await flush();
  assert.equal(seen.length, 1);
  assert.deepEqual(seen[0], { session_id: 's1', run_id: 'r1', reason: 'tool_result' });

  await settle(1000);
  assert.equal(seen.length, 2, 'the rest of the burst arrives as one trailing event');
  await settle(5000);
  assert.equal(seen.length, 2);

  ingest(ev(200, { kind: 'tool_result', title: 'read_file done', data: { tool: 'read_file' } }), { live: true });
  await settle(2000);
  assert.equal(seen.length, 2, 'a read does not change a checkout');

  ingest(ev(201, { kind: 'run_finished', title: 'done', data: { status: 'completed' } }), { live: true });
  assert.equal(seen.at(-1).reason, 'run_finished');
});
