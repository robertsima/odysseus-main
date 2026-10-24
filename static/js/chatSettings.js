/* chatSettings.js — per-chat settings and the context strip above the conversation.
 *
 * Each chat keeps its own setup (routes/session_routes.py /session/{id}/settings):
 *   · approval mode — whether risky tool calls stop for approval first
 *     (auto / ask_risky / ask_all; src/approval_modes.py). A chat with no
 *     mode follows the app default (Settings › Workbench › Default approvals).
 *   · tools switched off for this chat only (enforced server-side)
 *   · the toggles and workspace it last ran with (recorded by the chat route on
 *     every turn and restored here when the chat is reopened, so a chat no
 *     longer inherits whatever the previous chat left in the browser)
 *
 * The strip (#chat-status-line, between the top bar and the history) says who
 * answers (the loadout's agent in Agent mode, the shared Prompt persona
 * otherwise), the project, the open document and what the chat may do. Its
 * Change button opens the settings panel. With a document open it also holds
 * the document inclusion control (include / leave out),
 * saved as the chat's doc_target. The persona, workspace and context-use
 * indicators are moved into it (nodes and handlers unchanged). The panel
 * floats over the conversation above the composer, so opening it never
 * squeezes the chat.
 */

import uiModule from './ui.js';
import workspaceModule from './workspace.js';
import presetsModule from './presets.js';

const API = '';
// How bash/python run in this chat (src/shell_access.py). Separate from vault access.
const SHELL_INFO = {
  sandbox: { label: 'Sandboxed', short: 'sandboxed', desc: 'Runs in a sandbox that holds only the workspace (or a scratch folder) and the system tools: no app data, no vault. The default.' },
  host: { label: 'Full server shell', short: 'full', desc: "Runs unrestricted on the server as the app's user. It can read anything that user can, including the vault, whatever the vault setting says. Admins only." },
  off: { label: 'Off', short: 'off', desc: 'No bash or python in this chat.' },
};

const MODE_INFO = {
  auto: { label: 'Auto', desc: 'Tools run without asking.' },
  ask_risky: { label: 'Ask for risky', desc: 'Destructive or outward-facing actions ask first: deleting files, git push, publishing, sending or deleting email, sudo.' },
  ask_all: { label: 'Ask for everything', desc: 'Every tool that can change something asks first, including any shell command, and so does anything high-impact once web, email or file content has entered the run.' },
};
const DELEGATION_INFO = {
  never: { label: 'Never', short: 'never', desc: 'This chat never starts sub-agents or hands work to coding agents.' },
  explicit: { label: 'Only when I ask', short: 'when asked', desc: 'It delegates only when your message asks for another agent or names a loadout.' },
  auto: { label: 'Agent decides', short: 'agent decides', desc: 'It may start sub-agents and hand off work whenever it judges that useful.' },
};
// Settings that give a chat its own voice; mirrors agentMenu.js VOICE_KEYS.
const VOICE_KEYS = ['agent_profile', 'agent_instructions', 'agent_persona_name', 'agent_temperature', 'agent_max_tokens'];
// Group tool names for the per-chat tools picker.
const TOOL_GROUPS = [
  ['Shell & code', (t) => ['bash', 'python', 'manage_bg_jobs', 'delegate_to_claude_code', 'manage_agent_worktree'].includes(t)],
  ['Files', (t) => ['read_file', 'write_file', 'edit_file', 'apply_patch', 'grep', 'glob', 'ls', 'get_workspace'].includes(t)],
  ['Web', (t) => /^web_|browser/.test(t)],
  ['Email', (t) => /email|unsubscribe|attachment/.test(t)],
  ['Documents', (t) => /document/.test(t)],
  ['Sub-agents & sessions', (t) => /session|pipeline|chat_with_model|ask_teacher/.test(t)],
  ['Memory & knowledge', (t) => /memory|skill|search_documents|vault|rag/.test(t)],
  ['Notes, calendar & tasks', (t) => /notes|calendar|task|todo|contact|plan/.test(t)],
  ['Models & serving', (t) => /model|serve|download|cookbook|hf_/.test(t)],
];

const state = {
  sessionId: null,
  data: null,        // GET /settings payload
  context: null,     // last odysseus:context-usage detail for this chat
  tools: null,       // cached /api/tools list
  carriedDoc: null,  // document already open on entering this chat (not opened here)
  localTarget: null, // doc target picked in this chat
  quickActionsFor: '', // agent whose quick-action chips the strip shows
  loadSeq: 0,
};

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
function basename(p) { const parts = String(p || '').replace(/[\\/]+$/, '').split(/[\\/]/); return parts[parts.length - 1] || p; }

async function api(path, opts = {}) {
  const res = await fetch(API + path, Object.assign({ credentials: 'same-origin' }, opts));
  let body = null;
  try { body = await res.json(); } catch (_) {}
  if (!res.ok) {
    const err = new Error((body && (body.detail || body.error)) || `${res.status}`);
    err.status = res.status;
    throw err;
  }
  return body;
}
function patchSettings(patch) {
  return api(`/api/session/${encodeURIComponent(state.sessionId)}/settings`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch),
  });
}

// ── restore on chat switch ────────────────────────────────────────────────
async function onSessionSwitch(sessionId) {
  const fromNewChat = !state.sessionId;
  // A chat created from the new-chat screen keeps the document opened there.
  if (!(fromNewChat && sessionId)) {
    state.carriedDoc = (window.documentModule?.getCurrentDocId?.()) || null;
    state.localTarget = null;
  }
  const pendingTarget = fromNewChat && sessionId ? state.localTarget : null;
  state.sessionId = sessionId || null;
  state.data = null;
  state.context = null;
  closePanel();
  render();
  if (!sessionId) {
    // A new chat starts without a folder; the last chat's never carries over.
    // Only clear when leaving a chat, so a folder picked in the new chat
    // before its first message (the id is still null then) is not dropped.
    if (!fromNewChat && workspaceModule?.getWorkspace?.()) workspaceModule.setWorkspace('', { persist: false });
    return;
  }
  const seq = ++state.loadSeq;
  try {
    const data = await api(`/api/session/${encodeURIComponent(sessionId)}/settings`);
    if (seq !== state.loadSeq) return;
    state.data = data;
    restore(data.settings || {}, { fromNewChat });
    if (pendingTarget && !(data.settings || {}).doc_target) save({ doc_target: pendingTarget });
  } catch (_) {
    if (seq !== state.loadSeq) return;
    state.data = null;
  }
  render();
}

function restore(settings, { fromNewChat = false } = {}) {
  const toggles = settings.toggles;
  if (toggles && window.__odysseusApplyChatToggles) {
    try { window.__odysseusApplyChatToggles(toggles); } catch (_) {}
  }
  // The workspace belongs to the chat (or the agent that made it). A chat
  // that has run, or has a saved folder, gets exactly its own. A chat that has
  // not run yet keeps a folder picked for it while it was new, and saves it;
  // arriving from another chat, it starts without one.
  if (!workspaceModule?.setWorkspace) return;
  const current = workspaceModule.getWorkspace?.() || '';
  const saved = settings.workspace || '';
  const next = (saved || toggles) ? saved : (fromNewChat ? current : '');
  if (next !== current) workspaceModule.setWorkspace(next, { persist: false });
  else if (next && !saved) workspaceModule.setWorkspace(next);
}

// ── context strip ─────────────────────────────────────────────────────────
// Indicators that already own their behavior elsewhere (presets.js, workspace.js,
// chat.js). They move into the strip once; render() never rewrites them.
const MOVED_NODES = ['chat-context-pill', 'character-indicator-btn', 'workspace-indicator-btn'];
const DOC_TARGETS = [
  ['document', 'Include document'],
  ['general', 'Leave document out'],
];

function ensureLine() {
  let line = $('chat-status-line');
  if (line) return line;
  const top = document.querySelector('#chat-container > .chat-top-bar');
  if (!top || !top.parentNode) return null;
  line = document.createElement('div');
  line.id = 'chat-status-line';
  line.className = 'chat-status-line ctx-strip';
  line.setAttribute('role', 'toolbar');
  line.setAttribute('aria-label', 'Chat context');
  line.innerHTML = '<div class="ctx-l1"><span class="ctx-main" data-ctx="main"></span>'
    + '<span class="ctx-slot" data-ctx="slot"></span>'
    + '<div class="ctx-quick-actions"></div>'
    + '<span class="ctx-change" data-ctx="change"></span></div>'
    + '<div class="ctx-l2"><span class="ctx-access" data-ctx="access"></span><span class="ctx-target" data-ctx="target"></span></div>';
  top.parentNode.insertBefore(line, top.nextSibling);
  const slot = line.querySelector('[data-ctx="slot"]');
  MOVED_NODES.forEach((id) => { const n = $(id); if (n) slot.appendChild(n); });
  line.addEventListener('click', onLineClick);
  line.addEventListener('keydown', (e) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key) || !e.target.matches('[data-csl="target"]')) return;
    e.preventDefault();
    const buttons = [...line.querySelectorAll('[data-csl="target"]')];
    const index = e.key === 'Home' ? 0 : e.key === 'End' ? buttons.length - 1
      : (buttons.indexOf(e.target) + (e.key === 'ArrowRight' ? 1 : -1) + buttons.length) % buttons.length;
    buttons[index].click();
    line.querySelector(`[data-target="${buttons[index].dataset.target}"]`)?.focus();
  });
  return line;
}
const part = (line, name) => line.querySelector(`[data-ctx="${name}"]`);

const isAgentMode = () => document.body.classList.contains('composer-agent-mode');

/** Whether an owner-managed indicator is currently showing. */
function nodeShown(id) {
  const n = $(id);
  return !!n && !n.hidden && n.style.display !== 'none';
}

/** The document open in the editor, if any. */
function openDoc() {
  const dm = window.documentModule;
  const id = (dm && dm.getCurrentDocId && dm.getCurrentDocId()) || null;
  if (!id) return null;
  let title = (dm.getCurrentDocTitle && dm.getCurrentDocTitle()) || '';
  const ctxDoc = state.data && state.data.context && state.data.context.document;
  if (!title && ctxDoc && ctxDoc.id === id) title = ctxDoc.title || '';
  return { id, title: title || 'Untitled' };
}

/** What the open document stands for in the next message: the pick made in this
 *  chat, else the saved one, else the document itself when it was opened here,
 *  else a general conversation. A document that was already open when the chat
 *  was entered never aims the chat at it on its own. */
function effectiveTarget() {
  const saved = state.localTarget || ((state.data && state.data.settings) || {}).doc_target;
  // Legacy "project" never changed workspace access; it only excluded the doc.
  if (saved === 'project') return 'general';
  if (DOC_TARGETS.some(([key]) => key === saved)) return saved;
  const doc = openDoc();
  return doc && doc.id !== state.carriedDoc ? 'document' : 'general';
}

/** For the send path: the open document and the target to send with it. */
function docTarget() {
  const doc = openDoc();
  return { docId: doc ? doc.id : null, target: effectiveTarget() };
}

/** Who answers the next turn, by the rule chat.js uses: in Agent mode a chat
 *  with its own loadout voice speaks as that agent; otherwise the shared
 *  Prompt persona applies, if one is active. */
function identity() {
  const s = (state.data && state.data.settings) || {};
  const agent = isAgentMode();
  const voice = VOICE_KEYS.some((key) => s[key] != null && s[key] !== '');
  if (agent && voice) {
    const name = s.agent_persona_name || s.agent_profile || 'Custom agent';
    return { kind: 'agent', name, loadout: s.agent_profile || '', persona: s.agent_persona_name || '' };
  }
  let shared = '';
  try { shared = presetsModule.getCharacterName ? presetsModule.getCharacterName() : ''; } catch (_) {}
  if (shared) return { kind: 'persona', name: shared, agent };
  return agent ? { kind: 'default-agent', name: 'Default agent' } : null;
}

/** Whether the chat's tool allowlist (a loadout with tool_access "selected")
 *  lets it call `name`. Reads entries the way src/tool_policy.allowlist_permits
 *  does for plain names and `*` patterns. */
function allowlistPermits(name, settings) {
  if ((settings.tool_access || 'all') === 'all') return true;
  if (settings.tool_access !== 'selected') return false;
  return (settings.enabled_tools || []).some((entry) => {
    if (!entry.includes('*')) return entry === name;
    const re = new RegExp('^' + entry.split('*').map((p) => p.replace(/[.+?^${}()|[\]\\]/g, '\\$&')).join('.*') + '$');
    return re.test(name);
  });
}

/** Whether the chat's access differs from what a fresh chat has. */
function accessIsCustom(d, s) {
  return (d.approval_mode || 'auto') !== 'auto'
    || (s.tool_access && s.tool_access !== 'all')
    || (s.disabled_tools || []).length > 0
    || (d.shell_access || 'sandbox') !== 'sandbox';
}

function render() {
  const line = ensureLine();
  if (!line) return;
  const focusedTarget = line.contains(document.activeElement)
    ? document.activeElement?.dataset?.target : null;
  const d = state.data || {};
  const s = d.settings || {};
  const context = d.context || null;
  const doc = openDoc();
  const target = effectiveTarget();
  const who = identity();
  const dot = '<span class="ctx-dot" aria-hidden="true">·</span>';

  // Line 1: who · project · document
  const bits = [];
  if (who && !(who.kind === 'persona' && nodeShown('character-indicator-btn'))) {
    const label = who.kind === 'persona' ? `Persona: ${who.name}` : who.name;
    const title = who.kind === 'agent'
      ? `This chat runs as ${who.loadout || 'its own agent setup'}${who.persona ? ` (persona ${who.persona})` : ''}. Its instructions replace the shared Prompt persona.`
      : who.kind === 'persona'
        ? `The shared Prompt persona ${who.name} answers${who.agent ? ' (this chat has no loadout of its own)' : ''}.`
        : 'Agent mode with no loadout: the default agent setup answers.';
    bits.push(`<button type="button" class="ctx-who ctx-who-${esc(who.kind)}" data-csl="panel-agent" title="${esc(title)}">${esc(label)}</button>`);
  }
  const project = context && context.project;
  const wsName = nodeShown('workspace-indicator-btn') ? (($('workspace-indicator-name') || {}).textContent || '').trim() : '';
  if (project && project.toLowerCase() !== wsName.toLowerCase()) bits.push(`<span class="ctx-project" title="Project">${esc(project)}</span>`);
  if (doc && target === 'document') bits.push(`<span class="ctx-doc" title="The open document and selected text are included with your messages">Document: ${esc(doc.title)}</span>`);
  part(line, 'main').innerHTML = bits.join(dot);

  // Line 2: what the chat may do, and the target selector when a document is open
  const mode = d.approval_mode || 'auto';
  const modesEnforced = Array.isArray(d.approval_modes) && d.approval_modes.length > 0;
  const summary = context && context.access && context.access.summary;
  const links = [];
  if (d.parent_session && d.parent_session.id) {
    const from = s.agent_profile ? `${s.agent_profile} · ` : '';
    links.push(`<button type="button" class="ctx-link" data-csl="fork" data-id="${esc(d.parent_session.id)}" title="Sub-agent chat: open the chat that delegated this task">↳ ${esc(from)}from ${esc(d.parent_session.name || 'parent chat')}</button>`);
  }
  if (d.forked_from && d.forked_from.id) {
    links.push(`<button type="button" class="ctx-link" data-csl="fork" data-id="${esc(d.forked_from.id)}" title="Open the chat this was forked from">⫝ ${esc(d.forked_from.name || 'source chat')}</button>`);
  }
  const accessHtml = summary
    ? (modesEnforced
      ? `<button type="button" class="ctx-access-btn csl-mode-${esc(mode)}" data-csl="mode-menu" title="${esc(((MODE_INFO[mode] || {}).desc || '') + ' Click to change.')}">${esc(summary)}</button>`
      : `<span class="ctx-access-text">${esc(summary)}</span>`)
    : '';
  part(line, 'access').innerHTML = accessHtml + links.join('');
  part(line, 'target').innerHTML = doc
    ? `<span class="ctx-seg" role="radiogroup" aria-label="Include the open document in messages">${DOC_TARGETS.map(([key, label]) =>
      `<button type="button" role="radio" aria-checked="${key === target}" class="ctx-seg-btn${key === target ? ' on' : ''}" data-csl="target" data-target="${key}" title="${esc(doc.title)}">${esc(label)}</button>`).join('')}</span>`
      + '<span class="ctx-document-help">Applies to the open document and selected text only. Workspace and tool access stay unchanged.</span>'
    : '';
  part(line, 'change').innerHTML = state.sessionId
    ? '<button type="button" class="ctx-change-btn" data-csl="panel" title="Change who answers, approvals, tools and privacy for this chat">Change</button>'
    : '';

  // The answering agent's quick actions (agentActions.js owns the chips).
  const qaAgent = who && who.kind === 'agent' ? who.loadout : '';
  if (qaAgent !== state.quickActionsFor && window.agentActions?.renderQuickActions) {
    state.quickActionsFor = qaAgent;
    window.agentActions.renderQuickActions(line.querySelector('.ctx-quick-actions'), qaAgent)
      .then(() => { if (state.quickActionsFor === qaAgent) render(); })
      .catch(() => {});
  }

  const something = bits.length > 0 || links.length > 0 || !!doc
    || MOVED_NODES.some(nodeShown) || !!line.querySelector('.ctx-quick-actions > *')
    || !!(summary && accessIsCustom(d, s));
  line.hidden = !something;
  line.classList.toggle('ctx-no-access', !summary);
  if (focusedTarget) line.querySelector(`[data-csl="target"][data-target="${focusedTarget}"]`)?.focus();
}

async function onLineClick(e) {
  const b = e.target.closest('[data-csl]');
  if (!b) return;
  const act = b.dataset.csl;
  if (act === 'panel') togglePanel();
  else if (act === 'panel-tools') togglePanel('tools');
  else if (act === 'panel-privacy') togglePanel('privacy');
  else if (act === 'panel-agent') togglePanel('agent');
  else if (act === 'mode-menu') toggleModeMenu(b);
  else if (act === 'target') pickTarget(b.dataset.target);
  else if (act === 'fork') window.sessionModule?.selectSession?.(b.dataset.id);
}

/** The segmented control: remember the pick here at once, then save it on the chat. */
function pickTarget(value) {
  if (value === 'project') value = 'general';
  if (!DOC_TARGETS.some(([key]) => key === value)) return;
  state.localTarget = value;
  render();
  if (state.sessionId) save({ doc_target: value });
}

// ── approval mode quick menu ──────────────────────────────────────────────
function closeModeMenu() { $('csl-mode-menu')?.remove(); }
function toggleModeMenu(anchor) {
  if ($('csl-mode-menu')) { closeModeMenu(); return; }
  closePanel();
  const d = state.data || {};
  const current = (d.settings || {}).approval_mode || '';
  const menu = document.createElement('div');
  menu.id = 'csl-mode-menu';
  menu.className = 'csl-mode-menu';
  menu.setAttribute('role', 'menu');
  menu.innerHTML = ['', ...Object.keys(MODE_INFO)].map((key) => {
    const info = key ? MODE_INFO[key] : { label: 'App default', desc: `Currently ${MODE_INFO[d.approval_mode]?.label || 'Auto'}.` };
    return `<button type="button" role="menuitemradio" aria-checked="${current === key}" data-mode="${esc(key)}"><b>${esc(info.label)}</b><small>${esc(info.desc)}</small></button>`;
  }).join('');
  menu.addEventListener('click', async (e) => {
    const b = e.target.closest('button[data-mode]');
    if (!b) return;
    closeModeMenu();
    await save({ approval_mode: b.dataset.mode || null }, b.dataset.mode ? `Approvals: ${MODE_INFO[b.dataset.mode].label}` : 'Approvals follow the app default');
  });
  anchor.parentNode.insertBefore(menu, anchor);
  const r = anchor.getBoundingClientRect();
  const line = anchor.closest('#chat-status-line').getBoundingClientRect();
  menu.style.left = `${Math.max(0, r.left - line.left)}px`;
  const close = (e) => { if (!menu.contains(e.target) && e.target !== anchor) { closeModeMenu(); document.removeEventListener('mousedown', close, true); } };
  document.addEventListener('mousedown', close, true);
}

// ── settings panel ────────────────────────────────────────────────────────
function closePanel() {
  const p = $('chat-settings-panel');
  if (p) p.remove();
  document.removeEventListener('mousedown', outsideClose, true);
  document.removeEventListener('keydown', escClose, true);
  window.removeEventListener('resize', positionPanel);
  state.panelObserver?.disconnect();
  state.panelObserver = null;
}

/** Float the panel over the conversation, anchored to the composer: above it
 *  when there is room (the usual case), below the status line otherwise. */
function positionPanel() {
  const panel = $('chat-settings-panel');
  const bar = document.querySelector('.chat-input-bar');
  if (!panel || !bar) return;
  const r = bar.getBoundingClientRect();
  const line = $('chat-status-line')?.getBoundingClientRect();
  const vw = window.innerWidth, vh = window.innerHeight;
  const width = Math.min(Math.max(r.width, 360), vw - 16);
  const left = Math.min(Math.max(8, r.left + (r.width - width) / 2), vw - width - 8);
  const above = r.top - 8 - 50;
  const below = vh - (line ? line.bottom : r.bottom) - 14;
  panel.style.left = `${left}px`;
  panel.style.width = `${width}px`;
  if (above >= 420 || above >= below) {
    panel.style.top = '';
    panel.style.bottom = `${vh - r.top + 8}px`;
    panel.style.maxHeight = `${Math.max(160, Math.min(above, 640))}px`;
  } else {
    panel.style.bottom = '';
    panel.style.top = `${(line ? line.bottom : r.bottom) + 6}px`;
    panel.style.maxHeight = `${Math.max(160, Math.min(below, 640))}px`;
  }
}
function outsideClose(e) {
  const p = $('chat-settings-panel');
  if (p && !p.contains(e.target) && !e.target.closest('#chat-status-line')) closePanel();
}
function escClose(e) { if (e.key === 'Escape') { e.stopPropagation(); closePanel(); } }

async function togglePanel(section) {
  if ($('chat-settings-panel')) { closePanel(); return; }
  if (!state.sessionId) return;
  closeModeMenu();
  const line = ensureLine();
  const panel = document.createElement('div');
  panel.id = 'chat-settings-panel';
  panel.className = 'chat-settings-panel';
  panel.setAttribute('role', 'dialog');
  panel.setAttribute('aria-label', 'Chat settings');
  panel.innerHTML = '<div class="csp-empty">Loading…</div>';
  document.body.appendChild(panel);
  positionPanel();
  window.addEventListener('resize', positionPanel);
  if (window.ResizeObserver && line) {
    state.panelObserver = new ResizeObserver(() => positionPanel());
    state.panelObserver.observe(line);
    const bar = document.querySelector('.chat-input-bar');
    if (bar) state.panelObserver.observe(bar);
  }
  document.addEventListener('mousedown', outsideClose, true);
  document.addEventListener('keydown', escClose, true);
  if (!state.tools) {
    try { state.tools = ((await api('/api/tools')).tools || []); } catch (_) { state.tools = []; }
  }
  renderPanel();
  positionPanel();
  const target = { tools: '.csp-tools-section', privacy: '[data-csp="private-vault"]', agent: '.csp-agent' }[section];
  if (target) $('chat-settings-panel')?.querySelector(target)?.scrollIntoView({ block: 'start' });
}

function renderPanel() {
  const panel = $('chat-settings-panel');
  if (!panel) return;
  const d = state.data || {};
  const s = d.settings || {};
  const mode = (s.approval_mode) || '';
  const effective = d.approval_mode || 'auto';
  const modesEnforced = Array.isArray(d.approval_modes) && d.approval_modes.length > 0;
  const privateVault = s.private_vault_access === true;
  const shell = d.shell_access || 'sandbox';
  const shellRows = Object.keys(SHELL_INFO).map((key) => `<label class="csp-radio"><input type="radio" name="csp-shell" value="${esc(key)}"${shell === key ? ' checked' : ''}><span><b>${esc(SHELL_INFO[key].label)}</b><small>${esc(SHELL_INFO[key].desc)}</small></span></label>`).join('');
  const off = new Set(s.disabled_tools || []);
  const globallyOff = new Set((state.tools || []).filter((t) => t.enabled === false).map((t) => t.id));
  const notInLoadout = new Set((state.tools || []).map((t) => t.id).filter((name) => !allowlistPermits(name, s)));
  const who = identity();
  const agentMode = isAgentMode();
  const policy = s.delegation_policy || 'explicit';
  const delegationRows = Object.keys(DELEGATION_INFO).map((key) => `<label class="csp-radio"><input type="radio" name="csp-delegation" value="${esc(key)}"${policy === key ? ' checked' : ''}><span><b>${esc(DELEGATION_INFO[key].label)}</b><small>${esc(DELEGATION_INFO[key].desc)}</small></span></label>`).join('');
  let whoHtml;
  if (who && who.kind === 'agent') {
    const facts = [who.loadout ? `Loadout <b>${esc(who.loadout)}</b>` : 'Its own agent setup (no saved loadout)'];
    if (who.persona) facts.push(`persona <b>${esc(who.persona)}</b>`);
    whoHtml = `<div class="csp-who"><span class="csp-who-name">${esc(who.name)}</span><small>${facts.join(' · ')}. Its instructions replace the shared Prompt persona for this chat.</small></div>`;
  } else if (who && who.kind === 'persona') {
    whoHtml = `<div class="csp-who"><span class="csp-who-name">${esc(who.name)}</span><small>The shared Prompt persona${agentMode ? '. This chat has no loadout of its own, so the persona applies in Agent mode too' : ''}.</small></div>`;
  } else if (who) {
    whoHtml = '<div class="csp-who"><span class="csp-who-name">Default agent</span><small>No loadout and no persona: the app\'s default agent setup answers.</small></div>';
  } else {
    whoHtml = '<div class="csp-who"><span class="csp-who-name">Plain chat</span><small>No persona is active. Pick one in the composer\'s Prompt menu.</small></div>';
  }
  const accessFacts = [];
  if (s.tool_access === 'selected') accessFacts.push(`Tools: only the ${(s.enabled_tools || []).length} the loadout names`);
  else if (s.tool_access === 'none') accessFacts.push('Tools: none');
  if (s.memory_access) accessFacts.push(`Memory: ${esc(s.memory_access)}`);
  if (s.model_access && s.model_access !== 'current') accessFacts.push(`Worker models: ${esc(s.model_access)}`);
  if (s.max_parallel_workers != null) accessFacts.push(`Up to ${esc(s.max_parallel_workers)} parallel worker${Number(s.max_parallel_workers) === 1 ? '' : 's'}`);
  const modeRows = ['', ...Object.keys(MODE_INFO)].map((key) => {
    const info = key ? MODE_INFO[key] : { label: 'Default', desc: `Follow the app default (currently ${MODE_INFO[effective]?.label || effective} for this chat).` };
    return `<label class="csp-radio"><input type="radio" name="csp-mode" value="${esc(key)}"${mode === key ? ' checked' : ''}><span><b>${esc(info.label)}</b><small>${esc(info.desc)}</small></span></label>`;
  }).join('');
  const names = (state.tools || []).map((t) => t.id);
  const grouped = new Map();
  for (const name of names) {
    const group = (TOOL_GROUPS.find(([, test]) => test(name)) || ['Other'])[0];
    if (!grouped.has(group)) grouped.set(group, []);
    grouped.get(group).push(name);
  }
  const order = [...TOOL_GROUPS.map(([g]) => g), 'Other'].filter((g) => grouped.has(g));
  const toolRows = order.map((group) => `
    <fieldset class="csp-group"><legend>${esc(group)}</legend>
      ${grouped.get(group).map((name) => {
        const global = globallyOff.has(name);
        const outside = !global && notInLoadout.has(name);
        const locked = global || outside;
        const why = global ? 'Switched off for the whole app in Settings' : outside ? 'Not in this chat\'s loadout. Change it in the loadout editor.' : '';
        return `<label class="csp-tool${locked ? ' is-global' : ''}" title="${why}"><input type="checkbox" data-tool="${esc(name)}"${!off.has(name) && !locked ? ' checked' : ''}${locked ? ' disabled' : ''}><code>${esc(name)}</code></label>`;
      }).join('')}
    </fieldset>`).join('');
  const usable = names.filter((n) => !off.has(n) && !globallyOff.has(n) && !notInLoadout.has(n)).length;
  panel.innerHTML = `
    <div class="csp-head"><span class="csp-title">Chat settings</span><span class="csp-sub">Only this chat</span><button type="button" class="csp-close" data-csp="close" aria-label="Close">×</button></div>
    <section class="csp-section csp-agent">
      <h4>Who answers</h4>
      ${whoHtml}
      ${accessFacts.length ? `<p class="csp-facts">${accessFacts.join(' · ')}</p>` : ''}
      <div class="csp-agent-actions">
        ${agentMode ? '<button type="button" class="csp-btn" data-csp="pick-loadout">Change loadout</button>' : ''}
        ${agentMode && who && who.kind === 'agent' ? '<button type="button" class="csp-btn" data-csp="edit-agent">Edit this agent</button>' : ''}
        ${!agentMode ? '<span class="csp-hint">Switch the composer to Agent to run this chat under a loadout.</span>' : ''}
      </div>
    </section>
    ${agentMode ? `<section class="csp-section csp-delegation"><h4>Delegation <span class="csp-count">sub-agents and coding-agent handoffs</span></h4>${delegationRows}</section>` : ''}
    ${modesEnforced ? `<section class="csp-section"><h4>Approvals</h4>${modeRows}</section>` : ''}
    <section class="csp-section csp-privacy">
      <h4>Vault privacy</h4>
      <label class="csp-tool" title="When enabled, private vault excerpts and explicitly read private files may be sent to this chat's model endpoint. The shell is a separate setting below.">
        <input type="checkbox" data-csp="private-vault"${privateVault ? ' checked' : ''}>
        <span><b>Allow private vault reads</b><small>${privateVault ? 'Private excerpts may leave this machine via the selected model endpoint.' : 'Private vault content is hidden by default.'} Does not change the shell.</small></span>
      </label>
    </section>
    <section class="csp-section csp-shell">
      <h4>Shell (bash, python)</h4>
      ${shellRows}
    </section>
    <section class="csp-section csp-tools-section">
      <h4>Tools <span class="csp-count">${usable} of ${names.length} on${s.tool_access === 'selected' ? ', limited by the loadout' : ''}</span></h4>
      <div class="csp-tools-actions"><button type="button" class="csp-link" data-csp="all-on">Turn all on</button></div>
      <div class="csp-tools">${toolRows || '<div class="csp-empty">No tool list available.</div>'}</div>
    </section>
    <p class="csp-note">The mode, web, shell and workspace this chat last ran with are restored when you reopen it.</p>`;
  panel.querySelectorAll('input[name="csp-mode"]').forEach((input) => input.addEventListener('change', async () => {
    await save({ approval_mode: input.value || null }, input.value ? `Approvals: ${MODE_INFO[input.value].label}` : 'Approvals follow the app default');
  }));
  panel.querySelector('[data-csp="private-vault"]')?.addEventListener('change', (e) => {
    const enabled = Boolean(e.target.checked);
    save({ private_vault_access: enabled }, enabled ? 'Private vault reads enabled for this chat' : 'Private vault reads disabled for this chat');
  });
  panel.querySelectorAll('input[name="csp-shell"]').forEach((input) => input.addEventListener('change', () => {
    save({ shell_access: input.value }, `Shell: ${SHELL_INFO[input.value]?.label || input.value}`);
  }));
  panel.querySelectorAll('input[data-tool]').forEach((input) => input.addEventListener('change', () => {
    const next = new Set((state.data?.settings?.disabled_tools) || []);
    if (input.checked) next.delete(input.dataset.tool); else next.add(input.dataset.tool);
    save({ disabled_tools: [...next] });
  }));
  panel.querySelectorAll('input[name="csp-delegation"]').forEach((input) => input.addEventListener('change', () => {
    save({ delegation_policy: input.value }, `Delegation: ${DELEGATION_INFO[input.value]?.label || input.value}`);
  }));
  panel.querySelector('[data-csp="pick-loadout"]')?.addEventListener('click', () => {
    closePanel();
    window.agentMenuModule?.openMenu?.({ focus: true });
  });
  panel.querySelector('[data-csp="edit-agent"]')?.addEventListener('click', () => {
    closePanel();
    if (state.sessionId) window.agentsDashboard?.editLoadout?.(state.sessionId);
  });
  panel.querySelector('[data-csp="close"]')?.addEventListener('click', closePanel);
  panel.querySelector('[data-csp="all-on"]')?.addEventListener('click', () => save({ disabled_tools: null }, 'All tools on for this chat'));
}

let _saveChain = Promise.resolve();
function save(patch, toast) {
  // Serialise writes so rapid checkbox clicks can't race each other.
  _saveChain = _saveChain.then(async () => {
    try {
      state.data = await patchSettings(patch);
      if (toast) uiModule.showToast(toast);
    } catch (err) {
      uiModule.showToast(`Could not save chat settings: ${err.message}`, 'error');
    }
    render();
    renderPanel();
  });
  return _saveChain;
}

// ── wiring ────────────────────────────────────────────────────────────────
function init() {
  // A loadout applied or edited elsewhere (Agents menu, Control Room).
  document.addEventListener('odysseus:loadout-changed', () => {
    if (state.sessionId) api(`/api/session/${encodeURIComponent(state.sessionId)}/settings`).then((d) => { state.data = d; render(); renderPanel(); }).catch(() => {});
  });
  // The workspace picker saves on the chat; show the new folder in the line.
  document.addEventListener('odysseus:chat-settings-changed', (e) => {
    if (state.sessionId && e.detail?.sessionId === state.sessionId) {
      api(`/api/session/${encodeURIComponent(state.sessionId)}/settings`).then((d) => { state.data = d; render(); }).catch(() => {});
    }
  });
  // An approval decided in the chat can change what the settings report.
  document.addEventListener('odysseus:approval-decided', () => {
    if (state.sessionId) api(`/api/session/${encodeURIComponent(state.sessionId)}/settings`).then((d) => { state.data = d; render(); }).catch(() => {});
  });
  // Follow the open chat. Chats are opened, created and materialised from
  // many places with no single event, so watch the id (as the Workbench does).
  // The Agent/Chat toggle and the Prompt persona have no events either; the
  // same tick re-renders when what the line would say about them changes.
  let lastWho = '';
  const tick = () => {
    const sid = window.sessionModule?.getCurrentSessionId?.() || null;
    if (sid !== state.sessionId) { onSessionSwitch(sid); return; }
    const who = identity();
    const doc = openDoc();
    const key = `${isAgentMode()}:${who ? who.kind + ':' + who.name : ''}:${doc ? doc.id + doc.title : ''}:${effectiveTarget()}:${MOVED_NODES.map(nodeShown).join('')}`;
    if (key !== lastWho) { lastWho = key; render(); renderPanel(); }
  };
  tick();
  setInterval(tick, 700);
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();

/** The chat's project and open document, for quick-action placeholders. */
function getContext() {
  const doc = openDoc();
  const ctx = (state.data && state.data.context) || {};
  return { project: ctx.project || '', document: doc ? { id: doc.id, title: doc.title } : null };
}

const chatSettings = { onSessionSwitch, refresh: () => state.sessionId && onSessionSwitch(state.sessionId), render, docTarget, setDocTarget: pickTarget, getContext };
window.chatSettingsModule = chatSettings;
export default chatSettings;
