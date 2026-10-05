// The live turn-time badge in static/js/chat.js. Each round_complete duration
// contributes to one cumulative badge, matching the saved-history renderer.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { openChatPage, waitFor } from './_chatPage.mjs';

const page = await openChatPage();

test('completed rounds update one cumulative turn badge on the streaming reply', async () => {
  const stream = page.streamReply();
  await page.send('which river is longest');
  stream.event({ delta: 'The Nile.' });
  stream.event({ type: 'round_complete', round: 1, duration_s: 2.34 });

  const badges = () => [...page.box.querySelectorAll('.msg-ai .agent-turn-duration')].map((b) => b.textContent);
  await waitFor(() => badges().length > 0, { what: 'the turn badge after round one' });
  assert.deepEqual(badges(), ['Turn · 2.3s']);

  stream.event({ type: 'round_complete', round: 2, duration_s: 5 });
  await waitFor(() => badges()[0] === 'Turn · 7.3s', { what: 'the cumulative turn badge after round two' });
  assert.deepEqual(badges(), ['Turn · 7.3s']);

  stream.done();
  await page.settled();
});
