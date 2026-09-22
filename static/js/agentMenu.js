/* agentMenu.js — the composer's Agents menu, shown in place of Prompt in Agent mode.
 *
 * In Chat mode the overflow (chevron) menu offers "Prompt", which opens the
 * prompt/persona window. In Agent mode that entry becomes "Agents", which opens
 * this menu:
 *   · the configured loadouts (Settings › Workbench › Agent profiles). Picking
 *     one runs the open chat under it (POST /api/agents/sessions/{id}/loadout);
 *     picking Default clears it. A chat that does not exist yet (nothing sent)
 *     gets the loadout the moment it is created, before its first turn runs.
 *   · Open the Agents panel (the Agent Control Room)
 *   · Prompts & personas — the same window the Chat-mode Prompt entry opens
 *   · Manage loadouts — Settings › Workbench › Agent profiles
 *
 * Which entry shows is a body class that follows the mode toggle
 * (`composer-agent-mode`), so it never fights the per-button display:none the
 * UI-visibility settings write.
 */

import uiModule from './ui.js';
import presetsModule from './presets.js';

const state = {
  mode: 'chat',
  profiles: null,      // last /api/agents/profiles result
  current: null,       // loadout the open chat runs under
  pending: null,       // { name } chosen before the chat existed
  pendingFor: null,    // pending chat object it was chosen for
};

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const sessionId = () => window.sessionModule?.getCurrentSessionId?.() || null;

async function api(path, opts = {}) {
  const res = await fetch(path, Object.assign({ credentials: 'same-origin' }, opts));
  let body = null;
  try { body = await res.json(); } catch (_) {}
  if (!res.ok) throw new Error((body && (body.detail || body.error)) || `${res.status}`);
  return body;
}

// ── mode ──────────────────────────────────────────────────────────────────
export function applyMode(mode) {
  state.mode = mode === 'agent' ? 'agent' : 'chat';
  document.body.classList.toggle('composer-agent-mode', state.mode === 'agent');
  if (state.mode !== 'agent') closeMenu();
}

// ── loadout state ─────────────────────────────────────────────────────────
/** Drop a pre-chat pick once the user has moved to another chat. */
function prunePending() {
  if (!state.pending) return;
  const pendingChat = window.sessionModule?.getPendingChat?.() || null;
  if (sessionId() || (state.pendingFor && pendingChat !== state.pendingFor)) {
    state.pending = null;
    state.pendingFor = null;
  }
}

async function loadCurrent() {
  prunePending();
  const sid = sessionId();
  if (!sid) { state.current = state.pending?.name || null; return; }
  try {
    const data = await api(`/api/session/${encodeURIComponent(sid)}/settings`);
    state.current = data?.settings?.agent_profile || null;
  } catch (_) {
    state.current = null;
  }
}

async function loadProfiles() {
  try {
    const data = await api('/api/agents/profiles');
    state.profiles = data?.profiles || [];
  } catch (_) {
    state.profiles = state.profiles || [];
  }
}

function syncButton() {
  const label = $('overflow-agents-current');
  if (label) label.textContent = state.current || '';
  // Not `.active`: the chevron's dot counts active items even while this one
  // is hidden in Chat mode.
  $('overflow-agents-btn')?.classList.toggle('has-loadout', !!state.current);
}

async function choose(name) {
  const sid = sessionId();
  if (!sid) {
    // No chat yet: remember it and apply it when the first send creates one.
    state.pending = name ? { name } : null;
    state.pendingFor = name ? (window.sessionModule?.getPendingChat?.() || null) : null;
    state.current = name || null;
    syncButton();
    uiModule.showToast(name ? `${name} will run this chat once you send` : 'This chat will use the default setup');
    return;
  }
  try {
    const result = await api(`/api/agents/sessions/${encodeURIComponent(sid)}/loadout`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ profile: name || null }),
    });
    state.current = result?.agent_profile || null;
    syncButton();
    try { window.chatSettingsModule?.refresh?.(); } catch (_) {}
    uiModule.showToast(name ? `This chat now runs as ${name}` : 'Loadout cleared — default setup', 'success');
  } catch (err) {
    uiModule.showToast(`Could not apply loadout: ${err.message}`, 'error');
  }
}

/** Called by sessions.js right after a new chat is created, before its first
 *  turn is sent, so a loadout picked in an empty chat covers that turn. */
export async function applyPendingLoadout(newSessionId) {
  const pending = state.pending;
  state.pending = null;
  state.pendingFor = null;
  if (!pending?.name || !newSessionId) return;
  try {
    await api(`/api/agents/sessions/${encodeURIComponent(newSessionId)}/loadout`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ profile: pending.name }),
    });
    state.current = pending.name;
  } catch (err) {
    state.current = null;
    uiModule.showToast(`${pending.name} was not applied: ${err.message}`, 'error');
  }
  syncButton();
}

// ── menu ──────────────────────────────────────────────────────────────────
const ICONS = {
  panel: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><path d="M17.5 14v7M14 17.5h7"/></svg>',
  prompt: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>',
  gear: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"/></svg>',
};

function ensureMenu() {
  let menu = $('agent-menu');
  if (menu) return menu;
  menu = document.createElement('div');
  menu.id = 'agent-menu';
  menu.className = 'overflow-menu agent-menu hidden';
  menu.setAttribute('role', 'menu');
  menu.setAttribute('aria-label', 'Agents');
  menu.addEventListener('pointerdown', (e) => { if (!e.target.closest('input, textarea')) e.preventDefault(); });
  menu.addEventListener('click', onMenuClick);
  menu.addEventListener('keydown', onMenuKey);
  document.body.appendChild(menu);
  document.addEventListener('click', (e) => {
    if (menu.classList.contains('hidden')) return;
    if (!menu.contains(e.target) && !e.target.closest('#overflow-agents-btn')) closeMenu();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !menu.classList.contains('hidden')) { e.stopPropagation(); closeMenu(); }
  }, true);
  return menu;
}

function loadoutItem(name, title, detail) {
  const on = (state.current || '') === (name || '');
  return `<button type="button" class="overflow-menu-item agent-menu-loadout${on ? ' active' : ''}" role="menuitemradio" aria-checked="${on}" data-loadout="${esc(name)}">
    <span class="agent-menu-radio" aria-hidden="true"></span>
    <span class="agent-menu-text"><span>${esc(title)}</span>${detail ? `<small>${esc(detail)}</small>` : ''}</span>
  </button>`;
}

/** The persona or custom prompt the next turn sends, if any. It applies in
 *  Agent mode too, alongside a loadout's own instructions. */
function personaNote() {
  const name = presetsModule.getCharacterName?.() || '';
  if (name) return `<small>Persona: ${esc(name)}</small>`;
  const custom = presetsModule.getSelectedPreset?.() && presetsModule.getPreset?.('custom');
  if (custom && custom.enabled !== false && custom.system_prompt) return '<small>Custom prompt on</small>';
  return '';
}

function menuHtml() {
  const profiles = state.profiles;
  let loadouts;
  if (!profiles) loadouts = '<div class="agent-menu-note">Loading loadouts…</div>';
  else {
    loadouts = loadoutItem('', 'Default', 'This chat\'s own settings');
    // A chat can still carry a loadout that was since renamed or deleted.
    if (state.current && !profiles.some((p) => p.name === state.current)) {
      loadouts += loadoutItem(state.current, state.current, 'No longer configured');
    }
    loadouts += profiles.map((p) => loadoutItem(p.name, p.name,
      p.description || [p.tool_access === 'all' ? 'all tools' : `${p.tool_access} tools`, `${p.memory_access} memory`].join(' · '))).join('');
    if (!profiles.length) loadouts += '<div class="agent-menu-note">No loadouts configured yet.</div>';
  }
  return `<div class="agent-menu-head">Loadout${sessionId() ? '' : ' <small>for the next message</small>'}</div>
    <div class="agent-menu-loadouts" role="group" aria-label="Loadouts">${loadouts}</div>
    <div class="agent-menu-sep" role="separator"></div>
    <button type="button" class="overflow-menu-item" role="menuitem" data-agent-action="panel">${ICONS.panel}<span>Open Agents panel</span></button>
    <button type="button" class="overflow-menu-item" role="menuitem" data-agent-action="prompts">${ICONS.prompt}<span class="agent-menu-text"><span>Prompts &amp; personas</span>${personaNote()}</span></button>
    <button type="button" class="overflow-menu-item" role="menuitem" data-agent-action="manage">${ICONS.gear}<span>Manage loadouts</span></button>`;
}

function render() {
  const menu = $('agent-menu');
  if (!menu || menu.classList.contains('hidden')) return;
  const focusedKey = document.activeElement && menu.contains(document.activeElement)
    ? (document.activeElement.dataset.loadout ?? document.activeElement.dataset.agentAction) : null;
  menu.innerHTML = menuHtml();
  position(menu);
  if (focusedKey != null) {
    const again = [...menu.querySelectorAll('[data-loadout], [data-agent-action]')]
      .find((el) => (el.dataset.loadout ?? el.dataset.agentAction) === focusedKey);
    again?.focus();
  }
}

function position(menu) {
  const anchor = $('overflow-plus-btn');
  if (!anchor) return;
  const r = anchor.getBoundingClientRect();
  menu.style.left = `${Math.max(8, Math.min(r.left, window.innerWidth - menu.offsetWidth - 8))}px`;
  menu.style.right = 'auto';
  menu.style.bottom = 'auto';
  menu.style.maxHeight = '';
  menu.style.overflowY = '';
  const avail = r.top - 16;
  const natural = menu.scrollHeight;
  const h = Math.min(natural, avail);
  if (natural > avail) { menu.style.maxHeight = `${avail}px`; menu.style.overflowY = 'auto'; }
  menu.style.top = `${r.top - 8 - h}px`;
}

export async function openMenu({ focus = false } = {}) {
  const menu = ensureMenu();
  menu.classList.remove('closing', 'hidden');
  $('overflow-agents-btn')?.setAttribute('aria-expanded', 'true');
  render();
  if (focus) menu.querySelector('[data-loadout], [data-agent-action]')?.focus();
  await Promise.all([loadProfiles(), loadCurrent()]);
  syncButton();
  render();
  if (focus && !menu.contains(document.activeElement)) menu.querySelector('.agent-menu-loadout.active, [data-agent-action]')?.focus();
}

export function closeMenu() {
  const menu = $('agent-menu');
  if (!menu || menu.classList.contains('hidden') || menu.classList.contains('closing')) return;
  $('overflow-agents-btn')?.setAttribute('aria-expanded', 'false');
  const hadFocus = menu.contains(document.activeElement);
  menu.classList.add('closing');
  setTimeout(() => { menu.classList.add('hidden'); menu.classList.remove('closing'); }, 400);
  if (hadFocus) $('message')?.focus();
}

function onMenuClick(e) {
  const loadout = e.target.closest('[data-loadout]');
  if (loadout) {
    const name = loadout.dataset.loadout || '';
    closeMenu();
    if (name !== (state.current || '')) choose(name);
    return;
  }
  const action = e.target.closest('[data-agent-action]')?.dataset.agentAction;
  if (!action) return;
  closeMenu();
  if (action === 'panel') window.agentsDashboard?.open?.();
  else if (action === 'prompts') presetsModule.openCustomPresetModal?.();
  else if (action === 'manage') openLoadoutSettings();
}

function onMenuKey(e) {
  if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
  const items = [...e.currentTarget.querySelectorAll('[data-loadout], [data-agent-action]')];
  if (!items.length) return;
  e.preventDefault();
  const i = items.indexOf(document.activeElement);
  const next = e.key === 'ArrowDown' ? (i + 1) % items.length : (i <= 0 ? items.length - 1 : i - 1);
  items[next].focus();
}

function openLoadoutSettings() {
  if (typeof window.adminModule?.open !== 'function') return;
  window.adminModule.open('tools');
  setTimeout(() => $('set-agentProfiles')?.closest('.settings-col, [data-settings-panel]')
    ?.querySelector('.agent-profiles-title')?.scrollIntoView({ block: 'start', behavior: 'smooth' }), 150);
}

// ── wiring ────────────────────────────────────────────────────────────────
function init() {
  const btn = $('overflow-agents-btn');
  if (!btn) return;
  // Follow the Agent/Chat toggle. Several modules flip its buttons directly
  // (research, compare, documents, slash commands) rather than through one
  // setter, so watch the Agent button's state instead of hooking each one.
  const agentBtn = $('mode-agent-btn');
  if (agentBtn) {
    const sync = () => applyMode(agentBtn.classList.contains('active') ? 'agent' : 'chat');
    new MutationObserver(sync).observe(agentBtn, { attributes: true, attributeFilter: ['class'] });
    sync();
  }
  btn.setAttribute('aria-haspopup', 'menu');
  btn.setAttribute('aria-expanded', 'false');
  btn.addEventListener('click', (e) => {
    // The overflow menu closes itself on any item click (app.js); this one
    // opens in its place, anchored to the same chevron.
    e.stopPropagation();
    openMenu({ focus: e.detail === 0 });
  });
  window.addEventListener('resize', () => { const m = $('agent-menu'); if (m && !m.classList.contains('hidden')) position(m); });
  // Keep the entry's loadout label in step with the open chat.
  let lastSid;
  setInterval(() => {
    if (state.mode !== 'agent') return;
    const sid = sessionId();
    if (sid === lastSid) return;
    lastSid = sid;
    loadCurrent().then(syncButton);
  }, 1000);
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();

const agentMenu = { applyMode, openMenu, closeMenu, applyPendingLoadout };
window.agentMenuModule = agentMenu;
export default agentMenu;
