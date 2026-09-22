/* agentsDashboard.js — the Agents page: every chat and worker the user runs.
 *
 * A full-page view (rail button, sidebar item, Ctrl+Shift+A, /agents) with:
 *   Fleet     — every chat with agent activity in three triage buckets:
 *               Needs you → Active → Recent, filterable from the header.
 *   Detail    — the selected chat: its live event stream, child runs (sub-
 *               agents, Claude Code, background jobs) with Stop, pending
 *               approvals with Approve/Deny, a Steer box that lands mid-turn
 *               with the log of what became of each steering message, and a
 *               Reply box for an idle chat.
 *   Launch    — start a worker profile in a fresh chat from here.
 *
 * Data comes from /api/agents/* (owner-scoped, routes/agents_routes.py); the
 * live feed is one SSE stream of the user's activity events.
 */

import uiModule from './ui.js';
import Modals from './modalManager.js';
import { makeWindowDraggable } from './windowDrag.js';
import { snapModalToZone } from './tileManager.js';
import { applyEdgeDock } from './modalSnap.js';
import { nextToolWindowZ } from './toolWindowZOrder.js';

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const STATUS = {
  waiting_approval: ['Needs approval', 'warn'], running: ['Running', 'run'], failed: ['Failed', 'bad'],
  finished: ['Finished', 'ok'], stopped: ['Stopped', 'warn'], idle: ['Idle', ''],
  // A worker that spent its round budget with the task unfinished
  // (agent_control.launch_worker). Its result is partial work to resume from,
  // not a failure — without this entry pill() fell through to the raw status
  // word with no class at all.
  incomplete: ['Out of rounds', 'warn'],
  // Refused before it started (src/worker_preflight.py): no workspace for a
  // repository task, or the tools it needs are switched off.
  blocked: ['Blocked', 'warn'],
  // Steering-message states (src/agent_control.py). They share this map, and
  // therefore the same pill colours, because they answer the same question the
  // run statuses above do: is this still going, did it land, or did it not.
  // `cancelled` also covers a stopped child run, which until now fell through
  // to the raw word with no class.
  queued: ['Queued', 'warn'], acknowledged: ['Acknowledged', 'run'], injected: ['Injected', 'ok'],
  cancelled: ['Cancelled', 'warn'],
};
// What each state is evidence of, shown on hover. `injected` is where the
// trail ends on purpose: the server can see the message being handed to the
// model and nothing after that, so the UI does not claim the agent acted on it
// (see the STEER_STATES comment in src/agent_control.py).
const STEER_STATE_NOTE = {
  queued: 'Waiting — no turn has picked it up yet',
  acknowledged: 'A running turn took it off the queue',
  injected: 'Handed to the model. Whether the agent then acted on it is not something the server can see',
  cancelled: 'The turn ended before the agent read it',
  failed: 'Never reached the agent',
};
const SOURCE_LABEL = { odysseus: 'Odysseus', claude_code: 'Claude Code', session: 'Sub-agent', pipeline: 'Pipeline', bg_job: 'Background job', worktree: 'Worktree', system: 'System' };
const KIND_ICON = { run_started: '▸', run_finished: '■', message: '›', tool_start: '→', tool_result: '←', file_change: '±', commit: '●', status: '·', error: '!', note: '~' };
const MODAL_ID = 'agents-dashboard';
const FLEET_PAGE_SIZE = 8;
let returnFocus = null;

const state = {
  open: false, rows: [], totals: {}, profiles: [], chats: [], approvals: [], selected: null,
  providerLimits: {},
  events: new Map(), es: null, pollTimer: null, tick: null, launchOpen: false, filter: '',
  catalog: null, configOpen: false, configTab: 'general', configDrafts: new Map(),
  fleetWidth: Number(localStorage.getItem('odysseus-agents-fleet-width') || 380),
  error: '', refreshing: false, refreshQueued: false,
  // Triage bucket: 'all' | 'attention' | 'active' | 'recent' (see BUCKETS).
  bucket: 'all',
  fleetPage: 0,
  detailTab: 'overview',
  archiveView: false,
  detailDrafts: new Map(),
  // Parent session ids whose worker chats are unfolded under them in Recent.
  expandedParents: new Set(),
  compactFleet: localStorage.getItem('odysseus-agents-fleet-density') !== 'expanded',
};

async function api(path, opts = {}) {
  const r = await fetch(path, Object.assign({ credentials: 'same-origin' }, opts));
  let body = null;
  try { body = await r.json(); } catch (_) {}
  if (!r.ok) { const e = new Error((body && (body.detail || body.error)) || `${r.status}`); e.status = r.status; throw e; }
  return body;
}
// Reads fired by our own timers, not by the user. The header keeps the
// foreground gate from treating "a tab is open" as "the user is working", which
// would cancel the very background runs this dashboard exists to display.
async function apiPoll(path) {
  return api(path, { headers: { 'X-Odysseus-Poll': '1' } });
}
const post = (path, data) => api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data || {}) });
function fmtDur(a, b) {
  if (!a) return '';
  const s = Math.max(0, Math.round((b || Date.now() / 1000) - a));
  return s < 60 ? `${s}s` : s < 3600 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}
function fmtTime(ts) { return ts ? new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' }) : ''; }
function pill(status) { const [label, cls] = STATUS[status] || [status, '']; return `<span class="wb-pill ${cls}"><i aria-hidden="true"></i>${esc(label)}</span>`; }
function chip(source) { return `<span class="wb-chip wb-src-${esc(source)}">${esc(SOURCE_LABEL[source] || source)}</span>`; }

function hashUnit(value) {
  let hash = 2166136261;
  for (const ch of String(value || 'agent')) { hash ^= ch.charCodeAt(0); hash = Math.imul(hash, 16777619); }
  return Math.abs(hash >>> 0);
}
function robotHtml(agent, size = '') {
  const key = agent?.session_id || agent?.run_id || agent?.name || agent?.title || 'agent';
  const hue = hashUnit(key) % 360;
  const status = agent?.status === 'completed' ? 'finished' : (agent?.status || 'idle');
  const unit = String((hashUnit(key) % 99) + 1).padStart(2, '0');
  return `<span class="ag-bot ag-bot-${esc(status)}${size ? ` ag-bot-${esc(size)}` : ''}" style="--ag-bot-h:${hue}" aria-hidden="true">
    <span class="ag-bot-antenna"><i></i></span>
    <span class="ag-bot-head"><i class="ag-bot-eye"></i><i class="ag-bot-eye"></i><b></b></span>
    <span class="ag-bot-body"><i></i><small>${unit}</small></span>
  </span>`;
}
// ── data ──────────────────────────────────────────────────────────────────
async function refresh() {
  if (state.refreshing) { state.refreshQueued = true; return; }
  state.refreshing = true;
  try {
    const current = window.sessionModule?.getCurrentSessionId?.() || '';
    const overviewArgs = new URLSearchParams();
    if (current) overviewArgs.set('current_session', current);
    if (state.archiveView) overviewArgs.set('archived', 'true');
    const overviewRequest = apiPoll(`/api/agents/overview${overviewArgs.size ? `?${overviewArgs}` : ''}`);
    const [ov, ap] = await Promise.all([overviewRequest, apiPoll('/api/agents/approvals')]);
    state.rows = ov.rows || []; state.totals = ov.totals || {}; state.profiles = ov.profiles || []; state.chats = ov.chats || []; state.providerLimits = ov.provider_limits || {};
    state.approvals = ap.approvals || [];
    state.error = '';
    if (state.selected && !state.rows.some((r) => r.session_id === state.selected)) state.selected = null;
    if (!state.selected && state.rows.length) state.selected = state.rows[0].session_id;
  } catch (e) {
    if (e.status === 401 || e.status === 403) { close(); return; }
    state.error = e.message || 'Agents are temporarily unavailable';
  } finally {
    state.refreshing = false;
  }
  updateBadges();
  // Do not replace the dashboard shell here. A run finishing used to rebuild
  // the entire page, which stole focus, erased half-written steer/reply text,
  // closed the launch form, and visibly flashed the UI. Refresh only the data
  // regions and preserve the controls the user is working in.
  if (state.open) updateOpenView();
  if (state.refreshQueued) {
    state.refreshQueued = false;
    queueMicrotask(refresh);
  }
}
function connect() {
  if (state.es) return;
  const es = new EventSource('/api/agents/stream');
  state.es = es;
  es.onmessage = (m) => {
    let ev; try { ev = JSON.parse(m.data); } catch (_) { return; }
    if (!ev || ev.type === 'ready' || !ev.session_id) return;
    const list = state.events.get(ev.session_id) || [];
    list.push(ev); if (list.length > 400) list.splice(0, list.length - 400);
    state.events.set(ev.session_id, list);
    notifyFor(ev);
    if (['run_started', 'run_finished', 'status', 'note'].includes(ev.kind)) scheduleRefresh();
    else if (state.open && ev.session_id === state.selected) renderDetail();
  };
}
/** Desktop notifications for the two things worth interrupting for: an agent
 *  blocked on an approval, and a worker finishing — only when the user isn't
 *  already looking at that chat or the dashboard. */
const _notified = new Set();
function chatName(sid) { return (state.rows.find((r) => r.session_id === sid) || state.chats.find((c) => c.id === sid) || {}).name || 'a chat'; }
function notifyFor(ev) {
  if (!('Notification' in window) || Notification.permission !== 'granted') return;
  const d = ev.data || {};
  let title = '', body = '', key = '';
  if (ev.kind === 'status' && d.approval_id) {
    title = 'Approval needed'; body = `${d.tool || 'A tool'} in ${chatName(ev.session_id)}: ${String(ev.title || '').replace(/^Waiting for approval:\s*/, '')}`; key = `ap:${d.approval_id}`;
  } else if (ev.kind === 'run_finished' && ev.source === 'session' && /^Worker/.test(ev.title || '')) {
    title = d.status === 'failed' ? 'Worker failed' : 'Worker finished'; body = ev.title; key = `run:${ev.run_id}`;
  } else if (ev.kind === 'run_finished' && ev.source === 'claude_code') {
    title = d.status === 'failed' ? 'Claude Code failed' : 'Claude Code finished'; body = ev.title; key = `run:${ev.run_id}`;
  }
  if (!title || _notified.has(key)) return;
  _notified.add(key);
  const viewing = !document.hidden && (state.open || window.sessionModule?.getCurrentSessionId?.() === ev.session_id);
  if (viewing && !d.approval_id) return;
  const n = new Notification(title, { body: String(body).slice(0, 180), tag: key });
  n.onclick = () => { window.focus(); if (d.approval_id) { state.selected = ev.session_id; open(); } else window.sessionModule?.selectSession?.(ev.session_id); n.close(); };
  setTimeout(() => n.close(), 15000);
}
function disconnect() { if (state.es) { try { state.es.close(); } catch (_) {} state.es = null; } }
let _refreshTimer = null;
function scheduleRefresh() { if (_refreshTimer) return; _refreshTimer = setTimeout(() => { _refreshTimer = null; refresh(); }, 400); }
async function loadHistory(sid) {
  if (state.events.has(sid)) return;
  try {
    const h = await api(`/api/workbench/activity?session_id=${encodeURIComponent(sid)}&limit=300`);
    state.events.set(sid, h.events || []);
  } catch (_) {
    // Non-admin: history comes only from run events.
    const row = state.rows.find((r) => r.session_id === sid);
    const list = [];
    for (const c of (row?.children || [])) {
      try { const r = await api(`/api/agents/runs/${encodeURIComponent(c.run_id)}/events`); list.push(...(r.events || [])); } catch (_) {}
    }
    list.sort((a, b) => (a.seq || 0) - (b.seq || 0));
    state.events.set(sid, list);
  }
}
function updateBadges() {
  const attention = (state.totals.waiting_approval || 0);
  const running = (state.totals.running || 0);
  const badge = $('rail-agents-badge');
  if (badge) {
    badge.hidden = !(attention || running);
    badge.textContent = attention ? String(attention) : String(running);
    badge.classList.toggle('attn', attention > 0);
  }
  const dot = $('tool-agents-dot');
  if (dot) dot.style.display = attention ? 'inline-block' : 'none';
  $('rail-agents')?.classList.toggle('rail-notify', attention > 0);
  const summary = $('ag-window-summary');
  if (summary) summary.textContent = `${running} active · ${attention} waiting · ${state.totals.finished_24h || 0} completed today`;
}
async function loadCatalog() {
  if (state.catalog) return state.catalog;
  state.catalog = await api('/api/agents/catalog');
  return state.catalog;
}

// ── triage ────────────────────────────────────────────────────────────────
// Three buckets, because that is how many decisions the page actually supports:
// something is blocked on you, something is working, everything else is history.
// The six status groups this replaced put "Failed / Finished / Stopped / Workers
// & recent" under four separate headers that all mean "not running".
const BUCKETS = [
  ['attention', 'Needs you', (r) => r.status === 'waiting_approval'],
  ['active', 'Active', (r) => r.status === 'running'],
  ['recent', 'Recent', (r) => r.status !== 'waiting_approval' && r.status !== 'running'],
];

/** Whether the Workbench shortcut is worth offering. Workbench is admin-gated
 *  and workbench.js hides its rail button on a 403, so mirror that signal
 *  rather than showing a button whose only outcome is an error toast. */
function workbenchAvailable() {
  const btn = $('rail-workbench') || $('tool-workbench-btn');
  return !!(window.workbenchModule?.open && btn && btn.style.display !== 'none');
}

/** The live-connection dot. Replaces a Refresh button on a page that already
 *  polls every 5s and streams SSE — the honest thing to show is whether the
 *  feed is up, not a control implying it isn't. Still clickable to force one. */
function liveHtml() {
  const live = !!state.es;
  return `<button type="button" class="ag-live${live ? ' on' : ''}" data-ag="refresh"
    title="${live ? 'Live — click to refresh now' : 'Reconnecting — click to refresh now'}">
    <i aria-hidden="true"></i>${live ? 'Live' : 'Reconnecting'}</button>`;
}

function triageHtml() {
  const t = state.totals;
  const counts = { attention: t.waiting_approval || 0, active: t.running || 0, recent: null };
  const seg = BUCKETS.map(([key, label]) => {
    const n = counts[key];
    const on = state.bucket === key;
    const attn = key === 'attention' && n > 0;
    return `<button type="button" class="ag-seg${on ? ' on' : ''}${attn ? ' attn' : ''}"
      data-ag="bucket" data-bucket="${key}" aria-pressed="${on ? 'true' : 'false'}">${label}${
      n == null ? '' : `<b>${n}</b>`}</button>`;
  }).join('');
  // Context, not controls — one muted line. This used to sit above a strip of
  // four tiles ("Units active", "Need you", "Workers live", "Done today") that
  // restated the very same four numbers in a louder style, so the header
  // published every count three times: on the segment badges, on the tiles, and
  // here. The tiles are gone; this line is the single place the non-filterable
  // numbers live.
  const hist = [
    t.workers_running ? `${t.workers_running} worker${t.workers_running === 1 ? '' : 's'} live` : '',
    `${t.finished_24h || 0} done today`,
    t.failed_24h ? `<em class="ag-hist-bad">${t.failed_24h} failed</em>` : '',
  ].filter(Boolean).join(' · ');
  return `<div class="ag-seg-group" role="group" aria-label="Filter agents">${seg}</div>
    <span class="ag-hist">${hist}</span>`;
}

// ── render ────────────────────────────────────────────────────────────────
function render() {
  const root = $('agents-dashboard');
  if (!root || !state.open) return;
  const surface = $('agents-dashboard-body');
  if (!surface) return;
  const archiveToggle = `<button type="button" class="wb-btn wb-btn-ghost" data-ag="archive-view">${state.archiveView ? 'Back to fleet' : 'Archived'}</button>`;
  const headActions = state.archiveView ? archiveToggle : `${archiveToggle}<button type="button" class="wb-btn wb-btn-primary" data-ag="launch">Launch worker</button>${workbenchAvailable() ? '<button type="button" class="wb-btn wb-btn-ghost" data-ag="workbench" title="Repository changes, commits and PRs">Workbench</button>' : ''}`;
  // One header row. It used to carry a second window title ("Mission floor",
  // under a title bar already reading "Agent Control Room"), and an Expand
  // button doing exactly what the title bar's maximize button does.
  surface.innerHTML = `
    <div class="ag-head">
      ${state.configOpen
        ? '<button type="button" class="wb-btn wb-btn-ghost" data-ag="config-back">← Monitoring</button>'
        : `<div class="ag-triage">${triageHtml()}</div>`}
      <div class="ag-head-actions">
        ${liveHtml()}
        ${state.configOpen ? '' : headActions}
      </div>
    </div>
    <div class="ag-refresh-error" id="ag-refresh-error" role="status"${state.error ? '' : ' hidden'}>${esc(state.error)}</div>
    ${state.configOpen ? loadoutWorkspaceHtml() : `<div class="ag-body" style="--ag-fleet-width:${Math.max(250, state.fleetWidth || 380)}px">
      <aside class="ag-fleet wb-card ${state.compactFleet ? 'ag-fleet-compact' : 'ag-fleet-expanded'}">
        <div class="ag-fleet-tools"><input type="search" class="wb-input" id="ag-filter" placeholder="Filter units…" value="${esc(state.filter)}" aria-label="Filter agents"><button type="button" class="wb-btn wb-btn-sm wb-btn-ghost" data-ag="fleet-density" aria-pressed="${state.compactFleet}" title="${state.compactFleet ? 'Use larger cards' : 'Use compact cards'}">${state.compactFleet ? 'Compact' : 'Large'}</button></div>
        <div class="ag-fleet-list" data-wb-scroll="fleet">${fleetHtml()}</div>
      </aside>
      <div class="ag-pane-splitter" data-ag-splitter role="separator" aria-orientation="vertical" aria-label="Resize fleet and agent details" tabindex="0"><span></span></div>
      <section class="ag-detail wb-card" id="ag-detail"></section>
      ${state.launchOpen ? `<aside class="ag-launch wb-card" id="ag-launch">${launchHtml()}</aside>` : ''}
    </div>`}`;
  if (!state.configOpen) renderDetail();
  else {
    const pluginPicker = surface.querySelector('[data-ag-plugin-picker]');
    const selectedAgent = state.rows.find((item) => item.session_id === state.selected) || state.rows[0];
    if (pluginPicker && selectedAgent && window.OdysseusPluginCatalog?.mount) {
      window.OdysseusPluginCatalog.mount(pluginPicker, {
        sessionId: selectedAgent.session_id,
        api,
        onApplied: (result) => applyPluginSettings(selectedAgent, result),
      });
    }
  }
  $('ag-filter')?.addEventListener('input', (e) => { state.filter = e.target.value; state.fleetPage = 0; renderFleetOnly(); });
}
function filteredRows() {
  const q = state.filter.trim().toLowerCase();
  return state.rows.filter((r) => !q || (r.name || '').toLowerCase().includes(q) || (r.latest || '').toLowerCase().includes(q));
}
/** Parent → worker rows among `rows`, and the rows with no listed parent.
 *  A worker whose parent is filtered out or archived stands on its own. */
function fleetTree(rows) {
  const byId = new Map(rows.map((row) => [row.session_id, row]));
  const kids = new Map();
  rows.forEach((row) => {
    const parent = row.parent_session;
    if (!parent || parent === row.session_id || !byId.has(parent)) return;
    if (!kids.has(parent)) kids.set(parent, []);
    kids.get(parent).push(row);
  });
  const roots = rows.filter((row) => !row.parent_session || row.parent_session === row.session_id || !byId.has(row.parent_session));
  // A parent loop would leave its members with no root; list them flat.
  const reached = new Set();
  roots.forEach((root) => { reached.add(root.session_id); descendants(root, kids).forEach((row) => reached.add(row.session_id)); });
  rows.forEach((row) => { if (!reached.has(row.session_id)) { roots.push(row); reached.add(row.session_id); } });
  return { roots, kids };
}
function descendants(row, kids, seen = new Set([row.session_id])) {
  const out = [];
  (kids.get(row.session_id) || []).forEach((child) => {
    if (seen.has(child.session_id)) return;
    seen.add(child.session_id);
    out.push(child, ...descendants(child, kids, seen));
  });
  return out;
}
function isLiveAgent(row) { return row.status === 'running' || row.status === 'waiting_approval'; }
/** A card plus the workers under it: live (or selected) ones always, the rest
 *  only while the parent is unfolded. */
function treeHtml(row, kids, seen = new Set()) {
  seen.add(row.session_id);
  const workers = (kids.get(row.session_id) || []).filter((child) => !seen.has(child.session_id));
  if (!workers.length) return rowHtml(row);
  const pinned = (child) => [child, ...descendants(child, kids)].some((node) => isLiveAgent(node) || node.session_id === state.selected);
  const open = state.expandedParents.has(row.session_id);
  const folded = workers.filter((child) => !pinned(child)).length;
  const visible = open ? workers : workers.filter(pinned);
  const inner = visible.map((child) => treeHtml(child, kids, seen)).join('');
  return `${rowHtml(row, { folded, open })}${inner
    ? `<div class="ag-card-workers" role="group" aria-label="Workers of ${esc(row.name)}">${inner}</div>` : ''}`;
}
function fleetHtml() {
  const rows = filteredRows();
  // The segmented control is a real filter now. It used to only jump the
  // selection, so clicking "2 need approval" appeared to do nothing when the
  // first such chat was already selected.
  const shown = state.bucket === 'all' ? BUCKETS : BUCKETS.filter(([key]) => key === state.bucket);
  // Do not turn the fleet into an unbounded scroll just because a user has a
  // long history.  Filtering still searches every visible row, while paging
  // limits the expensive, interactive cards that need to stay easy to scan.
  //
  // Every bucket lists top-level agents; workers sit under their parent's card.
  // A live worker (running or waiting on you) is always shown there, and so is
  // the selected one. Finished workers fold behind a "N workers" toggle. A
  // parent is filed under the most urgent status anywhere in its tree, so a
  // finished chat whose worker is still running is listed once, under Active.
  const { roots, kids } = fleetTree(rows);
  const bucketOf = new Map(roots.map((root) => {
    const tree = [root, ...descendants(root, kids)];
    return [root.session_id, (BUCKETS.find(([, , match]) => tree.some(match)) || BUCKETS[BUCKETS.length - 1])[0]];
  }));
  const shownKeys = new Set(shown.map(([key]) => key));
  const candidates = roots.filter((root) => shownKeys.has(bucketOf.get(root.session_id)));
  const pages = Math.max(1, Math.ceil(candidates.length / FLEET_PAGE_SIZE));
  state.fleetPage = Math.min(Math.max(0, state.fleetPage), pages - 1);
  const pageRows = new Set(candidates.slice(state.fleetPage * FLEET_PAGE_SIZE, (state.fleetPage + 1) * FLEET_PAGE_SIZE).map((row) => row.session_id));
  const html = shown.map(([key, label]) => {
    const items = candidates.filter((row) => bucketOf.get(row.session_id) === key && pageRows.has(row.session_id));
    if (!items.length) return '';
    const solo = shown.length === 1;
    const cards = items.map((row) => treeHtml(row, kids)).join('');
    return `<div class="ag-group${key === 'attention' ? ' ag-group-attn' : ''}">${
      solo ? '' : `<div class="wb-group-h"><span class="wb-group-title">${label}</span><span class="wb-count">${items.length}</span></div>`
    }<div class="ag-card-grid">${cards}</div></div>`;
  }).join('');
  if (html) return `${html}${pages > 1 ? `<nav class="ag-fleet-pages" aria-label="Fleet pages"><button type="button" class="wb-btn wb-btn-sm wb-btn-ghost" data-ag="fleet-page" data-page="${state.fleetPage - 1}"${state.fleetPage === 0 ? ' disabled' : ''}>← Newer</button><span>${state.fleetPage * FLEET_PAGE_SIZE + 1}–${Math.min(candidates.length, (state.fleetPage + 1) * FLEET_PAGE_SIZE)} of ${candidates.length}</span><button type="button" class="wb-btn wb-btn-sm wb-btn-ghost" data-ag="fleet-page" data-page="${state.fleetPage + 1}"${state.fleetPage >= pages - 1 ? ' disabled' : ''}>Older →</button></nav>` : ''}`;
  if (!state.rows.length) return '<div class="wb-empty">Nothing is running. Send a chat a task, or launch a worker.</div>';
  if (state.bucket !== 'all') {
    const label = (BUCKETS.find(([k]) => k === state.bucket) || [, ''])[1];
    return `<div class="wb-empty">Nothing in “${label}”. <button type="button" class="ag-inline-link" data-ag="bucket" data-bucket="all">Show all</button></div>`;
  }
  return '<div class="wb-empty">No chats match.</div>';
}
function renderFleetOnly() {
  const list = $('agents-dashboard')?.querySelector('.ag-fleet-list');
  if (!list) return;
  const top = list.scrollTop;
  const focusedUnit = list.contains(document.activeElement) && document.activeElement.matches('.ag-card-select')
    ? document.activeElement.dataset.sid : null;
  list.innerHTML = fleetHtml();
  list.scrollTop = top;
  if (focusedUnit) list.querySelector(`.ag-card-select[data-sid="${CSS.escape(focusedUnit)}"]`)?.focus({ preventScroll: true });
}
function updateStats() {
  const box = $('agents-dashboard')?.querySelector('.ag-triage');
  if (box) box.innerHTML = triageHtml();
  const live = $('agents-dashboard')?.querySelector('.ag-live');
  if (live) live.outerHTML = liveHtml();
}
function updateOpenView() {
  if (!state.open || !$('agents-dashboard')?.querySelector('.ag-body')) return;
  updateStats();
  renderFleetOnly();
  renderDetail();
  const error = $('ag-refresh-error');
  if (error) { error.textContent = state.error; error.hidden = !state.error; }
}
const PLUGIN_CAPABILITY_KEYS = [
  'tool_access', 'enabled_tools', 'disabled_tools', 'skill_access', 'skill_names',
  'model_access', 'allowed_models', 'allowed_mcp_servers',
];
function applyPluginSettings(row, result) {
  const settings = result?.settings || {};
  row.config = Object.assign({}, row.config || {}, settings);
  const draft = configFor(row);
  // A plugin apply changes capabilities only. Preserve unsaved personality,
  // approval and delegation edits in the open form instead of replacing the
  // whole draft with the server's older copy of those fields.
  PLUGIN_CAPABILITY_KEYS.forEach((key) => {
    if (Object.prototype.hasOwnProperty.call(settings, key)) draft[key] = settings[key];
  });
  draft._explicitToolAccess = Object.prototype.hasOwnProperty.call(settings, 'tool_access')
    || draft._explicitToolAccess;
  draft._catalogReady = false;
  finalizeToolConfig(draft);
  render();
}
function finalizeToolConfig(config) {
  if (!state.catalog) return config;
  const disabled = new Set(config.disabled_tools || []);
  const names = state.catalog.tools.map((tool) => tool.name);
  if (config._explicitToolAccess) {
    if (config.tool_access === 'selected') {
      config.enabled_tools = (config.enabled_tools || []).filter((name) => names.includes(name) && !disabled.has(name));
    } else if (config.tool_access === 'none') {
      config.enabled_tools = [];
    } else {
      config.tool_access = 'all';
      config.enabled_tools = names.filter((name) => !disabled.has(name));
    }
  } else {
    // Backward compatibility for sessions saved before positive tool policy
    // existed: derive access solely from their denylist once.
    config.tool_access = !disabled.size ? 'all' : disabled.size >= names.length ? 'none' : 'selected';
    config.enabled_tools = names.filter((name) => !disabled.has(name));
  }
  config.mcp_access = config.allowed_mcp_servers?.includes('*') ? 'all' : config.allowed_mcp_servers?.length ? 'selected' : 'none';
  config._catalogReady = true;
  return config;
}
function configFor(row) {
  if (state.configDrafts.has(row.session_id)) {
    const existing = state.configDrafts.get(row.session_id);
    if (state.catalog && !existing._catalogReady) {
      finalizeToolConfig(existing);
    }
    return existing;
  }
  const stored = row.config || {};
  const base = Object.assign({
    agent_profile: '', agent_instructions: '', approval_mode: '', memory_access: 'write', skill_access: 'all', skill_names: [],
    model_access: 'all', allowed_models: [], delegation_policy: 'explicit', max_parallel_workers: 1,
    allowed_mcp_servers: ['*'], private_vault_access: false, disabled_tools: [], tool_access: 'all',
  }, stored);
  base._explicitToolAccess = Object.prototype.hasOwnProperty.call(stored, 'tool_access');
  base._catalogReady = false;
  finalizeToolConfig(base);
  state.configDrafts.set(row.session_id, base);
  return base;
}
function option(value, label, current) {
  return `<option value="${esc(value)}"${String(value) === String(current) ? ' selected' : ''}>${esc(label)}</option>`;
}
function capabilityChecks(items, selected, key, labelKey = 'name') {
  const enabled = new Set(selected || []);
  return (items || []).map((item) => {
    const value = String(item.name || item.id || '');
    const label = String(item[labelKey] || value);
    return `<label class="ag-cap-check" title="${esc(item.description || '')}"><input type="checkbox" data-config-list="${key}" value="${esc(value)}"${enabled.has(value) ? ' checked' : ''}><span>${esc(label)}</span></label>`;
  }).join('');
}
function profileConfig(profile) {
  const mcp = profile.mcp_access === 'none' ? [] : profile.mcp_access === 'selected' ? (profile.allowed_mcp_servers || []) : ['*'];
  return {
    // `mcp_access` must be carried explicitly. saveAgentConfig() reads it to
    // decide what to persist, and defaults to ['*'] — every connected server —
    // when it is missing. Without this line, applying a preset that grants no
    // MCP access (or a narrowed list) rendered correctly, then saved as full
    // access: the editor showed one policy and the server stored another.
    mcp_access: profile.mcp_access || 'all',
    agent_profile: profile.name || '', agent_instructions: profile.instructions || '', approval_mode: profile.approval_mode === 'inherit' ? '' : (profile.approval_mode || ''),
    memory_access: profile.memory_access || 'read', skill_access: profile.skill_access || 'all', skill_names: [...(profile.skill_names || [])],
    model_access: profile.model_access || 'current', allowed_models: [...(profile.allowed_models || [])],
    delegation_policy: profile.delegation_policy || 'explicit', max_parallel_workers: profile.max_parallel_workers ?? 1,
    allowed_mcp_servers: mcp, private_vault_access: !!profile.private_vault_access,
    disabled_tools: [...(profile.disabled_tools || [])], tool_access: profile.tool_access || 'all',
    enabled_tools: [...(profile.enabled_tools || [])],
    _catalogReady: true,
  };
}
function loadoutSummaryHtml(row) {
  const c = configFor(row);
  const mcp = c.allowed_mcp_servers?.includes('*') ? 'all connections' : `${c.allowed_mcp_servers?.length || 0} connections`;
  return `<button type="button" class="ag-loadout-summary" data-ag="config-toggle">
    <span class="ag-loadout-glyph" aria-hidden="true">⌬</span><span><b>Agent loadout</b><small>${esc(c.agent_profile || 'custom')} · ${esc(c.delegation_policy)} delegation · ${esc(c.memory_access)} memory · ${esc(mcp)}</small></span><i>Open editor</i>
  </button>`;
}
function configEditorHtml(row) {
  const c = configFor(row);
  const catalog = state.catalog;
  if (!catalog) return '<div class="ag-loadout-editor"><div class="wb-empty">Loading capabilities…</div></div>';
  const profileOptions = state.profiles.map((p) => option(p.name, `${p.name}${p.description ? ` — ${p.description}` : ''}`, c.agent_profile)).join('');
  const disabled = new Set(c.disabled_tools || []);
  const toolSelected = c.tool_access === 'all' ? catalog.tools.map((t) => t.name)
    : c.tool_access === 'none' ? [] : (c.enabled_tools || catalog.tools.filter((t) => !disabled.has(t.name)).map((t) => t.name));
  const toolGroups = new Map();
  catalog.tools.forEach((tool) => { const group = tool.group || 'Other'; if (!toolGroups.has(group)) toolGroups.set(group, []); toolGroups.get(group).push(tool); });
  const groupedTools = [...toolGroups.entries()].map(([group, tools]) => `<fieldset class="ag-cap-subgroup"><legend>${esc(group)}</legend>${capabilityChecks(tools, toolSelected, 'enabled_tools')}</fieldset>`).join('');
  const mcpSelected = c.allowed_mcp_servers?.includes('*') ? catalog.mcp_servers.map((m) => m.id) : (c.allowed_mcp_servers || []);
  const tab = state.configTab || 'general';
  const tabButton = (id, label, summary) => `<button type="button" class="ag-config-tab${tab === id ? ' active' : ''}" data-ag="config-tab" data-tab="${id}" aria-selected="${tab === id ? 'true' : 'false'}"><span>${label}</span><small>${summary}</small></button>`;
  const generalPanel = `<div class="ag-config-panel ag-config-general" data-config-panel="general">
    <div class="ag-panel-heading"><div><b>Behavior</b><span>Decide how independently this agent may operate.</span></div></div>
    <label class="ag-field"><span>Personality & instructions</span><textarea class="wb-input ag-textarea" rows="5" maxlength="8000" data-config="agent_instructions" placeholder="How this agent should communicate and approach its work">${esc(c.agent_instructions || '')}</textarea><small>Scoped to this agent. Platform security and capability policy always take priority.</small></label>
    <div class="ag-policy-grid">
      <label class="ag-field"><span>Delegation</span><select class="wb-select" data-config="delegation_policy">${option('never','Never delegate',c.delegation_policy)}${option('explicit','Only when I ask',c.delegation_policy)}${option('auto','Agent decides',c.delegation_policy)}</select><small>Controls sub-agents and coding-agent handoffs.</small></label>
      <label class="ag-field"><span>Approvals</span><select class="wb-select" data-config="approval_mode">${option('','Use global default',c.approval_mode)}${option('ask_risky','Ask for risky actions',c.approval_mode)}${option('ask_all','Ask for every change',c.approval_mode)}${option('auto','Run automatically',c.approval_mode)}</select><small>Human checkpoint before tools change things.</small></label>
      <label class="ag-field"><span>Child workers for this agent</span><input class="wb-input" type="number" min="0" max="8" data-config="max_parallel_workers" value="${esc(c.max_parallel_workers)}"><small>0 disables children; maximum 8. Separate from the provider-wide Claude Code capacity (${esc(state.providerLimits.claude_code || catalog.provider_limits?.claude_code || 1)}).</small></label>
    </div>
  </div>`;
  const toolsPanel = `<div class="ag-config-panel" data-config-panel="tools">
    <div class="ag-panel-heading"><div><b>Action tools</b><span>Choose what this agent can do. Unselected tools are blocked during execution.</span></div><label class="ag-panel-access">Access<select class="wb-select" data-config="tool_access">${option('all','All tools',c.tool_access)}${option('selected','Selected tools',c.tool_access)}${option('none','No action tools',c.tool_access)}</select></label></div>
    <div class="ag-cap-grid ag-tool-grid" data-show-when="tool_access:selected"${c.tool_access === 'selected' ? '' : ' hidden'}>${groupedTools}</div>
    ${c.tool_access === 'selected' && !catalog.tools.length ? '<div class="wb-empty">No tools are available to this account.</div>' : ''}
  </div>`;
  const knowledgePanel = `<div class="ag-config-panel" data-config-panel="knowledge">
    <div class="ag-panel-heading"><div><b>Knowledge & memory</b><span>Control persistent memory, reusable skills, and private-note retrieval.</span></div></div>
    <div class="ag-policy-grid ag-knowledge-policy"><label class="ag-field"><span>Memory</span><select class="wb-select" data-config="memory_access">${option('none','No memory',c.memory_access)}${option('read','Read only',c.memory_access)}${option('write','Read and write',c.memory_access)}</select><small>Read-only blocks add, edit and delete.</small></label>
      <label class="ag-field"><span>Skills</span><select class="wb-select" data-config="skill_access">${option('all','All skills',c.skill_access)}${option('selected','Selected skills',c.skill_access)}${option('none','No skills',c.skill_access)}</select><small>Skills add specialized procedures and instructions.</small></label></div>
    <label class="ag-switch-card"><input type="checkbox" data-config="private_vault_access"${c.private_vault_access ? ' checked' : ''}><span><b>Private vault reads</b><small>Allow this agent to retrieve private notes. Human access is unaffected.</small></span></label>
    <div class="ag-cap-grid" data-show-when="skill_access:selected"${c.skill_access === 'selected' ? '' : ' hidden'}>${capabilityChecks(catalog.skills, c.skill_names, 'skill_names')}</div>
  </div>`;
  const connectionsPanel = `<div class="ag-config-panel" data-config-panel="connections">
    <div class="ag-panel-heading"><div><b>Models & integrations</b><span>Limit model switching and connected MCP servers independently.</span></div></div>
    <section class="ag-connection-section"><div class="ag-connection-head"><div><b>Models</b><small>${c.model_access === 'current' ? 'Current model only' : c.model_access === 'all' ? 'All configured models' : `${c.allowed_models?.length || 0} selected`}</small></div><select class="wb-select" data-config="model_access">${option('current','Current model only',c.model_access)}${option('selected','Selected models',c.model_access)}${option('all','All configured models',c.model_access)}</select></div><div class="ag-cap-grid" data-show-when="model_access:selected"${c.model_access === 'selected' ? '' : ' hidden'}>${capabilityChecks(catalog.models.map((name) => ({name})), c.allowed_models, 'allowed_models')}</div></section>
    <section class="ag-connection-section"><div class="ag-connection-head"><div><b>MCP & integrations</b><small>${c.allowed_mcp_servers?.includes('*') ? 'All connected servers' : `${c.allowed_mcp_servers?.length || 0} selected`}</small></div><select class="wb-select" data-config="mcp_access">${option('all','All connected',c.allowed_mcp_servers?.includes('*') ? 'all' : c.allowed_mcp_servers?.length ? 'selected' : 'none')}${option('selected','Selected connections',c.allowed_mcp_servers?.includes('*') ? 'all' : c.allowed_mcp_servers?.length ? 'selected' : 'none')}${option('none','No connections',c.allowed_mcp_servers?.includes('*') ? 'all' : c.allowed_mcp_servers?.length ? 'selected' : 'none')}</select></div><div class="ag-cap-grid" data-show-when="mcp_access:selected"${!c.allowed_mcp_servers?.includes('*') && c.allowed_mcp_servers?.length ? '' : ' hidden'}>${capabilityChecks(catalog.mcp_servers, mcpSelected, 'allowed_mcp_servers', 'name')}</div></section>
  </div>`;
  const panels = { general: generalPanel, tools: toolsPanel, knowledge: knowledgePanel, connections: connectionsPanel };
  return `<div class="ag-loadout-editor" data-session="${esc(row.session_id)}">
    <div class="ag-preset-row"><label><span>Start from preset</span><select class="wb-select" data-config="agent_profile"><option value="">Custom loadout</option>${profileOptions}</select></label><small>Choosing a preset loads its settings; this agent keeps its own copy once saved.</small></div>
    <div class="ag-config-tabs" role="tablist" aria-label="Loadout sections">${tabButton('general','Behavior',`${c.delegation_policy} delegation`)}${tabButton('tools','Tools',c.tool_access === 'all' ? 'all available' : `${toolSelected.length} enabled`)}${tabButton('knowledge','Knowledge',`${c.memory_access} memory`)}${tabButton('connections','Models & MCP',c.model_access === 'current' ? 'current model' : c.model_access)}</div>
    <div class="ag-config-panel-scroll">${panels[tab] || generalPanel}</div>
    <div data-ag-plugin-picker></div>
    <div class="ag-config-actions"><span id="ag-config-msg"></span><button type="button" class="wb-btn wb-btn-primary" data-ag="save-config">Save loadout</button></div>
  </div>`;
}

function loadoutWorkspaceHtml() {
  const row = state.rows.find((item) => item.session_id === state.selected) || state.rows[0];
  if (!row) return '<section class="ag-loadout-workspace wb-card"><div class="wb-empty">No agent is available to configure.</div></section>';
  if (row.session_id !== state.selected) state.selected = row.session_id;
  const choices = state.rows.map((item) => `<option value="${esc(item.session_id)}"${item.session_id === row.session_id ? ' selected' : ''}>${esc(item.name)} — ${esc(STATUS[item.status]?.[0] || item.status)}</option>`).join('');
  return `<section class="ag-loadout-workspace wb-card">
    <div class="ag-loadout-workspace-head"><div class="ag-loadout-agent"><span class="ag-loadout-agent-avatar">${robotHtml(row, 'mini')}</span><label><span>Editing agent</span><select class="wb-select" data-config-agent>${choices}</select></label></div><div class="ag-loadout-context">${pill(row.status)}<span>${esc(row.model || 'Default model')}</span></div></div>
    ${configEditorHtml(row)}
  </section>`;
}
function rowHtml(r, nest = {}) {
  const sel = r.session_id === state.selected;
  const dur = r.status === 'running' && r.started_at ? fmtDur(r.started_at) : '';
  // Two lines, not three. The model/profile/worker chips used to occupy a whole
  // row of their own above the one line that says what the agent is doing; they
  // are now a muted prefix on that same line, so twice as many agents fit on
  // screen and the eye lands on the status text.
  const meta = [
    r.profile ? esc(r.profile) : '',
    r.model ? esc(String(r.model).split('/').pop()) : '',
    r.children_running ? `${r.children_running}w` : '',
  ].filter(Boolean).join(' · ');
  const blocked = r.pending_approvals
    ? `<span class="ag-row-attn">${r.pending_approvals} approval${r.pending_approvals === 1 ? '' : 's'}</span>` : '';
  const children = (r.children || []).slice(0, 5);
  const crew = children.length ? `<div class="ag-card-crew" title="${r.children?.length || 0} attached workers"><span class="ag-crew-line"></span>${children.map(c => robotHtml(c, 'micro')).join('')}${r.children.length > children.length ? `<b>+${r.children.length - children.length}</b>` : ''}</div>` : '';
  const status = r.status || 'idle';
  return `<div class="ag-row ag-bot-card ag-card-${esc(status)}${sel ? ' active' : ''}${status === 'waiting_approval' ? ' attn' : ''}" style="--ag-card-h:${hashUnit(r.session_id) % 360}" data-sid="${esc(r.session_id)}" role="group" aria-label="${esc(r.name)}">
    <div class="ag-card-beacon" aria-hidden="true"></div>
    <div class="ag-card-avatar">${robotHtml(r)}</div>
    <div class="ag-card-copy">
      <div class="ag-row-top"><button type="button" class="ag-row-name ag-card-select" data-ag="select-agent" data-sid="${esc(r.session_id)}" aria-pressed="${sel ? 'true' : 'false'}" title="Inspect ${esc(r.name)}">${esc(r.name)}</button>${dur ? `<span class="ag-row-dur" data-started="${r.started_at}">${esc(dur)}</span>` : ''}</div>
      <div class="ag-card-status">${pill(status)}${blocked}</div>
      <div class="ag-row-sub">${meta ? `<span class="ag-row-meta-inline">${meta}</span>` : ''}${r.latest ? `<span class="ag-row-latest" title="${esc(r.latest)}">${esc(r.latest)}</span>` : '<span class="ag-row-latest">Standing by</span>'}</div>
      ${crew}
      ${nest.folded ? `<button type="button" class="ag-workers-toggle" data-ag="toggle-workers" data-sid="${esc(r.session_id)}" aria-expanded="${nest.open ? 'true' : 'false'}" title="${nest.open ? 'Hide finished workers' : 'Show finished workers'}">${nest.open ? '▾' : '▸'} ${nest.folded} finished worker${nest.folded === 1 ? '' : 's'}</button>` : ''}
    </div>
    <div class="ag-card-actions"><button type="button" class="wb-icon-btn" data-ag="open-chat" data-sid="${esc(r.session_id)}" title="Open chat" aria-label="Open ${esc(r.name)} chat">↗</button>${status === 'running' ? `<button type="button" class="wb-icon-btn" data-ag="stop-chat" data-sid="${esc(r.session_id)}" title="Stop agent" aria-label="Stop ${esc(r.name)}">■</button>` : ''}</div>
  </div>`;
}
function renderDetail() {
  const box = $('ag-detail');
  if (!box) return;
  // Capture the outgoing agent before a selection changes.  renderDetail is
  // also called after `state.selected` has already changed, so limiting this
  // to the new selection would silently lose a half-written steer/reply.
  const renderedSid = box.dataset.sessionId;
  if (renderedSid) {
    const outgoing = {};
    box.querySelectorAll('textarea[id]').forEach((el) => { outgoing[el.id] = el.value; });
    if (Object.keys(outgoing).length) state.detailDrafts.set(renderedSid, Object.assign({}, state.detailDrafts.get(renderedSid), outgoing));
  }
  const active = box.contains(document.activeElement) ? document.activeElement : null;
  const activeId = active?.id || '';
  const selection = active && typeof active.selectionStart === 'number' ? [active.selectionStart, active.selectionEnd] : null;
  const draft = {};
  const sameSelection = box.dataset.sessionId === state.selected;
  if (sameSelection) box.querySelectorAll('textarea[id]').forEach((el) => { draft[el.id] = el.value; });
  const r = state.rows.find((x) => x.session_id === state.selected);
  if (!r) { delete box.dataset.sessionId; box.innerHTML = '<div class="wb-empty">Select a chat to see what its agent is doing.</div>'; return; }
  box.dataset.sessionId = r.session_id;
  const scroll = box.querySelector('[data-wb-scroll="detail-tab"]');
  const keep = scroll ? scroll.scrollTop : null;
  const approvals = state.approvals.filter((a) => a.session_id === r.session_id);
  const running = r.status === 'running' || r.status === 'waiting_approval';
  const events = (state.events.get(r.session_id) || []).slice(-200).reverse();
  const children = r.children || [];
  const tab = ['overview', 'activity', 'steering'].includes(state.detailTab) ? state.detailTab : 'overview';
  const tabs = [['overview', 'Overview'], ['activity', 'Activity'], ['steering', 'Steering']]
    .map(([key, label]) => `<button type="button" id="ag-tab-${key}" class="ag-detail-tab${tab === key ? ' active' : ''}" data-ag="detail-tab" data-tab="${key}" role="tab" aria-label="${label} for ${esc(r.name)}" aria-controls="ag-panel-${key}" aria-selected="${tab === key}" tabindex="${tab === key ? '0' : '-1'}">${label}</button>`).join('');
  const overview = `
    ${loadoutSummaryHtml(r)}
    ${(r.hidden_run_ids || []).length ? `<div class="ag-hidden-runs"><span>${r.hidden_run_ids.length} completed run card${r.hidden_run_ids.length === 1 ? '' : 's'} hidden</span><button type="button" class="wb-btn wb-btn-sm wb-btn-ghost" data-ag="restore-runs" data-sid="${esc(r.session_id)}">Restore cards</button></div>` : ''}
    ${approvals.length || children.length ? `<div class="ag-detail-top">
      ${approvals.length ? `<div class="ag-section"><div class="wb-group-h"><span class="wb-group-title">Waiting for your approval</span><span class="wb-count">${approvals.length}</span></div>${approvals.map(approvalHtml).join('')}</div>` : ''}
      ${children.length ? `<div class="ag-section"><div class="wb-group-h"><span class="wb-group-title">Child workers & jobs</span><span class="wb-count">${children.length}</span></div>${children.map(childHtml).join('')}</div>` : ''}
      ${!approvals.length && !children.length ? '<div class="wb-empty">No child workers or pending approvals.</div>' : ''}
    </div>` : '<div class="wb-empty">No child workers or pending approvals.</div>'}`;
  const activity = `<div class="ag-section ag-events"><div class="wb-group-h"><span class="wb-group-title">Live events</span><span class="wb-count">${events.length}</span></div><div class="wb-ev-list ag-ev-list">${events.map(eventHtml).join('') || '<div class="wb-empty">No events yet.</div>'}</div></div>`;
  const steering = `<div class="ag-section ag-compose">
      ${running
        ? `<label class="ag-compose-label" for="ag-steer">Steer <small>lands before the agent's next step${r.steer_queued ? ` · ${r.steer_queued} waiting to be picked up` : ''}</small></label><div class="ag-compose-row"><textarea id="ag-steer" class="wb-input ag-textarea" rows="2" placeholder="e.g. Skip the tests for now and focus on the migration"></textarea><button type="button" class="wb-btn wb-btn-primary" data-ag="steer" data-sid="${esc(r.session_id)}">Steer</button></div>`
        : `<label class="ag-compose-label" for="ag-reply">Send a message <small>opens the chat and sends it</small></label><div class="ag-compose-row"><textarea id="ag-reply" class="wb-input ag-textarea" rows="2" placeholder="Next task for this chat…"></textarea><button type="button" class="wb-btn wb-btn-primary" data-ag="reply" data-sid="${esc(r.session_id)}">Send</button></div>`}
      ${steerLogHtml(r)}
    </div>`;
  // The identity and primary actions deliberately stay outside the tab scroll.
  // This keeps Stop/Archive available even when a child produced a long log.
  box.innerHTML = `
    <div class="ag-console-hero">
      <div class="ag-console-robot-bay">${robotHtml(r, 'hero')}</div>
      <div class="ag-console-identity">
        <span class="ag-detail-name" title="${esc(r.name)}">${esc(r.name)}</span>
        <div class="ag-detail-meta">${r.model ? `<span class="wb-meta-item">${esc(r.model)}</span>` : ''}${r.started_at ? `<span class="wb-meta-item">started ${esc(fmtTime(r.started_at))}</span>` : ''}${r.is_current ? '<span class="wb-meta-item">open chat</span>' : ''}</div>
      </div>
      <div class="ag-console-status">
        <span class="ag-console-state">${pill(r.status)}${r.started_at ? `<strong class="ag-row-dur" data-started="${r.started_at}">${esc(fmtDur(r.started_at))}</strong>` : ''}</span>
        <span class="ag-console-actions"><button type="button" class="wb-btn wb-btn-sm" data-ag="open-chat" data-sid="${esc(r.session_id)}">Open chat</button>${
          r.parent_session ? `<button type="button" class="wb-btn wb-btn-sm wb-btn-ghost" data-ag="open-chat" data-sid="${esc(r.parent_session)}" title="Parent agent: ${esc(r.parent_name || r.parent_session)}">↳ ${esc(r.parent_name || 'parent')}</button>` : ''}${
          r.status === 'running' ? `<button type="button" class="wb-btn wb-btn-sm" data-ag="stop-chat" data-sid="${esc(r.session_id)}">Stop</button>` : ''}${
          r.archived ? `<button type="button" class="wb-btn wb-btn-sm wb-btn-ghost" data-ag="restore-agent" data-sid="${esc(r.session_id)}">Restore</button>` : !running ? `<button type="button" class="wb-btn wb-btn-sm wb-btn-ghost" data-ag="archive-agent" data-sid="${esc(r.session_id)}" title="Hide this idle chat; its chat and run history stay preserved">Archive</button>` : ''}</span>
      </div>
    </div>
    <div class="ag-detail-tabs" role="tablist" aria-label="${esc(r.name)} details">${tabs}</div>
    <div class="ag-detail-tab-panel" id="ag-panel-${tab}" role="tabpanel" aria-labelledby="ag-tab-${tab}" data-wb-scroll="detail-tab">${tab === 'overview' ? overview : tab === 'activity' ? activity : steering}</div>`;
  if (keep != null) { const s2 = box.querySelector('[data-wb-scroll="detail-tab"]'); if (s2) s2.scrollTop = keep; }
  Object.entries(draft).forEach(([id, value]) => { const el = $(id); if (el) el.value = value; });
  Object.entries(state.detailDrafts.get(r.session_id) || {}).forEach(([id, value]) => { const el = $(id); if (el) el.value = value; });
  if (sameSelection && activeId) {
    const next = $(activeId);
    if (next) {
      next.focus({ preventScroll: true });
      if (selection && next.setSelectionRange) next.setSelectionRange(selection[0], selection[1]);
    }
  }
  if (!state.events.has(r.session_id)) loadHistory(r.session_id).then(() => { if (state.selected === r.session_id) renderDetail(); });
}
/** The steering log for the selected chat: one row per message, newest first.
 *
 *  The Steer box used to be write-only — it said "2 queued", then the number
 *  went away, and nothing distinguished "the agent read them" from "the turn
 *  ended and they were dropped". A queued message that silently never lands is
 *  the failure worth seeing, so each row shows the state it reached and how
 *  long it has been in it: "Queued · 40s" (nothing has taken it) reads nothing
 *  like "Injected · round 7". Rows come from /api/agents/overview, which reads
 *  them back off the activity feed, so they survive the turn that made them.
 */
function steerLogHtml(r) {
  const messages = (r.steer && r.steer.messages) || [];
  if (!messages.length) return '';
  return `<div class="ag-section ag-steer-log">
    <div class="wb-group-h"><span class="wb-group-title">Steering messages</span><span class="wb-count">${messages.length}</span></div>
    ${messages.map(steerRowHtml).join('')}
  </div>`;
}
function steerRowHtml(m) {
  const state = STATUS[m.state] && STEER_STATE_NOTE[m.state] ? m.state : 'queued';
  // `live: false` on a queued row means the feed remembers it but the in-memory
  // queue does not — it was queued before a restart, so nothing is waiting to
  // read it. Saying so is the difference between "still pending" and "lost".
  const waiting = state === 'queued' && m.live !== false;
  const stamps = m.timestamps || {};
  // The clock stops at the state the message reached; an age that kept running
  // after injection would read as "still waiting".
  const finished = waiting ? '' : (stamps[state] || m.updated_at || '');
  const meta = [
    m.kind === 'peer' ? `from ${m.from_session_name || m.from_session || 'a peer agent'}` : '',
    state === 'injected' && m.round ? `round ${Number(m.round)}` : '',
    state === 'queued' && !waiting ? 'not in the live queue — lost on restart' : '',
    m.reason || '',
  ].filter(Boolean);
  const age = m.queued_at ? fmtDur(m.queued_at, finished || undefined) : '';
  return `<div class="ag-child${waiting ? '' : ' done'}" title="${esc(m.id || '')} — ${esc(STEER_STATE_NOTE[state])}">
    ${pill(state)}<span class="ag-child-title" title="${esc(m.text || '')}">${esc(m.text || '(no text)')}</span>
    ${meta.map((item) => `<span class="wb-meta-item">${esc(item)}</span>`).join('')}
    ${age ? `<span class="ag-row-dur" data-started="${esc(m.queued_at)}" data-finished="${esc(finished)}">${esc(age)}</span>` : ''}</div>`;
}
// An exact approval is decided on its card in the chat, which shows the sealed
// action in full; the overview only points there.
function approvalHtml(a) {
  return `<div class="ag-approval"><div class="ag-approval-head"><span class="approval-badge">Approval needed</span><code class="approval-tool">${esc(a.tool)}</code><span class="wb-meta-item">${esc(a.reason)}</span></div>
    <pre class="approval-command">${esc(a.command)}</pre>
    <div class="approval-actions"><button type="button" class="approval-btn approval-approve" data-ag="open-chat" data-sid="${esc(a.session_id)}">Open chat to decide</button></div></div>`;
}
function childHtml(c) {
  const live = c.status === 'running';
  const s = c.summary || {};
  const title = String(c.title || '').replace(/^(Sub-agent|Claude Code|Background job|Worker)\s*[·:]\s*/, '');
  return `<div class="ag-child${live ? '' : ' done'}">${robotHtml(c, 'mini')}${pill(c.status === 'completed' ? 'finished' : c.status)}${chip(c.source)}<span class="ag-child-title" title="${esc(c.title)}">${esc(title)}</span><span class="ag-row-dur" data-started="${c.started_at || ''}" data-finished="${c.finished_at || ''}">${esc(fmtDur(c.started_at, c.finished_at))}</span>
    ${s.target_session ? `<button type="button" class="wb-btn wb-btn-sm wb-btn-ghost" data-ag="open-chat" data-sid="${esc(s.target_session)}">Open</button>` : ''}
    <button type="button" class="wb-btn wb-btn-sm wb-btn-ghost" data-ag="inspect-run" data-run="${esc(c.run_id)}" data-sid="${esc(state.selected || '')}">Inspect</button>
    ${live ? `<button type="button" class="wb-btn wb-btn-sm" data-ag="stop-run" data-run="${esc(c.run_id)}">Stop</button>` : `<button type="button" class="wb-btn wb-btn-sm wb-btn-ghost" data-ag="hide-run" data-run="${esc(c.run_id)}" title="Hide this completed card; activity history remains">Hide</button>`}</div>`;
}
function eventHtml(ev) {
  const lvl = ev.level === 'error' || ev.kind === 'error' ? ' err' : ev.level === 'warning' ? ' warn' : '';
  return `<div class="wb-ev no-src${lvl}"><span class="wb-ev-time">${fmtTime(ev.ts)}</span><span class="wb-ev-kind">${KIND_ICON[ev.kind] || '·'}</span><div class="wb-ev-body"><span class="wb-ev-title">${chip(ev.source)} ${esc(ev.title || '')}</span>${ev.detail ? `<details class="wb-disclosure"><summary>Detail</summary><pre>${esc(ev.detail)}</pre></details>` : ''}</div></div>`;
}
function launchHtml() {
  const opts = state.profiles.map((p) => `<option value="${esc(p.name)}">${esc(p.name)}${p.description ? ` — ${esc(p.description)}` : ''}</option>`).join('');
  const chats = state.chats.map((c) => `<option value="${esc(c.id)}"${c.id === state.selected ? ' selected' : ''}>${esc(c.name)}</option>`).join('');
  return `<div class="ag-launch-head"><span class="wb-group-title">Launch a worker</span><button type="button" class="wb-icon-btn" data-ag="launch-close" aria-label="Close">✕</button></div>
    <label class="ag-field"><span>Profile</span><select id="ag-profile" class="wb-select"><option value="">No profile (parent chat's model, all tools)</option>${opts}</select></label>
    <label class="ag-field"><span>Task</span><textarea id="ag-task" class="wb-input ag-textarea" rows="5" placeholder="The whole task — the worker starts with no other context."></textarea></label>
    <label class="ag-field"><span>Report to chat</span><select id="ag-parent" class="wb-select"><option value="">None (standalone)</option>${chats}</select></label>
    <label class="ag-field"><span>Model override</span><input id="ag-model" class="wb-input" placeholder="optional, e.g. qwen3 or model@endpoint"></label>
    <label class="ag-field"><span>Workspace</span><input id="ag-workspace" class="wb-input" placeholder="optional checkout path; default: the parent chat's, or the one the task names"></label>
    <div class="ag-launch-actions"><button type="button" class="wb-btn wb-btn-primary" data-ag="launch-go">Launch</button><span class="ag-launch-msg" id="ag-launch-msg"></span></div>
    ${state.profiles.length ? '' : '<p class="wb-hint">No profiles yet — define workers under Settings › Workbench › Agent profiles.</p>'}`;
}

function syncConfigVisibility(editor, draft) {
  editor?.querySelectorAll('[data-show-when]').forEach((node) => {
    const [key, expected] = String(node.dataset.showWhen || '').split(':');
    node.hidden = String(draft[key] || '') !== expected;
  });
}
function onConfigChange(e) {
  if (e.target.matches('[data-config-agent]')) {
    state.selected = e.target.value;
    state.configTab = 'general';
    render();
    return;
  }
  const editor = e.target.closest('.ag-loadout-editor');
  const row = state.rows.find((item) => item.session_id === state.selected);
  if (!editor || !row) return;
  const field = e.target.dataset.config;
  const profile = field === 'agent_profile' && state.profiles.find((item) => item.name === e.target.value);
  if (profile) {
    // Choosing a preset loads its whole loadout. Recording only the name left
    // every field as it was, and saving then stored the old settings under
    // the new preset's label.
    state.configDrafts.set(row.session_id, profileConfig(profile));
    render();
    const msg = $('ag-config-msg');
    if (msg) msg.textContent = `Loaded ${profile.name} — unsaved`;
    return;
  }
  const draft = configFor(row);
  if (field) {
    draft[field] = e.target.type === 'checkbox' ? !!e.target.checked
      : e.target.type === 'number' ? Number(e.target.value || 0) : e.target.value;
    if (field === 'mcp_access') {
      if (e.target.value === 'all') draft.allowed_mcp_servers = ['*'];
      else if (e.target.value === 'none') draft.allowed_mcp_servers = [];
      else if (draft.allowed_mcp_servers?.includes('*')) draft.allowed_mcp_servers = (state.catalog?.mcp_servers || []).map((item) => item.id);
    }
  }
  const list = e.target.dataset.configList;
  if (list) {
    const values = new Set(draft[list] || []);
    e.target.checked ? values.add(e.target.value) : values.delete(e.target.value);
    draft[list] = [...values];
    if (list === 'enabled_tools' && e.target.checked) {
      // A direct human checkbox action is the explicit authorization needed
      // to remove this one tool from the denylist. Passive profile/plugin
      // merges never do this, so their additions still respect manual denies.
      draft.disabled_tools = (draft.disabled_tools || []).filter((name) => name !== e.target.value);
    }
  }
  syncConfigVisibility(editor, draft);
  const msg = $('ag-config-msg');
  if (msg) msg.textContent = 'Unsaved changes';
}
async function saveAgentConfig(row) {
  const draft = configFor(row);
  const allTools = (state.catalog?.tools || []).map((tool) => tool.name);
  // Explicit denials always win, including over plugin/profile additions.
  // Selecting tools narrows from that baseline; it never resurrects a tool
  // the user deliberately denied.
  const explicitDisabled = new Set(draft.disabled_tools || []);
  let disabledTools = [...explicitDisabled];
  if (draft.tool_access === 'none') disabledTools = [...new Set([...disabledTools, ...allTools])];
  else if (draft.tool_access === 'selected') {
    const enabled = new Set(draft.enabled_tools || []);
    disabledTools = [...new Set([...disabledTools, ...allTools.filter((name) => !enabled.has(name))])];
  }
  const enabledTools = (draft.enabled_tools || []).filter((name) => !explicitDisabled.has(name));
  let allowedMcp = ['*'];
  if (draft.mcp_access === 'none') allowedMcp = [];
  else if (draft.mcp_access === 'selected') allowedMcp = [...(draft.allowed_mcp_servers || [])];
  const payload = {
    agent_profile: draft.agent_profile || null,
    agent_instructions: draft.agent_instructions || null,
    approval_mode: draft.approval_mode || null,
    disabled_tools: disabledTools,
    tool_access: draft.tool_access,
    enabled_tools: draft.tool_access === 'selected' ? enabledTools : [],
    memory_access: draft.memory_access,
    skill_access: draft.skill_access,
    skill_names: draft.skill_access === 'selected' ? (draft.skill_names || []) : [],
    model_access: draft.model_access,
    allowed_models: draft.model_access === 'selected' ? (draft.allowed_models || []) : [],
    delegation_policy: draft.delegation_policy,
    max_parallel_workers: draft.max_parallel_workers,
    allowed_mcp_servers: allowedMcp,
    private_vault_access: !!draft.private_vault_access,
  };
  const result = await api(`/api/session/${encodeURIComponent(row.session_id)}/settings`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  });
  row.config = Object.assign({}, result.settings || payload);
  row.approval_mode = result.approval_mode;
  state.configDrafts.set(row.session_id, Object.assign({}, draft, row.config, {
    tool_access: draft.tool_access, enabled_tools: draft.enabled_tools || [], mcp_access: draft.mcp_access,
  }));
  return result;
}

// ── actions ───────────────────────────────────────────────────────────────
async function onClick(e) {
  const row = e.target.closest('.ag-row[data-sid]');
  const b = e.target.closest('[data-ag]');
  if (!b && row) {
    state.selected = row.dataset.sid;
    renderFleetOnly();
    renderDetail();
    return;
  }
  if (!b) return;
  const act = b.dataset.ag;
  try {
    if (act === 'select-agent') {
      state.selected = b.dataset.sid;
      state.detailTab = 'overview';
      renderFleetOnly(); renderDetail();
      $('agents-dashboard')?.querySelector(`.ag-card-select[data-sid="${CSS.escape(state.selected)}"]`)?.focus({ preventScroll: true });
    }
    else if (act === 'close') close();
    else if (act === 'workbench') {
      if (!window.workbenchModule?.open) throw new Error('Workbench is unavailable');
      window.workbenchModule.open();
    }
    else if (act === 'refresh') { b.disabled = true; await refresh(); if (b.isConnected) b.disabled = false; }
    else if (act === 'archive-view') {
      state.archiveView = !state.archiveView;
      state.selected = null; state.filter = ''; state.bucket = 'all'; state.fleetPage = 0;
      await refresh();
    }
    else if (act === 'launch') { state.launchOpen = true; render(); $('ag-task')?.focus(); }
    else if (act === 'launch-close') { state.launchOpen = false; render(); }
    else if (act === 'bucket') {
      // Toggle: clicking the active bucket clears the filter, so the control
      // can always get you back to everything without a separate "All" chip.
      const next = b.dataset.bucket;
      state.bucket = (next === 'all' || state.bucket === next) ? 'all' : next;
      state.filter = '';
      state.fleetPage = 0;
      const input = $('ag-filter'); if (input) input.value = '';
      // Keep a selection that is still visible in the new bucket.
      const match = (BUCKETS.find(([k]) => k === state.bucket) || [])[2];
      if (match && !state.rows.some((r) => r.session_id === state.selected && match(r))) {
        state.selected = (state.rows.find(match) || {}).session_id || state.selected;
      }
      updateStats(); renderFleetOnly(); renderDetail();
    }
    else if (act === 'toggle-workers') {
      const sid = b.dataset.sid;
      state.expandedParents.has(sid) ? state.expandedParents.delete(sid) : state.expandedParents.add(sid);
      renderFleetOnly();
    }
    else if (act === 'fleet-page') {
      state.fleetPage = Math.max(0, Number(b.dataset.page || 0));
      renderFleetOnly();
    }
    else if (act === 'fleet-density') {
      state.compactFleet = !state.compactFleet;
      localStorage.setItem('odysseus-agents-fleet-density', state.compactFleet ? 'compact' : 'expanded');
      render();
    }
    else if (act === 'detail-tab') {
      state.detailTab = b.dataset.tab || 'overview';
      renderDetail();
    }
    else if (act === 'open-chat') { await openChat(b.dataset.sid); }
    else if (act === 'config-toggle') {
      state.configOpen = true;
      state.configTab = 'general';
      render();
      try { await loadCatalog(); } catch (err) { uiModule.showToast(err.message || 'Capabilities unavailable', 'error'); }
      render();
      syncConfigVisibility(document.querySelector('.ag-loadout-editor'), configFor(state.rows.find((item) => item.session_id === state.selected)));
    }
    else if (act === 'config-back') {
      state.configOpen = false;
      render();
    }
    else if (act === 'config-tab') {
      state.configTab = b.dataset.tab || 'general';
      render();
      syncConfigVisibility(document.querySelector('.ag-loadout-editor'), configFor(state.rows.find((item) => item.session_id === state.selected)));
    }
    else if (act === 'save-config') {
      const row = state.rows.find((item) => item.session_id === state.selected);
      if (!row) return;
      b.disabled = true;
      const msg = $('ag-config-msg'); if (msg) msg.textContent = 'Saving…';
      await saveAgentConfig(row);
      uiModule.showToast(`Loadout saved for ${row.name}`, 'success');
      state.configOpen ? render() : renderDetail();
    }
    else if (act === 'inspect-run') {
      if (!window.workbenchModule?.openRun) throw new Error('Workbench inspection is unavailable');
      await selectChat(b.dataset.sid);
      await window.workbenchModule.openRun(b.dataset.run, b.dataset.sid);
    }
    else if (act === 'stop-chat') {
      b.disabled = true;
      const r = await post(`/api/chat/stop/${encodeURIComponent(b.dataset.sid)}`);
      uiModule.showToast(r.stopped ? 'Stopping' : 'Nothing to stop'); scheduleRefresh();
    } else if (act === 'archive-agent') {
      const selected = state.rows.find((item) => item.session_id === b.dataset.sid);
      if (!selected || !window.confirm(`Archive “${selected.name}”? Its chat and run history stay preserved. Active work cannot be archived.`)) return;
      b.disabled = true;
      await post(`/api/agents/sessions/${encodeURIComponent(b.dataset.sid)}/archive`);
      state.events.delete(b.dataset.sid);
      state.selected = null;
      uiModule.showToast('Archived — chat and run history were preserved', 'success');
      await refresh();
    } else if (act === 'restore-agent') {
      b.disabled = true;
      await post(`/api/agents/sessions/${encodeURIComponent(b.dataset.sid)}/unarchive`);
      state.events.delete(b.dataset.sid);
      state.selected = null;
      uiModule.showToast('Restored — available in chat history', 'success');
      await refresh();
    } else if (act === 'hide-run') {
      b.disabled = true;
      await post(`/api/agents/sessions/${encodeURIComponent(state.selected || '')}/cleanup-runs`, { run_ids: [b.dataset.run] });
      uiModule.showToast('Hidden from this overview — activity history remains', 'success');
      await refresh();
    } else if (act === 'restore-runs') {
      b.disabled = true;
      const row = state.rows.find((item) => item.session_id === b.dataset.sid);
      await post(`/api/agents/sessions/${encodeURIComponent(b.dataset.sid)}/restore-runs`, { run_ids: row?.hidden_run_ids || [] });
      uiModule.showToast('Completed run cards restored', 'success');
      await refresh();
    } else if (act === 'stop-run') {
      b.disabled = true; b.textContent = 'Stopping…';
      const r = await post(`/api/agents/runs/${encodeURIComponent(b.dataset.run)}/stop`);
      uiModule.showToast(r.stopped ? 'Stopping — its partial result goes back to the chat' : `Not stopped: ${r.reason || ''}`, r.stopped ? 'success' : 'warning'); scheduleRefresh();
    } else if (act === 'steer') {
      const ta = $('ag-steer'); const text = (ta?.value || '').trim(); if (!text) return;
      b.disabled = true;
      await post(`/api/agents/sessions/${encodeURIComponent(b.dataset.sid)}/steer`, { text });
      // "Queued" is a promise the toast used to make and never keep. It now
      // points at the row that will keep it: the message is listed below with
      // its state, so a steer that is never picked up stays visible instead of
      // being assumed delivered.
      ta.value = ''; b.disabled = false;
      uiModule.showToast('Steer queued — track it under Steering messages'); scheduleRefresh();
    } else if (act === 'reply') {
      const ta = $('ag-reply'); const text = (ta?.value || '').trim(); if (!text) return;
      await sendToChat(b.dataset.sid, text, { open: true });
    } else if (act === 'launch-go') {
      const task = ($('ag-task')?.value || '').trim();
      const msg = $('ag-launch-msg');
      if (!task) { if (msg) msg.textContent = 'Describe the task first.'; return; }
      b.disabled = true; if (msg) msg.textContent = 'Launching…';
      try {
        const r = await post('/api/agents/launch', { task, profile: $('ag-profile')?.value || '', parent_session: $('ag-parent')?.value || '', model: $('ag-model')?.value || '', workspace: $('ag-workspace')?.value || '' });
        state.launchOpen = false; state.selected = r.session_id; state.events.delete(r.session_id);
        uiModule.showToast(`Worker started: ${r.session_name}`, 'success');
        await refresh();
      } catch (err) { if (msg) msg.textContent = err.message; b.disabled = false; }
    }
  } catch (err) {
    uiModule.showToast(err.message || String(err), 'error');
    scheduleRefresh();
  }
}
/** Deliver a message to a chat: open it and send through the composer, so it
 *  becomes a normal turn with the chat's own settings. */
async function sendToChat(sid, text, { open = true } = {}) {
  if (open) await selectChat(sid);
  const ta = $('message');
  if (!ta) throw new Error('Chat composer is unavailable');
  ta.value = text;
  try { ta.dispatchEvent(new Event('input', { bubbles: true })); } catch (_) {}
  const send = document.querySelector('.send-btn');
  if (!send) throw new Error('Chat send control is unavailable');
  send.click();
}
async function selectChat(sid) {
  if (!window.sessionModule?.selectSession) throw new Error('Chat navigation is unavailable');
  await window.sessionModule.selectSession(sid);
  const current = window.sessionModule.getCurrentSessionId?.();
  if (current && current !== sid) throw new Error('Could not open the selected chat');
}
async function openChat(sid) {
  await selectChat(sid);
}

// ── open / close ──────────────────────────────────────────────────────────
function registerWithManager() {
  if (Modals.isRegistered(MODAL_ID)) return;
  Modals.register(MODAL_ID, {
    label: 'Agent Control Room',
    railBtnId: 'rail-agents',
    sidebarBtnId: 'tool-agents-btn',
    restoreFn: () => open(),
    closeFn: () => hideWindow(),
  });
}
function hideWindow() {
  const root = $(MODAL_ID); if (!root) return;
  const restoreFocus = root.contains(document.activeElement);
  state.open = false;
  root.hidden = true;
  root.classList.add('hidden');
  if (state.tick) { clearInterval(state.tick); state.tick = null; }
  if (restoreFocus && returnFocus?.isConnected) returnFocus.focus({ preventScroll: true });
}
function workspaceRect() {
  const css = getComputedStyle(document.documentElement);
  const nav = (parseFloat(css.getPropertyValue('--icon-rail-w')) || 0)
    + (parseFloat(css.getPropertyValue('--sidebar-w')) || 0);
  return { left: nav + 4, top: 4, width: Math.max(320, window.innerWidth - nav - 8), height: Math.max(320, window.innerHeight - 8) };
}
function bringToFront() {
  const root = $(MODAL_ID); if (!root) return;
  root.style.zIndex = String(nextToolWindowZ({ exclude: root, current: root.style.zIndex }));
}
export function open({ focus = true } = {}) {
  const root = $('agents-dashboard'); if (!root) return;
  registerWithManager();
  if (Modals.isMinimized(MODAL_ID)) { Modals.restore(MODAL_ID); return; }
  if (!state.open) returnFocus = document.activeElement;
  state.open = true;
  root.hidden = false;
  root.classList.remove('hidden');
  bringToFront();
  if ('Notification' in window && Notification.permission === 'default') { try { Notification.requestPermission(); } catch (_) {} }
  const cur = window.sessionModule?.getCurrentSessionId?.();
  if (cur && state.rows.some((r) => r.session_id === cur)) state.selected = cur;
  render(); refresh(); connect();
  if (focus) root.focus({ preventScroll: true });
  if (!state.tick) state.tick = setInterval(() => {
    if (!state.open) return;
    root.querySelectorAll('.ag-row-dur[data-started]').forEach((el) => {
      const started = Number(el.dataset.started);
      const finished = Number(el.dataset.finished) || undefined;
      if (started) el.textContent = fmtDur(started, finished);
    });
  }, 1000);
}
/** Show the panel for work the current chat just started. Leaves it alone
 * when it is already open or the user minimized it, and keeps focus in the
 * composer so the user can keep typing. */
export function openForRun() {
  if (state.open || Modals.isMinimized(MODAL_ID)) return;
  open({ focus: false });
}
export function close() {
  if (Modals.isRegistered(MODAL_ID)) Modals.close(MODAL_ID);
  else hideWindow();
}
export function toggle() {
  if (Modals.toggle(MODAL_ID)) return;
  state.open ? close() : open();
}

function setFleetWidth(body, width) {
  if (!body) return;
  const max = Math.max(250, body.getBoundingClientRect().width - 390);
  state.fleetWidth = Math.round(Math.max(250, Math.min(width, max)));
  body.style.setProperty('--ag-fleet-width', `${state.fleetWidth}px`);
  try { localStorage.setItem('odysseus-agents-fleet-width', String(state.fleetWidth)); } catch (_) {}
}

function beginFleetResize(e) {
  const splitter = e.target.closest('[data-ag-splitter]');
  if (!splitter || e.button !== 0) return;
  const body = splitter.closest('.ag-body');
  if (!body || getComputedStyle(splitter).display === 'none') return;
  e.preventDefault();
  const rect = body.getBoundingClientRect();
  splitter.classList.add('dragging');
  document.body.classList.add('ag-split-resizing');
  const move = (ev) => setFleetWidth(body, ev.clientX - rect.left);
  const end = () => {
    splitter.classList.remove('dragging');
    document.body.classList.remove('ag-split-resizing');
    window.removeEventListener('pointermove', move);
    window.removeEventListener('pointerup', end);
    window.removeEventListener('pointercancel', end);
  };
  window.addEventListener('pointermove', move);
  window.addEventListener('pointerup', end);
  window.addEventListener('pointercancel', end);
}

function init() {
  const root = $('agents-dashboard'); if (!root) return;
  const content = root.querySelector('.agents-modal-content');
  const header = root.querySelector('.agents-window-header');
  makeWindowDraggable(root, {
    content,
    header,
    skipSelector: 'button, input, select, label, textarea',
    enableDock: true,
    enableLeftDock: true,
    minWidth: 520,
    minHeight: 440,
    resizeStorageKey: 'odysseus-agents-window-size',
  });
  registerWithManager();
  Modals.injectMinimizeButton(root, MODAL_ID);
  $('close-agents-dashboard')?.addEventListener('click', close);
  $('ag-dock-left')?.addEventListener('click', () => applyEdgeDock(root, 'left'));
  $('ag-dock-right')?.addEventListener('click', () => applyEdgeDock(root, 'right'));
  $('ag-maximize')?.addEventListener('click', () => snapModalToZone(root, { name: 'maximize', rect: workspaceRect() }));
  root.addEventListener('pointerdown', bringToFront, true);
  root.addEventListener('pointerdown', beginFleetResize);
  root.addEventListener('click', onClick);
  root.addEventListener('keydown', (e) => {
    const tab = e.target?.closest?.('[data-ag="detail-tab"]');
    if (!tab || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) return;
    const tabs = [...root.querySelectorAll('[data-ag="detail-tab"]')];
    const current = tabs.indexOf(tab);
    const next = e.key === 'Home' ? 0 : e.key === 'End' ? tabs.length - 1
      : (current + (e.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
    e.preventDefault(); state.detailTab = tabs[next].dataset.tab || 'overview'; renderDetail();
    root.querySelector(`[data-ag="detail-tab"][data-tab="${CSS.escape(state.detailTab)}"]`)?.focus({ preventScroll: true });
  });
  root.addEventListener('change', onConfigChange);
  document.addEventListener('keydown', (e) => {
    if (state.open && e.target?.matches?.('[data-ag-splitter]') && (e.key === 'ArrowLeft' || e.key === 'ArrowRight')) {
      e.preventDefault();
      setFleetWidth(e.target.closest('.ag-body'), state.fleetWidth + (e.key === 'ArrowLeft' ? -24 : 24));
      return;
    }
    if (e.key === 'Escape' && state.open) { close(); return; }
    if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === 'a') { e.preventDefault(); toggle(); }
  });
  // Badges stay current while the page is closed: a light poll plus the feed.
  // The feed is held only by a visible tab. Every open tab used to keep this
  // stream (and the Workbench's) for its whole life, and six such connections
  // is all a browser allows per host over HTTP/1.1 -- after that every fetch
  // from any Odysseus tab queued indefinitely, which is how the agent strip
  // in the chat that launched a worker never got to ask about it (2026-09-17).
  if (!document.hidden) connect();
  refresh();
  state.pollTimer = setInterval(() => { if (document.visibilityState === 'visible') refresh(); }, state.open ? 5000 : 20000);
  let hiddenTimer = null;
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      if (!hiddenTimer) hiddenTimer = setTimeout(() => { hiddenTimer = null; if (document.hidden) disconnect(); }, 30000);
      return;
    }
    if (hiddenTimer) { clearTimeout(hiddenTimer); hiddenTimer = null; }
    connect();
    refresh();
  });
}
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();

const agentsDashboard = { open, openForRun, close, toggle, refresh };
window.agentsDashboard = agentsDashboard;
export default agentsDashboard;
