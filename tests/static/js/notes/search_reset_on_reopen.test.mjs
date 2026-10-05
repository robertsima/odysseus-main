// Issue #2919: a search typed in the Notes panel survived closing it. The
// reopened panel showed an empty search box but still filtered by the old
// query, so notes looked missing.
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

fake.route('GET', '/api/notes', () => ({
  notes: [
    { id: 'a', title: 'alpha', content: '' },
    { id: 'b', title: 'beta', content: '' },
  ],
}));

document.body.innerHTML = '<div id="chat-container"></div>';
const notes = await import('../../../../static/js/notes.js');

const shownIds = () => [...document.querySelectorAll('.note-card')].map((c) => c.dataset.noteId).sort();

test('a reopened Notes panel shows every note, not the last search', async () => {
  notes.openPanel();
  await waitFor(() => shownIds().length === 2, { what: 'both notes' });

  const search = document.getElementById('notes-search');
  search.value = 'alpha';
  search.dispatchEvent(new Event('input'));
  assert.deepEqual(shownIds(), ['a']);

  notes.closePanel();
  await waitFor(() => !document.getElementById('notes-pane'), { what: 'the panel to close' });

  notes.openPanel();
  await waitFor(() => fake.calls.filter((c) => c.url.pathname === '/api/notes').length === 2
    && document.querySelector('.note-card'), { what: 'the reopened panel to render' });
  assert.equal(document.getElementById('notes-search').value, '');
  assert.deepEqual(shownIds(), ['a', 'b']);
});
