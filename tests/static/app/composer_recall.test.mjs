// ArrowUp in the composer on the booted app (static/app.js wiring
// static/js/composerArrowUpRecall.js through chat.js). On an empty composer it
// recalls the last prompt; on a multi-line draft it must leave the text alone
// so the caret can move up a line. app.js once carried a second copy of the
// recall handler without that guard, and because it stopped the event it won:
// ArrowUp replaced a half-written prompt with the last one sent (#5862).
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { openAppPage } from './_appPage.mjs';

const page = await openAppPage({
  routes(fake) {
    fake.route('GET', '/api/sessions', () => [
      { id: 's1', name: 'chat', endpoint_url: 'http://model.test/v1', model: 'm', endpoint_id: 'ep', archived: false },
    ]);
    fake.route('GET', '/api/history/s1', () => ({
      history: [{ role: 'user', content: 'previous prompt' }, { role: 'assistant', content: 'answer' }],
      model: 'm', total: 2,
    }));
  },
});
await page.sessionModule.selectSession('s1', { showLoading: false });
const composer = document.getElementById('message');

function pressArrowUp() {
  const event = new KeyboardEvent('keydown', { key: 'ArrowUp', bubbles: true, cancelable: true });
  composer.dispatchEvent(event);
  return event;
}

test('ArrowUp recalls the last prompt into an empty composer and leaves a multi-line draft alone', async () => {
  composer.value = '';
  pressArrowUp();
  assert.equal(composer.value, 'previous prompt');
  await new Promise((resolve) => setTimeout(resolve, 10)); // the user starts typing afterwards

  const draft = 'first line of a new question\nsecond line';
  composer.value = draft;
  composer.dispatchEvent(new Event('input', { bubbles: true }));
  composer.selectionStart = composer.selectionEnd = draft.length;
  const event = pressArrowUp();

  assert.equal(composer.value, draft);
  assert.equal(event.defaultPrevented, false);
});
