// Tool-approval cards in static/js/chatRenderer.js check the server record
// behind them. A card whose record expired or was replaced used to keep its
// Allow button, and clicking it answered 409 with no reason (2026-10-02).
import assert from 'node:assert/strict';
import { after, beforeEach, test } from 'node:test';

import { installDom, waitFor } from '../_support/dom.mjs';
import { installFetchFake } from '../_support/fetchFake.mjs';

const dom = installDom();
const fake = installFetchFake();
after(async () => {
  fake.restore();
  await dom.restore();
});

const statuses = new Map();
fake.route('GET', /^\/api\/agents\/approvals\/[^/]+\/status$/, ({ url }) => {
  const id = decodeURIComponent(url.pathname.split('/')[4]);
  return { status: statuses.get(id) || 'pending' };
});

const { renderAskUserCard } = await import('../../../../static/js/chatRenderer.js');

function approvalCard(id, options = {}) {
  const root = document.getElementById('chat-history');
  return renderAskUserCard({
    kind: 'tool_approval',
    approval_id: id,
    session_id: 's1',
    question: 'Run this command?',
    options: [{ label: 'Allow', value: 'approve' }, { label: 'Deny', value: 'deny' }],
    action: { tool: 'bash', content: 'echo hi' },
  }, { root, scroll: false, focus: false, ...options });
}

const buttons = (card) => [...card.querySelectorAll('.ask-user-option')];

beforeEach(() => {
  document.body.innerHTML = '<div id="chat-history"></div>';
  statuses.clear();
});

test('a card whose approval expired loses its buttons', async () => {
  statuses.set('a-expired', 'expired');

  const card = approvalCard('a-expired');

  await waitFor(() => card.classList.contains('ask-user-card-lapsed'), { what: 'the card to lapse' });
  assert.ok(buttons(card).every((button) => button.disabled));
  assert.ok(card.querySelector('.ask-user-lapsed'));
});

test('a card restored from history stays disabled until the server says it is pending', async () => {
  const card = approvalCard('a-pending', { restored: true });

  assert.ok(buttons(card).every((button) => button.disabled), 'disabled before the check');
  await waitFor(() => buttons(card).every((button) => !button.disabled), { what: 'the buttons to enable' });
  assert.ok(!card.classList.contains('ask-user-card-lapsed'));
});

test('dismissing an approval card cancels the pending approval click', () => {
  const card = approvalCard('a-dismissed');
  let cancelled = 0;
  document.addEventListener('odysseus:tool-approval-cancel', () => { cancelled += 1; });

  card.querySelector('.ask-user-close').click();

  assert.equal(cancelled, 1);
});
