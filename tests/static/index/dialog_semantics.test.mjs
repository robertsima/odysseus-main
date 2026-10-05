// Screen readers announce the tool windows in static/index.html as dialogs
// only when each carries role="dialog" and an accessible name, and read a
// close button whose only content is a glyph as "heavy multiplication x"
// unless it has a label. These are dockable windows, not blocking modals, so
// they must not claim aria-modal.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, test } from 'node:test';

import { installDom } from '../js/_support/dom.mjs';

const dom = installDom();
after(() => dom.restore());

const page = new DOMParser().parseFromString(
  readFileSync(new URL('../../../static/index.html', import.meta.url), 'utf8'), 'text/html');

function accessibleName(el) {
  const labelledBy = el.getAttribute('aria-labelledby');
  if (labelledBy) {
    return labelledBy.split(/\s+/).map((id) => page.getElementById(id)?.textContent || '').join(' ').trim();
  }
  return (el.getAttribute('aria-label') || el.getAttribute('title') || '').trim();
}

const WINDOWS = ['memory-modal', 'theme-modal', 'custom-preset-modal', 'rename-session-modal', 'cookbook-modal', 'settings-modal'];

for (const id of WINDOWS) {
  test(`#${id} is announced as a named, non-modal dialog`, () => {
    const box = page.querySelector(`#${id} > .modal-content`);
    assert.ok(box, `index.html has #${id}`);
    assert.equal(box.getAttribute('role'), 'dialog');
    assert.notEqual(accessibleName(box), '');
    assert.notEqual(box.getAttribute('aria-modal'), 'true');
  });
}

test('every window close button has an accessible name', () => {
  const buttons = [...page.querySelectorAll('button.close-btn')];
  assert.ok(buttons.length > 0);
  assert.deepEqual(buttons.filter((b) => !accessibleName(b)).map((b) => b.outerHTML), []);
});
