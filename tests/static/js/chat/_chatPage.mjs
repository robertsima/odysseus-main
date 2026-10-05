// A chat page for node tests of static/js/chat.js: the composer, the toggles
// the send path reads, a chat history, the real chat and session modules, and
// a fetch fake that answers the history and session routes. Each test file
// gets one page (node runs every file in its own process).
//
//   const page = await openChatPage();
//   const stream = page.streamReply();          // answers the next POST /api/chat_stream
//   await page.send('hello');
//   stream.event({ delta: 'Hi' }); stream.done();
//   await page.settled();
import { after } from 'node:test';

import { installDom, waitFor } from '../_support/dom.mjs';
import { installFetchFake } from '../_support/fetchFake.mjs';

export { waitFor };

const PAGE = `
  <div id="sessions-section"><div id="session-list"></div></div>
  <div id="toast"></div>
  <div id="chat-container">
    <div id="welcome-screen" class="hidden"></div>
    <div id="chat-history"></div>
  </div>
  <div class="chat-input-bar">
    <form id="chat-form">
      <textarea id="message"></textarea>
      <button type="submit" class="send-btn"></button>
    </form>
    <input type="checkbox" id="web-toggle">
    <input type="checkbox" id="bash-toggle">
    <input type="checkbox" id="research-toggle">
    <input type="checkbox" id="rag-toggle" checked>
    <input type="checkbox" id="incognito-toggle">
  </div>`;

// One SSE response the test feeds event by event.
function controlledStream() {
  const encoder = new TextEncoder();
  let controller;
  const body = new ReadableStream({ start(c) { controller = c; } });
  return {
    response: new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } }),
    event(payload) { controller.enqueue(encoder.encode(`data: ${JSON.stringify(payload)}\n\n`)); },
    done() {
      controller.enqueue(encoder.encode('data: [DONE]\n\n'));
      controller.close();
    },
    fail(error = new TypeError('network error')) { controller.error(error); },
  };
}

export async function openChatPage({ sessionId = 's1', model = 'test-model', history = [] } = {}) {
  const dom = installDom();
  const fake = installFetchFake();
  after(async () => {
    fake.restore();
    await dom.restore();
  });

  // happy-dom has no canvas drawing; the thinking spinner draws on one. A
  // 2D context whose every method does nothing is enough for the page.
  HTMLCanvasElement.prototype.getContext = function getContext() {
    const state = {};
    return new Proxy(state, {
      get: (target, key) => (key in target ? target[key] : () => {}),
      set: (target, key, value) => { target[key] = value; return true; },
    });
  };

  document.body.innerHTML = PAGE;
  const replies = [];
  const posts = [];
  fake.route('GET', `/api/history/${sessionId}`, () => ({
    history, model, offset: 0, limit: 50, total: history.length, has_more_before: false,
  }));
  fake.route('POST', '/api/chat_stream', ({ body }) => {
    posts.push(body);
    const next = replies.shift();
    if (!next) throw new Error('POST /api/chat_stream without a reply queued by the test');
    return next.response;
  });

  const chatModule = (await import('../../../../static/js/chat.js')).default;
  const sessionModule = (await import('../../../../static/js/sessions.js')).default;
  chatModule.init('');
  document.getElementById('chat-form').addEventListener('submit', (e) => chatModule.handleChatSubmit(e));
  // sessionId null: a page with no chat open, as before a model is picked.
  if (sessionId) await sessionModule.selectSession(sessionId, { showLoading: false });

  const box = document.getElementById('chat-history');
  const input = document.getElementById('message');
  const sendButton = document.querySelector('.send-btn');

  return {
    fake,
    chatModule,
    sessionModule,
    box,
    input,
    sendButton,
    // Every FormData the page posted to /api/chat_stream, oldest first.
    posts,
    // Queue the response to the next chat_stream POST.
    streamReply() {
      const stream = controlledStream();
      replies.push(stream);
      return stream;
    },
    // Type a message and press send, as the user does.
    async send(text) {
      const before = posts.length;
      input.value = text;
      sendButton.click();
      await waitFor(() => posts.length > before, { what: 'the chat_stream POST' });
      return posts[posts.length - 1];
    },
    // Click a message's footer action by its title, opening the "···" menu
    // when the action is not one of the visible buttons.
    clickAction(bubble, title) {
      const visible = [...bubble.querySelectorAll('.msg-footer button')].find((b) => b.title === title);
      if (visible) return visible.click();
      bubble.querySelector('.msg-more-btn').click();
      const item = [...document.querySelectorAll('.msg-overflow-menu .msg-overflow-item')].find((b) => b.title === title);
      if (!item) throw new Error(`no "${title}" action on this message`);
      item.click();
    },
    // Wait until the send button is back to idle after a stream ends.
    async settled() {
      await waitFor(() => sendButton.dataset.mode !== 'streaming', { what: 'the stream to end' });
    },
  };
}
