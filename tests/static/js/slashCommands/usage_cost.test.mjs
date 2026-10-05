// /usage (static/js/slashCommands.js) reports what the session has cost. A
// chat on a local endpoint can still have billed usage, from a paid fallback
// route that answered some of its turns, and /usage must report that cost
// instead of claiming nothing billable was recorded.
import assert from 'node:assert/strict';
import { after, test } from 'node:test';

import { installDom } from '../_support/dom.mjs';
import { installFetchFake } from '../_support/fetchFake.mjs';

const dom = installDom();
const fake = installFetchFake();
after(async () => { fake.restore(); await dom.restore(); });

document.body.innerHTML = '<div id="chat-history"></div><div id="sessions-section"></div>';
fake.route('GET', '/api/sessions', () => [
  { id: 's1', name: 'Local chat', model: 'qwen3', endpoint_url: 'http://localhost:11434/v1', message_count: 4 },
]);
fake.route('POST', '/api/session/s1/message', () => ({ ok: true }));

const sessions = await import('../../../../static/js/sessions.js');
window.sessionModule = sessions.default;
const { handleSlashCommand } = await import('../../../../static/js/slashCommands.js');
await sessions.loadSessions();
sessions.setCurrentSessionId('s1');

test('/usage on a local endpoint still reports the cost of a paid fallback', async () => {
  localStorage.setItem('ody-session-cost', JSON.stringify({ s1: 0.125 }));
  await handleSlashCommand('/usage');
  const reply = [...document.querySelectorAll('#chat-history .msg-ai')].at(-1);
  assert.match(reply.querySelector('pre').textContent, /Estimated local cost: \$0\.125/);
});
