// What the buttons send, and what the outcome detail does.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { item, openToday, waitFor } from './_today.mjs';

const sent = [];
const view = await openToday({
  today: [
    item('todoist:t1', { title: 'Pay rent', why: 'Overdue since Tue' }),
    item('outcome:o1', {
      title: 'Ship the importer', source: 'outcome', why: 'In progress', state: 'active', open_url: '/?session=c1',
      actions: ['resume', 'defer', 'wait', 'done', 'open'],
    }),
  ],
  onAction: (body) => {
    sent.push(body);
    if (body.key === 'outcome:o2') {
      return {
        ok: false, error: 'Worker not started: no workspace',
        blocker: { summary: 'The worker was not started because its workspace is not usable.', user_action: 'Pick a repository folder and start again.' },
      };
    }
    return body.action === 'start' ? { ok: true, open_url: '/?session=c9' } : { ok: true };
  },
});
view.fake.route('GET', '/api/outcomes/o1', () => ({
  outcome: {
    id: 'o1', title: 'Ship the importer', state: 'active', next_action: 'Review the diff', detail: null,
    blocker: null, confirmed_by_person: false, evidence: [{ kind: 'commit', ref: 'abc1234', detail: 'fix parser', by: 'agent:w1' }],
    links: [{ id: 'l1', kind: 'chat', ref: 'c1', label: null }, { id: 'l2', kind: 'pull_request', ref: 'https://example.test/pr/4', label: null }],
    events: [{ id: 'e1', kind: 'state', actor: 'person', detail: { from: 'open', to: 'active' } }],
  },
}));
view.fake.route('POST', '/api/outcomes/o1/state', () => ({ outcome: { id: 'o1', state: 'done' } }));
await view.show();

test('a button sends that item key and that action', async () => {
  view.click(view.row('todoist:t1').querySelector('[data-today-action="defer"]'));
  await waitFor(() => sent.length === 1, { what: 'the action' });
  assert.deepEqual(sent[0], { key: 'todoist:t1', action: 'defer' });
  await waitFor(() => /Snoozed/.test(view.root.querySelector('.today-status')?.textContent || ''), { what: 'the confirmation' });
});

test('start opens the chat the server made', async () => {
  view.click(view.row('todoist:t1').querySelector('[data-today-action="start"]'));
  await waitFor(() => view.selected.length === 1, { what: 'the chat to open' });
  assert.deepEqual(view.selected, ['c9']);
  assert.equal(view.root.hidden, true, 'Today closes when the chat opens');
  view.today.open();
});

test('wait asks who first and sends waiting_on, with the date only when one is given', async () => {
  const before = sent.length;
  view.click(view.row('todoist:t1').querySelector('[data-today-action="wait"]'));
  const form = await waitFor(() => view.root.querySelector('[data-today-wait]'), { what: 'the wait form' }) || view.root.querySelector('[data-today-wait]');
  assert.equal(sent.length, before, 'nothing is sent until the person says who');
  form.querySelector('input[name="waiting_on"]').value = 'Dana';
  form.dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
  await waitFor(() => sent.length === before + 1, { what: 'the wait' });
  assert.deepEqual(sent.at(-1), { key: 'todoist:t1', action: 'wait', waiting_on: 'Dana' });
});

test('a refused action shows the reason and the blocker, in plain words', async () => {
  view.payload.today = [item('outcome:o2', { source: 'outcome', actions: ['resume'], open_url: null })];
  await view.today.refresh();
  await waitFor(() => view.row('outcome:o2'), { what: 'the new list' });
  view.click(view.row('outcome:o2').querySelector('[data-today-action="resume"]'));
  await waitFor(() => view.root.querySelector('.today-status[data-tone="error"]'), { what: 'the error' });
  const status = view.root.querySelector('.today-status').textContent;
  assert.match(status, /Worker not started: no workspace/);
  assert.match(status, /Pick a repository folder and start again\./);
});

test('clicking an outcome opens its detail: next action, links, evidence and history', async () => {
  view.payload.today = [item('outcome:o1', {
    title: 'Ship the importer', source: 'outcome', why: 'In progress', state: 'active', open_url: '/?session=c1', actions: ['done'],
  })];
  await view.today.refresh();
  await waitFor(() => view.row('outcome:o1'), { what: 'the list' });
  view.click(view.row('outcome:o1').querySelector('[data-today-open]'));
  await waitFor(() => view.root.querySelector('.today-detail'), { what: 'the detail' });

  const text = view.root.querySelector('.today-detail').textContent;
  assert.match(text, /In progress/);
  assert.match(text, /Review the diff/);
  assert.match(text, /fix parser/);
  assert.match(text, /open to active/);
  const links = [...view.root.querySelectorAll('.today-detail a')].map((a) => a.getAttribute('href'));
  assert.deepEqual(links, ['/?session=c1', 'https://example.test/pr/4']);
});

test('Mark done sends the person\'s confirmation, and says so on the button', async () => {
  const button = view.root.querySelector('[data-today-confirm-done]');
  assert.match(button.textContent, /record my confirmation/);
  view.click(button);
  await waitFor(() => view.posts('/api/outcomes/o1/state').length === 1, { what: 'the state change' });
  assert.deepEqual(view.posts('/api/outcomes/o1/state')[0], { state: 'done', confirm: true });
  await waitFor(() => view.root.querySelector('.today-lists'), { what: 'the list to return' });
  assert.match(view.root.querySelector('.today-status').textContent, /confirmation is recorded/);
});
