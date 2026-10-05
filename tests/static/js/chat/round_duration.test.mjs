// The live round-time badge, in static/js/chat.js. The chat route sends a
// round_complete event with the wall time of each finished agent round; the
// streaming reply shows it at once, the same badge a reload draws from the
// saved metadata (tests/test_agent_tree_timing_browser.py).
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { openChatPage, waitFor } from './_chatPage.mjs';

const page = await openChatPage();

test('a finished round shows its wall time on the streaming reply', async () => {
  const stream = page.streamReply();
  await page.send('which river is longest');
  stream.event({ delta: 'The Nile.' });
  stream.event({ type: 'round_complete', round: 1, duration_s: 2.34 });

  const badges = () => [...page.box.querySelectorAll('.msg-ai .agent-round-duration')].map((b) => b.textContent);
  await waitFor(() => badges().length > 0, { what: 'the round badge' });
  assert.deepEqual(badges(), ['Round · 2.3s']);

  stream.done();
  await page.settled();
});
