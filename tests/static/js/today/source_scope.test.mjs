import assert from 'node:assert/strict';
import { test } from 'node:test';
import { openToday, item } from './_today.mjs';

const view = await openToday({ today: [item('todoist:t1'), item('outcome:o1', { source: 'outcome' })] });

test('completion controls distinguish a source write from a local closure', async () => {
  await view.show();
  assert.equal(view.row('todoist:t1').querySelector('[data-today-action="done"]').textContent, 'Complete in Todoist');
  assert.equal(view.row('outcome:o1').querySelector('[data-today-action="done"]').textContent, 'Close outcome');
  assert.match(view.row('outcome:o1').textContent, /does not complete linked tasks/);
});

test('source availability and overflow are visible without implying a complete backlog', async () => {
  view.payload.sources = { todoist: 'ready' };
  view.payload.scope = { remaining: 4 };
  await view.today.refresh();
  assert.match(view.body().textContent, /4 more eligible items/);
  assert.equal(view.body().querySelector('.today-scope a').href, 'https://app.todoist.com/app/today');
  view.payload.sources.todoist = 'restricted';
  await view.today.refresh();
  assert.match(view.body().textContent, /not available to this account/);
  assert.equal(view.body().querySelector('.today-scope a'), null);
});
