// What the Today view draws from a fixed /api/today payload.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { item, openToday } from './_today.mjs';

const view = await openToday({
  today: [
    item('todoist:t1', { title: 'Pay rent', why: 'Overdue since Tue', due: '2020-01-01' }),
    item('outcome:o1', {
      title: 'Ship the importer', source: 'outcome', why: 'Blocked, waiting on you', state: 'blocked', open_url: null,
      blocker: { summary: 'The worker needs input from you.', recovery: 'user', user_action: "Paste the staging deploy key into note 'deploy'." },
      actions: ['resume', 'defer', 'wait', 'done'],
    }),
    item('event:e1', { title: 'Standup', source: 'calendar', why: 'On your calendar at 2:00 PM', actions: ['open'], open_url: '/calendar' }),
  ],
  next: [item('run:r1', {
    title: 'Fix the importer', source: 'agent_run', why: 'Interrupted 2 hours ago',
    actions: ['resume', 'defer', 'open'], open_url: '/?session=w1',
  })],
  degraded: ['todoist'],
});
await view.show();

test('items appear in the order the server ranked them, today before next', () => {
  assert.deepEqual(view.keys(), ['todoist:t1', 'outcome:o1', 'event:e1', 'run:r1']);
  const headings = [...view.root.querySelectorAll('.today-section-title')].map((h) => h.textContent);
  assert.deepEqual(headings, ['Today', 'Next']);
});

test('every item shows its one-line reason and a source label', () => {
  const row = view.row('todoist:t1');
  assert.equal(row.querySelector('.today-why').textContent, 'Overdue since Tue');
  assert.equal(row.querySelector('.today-source').textContent, 'Todoist');
  assert.equal(view.row('run:r1').querySelector('.today-source').textContent, 'Agent run');
});

test('a blocker shows what failed and the one step the person takes', () => {
  const blocker = view.row('outcome:o1').querySelector('.today-blocker');
  assert.match(blocker.textContent, /The worker needs input from you\./);
  assert.match(blocker.textContent, /You need to: Paste the staging deploy key into note 'deploy'\./);
});

test('buttons are exactly the actions the item allows, and Open needs somewhere to go', () => {
  assert.deepEqual(view.buttons('todoist:t1'), ['start', 'defer', 'wait', 'done', 'open']);
  assert.deepEqual(view.buttons('outcome:o1'), ['resume', 'defer', 'wait', 'done']);
  assert.deepEqual(view.buttons('event:e1'), ['open']);
  assert.deepEqual(view.buttons('run:r1'), ['resume', 'defer', 'open']);
});

test('only overdue and person-blocking items carry the attention mark', () => {
  const marks = Object.fromEntries(view.rows().map((li) => [li.dataset.key, li.dataset.attention || '']));
  assert.deepEqual(marks, { 'todoist:t1': 'overdue', 'outcome:o1': 'blocked', 'event:e1': '', 'run:r1': '' });
});

test('a source that failed is named, and the rest still shows', () => {
  assert.match(view.root.querySelector('.today-degraded').textContent, /Could not read Todoist/);
});

test('the capture line comes first, above both lists', () => {
  assert.equal(view.body().firstElementChild.id, 'today-capture');
  assert.equal(view.root.querySelector('#today-capture-input').placeholder, 'Capture a task, appointment or follow-up');
});
