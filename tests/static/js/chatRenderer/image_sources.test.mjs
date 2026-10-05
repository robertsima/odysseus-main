// Image sources that come from saved replies, in static/js/chatRenderer.js.
// A tool screenshot and a generated image's URL are stored with the message,
// so a reload renders whatever the agent loop once recorded. Only a raster
// data URL may become a screenshot, and only an http(s) or data-raster URL a
// generated image, which the page also opens on click.
import assert from 'node:assert/strict';
import { after, beforeEach, test } from 'node:test';

import { installDom } from '../_support/dom.mjs';
import { installFetchFake } from '../_support/fetchFake.mjs';

const dom = installDom();
const fake = installFetchFake();
after(async () => {
  fake.restore();
  await dom.restore();
});

const { addMessage, buildImageBubble } = await import('../../../../static/js/chatRenderer.js');
const PNG = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==';

beforeEach(() => {
  document.body.innerHTML = '<div id="chat-history"></div>';
});

function handlerAttributes(root) {
  return [...root.querySelectorAll('*')].flatMap((el) =>
    [...el.attributes].filter((a) => a.name.startsWith('on')).map((a) => `${el.tagName}[${a.name}]`));
}

test('a reloaded reply shows only raster screenshots from its tool steps', () => {
  const step = (screenshot) => ({ round: 1, tool: 'browser_take_screenshot', output: 'ok', exit_code: 0, screenshot });
  addMessage('assistant', 'Done.', 'test-model', {
    round_texts: ['Done.'],
    tool_events: [
      step('x" onerror="alert(1)'),
      step('data:image/svg+xml;base64,PHN2ZyBvbmxvYWQ9ImFsZXJ0KDEpIi8+'),
      step('javascript:alert(1)'),
      step(PNG),
    ],
  });

  const box = document.getElementById('chat-history');
  assert.deepEqual([...box.querySelectorAll('.agent-thread img')].map((img) => img.getAttribute('src')), [PNG]);
  assert.deepEqual(handlerAttributes(box), []);
});

test('a generated image with a script URL is not shown or opened', () => {
  const opened = [];
  window.open = (...args) => { opened.push(args); };

  const bubble = buildImageBubble('javascript:alert(1)', 'a cat', 'image-model');

  assert.equal(bubble.querySelectorAll('img').length, 0);
  assert.equal(bubble.querySelector('.body').textContent, '[Image unavailable]');
  assert.deepEqual(opened, []);
});

test('a generated image at a relative URL is shown and opens at its absolute URL', () => {
  const opened = [];
  window.open = (...args) => { opened.push(args); };

  const bubble = buildImageBubble('/api/gallery/cat.png', 'a cat', 'image-model');
  const img = bubble.querySelector('img');
  img.click();

  assert.equal(img.getAttribute('src'), 'http://localhost/api/gallery/cat.png');
  assert.deepEqual(opened, [['http://localhost/api/gallery/cat.png', '_blank', 'noopener,noreferrer']]);
});
