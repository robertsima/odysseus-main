// Issue #2791: the Notes panel's "Esc cancels select mode" listener sits on
// document in the capture phase and stops the event. It leaked on close, so
// after closing the panel in select mode the next Escape anywhere in the app
// was swallowed before any other handler (a modal's close, say) saw it.
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

fake.route('GET', '/api/notes', () => ({ notes: [{ id: 'a', title: 'alpha', content: '' }] }));

document.body.innerHTML = '<div id="chat-container"></div><button id="elsewhere"></button>';
const notes = await import('../../../../static/js/notes.js');

function pressEscape(target) {
  const event = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true });
  target.dispatchEvent(event);
  return event;
}

test('Escape cancels select mode while the panel is open', async () => {
  notes.openPanel();
  await waitFor(() => document.querySelector('.note-card'), { what: 'the notes' });
  document.getElementById('notes-select-btn').click();
  assert.equal(document.getElementById('notes-bulk-bar').classList.contains('hidden'), false);

  pressEscape(document.getElementById('notes-search'));

  assert.equal(document.getElementById('notes-bulk-bar').classList.contains('hidden'), true);
  assert.equal(notes.isPanelOpen(), true);
  notes.closePanel();
  await waitFor(() => !document.getElementById('notes-pane'), { what: 'the panel to close' });
});

test('after closing the panel in select mode, Escape elsewhere reaches its handlers', async () => {
  notes.openPanel();
  await waitFor(() => document.querySelector('.note-card'), { what: 'the notes' });
  document.getElementById('notes-select-btn').click();
  notes.closePanel();
  await waitFor(() => !document.getElementById('notes-pane'), { what: 'the panel to close' });

  const elsewhere = document.getElementById('elsewhere');
  let seen = 0;
  elsewhere.addEventListener('keydown', () => { seen += 1; });
  const event = pressEscape(elsewhere);

  assert.equal(seen, 1, 'a Notes listener left on document swallowed the Escape');
  assert.equal(event.defaultPrevented, false);
});
