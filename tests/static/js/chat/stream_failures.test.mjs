// What static/js/chat.js does when a live reply fails. The server runs the
// turn detached from the request, so:
// - a provider error the server reports (an `event: error` frame) is final:
//   the chat reloads the record the server saved, and nothing is sent again;
// - a dropped connection reattaches to the same run through
//   GET /api/chat/resume/{id}. Posting the message again would start a second
//   run on the selected model and repeat any tool side effects.
import assert from 'node:assert/strict';
import { after, beforeEach, test } from 'node:test';

import { DONE, openChatPage, sseResponse } from './_page.mjs';

const page = await openChatPage({ mode: 'agent' });
after(() => page.close());

const requests = (method, path) => page.fake.calls.filter((c) => c.method === method && c.url.pathname === path).length;
const ERROR_FRAME = 'event: error\ndata: {"status": 502, "error": "upstream refused the request"}\n\n';

let resumes;
page.fake.route('GET', '/api/chat/resume/s1', () => resumes.shift() || new Response(null, { status: 204 }));

beforeEach(() => {
  page.posts.length = 0;
  page.fake.calls.length = 0;
  resumes = [];
  page.history.s1 = [];
});

test('a provider error after partial output reloads the saved record and sends nothing again', async () => {
  page.history.s1 = [
    { role: 'user', content: 'summarise the report' },
    { role: 'assistant', content: 'The report covers (saved before the error)' },
  ];
  page.reply([{ delta: 'The report covers' }, ERROR_FRAME]);

  await page.send('summarise the report');
  await page.waitFor(() => page.chatBox().textContent.includes('(saved before the error)'), {
    what: 'the saved record to load',
  });

  assert.equal(page.posts.length, 1, 'the message was posted once');
  assert.equal(requests('GET', '/api/chat/resume/s1'), 0, 'no reconnect to a run that has ended');
});

test('a provider error before any output is shown in the reply', async () => {
  page.reply([ERROR_FRAME]);

  await page.send('hello');

  const last = page.aiMessages().at(-1);
  assert.ok(last.textContent.includes('upstream refused the request'), last.textContent);
  assert.equal(page.posts.length, 1);
  assert.equal(requests('GET', '/api/chat/resume/s1'), 0);
});

test('a dropped connection reattaches to the running turn instead of posting again', async () => {
  resumes.push(sseResponse([{ delta: 'The plan has three steps.' }, DONE]));
  page.reply(sseResponse([{ delta: 'The plan' }], { drop: true }));

  await page.send('make a plan');
  await page.waitFor(() => requests('GET', '/api/chat/resume/s1') === 1, { what: 'the reconnect' });
  await page.waitFor(() => page.chatBox().textContent.includes('three steps'), { what: 'the resumed reply' });

  assert.equal(page.posts.length, 1, 'the message was posted once');
});

test('a resumed turn that ends with agent_terminal reloads the saved record', async () => {
  resumes.push(sseResponse([
    { delta: 'Half of the answer' },
    { type: 'agent_terminal', data: { model: 'model-a' } },
    'event: error\ndata: {"status": 500, "error": "model crashed"}\n\n',
  ]));
  page.history.s1 = [
    { role: 'user', content: 'answer in full' },
    { role: 'assistant', content: 'Half of the answer (canonical record)' },
  ];
  page.reply(sseResponse([{ delta: 'Half' }], { drop: true }));

  await page.send('answer in full');
  await page.waitFor(() => requests('GET', '/api/chat/resume/s1') === 1, { what: 'the reconnect' });
  await page.waitFor(() => page.chatBox().textContent.includes('(canonical record)'), {
    what: 'the saved record to load',
  });

  assert.equal(page.posts.length, 1);
});
