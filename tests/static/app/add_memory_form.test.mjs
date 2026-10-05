// The Brain > Add Memory form in the app shell (#5828). The form once had no
// submit button and relied on a keypress listener for Enter, so on some
// platforms there was no way to submit. The button and Enter (keydown) both
// add the memory; Enter during IME composition only confirms the composition.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { openAppPage, waitFor } from './_appPage.mjs';

const added = [];
await openAppPage({
  routes(fake) {
    fake.route('GET', '/api/sessions', () => []);
    fake.route('POST', '/api/memory/add', ({ body }) => {
      added.push(JSON.parse(body));
      return { ok: true };
    });
    fake.route('GET', /^\/api\/memor/, () => ({ memories: [], total: 0 }));
  },
});

const input = () => document.getElementById('new-memory-input');

function pressEnter(init = {}) {
  const event = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true, ...init });
  input().dispatchEvent(event);
  return event;
}

test('the Add button adds the typed memory', async () => {
  input().value = 'I prefer metric units';
  document.getElementById('new-memory-add-btn').click();

  await waitFor(() => added.length === 1, { what: 'the memory POST' });
  assert.equal(added[0].text, 'I prefer metric units');
});

test('Enter adds the memory and does not submit a surrounding form', async () => {
  added.length = 0;
  input().value = 'My cat is called Pixel';

  const event = pressEnter();

  assert.equal(event.defaultPrevented, true);
  await waitFor(() => added.length === 1, { what: 'the memory POST' });
  assert.equal(added[0].text, 'My cat is called Pixel');
});

test('Enter that confirms an IME composition adds nothing', async () => {
  added.length = 0;
  input().value = 'こんにちは';

  pressEnter({ isComposing: true });
  await new Promise((resolve) => setTimeout(resolve, 100));

  assert.deepEqual(added, []);
});
