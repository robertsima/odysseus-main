/* capabilitiesPanel.js — Settings › Capabilities.
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
 */

const API = '';
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

const state = { capabilities: [], groups: [], isAdmin: false, loaded: false, busy: false, dirty: new Map() };

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
function controlHtml(s) {
  const id = `set-${s.key}`;
  const locked = s.locked ? ' disabled' : '';
  const lockNote = s.locked
    ? '<span class="set-locked">Pinned by this deployment’s environment</span>' : '';
  let input;
  if (s.type === 'bool') {
    input = `<label class="set-switch"><input type="checkbox" id="${id}" data-set-key="${esc(s.key)}"
      ${s.value ? 'checked' : ''}${locked}><span></span></label>`;
  } else if (s.type === 'choice') {
    const opts = (s.choices || []).map((c) =>
      `<option value="${esc(c)}"${String(s.value) === c ? ' selected' : ''}>${esc(c)}</option>`).join('');
    input = `<select id="${id}" class="set-input" data-set-key="${esc(s.key)}"${locked}>${opts}</select>`;
  } else if (s.type === 'json' || s.type === 'text' || s.type === 'list') {
    const text = s.type === 'json' ? JSON.stringify(s.value ?? null, null, 2)
      : Array.isArray(s.value) ? s.value.join('\n') : (s.value ?? '');
    input = `<textarea id="${id}" class="set-input set-textarea" rows="4"
      data-set-key="${esc(s.key)}"${locked}>${esc(text)}</textarea>`;
  } else if (s.type === 'int' || s.type === 'float') {
    input = `<input type="number" id="${id}" class="set-input set-input-num" data-set-key="${esc(s.key)}"
      value="${esc(s.value ?? '')}"${s.type === 'int' ? ' step="1"' : ''}${locked}>`;
  } else {
    const kind = s.type === 'secret' ? 'password' : 'text';
    input = `<input type="${kind}" id="${id}" class="set-input" data-set-key="${esc(s.key)}"
      value="${esc(s.value ?? '')}" placeholder="${esc(s.placeholder || '')}"${locked}
      ${s.type === 'secret' ? 'autocomplete="off"' : ''}>`;
  }
  return `
    <div class="set-row${s.locked ? ' set-row-locked' : ''}">
      <div class="set-label"><label for="${id}">${esc(s.label)}</label>${lockNote}</div>
      ${s.help ? `<p class="set-help">${esc(s.help)}</p>` : ''}
      <div class="set-control">${input}</div>
    </div>`;
}

function render() {
  const host = $('capabilities-panel-body');
  if (!host) return;
  if (!state.loaded) { host.innerHTML = '<p class="set-help">Loading…</p>'; return; }

  const caps = state.capabilities.length
    ? state.capabilities.map(capabilityHtml).join('')
    : '<p class="set-help">No capabilities are registered in this build.</p>';

  const groups = state.groups.map((g) => `
    <section class="set-group">
      <h4 class="set-group-title">${esc(g.group)}</h4>
      ${g.settings.map(controlHtml).join('')}
    </section>`).join('');

  host.innerHTML = `
    <div class="cap-intro">
      <h3 class="set-section-title">Capabilities</h3>
      <p class="set-help">What this install can do. A capability needs both your
        switch and its requirements on this machine — until both hold, its tools
        are kept out of the model’s reach rather than failing when called.</p>
      <button type="button" class="btn-secondary cap-recheck" data-cap-action="recheck">Re-check requirements</button>
    </div>
    <div class="cap-list">${caps}</div>
    <div class="set-schema">
      <h3 class="set-section-title">Configuration</h3>
      <p class="set-help">Choices live here. Values your deployment pins through
        the environment show as locked, because a click on them would not stick.</p>
      ${groups}
      <div class="set-actions">
        <button type="button" class="btn-primary" data-cap-action="save"
          ${state.isAdmin ? '' : 'disabled'}>Save changes</button>
        <span class="set-status" id="capabilities-save-status" role="status"></span>
      </div>
    </div>`;
}

function markDirty(key, value) { state.dirty.set(key, value); }

function readControl(el) {
  if (el.type === 'checkbox') return el.checked;
  return el.value;
}

async function load() {
  try {
    const [caps, schema] = await Promise.all([
      api('/api/capabilities'),
      api('/api/settings/schema'),
    ]);
    state.capabilities = caps.capabilities || [];
    state.groups = schema.groups || [];
    state.isAdmin = !!schema.is_admin;
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
      const host = $('capabilities-panel-body');
      if (host) host.innerHTML = `<p class="set-help">Could not load configuration: ${esc(e.message)}</p>`;
      return;
    }
  }
  state.loaded = true;
  state.dirty.clear();
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

function onEvent(e) {
  const btn = e.target.closest?.('[data-cap-action]');
  if (btn) {
    const act = btn.dataset.capAction;
    if (act === 'save') { save(); return; }
    if (act === 'recheck') {
      post('/api/capabilities/recheck')
        .then((out) => { state.capabilities = out.capabilities || []; render(); toast('Requirements re-checked'); })
        .catch((err) => toast(err.message || 'Re-check failed', 'error'));
      return;
    }
  }
  const toggle = e.target.closest?.('[data-cap-toggle]');
  if (toggle) { toggleCapability(toggle.dataset.capToggle, toggle.checked); return; }
  const setting = e.target.closest?.('[data-set-key]');
  if (setting) markDirty(setting.dataset.setKey, readControl(setting));
}

export function mount() {
  const host = $('capabilities-panel-body');
  if (!host || host.dataset.wired) return;
  host.dataset.wired = '1';
  host.addEventListener('click', onEvent);
  host.addEventListener('change', onEvent);
  host.addEventListener('input', (e) => {
    const setting = e.target.closest?.('[data-set-key]');
    if (setting && setting.type !== 'checkbox') markDirty(setting.dataset.setKey, readControl(setting));
  });
  load();
}

// The settings modal builds its panels lazily, so mount when this tab is opened
// as well as on first load — whichever happens first.
document.addEventListener('click', (e) => {
  const tab = e.target.closest?.('[data-settings-tab="capabilities"]');
  if (tab) setTimeout(mount, 0);
});
document.addEventListener('DOMContentLoaded', () => {
  if ($('capabilities-panel-body')) mount();
});

export default { mount, reload: load };
