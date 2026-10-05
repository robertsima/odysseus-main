// Bulk Mark Read / Mark Unread in the email library (static/js/emailLibrary.js).
// The library is a view of the mail server: marking messages read only in the
// page makes them unread again on the next refresh (#800). Each selected
// message must go to the provider's mark-read or mark-unread route, a refusal
// from the server must leave the message as it was, and a selection must not
// survive a change of folder, because IMAP UIDs are only unique per folder.
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

const EMAILS = [
  { uid: 11, subject: 'Invoice', from_name: 'Ann', from_address: 'ann@example.test', date: '2026-10-01T09:00:00Z', is_read: false, folder: 'INBOX' },
  { uid: 12, subject: 'Lunch', from_name: 'Bo', from_address: 'bo@example.test', date: '2026-10-01T10:00:00Z', is_read: false, folder: 'INBOX' },
];
let markReply = { success: true };
const marked = [];

// Each test opens its own account, so the library's per-account list cache
// from one test never paints another test's view.
const ACCOUNTS = ['read', 'unread', 'refused', 'folder', 'search'].map((id, i) => ({
  id, enabled: true, is_default: i === 0, from_address: `${id}@example.test`,
}));
fake.route('GET', '/api/email/accounts', () => ({ accounts: ACCOUNTS }));
fake.route('GET', '/api/email/folders', () => ({ folders: ['INBOX', 'Archive'] }));
fake.route('GET', '/api/email/list', () => ({ emails: EMAILS.map((e) => ({ ...e })), total: EMAILS.length, sync: {} }));
fake.route('POST', /^\/api\/email\/mark-(read|unread)\/\d+$/, ({ url }) => {
  const [, , , route, uid] = url.pathname.split('/');
  marked.push({ route, uid, folder: url.searchParams.get('folder') });
  return markReply;
});

// The bulk buttons show a canvas spinner while they work; happy-dom has no 2D canvas.
const noop2d = new Proxy({}, { get: () => () => ({ addColorStop() {} }) });
HTMLCanvasElement.prototype.getContext = () => noop2d;

document.body.innerHTML = '<div id="toast"></div>';
const { openEmailLibrary, closeEmailLibrary } = await import('../../../../static/js/emailLibrary.js');

// Clicks land away from the top-left corner, like a pointer would.
function click(el) {
  el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, clientX: 400, clientY: 200 }));
}

function cards() {
  return [...document.querySelectorAll('#email-lib-grid .doclib-card[data-uid]')];
}

function isUnread(card) {
  return card.classList.contains('email-card-unread');
}

async function openAndSelectAll(account) {
  closeEmailLibrary();
  openEmailLibrary({ account_id: account });
  await waitFor(() => cards().length === EMAILS.length && cards().every(isUnread), { what: 'the unread email cards' });
  click(document.getElementById('email-lib-select-btn'));
  const all = document.getElementById('email-lib-select-all');
  all.checked = true;
  all.dispatchEvent(new Event('change', { bubbles: true }));
}

async function runBulk(label) {
  click(document.getElementById('email-lib-bulk-actions'));
  const item = [...document.querySelectorAll('.email-bulk-menu .dropdown-item-compact')]
    .find((el) => el.textContent.trim() === label);
  click(item);
  await waitFor(() => marked.length === EMAILS.length, { what: 'one request per selected email' });
  await waitFor(() => document.getElementById('email-lib-bulk').classList.contains('hidden'), { what: 'the bulk action to finish' });
}

beforeEach(() => {
  marked.length = 0;
  markReply = { success: true };
});

test('Mark Read sends each selected email to the provider and shows it read', async () => {
  await openAndSelectAll('read');

  await runBulk('Mark Read');

  assert.deepEqual(marked.map((m) => [m.route, m.uid, m.folder]).sort(), [
    ['mark-read', '11', 'INBOX'],
    ['mark-read', '12', 'INBOX'],
  ]);
  assert.equal(cards().some(isUnread), false);
});

test('Mark Unread sends each selected email to the provider', async () => {
  await openAndSelectAll('unread');

  await runBulk('Mark Unread');

  assert.deepEqual(marked.map((m) => m.route), ['mark-unread', 'mark-unread']);
});

test('a refused Mark Read leaves the emails unread', async () => {
  markReply = { success: false, error: 'IMAP store failed' };
  await openAndSelectAll('refused');

  await runBulk('Mark Read');

  assert.ok(cards().every(isUnread));
});

test('changing folder clears the selection', async () => {
  await openAndSelectAll('folder');
  assert.equal(document.getElementById('email-lib-bulk').classList.contains('hidden'), false);

  const folder = document.getElementById('email-lib-folder');
  folder.value = 'Archive';
  folder.dispatchEvent(new Event('change', { bubbles: true }));

  assert.ok(document.getElementById('email-lib-bulk').classList.contains('hidden'), 'select mode ends');
  assert.equal(document.getElementById('email-lib-selected-count').textContent, '0 Selected');
});

test('typing a search clears the selection', async () => {
  await openAndSelectAll('search');
  assert.equal(document.getElementById('email-lib-bulk').classList.contains('hidden'), false);

  const search = document.getElementById('email-lib-search');
  search.value = 'invoice';
  search.dispatchEvent(new Event('input', { bubbles: true }));

  assert.ok(document.getElementById('email-lib-bulk').classList.contains('hidden'), 'select mode ends');
  assert.equal(document.getElementById('email-lib-selected-count').textContent, '0 Selected');
});
