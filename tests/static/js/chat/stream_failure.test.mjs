// A reply stream that breaks partway, in static/js/chat.js. The error path
// has to say what went wrong and hand the composer back. It once threw a
// ReferenceError instead, because helpers it calls were declared inside the
// try block it is the catch of, so the user saw a half reply with no error.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { openChatPage, waitFor } from './_chatPage.mjs';

const page = await openChatPage();

test('a stream that fails partway shows the error in the reply', async () => {
  const stream = page.streamReply();
  await page.send('summarize the report');
  stream.event({ delta: 'The report covers ' });
  await waitFor(() => page.box.textContent.includes('The report covers'), { what: 'the first delta' });

  stream.fail(new Error('upstream returned 502'));
  await page.settled();

  const replies = page.box.querySelectorAll('.msg-ai .body');
  const reply = replies[replies.length - 1];
  await waitFor(() => reply.textContent.includes('Error: upstream returned 502'), { what: 'the error to be shown' });
  assert.equal(page.input.disabled, false);
});
