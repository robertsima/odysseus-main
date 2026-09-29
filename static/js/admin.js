// static/js/admin.js — Admin panel module (ES6)
// Admin-backed Settings tabs: users, providers (endpoints), built-in tools,
// personal documents, logs, diagnostics, backup and the danger zone.

import uiModule from './ui.js';
import settingsModule from './settings.js';
import { resolveSettingsPanelId } from './settings/registry.js';
import { providerLogo, providerLogoFromUrl } from './providers.js';
import { sortModelObjects } from './modelSort.js';
import { PROVIDER_DEVICE_FLOWS, formatDeviceFlowError, runProviderDeviceFlow } from './providerDeviceFlow.js';
import { getSettings, getTools, invalidateSettings, invalidateTools } from './appConfig.js';

let initialized = false;
let modalEl = null;
// When the user adds an endpoint, store its id so the next render of
// the endpoints list can flash a glow on that row. Cleared once the
// animation fires.
let _recentlyAddedEpId = null;
let _authPolicy = { password_min_length: 8, reserved_usernames: [] };

function el(id) { return document.getElementById(id); }
function esc(s) { return uiModule.esc(s); }

/* ═══════════════════════════════════════════
   USERS TAB
   ═══════════════════════════════════════════ */
const PRIV_LABELS = {
  can_use_agent: 'Agent mode',
  can_use_browser: 'Browser automation',
  can_use_bash: 'Shell / Python / Files',
  can_use_documents: 'Document editor',
  can_use_research: 'Deep research',
  can_generate_images: 'Image generation',
  can_manage_memory: 'Memory & skills',
  can_approve_publish: 'Approve agent publishes (own chats)',
};

async function loadUsers() {
  const list = el('adm-userList');
  try {
    const res = await fetch('/api/auth/users', { credentials: 'same-origin' });
    if (res.status === 401 || res.status === 403) { list.innerHTML = '<div class="admin-empty">Access denied</div>'; return; }
    const data = await res.json();
    if (!data.users || data.users.length === 0) { list.innerHTML = '<div class="admin-empty">No users found</div>'; return; }
    list.innerHTML = '';
    data.users.forEach(u => {
      const row = document.createElement('div');
      row.className = 'admin-user-row';

      // Header: name + badges + delete
      const header = document.createElement('div');
      header.style.cssText = 'display:flex;align-items:center;justify-content:space-between;cursor:pointer;padding:4px 0;';
      const initial = u.username.charAt(0).toUpperCase();
      header.innerHTML = `
        <div class="admin-user-info">
          <div style="width:28px;height:28px;border-radius:50%;background:color-mix(in srgb, var(--accent) 20%, var(--panel));display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:600;flex-shrink:0;color:var(--accent);">${esc(initial)}</div>
          <div>
            <span class="admin-user-name">${esc(u.username)}</span>
            ${u.is_admin ? '<span class="admin-badge" style="margin-left:6px;">ADMIN</span>' : '<span style="font-size:10px;opacity:0.4;display:block;">Click to manage privileges</span>'}
          </div>
        </div>
        <div style="display:flex;gap:8px;align-items:center;">
          <button class="admin-btn-sm" data-adm-toggle-admin="${esc(u.username)}" data-make-admin="${u.is_admin ? '0' : '1'}" style="font-size:11px;">${u.is_admin ? 'Revoke admin' : 'Make admin'}</button>
          <button class="admin-btn-sm" data-adm-rename-user="${esc(u.username)}" style="font-size:11px;">Rename</button>
          ${u.is_admin ? '' : `<button class="admin-btn-delete" data-adm-del-user="${esc(u.username)}" style="font-size:11px;">Remove</button>`}
          ${u.is_admin ? '' : '<svg class="admin-user-chevron" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="opacity:0.3;transition:transform 0.2s,opacity 0.2s;"><polyline points="6 9 12 15 18 9"/></svg>'}
        </div>
      `;
      row.appendChild(header);

      // Privileges panel (hidden by default, not for admins)
      if (!u.is_admin) {
        const privPanel = document.createElement('div');
        privPanel.className = 'admin-priv-panel hidden';
        privPanel.style.cssText = 'padding:8px 0 4px;border-top:1px solid var(--border);margin-top:8px;';

        // Boolean toggles
        let html = '<div style="font-size:10px;text-transform:uppercase;letter-spacing:0.5px;opacity:0.35;font-weight:600;margin-bottom:4px;">Features</div>';
        for (const [key, label] of Object.entries(PRIV_LABELS)) {
          const checked = u.privileges && u.privileges[key] ? 'checked' : '';
          html += `<div style="display:flex;align-items:center;justify-content:space-between;padding:4px 0;">
            <span style="font-size:12px;">${label}</span>
            <label class="admin-switch" style="transform:scale(0.85);"><input type="checkbox" data-priv="${key}" data-user="${esc(u.username)}" ${checked}><span class="admin-slider"></span></label>
          </div>`;
        }
        // Rate limit
        html += '<div style="font-size:10px;text-transform:uppercase;letter-spacing:0.5px;opacity:0.35;font-weight:600;margin:10px 0 4px;">Limits</div>';
        const maxMsg = (u.privileges && u.privileges.max_messages_per_day) || 0;
        html += `<div style="display:flex;align-items:center;justify-content:space-between;padding:4px 0;">
          <div>
            <span style="font-size:12px;">Daily message limit</span>
            <div style="font-size:10px;opacity:0.4;">0 = no limit</div>
          </div>
          <input type="number" min="0" value="${maxMsg}" data-priv="max_messages_per_day" data-user="${esc(u.username)}" style="width:70px;padding:4px 6px;background:var(--bg);border:1px solid var(--border);border-radius:4px;color:var(--fg);font-size:12px;text-align:center;">
        </div>`;
        // Allowed models — checkbox list
        const allowedModels = Array.isArray(u.privileges && u.privileges.allowed_models)
          ? u.privileges.allowed_models
          : [];
        const allowedSet = new Set(allowedModels);
        const modelsRestricted = !!(u.privileges && u.privileges.allowed_models_restricted);
        const blockAllModels = !!(u.privileges && u.privileges.block_all_models);
        html += `<div style="padding:4px 0;">
          <div style="display:flex;align-items:center;justify-content:space-between;">
            <span style="font-size:12px;">Allowed models</span>
            <div style="display:flex;gap:8px;">
              <a href="#" class="priv-models-all" data-user="${esc(u.username)}" style="font-size:10px;opacity:0.5;">All</a>
              <a href="#" class="priv-models-none" data-user="${esc(u.username)}" style="font-size:10px;opacity:0.5;">None</a>
            </div>
          </div>
          <div style="font-size:10px;opacity:0.4;margin-bottom:4px;">${blockAllModels ? 'No models allowed' : (!modelsRestricted ? 'All models allowed (no restrictions)' : (allowedSet.size === 0 ? 'No models allowed' : allowedSet.size + ' model(s) allowed'))}</div>
          <div class="priv-models-list" data-user="${esc(u.username)}">
            <span style="opacity:0.4;font-size:11px;">Loading models...</span>
          </div>
        </div>`;
        privPanel.innerHTML = html;
        row.appendChild(privPanel);

        // Toggle panel visibility + rotate chevron + load models
        let _modelsLoaded = false;
        header.addEventListener('click', (e) => {
          if (e.target.closest('.admin-btn-delete, [data-adm-rename-user], [data-adm-toggle-admin]')) return;
          privPanel.classList.toggle('hidden');
          const chevron = header.querySelector('.admin-user-chevron');
          if (chevron) {
            const isOpen = !privPanel.classList.contains('hidden');
            chevron.style.transform = isOpen ? 'rotate(180deg)' : '';
            chevron.style.opacity = isOpen ? '0.7' : '0.3';
          }
          // Load models list on first expand
          if (!_modelsLoaded && !privPanel.classList.contains('hidden')) {
            _modelsLoaded = true;
            _loadModelsForUser(u.username, allowedSet, modelsRestricted, blockAllModels, privPanel);
          }
        });

        // Wire privilege changes (boolean + number inputs, not model checkboxes)
        privPanel.querySelectorAll('[data-priv]').forEach(input => {
          const handler = async () => {
            const username = input.dataset.user;
            const key = input.dataset.priv;
            let value;
            if (input.type === 'checkbox') value = input.checked;
            else if (input.type === 'number') value = parseInt(input.value) || 0;
            else value = input.value;
            try {
              await fetch(`/api/auth/users/${encodeURIComponent(username)}/privileges`, {
                method: 'PUT', credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ [key]: value }),
              });
            } catch (e) { uiModule.showError('Failed to update privilege'); }
          };
          if (input.type === 'checkbox') input.addEventListener('change', handler);
          else input.addEventListener('change', handler);
        });
      }

      // Rename button
      const renameBtn = row.querySelector('[data-adm-rename-user]');
      if (renameBtn) {
        renameBtn.addEventListener('click', async (e) => {
          e.stopPropagation();
          const oldUsername = renameBtn.dataset.admRenameUser;
          const next = await uiModule.styledPrompt(`Rename "${oldUsername}"`, {
            defaultValue: oldUsername,
            placeholder: 'New username',
            confirmText: 'Rename',
          });
          const username = (next || '').trim();
          if (!username || username === oldUsername) return;
          try {
            const res = await fetch(`/api/auth/users/${encodeURIComponent(oldUsername)}/rename`, {
              method: 'PUT',
              credentials: 'same-origin',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ username }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) {
              uiModule.showError(data.detail || 'Failed to rename user');
              return;
            }
            if (data.renamed_self) {
              window.location.reload();
              return;
            }
            loadUsers();
          } catch (err) {
            uiModule.showError('Failed to rename user');
          }
        });
      }

      // Delete button
      const delBtn = row.querySelector('[data-adm-del-user]');
      if (delBtn) {
        delBtn.addEventListener('click', async (e) => {
          e.stopPropagation();
          const username = delBtn.dataset.admDelUser;
          if (!await uiModule.styledConfirm(`Remove user "${username}"?`, { confirmText: 'Remove', danger: true })) return;
          const res = await fetch('/api/auth/users', { method: 'DELETE', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username }) });
          if (res.ok) loadUsers();
          else uiModule.showError('Failed to delete user');
        });
      }

      // Promote / demote (admin toggle) — present on every row
      const adminToggleBtn = row.querySelector('[data-adm-toggle-admin]');
      if (adminToggleBtn) {
        adminToggleBtn.addEventListener('click', async (e) => {
          e.stopPropagation();
          const username = adminToggleBtn.dataset.admToggleAdmin;
          const makeAdmin = adminToggleBtn.dataset.makeAdmin === '1';
          const confirmMsg = makeAdmin
            ? `Grant admin rights to "${username}"? They'll get full access to all settings and users — including the power to demote or remove other admins (you included).`
            : `Revoke admin rights from "${username}"? They'll lose access to the admin panel.`;
          if (!await uiModule.styledConfirm(confirmMsg, { confirmText: makeAdmin ? 'Make admin' : 'Revoke admin', danger: !makeAdmin })) return;
          adminToggleBtn.disabled = true;
          try {
            const res = await fetch(`/api/auth/users/${encodeURIComponent(username)}/admin`, {
              method: 'PUT',
              credentials: 'same-origin',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ is_admin: makeAdmin }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) {
              uiModule.showError(data.detail || 'Failed to change admin status');
              adminToggleBtn.disabled = false;
              return;
            }
            // Demoting yourself drops your own admin access — reload into the
            // normal-user view (mirrors the rename-self reload above).
            if (data.self) { window.location.reload(); return; }
            loadUsers();
          } catch (err) {
            uiModule.showError('Failed to change admin status');
            adminToggleBtn.disabled = false;
          }
        });
      }

      list.appendChild(row);
    });
  } catch (e) { list.innerHTML = '<div class="admin-error">Failed to load users</div>'; }
}

async function _loadModelsForUser(username, allowedSet, modelsRestricted, blockAllModels, privPanel) {
  const listEl = privPanel.querySelector(`.priv-models-list[data-user="${username}"]`);
  if (!listEl) return;
  try {
    // Use /api/model-endpoints rather than /api/models — the latter is
    // backed by `cached_models`, so endpoints that haven't been probed yet
    // (e.g. a freshly-added cloud API like DeepSeek) simply don't show up
    // until some other endpoint happens to trigger a cache refresh. The
    // endpoints listing always reflects every configured endpoint.
    const res = await fetch('/api/model-endpoints', { credentials: 'same-origin' });
    const data = await res.json();
    const allModels = [];
    (Array.isArray(data) ? data : []).forEach(ep => {
      if (!ep.online) return;
      (ep.models || []).forEach(mid => {
        allModels.push({ mid, epName: ep.name || '', display: mid.split('/').pop() });
      });
    });
    if (!allModels.length) {
      listEl.innerHTML = '<span style="opacity:0.4;font-size:11px;">No models available</span>';
      return;
    }
    let restricted = modelsRestricted;
    let blockAll = blockAllModels;
    listEl.innerHTML = sortModelObjects(allModels).map(m => {
      const checked = !blockAll && (!restricted || allowedSet.has(m.mid)) ? 'checked' : '';
      return `<label>
        <input type="checkbox" class="priv-model-cb" data-mid="${esc(m.mid)}" ${checked}>
        <span>${esc(m.display)}</span>
        <span style="opacity:0.3;font-size:10px;margin-left:auto;">${esc(m.epName)}</span>
      </label>`;
    }).join('');

    // Save on change
    function _saveModels() {
      const checked = [];
      listEl.querySelectorAll('.priv-model-cb').forEach(cb => {
        if (cb.checked) checked.push(cb.dataset.mid);
      });
      // Three distinct states the backend must be able to tell apart:
      //  - all checked   -> no restriction (allowed_models: [], block_all_models: false)
      //  - none checked  -> block everything (allowed_models: [], block_all_models: true)
      //  - some checked  -> allowlist (allowed_models: checked, block_all_models: false)
      let value, hintText;
      if (checked.length === allModels.length) {
        restricted = false;
        blockAll = false;
        value = [];
        hintText = 'All models allowed (no restrictions)';
      } else if (checked.length === 0) {
        restricted = true;
        blockAll = true;
        value = [];
        hintText = 'No models allowed';
      } else {
        restricted = true;
        blockAll = false;
        value = checked;
        hintText = value.length + ' model(s) allowed';
      }
      const hint = privPanel.querySelector('.priv-models-list[data-user]')?.previousElementSibling?.querySelector('div[style*="opacity"]');
      if (hint) hint.textContent = hintText;
      fetch(`/api/auth/users/${encodeURIComponent(username)}/privileges`, {
        method: 'PUT', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ allowed_models: value, allowed_models_restricted: restricted, block_all_models: blockAll }),
      }).catch(() => {});
    }
    listEl.querySelectorAll('.priv-model-cb').forEach(cb => cb.addEventListener('change', _saveModels));

    // All / None buttons
    privPanel.querySelector(`.priv-models-all[data-user="${username}"]`)?.addEventListener('click', (e) => {
      e.preventDefault();
      listEl.querySelectorAll('.priv-model-cb').forEach(cb => cb.checked = true);
      _saveModels();
    });
    privPanel.querySelector(`.priv-models-none[data-user="${username}"]`)?.addEventListener('click', (e) => {
      e.preventDefault();
      listEl.querySelectorAll('.priv-model-cb').forEach(cb => cb.checked = false);
      _saveModels();
    });
  } catch (e) {
    listEl.innerHTML = '<span style="opacity:0.4;font-size:11px;">Failed to load models</span>';
  }
}

function initSignupToggle() {
  const toggle = el('adm-signupToggle');
  fetch('/api/auth/status', { credentials: 'same-origin' })
    .then(r => r.json())
    .then(d => { toggle.checked = !!d.signup_enabled; })
    .catch(e => console.warn('Auth status fetch failed:', e));
  toggle.addEventListener('change', async () => {
    try {
      const res = await fetch('/api/auth/signup-toggle', { method: 'POST', credentials: 'same-origin' });
      const data = await res.json();
      toggle.checked = data.signup_enabled;
    } catch (e) { toggle.checked = !toggle.checked; }
  });
}

function initShareDefaultsToggle() {
  const toggle = el('adm-shareDefaultsToggle');
  getSettings()
    .then(d => { toggle.checked = !!d.share_defaults_with_users; })
    .catch(e => console.warn('Settings fetch failed:', e));
  toggle.addEventListener('change', async () => {
    try {
      const res = await fetch('/api/auth/settings', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ share_defaults_with_users: toggle.checked }),
      });
      const data = await res.json();
      toggle.checked = !!data.share_defaults_with_users;
    } catch (e) {
      toggle.checked = !toggle.checked;
    } finally {
      // Drop the shared snapshot: it still says what this toggle used to be.
      invalidateSettings();
    }
  });
}

function initAddUser() {
  fetch('/api/auth/policy', { credentials: 'same-origin' })
    .then(r => r.ok ? r.json() : null)
    .then(policy => {
      if (!policy) return;
      _authPolicy = policy;
      const admPw = el('adm-newPassword');
      if (admPw) admPw.placeholder = `Password (min ${policy.password_min_length})`;
    })
    .catch(() => {});
  el('adm-addBtn').addEventListener('click', async () => {
    const msg = el('adm-addMsg');
    msg.textContent = ''; msg.className = '';
    const username = el('adm-newUsername').value.trim();
    const password = el('adm-newPassword').value;
    const is_admin = el('adm-newIsAdmin').checked;
    if (!username) { msg.textContent = 'Username required'; msg.className = 'admin-error'; return; }
    if (password.length < _authPolicy.password_min_length) { msg.textContent = `Password must be at least ${_authPolicy.password_min_length} characters`; msg.className = 'admin-error'; return; }
    if (_authPolicy.reserved_usernames.includes(username.toLowerCase())) { msg.textContent = 'This username is reserved'; msg.className = 'admin-error'; return; }
    el('adm-addBtn').disabled = true;
    try {
      const res = await fetch('/api/auth/users', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username, password, is_admin }) });
      const data = await res.json();
      if (res.ok) { msg.textContent = 'User created'; msg.className = 'admin-success'; el('adm-newUsername').value = ''; el('adm-newPassword').value = ''; el('adm-newIsAdmin').checked = false; loadUsers(); }
      else { msg.textContent = data.detail || 'Failed'; msg.className = 'admin-error'; }
    } catch (e) { msg.textContent = 'Request failed'; msg.className = 'admin-error'; }
    el('adm-addBtn').disabled = false;
  });
}

/* ═══════════════════════════════════════════
   SERVICES TAB — Endpoints
   ═══════════════════════════════════════════ */
function _isLocalEndpoint(url) {
  if (!url) return false;
  try {
    const u = new URL(url);
    const h = u.hostname.toLowerCase();
    if (h === 'localhost' || h === '127.0.0.1' || h === '0.0.0.0') return true;
    if (h.endsWith('.local')) return true;
    if (/^10\./.test(h)) return true;
    if (/^192\.168\./.test(h)) return true;
    if (/^172\.(1[6-9]|2[0-9]|3[01])\./.test(h)) return true;
    // Tailscale CGNAT range (100.64.0.0/10 → 100.64.x–100.127.x). Servers
    // found via "Scan for Servers" come back as tailnet IPs, which are still
    // your own machines, so group them under Local rather than API.
    if (/^100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\./.test(h)) return true;
    // Single-label hostnames are LAN by convention.
    if (!h.includes('.')) return true;
    return false;
  } catch { return false; }
}

async function _refreshAfterEndpointChange(deletedEndpointId) {
  try {
    const sm = window.sessionModule;
    const pending = sm && sm.getPendingChat ? sm.getPendingChat() : null;
    if (deletedEndpointId && pending && String(pending.endpointId || '') === String(deletedEndpointId)) {
      if (sm.setPendingChat) sm.setPendingChat(null);
    }
  } catch (_) {}
  try {
    if (window.modelsModule && window.modelsModule.refreshModels) {
      await window.modelsModule.refreshModels(true);
    }
  } catch (_) {}
  try {
    window.dispatchEvent(new CustomEvent('ge:model-endpoints-updated', {
      detail: { deletedEndpointId: deletedEndpointId || null }
    }));
  } catch (_) {}
  try {
    if (window.sessionModule && window.sessionModule.updateModelPicker) {
      window.sessionModule.updateModelPicker();
    }
  } catch (_) {}
}

async function _selectAddedModelInChat(endpoint) {
  const modelId = endpoint && Array.isArray(endpoint.models) ? endpoint.models[0] : '';
  if (!modelId) return;
  try {
    if (window.modelsModule && window.modelsModule.refreshModels) {
      await window.modelsModule.refreshModels(true);
    }
  } catch (_) {}
  try {
    document.dispatchEvent(new CustomEvent('odysseus:auto-select-model', {
      detail: {
        endpointId: endpoint.id || '',
        endpointName: endpoint.name || '',
        modelId,
        url: endpoint.base_url || '',
      }
    }));
  } catch (_) {}
}

// The Providers card keeps the add form folded away behind "Add provider";
// it opens by itself only when nothing is connected yet.
function _setAddProviderOpen(open) {
  const form = el('adm-addProvider');
  const toggle = el('adm-addProviderToggle');
  if (!form) return;
  form.hidden = !open;
  if (toggle) toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
}

async function loadEndpoints() {
  const listLocal = el('adm-epList-local');
  const listApi = el('adm-epList-api');
  // Render endpoint rows first. Do not make Added Models wait on /api/models or
  // endpoint probes; explicit Refresh/Probe actions do that work.
  const refreshDependentModelUi = (force = false, endpoints = null) => {
    setTimeout(() => {
      if (window.modelsModule && window.modelsModule.refreshModels) {
        window.modelsModule.refreshModels(!!force, force ? {} : { cacheOnly: true }).then(() => {
          if (window.sessionModule && window.sessionModule.updateModelPicker) {
            window.sessionModule.updateModelPicker();
          }
        }).catch(() => {});
      }
      // Hand over the list just fetched so the model-role pickers do not
      // fetch /api/model-endpoints a second time for the same render.
      if (settingsModule && typeof settingsModule.refreshAiModelEndpoints === 'function') {
        settingsModule.refreshAiModelEndpoints(Array.isArray(endpoints) ? endpoints : undefined);
      }
    }, 0);
  };
  try {
    const res = await fetch('/api/model-endpoints', { credentials: 'same-origin' });
    // Treat a non-OK response (e.g. 401/403 for non-admins, or backend
    // returning an error envelope) the same as "no endpoints yet": show the
    // empty state, not "Failed to load". The user just installed the app —
    // there's literally nothing to load, so the error read as broken UI.
    let data = [];
    if (res.ok) {
      try { data = await res.json(); } catch { data = []; }
    }
    // Only a list the server actually returned is handed to the role pickers;
    // on an error they fetch for themselves instead of being emptied.
    const fetched = res.ok && Array.isArray(data) ? data : null;
    if (!Array.isArray(data) || data.length === 0) {
      const empty = '<div class="admin-empty">None</div>';
      if (listLocal) listLocal.innerHTML = empty;
      if (listApi) listApi.innerHTML = '<div class="admin-empty">None</div>';
      // Nothing connected yet: the add form is the only useful thing here.
      if (res.ok) _setAddProviderOpen(true);
      refreshDependentModelUi(false, fetched);
      return;
    }
    const rowHtml = data.map(ep => {
      const epModels = Array.isArray(ep.models) ? ep.models : [];
      const pinnedModels = Array.isArray(ep.pinned_models) ? ep.pinned_models : [];
      const visibleCount = ep.picker_requires_pinning ? pinnedModels.length : epModels.length;
      const totalCount = Number.isFinite(Number(ep.model_count))
        ? Number(ep.model_count)
        : visibleCount + (ep.hidden_count || 0);
      // `ep.models` is the *visible* set — when every model is hidden it's
      // empty, but we still need to render the expand panel so the user can
      // un-hide them. Gate on the total instead.
      const hasModels = ep.online && totalCount > 0;
      const statusBadge = ep.status === 'empty'
        ? '<span class="admin-badge">no models</span>'
        : ep.online
          ? `<span class="admin-badge">${visibleCount}/${totalCount} models enabled</span>`
          : '<span class="admin-badge admin-badge-off">offline</span>';
      const justAddedClass = (_recentlyAddedEpId && String(ep.id) === _recentlyAddedEpId) ? ' adm-ep-just-added' : '';
      const category = ep.category || (_isLocalEndpoint(ep.base_url) ? 'local' : 'api');
      // Editable rather than a static badge so endpoint topology remains an
      // explicit operator choice. Private-vault access is granted separately
      // for each chat and is not inferred from this classification.
      // It lives in the row's Advanced area: almost nobody needs to change it.
      const epKind = ep.endpoint_kind || 'auto';
      const kindLabels = { auto: 'auto (decide from the host)', local: 'local', api: 'api', proxy: 'proxy' };
      const kindSelect = ['auto', 'local', 'api', 'proxy']
        .map(k => `<option value="${k}"${epKind === k ? ' selected' : ''}>${kindLabels[k]}</option>`)
        .join('');
      const keyLabel = ep.has_key
        ? (ep.api_key_fingerprint ? ` (key ${esc(ep.api_key_fingerprint)})` : ' (key set)')
        : '';
      return `
        <div class="admin-user-row${ep.is_enabled ? '' : ' admin-ep-disabled'}${justAddedClass}" data-adm-ep-id="${ep.id}">
          <div style="display:flex;align-items:center;justify-content:space-between;${hasModels ? 'cursor:pointer;' : ''}padding:4px 0;" data-adm-ep-header="${ep.id}">
            <div class="admin-user-info" style="flex:1;flex-wrap:wrap;gap:0.3rem;align-items:center;">
              <span class="adm-ep-row-logo" style="display:inline-flex;align-items:center;justify-content:center;width:16px;height:16px;flex-shrink:0;opacity:0.9;">${providerLogoFromUrl(ep.base_url) || ''}</span>
              <span class="admin-user-name">${esc(ep.name)}</span>
              ${ep.model_type === 'image' ? '<span class="admin-badge" style="background:color-mix(in srgb, var(--accent) 20%, transparent);color:var(--accent);">Image</span>' : ''}
              ${statusBadge}
              ${ep.is_enabled ? '' : '<span class="admin-badge admin-badge-off">disabled</span>'}
              ${hasModels ? `<span style="font-size:10px;opacity:0.4;${category === 'api' ? 'flex-basis:100%;' : ''}">Click to manage models</span>` : ''}
            </div>
            <div style="display:flex;gap:4px;align-items:center;">
              <button class="admin-btn-sm" data-adm-toggle-ep="${ep.id}">${ep.is_enabled ? 'Disable' : 'Enable'}</button>
              <button class="admin-btn-delete" data-adm-del-ep="${ep.id}" data-adm-ep-online="${ep.online ? '1' : '0'}">Delete</button>
              ${hasModels ? '<svg class="admin-user-chevron" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="opacity:0.3;transition:transform 0.2s,opacity 0.2s;"><polyline points="6 9 12 15 18 9"/></svg>' : ''}
            </div>
          </div>
          <div class="admin-ep-detail">${esc(ep.base_url)}${category === 'local' ? `<button type="button" class="admin-ep-copy-btn" data-adm-copy-url="${esc(ep.base_url)}" title="Copy URL" aria-label="Copy URL" style="background:none;border:none;padding:0 2px;margin-left:6px;cursor:pointer;color:inherit;opacity:0.45;vertical-align:-2px;line-height:1;"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg></button>` : ''}${keyLabel}</div>
          <details class="admin-ep-advanced">
            <summary>Advanced</summary>
            <label class="admin-ep-kind-row"><span>Runs on</span>
              <select class="admin-select-sm" data-adm-ep-kind="${ep.id}" title="Where this model runs. Private-vault access is granted separately for each chat. 'auto' decides from the host.">${kindSelect}</select>
            </label>
          </details>
          ${hasModels ? `<div class="mcp-tools-panel hidden" data-adm-ep-models-panel="${ep.id}"></div>` : ''}
        </div>`;
    });
    // Partition rows into Local vs API for the split sections.
    // Subsections without any rows are hidden entirely (heading + all)
    // so empty groups don't take up vertical real estate.
    const _renderInto = (container, indices) => {
      if (!container) return;
      const section = container.closest('.adm-ep-section');
      if (!indices.length) {
        if (section) section.style.display = 'none';
        container.innerHTML = '';
        return;
      }
      if (section) section.style.display = '';
      container.innerHTML = indices.map(i => rowHtml[i]).join('');
    };
    const localIdx = [], apiIdx = [];
    data.forEach((ep, i) => ((ep.category || (_isLocalEndpoint(ep.base_url) ? 'local' : 'api')) === 'local' ? localIdx : apiIdx).push(i));
    // Sort each section: enabled endpoints first, disabled at the bottom.
    // Preserve original order within each group via stable sort.
    const _sortByEnabled = (a, b) => Number(!!data[b].is_enabled) - Number(!!data[a].is_enabled);
    localIdx.sort(_sortByEnabled);
    apiIdx.sort(_sortByEnabled);
    _renderInto(listLocal, localIdx);
    _renderInto(listApi, apiIdx);
    // Iterate matching nodes across both containers.
    const queryAll = (sel) => {
      const out = [];
      [listLocal, listApi].forEach(c => {
        if (c) c.querySelectorAll(sel).forEach(n => out.push(n));
      });
      return out;
    };
    queryAll('[data-adm-toggle-ep]').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        await fetch(`/api/model-endpoints/${btn.dataset.admToggleEp}`, { method: 'PATCH' });
        await _refreshAfterEndpointChange();
        loadEndpoints();
      });
    });
    queryAll('[data-adm-ep-kind]').forEach(sel => {
      const previous = sel.value;
      // The row header toggles the models panel; a click on the select must
      // not also expand/collapse it.
      sel.addEventListener('click', (e) => e.stopPropagation());
      sel.addEventListener('change', async (e) => {
        e.stopPropagation();
        const next = sel.value;
        sel.disabled = true;
        try {
          const res = await fetch(`/api/model-endpoints/${sel.dataset.admEpKind}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify({ endpoint_kind: next }),
          });
          if (!res.ok) throw new Error('save failed');
          await _refreshAfterEndpointChange();
          loadEndpoints();
        } catch (err) {
          sel.value = previous;
          sel.disabled = false;
        }
      });
    });
    queryAll('[data-adm-copy-url]').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const url = btn.dataset.admCopyUrl || '';
        if (!url) return;
        uiModule.copyToClipboard(url).then(() => {
          // Brief icon swap to a checkmark so the user gets feedback that
          // the copy actually happened. Reverts after ~1.4s.
          const prev = btn.innerHTML;
          btn.innerHTML = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
          btn.style.opacity = '1';
          setTimeout(() => { btn.innerHTML = prev; btn.style.opacity = ''; }, 1400);
        }).catch(() => {});
      });
    });
    queryAll('[data-adm-del-ep]').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        var epId = btn.dataset.admDelEp;
        var isOffline = btn.dataset.admEpOnline === '0';
        // Offline endpoints are already broken — skip the confirm dialog
        // entirely and delete immediately. The optimistic UI removal makes
        // the action feel instant.
        if (!isOffline) {
          var deps = [];
          try {
            var depRes = await fetch('/api/model-endpoints/' + epId + '/dependents', { credentials: 'same-origin' });
            var depData = await depRes.json();
            deps = depData.dependents || [];
          } catch (e) { /* proceed without warning */ }
          var msg = 'Delete this endpoint?';
          if (deps.length) {
            msg += '\n\nThe following settings use this endpoint and will be reset:\n— ' + deps.join('\n— ');
          }
          if (!await uiModule.styledConfirm(msg, { confirmText: 'Delete', danger: true })) return;
        }
        // Optimistic: remove from UI immediately
        const row = btn.closest('[data-adm-ep-id]');
        if (row) row.remove();
        fetch('/api/model-endpoints/' + epId, { method: 'DELETE' })
          .then(() => _refreshAfterEndpointChange(epId))
          .then(() => loadEndpoints())
          .catch(() => loadEndpoints());
      });
    });
    // Clear the just-added marker now that the row has been rendered
    // with the animation class — keeps the glow from re-firing on every
    // subsequent loadEndpoints() call (e.g. when toggling a model).
    if (_recentlyAddedEpId) _recentlyAddedEpId = null;
    // Models expand/collapse (click anywhere on card)
    queryAll('[data-adm-ep-id]').forEach(row => {
      const header = row.querySelector('[data-adm-ep-header]');
      if (!header) return;
      let _modelsLoaded = false;
      row.style.cursor = 'pointer';
      row.addEventListener('click', async (e) => {
        // Don't let interactions inside the expanded panel re-fire the
        // expand/collapse handler — the search box was getting closed
        // because clicking it bubbled up to here.
        if (e.target.closest('.admin-btn-sm, .admin-btn-delete, .mcp-tools-list, .mcp-tools-header, .mcp-tools-search, .admin-ep-advanced, input, label, select')) return;
        const epId = header.dataset.admEpHeader;
        const panel = row.querySelector(`[data-adm-ep-models-panel="${epId}"]`);
        if (!panel) return;
        panel.classList.toggle('hidden');
        const chevron = row.querySelector('.admin-user-chevron');
        const isOpen = !panel.classList.contains('hidden');
        if (chevron) {
          chevron.style.transform = isOpen ? 'rotate(180deg)' : '';
          chevron.style.opacity = isOpen ? '0.7' : '0.3';
        }
        if (!_modelsLoaded && isOpen) {
          _modelsLoaded = true;
          // Our shared whirlpool spinner (consistent with the rest of the app).
          panel.innerHTML = '';
          let _modelsSpin = null;
          const _ld = document.createElement('span');
          _ld.style.cssText = 'opacity:0.55;font-size:11px;display:inline-flex;align-items:center;gap:8px;';
          _ld.appendChild(document.createTextNode('Loading models…'));
          try {
            const _sp = (await import('./spinner.js')).default;
            _modelsSpin = _sp.createWhirlpool(14);
            _modelsSpin.element.style.cssText = 'width:14px;height:14px;margin:0;display:inline-block;';
            _ld.appendChild(_modelsSpin.element);
          } catch (_) {}
          panel.appendChild(_ld);
          const _stopSpin = () => { try { _modelsSpin && _modelsSpin.stop(); } catch (_) {} };
          const _loadingHtml = (label) => `<span style="opacity:0.55;font-size:11px;display:inline-flex;align-items:center;gap:8px;">${esc(label)}</span>`;
          const renderModels = (models, warning = '') => {
            const sortedModels = sortModelObjects(models);
            const usesPinnedPicker = sortedModels.some(m => !!m.picker_requires_pinning);
            panel.dataset.pickerMode = usesPinnedPicker ? 'pinned' : 'hidden';
            const warningHtml = warning ? `<div class="admin-error" style="font-size:11px;margin:6px 0;">${esc(warning)}</div>` : '';
            const attachRefresh = () => {
              panel.querySelector(`[data-ep-refresh-models="${epId}"]`)?.addEventListener('click', async (e) => {
                e.preventDefault();
                panel.innerHTML = _loadingHtml('Refreshing models...');
                try {
                  const res = await fetch(`/api/model-endpoints/${epId}/models?refresh=true&refresh_timeout=60`, { credentials: 'same-origin' });
                  const refreshWarning = res.headers.get('X-Model-Refresh-Warning') || '';
                  if (!res.ok) throw new Error(`HTTP ${res.status}`);
                  const refreshedModels = await res.json();
                  renderModels(refreshedModels, refreshWarning);
                  _refreshAfterEndpointChange();
                  if (refreshWarning && uiModule?.showToast) uiModule.showToast(refreshWarning, 6000);
                } catch (_) {
                  renderModels(sortedModels, 'Model refresh failed; kept cached models.');
                }
              });
            };
            if (!sortedModels.length) {
              panel.innerHTML = `<div class="mcp-tools-header">
                <span>Models</span>
                <span style="display:flex;gap:8px;align-items:center;">
                  <a href="#" data-ep-refresh-models="${epId}">Refresh</a>
                </span>
              </div>${warningHtml}<span style="opacity:0.5;font-size:11px;">No models</span>`;
              attachRefresh();
              return;
            }
            const enabledCount = usesPinnedPicker
              ? sortedModels.filter(m => m.is_pinned).length
              : sortedModels.filter(m => !m.is_hidden).length;
            const showSearch = sortedModels.length >= 8;
            panel.innerHTML = `<div class="mcp-tools-header">
              <span>Models</span>
              <span style="display:flex;gap:8px;align-items:center;">
                <a href="#" data-ep-refresh-models="${epId}">Refresh</a>
                <a href="#" data-ep-select-all="${epId}">All</a>
                <a href="#" data-ep-select-none="${epId}">None</a>
              </span>
            </div>${warningHtml}${showSearch ? `<input type="search" class="mcp-tools-search" placeholder="Search ${sortedModels.length} models..." data-ep-search="${epId}">` : ''}<div class="mcp-tools-list">` + sortedModels.map(m =>
              `<label title="${esc(m.id)}" data-ep-model-row data-search="${esc((m.display + ' ' + m.id).toLowerCase())}" class="adm-model-row">
                <input type="checkbox" class="adm-cb-hidden" data-ep-model-id="${esc(m.id)}" ${(usesPinnedPicker ? m.is_pinned : !m.is_hidden) ? 'checked' : ''}>
                <span class="adm-check-dot" aria-hidden="true"></span>
                <span>${esc(m.display)}</span>
              </label>`
            ).join('') + '</div>';
            const filterRows = (q) => {
              const needle = q.trim().toLowerCase();
              panel.querySelectorAll('[data-ep-model-row]').forEach(row => {
                row.style.display = (!needle || row.dataset.search.includes(needle)) ? '' : 'none';
              });
            };
            attachRefresh();
            panel.querySelector(`[data-ep-search="${epId}"]`)?.addEventListener('input', (e) => filterRows(e.target.value));
            panel.querySelector(`[data-ep-select-all="${epId}"]`)?.addEventListener('click', (e) => {
              e.preventDefault();
              panel.querySelectorAll('[data-ep-model-row]').forEach(row => {
                if (row.style.display !== 'none') row.querySelector('input[type=checkbox]').checked = true;
              });
              _saveEpModelState(epId, panel);
            });
            panel.querySelector(`[data-ep-select-none="${epId}"]`)?.addEventListener('click', (e) => {
              e.preventDefault();
              panel.querySelectorAll('[data-ep-model-row]').forEach(row => {
                if (row.style.display !== 'none') row.querySelector('input[type=checkbox]').checked = false;
              });
              _saveEpModelState(epId, panel);
            });
            panel.querySelectorAll('input[type=checkbox]').forEach(cb => {
              cb.addEventListener('change', () => _saveEpModelState(epId, panel));
            });
          };
          try {
            const res = await fetch(`/api/model-endpoints/${epId}/models`, { credentials: 'same-origin' });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const models = await res.json();
            _stopSpin();
            renderModels(models);
          } catch (e) { _stopSpin(); panel.innerHTML = '<span class="admin-error" style="font-size:11px;">Failed to load models</span>'; }
        }
      });
    });
    refreshDependentModelUi(false, fetched);
  } catch (e) {
    const err = '<div class="admin-error">Failed to load</div>';
    [listLocal, listApi].forEach(c => { if (c) c.innerHTML = err; });
  }
}

async function _saveEpModelState(epId, panel) {
  const hidden = [];
  const pinned = [];
  const usesPinnedPicker = panel && panel.dataset && panel.dataset.pickerMode === 'pinned';
  panel.querySelectorAll('input[type=checkbox]').forEach(cb => {
    if (cb.checked) pinned.push(cb.dataset.epModelId);
    else hidden.push(cb.dataset.epModelId);
  });
  const total = panel.querySelectorAll('input[type=checkbox]').length;
  const enabled = usesPinnedPicker ? pinned.length : total - hidden.length;
  try {
    await fetch(`/api/model-endpoints/${epId}/models`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify(usesPinnedPicker ? { pinned_models: pinned } : { hidden }),
    });
    const row = panel.closest('[data-adm-ep-id]');
    if (row) {
      const badge = row.querySelector('.admin-badge');
      if (badge && !badge.classList.contains('admin-badge-off')) {
        const match = String(badge.textContent || '').match(/\/(\d+)/);
        const canonicalTotal = match ? Number(match[1]) : total;
        badge.textContent = `${enabled}/${canonicalTotal} models enabled`;
      }
    }
    if (settingsModule && typeof settingsModule.refreshAiModelEndpoints === 'function') {
      settingsModule.refreshAiModelEndpoints();
    }
    _refreshAfterEndpointChange();
  } catch (e) { /* silent */ }
}

function initEndpointForm() {
  const provider = el('adm-epProvider');
  const urlInput = el('adm-epUrl');
  const kindSel = el('adm-epKind');

  // Custom provider picker — mirrors the (now hidden) <select id="adm-epProvider">
  // so the rest of this function (which reads provider.value and dispatches
  // change events) keeps working unchanged.
  const picker = el('adm-provider-picker');
  const pickerBtn = el('adm-provider-btn');
  const pickerMenu = el('adm-provider-menu');
  const pickerCurrent = picker ? picker.querySelector('.adm-provider-current') : null;
  const DEVICE_AUTH_PROVIDER_VALUES = new Set(Object.keys(PROVIDER_DEVICE_FLOWS));
  let deviceAuthPolling = false;
  function _selectedProviderOption() {
    return provider && provider.selectedOptions ? provider.selectedOptions[0] : null;
  }
  function _selectedDeviceAuthProvider() {
    const opt = _selectedProviderOption();
    const flow = opt && opt.dataset ? opt.dataset.authFlow : '';
    if (flow && DEVICE_AUTH_PROVIDER_VALUES.has(flow)) return flow;
    return DEVICE_AUTH_PROVIDER_VALUES.has(provider.value) ? provider.value : '';
  }
  function _isDeviceAuthSelected() {
    return !!_selectedDeviceAuthProvider();
  }
  function _setApiFormForProvider() {
    const deviceAuthProvider = _selectedDeviceAuthProvider();
    const deviceAuthConfig = PROVIDER_DEVICE_FLOWS[deviceAuthProvider] || null;
    const apiKey = el('adm-epApiKey');
    const testBtn = el('adm-epApiTestBtn');
    const addBtn = el('adm-epAddBtn');
    const status = el('adm-deviceAuthStatus');
    const msg = _endpointMsg('api');
    if (deviceAuthConfig) {
      urlInput.value = '';
      urlInput.placeholder = deviceAuthProvider === 'copilot'
        ? 'GitHub Copilot uses GitHub account sign-in'
        : 'ChatGPT Subscription uses OpenAI account sign-in';
      urlInput.readOnly = true;
      if (apiKey) {
        apiKey.value = '';
        apiKey.placeholder = 'No API key needed';
        apiKey.disabled = true;
      }
      if (testBtn) {
        testBtn.disabled = true;
        testBtn.style.opacity = '0.45';
        testBtn.style.cursor = 'not-allowed';
      }
      if (addBtn) {
        addBtn.disabled = false;
        addBtn.textContent = 'Add';
        addBtn.style.width = '55px';
        addBtn.style.display = '';
      }
      if (kindSel) kindSel.value = 'api';
      if (msg) {
        msg.textContent = '';
        msg.className = '';
      }
    } else {
      urlInput.placeholder = 'Base URL or pick provider';
      urlInput.readOnly = false;
      if (apiKey) {
        apiKey.placeholder = 'API key';
        apiKey.disabled = false;
      }
      if (testBtn) {
        testBtn.disabled = false;
        testBtn.style.opacity = '';
        testBtn.style.cursor = '';
      }
      if (addBtn) {
        addBtn.disabled = false;
        addBtn.textContent = 'Add';
        addBtn.style.width = '55px';
        addBtn.style.display = '';
      }
      if (msg) {
        msg.textContent = '';
        msg.className = '';
      }
      if (!deviceAuthPolling && status) status.textContent = '';
    }
  }
  function _renderPickerMenu() {
    if (!pickerMenu) return;
    pickerMenu.innerHTML = Array.from(provider.options).map(o => {
      const logo = o.dataset.logo ? (providerLogo(o.dataset.logo) || '') : '';
      const active = o.value === provider.value ? ' active' : '';
      return `<div class="adm-provider-item${active}" role="option" data-value="${o.value.replace(/"/g, '&quot;')}">
        <span class="adm-provider-logo">${logo}</span>
        <span>${o.textContent}</span>
      </div>`;
    }).join('');
  }
  function _syncPickerCurrent() {
    if (!pickerCurrent) return;
    const opt = provider.selectedOptions[0] || provider.options[0];
    const logo = opt.dataset.logo ? (providerLogo(opt.dataset.logo) || '') : '';
    pickerCurrent.querySelector('.adm-provider-logo').innerHTML = logo;
    // Nothing is preselected: until the user picks, say so instead of showing
    // "Custom URL" as if it were a choice they made.
    const chosen = provider.value || (picker && picker.dataset.chosen === '1');
    pickerCurrent.querySelector('.adm-provider-name').textContent = chosen ? opt.textContent : 'Choose provider';
  }
  if (picker && pickerBtn && pickerMenu && pickerCurrent) {
    _renderPickerMenu();
    _syncPickerCurrent();
    if (provider.value && !urlInput.value) urlInput.value = provider.value;
    pickerBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      pickerMenu.classList.toggle('hidden');
    });
    pickerMenu.addEventListener('click', (e) => {
      const item = e.target.closest('.adm-provider-item');
      if (!item) return;
      picker.dataset.chosen = '1';
      provider.value = item.dataset.value;
      provider.dispatchEvent(new Event('change', { bubbles: true }));
      pickerMenu.classList.add('hidden');
      _renderPickerMenu();
      _syncPickerCurrent();
    });
    document.addEventListener('click', (e) => {
      if (!picker.contains(e.target)) pickerMenu.classList.add('hidden');
    });
    // Capture-phase Esc: dismiss the picker menu without bubbling to the
    // settings-modal handler that would otherwise close the whole modal.
    document.addEventListener('keydown', (e) => {
      if (e.key !== 'Escape') return;
      if (pickerMenu.classList.contains('hidden')) return;
      e.stopPropagation();
      pickerMenu.classList.add('hidden');
    }, { capture: true });
  }

  provider.addEventListener('change', () => {
    if (_isDeviceAuthSelected()) {
      _setApiFormForProvider();
      _renderPickerMenu();
      _syncPickerCurrent();
      return;
    }
    if (provider.value) urlInput.value = provider.value;
    else urlInput.value = '';
    if (kindSel) kindSel.value = provider.value ? 'api' : 'proxy';
    _setApiFormForProvider();
  });
  urlInput.addEventListener('input', () => {
    if (provider.value && urlInput.value.trim() !== provider.value) {
      provider.value = '';
      if (kindSel) kindSel.value = 'api';
      _renderPickerMenu();
      _syncPickerCurrent();
    }
  });
  if (kindSel) kindSel.value = kindSel.value || 'api';
  function _apiEndpointKind() {
    return (kindSel && kindSel.value) ? kindSel.value : 'api';
  }
  function _modelRefreshModeForApiEndpoint(url, endpointKind) {
    if (endpointKind === 'proxy') return 'manual';
    try {
      if ((new URL(url)).hostname.toLowerCase() === 'generativelanguage.googleapis.com') return '';
    } catch (_) {}
    return 'auto';
  }
  function _normalizeBaseUrl(raw) {
    let u = raw.trim();
    // Fix common protocol typos
    u = u.replace(/^https?:\/(?!\/)/, m => m + '/');  // https:/ → https://
    u = u.replace(/^htp:/, 'http:').replace(/^htps:/, 'https:');
    u = u.replace(/^http:\/\/\//, 'http://');  // http:/// → http://
    u = u.replace(/^https:\/\/\//, 'https://');
    // Add http:// if no protocol
    if (!/^https?:\/\//.test(u)) u = 'http://' + u;
    // Strip trailing slashes
    u = u.replace(/\/+$/, '');
    // Strip trailing paths that shouldn't be in a base URL
    u = u.replace(/\/v1\/(models|chat\/completions|completions|messages)\/?$/i, '/v1');
    u = u.replace(/\/(models|chat\/completions|completions|v1\/messages)\/?$/i, '');
    u = u.replace(/\/api\/(chat|tags|generate)\/?$/i, '/api');
    // Fix double /v1/v1
    u = u.replace(/\/v1\/v1$/, '/v1');
    // Strip query params and fragments
    u = u.split('?')[0].split('#')[0];
    try {
      const parsed = new URL(u);
      if (parsed.hostname.endsWith('ollama.com')) {
        u = 'https://ollama.com/api';
      }
    } catch(e) {}
    // Ensure /v1 suffix for bare host:port URLs (not cloud providers)
    if (!u.includes('api.') && !u.includes('openrouter') && !u.includes('opencode.ai') && !u.includes('ollama.com') && !u.endsWith('/v1')) {
      try {
        const parsed = new URL(u);
        if (!parsed.pathname || parsed.pathname === '/') {
          u += '/v1';
        }
      } catch(e) {}
    }
    return u;
  }

  async function _defaultOllamaUrl() {
    try {
      const res = await fetch('/api/runtime', { credentials: 'same-origin' });
      if (res.ok) {
        const data = await res.json();
        if (data && data.ollama_base_url) return data.ollama_base_url;
      }
    } catch (_) {}
    return 'http://127.0.0.1:11434/v1';
  }

  function _renderEndpointTestResult(msg, res, d) {
    if (res.ok && d.status === 'empty') {
      msg.textContent = 'Online — no models found';
      msg.className = 'admin-success';
      return;
    }
    if (res.ok && d.online) {
      const models = d.models || [];
      const preview = models.slice(0, 3).map(m => esc(String(m).split('/').pop())).join(', ');
      msg.innerHTML = `Online — found ${models.length} model${models.length !== 1 ? 's' : ''}${preview ? `: ${preview}${models.length > 3 ? ', …' : ''}` : ''}`;
      msg.className = 'admin-success';
      return;
    }
    msg.textContent = (d && d.detail) || (d && d.ping_error ? `Offline — ${d.ping_error}` : 'Offline');
    msg.className = 'admin-error';
  }

  function _endpointMsg(kind) {
    return el(kind === 'local' ? 'adm-epLocalMsg' : 'adm-epApiMsg');
  }

  let apiTestController = null;
  const apiTestBtn = el('adm-epApiTestBtn');
  const apiCancelTestBtn = el('adm-epApiCancelTestBtn');
  if (apiTestBtn) {
    apiTestBtn.addEventListener('click', async () => {
      if (_isDeviceAuthSelected()) {
        const msg = _endpointMsg('api');
        msg.textContent = '';
        msg.className = '';
        return;
      }
      const msg = _endpointMsg('api');
      msg.textContent = ''; msg.className = '';
      const rawUrl = (urlInput.value || provider.value).trim();
      const apiKey = el('adm-epApiKey').value.trim();
      if (!rawUrl) { msg.textContent = 'Select a provider or enter a base URL'; msg.className = 'admin-error'; return; }
      if (provider.value && !apiKey) { msg.textContent = 'API key is required for cloud providers'; msg.className = 'admin-error'; return; }
      const url = provider.value && rawUrl === provider.value ? rawUrl : _normalizeBaseUrl(rawUrl);
      apiTestController = new AbortController();
      apiTestBtn.disabled = true;
      apiTestBtn.textContent = 'Testing...';
      if (apiCancelTestBtn) apiCancelTestBtn.classList.remove('hidden');
      try {
        const fd = new FormData();
        fd.append('base_url', url);
        fd.append('endpoint_kind', _apiEndpointKind());
        fd.append('model_refresh_timeout', '30');
        if (apiKey) fd.append('api_key', apiKey);
        const res = await fetch('/api/model-endpoints/test', {
          method: 'POST',
          body: fd,
          credentials: 'same-origin',
          signal: apiTestController.signal,
        });
        const d = await res.json();
        _renderEndpointTestResult(msg, res, d);
      } catch (e) {
        if (e && e.name === 'AbortError') {
          msg.textContent = 'Test canceled';
          msg.className = '';
        } else {
          msg.textContent = 'Test failed: ' + (e && e.message ? e.message : 'request failed');
          msg.className = 'admin-error';
        }
      }
      apiTestController = null;
      apiTestBtn.disabled = false;
      apiTestBtn.textContent = 'Test';
      if (apiCancelTestBtn) apiCancelTestBtn.classList.add('hidden');
    });
  }
  if (apiCancelTestBtn) {
    apiCancelTestBtn.addEventListener('click', () => {
      if (apiTestController) apiTestController.abort();
    });
  }

  el('adm-epAddBtn').addEventListener('click', async () => {
    const deviceAuthProvider = _selectedDeviceAuthProvider();
    if (deviceAuthProvider) {
      await _startProviderDeviceAuth(deviceAuthProvider, el('adm-epAddBtn'));
      return;
    }
    const msg = _endpointMsg('api');
    msg.textContent = ''; msg.className = '';
    const rawUrl = (urlInput.value || provider.value).trim();
    const apiKey = el('adm-epApiKey').value.trim();
    if (!rawUrl) { msg.textContent = 'Select a provider or enter a base URL'; msg.className = 'admin-error'; return; }
    if (provider.value && !apiKey) { msg.textContent = 'API key is required for cloud providers'; msg.className = 'admin-error'; return; }
    // Normalize URL (fix typos, add /v1, strip wrong paths)
    const url = provider.value && rawUrl === provider.value ? rawUrl : _normalizeBaseUrl(rawUrl);
    const btn = el('adm-epAddBtn');
    btn.disabled = true; btn.textContent = 'Adding...';
    try {
      const fd = new FormData();
      fd.append('base_url', url);
      const endpointKind = _apiEndpointKind();
      fd.append('endpoint_kind', endpointKind);
      const refreshMode = _modelRefreshModeForApiEndpoint(url, endpointKind);
      if (refreshMode) fd.append('model_refresh_mode', refreshMode);
      fd.append('model_refresh_timeout', '30');
      if (apiKey) fd.append('api_key', apiKey);
      if (provider.value && provider.selectedOptions && provider.selectedOptions[0]) {
        fd.append('name', provider.selectedOptions[0].textContent.trim());
      }
      if (provider.value && /openrouter\.ai|ollama\.com/i.test(provider.value)) fd.append('require_models', 'true');
      else fd.append('skip_probe', 'false');
      const res = await fetch('/api/model-endpoints', { method: 'POST', body: fd, credentials: 'same-origin' });
      const d = await res.json();
      if (res.ok) {
        const count = d.models ? d.models.length : 0;
        urlInput.value = ''; urlInput.style.display = '';
        el('adm-epApiKey').value = ''; provider.value = '';
        if (picker) delete picker.dataset.chosen;
        _renderPickerMenu();
        _syncPickerCurrent();
        if (kindSel) kindSel.value = 'proxy';
        if (d.id) _recentlyAddedEpId = String(d.id);
        await loadEndpoints();
        await _selectAddedModelInChat(d);
        // The new row is in the list right below and glows once.
        if (!d.online) {
          msg.textContent = 'Added (provider offline — will retry on next load)';
          msg.className = 'admin-error';
        } else if (d.status === 'empty') {
          msg.textContent = 'Added — provider reachable, no models found';
          msg.className = 'admin-success';
        } else {
          msg.textContent = `Added — found ${count} model${count !== 1 ? 's' : ''}`;
          msg.className = 'admin-success';
        }
      } else { msg.textContent = d.detail || 'Failed'; msg.className = 'admin-error'; }
    } catch (e) { msg.textContent = 'Request failed'; msg.className = 'admin-error'; }
    btn.disabled = false; btn.textContent = 'Add';
  });

  async function _startProviderDeviceAuth(providerKey, triggerEl = null) {
    if (deviceAuthPolling) return;
    const config = PROVIDER_DEVICE_FLOWS[providerKey];
    if (!config) return;
    const status = el('adm-deviceAuthStatus') || _endpointMsg('api');
    if (!status) return;
    const triggerText = triggerEl ? triggerEl.textContent : '';
    // Render an error with an inline "Try again" (the top button is hidden for
    // device-auth providers, so retry lives here). Built with DOM methods, not
    // innerHTML. Call reset() first so the deviceAuthPolling guard is cleared.
    const showAuthError = (text) => {
      status.className = 'admin-error';
      status.textContent = text + ' ';
      const retry = document.createElement('button');
      retry.type = 'button';
      retry.className = 'admin-btn-sm';
      retry.textContent = 'Try again';
      retry.addEventListener('click', () => { _startProviderDeviceAuth(providerKey, triggerEl); });
      status.appendChild(retry);
    };
    const reset = () => {
      if (triggerEl) {
        triggerEl.disabled = false;
        triggerEl.textContent = triggerText || 'Add';
      }
      deviceAuthPolling = false;
      _setApiFormForProvider();
    };
    status.textContent = '';
    status.className = 'adm-ep-inline-msg';
    if (triggerEl) {
      triggerEl.disabled = true;
      triggerEl.textContent = 'Starting...';
    }
    deviceAuthPolling = true;
    _setApiFormForProvider();
    status.textContent = `Starting ${config.label} sign-in...`;

    try {
      const result = await runProviderDeviceFlow(providerKey, {
        openWindow: () => {},
        onStart: ({ start, authUrl }) => {
          if (triggerEl) triggerEl.textContent = 'Waiting...';
          status.className = '';
          const authLabel = providerKey === 'copilot' ? 'Authorize on GitHub' : 'Authorize with OpenAI';
          const waitLabel = providerKey === 'copilot' ? 'Waiting for GitHub authorization...' : 'Waiting for ChatGPT authorization...';
          status.innerHTML =
            '<div class="adm-copilot-panel">' +
              '<div class="adm-copilot-wait"><span class="admin-spinner"></span>' +
                '<span>' + esc(waitLabel) + '</span></div>' +
              '<div class="adm-copilot-coderow">' +
                '<span class="adm-copilot-code-label">Code</span>' +
                '<code class="adm-copilot-code">' + esc(start.user_code) + '</code>' +
                '<button type="button" class="admin-btn-sm adm-device-auth-copy">Copy</button>' +
              '</div>' +
              '<a class="admin-btn-add adm-copilot-auth" href="' + encodeURI(authUrl || '') + '" target="_blank" rel="noopener">' + esc(authLabel) + ' ↗</a>' +
            '</div>';
          const copyBtn = status.querySelector('.adm-device-auth-copy');
          if (copyBtn) copyBtn.addEventListener('click', async () => {
            const code = start.user_code || '';
            let ok = false;
            try {
              if (navigator.clipboard && window.isSecureContext) {
                await navigator.clipboard.writeText(code);
                ok = true;
              }
            } catch (e) {}
            if (!ok) {
              // navigator.clipboard is unavailable in non-secure contexts (HTTP
              // self-host over a LAN IP), so fall back to execCommand('copy').
              const ta = document.createElement('textarea');
              ta.value = code;
              ta.style.cssText = 'position:fixed;top:0;left:0;width:1px;height:1px;padding:0;border:0;opacity:0;font-size:16px;';
              document.body.appendChild(ta);
              ta.focus();
              ta.select();
              try { ta.setSelectionRange(0, code.length); } catch (e) {}
              try { ok = document.execCommand('copy'); } catch (e) {}
              ta.remove();
            }
            copyBtn.textContent = ok ? 'Copied' : 'Failed';
            setTimeout(() => { copyBtn.textContent = 'Copy'; }, 1500);
          });
        },
      });
      if (result.status === 'authorized') {
        const endpoint = result.endpoint || {};
        const n = ((endpoint && endpoint.models) || []).length;
        status.className = 'admin-success';
        status.textContent = 'Connected - ' + n + ' ' + config.label + ' model' + (n !== 1 ? 's' : '') + ' available.';
        if (endpoint && endpoint.id) _recentlyAddedEpId = String(endpoint.id);
        await loadEndpoints();
        await _selectAddedModelInChat(endpoint || {});
        reset();
        return;
      }
      if (result.status === 'failed') {
        reset();
        showAuthError('Authorization failed (' + (result.error || 'denied') + ').');
        return;
      }
      if (result.status === 'expired') {
        reset();
        showAuthError('Authorization expired.');
        return;
      }
    } catch (e) {
      reset();
      showAuthError(formatDeviceFlowError(e));
    }
  }

  // API Key reveal toggle. The key inputs are hidden by default so the Add
  // form reads as a single action row; the Key button toggles the input row
  // and flips aria-expanded for screen readers / CSS pseudo-classes.
  const _wireKeyToggle = (btnId, rowId) => {
    const btn = el(btnId);
    const row = el(rowId);
    if (!btn || !row) return;
    btn.addEventListener('click', () => {
      const showing = row.style.display !== 'none';
      row.style.display = showing ? 'none' : '';
      btn.setAttribute('aria-expanded', showing ? 'false' : 'true');
      btn.style.opacity = showing ? '0.75' : '1';
      if (!showing) {
        const inp = row.querySelector('input');
        if (inp) inp.focus();
      }
    });
  };
  _wireKeyToggle('adm-epLocalKeyBtn', 'adm-epLocalApiKey-row');

  // Delegated link handler for jumping between settings tabs.
  //   [data-go-added-models]              → the Providers list on the Models tab
  //   [data-go-settings-tab="X"]          → the tab X, or the tab that owns the retired id X
  //   [data-go-scroll-to="#elementId"]    → after switching, scroll the element into view
  document.addEventListener('click', (e) => {
    const explicit = e.target.closest('[data-go-settings-tab]');
    if (explicit) {
      e.preventDefault();
      const tab = resolveSettingsPanelId(explicit.getAttribute('data-go-settings-tab'));
      const scrollTo = explicit.getAttribute('data-go-scroll-to');
      const btn = document.querySelector(`[data-settings-tab="${tab}"]`);
      if (btn) btn.click();
      if (scrollTo) {
        // Defer to the next frame so the panel has actually become visible
        // before we try to scroll into it.
        requestAnimationFrame(() => {
          const target = document.querySelector(scrollTo);
          if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
        });
      }
      return;
    }
    const link = e.target.closest('[data-go-added-models]');
    if (!link) return;
    e.preventDefault();
    const btn = document.querySelector('[data-settings-tab="models"]');
    if (btn) btn.click();
    requestAnimationFrame(() => el('set-providersCard')?.scrollIntoView({ behavior: 'smooth', block: 'start' }));
  });

  // "Add provider" folds the add form (cloud first, local servers in a
  // collapsed section) in and out of the Providers card.
  const addToggle = el('adm-addProviderToggle');
  if (addToggle && !addToggle.dataset.bound) {
    addToggle.dataset.bound = '1';
    addToggle.addEventListener('click', () => {
      const form = el('adm-addProvider');
      const open = !!(form && form.hidden);
      _setAddProviderOpen(open);
      if (open) {
        const first = el('adm-provider-btn');
        if (first) first.focus();
      }
    });
  }

  // ── Added Models toolbar: Probe + Clear offline ────────────────────
  // Both buttons act over the currently-rendered endpoint list. The
  // online/offline marker is stamped on each row's [data-adm-ep-online]
  // attribute by loadEndpoints(), so both buttons just iterate the DOM
  // without re-fetching anything they don't already have.
  const _refreshOfflineCount = () => {
    const lbl = el('adm-epOfflineCount');
    if (!lbl) return;
    const n = document.querySelectorAll('[data-adm-ep-id] [data-adm-ep-online="0"]').length;
    lbl.textContent = n > 0 ? `(${n})` : '';
    // Hide the button entirely when there's nothing offline — no point
    // showing an action that has nothing to act on.
    const btn = el('adm-epClearOfflineBtn');
    if (btn) btn.style.display = n === 0 ? 'none' : '';
  };
  // Wire after every loadEndpoints() run by patching the render hook —
  // simplest path: MutationObserver on the two list containers.
  const _obsRoots = ['adm-epList-local', 'adm-epList-api']
    .map(id => el(id)).filter(Boolean);
  if (_obsRoots.length) {
    const mo = new MutationObserver(_refreshOfflineCount);
    _obsRoots.forEach(r => mo.observe(r, { childList: true, subtree: true }));
    _refreshOfflineCount();
  }

  const _fetchWithTimeout = async (url, opts = {}, timeoutMs = 25000) => {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
      return await fetch(url, { ...opts, signal: ctrl.signal });
    } finally {
      clearTimeout(timer);
    }
  };
  const _collectAddedEndpointIds = async () => {
    const domIds = Array.from(document.querySelectorAll('[data-adm-ep-id]'))
      .map(r => r.getAttribute('data-adm-ep-id'))
      .filter(Boolean);
    if (domIds.length) return Array.from(new Set(domIds));
    try {
      const res = await fetch('/api/model-endpoints', { credentials: 'same-origin' });
      const data = await res.json().catch(() => []);
      return (Array.isArray(data) ? data : []).map(ep => ep && ep.id).filter(Boolean);
    } catch (_) {
      return [];
    }
  };
  const _setProbeAllButtonLabel = async (btn, text, whirlpoolRef) => {
    btn.innerHTML = '';
    if (whirlpoolRef && whirlpoolRef.element) btn.appendChild(whirlpoolRef.element);
    btn.appendChild(document.createTextNode(text));
  };
  if (!window.__admEpProbeAllWired) {
    window.__admEpProbeAllWired = true;
    document.addEventListener('click', async (ev) => {
      const probeAllBtn = ev.target.closest('#adm-epProbeAllBtn');
      if (!probeAllBtn || probeAllBtn.disabled) return;
      ev.preventDefault();
      probeAllBtn.disabled = true;
      const origHTML = probeAllBtn.innerHTML;
      let _wp = null;
      try {
        try {
          const sp = window.spinnerModule || (await import('./spinner.js')).default;
          _wp = sp.createWhirlpool(11);
          _wp.element.style.cssText = 'display:inline-flex;width:11px;height:11px;margin:0 4px 0 0;';
          await _setProbeAllButtonLabel(probeAllBtn, 'Probing', _wp);
        } catch (_) {
          probeAllBtn.innerHTML = '<span style="opacity:0.7;">Probing...</span>';
        }
        await _fetchWithTimeout('/api/model-endpoints/probe-local', { credentials: 'same-origin' }, 12000).catch(() => null);
        const ids = await _collectAddedEndpointIds();
        if (!ids.length) {
          await loadEndpoints();
          if (uiModule && uiModule.showToast) uiModule.showToast('No endpoints to probe', 1800);
          return;
        }
        let done = 0;
        let failed = 0;
        const lane = async (id) => {
          try {
            const res = await _fetchWithTimeout(`/api/model-endpoints/${encodeURIComponent(id)}/models?refresh=true&refresh_timeout=20`, {
              credentials: 'same-origin'
            }, 25000);
            if (!res || !res.ok || res.headers.get('X-Model-Refresh-Status') === 'failed') failed += 1;
            else await res.json().catch(() => null);
          } catch (err) {
            failed += 1;
            console.warn('Endpoint probe failed', id, err);
          } finally {
            done += 1;
            try { await _setProbeAllButtonLabel(probeAllBtn, `Probing ${done}/${ids.length}`, _wp); } catch (_) {}
          }
        };
        const queue = [...ids];
        const workers = Array.from({ length: Math.min(4, queue.length) }, () => (async () => {
          while (queue.length) {
            const id = queue.shift();
            if (id) await lane(id);
          }
        })());
        await Promise.all(workers);
        await loadEndpoints();
        await _refreshAfterEndpointChange();
        _refreshOfflineCount();
        if (uiModule && uiModule.showToast) {
          const ok = Math.max(0, ids.length - failed);
          uiModule.showToast(failed ? `Probed ${ok}/${ids.length} endpoints; ${failed} failed` : `Probed ${ids.length} endpoints`, failed ? 4200 : 1800);
        }
      } finally {
        if (_wp) { try { _wp.destroy(); } catch (_) {} }
        probeAllBtn.innerHTML = origHTML;
        probeAllBtn.disabled = false;
      }
    });
  }

  const clearOfflineBtn = el('adm-epClearOfflineBtn');
  if (clearOfflineBtn) {
    clearOfflineBtn.addEventListener('click', async () => {
      const offlineBtns = Array.from(document.querySelectorAll('[data-adm-del-ep][data-adm-ep-online="0"]'));
      const ids = offlineBtns.map(b => b.getAttribute('data-adm-del-ep')).filter(Boolean);
      if (!ids.length) {
        if (uiModule && uiModule.showToast) {
          uiModule.showToast('No offline endpoints — nothing to clear', 1800);
        }
        return;
      }
      const confirmMsg = ids.length === 1
        ? 'Remove 1 offline endpoint?'
        : `Remove ${ids.length} offline endpoints?`;
      if (uiModule && uiModule.styledConfirm) {
        const ok = await uiModule.styledConfirm(confirmMsg, { confirmText: 'Remove', danger: true });
        if (!ok) return;
      } else if (!confirm(confirmMsg)) {
        return;
      }
      clearOfflineBtn.disabled = true;
      // Optimistic UI: pull rows immediately, then fire the DELETEs.
      offlineBtns.forEach(b => {
        const row = b.closest('[data-adm-ep-id]');
        if (row) row.remove();
      });
      await Promise.all(ids.map(id =>
        fetch('/api/model-endpoints/' + id, { method: 'DELETE', credentials: 'same-origin' }).catch(() => {})
      ));
      await _refreshAfterEndpointChange();
      try { await loadEndpoints(); } catch (_) {}
      _refreshOfflineCount();
      if (uiModule && uiModule.showToast) uiModule.showToast(`Removed ${ids.length} offline endpoint${ids.length === 1 ? '' : 's'}`, 1800);
    });
  }

  // Clear-on-focus for the API key inputs. The fields are type=password so the
  // value is masked; users can't see what's there to edit it in place, so the
  // expected gesture is "click in, type new key". Wiping on focus removes the
  // select-all-and-delete dance.
  const _wireClearOnFocus = (id) => {
    const inp = el(id);
    if (!inp) return;
    inp.addEventListener('focus', () => {
      if (inp.value) inp.value = '';
    });
  };
  _wireClearOnFocus('adm-epLocalApiKey');
  _wireClearOnFocus('adm-epApiKey');

  // Drop the Ollama provider logo into the Ollama Quickstart button. Reuses
  // the same SVG the provider picker uses, so brand parity stays free.
  try {
    const _ollamaLogoSlot = document.querySelector('#adm-epOllamaBtn .adm-ollama-logo');
    if (_ollamaLogoSlot) {
      const svg = providerLogo('ollama') || '';
      if (svg) _ollamaLogoSlot.innerHTML = svg;
    }
  } catch (_) {}

  // Local "Add" button — sibling form for self-hosted base URLs.
  const localAddBtn = el('adm-epLocalAddBtn');
  const localTestBtn = el('adm-epLocalTestBtn');
  if (localTestBtn) {
    localTestBtn.addEventListener('click', async () => {
      const testOriginalHtml = localTestBtn.innerHTML || '>Test';
      const msg = _endpointMsg('local');
      msg.textContent = ''; msg.className = 'adm-ep-inline-msg';
      const raw = (el('adm-epLocalUrl').value || '').trim();
      if (!raw) { msg.textContent = 'Enter a base URL to test'; msg.className = 'admin-error'; return; }
      const url = _normalizeBaseUrl(raw);
      const keyEl = el('adm-epLocalApiKey');
      const apiKey = keyEl ? keyEl.value.trim() : '';
      localTestBtn.disabled = true;
      localTestBtn.innerHTML = testOriginalHtml.replace(/>Test\s*$/, '>Testing...');
      try {
        const fd = new FormData();
        fd.append('base_url', url);
        if (apiKey) fd.append('api_key', apiKey);
        const res = await fetch('/api/model-endpoints/test', { method: 'POST', body: fd, credentials: 'same-origin' });
        const d = await res.json();
        _renderEndpointTestResult(msg, res, d);
      } catch (e) {
        msg.textContent = 'Test failed: ' + (e && e.message ? e.message : 'request failed');
        msg.className = 'admin-error';
      }
      localTestBtn.disabled = false;
      localTestBtn.innerHTML = testOriginalHtml;
    });
  }
  if (localAddBtn) {
    localAddBtn.addEventListener('click', async () => {
      const addOriginalHtml = localAddBtn.innerHTML || '>Add';
      const msg = _endpointMsg('local');
      msg.textContent = ''; msg.className = 'adm-ep-inline-msg';
      const raw = (el('adm-epLocalUrl').value || '').trim();
      if (!raw) { msg.textContent = 'Enter a base URL (e.g. http://localhost:8002/v1)'; msg.className = 'admin-error'; return; }
      const url = _normalizeBaseUrl(raw);
      const keyEl = el('adm-epLocalApiKey');
      const apiKey = keyEl ? keyEl.value.trim() : '';
      localAddBtn.disabled = true;
      localAddBtn.innerHTML = addOriginalHtml.replace(/>Add\s*$/, '>Adding...');
      try {
        const fd = new FormData();
        fd.append('base_url', url);
        if (apiKey) fd.append('api_key', apiKey);
        fd.append('endpoint_kind', 'local');
        fd.append('model_refresh_mode', 'auto');
        const lt = el('adm-epLocalType');
        if (lt) fd.append('model_type', lt.value);
        fd.append('skip_probe', 'false');
        const res = await fetch('/api/model-endpoints', { method: 'POST', body: fd, credentials: 'same-origin' });
        const d = await res.json();
        if (res.ok) {
          el('adm-epLocalUrl').value = '';
          if (keyEl) keyEl.value = '';
          if (lt) lt.value = 'llm';
          if (d.id) _recentlyAddedEpId = String(d.id);
          await loadEndpoints();
          await _selectAddedModelInChat(d);
          const count = (d.models || []).length;
          const baseText = d.status === 'empty'
            ? 'Added — Ollama is running, no models pulled yet'
            : d.online
            ? `Added — found ${count} model${count !== 1 ? 's' : ''}`
            : 'Added (offline — will retry on next load)';
          msg.innerHTML = `${baseText} <a href="#" data-go-added-models style="margin-left:6px;text-decoration:underline;color:inherit;font-weight:600;">Added Models →</a>`;
          msg.className = d.online ? 'admin-success' : 'admin-error';
        } else { msg.textContent = d.detail || 'Failed'; msg.className = 'admin-error'; }
      } catch (e) { msg.textContent = 'Request failed'; msg.className = 'admin-error'; }
      localAddBtn.disabled = false;
      localAddBtn.innerHTML = addOriginalHtml;
    });
  }

  const ollamaBtn = el('adm-epOllamaBtn');
  if (ollamaBtn) {
    ollamaBtn.addEventListener('click', async () => {
      const input = el('adm-epLocalUrl');
      if (input) {
        input.value = await _defaultOllamaUrl();
        input.focus();
      }
      const msg = _endpointMsg('local');
      if (msg) {
        msg.innerHTML = '<span style="font-size:11px;opacity:0.55;">Ollama ready to test.</span>';
        msg.className = '';
      }
    });
  }

  // Discover local models button
  const discoverBtn = el('adm-epDiscoverBtn');
  if (discoverBtn) {
    const discoverOriginalHtml = discoverBtn.innerHTML;
    discoverBtn.addEventListener('click', async () => {
      const msg = _endpointMsg('local');
      discoverBtn.disabled = true;
      msg.className = 'adm-ep-inline-msg';
      msg.innerHTML = '';
      try {
        const sp = window.spinnerModule || (await import('./spinner.js')).default;
        const wp = sp.createWhirlpool(20);
        wp.element.style.cssText = 'display:inline-block;vertical-align:middle;margin:0 8px 0 0;';
        const wrap = document.createElement('div');
        wrap.style.cssText = 'display:flex;align-items:center;padding:8px 0;';
        wrap.appendChild(wp.element);
        const txt = document.createElement('span');
        txt.textContent = 'Scanning ports 8000-8020, 8080, 1234, 11434, and 11435 for model servers...';
        txt.style.cssText = 'font-size:12px;opacity:0.7;';
        wrap.appendChild(txt);
        msg.appendChild(wrap);
        discoverBtn._wp = wp;
      } catch(e) { msg.textContent = 'Scanning...'; }
      try {
        const res = await fetch('/api/discover');
        const data = await res.json();
        const items = data.items || [];
        if (!items.length) {
          msg.textContent = 'No model servers found. Make sure vLLM, llama.cpp, SGLang, or Ollama is running. Docker users may need Ollama bound to a trusted reachable interface.';
          msg.className = 'admin-error';
        } else {
          // Auto-add each discovered endpoint. Server dedupes on base_url
          // and returns `existing: true` for already-registered ones.
          // Map fingerprinted provider IDs to friendly display names.
          const _PROVIDER_DISPLAY = {
            llamacpp: 'llama.cpp', lmstudio: 'LM Studio', vllm: 'vLLM',
            ollama: 'Ollama',
          };
          let added = 0;
          let skipped = 0;
          for (const item of items) {
            const base = item.url.replace('/chat/completions', '').replace(/\/$/, '');
            const providerDisplay = _PROVIDER_DISPLAY[item.provider] || null;
            const fd = new FormData();
            fd.append('base_url', base);
            if (providerDisplay) {
              // Use "Provider (host:port)" so the endpoint is immediately
              // identifiable in the list, e.g. "llama.cpp (localhost:8080)".
              const hostPart = base.replace(/^https?:\/\//, '').split('/')[0];
              fd.append('name', `${providerDisplay} (${hostPart})`);
            }
            fd.append('endpoint_kind', 'local');
            fd.append('model_refresh_mode', 'auto');
            fd.append('skip_probe', 'false');
            const r = await fetch('/api/model-endpoints', { method: 'POST', body: fd });
            if (r.ok) {
              try {
                const dd = await r.json();
                if (dd && dd.existing) { skipped++; }
                else { added++; if (dd && dd.id) _recentlyAddedEpId = String(dd.id); }
              } catch (_) { added++; }
            }
          }
          const totalModels = items.reduce((n, i) => n + (i.models ? i.models.length : 0), 0);
          const serverNames = items.map(i =>
            (_PROVIDER_DISPLAY[i.provider] || i.url.replace(/^https?:\/\//, '').split('/')[0])
          );
          const parts = [
            `Found ${items.length} server${items.length !== 1 ? 's' : ''} (${serverNames.join(', ')}) with ${totalModels} model${totalModels !== 1 ? 's' : ''}`,
          ];
          if (added) parts.push(`added ${added} new`);
          if (skipped) parts.push(`${skipped} already added`);
          msg.innerHTML = parts.join(' — ');
          msg.className = 'admin-success';
          loadEndpoints();
        }
      } catch (e) {
        msg.textContent = 'Scan failed: ' + e.message;
        msg.className = 'admin-error';
      }
      if (discoverBtn._wp) { discoverBtn._wp.destroy(); discoverBtn._wp = null; }
      discoverBtn.disabled = false;
      discoverBtn.innerHTML = discoverOriginalHtml;
    });
  }
}

/* ═══════════════════════════════════════════
   AGENTS TAB — built-in tools
   ═══════════════════════════════════════════ */

// ── Built-in tools management ──
const TOOL_META = {
  bash:              { name: 'Shell',            desc: 'Execute bash commands',           cat: 'Code',       ctx: '~200' },
  python:            { name: 'Python',           desc: 'Run Python scripts',              cat: 'Code',       ctx: '~200' },
  read_file:         { name: 'Read File',        desc: 'Read files from disk',            cat: 'Code',       ctx: '~150' },
  write_file:        { name: 'Write File',       desc: 'Write/create files',              cat: 'Code',       ctx: '~150' },
  web_search:        { name: 'Web Search',       desc: 'Search the web via SearXNG',      cat: 'Search',     ctx: '~300' },
  search_chats:      { name: 'Search Chats',     desc: 'Search conversation history',     cat: 'Search',     ctx: '~150' },
  create_document:   { name: 'Create Document',  desc: 'Create new documents',            cat: 'Documents',  ctx: '~200' },
  update_document:   { name: 'Update Document',  desc: 'Modify existing documents',       cat: 'Documents',  ctx: '~200' },
  edit_document:     { name: 'Edit Document',    desc: 'Find & replace in documents',     cat: 'Documents',  ctx: '~200' },
  suggest_document:  { name: 'Suggest Changes',  desc: 'Propose document edits',          cat: 'Documents',  ctx: '~200' },
  manage_documents:  { name: 'Manage Documents', desc: 'List, delete, organize docs',     cat: 'Documents',  ctx: '~150' },
  generate_image:    { name: 'Generate Image',   desc: 'Create images via AI',            cat: 'Media',      ctx: '~150' },
  manage_memory:     { name: 'Memory',           desc: 'Save and recall memories',        cat: 'Knowledge',  ctx: '~200' },
  manage_skills:     { name: 'Skills',           desc: 'Learn and use procedures',        cat: 'Knowledge',  ctx: '~200' },
  manage_rag:        { name: 'RAG / Docs',       desc: 'Query indexed documents',         cat: 'Knowledge',  ctx: '~150' },
  chat_with_model:   { name: 'Chat with Model',  desc: 'Talk to another AI model',        cat: 'Multi-Agent', ctx: '~200' },
  pipeline:          { name: 'Pipeline',         desc: 'Multi-step AI workflows',         cat: 'Multi-Agent', ctx: '~200' },
  ask_teacher:       { name: 'Ask Teacher',      desc: 'Query a more capable model',      cat: 'Multi-Agent', ctx: '~150' },
  send_to_session:   { name: 'Send to Session',  desc: 'Send message to another chat',    cat: 'Sessions',   ctx: '~100' },
  create_session:    { name: 'Create Session',   desc: 'Start a new chat session',        cat: 'Sessions',   ctx: '~100' },
  list_sessions:     { name: 'List Sessions',    desc: 'Browse existing sessions',        cat: 'Sessions',   ctx: '~100' },
  manage_session:    { name: 'Manage Session',   desc: 'Rename, archive, configure',      cat: 'Sessions',   ctx: '~100' },
  list_models:       { name: 'List Models',      desc: 'Show available models',           cat: 'System',     ctx: '~100' },
  ui_control:        { name: 'UI Control',       desc: 'Change theme, layout, settings',  cat: 'System',     ctx: '~150' },
  manage_tasks:      { name: 'Tasks',            desc: 'Schedule automated tasks',        cat: 'System',     ctx: '~150' },
  api_call:          { name: 'API Call',         desc: 'Make HTTP requests',              cat: 'System',     ctx: '~200' },
  manage_endpoints:  { name: 'Endpoints',        desc: 'Add/remove model endpoints',      cat: 'System',     ctx: '~100' },
  manage_mcp:        { name: 'MCP Servers',      desc: 'Manage MCP connections',          cat: 'System',     ctx: '~100' },
  manage_webhooks:   { name: 'Webhooks',         desc: 'Configure webhook events',        cat: 'System',     ctx: '~100' },
  manage_tokens:     { name: 'API Tokens',       desc: 'Manage API access tokens',        cat: 'System',     ctx: '~100' },
  manage_settings:   { name: 'Settings',         desc: 'Change app settings',             cat: 'System',     ctx: '~100' },
};

async function loadBuiltinTools() {
  const list = el('adm-builtin-tools-list');
  if (!list) return;
  try {
    // This panel is an editor, and its save posts the whole disabled list
    // rebuilt from the checkboxes below. So it has to render authoritative
    // state: a snapshot that went stale out of band (the manage_settings tool,
    // another tab) would be re-posted wholesale on the next unrelated toggle
    // and would silently undo the newer state. refreshAll() calls this on every
    // panel open, so drop the shared entry and refill it. The startup read that
    // chatRenderer.js shares is unaffected; this panel just never edits a cache,
    // which is the same rule the settings panel follows by reading directly.
    invalidateTools();
    const data = await getTools();
    const tools = data.tools || [];
    if (!tools.length) { list.innerHTML = '<div class="admin-empty">No tools found</div>'; return; }

    // Group by category
    const groups = {};
    for (const t of tools) {
      const meta = TOOL_META[t.id] || { name: t.id, desc: '', cat: 'Other', ctx: '?' };
      const cat = meta.cat;
      if (!groups[cat]) groups[cat] = [];
      groups[cat].push({ ...t, ...meta });
    }

    // Category order
    const catOrder = ['Code', 'Search', 'Documents', 'Media', 'Knowledge', 'Multi-Agent', 'Sessions', 'System', 'Other'];
    let html = '';
    for (const cat of catOrder) {
      const items = groups[cat];
      if (!items) continue;
      const enabledCount = items.filter(i => i.enabled).length;
      const totalCount = items.length;
      const catId = 'tool-cat-' + cat.replace(/[^a-zA-Z]/g, '');
      const allEnabled = enabledCount === totalCount;
      html += `<div class="admin-tool-category">
        <div class="admin-tool-cat-header" data-tool-cat="${catId}" style="cursor:pointer;display:flex;align-items:center;justify-content:space-between;">
          <span>${esc(cat)}</span>
          <span style="display:flex;align-items:center;gap:6px;" class="admin-tool-cat-right">
            <span class="admin-tool-cat-count" style="font-size:10px;opacity:0.5;">${enabledCount}/${totalCount}</span>
            <label class="admin-switch" style="flex-shrink:0;">
              <input type="checkbox" data-tool-cat-toggle="${catId}" ${allEnabled ? 'checked' : ''}>
              <span class="admin-slider"></span>
            </label>
            <svg class="admin-tool-cat-chevron" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="opacity:0.3;transition:transform 0.2s,opacity 0.2s;"><polyline points="6 9 12 15 18 9"/></svg>
          </span>
        </div>
        <div class="admin-tool-cat-body hidden" id="${catId}">`;
      for (const t of items) {
        html += `
        <div class="admin-tool-row">
          <div class="admin-tool-info">
            <span class="admin-tool-name">${esc(t.name)}</span>
            <span class="admin-tool-desc">${esc(t.desc)}</span>
          </div>
          <span class="admin-tool-ctx" title="Approximate context tokens used">${esc(t.ctx)}</span>
          <label class="admin-switch" style="flex-shrink:0;">
            <input type="checkbox" data-tool-id="${esc(t.id)}" ${t.enabled ? 'checked' : ''}>
            <span class="admin-slider"></span>
          </label>
        </div>`;
      }
      html += '</div></div>';
    }
    list.innerHTML = html;

    // Prevent toggle clicks from expanding/collapsing
    list.querySelectorAll('.admin-tool-cat-right').forEach(span => {
      span.addEventListener('click', e => e.stopPropagation());
    });

    // Wire category expand/collapse
    list.querySelectorAll('[data-tool-cat]').forEach(header => {
      header.addEventListener('click', () => {
        const body = el(header.dataset.toolCat);
        if (!body) return;
        body.classList.toggle('hidden');
        const chevron = header.querySelector('.admin-tool-cat-chevron');
        const isOpen = !body.classList.contains('hidden');
        if (chevron) {
          chevron.style.transform = isOpen ? 'rotate(180deg)' : '';
          chevron.style.opacity = isOpen ? '0.7' : '0.3';
        }
      });
    });

    // Merge only the user's intended changes onto authoritative server state.
    // /api/tools replaces the full disabled list, so rebuilding it from this
    // panel's DOM can undo a change made by another tab or manage_settings
    // after the panel was opened.
    async function _saveToolState(changes) {
      invalidateTools();
      const latest = await getTools();
      const state = new Map(
        (latest.tools || []).map(t => [t.id, !!t.enabled])
      );

      for (const change of changes) {
        if (state.has(change.id)) {
          state.set(change.id, !!change.enabled);
        }
      }

      const disabled = Array.from(state.entries())
        .filter(([, enabled]) => !enabled)
        .map(([id]) => id);

      try {
        const res = await fetch('/api/tools', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ disabled }),
          credentials: 'same-origin',
        });
        if (!res.ok) throw new Error(`Failed to update tools (${res.status})`);

        // Bring the still-open editor forward to the same merged snapshot so an
        // out-of-band change is visible instead of leaving stale checkboxes.
        list.querySelectorAll('input[data-tool-id]').forEach(c => {
          if (state.has(c.dataset.toolId)) {
            c.checked = state.get(c.dataset.toolId);
          }
        });
        list.querySelectorAll('.admin-tool-category').forEach(_updateCatCounter);
      } finally {
        // This route persists disabled_tools into the settings store
        // (routes/model_routes.py), so both snapshots are now stale.
        invalidateTools();
        invalidateSettings();
      }
    }
    function _updateCatCounter(catEl) {
      if (!catEl) return;
      const catChecks = catEl.querySelectorAll('input[data-tool-id]');
      const catEnabled = Array.from(catChecks).filter(c => c.checked).length;
      const counter = catEl.querySelector('.admin-tool-cat-count');
      if (counter) counter.textContent = catEnabled + '/' + catChecks.length;
      const catToggle = catEl.querySelector('input[data-tool-cat-toggle]');
      if (catToggle) catToggle.checked = (catEnabled === catChecks.length);
    }

    // Wire individual tool toggles
    list.querySelectorAll('input[data-tool-id]').forEach(chk => {
      chk.addEventListener('change', async () => {
        await _saveToolState([
          { id: chk.dataset.toolId, enabled: chk.checked },
        ]);
        _updateCatCounter(chk.closest('.admin-tool-category'));
      });
    });

    // Wire category-level toggle (enable/disable all in category)
    list.querySelectorAll('input[data-tool-cat-toggle]').forEach(chk => {
      chk.addEventListener('change', async () => {
        const catEl = chk.closest('.admin-tool-category');
        if (!catEl) return;
        const checked = chk.checked;
        const changes = Array.from(catEl.querySelectorAll('input[data-tool-id]'))
          .map(c => ({ id: c.dataset.toolId, enabled: checked }));
        catEl.querySelectorAll('input[data-tool-id]').forEach(c => { c.checked = checked; });
        await _saveToolState(changes);
        _updateCatCounter(catEl);
      });
    });
  } catch (e) {
    console.error('Failed to load tools:', e);
    list.innerHTML = '<div class="admin-empty">Failed to load tools</div>';
  }
}

/* ── Embedding model ──
   No settings UI: the embedding model (RAG, semantic memory, tool selection)
   is fixed infrastructure that ships with the app, and swapping it would
   invalidate every existing vector. Configure via the FASTEMBED_MODEL /
   EMBEDDING_URL env vars if you really need to override it. */

/* ── RAG ── */
function _ragFileGroups(files) {
  const groups = new Map();
  for (const file of files || []) {
    const displayPath = String(file.name || file.path || 'Untitled').replace(/\\/g, '/');
    const slash = displayPath.lastIndexOf('/');
    const directory = slash > 0 ? displayPath.slice(0, slash) : 'Personal uploads';
    const filename = slash >= 0 ? displayPath.slice(slash + 1) : displayPath;
    if (!groups.has(directory)) groups.set(directory, []);
    groups.get(directory).push({ ...file, displayPath, filename });
  }
  return Array.from(groups, ([directory, items]) => ({
    directory,
    items: items.sort((a, b) => a.filename.localeCompare(b.filename, undefined, { sensitivity: 'base' })),
  })).sort((a, b) => a.directory.localeCompare(b.directory, undefined, { sensitivity: 'base' }));
}

function _renderRagFileGroups(files) {
  const folderIcon = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6h7l2 2h9v11H3z"/></svg>';
  const fileIcon = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 2h8l4 4v16H6z"/><path d="M14 2v5h5"/></svg>';
  return _ragFileGroups(files).map(group => {
    const privateCount = group.items.filter(file => file.sensitivity === 'private').length;
    const rows = group.items.map(file => {
      const size = file.size ? (file.size > 1024 ? (file.size / 1024).toFixed(1) + ' KB' : file.size + ' B') : '';
      const priv = file.sensitivity === 'private'
        ? '<span class="admin-badge" title="Withheld unless this chat has private-vault read access">private</span>'
        : '';
      return `<div class="admin-rag-item admin-rag-file-row">${fileIcon}<span class="admin-rag-item-name" title="${esc(file.displayPath)}">${esc(file.filename)}</span>${priv}<span class="admin-rag-item-meta">${size}</span><button class="admin-btn-delete" data-adm-rag-file="${esc(file.path || file.name)}">Delete</button></div>`;
    }).join('');
    return `<details class="admin-rag-file-group">
      <summary>${folderIcon}<span title="${esc(group.directory)}">${esc(group.directory)}</span>${privateCount ? `<span class="admin-badge">${privateCount} private</span>` : ''}<small>${group.items.length} file${group.items.length === 1 ? '' : 's'}</small></summary>
      <div class="admin-rag-file-group-body">${rows}</div>
    </details>`;
  }).join('');
}

async function loadRag() {
  if (!el('adm-ragDirList')) return;
  try {
    const res = await fetch('/api/personal');
    const data = await res.json();
    const dirList = el('adm-ragDirList');
    // Prefer the server-paired list: it resolves each directory's label the
    // same way retrieval does. Fall back to the flat list for an older server.
    const labels = data.directory_sensitivity || {};
    const dirs = Array.isArray(data.directories_detail) && data.directories_detail.length
      ? data.directories_detail.map(r => r.directory)
      : (data.directories || []);
    const labelFor = Array.isArray(data.directories_detail) && data.directories_detail.length
      ? Object.fromEntries(data.directories_detail.map(r => [r.directory, r.sensitivity]))
      : labels;
    if (dirs.length === 0) { dirList.innerHTML = '<div class="admin-empty">No directories indexed</div>'; }
    else {
      // The label is read-only here. A per-directory switch wrote a label
      // that bypassed the folder privacy policy, so the policy is the one
      // place to change it: Settings › Advanced › Knowledge.
      dirList.innerHTML = dirs.map(d => {
        const label = labelFor[d] === 'private' ? 'private' : 'public';
        return `<div class="admin-rag-item"><span class="admin-rag-item-name" title="${esc(d)}">${esc(d)}</span><span class="admin-badge${label === 'private' ? '' : ' admin-rag-public'}" title="Set by the folder privacy policy (Advanced › Knowledge). Private content is hidden unless a chat is granted private-vault reads.">${label}</span><button class="admin-btn-delete" data-adm-rag-dir="${esc(d)}">Remove</button></div>`;
      }).join('');
      dirList.querySelectorAll('[data-adm-rag-dir]').forEach(btn => {
        btn.addEventListener('click', async () => {
          if (!await uiModule.styledConfirm(`Remove directory "${btn.dataset.admRagDir}" from RAG?`, { confirmText: 'Remove', danger: true })) return;
          btn.disabled = true; btn.textContent = '...';
          try {
            const res = await fetch('/api/personal/remove_directory?directory=' + encodeURIComponent(btn.dataset.admRagDir), { method: 'DELETE' });
            if (res.ok) { ragMsg('Directory removed'); loadRag(); }
            else { const e = await res.json(); ragMsg(e.detail || 'Failed', true); }
          } catch (e) { ragMsg('Error: ' + e.message, true); }
        });
      });
    }
    const fileList = el('adm-ragFileList');
    const files = data.files || [];
    if (files.length === 0) { fileList.innerHTML = '<div class="admin-empty">No files indexed</div>'; }
    else {
      // Match the Vault explorer: directory groups are compact and closed by
      // default. Native <details> keeps expansion entirely user-driven.
      fileList.innerHTML = _renderRagFileGroups(files);
      fileList.querySelectorAll('[data-adm-rag-file]').forEach(btn => {
        btn.addEventListener('click', async () => {
          if (!await uiModule.styledConfirm(`Delete "${btn.dataset.admRagFile}" from RAG?`, { confirmText: 'Delete', danger: true })) return;
          btn.disabled = true; btn.textContent = '...';
          try {
            const res = await fetch('/api/personal/file?filepath=' + encodeURIComponent(btn.dataset.admRagFile), { method: 'DELETE' });
            if (res.ok) { ragMsg('File removed'); loadRag(); }
            else { const e = await res.json(); ragMsg(e.detail || 'Failed', true); }
          } catch (e) { ragMsg('Error: ' + e.message, true); }
        });
      });
    }
  } catch (e) {
    const dl = el('adm-ragDirList');
    const fl = el('adm-ragFileList');
    if (dl) dl.innerHTML = '<div class="admin-error">Failed to load</div>';
    if (fl) fl.innerHTML = '';
  }
}

let _ragMsgTimer = null;
function ragMsg(text, isError, persist) {
  const s = el('adm-ragStatus');
  s.textContent = text; s.style.color = isError ? 'var(--red)' : 'var(--fg)';
  if (_ragMsgTimer) { clearTimeout(_ragMsgTimer); _ragMsgTimer = null; }
  if (text && !persist) _ragMsgTimer = setTimeout(() => { s.textContent = ''; }, 5000);
}

async function ragUpload(files) {
  if (!files || files.length === 0) return;
  ragMsg('Uploading ' + files.length + ' file(s)...', false, true);
  const fd = new FormData();
  for (const f of files) fd.append('files', f);
  try {
    const res = await fetch('/api/personal/upload', { method: 'POST', body: fd });
    const data = await res.json();
    if (data.success) { ragMsg(`Uploaded ${data.uploaded.length} file(s), ${data.indexed_count} chunks indexed`); loadRag(); }
    else ragMsg(data.detail || 'Upload failed', true);
  } catch (e) { ragMsg('Upload error: ' + e.message, true); }
}

function initRag() {
  const dropZone = el('adm-ragDropZone');
  const fileInput = el('adm-ragFileInput');
  // This panel's handlers shipped in v1.0 without any matching markup, so
  // initRag was never wired up and loadRag never ran. Guard the lookup so the
  // same drift fails visibly at one line instead of silently taking out the
  // whole init.
  if (!dropZone || !fileInput) {
    console.warn('RAG panel markup missing; personal-document controls disabled');
    return;
  }
  dropZone.addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', () => ragUpload(fileInput.files));
  dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('dragover'); });
  dropZone.addEventListener('dragleave', () => dropZone.classList.remove('dragover'));
  dropZone.addEventListener('drop', e => { e.preventDefault(); dropZone.classList.remove('dragover'); ragUpload(e.dataTransfer.files); });
  el('adm-ragAddDirBtn').addEventListener('click', async () => {
    const dir = el('adm-ragDirInput').value.trim();
    if (!dir) return;
    const btn = el('adm-ragAddDirBtn');
    btn.disabled = true; btn.textContent = 'Indexing...';
    try {
      // No label is sent: the folder privacy policy decides it.
      const res = await fetch('/api/personal/add_directory', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({ directory: dir }),
      });
      const data = await res.json();
      if (data.success) { ragMsg(`Indexed ${data.indexed_count} chunks from directory${data.sensitivity ? ` (${data.sensitivity})` : ''}`); el('adm-ragDirInput').value = ''; loadRag(); }
      else ragMsg(data.detail || data.message || 'Failed', true);
    } catch (e) { ragMsg('Error: ' + e.message, true); }
    btn.disabled = false; btn.textContent = 'Add Directory';
  });
  el('adm-ragReloadBtn').addEventListener('click', async () => {
    const btn = el('adm-ragReloadBtn');
    btn.disabled = true; btn.textContent = 'Reloading...';
    try {
      const res = await fetch('/api/personal/reload', { method: 'POST' });
      const data = await res.json();
      ragMsg(`Index reloaded: ${data.count} documents`);
      loadRag();
    } catch (e) { ragMsg('Reload failed: ' + e.message, true); }
    btn.disabled = false; btn.textContent = 'Reload Index';
  });
}

/* ── Data Backup (export/import) ── */
function initBackup() {
  el('adm-exportDataBtn').addEventListener('click', async () => {
    const btn = el('adm-exportDataBtn');
    const msg = el('adm-backupMsg');
    btn.disabled = true; btn.textContent = 'Exporting...'; msg.textContent = '';
    try {
      const res = await fetch('/api/export', { credentials: 'same-origin' });
      if (!res.ok) throw new Error('Export failed');
      const blob = await res.blob();
      const disposition = res.headers.get('Content-Disposition') || '';
      const match = disposition.match(/filename=(.+)/);
      const filename = match ? match[1] : 'odysseus_backup.json';
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = filename;
      a.click();
      URL.revokeObjectURL(a.href);
      msg.textContent = 'Export downloaded.'; msg.className = 'admin-success';
    } catch (e) { msg.textContent = 'Export failed: ' + e.message; msg.className = 'admin-error'; }
    btn.disabled = false; btn.textContent = 'Export Data';
  });

  const fileInput = el('adm-importFile');
  el('adm-importDataBtn').addEventListener('click', () => { fileInput.value = ''; fileInput.click(); });
  fileInput.addEventListener('change', async () => {
    const file = fileInput.files[0];
    if (!file) return;
    const msg = el('adm-backupMsg');
    const btn = el('adm-importDataBtn');
    btn.disabled = true; btn.textContent = 'Importing...'; msg.textContent = '';
    try {
      const text = (await file.text()).replace(/^\uFEFF/, '').trim();
      let data;
      try {
        data = JSON.parse(text);
      } catch (e) {
        throw new Error('Invalid backup file: ' + e.message);
      }
      const res = await fetch('/api/import', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
      });
      const result = await res.json().catch(() => null);
      if (!result) {
        throw new Error(`Import failed: server returned ${res.status}`);
      }
      if (res.ok && result.ok) {
        msg.textContent = result.message || 'Import successful.'; msg.className = 'admin-success';
      } else {
        msg.textContent = result.message || result.detail || 'Import failed'; msg.className = 'admin-error';
      }
    } catch (e) { msg.textContent = 'Import failed: ' + e.message; msg.className = 'admin-error'; }
    btn.disabled = false; btn.textContent = 'Import Data';
  });
}

/* ── Danger Zone ── */
function initDangerZone() {
  // Per-category Danger Zone wipes. Each button declares its target
  // via data-wipe-kind; one delegated handler handles double-confirm,
  // POSTs to /api/admin/wipe/{kind}, and writes the result.
  const _LABELS = {
    chats: 'chats', memory: 'memory entries', skills: 'skills',
    notes: 'notes', tasks: 'tasks', documents: 'documents',
    gallery: 'gallery images', calendar: 'calendar items',
  };
  const _wipeMsg = el('adm-wipeMsg');
  modalEl.querySelectorAll('[data-wipe-kind]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const kind = btn.dataset.wipeKind;
      const isAll = kind === '__all__';
      const label = isAll ? 'data across every category' : (_LABELS[kind] || kind);
      if (!await uiModule.styledConfirm(`Delete ALL ${label}? This cannot be undone.`, { confirmText: 'Delete', danger: true })) return;
      if (!await uiModule.styledConfirm(`Really delete every one of your ${label}?`, { confirmText: isAll ? 'Yes, delete everything' : 'Yes, delete everything', danger: true })) return;
      btn.disabled = true;
      const prevHtml = btn.innerHTML;
      btn.innerHTML = isAll ? 'Deleting all…' : 'Deleting…';
      if (_wipeMsg) { _wipeMsg.textContent = ''; _wipeMsg.className = ''; }
      try {
        if (isAll) {
          // Iterate every known category. Failures in one shouldn't stop
          // the rest — record per-category counts and surface a summary.
          const kinds = Object.keys(_LABELS);
          const results = [];
          for (const k of kinds) {
            try {
              const r = await fetch(`/api/admin/wipe/${k}`, { method: 'DELETE', credentials: 'same-origin' });
              const d = await r.json().catch(() => ({}));
              results.push({ k, ok: r.ok, count: d.count ?? 0, error: r.ok ? null : (d.detail || 'failed') });
            } catch (e) {
              results.push({ k, ok: false, count: 0, error: e.message });
            }
          }
          const okCount = results.filter(r => r.ok).length;
          const total = results.reduce((n, r) => n + (r.ok ? r.count : 0), 0);
          const fails = results.filter(r => !r.ok).map(r => r.k);
          if (_wipeMsg) {
            if (!fails.length) {
              _wipeMsg.textContent = `Deleted ${total} items across all ${okCount} categories.`;
              _wipeMsg.className = 'admin-success';
            } else {
              _wipeMsg.textContent = `Deleted ${total} items; failed: ${fails.join(', ')}.`;
              _wipeMsg.className = 'admin-error';
            }
          }
        } else {
          const res = await fetch(`/api/admin/wipe/${kind}`, { method: 'DELETE', credentials: 'same-origin' });
          const data = await res.json().catch(() => ({}));
          if (res.ok) {
            if (_wipeMsg) { _wipeMsg.textContent = `Deleted ${data.count ?? 0} ${label}.`; _wipeMsg.className = 'admin-success'; }
          } else {
            if (_wipeMsg) { _wipeMsg.textContent = data.detail || 'Failed'; _wipeMsg.className = 'admin-error'; }
          }
        }
      } catch (e) {
        if (_wipeMsg) { _wipeMsg.textContent = 'Request failed: ' + e.message; _wipeMsg.className = 'admin-error'; }
      }
      btn.disabled = false; btn.innerHTML = prevHtml;
    });
  });
}

/* ═══════════════════════════════════════════
   TERMINAL LOGS VIEWER
   ═══════════════════════════════════════════ */
let logsPollInterval = null;
let isLogsPolling = false;
let cachedLogs = [];
let logsAbortController = null;

function renderLogs(isAutoPoll = false) {
  const consoleContainer = el('log-console-container');
  const levelSelect = el('log-level-select');
  const searchInput = el('log-search-input');

  if (!consoleContainer) return;

  const levelFilter = levelSelect ? levelSelect.value : 'ALL';
  const searchQuery = searchInput ? searchInput.value.trim().toLowerCase() : '';

  let logs = cachedLogs;

  // Filter by level locally
  if (levelFilter !== 'ALL') {
    logs = logs.filter(line => line.includes(` - ${levelFilter} - `));
  }

  // Filter by search query locally
  if (searchQuery) {
    logs = logs.filter(line => line.toLowerCase().includes(searchQuery));
  }

  if (logs.length === 0) {
    consoleContainer.innerHTML = '<div class="settings-system-logs-placeholder">No logs found matching current filters.</div>';
    return;
  }

  // Preserve scroll position if user is reading previous logs
  const atBottom = consoleContainer.scrollHeight - consoleContainer.scrollTop - consoleContainer.clientHeight < 40;

  consoleContainer.innerHTML = logs.map(line => {
    let levelClass = 'log-line-default';

    if (line.includes(' - INFO - ')) {
      levelClass = 'log-line-info';
    } else if (line.includes(' - WARNING - ')) {
      levelClass = 'log-line-warning';
    } else if (line.includes(' - ERROR - ') || line.includes(' - CRITICAL - ')) {
      levelClass = 'log-line-error';
    } else if (line.includes(' - DEBUG - ')) {
      levelClass = 'log-line-debug';
    }

    // XSS safe escape
    const escaped = line
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');

    return `<div class="log-line ${levelClass}">${escaped}</div>`;
  }).join('');

  if (!isAutoPoll || atBottom) {
    consoleContainer.scrollTop = consoleContainer.scrollHeight;
  }
}

async function loadLogs(isAutoPoll = false) {
  const consoleContainer = el('log-console-container');
  const limitSelect = el('log-limit-select');

  if (!consoleContainer) return;

  const limit = limitSelect ? limitSelect.value : 200;

  if (logsAbortController) {
    logsAbortController.abort();
  }
  logsAbortController = new AbortController();
  const { signal } = logsAbortController;

  try {
    const res = await fetch(`/api/diagnostics/logs?limit=${limit}`, {
      credentials: 'same-origin',
      signal
    });

    if (!res.ok) {
      if (!isAutoPoll) {
        consoleContainer.innerHTML = '';
        const errDiv = document.createElement('div');
        errDiv.style.color = 'var(--red)';
        errDiv.style.fontWeight = '600';
        errDiv.textContent = `Failed to load logs: HTTP ${res.status}`;
        consoleContainer.appendChild(errDiv);
      }
      return;
    }

    const data = await res.json();
    if (data.status !== 'success' || !data.logs) {
      if (!isAutoPoll) {
        consoleContainer.innerHTML = '';
        const errDiv = document.createElement('div');
        errDiv.style.color = 'var(--red)';
        errDiv.style.fontWeight = '600';
        errDiv.textContent = 'Failed to parse logs data';
        consoleContainer.appendChild(errDiv);
      }
      return;
    }

    cachedLogs = data.logs;
    renderLogs(isAutoPoll);
  } catch (err) {
    if (err.name === 'AbortError') {
      return; // Silently ignore deliberate abort
    }
    if (!isAutoPoll) {
      consoleContainer.innerHTML = '';
      const errDiv = document.createElement('div');
      errDiv.style.color = 'var(--red)';
      errDiv.style.fontWeight = '600';
      errDiv.textContent = `Error retrieving logs: ${err.message}`;
      consoleContainer.appendChild(errDiv);
    }
  } finally {
    if (logsAbortController?.signal === signal) {
      logsAbortController = null;
    }
  }
}

function startLogsPolling() {
  if (isLogsPolling) return;
  isLogsPolling = true;
  const toggle = el('log-auto-refresh-toggle');
  if (toggle) toggle.checked = true;

  logsPollInterval = setInterval(() => {
    const modal = el('settings-modal');
    const systemPanel = el('settings-modal')?.querySelector('[data-settings-panel="system"]');

    // Safe self-cleanup if modal or panel is hidden/closed
    if (!modal || modal.classList.contains('hidden') || !systemPanel || systemPanel.classList.contains('hidden')) {
      stopLogsPolling();
      return;
    }

    loadLogs(true);
  }, 3000);
}

function stopLogsPolling() {
  if (!isLogsPolling) return;
  isLogsPolling = false;
  if (logsPollInterval) {
    clearInterval(logsPollInterval);
    logsPollInterval = null;
  }
  const toggle = el('log-auto-refresh-toggle');
  if (toggle) toggle.checked = false;
}

function initLogsView() {
  const refreshBtn = el('log-refresh-btn');
  const levelSelect = el('log-level-select');
  const limitSelect = el('log-limit-select');
  const searchInput = el('log-search-input');
  const autoRefreshToggle = el('log-auto-refresh-toggle');

  if (refreshBtn) refreshBtn.addEventListener('click', () => loadLogs(false));
  if (levelSelect) levelSelect.addEventListener('change', () => renderLogs(false));
  if (limitSelect) limitSelect.addEventListener('change', () => loadLogs(false));
  if (searchInput) searchInput.addEventListener('input', () => renderLogs(false));

  if (autoRefreshToggle) {
    autoRefreshToggle.addEventListener('change', (e) => {
      if (e.target.checked) {
        startLogsPolling();
      } else {
        stopLogsPolling();
      }
    });
  }

  // Initial fetch on view loading
  loadLogs(false);
}

async function loadRouteLatency() {
  const list = el('adm-routeLatencyList');
  if (!list) return;
  list.innerHTML = '<span style="opacity:0.5;font-size:11px;">Loading...</span>';
  try {
    const res = await fetch('/api/diagnostics/route_latency', { credentials: 'same-origin' });
    const data = await res.json();
    const routes = data.routes || [];
    if (!routes.length) { list.innerHTML = '<div class="admin-empty">No requests recorded yet</div>'; return; }
    list.innerHTML = routes.slice(0, 25).map(r => `<div class="settings-row" style="font-size:11px;">
      <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${esc(r.method)} ${esc(r.path)}"><strong>${esc(r.method)}</strong> ${esc(r.path)}</span>
      <span style="opacity:0.7;white-space:nowrap;">avg ${r.avg_ms}ms · p95 ${r.p95_ms}ms · max ${r.max_ms}ms · n=${r.count}</span>
    </div>`).join('');
  } catch (e) {
    list.innerHTML = `<div class="admin-error">Failed to load route latency: ${esc(e.message)}</div>`;
  }
}

function initRouteLatencyView() {
  const refreshBtn = el('adm-routeLatencyRefresh');
  if (refreshBtn) refreshBtn.addEventListener('click', () => loadRouteLatency());
  loadRouteLatency();
}

/* ── Diagnostics bundle (/api/diagnostics/bundle) ── */
function _diagBundleParams(includeMessages) {
  const params = new URLSearchParams({ minutes: el('diag-bundle-minutes')?.value || '60' });
  const session = (el('diag-bundle-session')?.value || '').trim();
  if (session) params.append('session', session);
  if (includeMessages) params.set('include_messages', 'true');
  return params;
}

async function _diagErrorText(res) {
  try {
    const data = await res.json();
    return data.detail || data.error || `HTTP ${res.status}`;
  } catch (_) {
    return `HTTP ${res.status}`;
  }
}

function initDiagnosticsBundle() {
  const exportBtn = el('diag-bundle-export-btn');
  if (!exportBtn) return;
  const previewBtn = el('diag-bundle-preview-btn');
  const msgBox = el('diag-bundle-msg');
  const messagesCb = el('diag-bundle-messages');
  const warning = el('diag-bundle-messages-warning');
  const setMsg = (text, cls = '') => {
    msgBox.textContent = text;
    msgBox.className = 'settings-diag-bundle-msg' + (cls ? ' ' + cls : '');
  };

  if (messagesCb && warning) {
    messagesCb.addEventListener('change', () => warning.classList.toggle('hidden', !messagesCb.checked));
  }

  const currentBtn = el('diag-bundle-current-btn');
  if (currentBtn) currentBtn.addEventListener('click', async () => {
    try {
      const mod = await import('./sessions.js');
      const sid = (mod.default && mod.default.getCurrentSessionId && mod.default.getCurrentSessionId())
        || (mod.getCurrentSessionId && mod.getCurrentSessionId()) || '';
      if (!sid) { setMsg('No chat is open.', 'admin-error'); return; }
      el('diag-bundle-session').value = sid;
      setMsg('');
    } catch (e) {
      setMsg('Could not read the open chat: ' + e.message, 'admin-error');
    }
  });

  if (previewBtn) previewBtn.addEventListener('click', async () => {
    previewBtn.disabled = true;
    setMsg('Reading logs...');
    try {
      const res = await fetch(`/api/diagnostics/bundle/summary?${_diagBundleParams(false)}`, { credentials: 'same-origin' });
      if (!res.ok) throw new Error(await _diagErrorText(res));
      const d = await res.json();
      const logs = (d.logs || []).filter(l => l.lines).map(l => `${l.name} (${l.lines})`).join(', ') || 'none';
      const sessions = (d.sessions || []).map(s => `  ${s.id}${s.title ? ' - ' + s.title : ''}${s.model ? ' [' + s.model + ']' : ''}`);
      const lines = [
        `${d.total_lines || 0} log line(s) in the last ${d.window_minutes} min: ${logs}`,
        `${(d.sessions || []).length} chat(s)${d.sessions_capped ? ' (capped)' : ''}:`,
        ...(sessions.length ? sessions : ['  none found in the logs']),
        `Loadouts: ${(d.loadouts || []).join(', ') || 'none'}`,
      ];
      if ((d.sessions_not_found || []).length) lines.push(`Mentioned but gone: ${d.sessions_not_found.length} chat(s)`);
      if ((d.errors || []).length) lines.push(`Errors: ${d.errors.map(e => e.component).join(', ')}`);
      setMsg(lines.join('\n'));
    } catch (e) {
      setMsg('Preview failed: ' + e.message, 'admin-error');
    } finally {
      previewBtn.disabled = false;
    }
  });

  exportBtn.addEventListener('click', async () => {
    const includeMessages = !!(messagesCb && messagesCb.checked);
    if (includeMessages && !await uiModule.styledConfirm(
      'The bundle will contain the last messages of each included chat. They can hold private vault notes, email and anything else those chats saw. Export anyway?',
      { confirmText: 'Export with messages', danger: true })) return;
    const label = exportBtn.textContent;
    exportBtn.disabled = true;
    exportBtn.textContent = 'Building...';
    setMsg('');
    try {
      const res = await fetch(`/api/diagnostics/bundle?${_diagBundleParams(includeMessages)}`, { credentials: 'same-origin' });
      if (!res.ok) throw new Error(await _diagErrorText(res));
      const blob = await res.blob();
      const disposition = res.headers.get('Content-Disposition') || '';
      const match = disposition.match(/filename="?([^";]+)"?/);
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = match ? match[1] : 'odysseus-diagnostics.zip';
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 1000);
      setMsg(`Downloaded ${a.download} (${Math.max(1, Math.round(blob.size / 1024))} KB).`
        + (includeMessages ? ' It contains chat messages.' : ''), 'admin-success');
    } catch (e) {
      setMsg('Export failed: ' + e.message, 'admin-error');
    } finally {
      exportBtn.disabled = false;
      exportBtn.textContent = label;
    }
  });
}

/* ═══════════════════════════════════════════
   INIT & REFRESH
   ═══════════════════════════════════════════ */
function initAll() {
  modalEl = el('settings-modal');
  const inits = [
    initSignupToggle, initShareDefaultsToggle, initAddUser, initEndpointForm,
    initBackup, initDangerZone, initLogsView, initRouteLatencyView,
    initDiagnosticsBundle, initRag,
  ];
  for (const fn of inits) {
    try { fn(); } catch (e) { console.error('Admin init error in', fn.name || 'anonymous', e); }
  }
  initialized = true;
}

// What each Settings tab needs from this module. A tab's lists reload every
// time it is shown (so they are never stale) and only then: opening Models
// no longer also reloads users, tools, documents and logs.
const TAB_LOADERS = {
  models: [loadEndpoints],
  agents: [loadBuiltinTools],
  privacy: [loadRag],
  users: [loadUsers],
  system: [() => loadLogs(false)],
};

function refreshTab(tab) {
  const loaders = TAB_LOADERS[resolveSettingsPanelId(tab)] || [];
  for (const fn of loaders) {
    try { fn(); } catch (e) { console.error('Admin load error in', fn.name || 'anonymous', e); }
  }
}

/* ═══════════════════════════════════════════
   PUBLIC API
   ═══════════════════════════════════════════ */
export function _initData(tab) {
  if (!initialized) initAll();
  refreshTab(tab || 'models');
}

export function open(tab) {
  // settings.open() loads this module's data for the tab it shows, so the
  // tab's lists are fetched once whichever of the two entry points ran.
  settingsModule.open(resolveSettingsPanelId(tab || 'models'));
}

export function close() {
  stopLogsPolling();
  settingsModule.close();
}

const adminModule = { open, close, _initData, get _initialized() { return initialized; } };
export default adminModule;
