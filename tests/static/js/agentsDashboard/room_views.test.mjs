// How the Agent Control Room (static/js/agentsDashboard.js) opens and where
// Launch is. User report (2026-09-29): the room looked different depending
// on how it was opened, and sometimes there was no way to launch a worker.
// The room reopened in whatever view it was left in (the loadout editor and
// the archive had no Launch button), a launch left its form on screen, and a
// covered room closed on the first press instead of coming forward. It also
// opens itself for delegated work the open chat starts, workers included.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { jsonResponse } from '../_support/fetchFake.mjs';
import { openRoom, waitFor } from './_room.mjs';

const room = await openRoom({
  currentSession: 'p1',
  rows: [{ session_id: 'p1', name: 'Admin', status: 'idle', source: 'odysseus', config: {} }],
  profiles: [{ name: 'Lead Engineer', model: 'gpt-4o' }],
  chats: [{ id: 'p1', name: 'Admin' }],
});
const { dashboard, root } = room;

test('the activity stream opens the room for a worker the open chat started, and only for it', async () => {
  room.feed({ kind: 'run_started', source: 'session', session_id: 'w9', data: { parent_session: 'other-chat' } });
  assert.equal(room.isOpen(), false, 'another chat\'s worker leaves it closed');

  room.feed({ kind: 'run_started', source: 'session', session_id: 'w1', data: { parent_session: 'p1' } });
  assert.equal(room.isOpen(), true);
  dashboard.close();
});

test('the room\'s background reads are marked as polls', async () => {
  const reads = room.headers.filter((r) => /\/api\/agents\/(overview|approvals)/.test(r.url));
  assert.ok(reads.length >= 2, JSON.stringify(reads));
  assert.ok(reads.every((r) => r.headers['X-Odysseus-Poll'] === '1'), JSON.stringify(reads));
});

test('a fresh opening starts from the fleet, whatever view the room was left in', async () => {
  dashboard.openLoadouts();
  await waitFor(() => root.querySelector('.ag-loadouts-view'), { what: 'the loadouts view' });
  dashboard.close();

  dashboard.open();
  assert.ok(root.querySelector('.ag-fleet'), 'the fleet is shown');
  assert.equal(root.querySelector('.ag-loadouts-view'), null);
  dashboard.close();
});

test('every view has a Launch button', async () => {
  const views = {
    fleet: async () => {},
    history: async () => room.click('[data-ag="history-view"]'),
    archive: async () => room.click('[data-ag="archive-view"]'),
    loadouts: async () => room.click('[data-ag="loadouts-view"]'),
    "a chat's settings": () => dashboard.editLoadout('p1'),
  };
  for (const [name, show] of Object.entries(views)) {
    dashboard.open();
    await show();
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.ok(root.querySelector('[data-ag="launch"]'), `no Launch in the ${name} view`);
    dashboard.close();
  }
});

// The fake keeps the first route it is given for a path, so one route reads the answer the test sets.
let answer = () => ({ session_id: 'w2', session_name: 'Lead Engineer: fix' });
const launches = [];
test('an assigned task closes its sheet; a refused one keeps it and says why', async () => {
  room.fake.route('GET', '/api/agents/profiles', () => ({ profiles: [] }));
  room.fake.route('POST', '/api/agents/launch', ({ body }) => {
    launches.push(JSON.parse(body));
    return answer();
  });
  dashboard.open();

  const type = (text) => {
    const box = root.querySelector('[data-aa-field="task"]');
    box.value = text;
    box.dispatchEvent(new window.Event('input', { bubbles: true }));
  };
  room.click('[data-ag="launch"]');
  type('fix checkout');
  room.click('[data-aa="go"]');
  await waitFor(() => !root.querySelector('#ag-launch'), { what: 'the sheet to close' });
  assert.equal(launches.at(-1).task, 'fix checkout');

  answer = () => jsonResponse({ detail: 'no such workspace' }, { status: 400 });
  room.click('[data-ag="launch"]');
  type('fix it again');
  room.click('[data-aa="go"]');
  await waitFor(() => /no such workspace/.test(root.querySelector('[data-aa-part="msg"]')?.textContent || ''), { what: 'the refusal' });
  assert.ok(root.querySelector('#ag-launch'), 'the sheet stays to fix and retry');
  dashboard.close();
});

test('a task the server blocks shows the blocker with its buttons in the sheet', async () => {
  answer = () => jsonResponse({
    error: 'x', code: 'WORKSPACE_REQUIRED',
    blocker: { code: 'WORKSPACE_REQUIRED', summary: 'No folder chosen.', detail: {}, actions: [{ id: 'run_without_files', label: 'Run without files', requires: ['no_workspace'] }] },
  }, { status: 409 });
  dashboard.open();
  room.click('[data-ag="launch"]');
  const box = root.querySelector('[data-aa-field="task"]');
  box.value = 'look around';
  box.dispatchEvent(new window.Event('input', { bubbles: true }));
  room.click('[data-aa="go"]');
  await waitFor(() => root.querySelector('[data-aa-action="run_without_files"]'), { what: 'the recovery button' });
  assert.match(root.querySelector('.aa-blocker-text').textContent, /No folder chosen/);
  dashboard.close();
});

test('toggling a room that another window covers brings it forward instead of closing it', async () => {
  dashboard.open();
  // Another tool window drawn over the room.
  const coverZ = Number(getComputedStyle(root).zIndex) + 10;
  const cover = document.createElement('div');
  cover.className = 'modal';
  cover.style.zIndex = String(coverZ);
  document.body.appendChild(cover);

  dashboard.toggle();

  assert.equal(room.isOpen(), true);
  assert.ok(Number(root.style.zIndex) > coverZ, `room z-index ${root.style.zIndex}, cover ${coverZ}`);
  cover.remove();
  dashboard.close();
});
