// Nothing urgent is a calm page, not an error: say so plainly and keep the capture box.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { openToday } from './_today.mjs';

const view = await openToday({});
await view.show();

test('an empty Today says nothing is urgent and offers capture', () => {
  assert.match(view.root.querySelector('.today-empty-title').textContent, /Nothing urgent right now/);
  assert.equal(view.rows().length, 0);
  assert.ok(view.root.querySelector('#today-capture-input'));
  assert.equal(view.root.querySelector('.today-degraded'), null);
});
