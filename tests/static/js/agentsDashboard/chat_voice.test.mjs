// A chat's own voice, edited in the Agent Control Room's chat settings
// (static/js/agentsDashboard.js). Saving writes the persona name, temperature
// and reply cap to the chat's settings, where the server reads them for every
// turn the chat runs as an agent, and tells the composer's Agents menu, which
// then keeps the shared Prompt-window persona out of that chat.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { openRoom, waitFor } from './_room.mjs';

const room = await openRoom({
  rows: [{ session_id: 's1', name: 'Release chat', status: 'idle', source: 'odysseus', config: {} }],
  personaTemplates: [{ name: 'Release Captain', system_prompt: 'Ship small, ship often.', temperature: 0.7, max_tokens: 500 }],
});
const { dashboard, root } = room;

const saved = [];
room.fake.route('PATCH', '/api/session/s1/settings', ({ body }) => {
  saved.push(JSON.parse(body));
  return { settings: JSON.parse(body), approval_mode: null };
});
const announced = [];
document.addEventListener('odysseus:loadout-changed', (e) => announced.push(e.detail?.sessionId));

function edit(field, value) {
  const input = root.querySelector(`[data-config="${field}"]`);
  input.value = value;
  input.dispatchEvent(new Event('change', { bubbles: true }));
}

async function save() {
  const before = saved.length;
  root.querySelector('[data-ag="save-config"]').click();
  await waitFor(() => saved.length > before, { what: 'the settings PATCH' });
  return saved.at(-1);
}

test('saving a chat\'s voice stores it and tells the Agents menu', async () => {
  await dashboard.editLoadout('s1');
  await waitFor(() => root.querySelector('[data-config="agent_persona_name"]'), { what: 'the chat settings editor' });

  edit('agent_persona_name', 'Scout');
  edit('agent_temperature', '0.3');
  edit('agent_max_tokens', '900');
  const body = await save();

  assert.deepEqual(
    [body.agent_persona_name, body.agent_temperature, body.agent_max_tokens],
    ['Scout', 0.3, 900],
  );
  await waitFor(() => announced.includes('s1'), { what: 'the loadout-changed event' });
});

test('copying a saved persona fills the chat\'s voice', async () => {
  const picker = root.querySelector('[data-config-persona]');
  const option = [...picker.options].find((o) => o.textContent === 'Release Captain');
  assert.ok(option, 'the saved persona is offered');
  picker.value = option.value;
  picker.dispatchEvent(new Event('change', { bubbles: true }));
  await waitFor(() => root.querySelector('[data-config="agent_persona_name"]').value === 'Release Captain', { what: 'the copied persona' });

  const body = await save();
  assert.deepEqual(
    [body.agent_persona_name, body.agent_instructions, body.agent_temperature, body.agent_max_tokens],
    ['Release Captain', 'Ship small, ship often.', 0.7, 500],
  );
  dashboard.close();
});
