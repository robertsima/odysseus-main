// A compare pane's stream, in static/js/compare/stream.js. Each pane runs a
// model through POST /api/chat_stream like the main chat, and draws what the
// agent loop sends: tool names, generated images, search results. Those come
// from the model or a web page, so they are shown as data: names as text, an
// image only from an http(s) or raster data URL, a result link only to http(s).
import assert from 'node:assert/strict';
import { after, test } from 'node:test';

import { installDom, waitFor } from '../../_support/dom.mjs';
import { installFetchFake } from '../../_support/fetchFake.mjs';

const dom = installDom();
const fake = installFetchFake();
after(async () => {
  fake.restore();
  await dom.restore();
});

const { default: state } = await import('../../../../../static/js/compare/state.js');
const { streamToPane, _renderSearchResults } = await import('../../../../../static/js/compare/stream.js');

const encoder = new TextEncoder();
let pane;
fake.route('POST', '/api/chat_stream', () => new Response(new ReadableStream({
  start(controller) { pane = controller; },
}), { headers: { 'Content-Type': 'text/event-stream' } }));

const send = (payload) => pane.enqueue(encoder.encode(`data: ${JSON.stringify(payload)}\n\n`));

function openPane() {
  document.body.innerHTML =
    '<div class="compare-pane" data-pane="0"><div class="pane-history"><div class="msg msg-ai"><div class="body"></div></div></div></div>';
  state._compareMode = 'agent';
  const reply = document.querySelector('.msg-ai');
  pane = null;
  const done = streamToPane(0, 'cmp-1', 'compare this', reply, { timeout: 30 });
  return { reply, done };
}

async function finish(done) {
  send({ delta: 'ok' });
  pane.enqueue(encoder.encode('data: [DONE]\n\n'));
  pane.close();
  await done;
}

function handlerAttributes(root) {
  return [...root.querySelectorAll('*')].flatMap((el) =>
    [...el.attributes].filter((a) => a.name.startsWith('on')).map((a) => `${el.tagName}[${a.name}]`));
}

test('a tool name with markup is shown as text while the tool runs and after', async () => {
  const name = '<img src=x onerror=alert(1)>';
  const { reply, done } = openPane();
  await waitFor(() => pane, { what: 'the pane stream' });

  send({ type: 'tool_start', tool: name, command: 'go' });
  await waitFor(() => reply.querySelector('.agent-thread-node.running'), { what: 'the running tool step' });
  assert.equal(reply.querySelector('.agent-thread-tool').textContent, name);
  assert.equal(reply.querySelectorAll('.agent-thread-tool img').length, 0);

  send({ type: 'tool_output', tool: name, output: 'done', exit_code: 0 });
  await waitFor(() => !reply.querySelector('.agent-thread-node.running'), { what: 'the finished tool step' });
  assert.equal(reply.querySelector('.agent-thread-tool').textContent, name);
  assert.deepEqual(handlerAttributes(reply), []);
  await finish(done);
});

test('a generated image with a script URL is not shown', async () => {
  const { reply, done } = openPane();
  await waitFor(() => pane, { what: 'the pane stream' });

  send({ type: 'tool_output', tool: 'generate_image', image_url: 'javascript:alert(1)', image_prompt: 'a cat' });
  await waitFor(() => reply.textContent.includes('[Image unavailable]'), { what: 'the image result' });

  assert.equal(reply.querySelectorAll('img').length, 0);
  pane.close();
  await done;
});

test('a generated image at an https URL is shown', async () => {
  const { reply, done } = openPane();
  await waitFor(() => pane, { what: 'the pane stream' });

  send({ type: 'tool_output', tool: 'generate_image', image_url: 'https://img.example.com/cat.png', image_prompt: 'a cat' });
  await waitFor(() => reply.querySelector('img'), { what: 'the image' });

  assert.equal(reply.querySelector('img').getAttribute('src'), 'https://img.example.com/cat.png');
  pane.close();
  await done;
});

test('search results link only to http(s) pages', () => {
  const results = _renderSearchResults({ results: [
    { url: 'javascript:alert(1)', title: 'trap' },
    { url: 'https://example.com/page', title: 'page' },
  ] });

  const links = [...results.querySelectorAll('a.search-result-title')];
  assert.deepEqual(links.map((a) => a.getAttribute('href')), [null, 'https://example.com/page']);
});
