// The loadout library (static/js/agentLoadouts.js), the editor the Agent
// Control Room shows for saved loadouts. Deleting a loadout asks first.
// Import and Export go through /api/agents/profiles/import and /export, an
// import's report shows what was refused, and integration templates are
// listed with an Install button. Saving writes /api/auth/settings and drops
// the page's shared settings snapshot so other modules see the new loadouts.
import assert from 'node:assert/strict';
import { after, beforeEach, test } from 'node:test';

import { installDom, waitFor } from '../_support/dom.mjs';
import { installFetchFake, jsonResponse } from '../_support/fetchFake.mjs';

const dom = installDom();
const fake = installFetchFake();
after(async () => {
  fake.restore();
  await dom.restore();
});

const server = { agent_profiles: [{ name: 'Scout', model: 'gpt-4o' }] };
let templates = [];
fake.route('GET', '/api/presets/templates', () => []);
fake.route('GET', '/api/agents/profiles/templates', () => ({ templates }));
fake.route('GET', '/api/auth/settings', () => ({ ...server }));
fake.route('POST', '/api/auth/settings', ({ body }) => {
  server.agent_profiles = JSON.parse(body).agent_profiles;
  return { ...server };
});

const ui = (await import('../../../../static/js/ui.js')).default;
const { getSettings } = await import('../../../../static/js/appConfig.js');
const { mountLoadoutsEditor } = await import('../../../../static/js/agentLoadouts.js');

// The styled confirm dialog, answered by the test.
const asked = [];
let answer = true;
ui.styledConfirm = async (text, options) => {
  asked.push({ text, ...options });
  return answer;
};

let box;
beforeEach(() => {
  document.body.innerHTML = '<div id="library"></div>';
  box = document.getElementById('library');
  asked.length = 0;
  answer = true;
});

function mount(profiles = [{ name: 'Scout', model: 'gpt-4o' }]) {
  return mountLoadoutsEditor(box, { profiles, canEdit: true });
}

const names = () => [...box.querySelectorAll('.agent-profile-summary-name')].map((el) => el.textContent);
const button = (label) => [...box.querySelectorAll('button')].find((b) => b.textContent === label);

function chooseFile(doc, mode) {
  box.querySelector('select[aria-label="Import mode"]').value = mode;
  const input = box.querySelector('input[type="file"]');
  const file = new File([JSON.stringify(doc)], 'loadouts.json', { type: 'application/json' });
  Object.defineProperty(input, 'files', { value: [file], configurable: true });
  input.dispatchEvent(new Event('change'));
}

test('deleting a loadout asks first and keeps it when the user cancels', async () => {
  mount([{ name: 'Scout' }, { name: 'Builder' }]);

  answer = false;
  box.querySelector('.agent-profile-remove').click();
  await waitFor(() => asked.length === 1, { what: 'the confirm dialog' });
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.deepEqual(names(), ['Scout', 'Builder']);
  assert.equal(asked[0].danger, true);

  answer = true;
  box.querySelector('.agent-profile-remove').click();
  await waitFor(() => names().length === 1, { what: 'the loadout to go' });
  assert.deepEqual(names(), ['Builder']);
});

test('an import that keeps both posts a merge with renames and shows what was refused', async () => {
  const imports = [];
  fake.route('POST', '/api/agents/profiles/import', ({ body }) => {
    imports.push(JSON.parse(body));
    return {
      ok: true,
      message: 'Imported 1 loadout',
      profiles: [{ name: 'Scout' }, { name: 'Scout (2)' }],
      report: { added: ['Scout (2)'], errors: [{ name: 'bad/name', error: 'invalid name' }], warnings: ['model qwen9 is not configured'] },
    };
  });
  mount();
  const doc = { format: 'odysseus-agent-profiles', version: 1, profiles: [{ name: 'Scout' }, { name: 'bad/name' }] };

  chooseFile(doc, 'rename');
  await waitFor(() => names().length === 2, { what: 'the imported list' });

  assert.deepEqual(imports.at(-1), { document: doc, mode: 'merge', rename_conflicts: true });
  assert.deepEqual(names(), ['Scout', 'Scout (2)']);
  const report = box.textContent;
  assert.match(report, /Error in bad\/name: invalid name/);
  assert.match(report, /Warning: model qwen9 is not configured/);
});

test('a replacing import asks first', async () => {
  const before = fake.calls.filter((c) => c.url.pathname === '/api/agents/profiles/import').length;
  mount();
  answer = false;
  chooseFile({ format: 'odysseus-agent-profiles', version: 1, profiles: [] }, 'replace');
  await waitFor(() => asked.length === 1, { what: 'the confirm dialog' });
  await new Promise((resolve) => setTimeout(resolve, 0));

  assert.equal(asked[0].danger, true);
  assert.equal(fake.calls.filter((c) => c.url.pathname === '/api/agents/profiles/import').length, before);
  assert.deepEqual(names(), ['Scout']);
});

test('Export downloads the saved loadouts under the server\'s file name', async () => {
  fake.route('GET', '/api/agents/profiles/export', () => new Response('{"profiles": []}', {
    headers: { 'Content-Type': 'application/json', 'Content-Disposition': 'attachment; filename="loadouts-2026.json"' },
  }));
  // The browser's download: a blob URL and a click on a download link.
  // (Node's blob URLs reject happy-dom's Blob, so they are stood in for.)
  const downloads = [];
  const { click } = HTMLAnchorElement.prototype;
  const { createObjectURL, revokeObjectURL } = URL;
  HTMLAnchorElement.prototype.click = function recordDownload() { downloads.push(this.download); };
  URL.createObjectURL = () => 'blob:loadouts';
  URL.revokeObjectURL = () => {};
  try {
    mount();
    button('Export').click();
    await waitFor(() => downloads.length, { what: 'the download' });
  } finally {
    HTMLAnchorElement.prototype.click = click;
    Object.assign(URL, { createObjectURL, revokeObjectURL });
  }
  assert.deepEqual(downloads, ['loadouts-2026.json']);
});

test('integration templates are listed with an Install button that installs them', async () => {
  templates = [{
    integration: 'penpot', integration_name: 'Penpot', template: 'penpot-product-designer', installed: false,
    profiles: [{ name: 'Penpot Product Designer', description: 'Designs in Penpot' }],
  }];
  const installs = [];
  fake.route('POST', '/api/agents/profiles/templates/install', ({ body }) => {
    installs.push(JSON.parse(body));
    if (installs.length === 1) return jsonResponse({ detail: 'Penpot Product Designer already exists.' }, { status: 409 });
    return { ok: true, message: 'Installed', profiles: [{ name: 'Scout' }, { name: 'Penpot Product Designer' }], report: {} };
  });
  mount();
  await waitFor(() => button('Install'), { what: 'the template row' });
  assert.match(box.querySelector('.agent-profile-template').textContent, /Penpot Product Designer \(Penpot\)/);

  button('Install').click();
  await waitFor(() => names().includes('Penpot Product Designer'), { what: 'the installed loadout' });

  // The first install met an existing loadout; the user agreed to replace it.
  assert.deepEqual(installs, [
    { integration: 'penpot', template: 'penpot-product-designer', overwrite: false },
    { integration: 'penpot', template: 'penpot-product-designer', overwrite: true },
  ]);
  templates = [];
});

test('saving the loadouts refreshes the page\'s shared settings', async () => {
  assert.deepEqual((await getSettings()).agent_profiles.map((p) => p.name), ['Scout']);
  mount([{ name: 'Scout', model: 'gpt-4o' }]);
  button('Add loadout').click();
  const nameInput = [...box.querySelectorAll('.agent-profile-item')].at(-1).querySelector('input');
  nameInput.value = 'Reviewer';
  nameInput.dispatchEvent(new Event('input'));

  button('Save loadouts').click();
  await waitFor(() => server.agent_profiles.length === 2, { what: 'the save' });
  await waitFor(() => box.textContent.includes('Saved'), { what: 'the save to finish' });

  assert.deepEqual((await getSettings()).agent_profiles.map((p) => p.name), ['Scout', 'Reviewer']);
});

test('a loadout copies its voice from a saved persona and saves it', async () => {
  mount([{ name: 'Scout' }]);
  const picker = () => box.querySelector('.agent-profile-voice select');
  await waitFor(() => [...picker().options].some((o) => o.textContent === 'Socrates'), { what: 'the saved personas' });

  const option = [...picker().options].find((o) => o.textContent === 'Socrates');
  picker().value = option.value;
  picker().dispatchEvent(new Event('change'));
  button('Save loadouts').click();
  await waitFor(() => server.agent_profiles[0]?.persona_name === 'Socrates', { what: 'the save' });

  const [scout] = server.agent_profiles;
  assert.equal(scout.temperature, 0.9);
  assert.match(scout.instructions, /^Never answer directly/);
});
