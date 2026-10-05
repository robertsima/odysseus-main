// Settings > Privacy lists the indexed personal files grouped by folder,
// closed by default, and each Delete button removes the file by its stored
// path, not by the shortened name shown in the row.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, test } from 'node:test';

import { installDom, waitFor } from '../_support/dom.mjs';
import { installFetchFake } from '../_support/fetchFake.mjs';

const dom = installDom();
const fake = installFetchFake();
after(async () => {
  fake.restore();
  await dom.restore();
});

const index = new DOMParser().parseFromString(
  readFileSync(new URL('../../../../static/index.html', import.meta.url), 'utf8'), 'text/html');
document.body.appendChild(document.importNode(index.getElementById('settings-modal'), true));

fake.route('GET', '/api/personal', () => ({
  directories: [],
  files: [
    { name: 'notes/zeta.md', path: '/vault/notes/zeta.md', size: 10 },
    { name: 'notes\\alpha.md', path: 'C:\\vault\\notes\\alpha.md', size: 20 },
    { name: 'top.txt', path: '/vault/top.txt', size: 30 },
  ],
}));
fake.route('DELETE', '/api/personal/file', () => ({ ok: true }));

const admin = await import('../../../../static/js/admin.js');
admin._initData('privacy');

const fileList = () => document.getElementById('adm-ragFileList');
await waitFor(() => fileList().querySelector('details'), { what: 'the file groups' });

function groups() {
  return [...fileList().querySelectorAll('details')].map((group) => ({
    folder: group.querySelector('summary').textContent,
    files: [...group.querySelectorAll('.admin-rag-item-name')].map((n) => n.textContent),
    open: group.open,
  }));
}

test('files are grouped by folder, with Windows paths in the same folder', () => {
  const shown = groups();
  assert.deepEqual(shown.map((g) => g.files), [['alpha.md', 'zeta.md'], ['top.txt']]);
  assert.match(shown[0].folder, /^notes/);
});

test('every folder starts closed', () => {
  assert.deepEqual(groups().map((g) => g.open), [false, false]);
});

test('Delete removes the file by its stored path', async () => {
  const row = [...fileList().querySelectorAll('.admin-rag-file-row')]
    .find((r) => r.querySelector('.admin-rag-item-name').textContent === 'alpha.md');
  row.querySelector('button').click();
  await waitFor(() => document.getElementById('styled-confirm-ok'), { what: 'the confirm dialog' });
  document.getElementById('styled-confirm-ok').click();

  await waitFor(() => fake.calls.some((c) => c.method === 'DELETE'), { what: 'the DELETE request' });
  const call = fake.calls.find((c) => c.method === 'DELETE');
  assert.equal(call.url.searchParams.get('filepath'), 'C:\\vault\\notes\\alpha.md');
});
