// The #toast overlay in static/js/ui.js. A toast with an action button sets
// pointer-events: auto on #toast so the button can be clicked. When that toast
// goes away, by its × button or its timer, or a plain toast or an error toast
// replaces it, the overlay must stop taking pointer events. Otherwise an
// invisible fixed element near the top-right swallows clicks and touches.
import assert from 'node:assert/strict';
import { after, beforeEach, test } from 'node:test';

import { installDom, waitFor } from '../_support/dom.mjs';

const dom = installDom();
after(async () => { await dom.restore(); });

document.body.innerHTML = '<div id="toast"></div>';
const toast = document.getElementById('toast');
const ui = (await import('../../../../static/js/ui.js')).default;

function showActionToast(duration = 5000) {
  ui.showToast('Note deleted', { action: 'Undo', onAction() {}, duration });
  assert.equal(toast.style.pointerEvents, 'auto', 'the action button needs pointer events');
}

beforeEach(() => {
  toast.style.pointerEvents = '';
});

test('dismissing an action toast with its x button releases pointer events', () => {
  showActionToast();

  toast.querySelector('.toast-close-btn').click();

  assert.equal(toast.style.pointerEvents, '');
  assert.ok(!toast.classList.contains('show'));
});

test('an action toast that times out releases pointer events', async () => {
  showActionToast(30);

  await waitFor(() => !toast.classList.contains('show'), { what: 'the toast to hide' });

  assert.equal(toast.style.pointerEvents, '');
});

test('a plain toast after an action toast does not keep pointer events', () => {
  showActionToast();

  ui.showToast('Saved');

  assert.equal(toast.style.pointerEvents, '');
});

test('dismissing an error toast shown over an action toast releases pointer events', () => {
  showActionToast();
  ui.showError('Could not save');

  toast.querySelector('.toast-close-btn').click();

  assert.equal(toast.style.pointerEvents, '');
});
