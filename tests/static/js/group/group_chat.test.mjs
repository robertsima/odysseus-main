// Group chat, in static/js/group.js: several models answer the same message,
// each in its own bubble. A bubble's label is the model's display name, which
// comes from an endpoint's model list, and a participant's stream can carry a
// generated image URL from the model. The label is shown as text and an image
// only from an http(s) or raster data URL. Starting a group also registers its
// chat in the sidebar's stored list of group chats, which must survive a
// corrupted entry.
import assert from 'node:assert/strict';
import { after, test } from 'node:test';

import { installDom } from '../_support/dom.mjs';
import { installFetchFake } from '../_support/fetchFake.mjs';

const dom = installDom();
const fake = installFetchFake();
after(async () => {
  fake.restore();
  await dom.restore();
});

let nextId = 0;
fake.route('POST', '/api/session', () => ({ id: `g${++nextId}` }));
fake.route('POST', /^\/api\/session\/[^/]+\/inject_messages$/, () => ({ ok: true }));

const IMAGES = { g2: 'javascript:alert(1)', g3: 'https://img.example.com/cat.png' };
const encoder = new TextEncoder();
fake.route('POST', '/api/chat_stream', ({ body }) => {
  // An image-only reply: text after the image would replace the body when
  // the reply ends.
  const image = { type: 'generated_image', url: IMAGES[body.get('session')] };
  const frames = `data: ${JSON.stringify(image)}\n\ndata: [DONE]\n\n`;
  return new Response(new ReadableStream({
    start(controller) { controller.enqueue(encoder.encode(frames)); controller.close(); },
  }), { headers: { 'Content-Type': 'text/event-stream' } });
});

document.body.innerHTML = '<div id="chat-history"></div>';
localStorage.setItem('odysseus-group-sessions', '{"not": "a list"');
const group = (await import('../../../../static/js/group.js')).default;

const MODELS = [
  { mid: 'evil-model', display: 'evil<img src=x onerror=alert(1)>', url: 'http://127.0.0.1:9/v1' },
  { mid: 'good-model', display: 'good model', url: 'http://127.0.0.1:9/v1' },
];

test('starting a group registers its chat even when the stored list is corrupt', async () => {
  await group.startGroup(MODELS);

  assert.deepEqual(JSON.parse(localStorage.getItem('odysseus-group-sessions')), ['g1']);
});

test('each bubble shows its model name as text and only a safe generated image', async () => {
  group.setMode('parallel');
  await group.sendMessage('draw a cat');

  const [evil, good] = document.querySelectorAll('#chat-history .msg-group');
  assert.equal(evil.querySelectorAll('.role img').length, 0);
  assert.match(evil.querySelector('.role').textContent, /^evil<img src=x onerror=alert\(1\)>/);
  assert.deepEqual([...evil.querySelectorAll('img')].map((i) => i.getAttribute('src')), []);
  assert.deepEqual([...good.querySelectorAll('img')].map((i) => i.getAttribute('src')), [IMAGES.g3]);
});
