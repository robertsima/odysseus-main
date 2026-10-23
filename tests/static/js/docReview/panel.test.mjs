// The export menu's first item opens the Review changes sheet, and the sheet
// draws from /versions without writing anything. Bugs guarded: Review dropped
// from the menu head, the one-version document showing an empty panel instead
// of the first-version note, and the panel issuing a write.
import assert from 'node:assert/strict';
import { after, test } from 'node:test';

import { installDom, waitFor } from '../_support/dom.mjs';
import { installFetchFake } from '../_support/fetchFake.mjs';

const dom = installDom({ width: 1440 });
const fake = installFetchFake();
after(async () => {
  fake.restore();
  await dom.restore();
});

const OLD = '# Agreement\n\n## 1. Scope\n\nDesign work.\n\n## 2. Fees\n\n| Item | Fee |\n|---|---|\n| Design | 1000 |\n';
const LIVE = '# Agreement\n\n## 1. Scope\n\nDesign work, see [[annex]].\n\n## 2. Fees\n\n| Item | Fee |\n|---|---|\n| Design | 1200 |\n\nTODO confirm VAT\n';
let versions = [
  { id: 'b', version_number: 2, content: LIVE, summary: null, source: 'ai', created_at: '2026-10-23T10:00:00' },
  { id: 'a', version_number: 1, content: OLD, summary: null, source: 'user', created_at: '2026-10-22T10:00:00' },
];
fake.route('GET', /^\/api\/document\/[^/]+\/versions$/, () => versions);
fake.route('PUT', /^\/api\/document\/[^/]+$/, () => ({ version_count: 2 }));

document.body.innerHTML = '<div id="chat-container"></div><div id="toast"></div>';
const documentModule = await import('../../../../static/js/document.js');
documentModule.init('');
documentModule.openPanel();
documentModule.handleDocUpdate({ doc_id: 'agr', action: 'create', title: 'Agreement', language: 'markdown', content: LIVE, version: 2 });

// happy-dom lays every element out at (0, 0), where document.js reads a click
// as one on the hidden email Send button; click with real coordinates.
function clickItem(menu) {
  menu.querySelector('.doc-overflow-item').dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, clientX: 400, clientY: 200 }));
}

function openExportMenu() {
  const btn = document.getElementById('doc-footer-export-btn');
  btn.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, clientX: 400, clientY: 200 }));
  return document.getElementById('doc-export-menu');
}

test('Review changes is the first item in the export menu and opens the sheet', async () => {
  const menu = openExportMenu();
  const items = [...menu.querySelectorAll('.doc-overflow-item')].map((b) => b.textContent);
  assert.equal(items[0], 'Review changes');
  assert.ok(items.includes('Export as Word'));

  clickItem(menu);
  const panel = await waitFor(() => document.querySelector('#doc-review-panel .dr-summary'))
    .then(() => document.getElementById('doc-review-panel'));

  assert.deepEqual([...panel.querySelectorAll('.dr-summary li')].map((l) => l.textContent), [
    'Changed: 2 sections', 'Placeholders: 2 remain', 'Suggestions: none unresolved', 'Checks: none configured',
  ]);
  const sel = panel.querySelector('#doc-review-compare');
  assert.match(sel.options[sel.selectedIndex].textContent, /^Previous version \(v1, .*by you\)$/);
  const rows = [...panel.querySelectorAll('.dr-del .dr-text, .dr-add .dr-text')].map((n) => n.textContent);
  assert.ok(rows.includes('| Design | 1000 |') && rows.includes('| Design | 1200 |'));
  assert.deepEqual([...panel.querySelectorAll('.dr-ph')].map((b) => b.dataset.line), ['5', '13']);
  assert.deepEqual([...panel.querySelectorAll('.dr-foot button')].map((b) => b.textContent), ['Preview PDF', 'Export…']);
});

test('the sheet reads only: no write was sent', () => {
  assert.deepEqual(fake.calls.filter((c) => c.method !== 'GET'), []);
});

test('a single-version document says there is nothing to compare and still lists placeholders', async () => {
  versions = [versions[0]];
  document.getElementById('doc-review-panel')?.remove();
  clickItem(openExportMenu());
  const panel = await waitFor(() => document.querySelector('#doc-review-panel .dr-note:not(:only-child)'))
    .then(() => document.getElementById('doc-review-panel'));
  assert.match(panel.textContent, /This is the first version\. Nothing to compare yet\./);
  assert.equal(panel.querySelectorAll('.dr-ph').length, 2);
  assert.equal(panel.querySelector('#doc-review-compare'), null);
});
