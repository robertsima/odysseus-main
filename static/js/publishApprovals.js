/* publishApprovals.js — review and approve an agent's publish request in the UI.
 *
 * An agent asks to publish a change with manage_agent_worktree request_publish;
 * a person decides. This shows the open requests of the chat on screen (and of
 * the workers it started) above the composer, and a review window with the
 * files, any sensitive paths and the diff. Approving publishes on the server
 * (routes/publish_approval_routes.py): the branch is pushed and a draft PR
 * opened, and the one-time code never reaches the agent.
 *
 * Everything shown here was written by an agent (title, description, file
 * names, diff), so all of it is escaped before it touches the page.
 */

const API = '/api/agent-publish';
const POLL_MS = 30000;

let _sid = null;
let _rows = [];
let _poll = null;

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function safeHref(url) {
  try {
    const u = new URL(String(url || ''), window.location.origin);
    return (u.protocol === 'https:' || u.protocol === 'http:') ? u.href : '';
  } catch (_) {
    return '';
  }
}

function currentSession() {
  try { return window.sessionModule?.getCurrentSessionId?.() || null; } catch (_) { return null; }
}

async function api(path, options = {}) {
  const res = await fetch(API + path, { credentials: 'same-origin', ...options });
  let data = {};
  try { data = await res.json(); } catch (_) {}
  if (!res.ok) throw new Error(data.detail || data.error || `HTTP ${res.status}`);
  return data;
}

// ── banner ───────────────────────────────────────────────────────────────
function render() {
  const box = document.getElementById('publish-approvals');
  if (!box) return;
  if (!_rows.length) { box.hidden = true; box.innerHTML = ''; return; }
  box.hidden = false;
  box.innerHTML = _rows.map((r) => {
    const files = (r.changed_files || []).length;
    const sensitive = Object.keys(r.sensitive || {}).length;
    return `<div class="pa-row" data-id="${esc(r.id)}">
      <span class="pa-badge">Publish request</span>
      <span class="pa-title" title="${esc(r.title)}">${esc(r.title || r.branch)}</span>
      <span class="pa-meta">${esc(r.repo || '')} · ${files} file${files === 1 ? '' : 's'}${sensitive ? ' · <b class="pa-warn">sensitive</b>' : ''}</span>
      <button type="button" class="pa-btn pa-btn-primary" data-pa="review" data-id="${esc(r.id)}">${r.can_decide ? 'Review' : 'View'}</button>
    </div>`;
  }).join('');
}

export async function refresh(sessionId = currentSession()) {
  _sid = sessionId;
  if (!sessionId) { _rows = []; render(); return; }
  try {
    const data = await api(`/requests?session_id=${encodeURIComponent(sessionId)}`);
    if (_sid !== sessionId) return;           // the chat changed meanwhile
    _rows = data.requests || [];
  } catch (_) {
    _rows = [];
  }
  render();
}

// ── review window ────────────────────────────────────────────────────────
function modal() {
  let el = document.getElementById('pa-modal');
  if (el) return el;
  el = document.createElement('div');
  el.id = 'pa-modal';
  el.className = 'pa-modal hidden';
  el.setAttribute('role', 'dialog');
  el.setAttribute('aria-modal', 'true');
  el.setAttribute('aria-label', 'Publish request');
  el.innerHTML = '<div class="pa-dialog"><div class="pa-body"></div></div>';
  el.addEventListener('click', (e) => {
    if (e.target === el) close();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !el.classList.contains('hidden')) close();
  });
  document.body.appendChild(el);
  return el;
}

function close() {
  const el = document.getElementById('pa-modal');
  if (el) el.classList.add('hidden');
}

function patchHtml(patch) {
  return String(patch || '').split('\n').map((line) => {
    const cls = line.startsWith('+++') || line.startsWith('---') ? 'pa-d-file'
      : line.startsWith('@@') ? 'pa-d-hunk'
      : line.startsWith('+') ? 'pa-d-add'
      : line.startsWith('-') ? 'pa-d-del'
      : line.startsWith('diff --git') ? 'pa-d-file' : '';
    return `<span class="${cls}">${esc(line)}</span>`;
  }).join('\n');
}

function reviewHtml(r) {
  const files = r.changed_files || [];
  const sensitive = r.sensitive || {};
  const sensKeys = Object.keys(sensitive);
  const blockers = r.blockers || [];
  const review = r.review || {};
  const open = r.status === 'pending' || r.status === 'granted';
  const when = r.created_at ? new Date(r.created_at * 1000).toLocaleString() : '';
  let actions = '';
  if (!open) {
    actions = `<p class="pa-note">This request is ${esc(r.status)}.</p>`;
  } else if (!r.can_decide) {
    actions = '<p class="pa-note">Waiting for an admin, or for a user with the "Approve agent publishes" privilege.</p>';
  } else if (blockers.length) {
    actions = `<p class="pa-note pa-warn">Publishing is not set up on this server: ${esc(blockers.join('; '))}</p>
      <div class="pa-actions"><button type="button" class="pa-btn" data-pa="reject" data-id="${esc(r.id)}">Reject</button></div>`;
  } else {
    actions = `${sensKeys.length ? `<label class="pa-check"><input type="checkbox" data-pa-sensitive> I reviewed the sensitive paths above</label>` : ''}
      <div class="pa-actions">
        <button type="button" class="pa-btn" data-pa="reject" data-id="${esc(r.id)}">Reject</button>
        <button type="button" class="pa-btn pa-btn-primary" data-pa="approve" data-id="${esc(r.id)}">Approve and publish</button>
      </div>`;
  }
  return `
    <div class="pa-head">
      <div>
        <div class="pa-kicker">Publish request</div>
        <h3 class="pa-h">${esc(r.title || r.branch)}</h3>
        <div class="pa-meta">${esc(r.repo || '')} · <code>${esc(r.branch)}</code> @ <code>${esc(String(r.head_sha || '').slice(0, 12))}</code>
          → <code>${esc(r.base_branch || '')}</code> · asked by ${esc(r.requested_by || 'an agent')}${when ? ` · ${esc(when)}` : ''}</div>
      </div>
      <button type="button" class="pa-x" data-pa="close" aria-label="Close">×</button>
    </div>
    ${r.body ? `<div class="pa-section"><div class="pa-label">Description</div><div class="pa-text">${esc(r.body)}</div></div>` : ''}
    ${sensKeys.length ? `<div class="pa-section pa-sensitive"><div class="pa-label">Sensitive paths</div>
      ${sensKeys.map((k) => `<div><b>${esc(k)}</b>: ${(sensitive[k] || []).map((f) => `<code>${esc(f)}</code>`).join(', ')}</div>`).join('')}</div>` : ''}
    <div class="pa-section"><div class="pa-label">${files.length} changed file${files.length === 1 ? '' : 's'}</div>
      <div class="pa-files">${files.map((f) => `<code>${esc(f)}</code>`).join('')}</div></div>
    <div class="pa-section"><div class="pa-label">Diff${review.base ? ` against <code>${esc(review.base)}</code>` : ''}${review.truncated ? ' (truncated)' : ''}</div>
      ${review.error ? `<p class="pa-note pa-warn">${esc(review.error)}</p>` : `<pre class="pa-diff">${patchHtml(review.patch)}</pre>`}</div>
    <div class="pa-result" aria-live="polite"></div>
    ${actions}`;
}

async function openReview(id) {
  const el = modal();
  const body = el.querySelector('.pa-body');
  body.innerHTML = '<p class="pa-note">Loading…</p>';
  el.classList.remove('hidden');
  try {
    body.innerHTML = reviewHtml(await api(`/requests/${encodeURIComponent(id)}`));
  } catch (err) {
    body.innerHTML = `<p class="pa-note pa-warn">${esc(err.message)}</p>`;
  }
}

async function decide(id, action) {
  const el = modal();
  const result = el.querySelector('.pa-result');
  const buttons = el.querySelectorAll('[data-pa="approve"], [data-pa="reject"]');
  const sensitiveBox = el.querySelector('[data-pa-sensitive]');
  if (action === 'approve' && sensitiveBox && !sensitiveBox.checked) {
    result.innerHTML = '<p class="pa-note pa-warn">Tick the box to confirm you reviewed the sensitive paths.</p>';
    return;
  }
  buttons.forEach((b) => { b.disabled = true; });
  result.innerHTML = `<p class="pa-note">${action === 'approve' ? 'Publishing…' : 'Rejecting…'}</p>`;
  try {
    const data = await api(`/requests/${encodeURIComponent(id)}/${action}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ allow_sensitive: !!(sensitiveBox && sensitiveBox.checked) }),
    });
    if (action === 'approve') {
      const pr = (data.published || {}).pull_request || {};
      const href = safeHref(pr.html_url || pr.url);
      result.innerHTML = `<p class="pa-note pa-ok">Published ${esc((data.published || {}).branch || '')}.
        ${href ? `<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">Open the draft PR</a>` : ''}</p>`;
    } else {
      result.innerHTML = '<p class="pa-note">Rejected; nothing was pushed.</p>';
    }
    el.querySelectorAll('.pa-actions, .pa-check').forEach((n) => n.remove());
  } catch (err) {
    result.innerHTML = `<p class="pa-note pa-warn">${esc(err.message)}</p>`;
    buttons.forEach((b) => { b.disabled = false; });
  }
  refresh(_sid);
}

document.addEventListener('click', (e) => {
  const btn = e.target.closest('[data-pa]');
  if (!btn) return;
  const act = btn.dataset.pa;
  if (act === 'review') openReview(btn.dataset.id);
  else if (act === 'close') close();
  else if (act === 'approve' || act === 'reject') decide(btn.dataset.id, act);
});

// A chat was opened or re-rendered (a worker's hand-back reloads it).
document.addEventListener('odysseus:history-rendered', (e) => refresh(e.detail && e.detail.sessionId));
// A turn ended: the agent may just have asked to publish.
window.addEventListener('odysseus:chat-busy-change', (e) => {
  if (e.detail && e.detail.active === false) setTimeout(() => refresh(), 500);
});
_poll = setInterval(() => {
  if (document.visibilityState === 'visible') refresh();
}, POLL_MS);

window.publishApprovals = { refresh, openReview };
export default { refresh, openReview };
// For tests/test_publish_approvals_js.py: the renderers, run on hostile input.
export const _forTests = { reviewHtml, patchHtml, safeHref, esc };
