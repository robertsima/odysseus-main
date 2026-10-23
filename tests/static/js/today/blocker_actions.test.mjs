// A blocker that carries recovery actions shows them as buttons; one without stays text only.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { item, openToday, waitFor } from './_today.mjs';

const view = await openToday({
  today: [
    item('outcome:o1', {
      title: 'Explain the importer', source: 'outcome', why: 'Blocked', state: 'blocked', open_url: null,
      blocker: { summary: 'No code folder was chosen.', recovery: 'user', user_action: 'Choose one.',
        actions: [{ id: 'run_without_files', label: 'Run without files', requires: ['no_workspace'] }] },
      actions: ['defer'],
    }),
    item('outcome:o2', {
      title: 'Other', source: 'outcome', why: 'Blocked', state: 'blocked', open_url: null,
      blocker: { summary: 'Needs you.', recovery: 'user', user_action: 'Look.' }, actions: ['defer'],
    }),
  ],
});
await view.show();

test('recovery actions on a blocker render as buttons', () => {
  const labels = [...view.row('outcome:o1').querySelectorAll('[data-today-blocker-action]')].map((b) => b.textContent);
  assert.deepEqual(labels, ['Run without files']);
  assert.equal(view.row('outcome:o2').querySelectorAll('[data-today-blocker-action]').length, 0);
});

test('a recovery button opens the assign sheet with the item as the task', async () => {
  view.fake.route('GET', '/api/agents/profiles', () => ({ profiles: [] }));
  view.click(view.row('outcome:o1').querySelector('[data-today-blocker-action]'));
  await waitFor(() => document.querySelector('#aa-overlay [data-aa-field="task"]'), { what: 'the assign sheet' });
  assert.equal(document.querySelector('[data-aa-field="task"]').value, 'Explain the importer');
  assert.match(document.querySelector('[data-aa-part="applied"]').textContent, /Running without files/);
});
