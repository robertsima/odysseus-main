// Tool steps in a streaming agent reply, in static/js/chat.js. The tool name
// and a browser screenshot arrive in the stream from the agent loop, which can
// pass along whatever a model or a web page made up, so the timeline has to
// show them as data: the name as text, the screenshot only when it is a plain
// raster image.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { openChatPage, waitFor } from './_chatPage.mjs';

const page = await openChatPage();
const PNG = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==';

function lastThread() {
  const threads = page.box.querySelectorAll('.agent-thread');
  return threads[threads.length - 1];
}

function handlerAttributes(root) {
  return [...root.querySelectorAll('*')].flatMap((el) =>
    [...el.attributes].filter((a) => a.name.startsWith('on')).map((a) => `${el.tagName}[${a.name}]`));
}

async function runTool(events) {
  const stream = page.streamReply();
  await page.send('open the status page');
  for (const event of events) stream.event(event);
  stream.event({ delta: 'Done.' });
  stream.done();
  await page.settled();
  return lastThread();
}

test('a tool name with markup is shown as text while the tool runs and after', async () => {
  const name = '<img src=x onerror=alert(1)>';
  const stream = page.streamReply();
  await page.send('open the status page');
  stream.event({ type: 'tool_start', tool: name, command: 'go' });
  await waitFor(() => page.box.querySelector('.agent-thread-node.running'), { what: 'the running tool step' });
  const running = page.box.querySelector('.agent-thread-node.running .agent-thread-tool');
  assert.equal(running.textContent, name);
  assert.equal(running.querySelectorAll('img').length, 0);

  stream.event({ type: 'tool_output', tool: name, output: 'ok', exit_code: 0 });
  stream.event({ delta: 'Done.' });
  stream.done();
  await page.settled();
  const thread = lastThread();
  assert.equal(thread.querySelector('.agent-thread-tool').textContent, name);
  assert.deepEqual(handlerAttributes(thread), []);
});

test('a screenshot that is not a raster data URL is not shown', async () => {
  const thread = await runTool([
    { type: 'tool_start', tool: 'browser_take_screenshot', command: '' },
    { type: 'tool_output', tool: 'browser_take_screenshot', output: 'ok', exit_code: 0,
      screenshot: 'x" onerror="alert(1)' },
    { type: 'tool_start', tool: 'browser_take_screenshot', command: '' },
    { type: 'tool_output', tool: 'browser_take_screenshot', output: 'ok', exit_code: 0,
      screenshot: 'data:image/svg+xml;base64,PHN2ZyBvbmxvYWQ9ImFsZXJ0KDEpIi8+' },
  ]);

  assert.deepEqual(handlerAttributes(thread), []);
  assert.deepEqual([...thread.querySelectorAll('img')].map((img) => img.getAttribute('src')), []);
});

test('a PNG screenshot is shown in the tool step', async () => {
  const thread = await runTool([
    { type: 'tool_start', tool: 'browser_take_screenshot', command: '' },
    { type: 'tool_output', tool: 'browser_take_screenshot', output: 'ok', exit_code: 0, screenshot: PNG },
  ]);

  assert.deepEqual([...thread.querySelectorAll('img')].map((img) => img.getAttribute('src')), [PNG]);
});
