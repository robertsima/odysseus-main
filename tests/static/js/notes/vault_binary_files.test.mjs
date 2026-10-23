// A PDF, Word or image file in the vault tree opens a preview or a download.
// It never loads into the Markdown editor, and nothing is written to the file.
import assert from 'node:assert/strict';
import { after, test } from 'node:test';

import { installDom, waitFor } from '../_support/dom.mjs';
import { installFetchFake } from '../_support/fetchFake.mjs';

const dom = installDom();
const fake = installFetchFake();
after(async () => {
  fake.restore();
  await dom.restore();
});

const file = (path, kind) => ({ type: 'file', name: path.split('/').pop(), path, kind });
const TREE = {
  type: 'directory', name: 'Vault', path: '',
  children: [
    file('Agreements/msa.pdf', 'pdf'),
    file('Agreements/msa.docx', 'docx'),
    file('logo.png', 'image'),
    file('inbox.md', 'markdown'),
  ].map((f) => f),
};
// Put the first three under a folder like the real vault does.
TREE.children = [
  { type: 'directory', name: 'Agreements', path: 'Agreements', children: [TREE.children[0], TREE.children[1]] },
  TREE.children[2],
  TREE.children[3],
];

fake.route('GET', '/api/notes', () => ({ notes: [] }));
fake.route('GET', '/api/personal/vault/tree', () => ({ tree: TREE }));
fake.route('GET', '/api/personal/vault/file', () => ({ path: 'inbox.md', name: 'inbox.md', content: 'x', modified: 1 }));

document.body.innerHTML = '<div id="chat-container"></div><div id="toast"></div>';
const notes = await import('../../../../static/js/notes.js');

const opened = [];
window.open = (...args) => { opened.push(args); return null; };
const clicked = [];
document.addEventListener('click', (e) => {
  const a = e.target.closest?.('a[download]');
  if (a) { clicked.push(a.getAttribute('href')); e.preventDefault(); }
});

async function openVault() {
  notes.openPanel();
  await waitFor(() => document.querySelector('[data-notes-mode="vault"]'), { what: 'the panel' });
  document.querySelector('[data-notes-mode="vault"]').click();
  await waitFor(() => document.querySelector('.vault-tree [data-vault-file]'), { what: 'the vault tree' });
  const folder = document.querySelector('[data-vault-folder="Agreements"]');
  folder.open = true;
  folder.dispatchEvent(new Event('toggle'));
}

test('clicking a PDF opens the raw file in a new tab and not the Markdown editor', async () => {
  await openVault();
  const row = document.querySelector('[data-vault-asset="Agreements/msa.pdf"]');
  assert.ok(row, 'the PDF is listed');
  assert.equal(row.dataset.vaultKind, 'pdf');
  assert.equal(row.closest('.vault-asset-row').querySelector('.vault-kind-badge').textContent, 'PDF');
  row.click();
  assert.equal(opened.length, 1);
  assert.match(opened[0][0], /\/api\/personal\/vault\/raw\?path=Agreements%2Fmsa\.pdf$/);
  assert.equal(document.getElementById('vault-file-editor'), null);
  assert.equal(fake.calls.filter((c) => c.url.pathname === '/api/personal/vault/file').length, 0);
});

test('a Word file downloads instead of previewing, and its download button asks for an attachment', async () => {
  opened.length = 0;
  clicked.length = 0;
  document.querySelector('[data-vault-asset="Agreements/msa.docx"]').click();
  document.querySelector('[data-vault-download="logo.png"]').click();
  assert.equal(opened.length, 0);
  assert.equal(clicked.length, 2);
  assert.ok(clicked.every((href) => href.includes('download=1')));
  assert.equal(document.getElementById('vault-file-editor'), null);
});

test('a Markdown entry still opens in the editor', async () => {
  document.querySelector('[data-vault-file="inbox.md"]').click();
  await waitFor(() => document.getElementById('vault-file-editor')?.value === 'x', { what: 'the editor' });
});
