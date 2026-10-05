// The chat page for chat.js tests: the real static/index.html markup (its
// scripts, styles and frames removed), the shared fetch fake answering the
// session and history requests, and the chat form wired to
// chat.handleChatSubmit the way app.js wires it.
//
//   const page = await openChatPage();
//   page.streamReply([{ delta: 'hi' }, DONE]);
//   await page.send('hello');
//   after(() => page.close());
import { readFileSync } from 'node:fs';

import { installDom, waitFor } from '../_support/dom.mjs';
import { installFetchFake, jsonResponse } from '../_support/fetchFake.mjs';

export const DONE = 'data: [DONE]\n\n';
const STATIC_DIR = new URL('../../../../static/', import.meta.url);

function frame(event) {
  return typeof event === 'string' ? event : `data: ${JSON.stringify(event)}\n\n`;
}

// A text/event-stream response. Events are objects (sent as `data:` lines)
// or raw strings. `drop: true` ends the body with a network error instead of
// closing it; `hold: true` keeps it open until `release()`.
export function sseResponse(events, { runId = 'run-1', drop = false, hold = false } = {}) {
  const encoder = new TextEncoder();
  let release;
  const body = new ReadableStream({
    async start(controller) {
      for (const event of events) controller.enqueue(encoder.encode(frame(event)));
      if (hold) await new Promise((resolve) => { release = resolve; });
      if (drop) controller.error(new TypeError('network error'));
      else controller.close();
    },
  });
  const response = new Response(body, {
    status: 200,
    headers: { 'Content-Type': 'text/event-stream', 'X-Odysseus-Run-Id': runId },
  });
  response.release = () => release && release();
  return response;
}

export async function openChatPage({ model = 'model-a', endpointUrl = 'http://local.test/v1', mode = 'chat' } = {}) {
  const dom = installDom();
  const fake = installFetchFake();
  // app.js reports activity with sendBeacon; keep it off happy-dom's network.
  navigator.sendBeacon = () => true;
  // happy-dom has no 2D canvas; the thinking spinner draws on one. Every
  // drawing call is a no-op here.
  HTMLCanvasElement.prototype.getContext = () => new Proxy({}, { get: () => () => ({}) });
  localStorage.setItem('odysseus-toggles', JSON.stringify({ mode }));

  const history = { s1: [], s2: [] };
  fake.route('GET', '/api/sessions', () => [
    { id: 's1', name: 'first chat', model, endpoint_url: endpointUrl },
    { id: 's2', name: 'second chat', model, endpoint_url: endpointUrl },
  ]);
  fake.route('GET', /^\/api\/history\/(s1|s2)$/, ({ url }) => {
    const id = url.pathname.split('/').pop();
    const messages = history[id];
    return { history: messages, model, offset: 0, limit: 50, total: messages.length, has_more_before: false };
  });

  const replies = [];
  const posts = [];
  fake.route('POST', '/api/chat_stream', ({ body }) => {
    posts.push(Object.fromEntries(body.entries()));
    const next = replies.shift();
    if (!next) throw new Error('chat_stream posted with no reply queued');
    return typeof next === 'function' ? next() : next;
  });

  const markup = new DOMParser().parseFromString(readFileSync(new URL('index.html', STATIC_DIR), 'utf8'), 'text/html');
  markup.querySelectorAll('script, link, style, iframe').forEach((node) => node.remove());
  document.body.innerHTML = markup.body.innerHTML;

  const sessionModule = (await import('../../../../static/js/sessions.js')).default;
  const chat = (await import('../../../../static/js/chat.js')).default;
  chat.init('');
  chat.initListeners();
  document.getElementById('chat-form').onsubmit = (event) => chat.handleChatSubmit(event);
  await sessionModule.loadSessions();
  await sessionModule.selectSession('s1', { showLoading: false });

  const page = {
    dom,
    fake,
    chat,
    sessionModule,
    history,
    posts,
    // Queue the response for the next POST /api/chat_stream: a Response, a
    // function returning one, or a list of events for an SSE reply.
    reply(response) {
      replies.push(Array.isArray(response) ? sseResponse(response) : response);
    },
    composer: () => document.getElementById('message'),
    chatBox: () => document.getElementById('chat-history'),
    aiMessages: () => [...document.querySelectorAll('#chat-history .msg-ai')],
    async send(text) {
      page.composer().value = text;
      await chat.handleChatSubmit(new Event('submit', { cancelable: true }));
    },
    waitFor,
    async close() {
      fake.restore();
      await dom.restore();
    },
  };
  return page;
}

export { jsonResponse, waitFor };
