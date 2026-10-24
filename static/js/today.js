/* today.js — the Today view: a short, explained list of what needs you now.
 *
 * Opens from the rail, the sidebar and /today. It shows at most five items for
 * today and three for next, from GET /api/today, each with the one-line reason
 * the server gives it. The buttons on an item are exactly the item's `actions`.
 * Above the lists, one line captures a task, appointment or follow-up through
 * POST /api/capture. Clicking an outcome opens its detail from
 * GET /api/outcomes/{id}, where "Mark done" records the person's confirmation.
 *
 * The server owns ranking, ownership and every rule about what an action may
 * change; this file only draws what it is given and sends what the person chose.
 */

import Modals from './modalManager.js';

const MODAL_ID = 'today-modal';
const REFRESH_MS = 60000;
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

const ACTION_LABEL = { start: 'Start', resume: 'Resume', defer: 'Snooze', wait: 'Wait', done: 'Done', open: 'Open' };
const ACTION_HINT = {
  start: 'Open a chat for this and mark it in progress',
  resume: 'Send the worker back to work',
  defer: 'Hide until tomorrow morning. Nothing else changes.',
  wait: 'Park it until you hear back. Due dates stay as they are.',
  done: 'Mark it done',
  open: 'Open where it lives',
};
const SOURCE_LABEL = { todoist: 'Todoist', calendar: 'Calendar', note: 'Note', outcome: 'Outcome', agent_run: 'Agent run' };
const DESTINATION_LABEL = { todoist: 'Todoist', calendar: 'Calendar', note: 'Notes', outcome: 'Outcomes' };
const STATE_LABEL = { open: 'Not started', active: 'In progress', waiting: 'Waiting', blocked: 'Blocked', done: 'Done', dropped: 'Dropped' };
const LINK_LABEL = {
  todoist_task: 'Todoist task', calendar_event: 'Calendar event', note: 'Note', chat: 'Chat', agent_run: 'Agent run',
  scheduled_task: 'Scheduled task', worktree: 'Worktree', branch: 'Branch', pull_request: 'Pull request', url: 'Link',
};

const state = {
  open: false,
  data: null,        // last /api/today payload
  error: '',         // the list could not be loaded
  detail: null,      // { loading, outcome, error } while an outcome is open
  status: null,      // { text, tone, link } one line after an action
  waitingFor: null,  // key of the item whose "wait" form is open
  capture: null,     // { key, text } the submission in flight or retryable
  draft: '',         // what is typed in the capture line
  clarify: null,     // { question, options } waiting for the person's pick
  captureResult: null, // { destination, open_url } after a successful capture
  captureError: '',
  busy: false,
};
let timer = null;
let returnFocus = null;
let loadSeq = 0;

// ── network ──────────────────────────────────────────────────────────────────
function tzHeaders() {
  let name = '';
  try { name = Intl.DateTimeFormat().resolvedOptions().timeZone || ''; } catch (_) {}
  return { 'X-Tz-Offset': String(-new Date().getTimezoneOffset()), ...(name ? { 'X-Tz-Name': name } : {}) };
}
async function api(path, opts = {}) {
  const headers = { ...tzHeaders(), ...(opts.headers || {}) };
  const r = await fetch(path, { credentials: 'same-origin', ...opts, headers });
  let body = null;
  try { body = await r.json(); } catch (_) {}
  if (!r.ok) {
    const detail = body && (body.detail || body.error);
    const e = new Error(typeof detail === 'string' ? detail : `The server answered ${r.status}`);
    e.status = r.status; e.body = body;
    throw e;
  }
  return body;
}
const post = (path, data) => api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data || {}) });

// ── formatting ───────────────────────────────────────────────────────────────
function parseDue(value) {
  if (!value) return null;
  const text = String(value);
  if (/^\d{4}-\d{2}-\d{2}$/.test(text)) {
    const [y, m, d] = text.split('-').map(Number);
    return { date: new Date(y, m - 1, d), allDay: true };
  }
  const date = new Date(text);
  return Number.isNaN(date.getTime()) ? null : { date, allDay: false };
}
function startOfToday() { const d = new Date(); d.setHours(0, 0, 0, 0); return d; }
function fmtDay(date) {
  const diff = Math.round((date - startOfToday()) / 86400000);
  if (diff === 0) return 'today';
  if (diff === 1) return 'tomorrow';
  return date.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' });
}
function fmtTime(date) { return date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }); }
function fmtWhen(due) {
  const parsed = parseDue(due);
  if (!parsed) return '';
  return parsed.allDay ? fmtDay(parsed.date) : `${fmtDay(parsed.date)} ${fmtTime(parsed.date)}`;
}
function isOverdue(item) {
  const parsed = parseDue(item.due);
  return !!parsed && item.state !== 'waiting' && item.source !== 'calendar' && parsed.date < startOfToday();
}
function needsPerson(item) {
  return !!item.blocker && ['user', 'approval'].includes(item.blocker.recovery);
}
function dueHint(item) {
  if (!item.due) return '';
  const when = fmtWhen(item.due);
  if (item.source === 'calendar') return `At ${when}`;
  return item.state === 'waiting' ? `Follow up ${when}` : `Due ${when}`;
}
function newKey() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  return `cap-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

// ── rendering ────────────────────────────────────────────────────────────────
/** Only same-site paths and web links become hrefs; anything else is dropped. */
function safeHref(url) {
  const text = String(url || '');
  return (text.startsWith('/') && !text.startsWith('//')) || /^https?:\/\//i.test(text) ? text : '';
}
const linkAttr = (url) => (safeHref(url) ? ` href="${esc(safeHref(url))}" data-today-link="${esc(safeHref(url))}"` : '');
function blockerHtml(b, title = '') {
  if (!b) return '';
  const action = b.user_action ? `<span class="today-blocker-action"><strong>You need to:</strong> ${esc(b.user_action)}</span>` : '';
  // Recovery actions the server attached (what the person can do about it).
  const buttons = (b.actions || []).filter((a) => a && a.id && a.label).map((a) => (
    `<button type="button" class="today-btn" data-today-blocker-action="${esc(JSON.stringify(a))}" data-task="${esc(title)}">${esc(a.label)}</button>`)).join('');
  return `<p class="today-blocker"><span class="today-blocker-summary">${esc(b.summary || 'Work is blocked.')}</span> ${action}</p>${buttons ? `<div class="today-blocker-actions">${buttons}</div>` : ''}`;
}
function actionButtons(item) {
  const doneLabel = item.source === 'todoist' ? 'Complete in Todoist' : item.source === 'outcome' ? 'Close outcome' : 'Archive note';
  return (item.actions || []).filter((a) => ACTION_LABEL[a]).map((a) => (
    `<button type="button" class="today-btn" data-today-action="${esc(a)}" title="${esc(a === 'done' ? doneLabel : ACTION_HINT[a])}">${esc(a === 'done' ? doneLabel : ACTION_LABEL[a])}</button>`
  )).join('');
}
function waitForm(item) {
  return `<form class="today-wait" data-today-wait="${esc(item.key)}">
    <label>Waiting on <input name="waiting_on" type="text" required maxlength="120" placeholder="Who or what" autocomplete="off"></label>
    <label>Follow up <input name="until" type="date"></label>
    <button type="submit" class="today-btn">Park it</button>
    <button type="button" class="today-btn" data-today-wait-cancel>Cancel</button>
  </form>`;
}
function itemHtml(item) {
  const attention = needsPerson(item) ? 'blocked' : (isOverdue(item) ? 'overdue' : '');
  const isOutcome = item.source === 'outcome';
  const canOpen = isOutcome || !!item.open_url;
  const title = canOpen
    ? `<button type="button" class="today-title" data-today-open>${esc(item.title)}</button>`
    : `<span class="today-title">${esc(item.title)}</span>`;
  const hint = dueHint(item);
  return `<li class="today-item" data-key="${esc(item.key)}" data-source="${esc(item.source)}"${attention ? ` data-attention="${attention}"` : ''}>
    <div class="today-item-main">
      ${title}
      <p class="today-why">${esc(item.why)}</p>
      ${blockerHtml(item.blocker, item.title)}
      <p class="today-meta"><span class="today-source">${esc(SOURCE_LABEL[item.source] || item.source)}</span>${hint ? `<span class="today-due">${esc(hint)}</span>` : ''}</p>
      ${isOutcome ? '<p class="today-muted">Closing this outcome does not complete linked tasks. An open source task may appear again.</p>' : ''}
    </div>
    <div class="today-actions">${actionButtons(item)}</div>
    ${state.waitingFor === item.key ? waitForm(item) : ''}
  </li>`;
}
function sectionHtml(id, title, items) {
  if (!items.length) return '';
  return `<section class="today-section" aria-labelledby="today-h-${id}">
    <h4 id="today-h-${id}" class="today-section-title">${esc(title)}</h4>
    <ul class="today-list">${items.map(itemHtml).join('')}</ul>
  </section>`;
}
function captureHtml() {
  const text = state.draft;
  const clarify = state.clarify
    ? `<div class="today-clarify" role="group" aria-label="${esc(state.clarify.question)}">
        <p>${esc(state.clarify.question)}</p>
        <div class="today-clarify-options">${state.clarify.options.map((o, i) => (
          `<button type="button" class="today-btn" data-today-pick="${i}">${esc(o.label)}</button>`)).join('')}</div>
      </div>` : '';
  const done = state.captureResult
    ? `<p class="today-capture-note" role="status">Added to ${esc(DESTINATION_LABEL[state.captureResult.destination] || state.captureResult.destination)}${
        safeHref(state.captureResult.open_url) ? ` <a${linkAttr(state.captureResult.open_url)}>Open</a>` : ''}</p>` : '';
  const error = state.captureError ? `<p class="today-capture-error" role="alert">${esc(state.captureError)}</p>` : '';
  return `<form class="today-capture" id="today-capture" autocomplete="off">
    <label class="today-capture-label" for="today-capture-input">Capture</label>
    <div class="today-capture-row">
      <input id="today-capture-input" name="text" type="text" maxlength="500" value="${esc(text)}" placeholder="Capture a task, appointment or follow-up"${state.busy ? ' disabled' : ''}>
      <button type="submit" class="today-btn today-btn-primary"${state.busy ? ' disabled' : ''}>Add</button>
    </div>
    ${clarify}${done}${error}
  </form>`;
}
function statusHtml() {
  if (!state.status) return '';
  const link = safeHref(state.status.link) ? ` <a${linkAttr(state.status.link)}>Open</a>` : '';
  const blocker = state.status.blocker ? blockerHtml(state.status.blocker) : '';
  return `<div class="today-status" data-tone="${esc(state.status.tone || 'ok')}" role="status">${esc(state.status.text)}${link}${blocker}</div>`;
}
function emptyHtml() {
  return `<div class="today-empty">
    <p class="today-empty-title">Nothing urgent right now.</p>
    <p class="today-empty-body">Capture something above and it will show up here when it needs you.</p>
  </div>`;
}
function degradedHtml(degraded) {
  if (!degraded?.length) return '';
  const names = degraded.map((d) => (SOURCE_LABEL[d] || d.replace(/_/g, ' ')));
  return `<p class="today-degraded">Could not read ${esc(names.join(', '))} just now. The rest is up to date.</p>`;
}
function listHtml() {
  if (state.error && !state.data) {
    return `<div class="today-empty"><p class="today-empty-title">Today could not load.</p><p class="today-empty-body">${esc(state.error)}</p>
      <button type="button" class="today-btn" data-today-reload>Try again</button></div>`;
  }
  if (!state.data) return '<p class="today-loading">Loading</p>';
  const { today = [], next = [], degraded = [] } = state.data;
  const body = (today.length || next.length)
    ? sectionHtml('today', 'Today', today) + sectionHtml('next', 'Next', next)
    : emptyHtml();
  const source = state.data.sources?.todoist;
  const sourceText = {
    ready: 'Todoist: live tasks due today or overdue (not your full backlog).',
    unavailable: 'Todoist is unavailable. Try Refresh; no tasks were changed.',
    not_configured: 'Todoist is not connected or signed in on this server.',
    restricted: 'Todoist is not available to this account; this integration is admin / single-user only.',
  }[source] || 'Todoist shows tasks due today or overdue when connected and permitted.';
  const remaining = Number(state.data.scope?.remaining) || 0;
  return `<aside class="today-scope" aria-label="About this shortlist"><p>A ranked shortlist, not a task manager: up to 5 Today and 3 Next across your sources.</p>
    <p>${esc(sourceText)} ${source === 'ready' ? '<a href="https://app.todoist.com/app/today" target="_blank" rel="noopener noreferrer">View all in Todoist</a>' : ''}</p>
    <p>Snooze and Wait only hide or park items here. Change task dates, projects and recurrence in Todoist. Complete in Todoist finishes that task (recurring tasks advance there).</p>
    ${remaining ? `<p>${remaining} more eligible item${remaining === 1 ? '' : 's'} beyond this shortlist.</p>` : ''}</aside>` + body + degradedHtml(degraded);
}

function detailHtml() {
  const d = state.detail;
  const back = '<button type="button" class="today-btn today-back" data-today-back>Back to Today</button>';
  if (d.loading) return `${back}<p class="today-loading">Loading</p>`;
  if (d.error) return `${back}<p class="today-capture-error" role="alert">${esc(d.error)}</p>`;
  const o = d.outcome;
  const rows = [];
  if (o.next_action) rows.push(['Next action', esc(o.next_action)]);
  if (o.waiting_on) rows.push(['Waiting on', `${esc(o.waiting_on)}${o.follow_up_at ? `, follow up ${esc(fmtWhen(o.follow_up_at + (/[zZ]|[+-]\d\d:?\d\d$/.test(o.follow_up_at) ? '' : 'Z')))}` : ''}`]);
  if (o.detail) rows.push(['Details', esc(o.detail)]);
  const links = (o.links || []).map((l) => {
    const url = linkUrl(l);
    const label = `${LINK_LABEL[l.kind] || l.kind}${l.label ? `: ${l.label}` : (l.kind === 'chat' || l.kind === 'agent_run' ? '' : `: ${l.ref}`)}`;
    return `<li>${url ? `<a${linkAttr(url)}>${esc(label)}</a>` : esc(label)}</li>`;
  }).join('');
  const evidence = (o.evidence || []).map((e) => `<li><span class="today-source">${esc(e.kind)}</span> ${esc(e.detail || e.ref)} <span class="today-meta-by">${esc(e.by || '')}</span></li>`).join('');
  const events = (o.events || []).slice(-8).reverse().map((e) => `<li><span class="today-source">${esc(e.kind)}</span> ${esc(eventText(e))}</li>`).join('');
  const closed = o.state === 'done' || o.state === 'dropped';
  return `${back}
    <article class="today-detail" data-outcome="${esc(o.id)}">
      <h3 class="today-detail-title">${esc(o.title)}</h3>
      <p class="today-why">${esc(STATE_LABEL[o.state] || o.state)}${o.confirmed_by_person ? ', confirmed by you' : ''}</p>
      ${blockerHtml(o.blocker, o.title)}
      ${rows.length ? `<dl class="today-facts">${rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('')}</dl>` : ''}
      <h4 class="today-section-title">Links</h4>
      ${links ? `<ul class="today-plain">${links}</ul>` : '<p class="today-muted">Nothing linked yet.</p>'}
      <h4 class="today-section-title">Evidence</h4>
      ${evidence ? `<ul class="today-plain">${evidence}</ul>` : '<p class="today-muted">No evidence yet. Done needs evidence or your confirmation.</p>'}
      <h4 class="today-section-title">Recent history</h4>
      ${events ? `<ul class="today-plain">${events}</ul>` : '<p class="today-muted">No history.</p>'}
      ${closed ? '' : '<div class="today-detail-actions"><p class="today-muted">This closes only the local outcome. Linked tasks stay open in their source and may appear again.</p><button type="button" class="today-btn today-btn-primary" data-today-confirm-done>Close outcome and record my confirmation</button></div>'}
    </article>`;
}
function eventText(e) {
  const d = e.detail || {};
  if (e.kind === 'state') return `${d.from || ''} to ${d.to || ''}${d.confirmed ? ', you confirmed' : ''}`;
  if (e.kind === 'evidence') return d.detail || d.ref || '';
  if (e.kind === 'blocker') return d.cleared ? 'blocker cleared' : `blocker set${d.code ? `: ${d.code}` : ''}`;
  if (e.kind === 'link') return d.added ? `linked ${d.added.kind}` : (d.removed ? `unlinked ${d.removed.kind}` : '');
  if (e.kind === 'edit') return Object.keys(d.fields || {}).join(', ');
  return e.actor || '';
}
function linkUrl(l) {
  switch (l.kind) {
    case 'chat': return `/?session=${encodeURIComponent(l.ref)}`;
    case 'note': return `/notes?id=${encodeURIComponent(l.ref)}`;
    case 'calendar_event': return '/calendar';
    case 'todoist_task': return `https://app.todoist.com/app/task/${encodeURIComponent(l.ref)}`;
    case 'pull_request': case 'url': return /^https?:\/\//i.test(l.ref) ? l.ref : '';
    default: return '';
  }
}

function listsContainer() { return $(MODAL_ID)?.querySelector('#today-lists'); }
function render() {
  const body = $(MODAL_ID)?.querySelector('#today-body');
  if (!body) return;
  const focusId = body.contains(document.activeElement) ? document.activeElement.id : '';
  body.innerHTML = state.detail
    ? detailHtml()
    : `${captureHtml()}${statusHtml()}<div class="today-lists" id="today-lists" aria-live="polite">${listHtml()}</div>`;
  if (focusId) $(focusId)?.focus?.({ preventScroll: true });
}

// ── data ─────────────────────────────────────────────────────────────────────
export async function refresh() {
  const seq = ++loadSeq;
  try {
    const data = await api('/api/today', { headers: { 'X-Odysseus-Poll': '1' } });
    if (seq !== loadSeq) return;
    const changed = JSON.stringify(data) !== JSON.stringify(state.data);
    state.data = data; state.error = '';
    if (!changed && listsContainer()) return;
  } catch (e) {
    if (seq !== loadSeq) return;
    state.error = e.message || 'The server did not answer.';
  }
  if (!state.open || state.detail) return;
  // A background poll redraws only the lists, so a half-typed capture or wait form survives it.
  const lists = listsContainer();
  if (lists && !state.waitingFor) lists.innerHTML = listHtml(); else if (!lists) render();
}

// ── navigation out of Today ──────────────────────────────────────────────────
export function openTarget(url) {
  if (!url) return;
  let parsed;
  try { parsed = new URL(url, globalThis.location?.origin || 'http://localhost'); } catch (_) { return; }
  const here = globalThis.location?.origin;
  if (here && parsed.origin !== here && !/^https?:$/.test(parsed.protocol)) return;
  if (parsed.origin !== (here || parsed.origin)) { window.open(parsed.href, '_blank', 'noopener,noreferrer'); return; }
  const session = parsed.searchParams.get('session');
  close();
  if (parsed.pathname === '/' && session) {
    window.sessionModule?.selectSession?.(session);
  } else if (parsed.pathname === '/notes') {
    ($('tool-notes-btn') || $('rail-notes'))?.click();
  } else if (parsed.pathname === '/calendar') {
    ($('tool-calendar-btn') || $('rail-calendar'))?.click();
  }
}

// ── actions ──────────────────────────────────────────────────────────────────
function findItem(key) {
  return [...(state.data?.today || []), ...(state.data?.next || [])].find((i) => i.key === key);
}
function setStatus(text, tone = 'ok', extra = {}) { state.status = { text, tone, ...extra }; }
const ACTION_DONE = {
  defer: 'Snoozed until tomorrow morning.',
  wait: 'Parked. It comes back when the follow-up is due.',
  done: 'Marked done.',
};

async function runAction(key, action, extra = {}) {
  const item = findItem(key);
  try {
    const res = await post('/api/today/action', { key, action, ...extra });
    if (res && res.ok === false) {
      setStatus(res.error || 'That did not work.', 'error', { blocker: res.blocker || null });
      await refresh(); render();
      return;
    }
    if ((action === 'start' || action === 'resume' || action === 'open') && res.open_url) {
      state.status = null;
      openTarget(res.open_url);
      return;
    }
    const doneMessage = item?.source === 'outcome' ? 'Outcome closed here. Linked tasks were not completed.'
      : item?.source === 'todoist' ? 'Completed in Todoist. Recurring tasks follow their Todoist schedule.' : 'Note archived.';
    setStatus(action === 'done' ? doneMessage : ACTION_DONE[action] || 'Done.', 'ok');
  } catch (e) {
    setStatus(e.message || 'That did not work.', 'error');
  }
  state.waitingFor = null;
  await refresh();
  render();
  void item;
}

async function confirmDone() {
  const id = state.detail?.outcome?.id;
  if (!id) return;
  try {
    await post(`/api/outcomes/${encodeURIComponent(id)}/state`, { state: 'done', confirm: true });
    state.detail = null;
    setStatus('Outcome closed here. Your confirmation is recorded; linked tasks were not completed.', 'ok');
  } catch (e) {
    state.detail = { ...state.detail, error: e.message };
    render();
    return;
  }
  await refresh();
  render();
}

async function openDetail(key) {
  const id = String(key).replace(/^outcome:/, '');
  state.detail = { loading: true };
  render();
  try {
    const res = await api(`/api/outcomes/${encodeURIComponent(id)}`);
    state.detail = { outcome: res.outcome };
  } catch (e) {
    state.detail = { error: e.message || 'Could not load this outcome.' };
  }
  render();
}

// ── capture ──────────────────────────────────────────────────────────────────
export async function submitCapture(extra = {}) {
  const text = state.draft.trim();
  if (!text || state.busy) return;
  // A new line of text is a new submission. The same text, retried after a
  // failure or answered after a question, keeps its key so the server can
  // recognise it and never write it twice.
  if (!state.capture || state.capture.text !== text) state.capture = { key: newKey(), text };
  state.busy = true; state.captureError = ''; state.captureResult = null;
  render();
  let res = null;
  try {
    res = await post('/api/capture', { text, idempotency_key: state.capture.key, ...extra });
  } catch (e) {
    res = { status: 'failed', error: e.message || 'Could not reach the server.' };
  }
  state.busy = false;
  if (res.status === 'created' || res.status === 'duplicate') {
    state.capture = null; state.clarify = null; state.draft = '';
    state.captureResult = { destination: res.destination, open_url: res.open_url || '' };
    render();
    await refresh();
    return;
  }
  if (res.status === 'needs_clarification') {
    state.clarify = { question: res.question || 'Where should this go?', options: res.options || [] };
  } else {
    state.clarify = null;
    state.captureError = res.error || 'Could not add that. Your text is still here; try again.';
  }
  render();
}

// ── events ───────────────────────────────────────────────────────────────────
function onClick(e) {
  const t = e.target.closest ? e.target : e.target.parentElement;
  if (!t) return;
  const link = t.closest('[data-today-link]');
  if (link) { e.preventDefault(); openTarget(link.dataset.todayLink); return; }
  if (t.closest('[data-today-back]')) { state.detail = null; render(); refresh(); return; }
  if (t.closest('[data-today-reload]')) { refresh(); return; }
  if (t.closest('[data-today-confirm-done]')) { confirmDone(); return; }
  if (t.closest('[data-today-wait-cancel]')) { state.waitingFor = null; render(); return; }
  const pick = t.closest('[data-today-pick]');
  if (pick && state.clarify) {
    const option = state.clarify.options[Number(pick.dataset.todayPick)];
    if (option) submitCapture({ destination: option.destination, ...(option.due ? { due: option.due } : {}) });
    return;
  }
  const recovery = t.closest('[data-today-blocker-action]');
  if (recovery) {
    let action = null;
    try { action = JSON.parse(recovery.dataset.todayBlockerAction); } catch (_) { /* malformed: ignore */ }
    const run = (mod) => mod.applyBlockerAction(action, { task: recovery.dataset.task || '' });
    if (action) { if (window.agentActions) run(window.agentActions); else import('./agentActions.js').then((m) => run(m.default)); }
    return;
  }
  const row = t.closest('.today-item');
  if (!row) return;
  const key = row.dataset.key;
  if (t.closest('[data-today-open]')) {
    const item = findItem(key);
    if (item?.source === 'outcome') openDetail(key); else if (item?.open_url) openTarget(item.open_url);
    return;
  }
  const btn = t.closest('[data-today-action]');
  if (!btn) return;
  const action = btn.dataset.todayAction;
  if (action === 'open') { openTarget(findItem(key)?.open_url); return; }
  if (action === 'wait') { state.waitingFor = key; render(); row.ownerDocument.querySelector('[data-today-wait] input')?.focus?.(); return; }
  runAction(key, action);
}
function onSubmit(e) {
  const form = e.target;
  if (form.matches?.('#today-capture')) { e.preventDefault(); submitCapture(); return; }
  if (form.matches?.('[data-today-wait]')) {
    e.preventDefault();
    const data = new FormData(form);
    const waitingOn = String(data.get('waiting_on') || '').trim();
    if (!waitingOn) return;
    const until = String(data.get('until') || '');
    runAction(form.dataset.todayWait, 'wait', { waiting_on: waitingOn, ...(until ? { until } : {}) });
  }
}
function onInput(e) {
  if (e.target?.id === 'today-capture-input') {
    // Typing starts a fresh submission and clears the last answer.
    state.draft = e.target.value;
    state.captureError = ''; state.captureResult = null;
    if (state.capture && state.capture.text !== state.draft.trim()) state.capture = null;
    if (state.clarify) { state.clarify = null; render(); $('today-capture-input')?.focus?.(); }
  }
}

// ── open / close ─────────────────────────────────────────────────────────────
function registerWithManager() {
  if (Modals.isRegistered(MODAL_ID)) return;
  Modals.register(MODAL_ID, {
    label: 'Today',
    railBtnId: 'rail-today',
    sidebarBtnId: 'tool-today-btn',
    icon: 'M12 3v2M12 19v2M5.2 5.2l1.4 1.4M17.4 17.4l1.4 1.4M3 12h2M19 12h2M5.2 18.8l1.4-1.4M17.4 6.6l1.4-1.4M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8z',
    restoreFn: () => open(),
    closeFn: () => hide(),
  });
}
function hide() {
  const root = $(MODAL_ID);
  if (!root) return;
  const restoreFocus = root.contains(document.activeElement);
  state.open = false;
  root.hidden = true;
  root.classList.add('hidden');
  if (timer) { clearInterval(timer); timer = null; }
  if (restoreFocus && returnFocus?.isConnected) returnFocus.focus({ preventScroll: true });
}
export function open({ focus = true } = {}) {
  const root = $(MODAL_ID);
  if (!root) return;
  registerWithManager();
  if (Modals.isMinimized(MODAL_ID)) { Modals.restore(MODAL_ID); return; }
  const fresh = !state.open;
  if (fresh) { returnFocus = document.activeElement; state.detail = null; state.status = null; state.waitingFor = null; }
  state.open = true;
  root.hidden = false;
  root.classList.remove('hidden');
  render();
  refresh();
  if (focus) ($('today-capture-input') || root).focus?.({ preventScroll: true });
  if (!timer) timer = setInterval(() => { if (state.open && !state.detail && !state.busy && !document.hidden) refresh(); }, REFRESH_MS);
}
export function close() {
  if (Modals.isRegistered(MODAL_ID)) Modals.close(MODAL_ID); else hide();
}
export function toggle() {
  if (Modals.isRegistered(MODAL_ID) && Modals.toggle(MODAL_ID)) return;
  state.open ? close() : open();
}

function init() {
  const root = $(MODAL_ID);
  if (!root || root.dataset.todayWired === '1') return;
  root.dataset.todayWired = '1';
  root.addEventListener('click', onClick);
  root.addEventListener('submit', onSubmit);
  root.addEventListener('input', onInput);
  root.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !e.defaultPrevented) { e.stopPropagation(); close(); } });
  $('close-today')?.addEventListener('click', () => close());
}
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();

const todayModule = { open, close, toggle, refresh, openTarget, submitCapture };
window.todayModule = todayModule;
export default todayModule;
