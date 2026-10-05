// Uploading the composer's attachments (static/js/fileHandler.js). A refused
// upload (429 rate limit, 413 too large) once read as success: the files
// silently vanished and the message went out with no attachments, so the model
// never saw them (#1346). A refused upload now tells the user why and keeps
// the files attached for a retry.
import assert from 'node:assert/strict';
import { after, test } from 'node:test';

import { installDom } from '../_support/dom.mjs';
import { installFetchFake, jsonResponse } from '../_support/fetchFake.mjs';

const dom = installDom();
const fake = installFetchFake();
after(async () => {
  fake.restore();
  await dom.restore();
});

document.body.innerHTML = '<div id="toast"></div><div id="attach-strip"></div>';
const files = await import('../../../../static/js/fileHandler.js');
files.init('');

const toasts = [];
window.showToast = (message) => toasts.push(message);

let reply;
fake.route('POST', '/api/upload', () => reply());

test('a refused upload says why and keeps the file attached', async () => {
  reply = () => jsonResponse({ detail: 'Too many uploads, slow down' }, { status: 429 });
  await files.addFiles([new File(['notes'], 'notes.txt', { type: 'text/plain' })]);
  assert.equal(files.getPendingCount(), 1);

  const uploaded = await files.uploadPending();

  assert.deepEqual(uploaded, []);
  assert.deepEqual(toasts, ['Upload failed: Too many uploads, slow down']);
  assert.equal(files.getPendingCount(), 1, 'the file stays attached for a retry');
});

test('an accepted upload returns the stored file ids and clears the attachments', async () => {
  reply = () => jsonResponse({ files: [{ id: 'f1', filename: 'notes.txt' }] });

  const uploaded = await files.uploadPending();

  assert.deepEqual(uploaded, ['f1']);
  assert.equal(files.getPendingCount(), 0);
});
