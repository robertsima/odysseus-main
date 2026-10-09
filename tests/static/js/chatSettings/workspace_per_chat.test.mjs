// The workspace belongs to the chat (static/js/chatSettings.js restore,
// static/js/workspace.js setWorkspace). It lived in the browser, so a new chat
// inherited the last chat's folder and a chat's tools changed with whatever
// folder the browser held (2026-10-09).
import assert from 'node:assert/strict';
import { after, test } from 'node:test';

import { installDom } from '../_support/dom.mjs';
import { installFetchFake } from '../_support/fetchFake.mjs';

const dom = installDom();
const fake = installFetchFake();
// chatSettings polls the open chat id on an interval; the test drives switches itself.
globalThis.setInterval = () => 0;
after(async () => {
  fake.restore();
  await dom.restore();
});

let openChat = null;
window.sessionModule = { getCurrentSessionId: () => openChat };
const saved = {
  a: { toggles: { mode: 'agent' }, workspace: '/srv/project-a' },
  b: { toggles: { mode: 'agent' } },
};
const patches = [];
fake.route('GET', /^\/api\/session\/[^/]+\/settings$/, ({ url }) => {
  const id = url.pathname.split('/')[3];
  return { settings: saved[id] || {} };
});
fake.route('PATCH', /^\/api\/session\/[^/]+\/settings$/, ({ url, body }) => {
  const id = url.pathname.split('/')[3];
  const patch = JSON.parse(body);
  patches.push([id, patch.workspace]);
  saved[id] = { ...(saved[id] || {}), workspace: patch.workspace || undefined };
  return { settings: saved[id] };
});

const chatSettings = (await import('../../../../static/js/chatSettings.js')).default;
const workspace = (await import('../../../../static/js/workspace.js')).default;

async function open(id) {
  openChat = id;
  await chatSettings.onSessionSwitch(id);
}
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

test('each chat gets its own folder and another chat\'s never carries over', async () => {
  await open('a');
  assert.equal(workspace.getWorkspace(), '/srv/project-a');

  await open('b');
  assert.equal(workspace.getWorkspace(), '');

  await open('c');
  assert.equal(workspace.getWorkspace(), '');
});

test("a chat that has not run yet does not inherit the previous chat's folder", async () => {
  await open('a');
  await open('e');

  assert.equal(workspace.getWorkspace(), '');
});

test('picking a folder saves it on the open chat', async () => {
  await open('b');
  workspace.setWorkspace('/srv/project-b');
  await settle();

  assert.deepEqual(patches.at(-1), ['b', '/srv/project-b']);
  await open('a');
  await open('b');
  assert.equal(workspace.getWorkspace(), '/srv/project-b');
});

test('a new chat starts empty and keeps a folder picked before its first message', async () => {
  await open('a');
  await open(null);
  assert.equal(workspace.getWorkspace(), '');

  workspace.setWorkspace('/srv/new-work');
  await open('d');
  await settle();

  assert.equal(workspace.getWorkspace(), '/srv/new-work');
  assert.deepEqual(patches.at(-1), ['d', '/srv/new-work']);
});
