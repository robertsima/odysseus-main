// A pending AI-edit diff when the AI moves on to another document
// (static/js/document.js). The diff belongs to the document that was active
// when it opened. If the active document changes while the diff is still
// pending, the next tab switch rejects the diff into whichever document is
// active then, and autosave writes the first document's text over the second
// one (#2467). The diff must be discarded into its own document first, both
// when an update names another document (handleDocUpdate) and when the AI
// starts streaming a new one (streamDocOpen).
import assert from 'node:assert/strict';
import { after, test } from 'node:test';

import { installDom } from '../_support/dom.mjs';
import { installFetchFake } from '../_support/fetchFake.mjs';

const dom = installDom({ width: 1440 });
const fake = installFetchFake();
after(async () => {
  fake.restore();
  await dom.restore();
});

const saves = [];
fake.route('PUT', /^\/api\/document\/[^/]+$/, ({ url, body }) => {
  saves.push({ id: url.pathname.split('/').pop(), content: JSON.parse(body).content });
  return { version_count: 2 };
});

document.body.innerHTML = '<div id="chat-container"></div><div id="toast"></div>';
const documentModule = await import('../../../../static/js/document.js');
documentModule.init('');
documentModule.openPanel();

const ALPHA_BEFORE = 'alpha 1\nalpha 2\nalpha 3\nalpha 4';
const ALPHA_AFTER = 'ALPHA ONE\nALPHA TWO\nALPHA THREE\nALPHA FOUR';

// Opens document `id` and gives it an AI edit large enough to open the diff
// review instead of being applied in place.
function openAlphaWithPendingDiff(id) {
  documentModule.handleDocUpdate({ doc_id: id, action: 'create', title: `Alpha ${id}`, language: 'markdown', content: ALPHA_BEFORE, version: 1 });
  documentModule.handleDocUpdate({ doc_id: id, action: 'edit', title: `Alpha ${id}`, language: 'markdown', content: ALPHA_AFTER, version: 2 });
  assert.ok(document.getElementById('doc-editor-wrap').classList.contains('diff-mode'), 'the edit opened a diff review');
}

// A pointer click away from the top-left corner. document.js treats a click
// whose coordinates fall inside the email Send button's box as a Send click,
// and a hidden button's box is 0x0 at the origin, where element.click() lands.
function clickTab(id) {
  const tab = document.querySelector(`#doc-tab-bar .doc-tab[data-doc-id="${id}"]`);
  tab.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, clientX: 400, clientY: 200 }));
}

async function settle() {
  await new Promise((resolve) => setTimeout(resolve, 20));
}

test('an update to another document does not write the pending diff into it', async () => {
  saves.length = 0;
  openAlphaWithPendingDiff('alpha-1');

  documentModule.handleDocUpdate({ doc_id: 'beta-1', action: 'create', title: 'Beta', language: 'markdown', content: 'beta body', version: 1 });
  clickTab('alpha-1');
  clickTab('beta-1');
  await settle();

  assert.equal(documentModule.getCurrentDocId(), 'beta-1');
  const betaSaves = saves.filter((s) => s.id === 'beta-1').map((s) => s.content);
  assert.ok(!betaSaves.includes(ALPHA_BEFORE), `beta was saved with alpha's text: ${JSON.stringify(saves)}`);
  assert.equal(document.getElementById('doc-editor-textarea').value, 'beta body');
});

test('streaming a new document does not write the pending diff into it', async () => {
  saves.length = 0;
  openAlphaWithPendingDiff('alpha-2');

  documentModule.streamDocOpen('Gamma', 'markdown');
  documentModule.streamDocDelta('gamma body');
  documentModule.handleDocUpdate({ doc_id: 'gamma-2', action: 'create', title: 'Gamma', language: 'markdown', content: 'gamma body', version: 1 });
  await settle();

  assert.equal(documentModule.getCurrentDocId(), 'gamma-2');
  const otherSaves = saves.filter((s) => s.id !== 'alpha-2').map((s) => s.content);
  assert.ok(!otherSaves.includes(ALPHA_BEFORE), `the new document was saved with alpha's text: ${JSON.stringify(saves)}`);
  assert.equal(document.getElementById('doc-editor-textarea').value, 'gamma body');
});
