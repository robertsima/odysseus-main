// The session cost ledger in a streamed reply (static/js/chat.js). A turn that
// escalates to a teacher model reports two metrics frames in one detached run:
// the primary's and the teacher's. The ledger keys each cost by run id and
// segment, so both are billed. Keyed by the run id alone, the teacher's frame
// overwrites the primary's and the session total drops the primary's cost.
import assert from 'node:assert/strict';
import { after, test } from 'node:test';

import { DONE, openChatPage } from './_page.mjs';

const page = await openChatPage();
window.sessionModule = page.sessionModule;
const chatRenderer = (await import('../../../../static/js/chatRenderer.js')).default;
after(() => page.close());

const usage = (model) => ({
  model, input_tokens: 1000, output_tokens: 500, endpoint_cost_tracked: true,
});

test('a primary and a teacher segment of one run are both billed', async () => {
  const each = chatRenderer.getModelCost('gpt-4o', 1000, 500);
  assert.ok(each > 0, 'the price table knows gpt-4o');
  page.reply([
    { delta: 'answer' },
    { type: 'metrics', data: usage('gpt-4o') },
    { type: 'metrics', teacher: true, data: usage('gpt-4o') },
    DONE,
  ]);

  await page.send('hard question');
  await page.waitFor(() => chatRenderer.getSessionCost('s1') >= 2 * each - 1e-9, { what: 'the cost to be recorded' });

  assert.ok(Math.abs(chatRenderer.getSessionCost('s1') - 2 * each) < 1e-9);
});
