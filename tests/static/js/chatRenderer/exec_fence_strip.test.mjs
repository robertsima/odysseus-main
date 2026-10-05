// Live exec-fence stripping in static/js/chatRenderer.js (#3993). The tool
// tags come from GET /api/tools, so a tool the frontend has never heard of
// still has its executed fence removed from the streaming bubble. A
// hand-maintained list drifts: a new tool's fence then stays visible until
// the page reloads.
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { installDom, waitFor } from '../_support/dom.mjs';
import { installFetchFake } from '../_support/fetchFake.mjs';

const dom = installDom();
const fake = installFetchFake();
after(async () => {
  fake.restore();
  await dom.restore();
});

fake.route('GET', '/api/tools', () => ({
  tools: [
    { id: 'web_search', enabled: true },
    { id: 'tool_added_after_this_test', enabled: false },
    { id: 'bash', enabled: true },
    { id: 'python', enabled: true },
  ],
}));

const { stripToolBlocks } = await import('../../../../static/js/chatRenderer.js');

before(async () => {
  await waitFor(() => stripToolBlocks('```web_search\n{"q": "x"}\n```') === '', {
    what: 'the tool list from /api/tools',
  });
});

test('a fence for a tool the server lists is stripped from the live text', () => {
  const text = 'Looking it up.\n```tool_added_after_this_test\n{"id": 1}\n```\nDone.';

  assert.equal(stripToolBlocks(text), 'Looking it up.\n\nDone.');
});

test('a fence whose arguments sit on the tag line as JSON is stripped', () => {
  assert.equal(stripToolBlocks('Accounts:\n```web_search {}\n```'), 'Accounts:');
  assert.equal(
    stripToolBlocks('Emails:\n```web_search {"folder": "INBOX",\n"max_results": 2}\n```'),
    'Emails:',
  );
});

test('bash and python fences stay: they are code the user may have asked for', () => {
  for (const lang of ['bash', 'python']) {
    const example = '```' + lang + '\nls -la\n```';
    assert.equal(stripToolBlocks(example), example);
  }
});

test('a same-line tag that is not JSON stays visible as Markdown metadata', () => {
  const example = '```web_search {query="odysseus"}\n```';

  assert.equal(stripToolBlocks(example), example);
});
