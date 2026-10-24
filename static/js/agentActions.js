/* agentActions.js — "Talk to agent" and "Assign a task".
 *
 * Every place that shows an agent (the composer Agents menu, the Agents panel
 * cards, the loadout library) offers the same two actions:
 *
 *   Talk to agent    resumes its active chat (creates one on first use)
 *                    (POST /api/agents/profiles/{name}/chat).
 *   Assign a task    the agent works separately and its result returns to the
 *                    chat that assigned it (POST /api/agents/launch). A small
 *                    sheet: the task, what the agent can do here (GET
 *                    /api/agents/profiles/{name}/access), and an Advanced
 *                    disclosure with model, code folder and tools.
 *
 * When a task cannot start, the server answers with a blocker: one sentence
 * and recovery actions. The sheet renders them as buttons wired to what they
 * say (choose a folder, run without files, allow a tool for this run only, paste
 * an excerpt, pick another agent). The sheet never decides what is allowed.
 *
 * `renderQuickActions(container, agentName)` fills `container` with the
 * agent's starting-prompt chips; the chat context strip calls it.
 */

import uiModule from './ui.js';

const esc = (s) => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const $ = (id) => document.getElementById(id);

export const TALK_LABEL = 'Talk to agent';
export const ASSIGN_LABEL = 'Assign a task';
const EXCERPT_LIMIT = 20000;
const ACCESS_DEBOUNCE_MS = 350;

const toast = (text, kind) => {
  try { uiModule.showToast(text, kind); } catch (_) { /* no toast host */ }
};

async function call(path, opts = {}) {
  const res = await fetch(path, Object.assign({ credentials: 'same-origin' }, opts));
  let body = null;
  try { body = await res.json(); } catch (_) { /* not JSON */ }
  return { ok: res.ok, status: res.status, body };
}
const postJson = (path, data) => call(path, {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data),
});
function errorText(r, fallback) {
  const b = r && r.body;
  if (!b) return fallback || `Request failed (${r && r.status})`;
  if (typeof b.detail === 'string') return b.detail;
  return b.error || fallback || `Request failed (${r.status})`;
}

const sessions = () => window.sessionModule;
const currentSessionId = () => sessions()?.getCurrentSessionId?.() || '';

// ── markup shared by every place that lists agents ──────────────────────────
export function actionButtonsHtml(name, { className = '' } = {}) {
  const n = esc(name);
  return `<span class="aa-acts ${esc(className)}">`
    + `<button type="button" class="aa-btn aa-btn-primary" data-agent-act="talk" data-agent="${n}">${TALK_LABEL}</button>`
    + `<button type="button" class="aa-btn" data-agent-act="assign" data-agent="${n}">${ASSIGN_LABEL}</button></span>`;
}

export function actionButtonsEl(name) {
  const wrap = document.createElement('span');
  wrap.innerHTML = actionButtonsHtml(name);
  return wrap.firstElementChild;
}

// ── Talk ────────────────────────────────────────────────────────────────────
/** Resume this agent's active chat; newChat explicitly starts another. */
export async function talkToAgent(name, { open, newChat = false, fromSession } = {}) {
  let from = currentSessionId();
  const sm = sessions();
  if (!from && sm?.getPendingChat?.() && (sm.getSessions?.() || []).length === 0) {
    // A person with no chat yet: the empty one on screen lends its model.
    try { await sm.materializePendingSession(); } catch (_) { /* server falls back */ }
    from = currentSessionId();
  }
  const r = await postJson(`/api/agents/profiles/${encodeURIComponent(name)}/chat`, { from_session: fromSession || from || undefined, new_chat: newChat });
  if (!r.ok || !r.body?.session_id) {
    toast(errorText(r, `Could not start a chat with ${name}`), 'error');
    return null;
  }
  const sid = r.body.session_id;
  try { await sm?.loadSessions?.(); } catch (_) { /* the list refreshes on its own */ }
  if (typeof open === 'function') await open(sid);
  else if (currentSessionId() === from) await sm?.selectSession?.(sid);
  try { document.dispatchEvent(new CustomEvent('odysseus:loadout-changed')); } catch (_) { /* no DOM events */ }
  return sid;
}

// ── Quick actions ───────────────────────────────────────────────────────────
let profileCache = { at: 0, rows: null };
async function profileRows(force = false) {
  if (!force && profileCache.rows && Date.now() - profileCache.at < 30000) return profileCache.rows;
  const r = await call('/api/agents/profiles');
  profileCache = { at: Date.now(), rows: r.ok ? (r.body?.profiles || []) : [] };
  return profileCache.rows;
}

/** The chat's project and open document, when the context strip exposes them. */
function chatContext() {
  const source = window.chatSettingsModule?.getContext?.() || window.chatContext || {};
  const project = typeof source.project === 'string' ? source.project : (source.project?.name || '');
  const document_ = typeof source.document === 'string' ? source.document : (source.document?.title || '');
  return { project, document: document_ };
}

/** Fill `{project}` and `{document}`; a placeholder with no value stays as written. */
export function fillPlaceholders(prompt, ctx = chatContext()) {
  return String(prompt || '')
    .replace(/\{project\}/g, ctx.project || '{project}')
    .replace(/\{document\}/g, ctx.document || '{document}');
}

/** Put `prompt` in the composer, after anything already typed. */
export function fillComposer(prompt) {
  const ta = $('message');
  if (!ta) return false;
  const text = fillPlaceholders(prompt);
  ta.value = ta.value.trim() ? `${ta.value.replace(/\s+$/, '')}\n${text}` : text;
  try { ta.dispatchEvent(new Event('input', { bubbles: true })); } catch (_) { /* ignore */ }
  ta.focus();
  try { ta.setSelectionRange(ta.value.length, ta.value.length); } catch (_) { /* ignore */ }
  return true;
}

/** Chips for the agent's quick actions in `container`; hidden when it has none. */
export async function renderQuickActions(container, agentName, { onPick } = {}) {
  if (!container) return [];
  container.textContent = '';
  container.hidden = true;
  if (!agentName) return [];
  const rows = await profileRows();
  const row = rows.find((p) => p.name === agentName);
  const actions = (row?.quick_actions || []).filter((a) => a && a.label && a.prompt);
  if (!actions.length) return [];
  container.classList.add('aa-chips');
  actions.forEach((a) => {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'aa-chip';
    chip.textContent = a.label;
    chip.title = fillPlaceholders(a.prompt);
    chip.addEventListener('click', () => { if (typeof onPick === 'function') onPick(a); else fillComposer(a.prompt); });
    container.appendChild(chip);
  });
  container.hidden = false;
  return actions;
}

// ── Assign sheet ────────────────────────────────────────────────────────────
const APPLIED_LABEL = {
  no_workspace: 'Running without files',
  read_only: 'Analysis only, no edits',
};

function parentName(id) {
  const rows = sessions()?.getSessions?.() || [];
  return (rows.find((s) => s.id === id) || {}).name || '';
}

function sheetHtml(st, agents) {
  const options = agents.map((p) => `<option value="${esc(p.name)}"${p.name === st.agent ? ' selected' : ''}>${esc(p.name)}</option>`).join('');
  const chats = (sessions()?.getSessions?.() || []).filter((s) => !s.archived).slice(0, 60)
    .map((s) => `<option value="${esc(s.id)}"${s.id === st.parent ? ' selected' : ''}>${esc(s.name || s.id)}</option>`).join('');
  return `<div class="aa-sheet" role="group" aria-label="${ASSIGN_LABEL}">
    <div class="aa-head"><strong class="aa-title">${ASSIGN_LABEL}</strong>${st.closable ? '<button type="button" class="aa-x" data-aa="close" aria-label="Close">✕</button>' : ''}</div>
    <label class="aa-field"><span>Agent</span>
      <select class="aa-input" data-aa-field="agent"><option value="">Default (this chat's setup)</option>${options}</select></label>
    <label class="aa-field"><span>Task</span>
      <textarea class="aa-input aa-task" data-aa-field="task" rows="4" placeholder="Describe the whole task. The agent starts with no other context.">${esc(st.task)}</textarea></label>
    <div class="aa-excerpt" data-aa-part="excerpt"${st.excerpt || st.excerptOpen ? '' : ' hidden'}>
      <label class="aa-field"><span>Excerpt the agent can read</span>
        <textarea class="aa-input" data-aa-field="excerpt" rows="4" placeholder="Paste the part of the document the task needs.">${esc(st.excerpt)}</textarea></label>
    </div>
    <p class="aa-summary" data-aa-part="summary" aria-live="polite"></p>
    <div class="aa-blocker" data-aa-part="blocker" role="alert" hidden></div>
    <p class="aa-applied" data-aa-part="applied" hidden></p>
    <label class="aa-field aa-parent"><span>Result returns to</span>
      <select class="aa-input" data-aa-field="parent"><option value="">No chat (stand-alone)</option>${chats}</select></label>
    <p class="aa-return" data-aa-part="return"></p>
    <details class="aa-adv"${st.advancedOpen ? ' open' : ''}>
      <summary>Advanced</summary>
      <div class="aa-adv-body">
        <label class="aa-field"><span>Model override</span>
          <input class="aa-input" data-aa-field="model" data-model-override-control value="${esc(st.model)}" placeholder="optional, e.g. qwen3 or model@endpoint"></label>
        <label class="aa-field"><span>Code folder</span>
          <input class="aa-input" data-aa-field="workspace" value="${esc(st.workspace)}" placeholder="optional; the chat's folder, or the one the task names"></label>
        <div class="aa-field" data-aa-part="tools"></div>
      </div>
    </details>
    <div class="aa-actions"><button type="button" class="aa-btn aa-btn-primary" data-aa="go">Assign</button><span class="aa-msg" data-aa-part="msg" role="status"></span></div>
  </div>`;
}

/**
 * Mount the Assign sheet in `container`.
 * @param {HTMLElement} container
 * @param {{agent?: string, task?: string, parent?: string, closable?: boolean,
 *   initial?: object, onStarted?: (result: object) => void, onClose?: () => void,
 *   onChange?: (snapshot: object) => void}} opts
 * @returns {{destroy: () => void, snapshot: () => object, apply: (action: object) => void,
 *   refresh: () => Promise<void>}}
 */
export function mountAssignSheet(container, opts = {}) {
  const st = Object.assign({
    agent: opts.agent || '', task: opts.task || '', parent: opts.parent != null ? opts.parent : currentSessionId(),
    model: '', workspace: '', excerpt: '', excerptOpen: false, advancedOpen: false,
    requires: [], extra: [], startAnyway: false, closable: !!opts.closable,
  }, opts.initial || {});
  st.closable = !!opts.closable;
  // One sheet per container: mounting again (a re-render) retires the old one's listeners.
  if (container.__aaSheet) container.__aaSheet.destroy();
  let agents = [];
  let access = null;
  let blocker = null;     // from the last access check
  let refusal = null;     // from the last refused launch; stays until the person changes something
  const shown = () => refusal || blocker;
  let seq = 0;
  let timer = null;
  let busy = false;
  let dead = false;

  const part = (name) => container.querySelector(`[data-aa-part="${name}"]`);
  const field = (name) => container.querySelector(`[data-aa-field="${name}"]`);
  const fullTask = () => {
    const task = st.task.trim();
    const excerpt = st.excerpt.trim();
    return excerpt ? `${task}\n\nExcerpt:\n\`\`\`\n${excerpt}\n\`\`\`` : task;
  };
  const notify = () => { refusal = null; if (typeof opts.onChange === 'function') opts.onChange(snapshot()); };
  const snapshot = () => ({ agent: st.agent, task: st.task, parent: st.parent, model: st.model,
    workspace: st.workspace, excerpt: st.excerpt, excerptOpen: st.excerptOpen, advancedOpen: st.advancedOpen,
    requires: [...st.requires], extra: [...st.extra], startAnyway: st.startAnyway });

  function renderSummary() {
    const el = part('summary');
    if (!el) return;
    if (!st.agent) { el.textContent = "Runs with this chat's own setup and tools."; return; }
    // The line below the summary names the chat; drop the server's generic copy of it.
    el.textContent = (access?.assign?.summary || 'Checking what this agent can do…').replace(/\s*Results return to this chat\.$/, '');
  }
  function renderReturn() {
    const el = part('return');
    if (!el) return;
    const name = st.parent ? parentName(st.parent) : '';
    el.textContent = st.parent ? `Results return to ${name ? `“${name}”` : 'this chat'}.` : 'Results stay in the agent\'s own chat.';
  }
  function renderTools() {
    const el = part('tools');
    if (!el) return;
    const adv = access?.advanced;
    if (!adv) { el.innerHTML = ''; return; }
    const tools = (adv.tools || []).slice(0, 40).map(esc).join(', ');
    const skills = Array.isArray(adv.skills) ? adv.skills.map(esc).join(', ') || 'none' : esc(adv.skills);
    el.innerHTML = `<span>What it can use</span><p class="aa-tools"><b>${esc(adv.tool_access)}</b>${adv.tool_count ? ` (${adv.tool_count})` : ''}: ${tools || 'none'}${(adv.tools || []).length > 40 ? ', …' : ''}</p>`
      + `<p class="aa-tools"><b>Skills</b>: ${skills} · <b>Commands</b>: ${esc(adv.shell)} · <b>Approvals</b>: ${esc(adv.approval)}</p>`
      + ((adv.withheld || []).length ? `<p class="aa-tools"><b>Not available when assigned</b>: ${adv.withheld.map((w) => `${esc(w.tool)} (${esc(w.reason)})`).join('; ')}</p>` : '');
  }
  function renderApplied() {
    const el = part('applied');
    if (!el) return;
    const bits = [];
    st.requires.forEach((r) => { if (APPLIED_LABEL[r]) bits.push(APPLIED_LABEL[r]); });
    if (st.workspace) bits.push('Using the chosen code folder');
    if (st.extra.length) bits.push('Extra access for this run only');
    if (st.startAnyway) bits.push('Starting without the document check');
    el.hidden = !bits.length;
    el.innerHTML = bits.length ? `${bits.map(esc).join(' · ')} <button type="button" class="aa-link" data-aa="reset">Undo</button>` : '';
  }
  function actionLabel(a) {
    return a.label || a.id;
  }
  function renderBlocker() {
    const el = part('blocker');
    if (!el) return;
    const b = shown();
    if (!b) { el.hidden = true; el.innerHTML = ''; return; }
    const buttons = (b.actions || []).map((a, i) => {
      const hint = a.extra_tools ? ` title="Lets ${esc(st.agent)} use ${esc(a.extra_tools.join(', '))} for this task only"` : '';
      return `<button type="button" class="aa-btn aa-btn-sm" data-aa-action="${esc(a.id)}" data-aa-index="${i}"${hint}>${esc(actionLabel(a))}</button>`;
    }).join('');
    el.hidden = false;
    el.innerHTML = `<p class="aa-blocker-text">${esc(b.summary)}</p><div class="aa-blocker-actions">${buttons}</div><div class="aa-blocker-extra" data-aa-part="blocker-extra"></div>`;
  }
  function renderAll() {
    renderSummary(); renderReturn(); renderTools(); renderApplied(); renderBlocker();
    const go = container.querySelector('[data-aa="go"]');
    if (go) go.textContent = st.startAnyway && shown() ? 'Assign anyway' : 'Assign';
  }

  async function refresh() {
    clearTimeout(timer);
    const mine = ++seq;
    if (!st.agent) { access = null; blocker = null; renderAll(); return; }
    const params = new URLSearchParams();
    if (st.parent) params.set('session_id', st.parent);
    const task = fullTask();
    if (task) params.set('task', task);
    if (st.workspace) params.set('workspace', st.workspace);
    if (st.requires.length) params.set('requires', st.requires.join(','));
    if (st.extra.length) params.set('extra_tools', st.extra.join(','));
    const r = await call(`/api/agents/profiles/${encodeURIComponent(st.agent)}/access?${params}`);
    if (dead || mine !== seq) return;
    if (r.ok) {
      access = r.body;
      blocker = st.startAnyway && access?.assign?.blocker?.code === 'DOCUMENT_UNREADABLE' ? null : (access?.assign?.blocker || null);
    } else {
      access = null; blocker = null;
      const el = part('summary');
      if (el) el.textContent = errorText(r, 'Could not check what this agent can do.');
    }
    renderAll();
    if (!r.ok) { const el = part('summary'); if (el) el.textContent = errorText(r, 'Could not check what this agent can do.'); }
  }
  const later = () => { clearTimeout(timer); timer = setTimeout(refresh, ACCESS_DEBOUNCE_MS); };

  function resetOptions() {
    st.requires = []; st.extra = []; st.workspace = ''; st.startAnyway = false;
    const ws = field('workspace'); if (ws) ws.value = '';
  }

  function chooseFolder(action) {
    const host = part('blocker-extra');
    if (!host) return;
    const opts_ = (action.candidates || []).map((c) => `<option value="${esc(c.path)}" title="${esc(c.path)}">${esc(c.name)}</option>`).join('');
    host.innerHTML = `<label class="aa-field"><span>Code folder</span><select class="aa-input" data-aa-pick="workspace">${opts_}</select></label>
      <button type="button" class="aa-btn aa-btn-sm aa-btn-primary" data-aa="use-folder">Use this folder</button>`;
    host.querySelector('select')?.focus();
  }

  function apply(action) {
    if (!action) return;
    switch (action.id) {
      case 'choose_workspace': chooseFolder(action); return;
      case 'run_without_files':
      case 'read_only':
        (action.requires || []).forEach((r) => { if (!st.requires.includes(r)) st.requires.push(r); });
        break;
      case 'allow_for_this_run':
        (action.extra_tools || []).forEach((t) => { if (!st.extra.includes(t)) st.extra.push(t); });
        break;
      case 'analysis_only':
        if (action.task_prefix && !st.task.startsWith(action.task_prefix)) {
          st.task = action.task_prefix + st.task;
          const ta = field('task'); if (ta) ta.value = st.task;
        }
        break;
      case 'attach_excerpt': {
        st.excerptOpen = true;
        const box = part('excerpt'); if (box) box.hidden = false;
        field('excerpt')?.focus();
        return;
      }
      case 'choose_accessible_file': {
        const input = document.createElement('input');
        input.type = 'file';
        input.accept = '.txt,.md,.markdown,.csv,.json,text/*';
        input.addEventListener('change', async () => {
          const file = input.files && input.files[0];
          if (!file) return;
          try {
            const text = (await file.text()).slice(0, EXCERPT_LIMIT);
            st.excerpt = st.excerpt ? `${st.excerpt}\n\n${text}` : text;
            st.excerptOpen = true;
            const box = part('excerpt'); if (box) box.hidden = false;
            const ta = field('excerpt'); if (ta) ta.value = st.excerpt;
            notify();
            await refresh();
          } catch (_) { toast('That file could not be read as text.', 'error'); }
        });
        input.click();
        return;
      }
      case 'pick_another_agent': {
        const sel = field('agent');
        sel?.focus();
        try { sel?.showPicker?.(); } catch (_) { /* focus is enough */ }
        return;
      }
      case 'start_anyway': st.startAnyway = true; break;
      case 'open_agents': try { window.agentsDashboard?.open?.(); } catch (_) { /* ignore */ } return;
      case 'wait': toast('Try again when one of the running agents has finished.'); return;
      default: return;
    }
    notify();
    refresh();
  }

  async function go() {
    if (busy) return;
    const msg = part('msg');
    const say = (t) => { if (msg) msg.textContent = t; };
    if (!st.task.trim()) { say('Describe the task first.'); field('task')?.focus(); return; }
    busy = true;
    clearTimeout(timer);
    const btn = container.querySelector('[data-aa="go"]');
    if (btn) btn.disabled = true;
    say('Assigning…');
    try {
      let parent = st.parent;
      if (!parent && !currentSessionId() && sessions()?.getPendingChat?.() && sessions()?.materializePendingSession) {
        try { await sessions().materializePendingSession(); parent = currentSessionId(); st.parent = parent; } catch (_) { /* stand-alone */ }
      }
      const r = await postJson('/api/agents/launch', {
        task: fullTask(), profile: st.agent, parent_session: parent || '', model: st.model.trim(),
        workspace: st.workspace.trim(), requires: st.requires, extra_tools: st.extra,
        start_anyway: st.startAnyway || (!!st.excerpt.trim() || undefined),
      });
      if (r.status === 409 && r.body?.blocker) {
        // The server's refusal is newer than any check still on its way.
        clearTimeout(timer); seq += 1;
        refusal = r.body.blocker;
        renderAll();
        say('');
        return;
      }
      if (!r.ok) { say(errorText(r, 'The task was not assigned.')); return; }
      say('');
      const back = parent ? parentName(parent) : '';
      toast(`Task assigned${st.agent ? ` to ${st.agent}` : ''}. ${parent ? `Its result returns to ${back ? `“${back}”` : 'this chat'}.` : 'Its result stays in its own chat.'}`, 'success');
      if (typeof opts.onStarted === 'function') opts.onStarted(r.body || {});
    } catch (err) {
      say(err && err.message ? err.message : 'The task was not assigned.');
    } finally {
      busy = false;
      if (btn) btn.disabled = false;
    }
  }

  function onClick(e) {
    const act = e.target.closest('[data-aa-action]');
    if (act && container.contains(act)) {
      apply((shown()?.actions || [])[Number(act.dataset.aaIndex)]);
      return;
    }
    const b = e.target.closest('[data-aa]');
    if (!b || !container.contains(b)) return;
    const what = b.dataset.aa;
    if (what === 'go') go();
    else if (what === 'close') { if (typeof opts.onClose === 'function') opts.onClose(); }
    else if (what === 'reset') { resetOptions(); notify(); renderApplied(); refresh(); }
    else if (what === 'use-folder') {
      const picked = container.querySelector('[data-aa-pick="workspace"]')?.value || '';
      if (picked) { st.workspace = picked; const ws = field('workspace'); if (ws) ws.value = picked; notify(); refresh(); }
    }
  }
  function onInput(e) {
    const name = e.target?.dataset?.aaField;
    if (!name) return;
    if (name === 'task') { st.task = e.target.value; notify(); later(); }
    else if (name === 'excerpt') { st.excerpt = e.target.value; notify(); later(); }
    else if (name === 'model') { st.model = e.target.value; notify(); }
    else if (name === 'workspace') { st.workspace = e.target.value; notify(); later(); }
  }
  function onChange(e) {
    const name = e.target?.dataset?.aaField;
    if (name === 'agent') {
      st.agent = e.target.value;
      resetOptions();
      blocker = null; access = null;
      notify(); renderAll(); refresh();
    } else if (name === 'parent') { st.parent = e.target.value; notify(); renderReturn(); refresh(); }
  }
  function onToggle(e) {
    if (e.target.matches?.('.aa-adv')) { st.advancedOpen = e.target.open; notify(); }
  }

  container.innerHTML = '';
  const host = document.createElement('div');
  host.className = 'aa-host';
  container.appendChild(host);
  // The sheet's own container is what listeners attach to, so remounting into a
  // rebuilt container never leaves a handler on a detached node.
  container.addEventListener('click', onClick);
  container.addEventListener('input', onInput);
  container.addEventListener('change', onChange);
  container.addEventListener('toggle', onToggle, true);

  const draw = (list) => {
    agents = list || [];
    host.innerHTML = sheetHtml(st, agents);
    renderAll();
  };
  draw(profileCache.rows || []);
  profileRows(true).then((list) => {
    if (dead) return;
    const focused = document.activeElement;
    const typing = focused && container.contains(focused) && focused.matches?.('textarea, input');
    // Redrawing under the cursor would drop what is being typed; the agent list
    // only matters when it has changed, and the picker is rebuilt in place.
    if (typing) {
      const sel = field('agent');
      if (sel) sel.innerHTML = `<option value="">Default (this chat's setup)</option>${list.map((p) => `<option value="${esc(p.name)}"${p.name === st.agent ? ' selected' : ''}>${esc(p.name)}</option>`).join('')}`;
      agents = list;
    } else draw(list);
    refresh();
  });
  refresh();

  const controller = {
    destroy() {
      dead = true; clearTimeout(timer);
      if (container.__aaSheet === controller) container.__aaSheet = null;
      container.removeEventListener('click', onClick);
      container.removeEventListener('input', onInput);
      container.removeEventListener('change', onChange);
      container.removeEventListener('toggle', onToggle, true);
    },
    snapshot, apply, refresh,
  };
  container.__aaSheet = controller;
  return controller;
}

/** Open the Assign sheet as a dialog over the page. */
export function openAssign({ agent = '', task = '', initial = null, onStarted } = {}) {
  closeAssign();
  const returnTo = document.activeElement;
  const overlay = document.createElement('div');
  overlay.className = 'aa-overlay';
  overlay.id = 'aa-overlay';
  const dialog = document.createElement('div');
  dialog.className = 'aa-dialog';
  dialog.setAttribute('role', 'dialog');
  dialog.setAttribute('aria-modal', 'true');
  dialog.setAttribute('aria-label', ASSIGN_LABEL);
  overlay.appendChild(dialog);
  document.body.appendChild(overlay);
  let sheet = null;
  const close = () => {
    sheet?.destroy();
    overlay.remove();
    document.removeEventListener('keydown', onKey, true);
    try { returnTo?.focus?.(); } catch (_) { /* element gone */ }
  };
  const onKey = (e) => { if (e.key === 'Escape') { e.stopPropagation(); close(); } };
  document.addEventListener('keydown', onKey, true);
  overlay.addEventListener('mousedown', (e) => { if (e.target === overlay) close(); });
  sheet = mountAssignSheet(dialog, {
    agent, task, initial, closable: true, onClose: close,
    onStarted: (result) => { close(); if (typeof onStarted === 'function') onStarted(result); },
  });
  (dialog.querySelector('[data-aa-field="task"]'))?.focus();
  return { close, sheet };
}
export function closeAssign() { $('aa-overlay')?.remove(); }

/** Handle a click on a `[data-agent-act]` button. Returns true when it was one. */
export function handleActionClick(event, { openChat, onStarted } = {}) {
  const b = event.target.closest?.('[data-agent-act]');
  if (!b) return false;
  event.preventDefault();
  event.stopPropagation();
  const name = b.dataset.agent || '';
  if (b.dataset.agentAct === 'talk') talkToAgent(name, { open: openChat });
  else if (b.dataset.agentAct === 'assign') openAssign({ agent: name, onStarted });
  return true;
}

/** Apply a stored blocker's recovery action (Today): open the sheet with it set. */
export function applyBlockerAction(action, { task = '', agent = '' } = {}) {
  if (!action) return;
  if (action.id === 'open_agents') { window.agentsDashboard?.open?.(); return; }
  const { sheet } = openAssign({ agent, task });
  if (action.id !== 'pick_another_agent') sheet.apply(action);
  else sheet.apply(action);
}

const agentActions = {
  talkToAgent, openAssign, closeAssign, mountAssignSheet, renderQuickActions, fillComposer, fillPlaceholders,
  actionButtonsHtml, actionButtonsEl, handleActionClick, applyBlockerAction, TALK_LABEL, ASSIGN_LABEL,
};
window.agentActions = agentActions;
export default agentActions;
