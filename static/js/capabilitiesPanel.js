/* capabilitiesPanel.js — Settings › Advanced, plus the capability list on
 * Settings › Agents.
 *
 * The half of in-app configuration that an API alone does not deliver: a place
 * to see what this install can do, what it cannot do yet, and why.
 *
 * The distinction this page exists to make visible is between a capability the
 * operator switched off and one the host cannot satisfy. Those look identical
 * in a plain on/off list, and they have completely different fixes — flip a
 * switch versus install a dependency. So an unsatisfied capability shows its
 * missing requirement and the hint that says what to do, and its switch stays
 * usable: turning it on before the dependency exists is legitimate, and the
 * tool stays out of the model's schema until the requirement is met.
 *
 * Settings are rendered from the declared schema (/api/settings/schema) rather
 * than hand-built here, so a newly declared setting gets a control without this
 * file changing. That drift is why 17 of 83 settings keys previously had none.
 *
 * The opposite drift matters too: a setting that already has a hand-built
 * control on another tab must not get a second one here (two controls for one
 * value, one of them stale after the other saves). Every dedicated control in
 * the Settings modal carries data-setting-key="<key> [<key>…]", and this panel
 * skips every spec named by one — so the Advanced tab is exactly "everything
 * without a better home".
 */

const API = '';
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

// Main host (Advanced tab) and the capability list's host (Agents tab). When
// the Agents host is absent the list renders at the top of Advanced instead.
const SCHEMA_HOST = 'capabilities-panel-body';
const STATUS_HOST = 'capabilities-status-body';

// Keys the server still declares but that no longer do anything. The backend
// is removing them; until it does they must not look like working controls.
const RETIRED_KEYS = new Set(['agent_max_rounds']);

const state = {
  capabilities: [], groups: [], isAdmin: false, loaded: false, busy: false,
  dirty: new Map(), activeGroup: '', query: '', showAdvanced: false, capabilitiesOpen: false,
  endpoints: [], models: [],
};

async function api(path, opts = {}) {
  const res = await fetch(`${API}${path}`, Object.assign({ credentials: 'same-origin' }, opts));
  let body = null;
  try { body = await res.json(); } catch (_) {}
  if (!res.ok) {
    const err = new Error((body && (body.detail || body.error)) || `HTTP ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return body;
}
const post = (path, data) => api(path, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(data || {}),
});

function toast(msg, kind) {
  try {
    if (window.uiModule?.showToast) window.uiModule.showToast(msg, kind);
    else if (kind === 'error') console.error(msg);
  } catch (_) {}
}

// ── keys owned by a dedicated control elsewhere in Settings ───────────────
function dedicatedKeys() {
  const keys = new Set(RETIRED_KEYS);
  const modal = document.getElementById('settings-modal') || document;
  modal.querySelectorAll('[data-setting-key]').forEach((node) => {
    if (node.closest(`#${SCHEMA_HOST}, #${STATUS_HOST}`)) return;
    String(node.getAttribute('data-setting-key') || '').split(/\s+/).forEach((key) => {
      if (key) keys.add(key);
    });
  });
  return keys;
}

// The settings of one group this panel should offer, before the advanced /
// search filters apply.
function ownSettings(group, skip) {
  return (group.settings || []).filter((s) => !skip.has(s.key));
}

// ── capability cards ──────────────────────────────────────────────────────
// Not a card per row: a capability that needs attention gets a warning rail so
// "this cannot work yet" reads at a glance, and the rest stay quiet.
function capabilityHtml(cap) {
  const blocked = cap.enabled && !cap.satisfied;
  const cls = blocked ? ' cap-blocked' : (cap.available ? ' cap-on' : '');
  const stateLabel = cap.available ? 'Active'
    : blocked ? 'Needs setup'
    : cap.satisfied ? 'Off'
    : 'Off · unavailable here';
  const unmet = (cap.unmet || []).map((u) => `
    <li class="cap-unmet-item">
      <span class="cap-unmet-req">${esc(u.requirement)}</span>
      <span class="cap-unmet-detail">${esc(u.detail)}</span>
      ${u.hint ? `<span class="cap-unmet-hint">${esc(u.hint)}</span>` : ''}
    </li>`).join('');
  const tools = (cap.tools || []).length
    ? `<p class="cap-tools">Tools: <code>${cap.tools.map(esc).join('</code> <code>')}</code>${
        cap.available ? '' : ' <em>— withheld from the model while unavailable</em>'}</p>`
    : '';
  return `
    <div class="cap-row${cls}" data-cap="${esc(cap.name)}">
      <div class="cap-main">
        <div class="cap-head">
          <span class="cap-title">${esc(cap.title)}</span>
          <span class="cap-state">${esc(stateLabel)}</span>
        </div>
        <p class="cap-summary">${esc(cap.summary)}</p>
        ${unmet ? `<ul class="cap-unmet">${unmet}</ul>` : ''}
        ${tools}
      </div>
      <label class="cap-switch">
        <input type="checkbox" data-cap-toggle="${esc(cap.name)}" ${cap.enabled ? 'checked' : ''}
               ${state.isAdmin ? '' : 'disabled'} aria-label="Enable ${esc(cap.title)}">
        <span></span>
      </label>
    </div>`;
}

// ── schema-driven settings controls ───────────────────────────────────────
function currentValue(s) {
  return state.dirty.has(s.key) ? state.dirty.get(s.key) : s.value;
}

function choiceLabel(s, value) {
  const i = (s.choices || []).findIndex((choice) => String(choice) === String(value));
  if (i >= 0 && (s.choice_labels || [])[i]) return s.choice_labels[i];
  if (value === '') return 'Use the app default';
  return String(value || '').replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
}

function displayNumber(s, raw) {
  const num = Number(raw);
  if (!Number.isFinite(num)) return raw ?? '';
  return num / (Number(s.scale) || 1);
}

function defaultLabel(s) {
  const value = s.default;
  if (s.type === 'bool') return value ? 'On' : 'Off';
  if (s.type === 'choice') return choiceLabel(s, value);
  if (s.type === 'int' || s.type === 'float') {
    const shown = displayNumber(s, value);
    return `${shown}${s.unit ? ` ${s.unit}` : ''}`;
  }
  if (Array.isArray(value)) return value.length ? `${value.length} entries` : 'Empty';
  if (value && typeof value === 'object') return Object.keys(value).length ? 'Configured' : 'Empty';
  return value === '' || value == null ? 'Empty' : String(value);
}

// A list whose items are objects (e.g. [{endpoint_id, model}]) has no
// one-entry-per-line text form: joining it printed "[object Object]" and
// saving that text replaced the list with those strings.
function listHasObjects(value) {
  return Array.isArray(value) && value.some((item) => item !== null && typeof item === 'object');
}

function sourcedOptions(s, value) {
  const endpoints = state.endpoints.filter((endpoint) => endpoint.is_enabled !== false);
  const allModels = state.models || [];
  let options = [];
  if (s.options_source === 'endpoints') {
    options = endpoints.map((endpoint) => ({ value: String(endpoint.id), label: `${endpoint.name}${endpoint.online === false ? ' (offline)' : ''}` }));
  } else if (s.options_source === 'models') {
    options = allModels.map((model) => ({ value: model, label: model }));
  } else if (s.options_source === 'tts_providers') {
    options = [{ value: 'disabled', label: 'Disabled' }, { value: 'browser', label: 'Browser / operating system' }, { value: 'local', label: 'Local speech service' }]
      .concat(endpoints.filter((endpoint) => (endpoint.models || []).some((model) => /tts|audio|speech/i.test(model)))
        .map((endpoint) => ({ value: `endpoint:${endpoint.id}`, label: `${endpoint.name} (API endpoint)` })));
  } else if (s.options_source === 'stt_providers') {
    options = [{ value: 'disabled', label: 'Disabled' }, { value: 'local', label: 'Local Whisper' }]
      .concat(endpoints.map((endpoint) => ({ value: `endpoint:${endpoint.id}`, label: `${endpoint.name} (API endpoint)` })));
  } else if (s.options_source === 'tts_models') {
    options = ['tts-1', 'tts-1-hd', 'gpt-4o-mini-tts']
      .concat(allModels.filter((model) => /tts|audio|speech/i.test(model)))
      .map((model) => ({ value: model, label: model }));
  } else if (s.options_source === 'stt_models') {
    options = ['tiny', 'base', 'small', 'medium', 'large-v3']
      .concat(allModels.filter((model) => /whisper|transcri|audio/i.test(model)))
      .map((model) => ({ value: model, label: model }));
  }
  const seen = new Set();
  options = options.filter((item) => item.value && !seen.has(item.value) && seen.add(item.value));
  const current = String(value ?? '');
  if (current && !options.some((item) => item.value === current)) {
    options.unshift({ value: current, label: `${current} (saved; not currently detected)` });
  }
  return options;
}

function controlHtml(s) {
  const id = `set-${s.key}`;
  const value = currentValue(s);
  const readOnly = s.type === 'list' && listHasObjects(value);
  const locked = s.locked || readOnly ? ' disabled' : '';
  const lockNote = s.locked
    ? '<span class="set-locked">Pinned by this deployment’s environment</span>'
    : readOnly ? '<span class="set-locked">Read-only here: its entries are structured</span>' : '';
  let input;
  if (readOnly) {
    // Shown for reference only; there is no text form that round-trips.
    input = `<pre id="${id}" class="set-input set-textarea set-readonly" aria-readonly="true">${esc(JSON.stringify(value, null, 2))}</pre>`;
  } else if (s.type === 'bool') {
    input = `<label class="set-switch"><input type="checkbox" id="${id}" data-set-key="${esc(s.key)}"
      ${value ? 'checked' : ''}${locked}><span></span></label>`;
  } else if (s.options_source) {
    const options = sourcedOptions(s, value);
    const blank = `<option value=""${value ? '' : ' selected'}>Use the app default</option>`;
    input = `<select id="${id}" class="set-input" data-set-key="${esc(s.key)}"${locked}>${blank}${options.map((item) => `<option value="${esc(item.value)}"${String(value) === item.value ? ' selected' : ''}>${esc(item.label)}</option>`).join('')}</select>`;
  } else if (s.type === 'choice') {
    const opts = (s.choices || []).map((c, i) => {
      const label = (s.choice_labels || [])[i] || choiceLabel(s, c);
      return `<option value="${esc(c)}"${String(value) === String(c) ? ' selected' : ''}>${esc(label)}</option>`;
    }).join('');
    input = `<select id="${id}" class="set-input" data-set-key="${esc(s.key)}"${locked}>${opts}</select>`;
  } else if ((s.suggestions || []).length) {
    input = `<input type="text" id="${id}" class="set-input" data-set-key="${esc(s.key)}"
      value="${esc(value ?? '')}" list="${id}-suggestions" placeholder="${esc(s.placeholder || 'Choose or type a value')}"${locked}>
      <datalist id="${id}-suggestions">${s.suggestions.map((suggestion) => `<option value="${esc(suggestion)}"></option>`).join('')}</datalist>`;
  } else if (s.type === 'json' || s.type === 'text' || s.type === 'list') {
    const text = s.type === 'json' ? JSON.stringify(value ?? null, null, 2)
      : Array.isArray(value) ? value.join('\n') : (value ?? '');
    input = `<textarea id="${id}" class="set-input set-textarea" rows="4"
      data-set-key="${esc(s.key)}" data-set-type="${esc(s.type)}"${locked}>${esc(text)}</textarea>`;
  } else if (s.type === 'int' || s.type === 'float') {
    const scale = Number(s.scale) || 1;
    const min = s.min == null || (scale > 1 && Number(s.min) < scale) ? '' : ` min="${esc(Number(s.min) / scale)}"`;
    const max = s.max == null ? '' : ` max="${esc(Number(s.max) / scale)}"`;
    const step = s.step != null ? Number(s.step) / scale : (scale > 1 || s.type === 'int' ? 1 : 'any');
    input = `<div class="set-number-wrap"><input type="number" id="${id}" class="set-input set-input-num"
      data-set-key="${esc(s.key)}" data-set-type="${esc(s.type)}" data-set-scale="${esc(scale)}"
      value="${esc(displayNumber(s, value))}" step="${esc(step)}"${min}${max}${locked}>
      ${s.unit ? `<span class="set-unit">${esc(s.unit)}</span>` : ''}</div>`;
  } else {
    const kind = s.type === 'secret' ? 'password' : 'text';
    input = `<input type="${kind}" id="${id}" class="set-input" data-set-key="${esc(s.key)}"
      value="${esc(value ?? '')}" placeholder="${esc(s.placeholder || '')}"${locked}
      ${s.type === 'secret' ? 'autocomplete="off"' : ''}>`;
  }
  return `
    <div class="set-row${s.locked || readOnly ? ' set-row-locked' : ''}" data-setting-row="${esc(s.key)}">
      <div class="set-label"><label for="${id}">${esc(s.label)}</label>
        ${s.advanced ? '<span class="set-advanced-badge">Advanced</span>' : ''}${lockNote}</div>
      ${s.help ? `<p class="set-help">${esc(s.help)}</p>` : ''}
      <div class="set-control">${input}
        ${readOnly ? '' : `<button type="button" class="set-reset" data-reset-setting="${esc(s.key)}"${locked}
          title="Restore the built-in default">Reset</button>`}
      </div>
      <span class="set-default">Default: ${esc(defaultLabel(s))}</span>
    </div>`;
}

function settingsForView(skip) {
  const query = state.query.trim().toLowerCase();
  return state.groups.map((group) => {
    const settings = ownSettings(group, skip).filter((s) => {
      if (s.advanced && !state.showAdvanced) return false;
      if (!query) return group.group === state.activeGroup;
      return `${s.label} ${s.key} ${s.help || ''} ${group.group}`.toLowerCase().includes(query);
    });
    return Object.assign({}, group, { settings });
  }).filter((group) => group.settings.length);
}

function capabilitiesHtml() {
  const caps = state.capabilities.length
    ? state.capabilities.map(capabilityHtml).join('')
    : '<p class="set-help">No capabilities are registered in this build.</p>';
  const activeCaps = state.capabilities.filter((cap) => cap.available).length;
  const attentionCaps = state.capabilities.filter((cap) => cap.enabled && !cap.satisfied).length;
  return `
    <details class="cap-section"${state.capabilitiesOpen ? ' open' : ''}>
      <summary><span>Capabilities</span><span class="cap-summary-count">${activeCaps} active${attentionCaps ? ` · ${attentionCaps} need setup` : ''}</span></summary>
      <div class="cap-intro">
        <p class="set-help">A capability needs both your switch and its requirements on this machine.
          Unavailable tools stay out of the model’s reach instead of failing when called.</p>
        <button type="button" class="btn-secondary cap-recheck" data-cap-action="recheck">Re-check requirements</button>
      </div>
      <div class="cap-list">${caps}</div>
    </details>`;
}

function render() {
  const host = $(SCHEMA_HOST);
  const statusHost = $(STATUS_HOST);
  if (!host && !statusHost) return;
  if (!state.loaded) {
    if (host) host.innerHTML = '<p class="set-help">Loading…</p>';
    if (statusHost) statusHost.innerHTML = '';
    return;
  }

  // Capability status lives on the Agents tab; without that host it falls
  // back to the top of this one. A non-admin sees no capabilities at all.
  const showCaps = state.isAdmin || state.capabilities.length;
  if (statusHost) statusHost.innerHTML = showCaps ? capabilitiesHtml() : '';
  if (!host) return;

  const skip = dedicatedKeys();
  const availableGroups = state.groups.filter((g) =>
    ownSettings(g, skip).some((s) => state.showAdvanced || !s.advanced));
  if (!availableGroups.some((g) => g.group === state.activeGroup)) {
    state.activeGroup = (availableGroups[0] || {}).group || '';
  }
  const groupOptions = availableGroups.map((g) => {
    const count = ownSettings(g, skip).filter((s) => state.showAdvanced || !s.advanced).length;
    return `<option value="${esc(g.group)}"${g.group === state.activeGroup ? ' selected' : ''}>${esc(g.group)} (${count})</option>`;
  }).join('');
  const groups = settingsForView(skip).map((g) => `
    <section class="set-group">
      <h4 class="set-group-title">${esc(g.group)}</h4>
      ${g.help ? `<p class="set-group-help">${esc(g.help)}</p>` : ''}
      ${g.settings.map(controlHtml).join('')}
    </section>`).join('');

  const advancedCount = state.groups.reduce((n, g) => n + ownSettings(g, skip).filter((s) => s.advanced).length, 0);

  host.innerHTML = `
    ${statusHost ? '' : (showCaps ? capabilitiesHtml() : '')}
    <div class="set-schema">
      <h3 class="set-section-title">Advanced settings</h3>
      <p class="set-help">Everything without its own control on another tab. Choose a category or
        search by name; advanced controls have safe defaults and can stay untouched on most installations.</p>
      <div class="set-toolbar">
        <label class="set-toolbar-field"><span>Category</span>
          <select id="settings-group-filter" class="set-input">${groupOptions}</select></label>
        <label class="set-toolbar-field set-search-field"><span>Find a setting</span>
          <input id="settings-schema-search" class="set-input" type="search" value="${esc(state.query)}"
            placeholder="Search labels and descriptions…"></label>
        <button type="button" class="btn-secondary set-advanced-toggle" data-cap-action="advanced"
          aria-pressed="${state.showAdvanced}">${state.showAdvanced ? 'Hide' : 'Show'} advanced (${advancedCount})</button>
      </div>
      <div class="set-groups">${groups || '<p class="set-empty">No settings match this search.</p>'}</div>
      <div class="set-actions">
        <button type="button" class="btn-primary" data-cap-action="save"
          ${state.isAdmin && state.dirty.size ? '' : 'disabled'}>Save ${state.dirty.size ? `${state.dirty.size} change${state.dirty.size === 1 ? '' : 's'}` : 'changes'}</button>
        <span class="set-status" id="capabilities-save-status" role="status"></span>
      </div>
    </div>`;
}

// Compare what a control holds with the saved value in the value's own shape.
// A JSON textarea reads as text; comparing that string with the stored object
// marked the setting changed the moment the field was focused.
function sameValue(setting, value) {
  return JSON.stringify(value) === JSON.stringify(setting.value);
}

function markDirty(key, value) {
  const setting = findSetting(key);
  if (setting && sameValue(setting, value)) state.dirty.delete(key);
  else state.dirty.set(key, value);
  const saveButton = document.querySelector('[data-cap-action="save"]');
  if (saveButton && state.isAdmin) {
    const count = state.dirty.size;
    saveButton.disabled = count === 0;
    saveButton.textContent = count
      ? `Save ${count} change${count === 1 ? '' : 's'}`
      : 'Save changes';
  }
}

function readControl(el) {
  if (el.type === 'checkbox') return el.checked;
  const kind = el.dataset.setType;
  const scale = Number(el.dataset.setScale || 1);
  if (kind === 'int' || kind === 'float') {
    const value = Number(el.value) * scale;
    if (!Number.isFinite(value)) return el.value;
    return kind === 'int' ? Math.round(value) : value;
  }
  if (kind === 'json') {
    // Text that does not parse yet stays text: it is a real, unsaved edit,
    // and the server reports what is wrong with it on save.
    try { return JSON.parse(el.value || 'null'); } catch (_) { return el.value; }
  }
  if (kind === 'list') {
    return String(el.value || '').split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  }
  return el.value;
}

function findSetting(key) {
  for (const group of state.groups) {
    const found = (group.settings || []).find((s) => s.key === key);
    if (found) return found;
  }
  return null;
}

async function load() {
  try {
    const [caps, schema, endpoints] = await Promise.all([
      api('/api/capabilities'),
      api('/api/settings/schema'),
      api('/api/model-endpoints').catch(() => []),
    ]);
    state.capabilities = caps.capabilities || [];
    state.groups = schema.groups || [];
    state.isAdmin = !!schema.is_admin;
    state.endpoints = Array.isArray(endpoints) ? endpoints : [];
    state.models = [...new Set(state.endpoints.flatMap((endpoint) => endpoint.models || []).map(String))].sort((a, b) => a.localeCompare(b));
  } catch (e) {
    if (e.status === 403) {
      // A non-admin can still see their own settings; capability state is not
      // theirs to read, so show the half they are entitled to.
      try {
        const schema = await api('/api/settings/schema');
        state.groups = schema.groups || [];
        state.isAdmin = false;
        state.capabilities = [];
      } catch (_) { /* fall through to the error below */ }
    } else {
      const host = $(SCHEMA_HOST);
      if (host) host.innerHTML = `<p class="set-help">Could not load configuration: ${esc(e.message)}</p>`;
      return;
    }
  }
  state.loaded = true;
  state.dirty.clear();
  if (!state.activeGroup || !state.groups.some((g) => g.group === state.activeGroup)) {
    try { state.activeGroup = localStorage.getItem('odysseus-settings-group') || ''; } catch (_) {}
  }
  if (!state.groups.some((g) => g.group === state.activeGroup)) {
    state.activeGroup = (state.groups.find((g) => g.group === 'Agents') || state.groups[0] || {}).group || '';
  }
  render();
}

async function save() {
  if (state.busy) return;
  const status = $('capabilities-save-status');
  const payload = {};
  state.dirty.forEach((v, k) => { payload[k] = v; });
  if (!Object.keys(payload).length) { if (status) status.textContent = 'Nothing changed.'; return; }
  state.busy = true;
  if (status) status.textContent = 'Saving…';
  try {
    const out = await post('/api/settings/schema', { settings: payload });
    const saved = (out.saved || []).length;
    const locked = out.locked_by_environment || [];
    // Report what actually happened: silently dropping a pinned value is how an
    // operator comes to believe a setting took effect when it did not.
    let msg = saved ? `Saved ${saved} setting${saved === 1 ? '' : 's'}.` : 'No changes saved.';
    if (locked.length) msg += ` ${locked.length} pinned by the environment and left unchanged.`;
    if (status) status.textContent = msg;
    state.dirty.clear();
    await load();
  } catch (e) {
    if (status) status.textContent = '';
    toast(e.message || 'Could not save settings', 'error');
  } finally {
    state.busy = false;
  }
}

async function toggleCapability(name, enabled) {
  try {
    const out = await post(`/api/capabilities/${encodeURIComponent(name)}`, { enabled });
    const cap = out.capability;
    if (cap) {
      const i = state.capabilities.findIndex((c) => c.name === cap.name);
      if (i >= 0) state.capabilities[i] = cap; else state.capabilities.push(cap);
      render();
      if (cap.enabled && !cap.satisfied) {
        const first = (cap.unmet || [])[0] || {};
        toast(`${cap.title} is on, but not usable yet: ${first.hint || first.detail || 'a requirement is missing'}`, 'warning');
      }
    }
  } catch (e) {
    toast(e.message || 'Could not change that capability', 'error');
    await load();
  }
}

// Buttons act on click. Form controls act on change/input only: a click on a
// checkbox is followed by its change event, and handling both posted each
// capability toggle twice; a click into a textarea is not an edit at all.
function onClick(e) {
  const btn = e.target.closest?.('[data-cap-action]');
  if (btn) {
    const act = btn.dataset.capAction;
    if (act === 'save') { save(); return; }
    if (act === 'advanced') {
      state.showAdvanced = !state.showAdvanced;
      if (state.showAdvanced) {
        const skip = dedicatedKeys();
        const current = state.groups.find((group) => group.group === state.activeGroup);
        if (!current || !ownSettings(current, skip).some((setting) => setting.advanced)) {
          state.activeGroup = (state.groups.find((group) => ownSettings(group, skip).some((setting) => setting.advanced)) || current || {}).group || state.activeGroup;
        }
      }
      render();
      toast(state.showAdvanced ? 'Advanced settings are now visible' : 'Advanced settings hidden');
      return;
    }
    if (act === 'recheck') {
      post('/api/capabilities/recheck')
        .then((out) => { state.capabilities = out.capabilities || []; render(); toast('Requirements re-checked'); })
        .catch((err) => toast(err.message || 'Re-check failed', 'error'));
      return;
    }
  }
  const reset = e.target.closest?.('[data-reset-setting]');
  if (reset) {
    const setting = findSetting(reset.dataset.resetSetting);
    if (setting && !setting.locked) {
      markDirty(setting.key, setting.default);
      render();
    }
  }
}

function onChange(e) {
  if (e.target.id === 'settings-group-filter') {
    state.activeGroup = e.target.value;
    state.query = '';
    try { localStorage.setItem('odysseus-settings-group', state.activeGroup); } catch (_) {}
    render();
    return;
  }
  const toggle = e.target.closest?.('[data-cap-toggle]');
  if (toggle) { toggleCapability(toggle.dataset.capToggle, toggle.checked); return; }
  const setting = e.target.closest?.('[data-set-key]');
  if (setting) markDirty(setting.dataset.setKey, readControl(setting));
}

function onInput(e) {
  if (e.target.id === 'settings-schema-search') {
    state.query = e.target.value;
    render();
    const search = $('settings-schema-search');
    if (search) { search.focus(); search.setSelectionRange(search.value.length, search.value.length); }
    return;
  }
  const setting = e.target.closest?.('[data-set-key]');
  if (setting && setting.type !== 'checkbox') markDirty(setting.dataset.setKey, readControl(setting));
}

function wire(host) {
  if (!host || host.dataset.wired) return;
  host.dataset.wired = '1';
  host.addEventListener('click', onClick);
  host.addEventListener('change', onChange);
  host.addEventListener('input', onInput);
  host.addEventListener('toggle', (e) => {
    if (e.target.matches?.('.cap-section')) state.capabilitiesOpen = e.target.open;
  }, true);
}

let _loading = null;
export function mount(tab) {
  wire($(SCHEMA_HOST));
  wire($(STATUS_HOST));
  // Re-read the schema each time Advanced is shown (another tab, a second
  // browser or the manage_settings tool may have changed a value), unless an
  // unsaved edit here would be thrown away.
  const refresh = !state.loaded || (tab === 'capabilities' && !state.dirty.size);
  if (!refresh) { render(); return; }
  if (!_loading) _loading = load().finally(() => { _loading = null; });
}

// Jump to one category, e.g. a "folder privacy is under Advanced › Knowledge"
// link: <a data-go-settings-tab="capabilities" data-cap-group="Knowledge">.
document.addEventListener('click', (e) => {
  const link = e.target.closest?.('[data-cap-group]');
  if (!link) return;
  state.activeGroup = link.getAttribute('data-cap-group') || state.activeGroup;
  state.query = '';
  if (state.loaded) render();
});

// Load when one of the two tabs that show this panel is shown, however it was
// opened (nav click, search, open('advanced')): not at page load, where it
// fetched capabilities, the schema and every endpoint for a hidden panel.
document.addEventListener('settings:panel-activated', (e) => {
  const tab = e.detail && e.detail.tab;
  if (tab === 'capabilities' || tab === 'agents') mount(tab);
});

export default { mount, reload: load };
