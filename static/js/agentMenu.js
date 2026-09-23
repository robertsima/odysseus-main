/* agentMenu.js — the composer's Agents menu, shown in place of Prompt in Agent mode.
 *
 * In Chat mode the overflow (chevron) menu offers "Prompt", which opens the
 * prompt/persona window. In Agent mode that entry becomes "Agents", which opens
 * this menu:
 *   · the configured loadouts (Settings › Workbench › Agent profiles). Picking
 *     one runs the open chat under it (POST /api/agents/sessions/{id}/loadout);
 *     picking Default clears it. A chat that does not exist yet (nothing sent)
 *     gets the loadout the moment it is created, before its first turn runs.
 *   · Edit this agent's persona — the open chat's own loadout copy, in the
 *     Agent Control Room's editor
 *   · Open the Agents panel (the Agent Control Room)
 *   · Shared prompt & personas — the Prompt window. It edits only the shared
 *     prompt (Chat mode, and agents with no loadout); never an agent's own.
 *   · Manage loadouts — Settings › Workbench › Agent profiles
 *
 * One rule: the Prompt window edits the shared prompt; an agent's persona is
 * edited with the agent. syncPromptScope labels the Prompt window accordingly.
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
  voice: null,         // { persona } when the open chat has its own loadout voice
};
// Settings that give a chat its own voice (src/session_settings.loadout_voice).
const VOICE_KEYS = ['agent_profile', 'agent_instructions', 'agent_persona_name', 'agent_temperature', 'agent_max_tokens'];

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
  if (!sid) {
    state.current = state.pending?.name || null;
    const profile = state.current && (state.profiles || []).find((p) => p.name === state.current);
    state.voice = state.current ? { persona: profile?.persona_name || '' } : null;
    return;
  }
  try {
    const settings = (await api(`/api/session/${encodeURIComponent(sid)}/settings`))?.settings || {};
    state.current = settings.agent_profile || null;
    state.voice = VOICE_KEYS.some((key) => settings[key] != null && settings[key] !== '')
      ? { persona: settings.agent_persona_name || '' } : null;
  } catch (_) {
    state.current = null;
    state.voice = null;
  }
}

/** Label the Prompt window: it edits the shared prompt, and says so plainly
 *  when the open chat is an agent that uses its own persona instead. */
export function syncPromptScope() {
  const banner = $('preset-scope-agent');
  if (!banner) return;
  const agentVoice = sharedPersonaSuppressed();
  banner.hidden = !agentVoice;
  if (!agentVoice) return;
  const who = state.current || state.voice.persona || 'a custom agent';
  const name = $('preset-scope-agent-name');
  if (name) name.textContent = who;
  const edit = $('preset-scope-edit-agent');
  if (edit) {
    edit.hidden = !sessionId();
    if (!edit._wired) {
      edit._wired = true;
      edit.addEventListener('click', () => {
        document.getElementById('close-custom-preset')?.click();
        editThisAgent();
      });
    }
  }
}

function editThisAgent() {
  const sid = sessionId();
  if (!sid) {
    uiModule.showToast('Send a message first. This agent\'s editor opens once the chat exists.');
    return;
  }
  window.agentsDashboard?.editLoadout?.(sid);
}

/** True when the next turn runs as an agent under a loadout, so the shared
 *  persona, prompt and inject text from the Prompt window stay out of it. */
export function sharedPersonaSuppressed() { return state.mode === 'agent' && !!state.voice; }
export function loadoutPersonaName() { return state.voice?.persona || ''; }

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
  // The shared persona chip would claim a persona this chat is not using.
  document.body.classList.toggle('loadout-voice', !!state.voice);
}

async function choose(name) {
  const sid = sessionId();
  if (!sid) {
    // No chat yet: remember it and apply it when the first send creates one.
    state.pending = name ? { name } : null;
    state.pendingFor = name ? (window.sessionModule?.getPendingChat?.() || null) : null;
    await loadCurrent();
    syncButton();
    uiModule.showToast(name ? `${name} will run this chat once you send` : 'This chat will use the default setup');
    return;
  }
  try {
    const result = await api(`/api/agents/sessions/${encodeURIComponent(sid)}/loadout`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ profile: name || null }),
    });
    state.current = result?.agent_profile || null;
    await loadCurrent();
    syncButton();
    try { window.chatSettingsModule?.refresh?.(); } catch (_) {}
    uiModule.showToast(name ? `This chat now runs as ${name} (its model applies to delegated workers only)` : 'Loadout cleared — default setup', 'success');
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
  } catch (err) {
    uiModule.showToast(`${pending.name} was not applied: ${err.message}`, 'error');
  }
  await loadCurrent();
  syncButton();
}

// ── menu ──────────────────────────────────────────────────────────────────
const ICONS = {
  panel: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><path d="M17.5 14v7M14 17.5h7"/></svg>',
  prompt: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>',
  shared: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>',
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
  if (state.voice) return '<small>Not used by this agent, which has its own</small>';
  const name = presetsModule.getCharacterName?.() || '';
  if (name) return `<small>Persona: ${esc(name)} · for chats and agents without a loadout</small>`;
  const custom = presetsModule.getSelectedPreset?.() && presetsModule.getPreset?.('custom');
  if (custom && custom.enabled !== false && custom.system_prompt) return '<small>Custom prompt on · for chats and agents without a loadout</small>';
  return '<small>For chats and agents without a loadout</small>';
}

function menuHtml() {
  const profiles = state.profiles;
  let loadouts;
  if (!profiles) loadouts = '<div class="agent-menu-note">Loading loadouts…</div>';
  else {
    loadouts = loadoutItem('', 'Default', 'No loadout: uses the shared prompt and this chat\'s settings');
    // A chat can still carry a loadout that was since renamed or deleted.
    if (state.current && !profiles.some((p) => p.name === state.current)) {
      loadouts += loadoutItem(state.current, state.current, 'No longer configured');
    }
    loadouts += profiles.map((p) => loadoutItem(p.name, p.name,
      [p.persona_name ? `as ${p.persona_name}` : '',
        p.description || [p.tool_access === 'all' ? 'all tools' : `${p.tool_access} tools`, `${p.memory_access} memory`].join(' · ')]
        .filter(Boolean).join(' — '))).join('');
    if (!profiles.length) loadouts += '<div class="agent-menu-note">No loadouts configured yet.</div>';
  }
  return `<div class="agent-menu-head">Loadout${sessionId() ? '' : ' <small>for the next message</small>'}</div>
    <p class="agent-menu-sub">A loadout sets what this agent may do (tools, memory, approvals) and how it sounds (persona, temperature).</p>
    <div class="agent-menu-loadouts" role="group" aria-label="Loadouts">${loadouts}</div>
    <div class="agent-menu-sep" role="separator"></div>
    ${state.voice ? `<button type="button" class="overflow-menu-item" role="menuitem" data-agent-action="edit-agent">${ICONS.prompt}<span class="agent-menu-text"><span>Edit this agent's persona</span><small>${esc(state.voice.persona ? `Answers as ${state.voice.persona}` : 'Persona, instructions, temperature')}</small></span></button>` : ''}
    <button type="button" class="overflow-menu-item" role="menuitem" data-agent-action="panel">${ICONS.panel}<span class="agent-menu-text"><span>Open Agents panel</span><small>Watch, steer and approve running agents</small></span></button>
    <button type="button" class="overflow-menu-item" role="menuitem" data-agent-action="prompts">${ICONS.shared}<span class="agent-menu-text"><span>Shared prompt &amp; personas</span>${personaNote()}</span></button>
    <button type="button" class="overflow-menu-item" role="menuitem" data-agent-action="manage">${ICONS.gear}<span class="agent-menu-text"><span>Manage loadouts</span><small>Create and edit reusable agent presets</small></span></button>`;
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
  if (action === 'edit-agent') editThisAgent();
  else if (action === 'panel') window.agentsDashboard?.open?.();
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
  // Tracked in both modes, so switching to Agent already knows the voice.
  let lastSid;
  document.addEventListener('odysseus:loadout-changed', () => { loadCurrent().then(syncButton); });
  setInterval(() => {
    const sid = sessionId();
    if (sid === lastSid) return;
    lastSid = sid;
    loadCurrent().then(syncButton);
  }, 1000);
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();

const agentMenu = { applyMode, openMenu, closeMenu, applyPendingLoadout, sharedPersonaSuppressed, loadoutPersonaName, syncPromptScope };
window.agentMenuModule = agentMenu;
export default agentMenu;
