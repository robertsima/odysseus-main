// Archive acts on a whole Phalanx unit (static/js/agentsDashboard.js). User
// report (2026-10-07): archiving one old agent made five more appear, its
// workers promoted to units of their own. The server now archives the unit;
// the room has to say so before it happens and offer Restore on a worker the
// archive view lists under its archived parent.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { openRoom, waitFor } from './_room.mjs';

const lead = { session_id: 'p1', name: 'Release lead', status: 'idle', source: 'session', config: {} };
const worker = (n) => ({ session_id: `w${n}`, name: `Worker ${n}`, status: 'finished', source: 'session',
  parent_session: 'p1', config: {} });

const room = await openRoom({ rows: [lead, worker(1), worker(2)] });
const { dashboard, root } = room;

test('archiving a parent names the workers that go with it, and declining sends nothing', async () => {
  const asked = [];
  window.confirm = (message) => { asked.push(message); return false; };
  dashboard.open({ select: 'p1' });
  await waitFor(() => root.querySelector('#ag-detail [data-ag="archive-agent"]'), { what: 'the Archive button' });
  room.click('#ag-detail [data-ag="archive-agent"]');
  await waitFor(() => asked.length, { what: 'the confirmation' });
  assert.match(asked[0], /and its 2 workers/);
  assert.ok(!room.fake.calls.some((c) => c.method === 'POST' && c.url.pathname.endsWith('/archive')));
  dashboard.close();
});

test('a worker listed under its archived parent offers Restore, not Archive', async () => {
  room.overview.rows = [{ ...lead, archived: true },
    { ...worker(1), archived: false, archived_with_parent: true }];
  dashboard.open({ view: 'archive', select: 'w1' });
  await waitFor(() => root.querySelector('#ag-detail [data-ag="restore-agent"][data-sid="w1"]'), { what: 'Restore on the worker' });
  assert.equal(root.querySelector('#ag-detail [data-ag="archive-agent"]'), null);
  dashboard.close();
});
