// Typing an exact parent command such as `/setup` in the composer lists the
// command and every one of its subcommands in static/js/slashAutocomplete.js.
// When the rows did not fit the popup's row cap, the last /setup subcommand
// (ChatGPT Subscription) or the /setup row itself was cut. Without the /setup
// row, Enter no longer counts as sending the typed command and replaces it
// with the first suggestion.
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

fake.route('GET', '/api/skills/slash-catalog', () => ({ skills: [] }));

document.body.innerHTML = '<textarea id="message"></textarea>';

const { COMMANDS } = await import('../../../../static/js/slashCommands.js');
const { initSlashAutocomplete } = await import('../../../../static/js/slashAutocomplete.js');

const composer = document.getElementById('message');
initSlashAutocomplete(composer);
await waitFor(() => fake.calls.length > 0, { what: 'the skill catalog request' });

function type(text) {
  composer.value = text;
  composer.dispatchEvent(new Event('input', { bubbles: true }));
  return [...document.querySelectorAll('#slash-autocomplete .slash-ac-row')].map((row) => row.dataset.token);
}

test('typing /setup lists /setup and every /setup subcommand', () => {
  const expected = Object.entries(COMMANDS.setup.subs)
    .filter(([sub, def]) => !sub.startsWith('_') && !def.hidden)
    .map(([sub]) => `/setup ${sub}`)
    .concat('/setup');

  const shown = type('/setup');

  assert.ok(shown.includes('/setup chatgpt-subscription'), `missing from ${JSON.stringify(shown)}`);
  assert.deepEqual(shown.filter((t) => t === '/setup' || t.startsWith('/setup ')).sort(), expected.sort());
});

test('Enter on a fully typed /setup sends it instead of taking a suggestion', () => {
  type('/setup');
  const enter = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true });
  composer.dispatchEvent(enter);

  assert.equal(enter.defaultPrevented, false);
  assert.equal(composer.value, '/setup');
});
