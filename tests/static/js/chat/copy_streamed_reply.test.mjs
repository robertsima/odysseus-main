// Copying a reply that just streamed in, through the footer of
// static/js/chatRenderer.js. The streamed bubble keeps the raw model output,
// <think> reasoning and text-form tool calls included, and Copy has to give
// the reply as the page shows it (#3722).
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { openChatPage, waitFor } from './_chatPage.mjs';

const page = await openChatPage();
const copied = [];
Object.defineProperty(navigator, 'clipboard', {
  configurable: true,
  value: { writeText: async (text) => { copied.push(text); } },
});

test('Copy on a streamed reply leaves out its reasoning and tool-call blocks', async () => {
  const stream = page.streamReply();
  await page.send('summarize the lesson');
  stream.event({ delta: '<think>Let me plan the summary first.</think>### Summary\n' });
  stream.event({ delta: '[TOOL_CALL]{"name": "web_search", "arguments": {"q": "x"}}[/TOOL_CALL]\n' });
  stream.event({ delta: 'The lesson juxtaposes two norms.' });
  stream.done();
  await page.settled();

  const replies = page.box.querySelectorAll('.msg-ai');
  replies[replies.length - 1].querySelector('.footer-copy-btn').click();
  await waitFor(() => copied.length === 1, { what: 'the clipboard write' });

  assert.doesNotMatch(copied[0], /plan the summary|TOOL_CALL|web_search/);
  assert.match(copied[0], /^### Summary\s+The lesson juxtaposes two norms\.$/);
});
