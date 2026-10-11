// The capture line: POST /api/capture, and what each answer does to the page.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { openToday, waitFor } from './_today.mjs';

const answers = [];
const view = await openToday({ onCapture: () => answers.shift() });
await view.show();

const captures = () => view.posts('/api/capture');
const input = () => view.root.querySelector('#today-capture-input');

test('created: says where it went, links to it, clears the line and refreshes Today', async () => {
  answers.push({ status: 'created', destination: 'todoist', ref: 't9', open_url: 'https://app.todoist.com/app/task/t9' });
  const reads = view.reads();
  view.type('Pay rent tomorrow');
  view.submit();
  await waitFor(() => view.root.querySelector('.today-capture-note'), { what: 'the confirmation' });

  const note = view.root.querySelector('.today-capture-note');
  assert.match(note.textContent, /Added to Todoist/);
  assert.equal(note.querySelector('a').getAttribute('href'), 'https://app.todoist.com/app/task/t9');
  assert.equal(input().value, '');
  await waitFor(() => view.reads() > reads, { what: 'Today to refresh' });
  assert.equal(captures()[0].text, 'Pay rent tomorrow');
  assert.ok(captures()[0].idempotency_key);
});

test('duplicate reads the same as created, so a double submit looks like one add', async () => {
  answers.push({ status: 'duplicate', destination: 'note', ref: 'n1', open_url: '/notes?id=n1' });
  view.type('Water plants');
  view.submit();
  await waitFor(() => /Added to Notes/.test(view.root.querySelector('.today-capture-note')?.textContent || ''), { what: 'the duplicate note' });
});

test('needs_clarification asks the question and resends with the chosen destination and the same key', async () => {
  answers.push({
    status: 'needs_clarification', question: 'Is this a task or an appointment?',
    options: [{ label: 'Task in Todoist', destination: 'todoist', due: '2026-10-12' }, { label: 'Calendar event', destination: 'calendar' }],
  });
  const before = captures().length;
  view.type('Dentist Friday');
  view.submit();
  await waitFor(() => view.root.querySelector('.today-clarify'), { what: 'the question' });
  assert.match(view.root.querySelector('.today-clarify').textContent, /Is this a task or an appointment\?/);
  const labels = [...view.root.querySelectorAll('[data-today-pick]')].map((b) => b.textContent);
  assert.deepEqual(labels, ['Task in Todoist', 'Calendar event']);

  answers.push({ status: 'created', destination: 'todoist', open_url: '' });
  view.click(view.root.querySelector('[data-today-pick="0"]'));
  await waitFor(() => captures().length === before + 2, { what: 'the resend' });

  const [first, second] = captures().slice(before);
  assert.equal(second.text, first.text);
  assert.equal(second.idempotency_key, first.idempotency_key, 'a resend must keep its key, or the server writes it twice');
  assert.equal(second.destination, 'todoist');
  assert.equal(second.due, '2026-10-12');
  assert.equal(first.destination, undefined);
});

test('failed shows the error, keeps the text, and a retry reuses the key', async () => {
  answers.push({ status: 'failed', error: 'Todoist did not answer in time.' });
  const before = captures().length;
  view.type('Call the bank');
  view.submit();
  await waitFor(() => view.root.querySelector('.today-capture-error'), { what: 'the error' });
  assert.match(view.root.querySelector('.today-capture-error').textContent, /Todoist did not answer in time\./);
  assert.equal(input().value, 'Call the bank');

  answers.push({ status: 'created', destination: 'todoist' });
  view.submit();
  await waitFor(() => captures().length === before + 2, { what: 'the retry' });
  const [first, retry] = captures().slice(before);
  assert.equal(retry.idempotency_key, first.idempotency_key);
});

test('a network failure is a failure too: the text stays', async () => {
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    if (String(url).includes('/api/capture')) throw new TypeError('network down');
    return realFetch(url, init);
  };
  try {
    view.type('Book the trip');
    view.submit();
    await waitFor(() => /network down/.test(view.root.querySelector('.today-capture-error')?.textContent || ''), { what: 'the error' });
    assert.equal(input().value, 'Book the trip');
  } finally {
    globalThis.fetch = realFetch;
  }
});

test('different text is a different submission with its own key', async () => {
  answers.push({ status: 'created', destination: 'todoist' }, { status: 'created', destination: 'todoist' });
  const before = captures().length;
  view.type('One thing');
  view.submit();
  await waitFor(() => captures().length === before + 1, { what: 'first' });
  await waitFor(() => !view.root.querySelector('#today-capture-input').disabled, { what: 'the input to unlock' });
  view.type('Another thing');
  view.submit();
  await waitFor(() => captures().length === before + 2, { what: 'second' });
  const [a, b] = captures().slice(before);
  assert.notEqual(a.idempotency_key, b.idempotency_key);
});

test('an unsafe link from the server is shown as text, never as a link', async () => {
  answers.push({ status: 'created', destination: 'note', open_url: 'javascript:alert(1)' });
  await waitFor(() => !view.root.querySelector('#today-capture-input').disabled, { what: 'the input to unlock' });
  view.type('Odd link');
  view.submit();
  await waitFor(() => view.root.querySelector('.today-capture-note'), { what: 'the note' });
  assert.equal(view.root.querySelector('.today-capture-note a')?.outerHTML, undefined, 'no anchor for an unsafe url');
});
