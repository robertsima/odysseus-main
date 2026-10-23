// What a send says about the open document (static/js/chat.js). 2026-10-23:
// opening a document made every message about it: the page saved it, sent its
// id and switched the chat to Agent mode, whatever the person wanted to talk about.
import assert from 'node:assert/strict';
import { after, test } from 'node:test';

import { DONE, openChatPage } from './_page.mjs';

const page = await openChatPage({ mode: 'chat' });
after(() => page.close());

const documentModule = (await import('../../../../static/js/document.js?v=20260928docedittarget1')).default;
const chatSettings = (await import('../../../../static/js/chatSettings.js')).default;

async function openDocument(title) {
  page.reply([{ type: 'doc_stream_open', title, language: 'markdown' }, { type: 'doc_stream_delta', content: 'x' }, DONE]);
  await page.send('write something');
  assert.ok(documentModule.getCurrentDocId(), 'a document is open');
}

test('a document opened in this chat is the target, and the send says so', async () => {
  await openDocument('DA-02');
  const docId = documentModule.getCurrentDocId();
  page.reply([{ delta: 'ok' }, DONE]);

  await page.send('tighten section 2');
  const form = page.posts[page.posts.length - 1];

  assert.equal(form.active_doc_id, docId);
  assert.equal(form.doc_target, 'document');
});

test('a general conversation sends no document id and does not switch to Agent mode', async () => {
  await openDocument('DA-03');
  chatSettings.setDocTarget('general');
  page.reply([{ delta: 'ok' }, DONE]);

  await page.send('what is the capital of France');
  const form = page.posts[page.posts.length - 1];

  assert.equal(form.active_doc_id, undefined);
  assert.equal(form.doc_target, 'general');
  assert.equal(form.mode, 'chat');
});




