import * as Modals from './modalManager.js';
import { makeWindowDraggable } from './windowDrag.js';

let _open = false;
let _selected = null;

const EMOTIONS = {
  pleasant_high: [
    ['energized', 'Energized'], ['excited', 'Excited'], ['joyful', 'Joyful'],
    ['confident', 'Confident'], ['inspired', 'Inspired'], ['playful', 'Playful'],
  ],
  pleasant_low: [
    ['calm', 'Calm'], ['content', 'Content'], ['grateful', 'Grateful'],
    ['peaceful', 'Peaceful'], ['safe', 'Safe'], ['rested', 'Rested'],
  ],
  unpleasant_high: [
    ['anxious', 'Anxious'], ['overwhelmed', 'Overwhelmed'], ['angry', 'Angry'],
    ['stressed', 'Stressed'], ['restless', 'Restless'], ['frustrated', 'Frustrated'],
  ],
  unpleasant_low: [
    ['sad', 'Sad'], ['drained', 'Drained'], ['lonely', 'Lonely'],
    ['discouraged', 'Discouraged'], ['numb', 'Numb'], ['tired', 'Tired'],
  ],
};

const FAMILY_META = {
  pleasant_high: { label: 'Pleasant · high energy', valence: 0.7, energy: 0.8, icon: '↗' },
  pleasant_low: { label: 'Pleasant · low energy', valence: 0.6, energy: 0.25, icon: '↘' },
  unpleasant_high: { label: 'Unpleasant · high energy', valence: -0.7, energy: 0.8, icon: '↖' },
  unpleasant_low: { label: 'Unpleasant · low energy', valence: -0.65, energy: 0.2, icon: '↙' },
};

function esc(value) {
  const node = document.createElement('span');
  node.textContent = value == null ? '' : String(value);
  return node.innerHTML;
}

function _familyLabel(family) {
  return FAMILY_META[family]?.label || family || 'Check-in';
}

function _localTimestamp() {
  const now = new Date();
  const offset = -now.getTimezoneOffset();
  const sign = offset >= 0 ? '+' : '-';
  const hh = String(Math.floor(Math.abs(offset) / 60)).padStart(2, '0');
  const mm = String(Math.abs(offset) % 60).padStart(2, '0');
  const local = new Date(now.getTime() - now.getTimezoneOffset() * 60000)
    .toISOString().slice(0, 19);
  return `${local}${sign}${hh}:${mm}`;
}

async function api(path, options = {}) {
  const response = await fetch(`/api/lotus${path}`, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  });
  let data = {};
  try { data = await response.json(); } catch (_) {}
  if (!response.ok) throw new Error(data.detail || 'Lotus request failed');
  return data;
}

function _setMessage(text, error = false) {
  const node = document.getElementById('lotus-message');
  if (!node) return;
  node.textContent = text || '';
  node.classList.toggle('is-error', error);
}

function _renderEmotionChoices(family) {
  const wrap = document.getElementById('lotus-emotions');
  if (!wrap) return;
  wrap.innerHTML = (EMOTIONS[family] || []).map(([value, label]) =>
    `<button type="button" class="lotus-emotion-chip" data-emotion="${value}">${label}</button>`
  ).join('');
  wrap.querySelectorAll('[data-emotion]').forEach((button) => {
    button.addEventListener('click', () => {
      wrap.querySelectorAll('[data-emotion]').forEach((item) => item.classList.remove('active'));
      button.classList.add('active');
      _selected = { family, emotion: button.dataset.emotion };
      document.getElementById('lotus-detail-step')?.classList.remove('lotus-disabled');
    });
  });
}

function _wireCheckinForm() {
  document.querySelectorAll('[data-lotus-family]').forEach((button) => {
    button.addEventListener('click', () => {
      document.querySelectorAll('[data-lotus-family]').forEach((item) => item.classList.remove('active'));
      button.classList.add('active');
      _selected = null;
      document.getElementById('lotus-detail-step')?.classList.add('lotus-disabled');
      _renderEmotionChoices(button.dataset.lotusFamily);
    });
  });
  const intensity = document.getElementById('lotus-intensity');
  intensity?.addEventListener('input', () => {
    const value = document.getElementById('lotus-intensity-value');
    if (value) value.textContent = intensity.value;
  });
  document.getElementById('lotus-checkin-form')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!_selected) {
      _setMessage('Choose an emotion before saving.', true);
      return;
    }
    const submit = document.getElementById('lotus-save-checkin');
    const meta = FAMILY_META[_selected.family];
    const tags = (document.getElementById('lotus-tags')?.value || '')
      .split(',').map((tag) => tag.trim()).filter(Boolean);
    submit.disabled = true;
    _setMessage('Saving…');
    try {
      await api('/checkins', {
        method: 'POST',
        body: JSON.stringify({
          occurred_at: _localTimestamp(),
          timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC',
          emotion_label: _selected.emotion,
          emotion_family: _selected.family,
          valence: meta.valence,
          energy: meta.energy,
          intensity: Number(document.getElementById('lotus-intensity').value) / 10,
          note: document.getElementById('lotus-note').value || null,
          tags,
          context: { entry_method: 'odysseus_ui' },
        }),
      });
      event.target.reset();
      document.getElementById('lotus-intensity-value').textContent = '5';
      document.querySelectorAll('[data-lotus-family]').forEach((item) => item.classList.remove('active'));
      document.getElementById('lotus-emotions').innerHTML = '<span class="lotus-hint">Choose an energy quadrant first.</span>';
      document.getElementById('lotus-detail-step')?.classList.add('lotus-disabled');
      _selected = null;
      _setMessage('Check-in saved.');
      await Promise.all([loadOverview(), loadHistory()]);
    } catch (error) {
      _setMessage(error.message, true);
    } finally {
      submit.disabled = false;
    }
  });
}

async function loadOverview() {
  try {
    const data = await api('/overview');
    const total = document.getElementById('lotus-total-count');
    const recent = document.getElementById('lotus-month-count');
    const last = document.getElementById('lotus-last-checkin');
    if (total) total.textContent = data.total_checkins;
    if (recent) recent.textContent = data.last_30_days;
    if (last) last.textContent = data.last_checkin
      ? `${data.last_checkin.emotion_label} · ${new Date(data.last_checkin.occurred_at).toLocaleString()}`
      : 'No check-ins yet';
  } catch (error) {
    _setMessage(error.message, true);
  }
}

async function loadHistory() {
  const list = document.getElementById('lotus-history-list');
  if (!list) return;
  list.innerHTML = '<div class="lotus-empty">Loading check-ins…</div>';
  try {
    const data = await api('/checkins?limit=100');
    if (!data.checkins.length) {
      list.innerHTML = '<div class="lotus-empty">Your check-ins will appear here.</div>';
      return;
    }
    list.innerHTML = data.checkins.map((entry) => `
      <article class="lotus-history-card">
        <div class="lotus-history-main">
          <span class="lotus-history-emotion">${esc(entry.emotion_label)}</span>
          <span class="lotus-family-pill ${esc(entry.emotion_family)}">${esc(_familyLabel(entry.emotion_family))}</span>
          <time>${esc(new Date(entry.occurred_at).toLocaleString())}</time>
        </div>
        <div class="lotus-history-meta">Intensity ${Math.round((entry.intensity || 0) * 10)}/10${entry.tags?.length ? ` · ${entry.tags.map(esc).join(', ')}` : ''}</div>
        ${entry.note ? `<p>${esc(entry.note)}</p>` : ''}
        <button type="button" class="lotus-delete" data-delete-checkin="${esc(entry.id)}" aria-label="Delete check-in">Delete</button>
      </article>
    `).join('');
    list.querySelectorAll('[data-delete-checkin]').forEach((button) => {
      button.addEventListener('click', async () => {
        if (!window.confirm('Delete this check-in?')) return;
        try {
          await api(`/checkins/${encodeURIComponent(button.dataset.deleteCheckin)}`, { method: 'DELETE' });
          await Promise.all([loadOverview(), loadHistory()]);
        } catch (error) {
          _setMessage(error.message, true);
        }
      });
    });
  } catch (error) {
    list.innerHTML = `<div class="lotus-empty is-error">${esc(error.message)}</div>`;
  }
}

async function loadPreferences() {
  try {
    const prefs = await api('/preferences');
    document.getElementById('lotus-reminder-enabled').checked = prefs.reminder_enabled;
    document.getElementById('lotus-reminder-time').value = prefs.reminder_times?.[0] || '20:00';
    document.getElementById('lotus-timezone').value = prefs.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  } catch (error) {
    _setMessage(error.message, true);
  }
}

function _wirePreferences() {
  document.getElementById('lotus-reminder-form')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const enabled = document.getElementById('lotus-reminder-enabled').checked;
    const time = document.getElementById('lotus-reminder-time').value;
    try {
      await api('/preferences', {
        method: 'PUT',
        body: JSON.stringify({
          timezone: document.getElementById('lotus-timezone').value || 'UTC',
          reminder_enabled: enabled,
          reminder_times: enabled && time ? [time] : [],
          reminder_weekdays: [0, 1, 2, 3, 4, 5, 6],
          quiet_start: null,
          quiet_end: null,
          snooze_minutes: 30,
        }),
      });
      _setMessage('Reminder preference saved. Notification delivery will be enabled in a later milestone.');
    } catch (error) {
      _setMessage(error.message, true);
    }
  });
}

function _wireTabs(modal) {
  modal.querySelectorAll('[data-lotus-tab]').forEach((button) => {
    button.addEventListener('click', () => {
      modal.querySelectorAll('[data-lotus-tab]').forEach((item) => item.classList.toggle('active', item === button));
      modal.querySelectorAll('[data-lotus-panel]').forEach((panel) => {
        panel.hidden = panel.dataset.lotusPanel !== button.dataset.lotusTab;
      });
      if (button.dataset.lotusTab === 'history') loadHistory();
      if (button.dataset.lotusTab === 'reminders') loadPreferences();
    });
  });
}

function _doClose() {
  document.getElementById('lotus-modal')?.remove();
  _open = false;
  _selected = null;
}

export function openLotus() {
  if (Modals.isRegistered('lotus-modal') && Modals.isMinimized('lotus-modal')) {
    Modals.restore('lotus-modal');
    return;
  }
  if (_open) return;
  _open = true;
  const modal = document.createElement('div');
  modal.className = 'modal';
  modal.id = 'lotus-modal';
  modal.innerHTML = `
    <div class="modal-content lotus-modal-content">
      <div class="modal-header">
        <h4><span class="lotus-mark">✦</span> Lotus <span class="lotus-subtitle">private daily check-ins</span></h4>
        <button class="modal-close" id="lotus-close" aria-label="Close Lotus">&times;</button>
      </div>
      <div class="lotus-stats">
        <div><strong id="lotus-total-count">0</strong><span>all check-ins</span></div>
        <div><strong id="lotus-month-count">0</strong><span>last 30 days</span></div>
        <div class="lotus-last"><strong>Latest</strong><span id="lotus-last-checkin">No check-ins yet</span></div>
      </div>
      <div class="lotus-tabs" role="tablist">
        <button class="active" data-lotus-tab="checkin">Check in</button>
        <button data-lotus-tab="history">History</button>
        <button data-lotus-tab="reminders">Reminders</button>
      </div>
      <div class="lotus-body">
        <section data-lotus-panel="checkin">
          <form id="lotus-checkin-form">
            <h2>How are you feeling right now?</h2>
            <p class="lotus-hint">Start with pleasantness and energy. There is no right answer.</p>
            <div class="lotus-family-grid">
              ${Object.entries(FAMILY_META).map(([key, meta]) => `<button type="button" class="lotus-family ${key}" data-lotus-family="${key}"><span>${meta.icon}</span>${meta.label}</button>`).join('')}
            </div>
            <div class="lotus-emotion-section">
              <label>Choose a word</label>
              <div id="lotus-emotions" class="lotus-emotions"><span class="lotus-hint">Choose an energy quadrant first.</span></div>
            </div>
            <div id="lotus-detail-step" class="lotus-detail-step lotus-disabled">
              <label for="lotus-intensity">How strongly? <strong id="lotus-intensity-value">5</strong>/10</label>
              <input id="lotus-intensity" type="range" min="1" max="10" value="5">
              <label for="lotus-tags">Context <span>(optional, comma separated)</span></label>
              <input id="lotus-tags" maxlength="300" placeholder="work, family, sleep">
              <label for="lotus-note">Private note <span>(optional)</span></label>
              <textarea id="lotus-note" maxlength="8000" rows="3" placeholder="What is shaping this feeling?"></textarea>
              <button id="lotus-save-checkin" class="lotus-primary" type="submit">Save check-in</button>
            </div>
          </form>
        </section>
        <section data-lotus-panel="history" hidden>
          <div id="lotus-history-list" class="lotus-history-list"></div>
        </section>
        <section data-lotus-panel="reminders" hidden>
          <form id="lotus-reminder-form" class="lotus-reminder-form">
            <h2>Daily reminder</h2>
            <p class="lotus-hint">Your schedule is saved now. In-app, ntfy, and phone delivery will be connected in a later milestone.</p>
            <label class="lotus-toggle"><input id="lotus-reminder-enabled" type="checkbox"><span>Remind me to check in</span></label>
            <label for="lotus-reminder-time">Reminder time</label>
            <input id="lotus-reminder-time" type="time" value="20:00">
            <label for="lotus-timezone">Timezone</label>
            <input id="lotus-timezone" maxlength="64">
            <button class="lotus-primary" type="submit">Save reminder preference</button>
          </form>
        </section>
      </div>
      <div id="lotus-message" role="status" aria-live="polite"></div>
    </div>`;
  document.body.appendChild(modal);
  Modals.register('lotus-modal', {
    sidebarBtnId: 'tool-lotus-btn',
    closeFn: _doClose,
    restoreFn: () => {},
  });
  makeWindowDraggable(modal, {
    content: modal.querySelector('.lotus-modal-content'),
    header: modal.querySelector('.modal-header'),
    minWidth: 420,
    minHeight: 480,
  });
  document.getElementById('lotus-close').addEventListener('click', _doClose);
  _wireTabs(modal);
  _wireCheckinForm();
  _wirePreferences();
  loadOverview();
}

export function closeLotus() { _doClose(); }
export function isLotusOpen() { return _open; }

export default { openLotus, closeLotus, isLotusOpen };
