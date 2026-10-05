// Live thinking in a streaming reply, in static/js/chat.js. Reasoning streams
// into a "Thinking…" box above the reply. The box has to end when thinking
// ends: on </think>, on a tool call, at the end of the stream, and after the
// short grace the page gives a suspiciously short <think>…</think> (models
// emit <think>The</think> and keep reasoning untagged) even when no further
// delta arrives. Long reasoning must not cost a full re-scan of the round per
// delta.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { openChatPage, waitFor } from './_chatPage.mjs';

const page = await openChatPage();
const markdownModule = (await import('../../../../static/js/markdown.js')).default;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function lastReply() {
  const replies = page.box.querySelectorAll('.msg-ai');
  return replies[replies.length - 1];
}

// The grace is never armed today: the branch that opens the live box reads
// _thinkingRecheckAt after _cancelLiveThinkingWork() has reset it to 0, so the
// reply stays inside "Thinking…" until the stream ends. Kept as a todo so it
// turns green when that is fixed.
test('a short think tag closes into the reply after its grace, with no further delta', {
  todo: 'chat.js arms the false-close grace after cancelling it (found 2026-10-04)',
}, async () => {
  const stream = page.streamReply();
  await page.send('capital of France?');
  try {
    stream.event({ delta: '<think>The</think>Paris is the capital of France.' });
    await waitFor(() => lastReply()?.querySelector('.thinking-section'), { what: 'the live thinking box' });

    await waitFor(() => !lastReply().querySelector('.thinking-section'), { timeout: 1500, what: 'the grace to end' });
    assert.match(lastReply().querySelector('.body').textContent, /Paris is the capital of France\./);
  } finally {
    stream.done();
    await page.settled();
  }
});

test('streaming reasoning does not re-normalize the whole round on every delta', async () => {
  const stream = page.streamReply();
  await page.send('think it through');
  stream.event({ delta: 'step 0. ', thinking: true });
  await waitFor(() => lastReply()?.querySelector('.thinking-section'), { what: 'the live thinking box' });

  const original = markdownModule.normalizeThinkingMarkup;
  let calls = 0;
  markdownModule.normalizeThinkingMarkup = (...args) => { calls += 1; return original(...args); };
  try {
    for (let i = 1; i <= 200; i += 1) stream.event({ delta: `step ${i}. `, thinking: true });
    await sleep(100);
  } finally {
    markdownModule.normalizeThinkingMarkup = original;
  }

  assert.ok(calls < 40, `normalizeThinkingMarkup ran ${calls} times for 200 reasoning deltas`);
  stream.event({ delta: 'Done.' });
  stream.done();
  await page.settled();
});

test('a tool call ends the live thinking box before the tool runs', async () => {
  const before = page.box.querySelectorAll('.thinking-section').length;
  const stream = page.streamReply();
  await page.send('look it up');
  stream.event({ delta: 'I should search for the population figure first.', thinking: true });
  await waitFor(() => lastReply()?.querySelector('.thinking-section'), { what: 'the live thinking box' });

  stream.event({ type: 'tool_start', tool: 'web_search', command: 'population' });
  await waitFor(() => page.box.querySelector('.agent-thread-node.running'), { what: 'the running tool step' });

  const sections = [...page.box.querySelectorAll('.thinking-section')].slice(before);
  assert.equal(sections.length, 1);
  assert.match(sections[0].textContent, /search for the population figure/);
  assert.doesNotMatch(sections[0].textContent, /Thinking…/);
  assert.equal(sections[0].querySelectorAll('.live-think-spinner-slot').length, 0);

  stream.event({ type: 'tool_output', tool: 'web_search', output: '8 million', exit_code: 0 });
  stream.event({ type: 'agent_step', round: 2 });
  stream.event({ delta: 'About 8 million.' });
  stream.done();
  await page.settled();
});
