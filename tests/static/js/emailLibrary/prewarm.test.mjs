// Background email prewarm (static/js/emailLibrary.js). Before the person
// opens Email, the page may fetch the first inbox page so the library opens
// instantly. That fetch is optional and goes to the mail server, so it must
// only run in genuine idle time, never while the page is hidden or a chat is
// busy, at most one at a time, for one account, one bounded page, and it must
// give way as soon as the person opens or closes the library.
import assert from 'node:assert/strict';
import { after, beforeEach, test } from 'node:test';

import { installDom, waitFor } from '../_support/dom.mjs';
import { installFetchFake } from '../_support/fetchFake.mjs';

const dom = installDom({ width: 1440 });
const fake = installFetchFake();
after(async () => {
  fake.restore();
  await dom.restore();
});

// requestIdleCallback under the test's control: callbacks wait in `idle`
// until fireIdle() runs the oldest one.
const idle = new Map();
let nextIdle = 1;
window.requestIdleCallback = (callback) => {
  const handle = nextIdle++;
  idle.set(handle, callback);
  return handle;
};
window.cancelIdleCallback = (handle) => idle.delete(handle);
function fireIdle({ didTimeout = false, remaining = 10 } = {}) {
  const next = idle.entries().next().value;
  assert.ok(next, 'an idle callback is pending');
  idle.delete(next[0]);
  next[1]({ didTimeout, timeRemaining: () => remaining });
}

const noop2d = new Proxy({}, { get: () => () => ({ addColorStop() {} }) });
HTMLCanvasElement.prototype.getContext = () => noop2d;

fake.route('GET', '/api/email/accounts', () => ({
  accounts: [
    { id: 'work', enabled: true },
    { id: 'home', enabled: true, is_default: true },
    { id: 'retired', enabled: false },
  ],
}));
fake.route('GET', '/api/email/list', () => ({ emails: [{ uid: 1, subject: 'hi', folder: 'INBOX' }], total: 1, sync: {} }));

document.body.innerHTML = '<div id="toast"></div>';
const emailLibrary = await import('../../../../static/js/emailLibrary.js');
const { state } = await import('../../../../static/js/emailLibrary/state.js');

function emailCalls() {
  return fake.calls
    .filter((c) => c.url.pathname.startsWith('/api/email/'))
    .map((c) => ({ path: c.url.pathname, params: Object.fromEntries(c.url.searchParams) }));
}
function listCalls() {
  return emailCalls().filter((c) => c.path === '/api/email/list');
}
// A promise that has not settled within `ms` counts as still pending.
function settledWithin(promise, ms = 200) {
  return Promise.race([promise, new Promise((resolve) => setTimeout(() => resolve('pending'), ms))]);
}

beforeEach(() => {
  fake.calls.length = 0;
  idle.clear();
});

test('the library prewarm waits for idle time, then fetches one first page of the default account', async () => {
  const pending = emailLibrary.prewarmEmailLibrary({ delay: 0 });
  assert.deepEqual(emailCalls(), [], 'nothing is fetched before idle time');

  fireIdle({ didTimeout: true });
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.deepEqual(emailCalls(), [], 'an idle callback that timed out is not idle time');

  await waitFor(() => idle.size === 1, { what: 'the retry to ask for idle time again' });
  fireIdle();
  assert.equal(await pending, true);

  assert.deepEqual(emailCalls().map((c) => c.path), ['/api/email/accounts', '/api/email/list']);
  const [list] = listCalls();
  assert.equal(list.params.folder, 'INBOX');
  assert.equal(list.params.limit, '100');
  assert.equal(list.params.offset, '0');
  assert.equal(list.params.account_id, 'home');
});

test('a second warm while one is pending joins it', async () => {
  const first = emailLibrary.prewarmUnreadEmails({ limit: 5 });
  const second = emailLibrary.prewarmUnreadEmails({ limit: 6 });
  assert.equal(second, first);

  fireIdle();
  assert.equal(await first, true);
  assert.equal(listCalls().length, 1);
  assert.equal(listCalls()[0].params.filter, 'unread');
});

test('the unread warm fetches at most 20 messages', async () => {
  const pending = emailLibrary.prewarmUnreadEmails({ limit: 50 });
  fireIdle();
  await pending;

  assert.equal(listCalls()[0].params.limit, '20');
});

test('without requestIdleCallback nothing is fetched', async () => {
  const saved = window.requestIdleCallback;
  delete window.requestIdleCallback;
  try {
    const pending = emailLibrary.prewarmUnreadEmails({ limit: 7 });
    await new Promise((resolve) => setTimeout(resolve, 50));
    assert.equal(await settledWithin(pending), false);
  } finally {
    window.requestIdleCallback = saved;
  }
  assert.deepEqual(emailCalls(), []);
});

test('a hidden page fetches nothing', async () => {
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' });
  try {
    const pending = emailLibrary.prewarmUnreadEmails({ limit: 8 });
    fireIdle();
    assert.equal(await settledWithin(pending), false);
  } finally {
    delete document.visibilityState;
  }
  assert.deepEqual(listCalls(), []);
});

test('a busy chat holds the warm back until it is idle again', async () => {
  window.__odysseusChatBusy = true;
  const pending = emailLibrary.prewarmUnreadEmails({ limit: 9 });
  fireIdle();
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.deepEqual(listCalls(), [], 'nothing runs while the chat is busy');

  window.__odysseusChatBusy = false;
  window.dispatchEvent(new Event('odysseus:chat-busy-change'));
  await waitFor(() => idle.size === 1, { what: 'the warm to ask for idle time again' });
  fireIdle();

  assert.equal(await pending, true);
  assert.equal(listCalls().length, 1);
});

test('the warm uses the remembered account when it is enabled, else the default', async () => {
  state._libAccountId = null;
  localStorage.setItem('odysseus.email.lastAccountId', 'work');
  let pending = emailLibrary.prewarmUnreadEmails({ limit: 10 });
  fireIdle();
  await pending;
  assert.equal(listCalls().at(-1).params.account_id, 'work');

  state._libAccountId = null;
  localStorage.setItem('odysseus.email.lastAccountId', 'retired');
  pending = emailLibrary.prewarmUnreadEmails({ limit: 11 });
  fireIdle();
  await pending;
  assert.equal(listCalls().at(-1).params.account_id, 'home');
});

test('opening the library cancels a pending warm', async () => {
  const pending = emailLibrary.prewarmUnreadEmails({ limit: 12 });
  emailLibrary.openEmailLibrary();
  try {
    assert.equal(idle.size, 0, 'the idle request is withdrawn');
    assert.equal(await settledWithin(pending), false);
  } finally {
    emailLibrary.closeEmailLibrary();
  }
});

test('closing the library cancels a warm queued while it was open', async () => {
  emailLibrary.openEmailLibrary();
  const pending = emailLibrary.prewarmUnreadEmails({ limit: 13 });
  emailLibrary.closeEmailLibrary();

  assert.equal(idle.size, 0, 'the idle request is withdrawn');
  assert.equal(await settledWithin(pending), false);
});
