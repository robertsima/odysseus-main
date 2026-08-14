import * as Modals from './modalManager.js';
import { makeWindowDraggable } from './windowDrag.js';

let _open = false;
let _selected = null;
let _pausedUntil = null;

// Server caps reminder_times at 8 (ReminderPreferences.max_length).
const MAX_REMINDER_TIMES = 8;
// Index order matches Python's datetime.weekday(): Monday is 0.
const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
// Values mirror src/reminder_personas.py; 'plain' means no LLM rewriting.
const MESSAGE_STYLES = [
  ['plain', 'Plain (no AI phrasing)'],
  ['spark', 'Spark — bright and playful'],
  ['razor', 'Razor — blunt and minimal'],
  ['odysseus', 'Odysseus — composed and noble'],
  ['socrates', 'Socrates — only questions'],
  ['nietzsche', 'Nietzsche — aphoristic'],
];

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

function _renderReminderTimes(times) {
  const wrap = document.getElementById('lotus-times');
  if (!wrap) return;
  const values = (times && times.length ? times : ['20:00']).slice(0, MAX_REMINDER_TIMES);
  wrap.innerHTML = values.map((value, index) => `
    <div class="lotus-time-row">
      <input type="time" class="lotus-time" value="${esc(value)}">
      <button type="button" class="lotus-mini" data-remove-time="${index}" aria-label="Remove reminder time"${values.length === 1 ? ' disabled' : ''}>&times;</button>
    </div>`).join('');
  wrap.querySelectorAll('[data-remove-time]').forEach((button) => {
    button.addEventListener('click', () => {
      const current = _collectTimes();
      current.splice(Number(button.dataset.removeTime), 1);
      _renderReminderTimes(current);
    });
  });
  const add = document.getElementById('lotus-add-time');
  if (add) add.disabled = values.length >= MAX_REMINDER_TIMES;
}

function _collectTimes() {
  return Array.from(document.querySelectorAll('#lotus-times .lotus-time'))
    .map((input) => input.value)
    .filter(Boolean);
}

function _renderWeekdays(selected) {
  const wrap = document.getElementById('lotus-weekdays');
  if (!wrap) return;
  const chosen = new Set((selected || [0, 1, 2, 3, 4, 5, 6]).map(Number));
  wrap.innerHTML = WEEKDAYS.map((label, index) =>
    `<label class="lotus-weekday"><input type="checkbox" value="${index}"${chosen.has(index) ? ' checked' : ''}><span>${label}</span></label>`
  ).join('');
}

function _prefsFromForm() {
  const enabled = document.getElementById('lotus-reminder-enabled').checked;
  const weekdays = Array.from(document.querySelectorAll('#lotus-weekdays input:checked'))
    .map((input) => Number(input.value));
  const times = _collectTimes();
  return {
    timezone: document.getElementById('lotus-timezone').value.trim() || 'UTC',
    reminder_enabled: enabled,
    reminder_times: times,
    reminder_weekdays: weekdays.length ? weekdays : [0, 1, 2, 3, 4, 5, 6],
    quiet_start: document.getElementById('lotus-quiet-start').value || null,
    quiet_end: document.getElementById('lotus-quiet-end').value || null,
    snooze_minutes: Number(document.getElementById('lotus-snooze-minutes').value) || 30,
    channel: document.getElementById('lotus-channel').value,
    min_hours_between: Number(document.getElementById('lotus-min-hours').value) || 0,
    skip_if_checked_in: document.getElementById('lotus-skip-checked-in').checked,
    message_style: document.getElementById('lotus-message-style').value,
    paused_until: _pausedUntil,
    insights_enabled: document.getElementById('lotus-insights-enabled').checked,
    insights_frequency: document.getElementById('lotus-insights-frequency').value,
    insights_weekday: Number(document.getElementById('lotus-insights-weekday').value),
    insights_time: document.getElementById('lotus-insights-time').value || '09:00',
  };
}

function _renderPauseState() {
  const node = document.getElementById('lotus-pause-state');
  if (!node) return;
  if (!_pausedUntil) {
    node.textContent = 'Not snoozed.';
  } else {
    node.textContent = `Snoozed until ${new Date(_pausedUntil).toLocaleString()}.`;
  }
  const clear = document.getElementById('lotus-clear-snooze');
  if (clear) clear.hidden = !_pausedUntil;
}

async function loadPreferences() {
  try {
    const prefs = await api('/preferences');
    _pausedUntil = prefs.paused_until || null;
    document.getElementById('lotus-reminder-enabled').checked = !!prefs.reminder_enabled;
    document.getElementById('lotus-timezone').value = prefs.timezone
      || Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
    _renderReminderTimes(prefs.reminder_times);
    _renderWeekdays(prefs.reminder_weekdays);
    document.getElementById('lotus-quiet-start').value = prefs.quiet_start || '';
    document.getElementById('lotus-quiet-end').value = prefs.quiet_end || '';
    document.getElementById('lotus-min-hours').value = prefs.min_hours_between ?? 0;
    document.getElementById('lotus-skip-checked-in').checked = prefs.skip_if_checked_in !== false;
    document.getElementById('lotus-channel').value = prefs.channel || 'inherit';
    document.getElementById('lotus-message-style').value = prefs.message_style || 'plain';
    document.getElementById('lotus-snooze-minutes').value = prefs.snooze_minutes ?? 30;
    document.getElementById('lotus-insights-enabled').checked = !!prefs.insights_enabled;
    document.getElementById('lotus-insights-frequency').value = prefs.insights_frequency || 'weekly';
    document.getElementById('lotus-insights-weekday').value = String(prefs.insights_weekday ?? 6);
    document.getElementById('lotus-insights-time').value = prefs.insights_time || '09:00';
    _renderPauseState();
  } catch (error) {
    _setMessage(error.message, true);
  }
}

async function loadNotifications() {
  const list = document.getElementById('lotus-notification-list');
  if (!list) return;
  list.innerHTML = '<div class="lotus-empty">Loading…</div>';
  try {
    const data = await api('/notifications?limit=25');
    if (!data.notifications.length) {
      list.innerHTML = '<div class="lotus-empty">Nothing has been sent yet.</div>';
      return;
    }
    list.innerHTML = data.notifications.map((entry) => `
      <div class="lotus-notification${entry.delivered ? '' : ' is-failed'}">
        <div><strong>${esc(entry.title)}</strong> <span class="lotus-pill">${esc(entry.kind)}</span> <span class="lotus-pill">${esc(entry.channel)}</span></div>
        <time>${esc(new Date(entry.created_at).toLocaleString())}${entry.delivered ? '' : ' · not delivered'}</time>
        <p>${esc(entry.body)}</p>
      </div>`).join('');
  } catch (error) {
    list.innerHTML = `<div class="lotus-empty is-error">${esc(error.message)}</div>`;
  }
}

function _wirePreferences() {
  document.getElementById('lotus-add-time')?.addEventListener('click', () => {
    const times = _collectTimes();
    if (times.length >= MAX_REMINDER_TIMES) return;
    times.push('09:00');
    _renderReminderTimes(times);
  });

  document.getElementById('lotus-reminder-form')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    try {
      await api('/preferences', { method: 'PUT', body: JSON.stringify(_prefsFromForm()) });
      _setMessage('Reminder settings saved.');
      await loadPreferences();
    } catch (error) {
      _setMessage(error.message, true);
    }
  });

  document.getElementById('lotus-test-notification')?.addEventListener('click', async (event) => {
    event.preventDefault();
    const button = event.currentTarget;
    button.disabled = true;
    _setMessage('Sending a test notification…');
    try {
      const result = await api('/preferences/test', { method: 'POST' });
      _setMessage(result.delivered
        ? `Test notification sent via ${result.channel}.`
        : `Could not deliver via ${result.channel}. Check that channel's settings.`, !result.delivered);
      await loadNotifications();
    } catch (error) {
      _setMessage(error.message, true);
    } finally {
      button.disabled = false;
    }
  });

  document.getElementById('lotus-snooze')?.addEventListener('click', async (event) => {
    event.preventDefault();
    try {
      const minutes = Number(document.getElementById('lotus-snooze-minutes').value) || 30;
      const result = await api('/snooze', { method: 'POST', body: JSON.stringify({ minutes }) });
      _pausedUntil = result.paused_until;
      _renderPauseState();
      _setMessage(`Snoozed for ${minutes} minutes.`);
    } catch (error) {
      _setMessage(error.message, true);
    }
  });

  document.getElementById('lotus-clear-snooze')?.addEventListener('click', async (event) => {
    event.preventDefault();
    try {
      await api('/snooze', { method: 'DELETE' });
      _pausedUntil = null;
      _renderPauseState();
      _setMessage('Snooze cleared.');
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
      if (button.dataset.lotusTab === 'reminders') {
        loadPreferences();
        loadNotifications();
      }
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
            <h2>Reminders</h2>
            <p class="lotus-hint">Nudges are delivered in-app or through the email / ntfy / webhook channel you pick. External channels receive the notification text, but never your private check-in notes.</p>
            <label class="lotus-toggle"><input id="lotus-reminder-enabled" type="checkbox"><span>Remind me to check in</span></label>

            <label>Reminder times <span>(up to ${MAX_REMINDER_TIMES})</span></label>
            <div id="lotus-times" class="lotus-times"></div>
            <button id="lotus-add-time" type="button" class="lotus-mini lotus-add">+ Add a time</button>

            <label>Days</label>
            <div id="lotus-weekdays" class="lotus-weekdays"></div>

            <div class="lotus-grid-2">
              <div>
                <label for="lotus-quiet-start">Quiet hours from</label>
                <input id="lotus-quiet-start" type="time">
              </div>
              <div>
                <label for="lotus-quiet-end">until</label>
                <input id="lotus-quiet-end" type="time">
              </div>
            </div>
            <p class="lotus-hint">A quiet window may cross midnight (22:00 → 07:00).</p>

            <div class="lotus-grid-2">
              <div>
                <label for="lotus-min-hours">Minimum hours between nudges</label>
                <input id="lotus-min-hours" type="number" min="0" max="168" step="1" value="0">
              </div>
              <div>
                <label for="lotus-channel">Deliver via</label>
                <select id="lotus-channel">
                  <option value="inherit">App default</option>
                  <option value="browser">In-app / browser</option>
                  <option value="email">Email</option>
                  <option value="ntfy">ntfy</option>
                  <option value="webhook">Webhook</option>
                </select>
              </div>
            </div>

            <label class="lotus-toggle"><input id="lotus-skip-checked-in" type="checkbox"><span>Skip the nudge if I already checked in that day</span></label>

            <label for="lotus-message-style">Message style</label>
            <select id="lotus-message-style">
              ${MESSAGE_STYLES.map(([value, label]) => `<option value="${value}">${label}</option>`).join('')}
            </select>
            <p class="lotus-hint">AI phrasing follows your Lotus model-access policy in Settings &gt; Privacy; otherwise the plain wording is used.</p>

            <h2 class="lotus-section-title">Observations</h2>
            <p class="lotus-hint">A periodic summary of what your check-ins contain — counts, averages, and energy by time of day, always with the sample size behind them.</p>
            <label class="lotus-toggle"><input id="lotus-insights-enabled" type="checkbox"><span>Send me periodic observations</span></label>
            <div class="lotus-grid-3">
              <div>
                <label for="lotus-insights-frequency">How often</label>
                <select id="lotus-insights-frequency">
                  <option value="weekly">Weekly</option>
                  <option value="biweekly">Every 2 weeks</option>
                  <option value="monthly">Monthly</option>
                </select>
              </div>
              <div>
                <label for="lotus-insights-weekday">On</label>
                <select id="lotus-insights-weekday">
                  ${WEEKDAYS.map((label, index) => `<option value="${index}">${label}</option>`).join('')}
                </select>
              </div>
              <div>
                <label for="lotus-insights-time">At</label>
                <input id="lotus-insights-time" type="time" value="09:00">
              </div>
            </div>

            <label for="lotus-timezone">Timezone</label>
            <input id="lotus-timezone" maxlength="64" placeholder="Europe/Warsaw">

            <div class="lotus-actions">
              <button class="lotus-primary" type="submit">Save reminder settings</button>
              <button id="lotus-test-notification" type="button" class="lotus-secondary">Send test notification</button>
            </div>
          </form>

          <div class="lotus-snooze-card">
            <label for="lotus-snooze-minutes">Pause reminders for</label>
            <div class="lotus-snooze-row">
              <input id="lotus-snooze-minutes" type="number" min="5" max="10080" step="5" value="30">
              <span>minutes</span>
              <button id="lotus-snooze" type="button" class="lotus-secondary">Snooze</button>
              <button id="lotus-clear-snooze" type="button" class="lotus-mini" hidden>Resume now</button>
            </div>
            <p id="lotus-pause-state" class="lotus-hint">Not snoozed.</p>
          </div>

          <h2 class="lotus-section-title">Recently sent</h2>
          <div id="lotus-notification-list" class="lotus-notification-list"></div>
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
