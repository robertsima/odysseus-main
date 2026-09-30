/* taskChecklist.js — the open chat's task checklist, above the composer.
 *
 * An agent keeps a checklist of the steps of the request it is working on
 * (update_plan / todowrite → src/task_checklist.py). The agent re-reads it
 * every turn, but the person never saw it: `plan_update` events only reached
 * localStorage. This shows it live, the way Claude Code shows its todo list,
 * so a multi-step request can be followed (and a dropped step noticed) without
 * reading every tool card.
 *
 * Sources: `odysseus:plan-update` (dispatched by chat.js for the chat on
 * screen) while a turn runs, and GET /api/agents/sessions/{id}/checklist when a
 * chat is opened or a turn ends. Checklist text is written by an agent, so it
 * is escaped before it touches the page.
 */

const COLLAPSE_KEY = 'odysseus-checklist-collapsed';
const ITEM_RE = /^\s*[-*+]\s*\[([ xX~-])\]\s*(.+?)\s*$/;

let _sid = null;
let _items = [];
let _seq = 0;

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function currentSession() {
  try { return window.sessionModule?.getCurrentSessionId?.() || null; } catch (_) { return null; }
}

/** `- [ ]` / `- [x]` lines as {text, done}; `[~]` and `[-]` (dropped) count as done. */
export function parsePlan(plan) {
  const out = [];
  for (const line of String(plan || '').split('\n')) {
    const m = ITEM_RE.exec(line);
    if (m) out.push({ text: m[2], done: m[1] !== ' ' });
  }
  return out;
}

function collapsed() {
  try { return localStorage.getItem(COLLAPSE_KEY) === '1'; } catch (_) { return false; }
}

function setCollapsed(on) {
  try { localStorage.setItem(COLLAPSE_KEY, on ? '1' : '0'); } catch (_) {}
}

export function checklistHtml(items, isCollapsed) {
  const done = items.filter((it) => it.done).length;
  const current = items.find((it) => !it.done);
  const rows = items.map((it) => {
    const cls = it.done ? 'tc-done' : (it === current ? 'tc-current' : 'tc-open');
    const mark = it.done ? '✓' : (it === current ? '▸' : '○');
    return `<li class="tc-item ${cls}"><span class="tc-mark" aria-hidden="true">${mark}</span><span class="tc-text">${esc(it.text)}</span></li>`;
  }).join('');
  return `<div class="tc-head">
      <button type="button" class="tc-toggle" data-tc="toggle" aria-expanded="${isCollapsed ? 'false' : 'true'}" aria-controls="tc-list">
        <span class="tc-caret" aria-hidden="true">▸</span>
        <span class="tc-label">Checklist</span>
        <span class="tc-count">${done}/${items.length}</span>
        ${current ? `<span class="tc-now" title="${esc(current.text)}">${esc(current.text)}</span>` : ''}
      </button>
    </div>
    <ol id="tc-list" class="tc-list"${isCollapsed ? ' hidden' : ''}>${rows}</ol>`;
}

function render() {
  const box = document.getElementById('task-checklist');
  if (!box) return;
  // Shown while there is something left to do; a finished list steps aside.
  if (!_items.length || _items.every((it) => it.done)) {
    box.hidden = true;
    box.innerHTML = '';
    return;
  }
  box.hidden = false;
  box.innerHTML = checklistHtml(_items, collapsed());
}

function show(sid, plan) {
  if (sid && sid !== currentSession()) return;
  _sid = sid || currentSession();
  _items = parsePlan(plan);
  render();
}

async function refresh(sessionId) {
  const sid = sessionId || currentSession();
  const seq = ++_seq;
  if (!sid) { _items = []; render(); return; }
  if (sid !== _sid) { _sid = sid; _items = []; render(); }
  try {
    const res = await fetch(`/api/agents/sessions/${encodeURIComponent(sid)}/checklist`, { credentials: 'same-origin' });
    if (!res.ok) return;
    const data = await res.json();
    // A later refresh (another chat opened meanwhile) wins.
    if (seq !== _seq || sid !== currentSession()) return;
    show(sid, data.plan || '');
  } catch (_) { /* the panel is a convenience; the agent's copy is authoritative */ }
}

document.addEventListener('click', (e) => {
  const btn = e.target.closest('[data-tc="toggle"]');
  if (!btn) return;
  setCollapsed(!collapsed());
  render();
});

// The agent ticked a step or rewrote the list during the running turn.
document.addEventListener('odysseus:plan-update', (e) => show(e.detail?.sessionId, e.detail?.plan));
// A chat was opened or re-rendered.
document.addEventListener('odysseus:history-rendered', (e) => refresh(e.detail && e.detail.sessionId));
// A turn ended: todowrite and background turns update the stored copy too.
window.addEventListener('odysseus:chat-busy-change', (e) => {
  if (e.detail && e.detail.active === false) setTimeout(() => refresh(), 400);
});

window.taskChecklist = { refresh };
export default { refresh };
export const _forTests = { parsePlan, checklistHtml, esc };
