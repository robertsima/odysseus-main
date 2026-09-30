/* agentLoadouts.js — the loadout library, shown in the Agent Control Room.
 *
 * A loadout (agent profile) is a reusable agent definition: persona and
 * instructions, worker model, tools, skills, MCP connections, memory and vault
 * access, approvals, delegation and worker limits. A chat or worker based on
 * one keeps its own copy of it, which the Control Room's chat editor changes;
 * this is where the loadouts themselves are made, changed and deleted.
 *
 * This editor used to live in Settings › Agents, beside a near-identical
 * per-chat editor in the Control Room that saved somewhere else, and nothing
 * on screen said which one you were in. It moved here unchanged, except that
 * deleting a loadout now asks first.
 *
 * Saving writes every loadout through POST /api/auth/settings (admin only),
 * which validates them and carries policy edits into the chats based on them
 * that have not changed those settings themselves (agent_profiles.
 * propagate_profile_edits). Export/Import use /api/agents/profiles/*.
 */
import uiModule from './ui.js';

const LIST_KEYS = ['disabled_tools', 'enabled_tools', 'skill_names', 'allowed_mcp_servers', 'allowed_models', 'model_fallbacks'];
const NUMBER_KEYS = ['max_rounds', 'max_parallel_workers', 'temperature', 'max_tokens'];

function blankLoadout() {
  return { name: '', description: '', model: '', model_fallbacks: [], model_access: 'current', allowed_models: [],
    max_rounds: 0, max_parallel_workers: 1, disabled_tools: [], tool_access: 'all', enabled_tools: [],
    memory_access: 'read', skill_access: 'all', skill_names: [], mcp_access: 'all', allowed_mcp_servers: [],
    private_vault_access: false, shell_access: 'sandbox', approval_mode: 'inherit', delegation_policy: 'explicit', instructions: '',
    persona_name: '', temperature: null, max_tokens: null };
}

async function ask(text, { confirmText = 'Confirm', title = 'Confirm', danger = false } = {}) {
  try {
    return await (uiModule && uiModule.styledConfirm
      ? uiModule.styledConfirm(text, { confirmText, cancelText: 'Cancel', title, danger })
      : Promise.resolve(window.confirm(text)));
  } catch (_) { return false; }
}

function node(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text != null) el.textContent = text;
  return el;
}

function button(label, title) {
  const b = node('button', 'ats-btn', label);
  b.type = 'button';
  if (title) b.title = title;
  return b;
}

/**
 * Render the loadout library into `container`.
 * @param {HTMLElement} container
 * @param {{profiles: object[], canEdit: boolean, onSaved?: (profiles: object[]) => void}} opts
 * @returns {{isDirty: () => boolean}}
 */
export function mountLoadoutsEditor(container, { profiles: initial = [], canEdit = false, onSaved } = {}) {
  container.textContent = '';
  let profiles = (initial || []).map((p) => Object.assign({}, p));
  let dirty = false;
  // Which rows are open survives a re-render (add, remove, persona copy) and a
  // save, which replaces every object with the server's normalized copy.
  let openProfiles = new WeakSet();

  const list = node('div', 'agent-profiles');
  const actions = node('div', 'agent-profiles-actions');
  const addBtn = button('Add loadout');
  const saveBtn = button('Save loadouts');
  const exportBtn = button('Export', "Download the saved loadouts as a JSON file. Credentials pasted into a loadout's text are redacted.");
  const importBtn = button('Import', 'Load loadouts from an exported JSON file. Each one is validated like a normal save.');
  const modeSel = node('select', 'settings-select');
  modeSel.style.maxWidth = '220px';
  modeSel.title = 'What Import does with loadouts that already exist';
  modeSel.setAttribute('aria-label', 'Import mode');
  [['merge', 'Merge, overwrite same names'], ['rename', 'Merge, keep both'], ['replace', 'Replace all loadouts']].forEach(([v, l]) => {
    const o = node('option', '', l); o.value = v; modeSel.appendChild(o);
  });
  const fileInput = node('input');
  fileInput.type = 'file'; fileInput.accept = '.json,application/json'; fileInput.hidden = true;
  const note = node('span', 'admin-toggle-sub');
  note.setAttribute('role', 'status');
  const reportBox = node('div', 'admin-toggle-sub');
  reportBox.style.marginTop = '6px';
  reportBox.hidden = true;
  actions.append(addBtn, saveBtn, exportBtn, importBtn, modeSel, fileInput, note);
  container.append(list, actions, reportBox);

  if (!canEdit) {
    [addBtn, saveBtn, importBtn, modeSel].forEach((el) => { el.disabled = true; });
    addBtn.title = saveBtn.title = importBtn.title = 'Only an admin can change loadouts';
  }

  function say(text, bad) {
    note.textContent = text;
    note.style.color = bad ? 'var(--red)' : 'var(--fg)';
  }
  function markDirty(text) {
    dirty = true;
    say(text || 'Unsaved changes');
  }

  // Saved personas a loadout can start from. Choosing one copies it into the
  // loadout, which then keeps its own persona.
  const personaSources = [];
  Promise.all([
    import('./presets.js').then((m) => m.PROMPT_TEMPLATES || []).catch(() => []),
    fetch('/api/presets/templates', { credentials: 'same-origin' }).then((r) => (r.ok ? r.json() : [])).catch(() => []),
  ]).then(([templates, rows]) => {
    templates.forEach((t) => {
      personaSources.push({ name: t.name, persona_name: t.noName ? '' : t.name, instructions: t.prompt || '', temperature: t.temperature });
    });
    (Array.isArray(rows) ? rows : []).forEach((t) => {
      if (!t || !t.name || personaSources.some((s) => s.name === t.name)) return;
      personaSources.push({ name: t.name, persona_name: t.name, instructions: t.system_prompt || '',
        temperature: t.temperature, max_tokens: t.max_tokens || null });
    });
    if (!list.contains(document.activeElement)) render();
  }).catch(() => {});

  function field(label, input, hint) {
    const wrap = node('label', 'agent-profile-field');
    const span = node('span', '', label);
    if (hint) span.title = hint;
    wrap.append(span, input);
    return wrap;
  }
  function toolsSummary(p) {
    const access = p.tool_access || 'all';
    const text = access === 'none' ? 'no action tools'
      : access === 'selected' ? `${(p.enabled_tools || []).length} selected tools` : 'all tools';
    const denied = (p.disabled_tools || []).length;
    return denied ? `${text}, ${denied} denied` : text;
  }
  function fillSummary(summary, p) {
    summary.textContent = '';
    summary.append(
      node('span', 'agent-profile-summary-name', p.name || '(unnamed)'),
      node('span', 'agent-profile-summary-meta', [p.model ? p.model : 'workers use the calling chat’s model', toolsSummary(p)].join(' · ')),
    );
    if (p.description) summary.appendChild(node('span', 'agent-profile-summary-desc', p.description));
  }

  function render() {
    list.textContent = '';
    if (!profiles.length) {
      list.appendChild(node('div', 'admin-toggle-sub', 'No loadouts yet.'));
      return;
    }
    profiles.forEach((p, i) => {
      const item = node('details', 'agent-profile-item');
      if (openProfiles.has(p)) item.open = true;
      item.addEventListener('toggle', () => { if (item.open) openProfiles.add(p); else openProfiles.delete(p); });
      const summary = node('summary');
      fillSummary(summary, p);
      item.appendChild(summary);
      const refreshSummary = () => fillSummary(summary, p);
      const card = node('div', 'agent-profile');
      const mk = (tag, key, attrs) => {
        const inp = document.createElement(tag);
        inp.className = tag === 'textarea' ? 'settings-textarea' : 'settings-input';
        Object.keys(attrs || {}).forEach((a) => inp.setAttribute(a, attrs[a]));
        const v = p[key];
        inp.value = Array.isArray(v) ? v.join(', ') : (v == null ? '' : String(v));
        inp.disabled = !canEdit;
        inp.addEventListener('input', () => {
          p[key] = NUMBER_KEYS.includes(key) ? inp.value : (LIST_KEYS.includes(key)
            ? inp.value.split(/[\n,]+/).map((s) => s.trim()).filter(Boolean) : inp.value);
          markDirty();
          refreshSummary();
        });
        return inp;
      };
      const choice = (key, options, hint) => {
        const sel = node('select', 'settings-select');
        const current = p[key] == null ? options[0][0] : p[key];
        options.forEach(([value, label]) => {
          const opt = node('option', '', label); opt.value = value;
          if (String(current) === String(value)) opt.selected = true;
          sel.appendChild(opt);
        });
        sel.title = hint || '';
        sel.disabled = !canEdit;
        sel.addEventListener('change', () => { p[key] = sel.value; markDirty(); refreshSummary(); });
        return sel;
      };
      const head = node('div', 'agent-profile-head');
      head.appendChild(field('Name', mk('input', 'name', { placeholder: 'researcher', maxlength: '40' })));
      head.appendChild(field('Model', mk('input', 'model', { placeholder: 'workers: empty = calling chat’s model' }), 'model or model@endpoint. Applies to delegated workers only; a chat switched to this loadout keeps its own model.'));
      head.appendChild(field('Rounds', mk('input', 'max_rounds', { type: 'number', min: '0', max: '200', placeholder: '0 = no budget' }), 'Round budget (0 = no budget, max 200). A positive number is the round at which the worker is asked to wrap up and hand back what it has, including what is left. It is never cut off mid-task.'));
      const remove = button('Delete');
      remove.classList.add('agent-profile-remove');
      remove.disabled = !canEdit;
      remove.addEventListener('click', async () => {
        const label = p.name || 'this unnamed loadout';
        const ok = await ask(
          `Delete the loadout “${label}”? It is removed when you click Save loadouts. Chats already based on it keep their own settings; workers can no longer be launched with it.`,
          { title: 'Delete loadout', confirmText: 'Delete', danger: true });
        if (!ok) return;
        profiles.splice(profiles.indexOf(p), 1);
        render();
        markDirty(`Deleted ${label} — click Save loadouts to apply`);
      });
      head.appendChild(remove);
      card.appendChild(head);
      card.appendChild(field('Description', mk('input', 'description', { placeholder: 'What this worker is for (shown to the agent)', maxlength: '300' })));
      // Each loadout's own voice. A chat running under it uses these instead
      // of the shared persona from the Prompt window.
      const voice = node('div', 'agent-profile-voice');
      const from = node('select', 'settings-select');
      from.innerHTML = '<option value="">Copy a persona…</option>';
      personaSources.forEach((src, idx) => { const opt = node('option', '', src.name); opt.value = String(idx); from.appendChild(opt); });
      from.disabled = !canEdit;
      from.addEventListener('change', () => {
        const src = personaSources[Number(from.value)];
        if (!src) return;
        p.persona_name = src.persona_name || '';
        p.instructions = src.instructions || '';
        if (src.temperature != null) p.temperature = src.temperature;
        if (src.max_tokens) p.max_tokens = src.max_tokens;
        render();
        markDirty(`Copied ${src.name} — unsaved`);
      });
      voice.appendChild(field('Start from persona', from, 'Copies a saved persona into this loadout'));
      voice.appendChild(field('Persona name', mk('input', 'persona_name', { placeholder: 'none', maxlength: '60' }), 'The name this agent answers as'));
      voice.appendChild(field('Temperature', mk('input', 'temperature', { type: 'number', min: '0', max: '2', step: '0.05', placeholder: 'default' })));
      voice.appendChild(field('Max tokens', mk('input', 'max_tokens', { type: 'number', min: '0', max: '65536', step: '1', placeholder: 'default' }), '0 or blank lets the server decide'));
      voice.appendChild(field('Reasoning effort', choice('reasoning_effort', [['', 'Default'], ['minimal', 'Minimal'], ['low', 'Low (faster)'], ['medium', 'Medium'], ['high', 'High (slower)']]),
        'How much a ChatGPT-subscription model thinks before each step. Low makes skim-and-collect workers much faster per round.'));
      card.appendChild(voice);
      card.appendChild(field('Instructions / personality', mk('textarea', 'instructions', { rows: '3', placeholder: 'How this agent thinks, speaks and works' })));
      const policies = node('div', 'agent-profile-policy-grid');
      policies.appendChild(field('Delegation', choice('delegation_policy', [['explicit', 'Only when asked'], ['never', 'Never'], ['auto', 'Agent decides']]), 'When this worker may create or hand off to other agents.'));
      policies.appendChild(field('Approvals', choice('approval_mode', [['inherit', 'Global default'], ['ask_risky', 'Ask for risky'], ['ask_all', 'Ask for every change'], ['auto', 'Automatic']])));
      policies.appendChild(field('Memory', choice('memory_access', [['read', 'Read only'], ['none', 'Off'], ['write', 'Read + write']])));
      policies.appendChild(field('Models', choice('model_access', [['current', 'Current only'], ['selected', 'Selected models'], ['all', 'All configured']])));
      policies.appendChild(field('Skills', choice('skill_access', [['all', 'All skills'], ['selected', 'Selected skills'], ['none', 'No skills']])));
      policies.appendChild(field('Tools', choice('tool_access', [['all', 'All tools'], ['selected', 'Selected tools'], ['none', 'No action tools']])));
      policies.appendChild(field('MCP / integrations', choice('mcp_access', [['all', 'All connected'], ['selected', 'Selected servers'], ['none', 'None']])));
      policies.appendChild(field('Parallel workers', mk('input', 'max_parallel_workers', { type: 'number', min: '0', max: '8', placeholder: '1' })));
      if (!p.shell_access) p.shell_access = 'sandbox';
      policies.appendChild(field('Shell (bash, python)', choice('shell_access', [['sandbox', 'Sandboxed'], ['host', 'Full server shell'], ['off', 'Off']]), 'Sandboxed sees only the workspace (or a scratch folder): no app data, no vault. Full runs unrestricted and can read everything, the vault included. Separate from vault reads.'));
      card.appendChild(policies);
      const vault = node('label', 'agent-profile-private');
      const vaultCheck = node('input');
      vaultCheck.type = 'checkbox'; vaultCheck.checked = !!p.private_vault_access; vaultCheck.disabled = !canEdit;
      vaultCheck.addEventListener('change', () => { p.private_vault_access = vaultCheck.checked; markDirty(); });
      vault.append(vaultCheck, node('span', '', 'Allow private vault reads (private notes via retrieval and file tools; the shell is set separately above)'));
      card.appendChild(vault);
      const advanced = node('details', 'agent-profile-capabilities');
      advanced.appendChild(node('summary', '', 'Capability allowlists'));
      const grid = node('div', 'agent-profile-cap-grid');
      grid.appendChild(field('Enabled tools', mk('textarea', 'enabled_tools', { rows: '2', placeholder: 'Used when Tools = Selected. Every tool the worker may call, MCP included: web_search, mcp__email__list_emails, mcp__github__* (whole server), mcp__* (all servers). Anything unlisted is denied, including tools added later, except the few that touch only its own state (discover_tools, update_plan, recall_tool_output, recall_chat_history).' })));
      grid.appendChild(field('Extra denied tools', mk('textarea', 'disabled_tools', { rows: '2', placeholder: 'Always denied, e.g. bash, send_email' })));
      grid.appendChild(field('Selected skills', mk('textarea', 'skill_names', { rows: '2', placeholder: 'Skill names; used when Skills = Selected' })));
      grid.appendChild(field('Selected MCP servers', mk('textarea', 'allowed_mcp_servers', { rows: '2', placeholder: 'Server IDs; used when MCP = Selected' })));
      grid.appendChild(field('Allowed models', mk('textarea', 'allowed_models', { rows: '2', placeholder: 'model or model@endpoint; used when Models = Selected' })));
      grid.appendChild(field('Model fallbacks', mk('textarea', 'model_fallbacks', { rows: '2', placeholder: 'Try in order if the primary model is unavailable' })));
      advanced.appendChild(grid);
      card.appendChild(advanced);
      item.appendChild(card);
      list.appendChild(item);
    });
  }

  function replaceProfiles(next) {
    const openNames = new Set(profiles.filter((p) => openProfiles.has(p)).map((p) => String(p.name || '').toLowerCase()));
    profiles = (next || []).map((p) => Object.assign({}, p));
    openProfiles = new WeakSet();
    profiles.forEach((p) => { if (openNames.has(String(p.name || '').toLowerCase())) openProfiles.add(p); });
    dirty = false;
    render();
    if (typeof onSaved === 'function') onSaved(profiles);
  }

  addBtn.addEventListener('click', () => {
    const fresh = blankLoadout();
    profiles.push(fresh);
    openProfiles.add(fresh);
    render();
    markDirty();
    const inputs = list.querySelectorAll('.agent-profile-item:last-child input');
    if (inputs[0]) inputs[0].focus();
  });

  saveBtn.addEventListener('click', async () => {
    say('Saving…');
    try {
      const r = await fetch('/api/auth/settings', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ agent_profiles: profiles }),
      });
      let body = null;
      try { body = await r.json(); } catch (_) {}
      if (!r.ok) {
        say((body && body.detail) || `Not saved (${r.status})`, true);
        return;
      }
      replaceProfiles((body && body.agent_profiles) || profiles);
      say('Saved');
    } catch (_) { say('Failed to save', true); }
  });

  function showReport(report) {
    reportBox.textContent = '';
    const lines = [];
    [['added', 'Added'], ['updated', 'Updated'], ['removed', 'Removed']].forEach(([key, label]) => {
      const names = report[key] || [];
      if (names.length) lines.push(`${label}: ${names.join(', ')}`);
    });
    Object.keys(report.narrowed || {}).forEach((name) => lines.push(`Narrowed ${name}: ${report.narrowed[name].join('; ')}`));
    (report.skipped || []).forEach((s) => lines.push(`Skipped ${s.name}: ${s.reason}`));
    (report.errors || []).forEach((e) => lines.push(`Error in ${e.name || `loadout ${e.index + 1}`}: ${e.error}`));
    (report.warnings || []).forEach((w) => lines.push(`Warning: ${w}`));
    lines.forEach((line) => reportBox.appendChild(node('div', '', line)));
    reportBox.hidden = !lines.length;
  }

  exportBtn.addEventListener('click', async () => {
    if (dirty) say('Exporting the saved loadouts (unsaved changes are not included)');
    try {
      const r = await fetch('/api/agents/profiles/export', { credentials: 'same-origin' });
      if (!r.ok) {
        let err = null;
        try { err = await r.json(); } catch (_) {}
        say((err && err.detail) || `Export failed (${r.status})`, true);
        return;
      }
      const blob = await r.blob();
      const match = (r.headers.get('Content-Disposition') || '').match(/filename="?([^";]+)"?/);
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = match ? match[1] : 'odysseus-agent-profiles.json';
      a.click();
      URL.revokeObjectURL(a.href);
      say('Export downloaded');
    } catch (_) { say('Export failed', true); }
  });

  importBtn.addEventListener('click', async () => {
    if (dirty && !(await ask('Importing reloads the loadouts from the server. Discard unsaved changes?', { confirmText: 'Discard' }))) return;
    fileInput.value = '';
    fileInput.click();
  });

  fileInput.addEventListener('change', async () => {
    const file = fileInput.files && fileInput.files[0];
    if (!file) return;
    const mode = modeSel.value || 'merge';
    if (mode === 'replace' &&
        !(await ask(`Replace all loadouts with the ones in ${file.name}? Loadouts not in the file are deleted.`,
          { title: 'Replace loadouts', confirmText: 'Replace', danger: true }))) return;
    let doc;
    try {
      doc = JSON.parse((await file.text()).replace(/^﻿/, ''));
    } catch (e) { say(`Not a JSON file: ${e.message}`, true); return; }
    say('Importing…');
    try {
      const r = await fetch('/api/agents/profiles/import', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ document: doc, mode: mode === 'rename' ? 'merge' : mode,
          rename_conflicts: mode === 'rename' }),
      });
      let body = null;
      try { body = await r.json(); } catch (_) {}
      if (!r.ok || !body) {
        showReport({});
        say((body && body.detail) || `Import failed (${r.status})`, true);
        return;
      }
      if (Array.isArray(body.profiles)) replaceProfiles(body.profiles);
      showReport(body.report || {});
      say(body.message || 'Imported', !body.ok);
    } catch (_) { say('Import failed', true); }
  });

  render();
  if (!canEdit) say('Only an admin can change loadouts.');
  return { isDirty: () => dirty };
}

export default { mountLoadoutsEditor };
