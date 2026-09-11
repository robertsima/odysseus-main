/* workbench.js — the agent observability window.
 *
 * One adjustable window (drag / resize / dock / minimize like every other
 * tool window) with four views:
 *   Activity  — the unified agent activity feed: every Odysseus turn,
 *               Claude Code run, sub-agent (send_to_session mode=agent),
 *               model pipeline and background-job follow-up, live over SSE.
 *   Changes   — what a run (or any approved repository) changed: per-file
 *               stats, unified or side-by-side "old vs new" diff.
 *   Commits   — commits since a run started (or the repository log).
 *   Pull Requests — the worktree repository's PRs with checks, files,
 *               comments and reviews; post comments / reviews from here.
 *
 * It also owns the live "agent run" cards inside the chat: when an activity
 * event announces a sub-process for the current session (Claude Code, a
 * sub-agent, a pipeline, a background job) a compact card appears in the
 * conversation and fills in as the run progresses, with "Open in Workbench"
 * for the full transcript / diff.
 *
 * Everything the window shows comes from /api/workbench/* and
 * /api/claude-code/tasks/*; those routes are admin-only, so a non-admin user
 * never sees the rail button (the settings probe below hides it).
 */

import Modals from './modalManager.js';
import { esc, showToast } from './ui.js';
import { makeWindowDraggable } from './windowDrag.js';
import { snapModalToZone } from './tileManager.js';
import { applyRightDock } from './modalSnap.js';
import { renderDiffText, renderFileTable, parseUnifiedDiff, diffStats } from './diffView.js';

const PREFS_KEY = 'odysseus-workbench-prefs';
const MAX_EVENTS = 1500;
const CHAT_CARD_SOURCES = new Set(['claude_code', 'session', 'pipeline', 'bg_job', 'worktree']);
const SOURCE_LABEL = {
  odysseus: 'Odysseus', claude_code: 'Claude Code', session: 'Sub-agent', pipeline: 'Pipeline',
  bg_job: 'Background job', worktree: 'Worktree', system: 'System',
};
const KIND_ICON = {
  run_started: '>', run_finished: '#', message: '"', tool_start: '*', tool_result: '*',
  file_change: '±', commit: '@', status: '·', error: '!', note: '~',
};

const state = {
  inited: false,
  enabled: true,
  autoOpen: true,
  prefs: { tab: 'activity', mode: 'split', repo: '', scope: 'session', filter: '' },
  sessionId: null,
  es: null,
  esKey: null,
  lastSeq: 0,
  events: [],
  runs: new Map(),          // run_id → run summary (built from events)
  paused: false,
  // Changes / Commits view context: either a repository path or a Claude Code task.
  repoCtx: { path: '', base: '', taskId: null, label: '' },
  files: [],
  commits: [],
  selectedFile: null,
  selectedCommit: null,
  diffText: '',
  pr: { config: null, list: [], selected: null, detail: null, stateFilter: 'open' },
  chatCards: new Map(),     // run_id → element
};

// ── prefs ─────────────────────────────────────────────────────────────────
function loadPrefs() {
  try {
    const raw = localStorage.getItem(PREFS_KEY);
    if (raw) Object.assign(state.prefs, JSON.parse(raw) || {});
  } catch (_) {}
}
function savePrefs() {
  try { localStorage.setItem(PREFS_KEY, JSON.stringify(state.prefs)); } catch (_) {}
}

// ── small helpers ─────────────────────────────────────────────────────────
const $ = (id) => document.getElementById(id);
function currentSessionId() {
  try { return window.sessionModule?.getCurrentSessionId?.() || null; } catch (_) { return null; }
}
function fmtTime(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}
function fmtDur(a, b) {
  if (!a || !b) return '';
  const s = Math.max(0, Math.round(b - a));
  if (s < 60) return `${s}s`;
  return `${Math.floor(s / 60)}m ${s % 60}s`;
}
async function api(path, opts = {}) {
  const r = await fetch(path, Object.assign({ credentials: 'same-origin' }, opts));
  let body = null;
  try { body = await r.json(); } catch (_) {}
  if (!r.ok) {
    const msg = (body && (body.detail || body.error)) || `${r.status} ${r.statusText}`;
    const err = new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
    err.status = r.status;
    throw err;
  }
  return body;
}
function post(path, data) {
  return api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data || {}) });
}
function fillComposer(text) {
  const ta = $('message');
  if (!ta) return;
  const cur = ta.value.trim();
  ta.value = (cur ? cur + '\n\n' : '') + text;
  ta.focus();
  try { ta.dispatchEvent(new Event('input', { bubbles: true })); } catch (_) {}
}

// ── activity model ────────────────────────────────────────────────────────
function ingest(ev, { live = true } = {}) {
  if (!ev || typeof ev.seq !== 'number' && ev.seq == null) return;
  state.events.push(ev);
  if (state.events.length > MAX_EVENTS) state.events.splice(0, state.events.length - MAX_EVENTS);
  if (ev.seq > state.lastSeq && (state.prefs.scope === 'all' || ev.session_id === state.sessionId)) state.lastSeq = ev.seq;
  if (ev.run_id) {
    let run = state.runs.get(ev.run_id);
    if (!run) {
      run = { run_id: ev.run_id, source: ev.source, session_id: ev.session_id, title: ev.title, status: 'running',
              started_at: ev.ts, finished_at: null, events: [], data: {}, tools: 0, errors: 0 };
      state.runs.set(ev.run_id, run);
    }
    run.events.push(ev);
    if (ev.kind === 'run_started') { run.title = ev.title; run.started_at = ev.ts; run.detail = ev.detail; Object.assign(run.data, ev.data || {}); }
    if (ev.kind === 'run_finished') { run.status = (ev.data && ev.data.status) || 'completed'; run.finished_at = ev.ts; run.result = ev.title; Object.assign(run.data, ev.data || {}); }
    if (ev.kind === 'tool_start') run.tools += 1;
    if (ev.kind === 'error' || ev.level === 'error') run.errors += 1;
    if (ev.kind === 'status' && ev.data && ev.data.status && ev.data.status !== 'running') run.status = ev.data.status;
  }
  if (live) {
    updateChatCard(ev);
    if (!state.paused && isOpen()) scheduleRender('activity');
    if (ev.kind === 'run_started' && CHAT_CARD_SOURCES.has(ev.source) && ev.session_id === state.sessionId) {
      try { document.dispatchEvent(new CustomEvent('workbench:run-started', { detail: ev })); } catch (_) {}
    }
  }
}

function isOpen() {
  const m = $('workbench-modal');
  return !!m && !m.classList.contains('hidden');
}

let _renderTimer = null;
function scheduleRender(which) {
  if (_renderTimer) return;
  _renderTimer = setTimeout(() => { _renderTimer = null; if (which === 'activity' && state.prefs.tab === 'activity') renderActivity(); }, 120);
}

// ── SSE ───────────────────────────────────────────────────────────────────
function streamKey() {
  return state.prefs.scope === 'all' ? '*' : (state.sessionId || '');
}
async function connect(force = false) {
  const key = streamKey();
  if (!force && state.es && state.esKey === key) return;
  disconnect();
  state.events = [];
  state.runs.clear();
  state.lastSeq = 0;
  if (!key) { renderActivity(); return; }
  state.esKey = key;
  // History first (bounded), then the live stream from the last seq we saw.
  if (key !== '*') {
    try {
      const h = await api(`/api/workbench/activity?session_id=${encodeURIComponent(key)}&limit=400`);
      (h.events || []).forEach((ev) => ingest(ev, { live: false }));
      state.lastSeq = h.seq || state.lastSeq;
    } catch (e) {
      if (e.status === 403) { state.enabled = false; hideRail(); return; }
    }
  }
  const es = new EventSource(`/api/workbench/activity/stream?session_id=${encodeURIComponent(key)}&since=${state.lastSeq}`);
  state.es = es;
  es.onmessage = (m) => {
    let ev = null;
    try { ev = JSON.parse(m.data); } catch (_) { return; }
    if (!ev || ev.type === 'ready') return;
    ingest(ev, { live: true });
  };
  es.onerror = () => { /* EventSource reconnects itself; the `since` is stale but history replay covers the gap on reopen. */ };
  renderActivity();
}
function disconnect() {
  if (state.es) { try { state.es.close(); } catch (_) {} }
  state.es = null;
  state.esKey = null;
}

// Follow the chat's current session. There is no session-change event, so a
// light poll (the same approach other windows use) keeps the feed aligned.
function watchSession() {
  const tick = () => {
    const sid = currentSessionId();
    if (sid !== state.sessionId) {
      state.sessionId = sid;
      state.chatCards.clear();
      if (state.es || isOpen()) connect(true);
      else if (state.enabled) connect(true);
    }
  };
  tick();
  setInterval(tick, 1500);
}

// ── rendering: activity ───────────────────────────────────────────────────
function statusPill(status) {
  const s = status || 'running';
  const cls = s === 'running' ? 'run' : (s === 'completed' || s === 'succeeded' || s === 'done') ? 'ok' : (s === 'cancelled' ? 'warn' : 'bad');
  return `<span class="wb-pill ${cls}">${esc(s)}</span>`;
}
function runCardHtml(run) {
  const d = run.data || {};
  const repo = d.repository ? `<span class="wb-meta" title="${esc(d.repository)}">${esc(String(d.repository).split('/').slice(-2).join('/'))}</span>` : '';
  const model = d.model ? `<span class="wb-meta">${esc(d.model)}</span>` : '';
  const counts = [];
  if (run.tools) counts.push(`${run.tools} tool${run.tools === 1 ? '' : 's'}`);
  if (d.changes_count) counts.push(`${d.changes_count} file${d.changes_count === 1 ? '' : 's'}`);
  if (d.commit_count) counts.push(`${d.commit_count} commit${d.commit_count === 1 ? '' : 's'}`);
  if (run.errors) counts.push(`<span style="color:var(--red)">${run.errors} error${run.errors === 1 ? '' : 's'}</span>`);
  const dur = run.finished_at ? fmtDur(run.started_at, run.finished_at) : fmtDur(run.started_at, Date.now() / 1000);
  const actions = [];
  if (run.source === 'claude_code' && d.task_id) actions.push(`<button class="ats-btn wb-mini" data-wb-act="run-changes" data-task="${esc(d.task_id)}">Changes</button>`);
  if (run.source === 'claude_code' && d.task_id) actions.push(`<button class="ats-btn wb-mini" data-wb-act="run-transcript" data-task="${esc(d.task_id)}">Transcript</button>`);
  actions.push(`<button class="ats-btn wb-mini" data-wb-act="run-events" data-run="${esc(run.run_id)}">Events</button>`);
  return `<div class="wb-run" data-run="${esc(run.run_id)}">
    <div class="wb-run-head">
      <span class="wb-src wb-src-${esc(run.source)}">${esc(SOURCE_LABEL[run.source] || run.source)}</span>
      <span class="wb-run-title" title="${esc(run.detail || '')}">${esc(run.title || run.run_id)}</span>
      ${statusPill(run.status)}
      <span class="wb-meta">${esc(dur)}</span>
    </div>
    <div class="wb-run-sub">${repo}${model}${counts.length ? `<span class="wb-meta">${counts.join(' · ')}</span>` : ''}${run.result && run.status !== 'running' ? `<span class="wb-meta wb-result">${esc(run.result)}</span>` : ''}</div>
    <div class="wb-run-actions">${actions.join('')}</div>
  </div>`;
}
function eventRowHtml(ev) {
  const lvl = ev.level === 'error' || ev.kind === 'error' ? ' err' : (ev.level === 'warning' ? ' warn' : '');
  const detail = ev.detail ? `<details class="wb-ev-detail"><summary>detail</summary><pre>${esc(ev.detail)}</pre></details>` : '';
  const data = ev.data && Object.keys(ev.data).length ? `<details class="wb-ev-detail"><summary>data</summary><pre>${esc(JSON.stringify(ev.data, null, 1))}</pre></details>` : '';
  return `<div class="wb-ev${lvl}" data-seq="${ev.seq}" data-run="${esc(ev.run_id || '')}">
    <span class="wb-ev-time">${fmtTime(ev.ts)}</span>
    <span class="wb-src wb-src-${esc(ev.source)}">${esc(SOURCE_LABEL[ev.source] || ev.source)}</span>
    <span class="wb-ev-kind" title="${esc(ev.kind)}">${KIND_ICON[ev.kind] || '·'}</span>
    <span class="wb-ev-title">${esc(ev.title || '')}</span>
    ${detail}${data}
  </div>`;
}
function renderActivity() {
  const box = $('wb-activity');
  if (!box) return;
  const filter = state.prefs.filter || '';
  const runs = Array.from(state.runs.values()).filter((r) => !filter || r.source === filter)
    .sort((a, b) => (b.started_at || 0) - (a.started_at || 0));
  const active = runs.filter((r) => r.status === 'running');
  const recent = runs.filter((r) => r.status !== 'running').slice(0, 12);
  const evs = state.events.filter((e) => !filter || e.source === filter).slice(-300);
  const focusRun = state.focusRun ? state.runs.get(state.focusRun) : null;
  const shown = focusRun ? focusRun.events : evs;
  const empty = !state.sessionId && state.prefs.scope !== 'all';
  box.innerHTML = `
    <div class="wb-section"><div class="wb-section-h">Running <span class="wb-count">${active.length}</span></div>
      ${active.length ? active.map(runCardHtml).join('') : '<div class="wb-empty">Nothing running. Delegations to Claude Code, sub-agents, pipelines and background jobs appear here as they start.</div>'}
    </div>
    <div class="wb-section"><div class="wb-section-h">Recent runs <span class="wb-count">${recent.length}</span></div>
      ${recent.length ? recent.map(runCardHtml).join('') : `<div class="wb-empty">${empty ? 'Open a chat to follow its activity, or switch the scope to all sessions.' : 'No finished runs yet.'}</div>`}
    </div>
    <div class="wb-section wb-events"><div class="wb-section-h">${focusRun ? `Events for <em>${esc(focusRun.title)}</em> <button class="ats-btn wb-mini" data-wb-act="unfocus">all</button>` : 'Event stream'} <span class="wb-count">${shown.length}</span>${state.paused ? ' <span class="wb-pill warn">paused</span>' : ''}</div>
      <div class="wb-ev-list">${shown.slice().reverse().map(eventRowHtml).join('') || '<div class="wb-empty">No events.</div>'}</div>
    </div>`;
}

// ── rendering: changes / commits ──────────────────────────────────────────
function fileRowHtml(f, selected) {
  const status = f.status || (f.new_file ? 'A' : 'M');
  const bin = f.binary ? '<span class="wb-meta">binary</span>' : '';
  return `<div class="wb-file${selected ? ' active' : ''}" data-path="${esc(f.path)}" title="${esc(f.path)}">
    <span class="wb-file-status wb-st-${esc(status)}">${esc(status)}</span>
    <span class="wb-file-path">${esc(f.path)}</span>
    <span class="wb-file-stat"><span class="diff-stat-add">+${f.additions || 0}</span> <span class="diff-stat-del">−${f.deletions || 0}</span>${bin}</span>
  </div>`;
}
function ctxLabel() {
  const c = state.repoCtx;
  if (c.taskId) return `Claude Code run ${c.label ? esc(c.label) : esc(c.taskId)}`;
  if (c.path) return esc(c.path);
  return 'no repository selected';
}
function renderChanges() {
  const box = $('wb-changes');
  if (!box) return;
  const c = state.repoCtx;
  const total = state.files.reduce((a, f) => { a.add += f.additions || 0; a.del += f.deletions || 0; return a; }, { add: 0, del: 0 });
  box.innerHTML = `
    <div class="wb-toolbar">
      <span class="wb-ctx" title="${esc(c.path || '')}">${ctxLabel()}</span>
      ${c.taskId ? '' : `<input class="settings-select wb-base" id="wb-base" placeholder="base ref (default: working tree vs HEAD)" value="${esc(c.base || '')}">`}
      <button class="ats-btn wb-mini" data-wb-act="refresh-changes">Refresh</button>
      <span class="wb-meta"><span class="diff-stat-add">+${total.add}</span> <span class="diff-stat-del">−${total.del}</span> · ${state.files.length} file${state.files.length === 1 ? '' : 's'}</span>
      <span style="flex:1"></span>
      <span class="wb-seg"><button class="${state.prefs.mode === 'split' ? 'on' : ''}" data-wb-act="mode" data-mode="split">Split</button><button class="${state.prefs.mode === 'unified' ? 'on' : ''}" data-wb-act="mode" data-mode="unified">Unified</button></span>
    </div>
    <div class="wb-split">
      <div class="wb-filelist">${state.files.length ? state.files.map((f) => fileRowHtml(f, f.path === state.selectedFile)).join('') : `<div class="wb-empty">${c.path || c.taskId ? 'No changes.' : 'Pick a repository or a Claude Code run.'}</div>`}</div>
      <div class="wb-diffpane" id="wb-diffpane">${diffPaneHtml()}</div>
    </div>`;
}
function diffPaneHtml() {
  if (!state.selectedFile) return '<div class="wb-empty">Select a file to see old vs new.</div>';
  if (state.diffText == null) return '<div class="wb-empty">Loading diff…</div>';
  const st = diffStats(state.diffText);
  const head = `<div class="wb-diff-head"><span class="wb-file-path">${esc(state.selectedFile)}</span><span class="wb-meta"><span class="diff-stat-add">+${st.additions}</span> <span class="diff-stat-del">−${st.deletions}</span></span><span style="flex:1"></span><button class="ats-btn wb-mini" data-wb-act="popout">Pop out</button><button class="ats-btn wb-mini" data-wb-act="send-file">Send to agent</button></div>`;
  if (!state.diffText.trim()) return head + '<div class="wb-empty">No textual diff (binary or unchanged).</div>';
  return head + `<div class="wb-diff-scroll">${renderDiffText(state.diffText, { mode: state.prefs.mode, path: state.selectedFile })}</div>`;
}
function renderCommits() {
  const box = $('wb-commits');
  if (!box) return;
  const c = state.repoCtx;
  const sel = state.selectedCommit;
  box.innerHTML = `
    <div class="wb-toolbar"><span class="wb-ctx">${ctxLabel()}</span><button class="ats-btn wb-mini" data-wb-act="refresh-commits">Refresh</button><span class="wb-meta">${state.commits.length} commit${state.commits.length === 1 ? '' : 's'}${c.taskId ? ' since the run started' : ''}</span></div>
    <div class="wb-split">
      <div class="wb-filelist">${state.commits.length ? state.commits.map((k) => `<div class="wb-commit${sel && sel.sha === k.sha ? ' active' : ''}" data-sha="${esc(k.sha)}"><code>${esc((k.sha || '').slice(0, 8))}</code> <span class="wb-commit-subj">${esc(k.subject || k.message || '')}</span><div class="wb-meta">${esc(k.author || '')} · ${esc(k.date || (k.ts ? fmtTime(k.ts) : ''))}</div></div>`).join('') : `<div class="wb-empty">${c.path || c.taskId ? 'No commits.' : 'Pick a repository or a Claude Code run.'}</div>`}</div>
      <div class="wb-diffpane" id="wb-commitpane">${commitPaneHtml()}</div>
    </div>`;
}
function commitPaneHtml() {
  const k = state.selectedCommit;
  if (!k) return '<div class="wb-empty">Select a commit.</div>';
  if (k.loading) return '<div class="wb-empty">Loading…</div>';
  const files = k.files || [];
  const body = k.body ? `<pre class="wb-commit-body">${esc(k.body)}</pre>` : '';
  const fileList = files.map((f) => fileRowHtml(f, f.path === k.selectedFile)).join('');
  const diff = k.selectedFile ? (k.diffText == null ? '<div class="wb-empty">Loading diff…</div>'
    : `<div class="wb-diff-head"><span class="wb-file-path">${esc(k.selectedFile)}</span><span style="flex:1"></span><button class="ats-btn wb-mini" data-wb-act="popout-commit">Pop out</button></div><div class="wb-diff-scroll">${renderDiffText(k.diffText, { mode: state.prefs.mode, path: k.selectedFile })}</div>`) : '';
  return `<div class="wb-commit-detail"><div><code>${esc(k.sha || '')}</code> ${esc(k.subject || '')}</div><div class="wb-meta">${esc(k.author || '')} ${esc(k.email ? `<${k.email}>` : '')} · ${esc(k.date || '')}</div>${body}
    <div class="wb-toolbar" style="margin-top:6px"><span class="wb-meta">${files.length} file${files.length === 1 ? '' : 's'}</span><span style="flex:1"></span><span class="wb-seg"><button class="${state.prefs.mode === 'split' ? 'on' : ''}" data-wb-act="mode" data-mode="split">Split</button><button class="${state.prefs.mode === 'unified' ? 'on' : ''}" data-wb-act="mode" data-mode="unified">Unified</button></span></div>
    <div class="wb-commit-files">${fileList || '<div class="wb-empty">No file list.</div>'}</div>${diff}</div>`;
}

// ── rendering: pull requests ──────────────────────────────────────────────
function checkPill(c) {
  const st = (c.conclusion || c.status || '').toLowerCase();
  const cls = st === 'success' ? 'ok' : (st === 'failure' || st === 'error' || st === 'timed_out' || st === 'cancelled') ? 'bad' : (st === 'in_progress' || st === 'queued' || st === 'pending') ? 'run' : 'warn';
  return `<span class="wb-pill ${cls}" title="${esc(c.name || c.context || '')}">${esc(c.name || c.context || 'check')}: ${esc(st || '?')}</span>`;
}
function renderPRs() {
  const box = $('wb-prs');
  if (!box) return;
  const p = state.pr;
  if (p.config && !p.config.configured) {
    box.innerHTML = `<div class="wb-empty">Pull request review needs the worktree GitHub credential.<ul>${(p.config.blockers || []).map((b) => `<li>${esc(b)}</li>`).join('')}</ul>Configure it under Settings → Agent worktree.</div>`;
    return;
  }
  const list = p.list.map((pr) => `<div class="wb-pr${p.selected === pr.number ? ' active' : ''}" data-number="${pr.number}">
      <span class="wb-pill ${pr.draft ? 'warn' : (pr.state === 'open' ? 'ok' : 'run')}">${pr.draft ? 'draft' : esc(pr.state || '')}</span>
      <span class="wb-pr-title">#${pr.number} ${esc(pr.title || '')}</span>
      <div class="wb-meta">${esc(pr.user || pr.author || '')} · ${esc(pr.head || '')} → ${esc(pr.base || '')}</div>
    </div>`).join('');
  box.innerHTML = `
    <div class="wb-toolbar"><span class="wb-ctx">${esc((p.config && p.config.repo) || '')}</span>
      <span class="wb-seg"><button class="${p.stateFilter === 'open' ? 'on' : ''}" data-wb-act="pr-state" data-state="open">Open</button><button class="${p.stateFilter === 'closed' ? 'on' : ''}" data-wb-act="pr-state" data-state="closed">Closed</button><button class="${p.stateFilter === 'all' ? 'on' : ''}" data-wb-act="pr-state" data-state="all">All</button></span>
      <button class="ats-btn wb-mini" data-wb-act="refresh-prs">Refresh</button></div>
    <div class="wb-split"><div class="wb-filelist">${list || '<div class="wb-empty">No pull requests.</div>'}</div><div class="wb-diffpane" id="wb-prpane">${prPaneHtml()}</div></div>`;
}
function prPaneHtml() {
  const d = state.pr.detail;
  if (!state.pr.selected) return '<div class="wb-empty">Select a pull request.</div>';
  if (!d) return '<div class="wb-empty">Loading…</div>';
  if (d.error) return `<div class="wb-empty" style="color:var(--red)">${esc(d.error)}</div>`;
  const pr = d.pull || {};
  const checks = (d.checks || []).map(checkPill).join(' ') || '<span class="wb-meta">no checks</span>';
  const files = (d.files || []).map((f) => `<div class="wb-file${d.selectedFile === f.path ? ' active' : ''}" data-prfile="${esc(f.path)}"><span class="wb-file-status wb-st-${esc((f.status || 'M')[0].toUpperCase())}">${esc((f.status || 'M')[0].toUpperCase())}</span><span class="wb-file-path">${esc(f.path)}</span><span class="wb-file-stat"><span class="diff-stat-add">+${f.additions || 0}</span> <span class="diff-stat-del">−${f.deletions || 0}</span></span></div>`).join('');
  const comments = [...(d.comments || []).map((c) => ({ ...c, _t: 'comment' })), ...(d.review_comments || []).map((c) => ({ ...c, _t: 'review' })), ...(d.reviews || []).filter((r) => r.body).map((r) => ({ ...r, _t: 'reviewsum' }))]
    .sort((a, b) => String(a.created_at || a.submitted_at || '').localeCompare(String(b.created_at || b.submitted_at || '')));
  const thread = comments.map((c) => `<div class="wb-comment"><div class="wb-meta"><b>${esc(c.user || c.author || '')}</b> · ${esc(c._t === 'review' ? `review comment on ${c.path || ''}${c.line ? ':' + c.line : ''}` : c._t === 'reviewsum' ? `review: ${c.state || ''}` : 'comment')} · ${esc(c.created_at || c.submitted_at || '')}</div><div class="wb-comment-body">${esc(c.body || '')}</div></div>`).join('') || '<div class="wb-empty">No comments yet.</div>';
  let diff = '';
  if (d.selectedFile) {
    const parsed = d.diffText ? parseUnifiedDiff(d.diffText) : [];
    const f = parsed.find((x) => x.newPath === d.selectedFile || x.oldPath === d.selectedFile);
    diff = `<div class="wb-diff-head"><span class="wb-file-path">${esc(d.selectedFile)}</span><span style="flex:1"></span><span class="wb-seg"><button class="${state.prefs.mode === 'split' ? 'on' : ''}" data-wb-act="mode" data-mode="split">Split</button><button class="${state.prefs.mode === 'unified' ? 'on' : ''}" data-wb-act="mode" data-mode="unified">Unified</button></span><button class="ats-btn wb-mini" data-wb-act="popout-pr">Pop out</button></div>
      <div class="wb-diff-scroll">${d.diffText == null ? '<div class="wb-empty">Loading diff…</div>' : (f ? renderFileTable(f, { mode: state.prefs.mode, path: d.selectedFile }) : '<div class="wb-empty">No textual diff for this file.</div>')}</div>
      <div class="wb-meta" style="margin:4px 0 8px">Click a line number to attach a review comment to it.</div>`;
  }
  const pending = d.pendingComments || [];
  return `<div class="wb-pr-detail">
    <div class="wb-pr-head"><a href="${esc(pr.html_url || pr.url || '#')}" target="_blank" rel="noopener">#${pr.number} ${esc(pr.title || '')}</a> ${statusPill(pr.merged ? 'merged' : (pr.draft ? 'draft' : pr.state))}<div class="wb-meta">${esc(pr.user || '')} · ${esc(pr.head || '')} → ${esc(pr.base || '')} · ${esc(pr.mergeable_state || '')}</div></div>
    <div class="wb-checks">${checks}</div>
    ${pr.body ? `<details class="wb-ev-detail"><summary>Description</summary><pre>${esc(pr.body)}</pre></details>` : ''}
    <div class="wb-pr-cols">
      <div><div class="wb-section-h">Files <span class="wb-count">${(d.files || []).length}</span></div><div class="wb-commit-files">${files || '<div class="wb-empty">No files.</div>'}</div>${diff}</div>
      <div><div class="wb-section-h">Conversation <span class="wb-count">${comments.length}</span></div><div class="wb-thread">${thread}</div>
        ${pending.length ? `<div class="wb-section-h">Pending line comments <span class="wb-count">${pending.length}</span></div>${pending.map((c, i) => `<div class="wb-comment pending"><div class="wb-meta">${esc(c.path)}:${c.line} <button class="ats-btn wb-mini" data-wb-act="drop-pending" data-i="${i}">remove</button></div><div class="wb-comment-body">${esc(c.body)}</div></div>`).join('')}` : ''}
        <textarea id="wb-pr-body" class="wb-textarea" placeholder="Write a comment or review summary…"></textarea>
        <div class="wb-toolbar">
          <button class="ats-btn wb-mini" data-wb-act="pr-comment">Comment</button>
          <button class="ats-btn wb-mini" data-wb-act="pr-review" data-event="COMMENT">Review</button>
          <button class="ats-btn wb-mini" data-wb-act="pr-review" data-event="APPROVE">Approve</button>
          <button class="ats-btn wb-mini" data-wb-act="pr-review" data-event="REQUEST_CHANGES">Request changes</button>
          <span style="flex:1"></span>
          <button class="ats-btn wb-mini" data-wb-act="pr-to-agent">Ask Odysseus to address</button>
        </div>
      </div>
    </div></div>`;
}

// ── data loading ──────────────────────────────────────────────────────────
async function loadRoots() {
  const sel = $('wb-repo');
  if (!sel) return;
  try {
    const r = await api('/api/workbench/repo/roots');
    const repos = r.repositories || [];
    sel.innerHTML = '<option value="">— repository —</option>' + repos.map((x) => `<option value="${esc(x.path)}">${esc(x.path)}${x.branch ? ' (' + esc(x.branch) + ')' : ''}</option>`).join('');
    const want = state.repoCtx.path || state.prefs.repo || (r.workspace && repos.some((x) => x.path === r.workspace) ? r.workspace : '') || (repos[0] && repos[0].path) || '';
    if (want && repos.some((x) => x.path === want)) { sel.value = want; if (!state.repoCtx.taskId && state.repoCtx.path !== want) setRepo(want); }
  } catch (e) {
    if (e.status === 403) { state.enabled = false; hideRail(); }
    sel.innerHTML = `<option value="">${esc(e.message)}</option>`;
  }
}
function setRepo(path) {
  state.repoCtx = { path, base: '', taskId: null, label: '' };
  state.prefs.repo = path; savePrefs();
  state.files = []; state.commits = []; state.selectedFile = null; state.selectedCommit = null; state.diffText = '';
  refreshChanges(); refreshCommits();
}
async function loadTask(taskId, { tab = 'changes' } = {}) {
  state.repoCtx = { path: '', base: '', taskId, label: '' };
  state.files = []; state.commits = []; state.selectedFile = null; state.selectedCommit = null; state.diffText = '';
  setTab(tab);
  renderChanges(); renderCommits();
  try {
    const r = await api(`/api/claude-code/tasks/${encodeURIComponent(taskId)}/changes`);
    state.repoCtx.path = r.repository || '';
    state.repoCtx.base = r.start_commit || '';
    state.repoCtx.label = r.label || '';
    state.repoCtx.task = r;
    state.files = r.changes || [];
    state.commits = r.commits || [];
    if (r.changes_error) showToast(`Change list: ${r.changes_error}`, 'warning');
    const sel = $('wb-repo');
    if (sel && r.repository && Array.from(sel.options).some((o) => o.value === r.repository)) sel.value = r.repository;
  } catch (e) { showToast(`Could not load run changes: ${e.message}`, 'error'); }
  renderChanges(); renderCommits();
}
async function refreshChanges() {
  const c = state.repoCtx;
  if (c.taskId) return loadTask(c.taskId, { tab: state.prefs.tab });
  if (!c.path) { renderChanges(); return; }
  const baseInput = $('wb-base');
  if (baseInput) c.base = baseInput.value.trim();
  try {
    const r = await api(`/api/workbench/repo/changes?path=${encodeURIComponent(c.path)}${c.base ? '&base=' + encodeURIComponent(c.base) : ''}`);
    state.files = r.files || [];
    if (r.truncated) showToast('Change list truncated', 'warning');
  } catch (e) { state.files = []; showToast(e.message, 'error'); }
  if (state.selectedFile && !state.files.some((f) => f.path === state.selectedFile)) { state.selectedFile = null; state.diffText = ''; }
  renderChanges();
}
async function refreshCommits() {
  const c = state.repoCtx;
  if (!c.path && !c.taskId) { renderCommits(); return; }
  if (c.taskId) { renderCommits(); return; }
  try {
    const r = await api(`/api/workbench/repo/commits?path=${encodeURIComponent(c.path)}&limit=50${c.base ? '&base=' + encodeURIComponent(c.base) : ''}`);
    state.commits = r.commits || [];
  } catch (e) { state.commits = []; showToast(e.message, 'error'); }
  renderCommits();
}
async function selectFile(path) {
  state.selectedFile = path; state.diffText = null; renderChanges();
  const c = state.repoCtx;
  try {
    const r = c.taskId
      ? await api(`/api/claude-code/tasks/${encodeURIComponent(c.taskId)}/diff?path=${encodeURIComponent(path)}`)
      : await api(`/api/workbench/repo/diff?path=${encodeURIComponent(c.path)}&file=${encodeURIComponent(path)}${c.base ? '&base=' + encodeURIComponent(c.base) : ''}`);
    state.diffText = r.diff || r.text || '';
    if (r.truncated) showToast('Diff truncated', 'warning');
  } catch (e) { state.diffText = ''; showToast(e.message, 'error'); }
  if (state.selectedFile === path) { const pane = $('wb-diffpane'); if (pane) pane.innerHTML = diffPaneHtml(); }
}
async function selectCommit(sha) {
  const k = state.commits.find((x) => x.sha === sha) || { sha };
  state.selectedCommit = Object.assign({ loading: true }, k);
  renderCommits();
  try {
    const r = await api(`/api/workbench/repo/commit?path=${encodeURIComponent(state.repoCtx.path)}&sha=${encodeURIComponent(sha)}`);
    state.selectedCommit = Object.assign({}, k, r, { loading: false, selectedFile: null, diffText: null });
  } catch (e) { state.selectedCommit = Object.assign({}, k, { loading: false, body: e.message }); }
  renderCommits();
}
async function selectCommitFile(path) {
  const k = state.selectedCommit; if (!k) return;
  k.selectedFile = path; k.diffText = null; renderCommits();
  try {
    const r = await api(`/api/workbench/repo/diff?path=${encodeURIComponent(state.repoCtx.path)}&file=${encodeURIComponent(path)}&commit=${encodeURIComponent(k.sha)}`);
    k.diffText = r.diff || r.text || '';
  } catch (e) { k.diffText = ''; showToast(e.message, 'error'); }
  renderCommits();
}
async function loadPRConfig() {
  try { state.pr.config = await api('/api/workbench/prs/config'); }
  catch (e) { state.pr.config = { configured: false, blockers: [e.message] }; }
  renderPRs();
  if (state.pr.config.configured) refreshPRs();
}
async function refreshPRs() {
  try {
    const r = await api(`/api/workbench/prs?state=${encodeURIComponent(state.pr.stateFilter)}&limit=30`);
    state.pr.list = r.pulls || [];
  } catch (e) { state.pr.list = []; showToast(e.message, 'error'); }
  renderPRs();
}
async function selectPR(number) {
  state.pr.selected = number; state.pr.detail = null; renderPRs();
  try {
    const d = await api(`/api/workbench/prs/${number}`);
    d.pendingComments = []; d.selectedFile = null; d.diffText = null;
    state.pr.detail = d;
  } catch (e) { state.pr.detail = { error: e.message }; }
  renderPRs();
}
async function selectPRFile(path) {
  const d = state.pr.detail; if (!d) return;
  d.selectedFile = path; renderPRs();
  if (d.diffText == null) {
    try { const r = await api(`/api/workbench/prs/${state.pr.selected}/diff`); d.diffText = r.diff || r.text || ''; }
    catch (e) { d.diffText = ''; showToast(e.message, 'error'); }
    renderPRs();
  }
}
async function postPRComment() {
  const ta = $('wb-pr-body'); const body = ta ? ta.value.trim() : '';
  if (!body) { showToast('Write a comment first', 'warning'); return; }
  try {
    await post(`/api/workbench/prs/${state.pr.selected}/comment`, { body, session_id: state.sessionId });
    showToast('Comment posted', 'success'); selectPR(state.pr.selected);
  } catch (e) { showToast(e.message, 'error'); }
}
async function postPRReview(event) {
  const d = state.pr.detail; if (!d) return;
  const ta = $('wb-pr-body'); const body = ta ? ta.value.trim() : '';
  const comments = (d.pendingComments || []).map((c) => ({ path: c.path, line: c.line, body: c.body }));
  if (!body && !comments.length && event !== 'APPROVE') { showToast('Write a review summary or add line comments', 'warning'); return; }
  try {
    await post(`/api/workbench/prs/${state.pr.selected}/review`, { event, body, comments, session_id: state.sessionId });
    showToast(`Review submitted (${event.toLowerCase().replace('_', ' ')})`, 'success'); selectPR(state.pr.selected);
  } catch (e) { showToast(e.message, 'error'); }
}
function prToAgent() {
  const d = state.pr.detail; if (!d || !d.pull) return;
  const pr = d.pull;
  const failing = (d.checks || []).filter((c) => ['failure', 'error', 'timed_out'].includes(String(c.conclusion || '').toLowerCase())).map((c) => c.name || c.context);
  const lines = [`Please look at pull request #${pr.number} "${pr.title}" (${pr.head} → ${pr.base}) in ${d.repo || ''}.`];
  if (failing.length) lines.push(`Failing checks: ${failing.join(', ')}.`);
  const open = (d.review_comments || []).slice(-5).map((c) => `- ${c.user || ''} on ${c.path || ''}${c.line ? ':' + c.line : ''}: ${String(c.body || '').split('\n')[0].slice(0, 200)}`);
  if (open.length) lines.push('Recent review comments:', ...open);
  lines.push('Address the feedback and failing checks, then summarise what you changed.');
  fillComposer(lines.join('\n'));
}

// ── pop-out diff window ───────────────────────────────────────────────────
function popout(title, html) {
  let modal = $('workbench-diff-modal');
  if (!modal) return;
  modal.querySelector('.wb-popout-title').textContent = title;
  modal.querySelector('.wb-popout-body').innerHTML = html;
  modal.classList.remove('hidden');
  Modals.register('workbench-diff-modal', { label: 'Diff', closeFn: () => modal.classList.add('hidden') });
  try { window.dispatchEvent(new CustomEvent('odysseus:modal-opened', { detail: { id: 'workbench-diff-modal' } })); } catch (_) {}
}

// ── chat run cards ────────────────────────────────────────────────────────
function chatHistory() { return $('chat-history'); }
function updateChatCard(ev) {
  if (!ev.run_id || !CHAT_CARD_SOURCES.has(ev.source)) return;
  if (state.prefs.scope !== 'all' && ev.session_id !== state.sessionId) return;
  if (state.prefs.scope === 'all' && ev.session_id !== state.sessionId) return;
  const run = state.runs.get(ev.run_id); if (!run) return;
  const hist = chatHistory(); if (!hist) return;
  let card = state.chatCards.get(ev.run_id);
  if (!card) {
    card = document.createElement('div');
    card.className = 'agent-run-card';
    card.dataset.run = ev.run_id;
    card.innerHTML = '<div class="agent-run-head"></div><div class="agent-run-steps"></div><div class="agent-run-foot"></div>';
    state.chatCards.set(ev.run_id, card);
    // Attach inside the assistant turn that is streaming (so the card sits with
    // the conversation), else at the end of the history.
    const live = hist.querySelector('.message.assistant:last-of-type, .msg.assistant:last-of-type') || hist.lastElementChild;
    if (live && live.classList && (live.classList.contains('assistant') || live.querySelector('.agent-thread'))) live.appendChild(card); else hist.appendChild(card);
    card.addEventListener('click', (e) => {
      const b = e.target.closest('[data-wb-act]');
      if (!b) { card.classList.toggle('open'); return; }
      e.stopPropagation();
      const act = b.dataset.wbAct;
      if (act === 'open-wb') { open(); state.focusRun = ev.run_id; setTab('activity'); }
      if (act === 'open-changes' && run.data.task_id) { open(); loadTask(run.data.task_id); }
    });
    const near = hist.scrollHeight - hist.scrollTop - hist.clientHeight < 160;
    if (near) hist.scrollTop = hist.scrollHeight;
  }
  const head = card.querySelector('.agent-run-head');
  const steps = card.querySelector('.agent-run-steps');
  const foot = card.querySelector('.agent-run-foot');
  const running = run.status === 'running';
  card.classList.toggle('running', running);
  card.classList.toggle('failed', !running && run.status !== 'completed');
  head.innerHTML = `<span class="agent-run-dot"></span><span class="wb-src wb-src-${esc(run.source)}">${esc(SOURCE_LABEL[run.source] || run.source)}</span><span class="agent-run-title">${esc(run.title || '')}</span>${statusPill(run.status)}<span class="agent-run-count">${run.tools ? run.tools + ' steps' : ''}</span>`;
  if (['message', 'tool_start', 'tool_result', 'file_change', 'commit', 'status', 'error'].includes(ev.kind)) {
    const row = document.createElement('div');
    row.className = 'agent-run-step' + (ev.kind === 'error' || ev.level === 'error' ? ' err' : '');
    row.innerHTML = `<span class="wb-ev-kind">${KIND_ICON[ev.kind] || '·'}</span><span>${esc(ev.title || '')}</span>${ev.detail ? `<details class="wb-ev-detail"><summary>more</summary><pre>${esc(ev.detail)}</pre></details>` : ''}`;
    steps.appendChild(row);
    while (steps.children.length > 60) steps.removeChild(steps.firstChild);
  }
  if (!running) {
    const d = run.data || {};
    const bits = [];
    if (d.changes_count) bits.push(`${d.changes_count} file${d.changes_count === 1 ? '' : 's'} changed`);
    if (d.commit_count) bits.push(`${d.commit_count} commit${d.commit_count === 1 ? '' : 's'}`);
    if (d.result_excerpt) bits.push(esc(String(d.result_excerpt).slice(0, 240)));
    foot.innerHTML = `<span class="wb-meta">${bits.join(' · ')}</span><span style="flex:1"></span>${d.task_id ? '<button class="ats-btn wb-mini" data-wb-act="open-changes">View changes</button>' : ''}<button class="ats-btn wb-mini" data-wb-act="open-wb">Open in Workbench</button>`;
  } else if (!foot.innerHTML) {
    foot.innerHTML = '<span style="flex:1"></span><button class="ats-btn wb-mini" data-wb-act="open-wb">Open in Workbench</button>';
  }
}

// ── window plumbing ───────────────────────────────────────────────────────
function hideRail() { const b = $('rail-workbench'); if (b) b.style.display = 'none'; }
function setTab(tab) {
  state.prefs.tab = tab; savePrefs();
  document.querySelectorAll('#workbench-modal [data-wb-tab]').forEach((b) => b.classList.toggle('active', b.dataset.wbTab === tab));
  document.querySelectorAll('#workbench-modal [data-wb-panel]').forEach((p) => p.classList.toggle('hidden', p.dataset.wbPanel !== tab));
  if (tab === 'activity') renderActivity();
  if (tab === 'changes') renderChanges();
  if (tab === 'commits') renderCommits();
  if (tab === 'prs') { if (!state.pr.config) loadPRConfig(); else renderPRs(); }
}
export function open() {
  const modal = $('workbench-modal'); if (!modal) return;
  if (modal.classList.contains('hidden')) {
    modal.classList.remove('hidden');
    try { window.dispatchEvent(new CustomEvent('odysseus:modal-opened', { detail: { id: 'workbench-modal' } })); } catch (_) {}
  }
  connect();
  loadRoots();
  setTab(state.prefs.tab || 'activity');
}
export function close() {
  const modal = $('workbench-modal'); if (!modal) return;
  modal.classList.add('hidden');
}
export function toggle() {
  if (Modals.toggle('workbench-modal')) return;
  if (isOpen()) close(); else open();
}

function wireWindow() {
  const modal = $('workbench-modal'); if (!modal) return;
  const content = modal.querySelector('.modal-content');
  const header = modal.querySelector('.modal-header');
  makeWindowDraggable(modal, {
    content, header, skipSelector: 'button, input, select, label, textarea', enableDock: true, enableLeftDock: true,
    resizeStorageKey: 'odysseus-workbench-size',
    onEnterFullscreen: () => snapModalToZone(modal, { name: 'fullscreen', rect: { left: 0, top: 0, width: window.innerWidth, height: window.innerHeight } }),
  });
  Modals.register('workbench-modal', {
    railBtnId: 'rail-workbench', label: 'Workbench',
    icon: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4M6 8l3 3-3 3M11 14h5"/></svg>',
    restoreFn: () => open(), closeFn: () => close(),
  });
  $('close-workbench-modal')?.addEventListener('click', close);
  $('wb-dock-right')?.addEventListener('click', () => { try { applyRightDock(modal); } catch (_) {} });
  $('rail-workbench')?.addEventListener('click', () => { if (!Modals.toggle('workbench-modal')) toggle(); });
  $('close-workbench-diff-modal')?.addEventListener('click', () => $('workbench-diff-modal')?.classList.add('hidden'));
  const dm = $('workbench-diff-modal');
  if (dm) makeWindowDraggable(dm, { content: dm.querySelector('.modal-content'), header: dm.querySelector('.modal-header'), resizeStorageKey: 'odysseus-workbench-diff-size' });

  modal.querySelectorAll('[data-wb-tab]').forEach((b) => b.addEventListener('click', () => setTab(b.dataset.wbTab)));
  $('wb-scope')?.addEventListener('change', (e) => { state.prefs.scope = e.target.value; savePrefs(); state.focusRun = null; connect(true); });
  $('wb-filter')?.addEventListener('change', (e) => { state.prefs.filter = e.target.value; savePrefs(); renderActivity(); });
  $('wb-pause')?.addEventListener('click', (e) => { state.paused = !state.paused; e.target.textContent = state.paused ? 'Resume' : 'Pause'; renderActivity(); });
  $('wb-clear')?.addEventListener('click', () => { state.events = []; state.runs.clear(); state.focusRun = null; renderActivity(); });
  $('wb-repo')?.addEventListener('change', (e) => { if (e.target.value) setRepo(e.target.value); });
  const scope = $('wb-scope'); if (scope) scope.value = state.prefs.scope;
  const filt = $('wb-filter'); if (filt) filt.value = state.prefs.filter;

  // One delegated click handler for every action inside the window.
  modal.addEventListener('click', (e) => {
    const file = e.target.closest('.wb-file[data-path]');
    if (file) { selectFile(file.dataset.path); return; }
    const prfile = e.target.closest('.wb-file[data-prfile]');
    if (prfile) { selectPRFile(prfile.dataset.prfile); return; }
    const commit = e.target.closest('.wb-commit[data-sha]');
    if (commit) { selectCommit(commit.dataset.sha); return; }
    const pr = e.target.closest('.wb-pr[data-number]');
    if (pr) { selectPR(parseInt(pr.dataset.number, 10)); return; }
    const lineNo = e.target.closest('td.wb-no');
    const lineRow = lineNo && lineNo.closest('tr.wb-l[data-path]');
    if (lineRow && (lineRow.dataset.new || lineRow.dataset.old)) { onLineClick(lineRow); return; }
    const b = e.target.closest('[data-wb-act]');
    if (!b) return;
    const act = b.dataset.wbAct;
    switch (act) {
      case 'mode': state.prefs.mode = b.dataset.mode; savePrefs(); renderChanges(); renderCommits(); renderPRs(); break;
      case 'refresh-changes': refreshChanges(); break;
      case 'refresh-commits': refreshCommits(); break;
      case 'refresh-prs': refreshPRs(); break;
      case 'pr-state': state.pr.stateFilter = b.dataset.state; refreshPRs(); break;
      case 'run-changes': loadTask(b.dataset.task, { tab: 'changes' }); break;
      case 'run-transcript': showTranscript(b.dataset.task); break;
      case 'run-events': state.focusRun = b.dataset.run; renderActivity(); break;
      case 'unfocus': state.focusRun = null; renderActivity(); break;
      case 'popout': popout(state.selectedFile || 'diff', renderDiffText(state.diffText || '', { mode: state.prefs.mode, path: state.selectedFile })); break;
      case 'popout-commit': { const k = state.selectedCommit; if (k && k.selectedFile) popout(`${(k.sha || '').slice(0, 8)} ${k.selectedFile}`, renderDiffText(k.diffText || '', { mode: state.prefs.mode, path: k.selectedFile })); break; }
      case 'popout-pr': { const d = state.pr.detail; if (d && d.selectedFile) { const f = parseUnifiedDiff(d.diffText || '').find((x) => x.newPath === d.selectedFile || x.oldPath === d.selectedFile); popout(`PR #${state.pr.selected} ${d.selectedFile}`, f ? renderFileTable(f, { mode: state.prefs.mode, path: d.selectedFile }) : ''); } break; }
      case 'send-file': sendFileToAgent(); break;
      case 'pr-comment': postPRComment(); break;
      case 'pr-review': postPRReview(b.dataset.event); break;
      case 'pr-to-agent': prToAgent(); break;
      case 'drop-pending': { const d = state.pr.detail; if (d) { d.pendingComments.splice(parseInt(b.dataset.i, 10), 1); renderPRs(); } break; }
      default: break;
    }
  });
}

function onLineClick(row) {
  const path = row.dataset.path || state.selectedFile || '';
  const line = row.dataset.new || row.dataset.old;
  const code = Array.from(row.querySelectorAll('.wb-code')).map((c) => c.textContent.trim()).filter(Boolean).pop() || '';
  if (state.prefs.tab === 'prs' && state.pr.detail && state.pr.detail.selectedFile) {
    const text = window.prompt(`Review comment for ${path}:${line}`, '');
    if (!text) return;
    state.pr.detail.pendingComments.push({ path, line: parseInt(line, 10), body: text });
    renderPRs();
    return;
  }
  fillComposer(`In \`${path}\` line ${line}:\n\`\`\`\n${code}\n\`\`\`\n`);
  showToast('Line reference added to the composer', 'success');
}
function sendFileToAgent() {
  if (!state.selectedFile) return;
  const st = diffStats(state.diffText || '');
  const c = state.repoCtx;
  fillComposer(`Review the change to \`${state.selectedFile}\`${c.path ? ` in ${c.path}` : ''} (+${st.additions}/−${st.deletions}):\n\`\`\`diff\n${String(state.diffText || '').slice(0, 6000)}\n\`\`\``);
  showToast('Diff added to the composer', 'success');
}
async function showTranscript(taskId) {
  try {
    const r = await api(`/api/claude-code/tasks/${encodeURIComponent(taskId)}/changes`);
    const rows = (r.transcript || []).map((t) => `<div class="wb-ev${t.type === 'error' ? ' err' : ''}"><span class="wb-ev-time">${esc(t.role || t.type || '')}</span><span class="wb-ev-title">${esc(t.title || t.text || t.summary || JSON.stringify(t).slice(0, 300))}</span>${t.detail ? `<details class="wb-ev-detail"><summary>detail</summary><pre>${esc(t.detail)}</pre></details>` : ''}</div>`).join('');
    popout(`Transcript ${r.label || taskId}`, `<div class="wb-ev-list">${rows || '<div class="wb-empty">No transcript recorded (stream-json unsupported or disabled).</div>'}${r.transcript_truncated ? '<div class="wb-meta">transcript truncated</div>' : ''}</div>${r.result ? `<div class="wb-section-h">Result</div><pre class="wb-commit-body">${esc(r.result)}</pre>` : ''}`);
  } catch (e) { showToast(e.message, 'error'); }
}

async function probeSettings() {
  try {
    const r = await fetch('/api/auth/settings', { credentials: 'same-origin' });
    if (r.status === 403 || r.status === 401) { state.enabled = false; hideRail(); return; }
    const s = await r.json();
    state.enabled = s.workbench_enabled !== false;
    state.autoOpen = s.workbench_auto_open !== false;
    if (!state.enabled) { hideRail(); disconnect(); }
  } catch (_) {}
}

export async function init() {
  if (state.inited) return;
  state.inited = true;
  loadPrefs();
  wireWindow();
  await probeSettings();
  if (!state.enabled) return;
  watchSession();
  // Auto-open on the first sub-process run of the session (Claude Code, a
  // sub-agent…), so the user sees the work as it happens.
  document.addEventListener('workbench:run-started', () => { if (state.autoOpen && !isOpen()) open(); });
}

export function refreshSettings() { return probeSettings(); }
export const _state = state;
export default { init, open, close, toggle, refreshSettings };
