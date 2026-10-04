// Older-history paging in static/js/sessions.js. Opening a chat renders only
// its newest page; scrolling to the top fetches the page before it and
// inserts it above. The two pages must meet exactly: one message too many
// at the seam shows the same bubble twice.
import assert from 'node:assert/strict';
import { after, test } from 'node:test';

import { installDom, waitFor } from '../_support/dom.mjs';
import { installFetchFake } from '../_support/fetchFake.mjs';

const dom = installDom({ width: 1440 });
const fake = installFetchFake();
after(async () => {
  fake.restore();
  await dom.restore();
});

const MESSAGES = Array.from({ length: 30 }, (_, i) => ({
  role: i % 2 ? 'assistant' : 'user',
  content: `message ${i}`,
}));

// Pages the way GET /api/history/{id} does (routes/history/history_routes.py):
// without an offset it returns the newest `limit` messages.
fake.route('GET', '/api/history/s1', ({ url }) => {
  const total = MESSAGES.length;
  const limit = Number(url.searchParams.get('limit'));
  const offset = url.searchParams.has('offset')
    ? Number(url.searchParams.get('offset'))
    : Math.max(total - limit, 0);
  return {
    history: MESSAGES.slice(offset, offset + limit),
    model: 'test-model',
    offset,
    limit,
    total,
    has_more_before: offset > 0,
  };
});

document.body.innerHTML = '<div id="chat-history"></div>';
const { selectSession } = await import('../../../../static/js/sessions.js');

function shownMessages(box) {
  return [...box.querySelectorAll('.msg')].map((el) => el.dataset.raw);
}

test('scrolling up to the older page shows every message once, in order', async () => {
  const box = document.getElementById('chat-history');
  await selectSession('s1', { showLoading: false });
  const firstPage = shownMessages(box).length;
  assert.ok(firstPage < MESSAGES.length, 'the chat opens on its newest page only');

  box.scrollTop = 0;
  box.dispatchEvent(new Event('scroll'));
  await waitFor(() => shownMessages(box).length > firstPage, { what: 'the older page to render' });

  assert.deepEqual(shownMessages(box), MESSAGES.map((m) => m.content));
});
