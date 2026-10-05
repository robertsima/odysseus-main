// The Notes panel's "Vault files" view: a folder tree of the personal vault's
// Markdown files and an editor for the open one. These tests drive it the way
// a person does, against a fake /api/personal/vault.
import assert from 'node:assert/strict';
import { after, beforeEach, test } from 'node:test';

import { installDom, waitFor } from '../_support/dom.mjs';
import { installFetchFake, jsonResponse } from '../_support/fetchFake.mjs';

const dom = installDom();
const fake = installFetchFake();
after(async () => {
  fake.restore();
  await dom.restore();
});

const file = (path, extra = {}) => ({ type: 'file', name: path.split('/').pop(), path, ...extra });
const TREE = {
  type: 'directory', name: 'Vault', path: '',
  children: [
    {
      type: 'directory', name: 'journal', path: 'journal',
      children: [file('journal/monday.md'), file('journal/secret.md', { sensitivity: 'private', readonly: true })],
    },
    { type: 'directory', name: 'work', path: 'work', children: [file('work/plan.md')] },
    file('inbox.md'),
  ],
};
const FILES = {
  'journal/monday.md': { path: 'journal/monday.md', name: 'monday.md', content: 'saved text', modified: 1700000000.5 },
  'journal/secret.md': {
    path: 'journal/secret.md', name: 'secret.md', content: 'private text', modified: 1, sensitivity: 'private', readonly: true,
  },
};

// What the next PUT answers; a test that wants a failed save sets it.
let putAnswer = null;
fake.route('GET', '/api/notes', () => ({ notes: [] }));
fake.route('GET', '/api/personal/vault/tree', () => ({ tree: TREE }));
fake.route('GET', '/api/personal/vault/file', ({ url }) => FILES[url.searchParams.get('path')]);
fake.route('PUT', '/api/personal/vault/file', ({ body }) => putAnswer || { ...JSON.parse(body), modified: 2 });
fake.route('POST', '/api/personal/vault/file', ({ body }) => {
  const { path, content } = JSON.parse(body);
  return { path, name: path.split('/').pop(), content, modified: 3 };
});
fake.route('DELETE', '/api/personal/vault/file', () => ({ ok: true }));

document.body.innerHTML = '<div id="chat-container"></div><div id="toast"></div>';
const notes = await import('../../../../static/js/notes.js');

const requests = (method) => fake.calls.filter((c) => c.method === method && c.url.pathname === '/api/personal/vault/file');
const folder = (path) => document.querySelector(`[data-vault-folder="${path}"]`);
const editor = () => document.getElementById('vault-file-editor');

async function openVault() {
  if (notes.isPanelOpen()) {
    notes.closePanel();
    await waitFor(() => !document.getElementById('notes-pane'), { what: 'the panel to close' });
  }
  notes.openPanel();
  await waitFor(() => document.querySelector('[data-notes-mode="vault"]'), { what: 'the panel' });
  document.querySelector('[data-notes-mode="vault"]').click();
  await waitFor(() => document.querySelector('.vault-tree [data-vault-file]'), { what: 'the vault tree' });
}

async function openFile(path) {
  document.querySelector(`[data-vault-file="${path}"]`).click();
  await waitFor(() => editor()?.value === FILES[path].content, { what: `${path} in the editor` });
}

function type(text) {
  editor().value = text;
  editor().dispatchEvent(new Event('input'));
}

beforeEach(() => {
  putAnswer = null;
  fake.calls.length = 0;
});

test('the tree starts with the top-level folders collapsed, without a wrapping root folder', async () => {
  await openVault();
  const folders = [...document.querySelectorAll('.vault-tree > [data-vault-folder]')].map((f) => f.dataset.vaultFolder);
  assert.deepEqual(folders, ['journal', 'work']);
  assert.equal(folder('journal').open, false);
  assert.equal(folder('work').open, false);
});

test('a folder the user opened stays open when the view re-renders', async () => {
  await openVault();
  folder('journal').open = true;
  folder('journal').dispatchEvent(new Event('toggle'));
  await openFile('journal/monday.md');
  assert.equal(folder('journal').open, true);
  assert.equal(folder('work').open, false);
});

test('a search lists only the matching files, flat, and leaves the folders collapsed afterwards', async () => {
  await openVault();
  const search = document.getElementById('notes-search');
  search.value = 'plan';
  search.dispatchEvent(new Event('input'));
  const shown = [...document.querySelectorAll('.vault-tree [data-vault-file]')].map((b) => b.dataset.vaultFile);
  assert.deepEqual(shown, ['work/plan.md']);
  assert.equal(document.querySelectorAll('.vault-tree [data-vault-folder]').length, 0);

  search.value = '';
  search.dispatchEvent(new Event('input'));
  assert.equal(folder('work').open, false);
});

test('a read-only private file shows both LLM policies and stays editable by the person', async () => {
  await openVault();
  await openFile('journal/secret.md');
  const badges = [...document.querySelectorAll('.vault-policy-badge')].map((b) => b.textContent);
  assert.deepEqual(badges, ['private', 'readonly']);
  assert.equal(editor().readOnly, false);
  assert.equal(editor().disabled, false);
});

test('saving sends the modified time the file was opened with', async () => {
  await openVault();
  await openFile('journal/monday.md');
  type('new text');
  document.getElementById('vault-file-save').click();
  const save = () => document.getElementById('vault-file-save');
  await waitFor(() => save().disabled && save().textContent === 'Save', { what: 'the save to finish' });
  assert.equal(requests('PUT').length, 1);
  assert.deepEqual(JSON.parse(requests('PUT')[0].body), {
    path: 'journal/monday.md', content: 'new text', modified: 1700000000.5,
  });
});

test('a new note is created in the chosen folder', async () => {
  await openVault();
  document.getElementById('vault-new-note').click();
  document.getElementById('vault-new-folder').value = 'work';
  document.getElementById('vault-new-name').value = 'ideas';
  document.getElementById('vault-new-form').dispatchEvent(new Event('submit', { cancelable: true }));
  await waitFor(() => editor()?.value === '# ideas\n\n' || document.getElementById('toast').classList.contains('error'), { what: 'the new note in the editor' });
  console.log('TOAST', document.getElementById('toast').textContent);
  assert.equal(requests('POST').length, 1);
  assert.equal(JSON.parse(requests('POST')[0].body).path, 'work/ideas.md');
});

test('deleting the open note asks first, then deletes that path', async () => {
  await openVault();
  await openFile('journal/monday.md');
  document.getElementById('vault-file-delete').click();
  await waitFor(() => document.getElementById('styled-confirm-ok'), { what: 'the confirm dialog' });
  assert.equal(requests('DELETE').length, 0, 'deleted before the person confirmed');
  document.getElementById('styled-confirm-ok').click();
  await waitFor(() => !editor(), { what: 'the deleted note to close' });
  assert.equal(requests('DELETE').length, 1);
  assert.equal(requests('DELETE')[0].url.searchParams.get('path'), 'journal/monday.md');
});

// Last: it leaves an unsaved draft behind.
test('a save the server refuses keeps the draft in the editor', async () => {
  await openVault();
  await openFile('journal/monday.md');
  putAnswer = jsonResponse({ detail: 'The file changed on disk since you opened it' }, { status: 409 });
  type('my unsaved draft');
  document.getElementById('vault-file-save').click();
  await waitFor(() => document.getElementById('vault-file-save').textContent === 'Save', { what: 'the save to settle' });
  assert.equal(editor().value, 'my unsaved draft');
  assert.equal(document.getElementById('vault-file-save').disabled, false);
});
