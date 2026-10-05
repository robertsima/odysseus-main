// The confirm and prompt dialogs built by static/js/ui.js block the page, so
// they are modal dialogs named by their title. While open, Tab stays inside
// them; when they close, focus returns to what had it. Every toast has a
// labelled button that dismisses it.
import assert from 'node:assert/strict';
import { after, test } from 'node:test';

import { installDom, waitFor } from '../_support/dom.mjs';

const dom = installDom();
after(() => dom.restore());

document.body.innerHTML = '<button id="trigger">Delete</button><div id="toast" class="toast"></div>';
const ui = await import('../../../../static/js/ui.js');

const trigger = document.getElementById('trigger');

function dialogOf(overlayId) {
  return document.querySelector(`#${overlayId} [role="dialog"]`);
}

function nameOf(dialog) {
  return document.getElementById(dialog.getAttribute('aria-labelledby'))?.textContent;
}

// The dialog has to cancel Tab's default move and place focus itself; left
// alone, the browser would move focus to the page behind.
function pressTab(shiftKey = false) {
  const before = document.activeElement;
  const event = new KeyboardEvent('keydown', { key: 'Tab', shiftKey, bubbles: true, cancelable: true });
  document.dispatchEvent(event);
  assert.equal(event.defaultPrevented, true, 'Tab is handled by the dialog');
  assert.notEqual(document.activeElement, before, 'Tab moves focus');
}

test('the confirm dialog is a modal dialog named by its title', async () => {
  trigger.focus();
  const answer = ui.styledConfirm('Delete this chat?', { title: 'Delete chat' });
  const dialog = dialogOf('styled-confirm-overlay');

  assert.equal(dialog.getAttribute('aria-modal'), 'true');
  assert.equal(nameOf(dialog), 'Delete chat');

  document.getElementById('styled-confirm-cancel').click();
  assert.equal(await answer, false);
});

test('Tab stays inside the confirm dialog and focus returns to the trigger on close', async () => {
  trigger.focus();
  const answer = ui.styledConfirm('Delete this chat?');
  const dialog = dialogOf('styled-confirm-overlay');

  for (let i = 0; i < 4; i += 1) {
    pressTab(i % 2 === 1);
    assert.ok(dialog.contains(document.activeElement), `focus left the dialog after Tab ${i + 1}`);
  }
  document.getElementById('styled-confirm-ok').click();
  assert.equal(await answer, true);
  assert.equal(document.activeElement, trigger);
});

test('the prompt dialog is named, traps Tab and gives focus back', async () => {
  trigger.focus();
  const answer = ui.styledPrompt('New name', { title: 'Rename chat', defaultValue: 'plans' });
  const dialog = dialogOf('styled-prompt-overlay');

  assert.equal(dialog.getAttribute('aria-modal'), 'true');
  assert.equal(nameOf(dialog), 'Rename chat');
  document.getElementById('styled-prompt-input').focus();
  for (let i = 0; i < 4; i += 1) {
    pressTab();
    assert.ok(dialog.contains(document.activeElement), `focus left the dialog after Tab ${i + 1}`);
  }
  document.getElementById('styled-prompt-ok').click();
  assert.equal(await answer, 'plans');
  assert.equal(document.activeElement, trigger);
});

for (const [name, show] of [['showToast', (m) => ui.showToast(m, 60000)], ['showError', (m) => ui.showError(m)]]) {
  test(`${name} has a labelled button that dismisses it`, async () => {
    const toast = document.getElementById('toast');
    show('Saved');
    await waitFor(() => toast.classList.contains('show'), { what: 'the toast' });

    const dismiss = [...toast.querySelectorAll('button')].find((b) => b.getAttribute('aria-label'));
    assert.ok(dismiss, 'the toast has a button with an accessible name');
    dismiss.click();
    assert.equal(toast.classList.contains('show'), false);
  });
}
