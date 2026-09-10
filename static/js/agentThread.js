/* agentThread.js — folding and summaries for tool-call timelines.
 *
 * A turn that makes hundreds of tool calls (the agent tool-call ceiling is
 * 500) used to render as hundreds of stacked cards with nothing to tell the
 * reader what happened. This module owns the thread-level chrome that both
 * the live stream (chat.js) and history reload (chatRenderer.js) call after
 * they append a node:
 *
 *   - a summary bar: "37 tool calls · 2 failed · read_file ×20, grep ×9 …"
 *     with Expand all / Collapse all;
 *   - automatic folding after `chat_tool_fold_after` calls (Settings > Tools
 *     > Agent; default 12; 0 = never): the first two and the last three nodes
 *     stay visible, the rest collapse into one "… N more" row. The newest
 *     (running) node is always in the visible tail while streaming.
 *
 * Nothing here holds per-node listeners: a single delegated click handler on
 * document.body drives the buttons and the gap row, matching how chat.js
 * handles node expand/collapse.
 */

const DEFAULT_FOLD_AFTER = 12;
const KEEP_HEAD = 2;
const KEEP_TAIL = 3;

let _foldAfter = DEFAULT_FOLD_AFTER;
let _settingsLoaded = false;

function _esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

export function foldThreshold() {
  return _foldAfter;
}

/** Update the threshold at runtime (the settings panel calls this on save). */
export function setFoldThreshold(n) {
  const v = parseInt(n, 10);
  _foldAfter = isNaN(v) ? DEFAULT_FOLD_AFTER : Math.max(0, Math.min(500, v));
  document.querySelectorAll('.agent-thread').forEach((t) => refreshThread(t));
}

async function _loadSettings() {
  if (_settingsLoaded) return;
  _settingsLoaded = true;
  try {
    const res = await fetch('/api/auth/settings', { credentials: 'same-origin' });
    if (!res.ok) return;
    const s = await res.json();
    if (s && s.chat_tool_fold_after != null) {
      const v = parseInt(s.chat_tool_fold_after, 10);
      if (!isNaN(v)) _foldAfter = Math.max(0, Math.min(500, v));
    }
  } catch (e) { /* offline/anon: keep the default */ }
}

function _nodes(thread) {
  return Array.from(thread.children).filter((c) => c.classList.contains('agent-thread-node'));
}

/**
 * Recompute the summary bar and fold state of one `.agent-thread`.
 * Safe to call after every node append/update; it is O(nodes) and touches
 * only class names plus one small innerHTML.
 */
export function refreshThread(thread) {
  if (!thread || !thread.classList || !thread.classList.contains('agent-thread')) return;
  const nodes = _nodes(thread);
  const total = nodes.length;
  let summary = thread.querySelector(':scope > .agent-thread-summary');
  let gap = thread.querySelector(':scope > .agent-thread-gap');

  // Tiny threads need no chrome at all.
  if (total < 3) {
    if (summary) summary.remove();
    if (gap) gap.remove();
    nodes.forEach((n) => n.classList.remove('agent-thread-hidden'));
    thread.classList.remove('folded');
    return;
  }

  let failed = 0, running = 0;
  const counts = {};
  for (const n of nodes) {
    if (n.classList.contains('error')) failed++;
    if (n.classList.contains('running')) running++;
    const t = (n.querySelector('.agent-thread-tool')?.textContent || '').trim();
    if (t) counts[t] = (counts[t] || 0) + 1;
  }
  const entries = Object.entries(counts).sort((a, b) => b[1] - a[1]);
  const top = entries.slice(0, 4).map(([t, c]) => (c > 1 ? `${t} ×${c}` : t)).join(', ');
  const more = Math.max(0, entries.length - 4);

  const foldable = _foldAfter > 0 && total > _foldAfter;
  const fold = foldable && thread.dataset.fold !== 'open';
  nodes.forEach((n, i) => {
    const hide = fold && i >= KEEP_HEAD && i < total - KEEP_TAIL;
    n.classList.toggle('agent-thread-hidden', hide);
  });
  thread.classList.toggle('folded', fold);
  const hidden = fold ? Math.max(0, total - KEEP_HEAD - KEEP_TAIL) : 0;

  if (!summary) {
    summary = document.createElement('div');
    summary.className = 'agent-thread-summary';
    thread.insertBefore(summary, thread.firstChild);
  }
  const parts = [`<span class="ats-count">${total} tool call${total === 1 ? '' : 's'}</span>`];
  if (running) parts.push(`<span class="ats-running">${running} running</span>`);
  if (failed) parts.push(`<span class="ats-failed">${failed} failed</span>`);
  if (top) parts.push(`<span class="ats-tools">${_esc(top)}${more ? ` +${more} more` : ''}</span>`);
  const actions = [];
  if (hidden) actions.push(`<button type="button" class="ats-btn" data-ats="unfold" title="Show every tool call in this turn">Show all ${total}</button>`);
  else if (foldable) actions.push(`<button type="button" class="ats-btn" data-ats="fold" title="Fold the middle of this timeline">Fold</button>`);
  actions.push(`<button type="button" class="ats-btn" data-ats="expand" title="Open every card">Expand all</button>`);
  actions.push(`<button type="button" class="ats-btn" data-ats="collapse" title="Close every card">Collapse all</button>`);
  summary.innerHTML = parts.join(' <span class="ats-sep">·</span> ') + `<span class="ats-actions">${actions.join('')}</span>`;

  if (hidden) {
    if (!gap) {
      gap = document.createElement('div');
      gap.className = 'agent-thread-gap';
      gap.setAttribute('role', 'button');
      gap.tabIndex = 0;
    }
    gap.textContent = `… ${hidden} more tool call${hidden === 1 ? '' : 's'} folded — click to show`;
    const anchor = nodes[total - KEEP_TAIL];
    if (gap.nextSibling !== anchor) thread.insertBefore(gap, anchor);
  } else if (gap) {
    gap.remove();
  }
}

/** Refresh every thread on the page (after a bulk history render). */
export function refreshAllThreads(root) {
  (root || document).querySelectorAll('.agent-thread').forEach((t) => refreshThread(t));
}

function _onClick(e) {
  const btn = e.target.closest('[data-ats]');
  const gapEl = e.target.closest('.agent-thread-gap');
  const thread = (btn || gapEl)?.closest('.agent-thread');
  if (!thread) return;
  const action = gapEl ? 'unfold' : btn.dataset.ats;
  if (action === 'unfold') thread.dataset.fold = 'open';
  else if (action === 'fold') thread.dataset.fold = 'auto';
  else if (action === 'expand') {
    thread.dataset.fold = 'open';
    _nodes(thread).forEach((n) => { if (!n.classList.contains('running')) n.classList.add('open'); });
  } else if (action === 'collapse') {
    _nodes(thread).forEach((n) => n.classList.remove('open'));
  }
  refreshThread(thread);
  e.preventDefault();
  e.stopPropagation();
}

if (typeof document !== 'undefined' && !window.__odysseus_thread_summary_bound) {
  document.body.addEventListener('click', _onClick);
  document.body.addEventListener('keydown', (e) => {
    if ((e.key === 'Enter' || e.key === ' ') && e.target.classList?.contains('agent-thread-gap')) _onClick(e);
  });
  window.__odysseus_thread_summary_bound = true;
  _loadSettings();
}

const agentThread = { refreshThread, refreshAllThreads, foldThreshold, setFoldThreshold };
export default agentThread;
window.agentThread = agentThread;
