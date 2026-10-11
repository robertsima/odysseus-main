// A worker that reported itself blocked or partly done must not read as Finished
// (static/js/agentsDashboard.js). When it left a `Needs user:` line it is listed
// under "Needs you"; without one it is shown for what it is in Recent.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { openRoom, waitFor } from './_room.mjs';

const row = (sid, status, extra = {}) => ({
  session_id: sid, name: sid, status, source: 'session', config: {}, needs: [], children: [], ...extra,
});

const room = await openRoom({
  currentSession: 'p1',
  rows: [
    row('w-blocked', 'blocked', { needs: ['paste the staging key into note deploy'] }),
    row('w-blocked-bare', 'blocked'),
    row('w-partial', 'partial'),
    row('w-done', 'finished'),
  ],
});
const { dashboard, root } = room;
dashboard.open();
await waitFor(() => root.querySelector('.ag-row[data-sid="w-done"]'), { what: 'the fleet' });

const pillOf = (sid) => root.querySelector(`.ag-row[data-sid="${sid}"] .wb-pill`)?.textContent.trim();
const inNeedsYou = (sid) => !!root.querySelector(`.ag-group-attn .ag-row[data-sid="${sid}"]`);

test('blocked and partial read as themselves, not as Finished', () => {
  assert.equal(pillOf('w-blocked'), 'Blocked');
  assert.equal(pillOf('w-partial'), 'Partly done');
  assert.equal(pillOf('w-done'), 'Finished');
});

test('a blocked worker with a Needs user line is under Needs you, with the line shown', () => {
  assert.ok(inNeedsYou('w-blocked'));
  assert.match(root.querySelector('.ag-row[data-sid="w-blocked"] .ag-row-need').textContent, /paste the staging key/);
});

test('blocked or partial without a Needs user line is not under Needs you', () => {
  assert.equal(inNeedsYou('w-blocked-bare'), false);
  assert.equal(inNeedsYou('w-partial'), false);
  assert.equal(inNeedsYou('w-done'), false);
});
