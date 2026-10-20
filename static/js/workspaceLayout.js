/** One geometry owner for multi-tool workspaces. Legacy two-pane docks remain
 * untouched until a third section opens. Extra tools stay open behind tabs,
 * not closed/minimized, so drafts and live jobs survive switching.
 *
 * The person controls the grid: the gutters between columns and rows drag
 * (double-click resets them), and each tab can minimize its section (it stays
 * open, the rest take its room) or maximize it (the only one shown). */
const TOOL_IDS = ['agents-dashboard', 'workbench-modal', 'email-lib-modal',
  'doclib-modal', 'memory-modal', 'tasks-modal',
  'calendar-modal', 'gallery-modal', 'research-overlay', 'skills-modal'];
let active = null;
let explicitFocus = null;
let bar = null;
let frame = 0;
let previous = new Set();
let previousFullscreen = null;
const managed = new Set();
// Sections the person minimized or maximized from the tab strip. Elements, so
// two tools with the same title stay apart; dropped when the element goes.
const minimized = new Set();
let maximized = null;
let gutters = null;
let dragging = null;

const SPLIT_KEY = 'odysseus-workspace-split';
const MIN_RATIO = 0.2, MAX_RATIO = 0.8;
const clampRatio = (v) => (Number.isFinite(v) ? Math.min(MAX_RATIO, Math.max(MIN_RATIO, v)) : 0.5);
function loadSplit() {
  try {
    const saved = JSON.parse(localStorage.getItem(SPLIT_KEY) || '{}');
    return { col: clampRatio(Number(saved.col ?? 0.5)), row: clampRatio(Number(saved.row ?? 0.5)) };
  } catch (_) { return { col: 0.5, row: 0.5 }; }
}
const split = loadSplit();
function saveSplit() { try { localStorage.setItem(SPLIT_KEY, JSON.stringify(split)); } catch (_) {} }

/** Rectangles for `count` sections: one, two side by side, three (two over
 *  one) or four (2x2). `ratios.col` is the left column's share of the width,
 *  `ratios.row` the top row's share of the height. */
export function sectionRects(count, width, height, left = 0, ratios = { col: 0.5, row: 0.5 }) {
  const narrow = width < 680 || height < 460;
  if (narrow || count <= 1) return [{ left, top: 42, width, height: Math.max(0, height - 42) }];
  const n = Math.min(4, count);
  const body = height - 42;
  const w0 = Math.round(width * clampRatio(ratios.col));
  const h0 = n > 2 ? Math.round(body * clampRatio(ratios.row)) : body;
  return Array.from({ length: n }, (_, i) => {
    const second = i % 2 === 1, bottom = i >= 2;
    return {
      left: left + (second ? w0 : 0), top: 42 + (bottom ? h0 : 0),
      width: n === 3 && i === 2 ? width : (second ? width - w0 : w0),
      height: bottom ? body - h0 : h0,
    };
  });
}
function available(el) {
  return el && !el.closest('.hidden, .modal-minimized')
    && getComputedStyle(el).display !== 'none';
}
function surfaces() {
  const chat = document.getElementById('chat-container');
  // Legacy mobile document styles hide chat; it is still an open section.
  const result = chat && !chat.closest('.hidden') ? [{ el: chat, owner: chat, name: 'Chat' }] : [];
  for (const owner of document.querySelectorAll(['.doc-editor-pane', '#notes-pane', '.email-window-modal', ...TOOL_IDS.map(id => `#${id}`)].join(','))) {
    if (!available(owner)) continue;
    const el = owner.matches('.doc-editor-pane, #notes-pane') ? owner
      : owner.querySelector('.modal-content, .research-pane');
    if (!el || result.some(s => s.el === el)) continue;
    result.push({ el, owner, name: owner.querySelector('.modal-title, .notes-pane-title, .doc-title')?.textContent?.trim()
      || ({ 'agents-dashboard': 'Phalanx', 'workbench-modal': 'Workbench', 'notes-pane': 'Notes' })[owner.id]
      || (owner.matches('.doc-editor-pane') ? 'Document' : owner.id.replace(/-modal|-overlay|-lib/g, '').replaceAll('-', ' ')) });
  }
  return result;
}
function clear() {
  for (const el of managed) {
    el.classList.remove('workspace-section', 'workspace-parked', 'workspace-owner');
    for (const p of ['--section-left', '--section-top', '--section-width', '--section-height']) el.style.removeProperty(p);
  }
  managed.clear();
  minimized.clear(); maximized = null;
  if (document.body.classList.contains('workspace-split')) document.body.classList.remove('workspace-split');
  bar?.remove(); bar = null;
  gutters?.col.remove(); gutters?.row.remove(); gutters = null;
}
function mark(el, cls) { if (!el.classList.contains(cls)) el.classList.add(cls); managed.add(el); }
export function layoutWorkspace() {
  const all = surfaces();
  const opened = all.filter(s => !previous.has(s.el));
  previous = new Set(all.map(s => s.el));
  if (explicitFocus && all.some(s => s.el === explicitFocus)) active = explicitFocus;
  else if (opened.length) active = opened.at(-1).el;
  explicitFocus = null;
  if (all.length < 3) { clear(); return; }
  for (const el of [...minimized]) if (!all.some(s => s.el === el)) minimized.delete(el);
  if (maximized && !all.some(s => s.el === maximized)) maximized = null;
  // A tool opened just now is shown, even where it had been minimized before.
  for (const s of opened) minimized.delete(s.el);
  if (!all.some(s => s.el === active) || minimized.has(active)) {
    active = (all.filter(s => !minimized.has(s.el)).at(-1) || all.at(-1)).el;
  }
  minimized.delete(active);
  if (maximized && maximized !== active) maximized = null;
  const nav = [document.getElementById('sidebar'), document.querySelector('.icon-rail')];
  const left = window.innerWidth <= 768 ? 0 : Math.max(0, ...nav.filter(available).map(el => el.getBoundingClientRect().right));
  const width = Math.max(0, window.innerWidth - left);
  const fullscreen = all.find(s => [...s.el.classList, ...s.owner.classList].some(c => c.endsWith('-fullscreen')));
  if (fullscreen && fullscreen.el !== previousFullscreen) active = fullscreen.el;
  previousFullscreen = fullscreen?.el || null;
  const pool = all.filter(s => !minimized.has(s.el));
  const single = fullscreen?.el === active || maximized === active;
  const rects = single ? [{ left, top: 42, width, height: window.innerHeight - 42 }]
    : sectionRects(pool.length, width, window.innerHeight, left, split);
  const visible = rects.length === 1 ? pool.filter(s => s.el === active)
    : [pool[0], ...pool.slice(1).filter(s => s.el !== active).slice(-(active === pool[0].el ? 3 : 2)), ...pool.slice(1).filter(s => s.el === active)].slice(0, 4);
  // Remove geometry from closed/removed windows, without clearing live dock state.
  for (const el of [...managed]) if (!all.some(s => s.el === el || s.owner === el)) {
    el.classList.remove('workspace-section', 'workspace-parked', 'workspace-owner'); managed.delete(el);
  }
  if (!document.body.classList.contains('workspace-split')) document.body.classList.add('workspace-split');
  all.forEach(s => {
    mark(s.el, 'workspace-section');
    if (s.el.classList.contains('workspace-parked') !== !visible.includes(s)) s.el.classList.toggle('workspace-parked', !visible.includes(s));
    if (s.owner !== s.el) mark(s.owner, 'workspace-owner');
  });
  visible.forEach((s, i) => {
    const r = rects[i];
    for (const key of ['left', 'top', 'width', 'height']) {
      const prop = `--section-${key}`, value = `${r[key]}px`;
      if (s.el.style.getPropertyValue(prop) !== value) s.el.style.setProperty(prop, value);
    }
  });
  if (!bar) { bar = document.createElement('nav'); bar.id = 'workspace-section-tabs'; bar.setAttribute('aria-label', 'Open workspace sections'); document.body.appendChild(bar); }
  bar.style.left = `${left}px`;
  const signature = all.map(s => `${s.name}:${s.el === active}:${visible.includes(s)}:${minimized.has(s.el)}:${maximized === s.el}`).join('|');
  if (bar.dataset.signature !== signature) {
    bar.dataset.signature = signature;
    bar.replaceChildren(...all.map(s => sectionTab(s, visible.includes(s))));
  }
  placeGutters(rects, visible.length, left, width);
}

const ICON_MIN = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M5 12h14"/></svg>';
const ICON_MAX = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="4" width="16" height="16" rx="2"/></svg>';
const ICON_RESTORE = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="8" width="13" height="13" rx="2"/><path d="M8 8V5a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2h-3"/></svg>';

function iconButton(label, icon, onClick) {
  const b = document.createElement('button');
  b.type = 'button'; b.className = 'ws-tab-ctl'; b.innerHTML = icon;
  b.title = label; b.setAttribute('aria-label', label);
  b.onclick = (e) => { e.stopPropagation(); onClick(); };
  return b;
}

function sectionTab(s, shown) {
  const isMin = minimized.has(s.el), isMax = maximized === s.el;
  const tab = document.createElement('div');
  tab.className = 'ws-tab' + (isMin ? ' is-minimized' : '') + (isMax ? ' is-maximized' : '');
  const btn = document.createElement('button'); btn.type = 'button'; btn.textContent = s.name;
  btn.dataset.sectionTab = '';
  btn.setAttribute('aria-pressed', String(s.el === active));
  btn.title = isMin ? `Restore ${s.name}` : shown ? `Focus ${s.name}` : `Show ${s.name}`;
  btn.onclick = () => { minimized.delete(s.el); active = s.el; layoutWorkspace(); s.el.querySelector('textarea, input, button')?.focus(); };
  tab.append(btn);
  if (isMax) {
    tab.append(iconButton('Show all sections', ICON_RESTORE, () => { maximized = null; layoutWorkspace(); }));
  } else if (!isMin) {
    tab.append(iconButton(`Maximize ${s.name}`, ICON_MAX, () => { maximized = s.el; active = s.el; layoutWorkspace(); }));
    tab.append(iconButton(`Minimize ${s.name}`, ICON_MIN, () => minimizeSection(s.el)));
  }
  return tab;
}

function minimizeSection(el) {
  const others = surfaces().filter(s => s.el !== el && !minimized.has(s.el));
  if (!others.length) return;   // the last shown section stays
  minimized.add(el);
  if (maximized === el) maximized = null;
  if (active === el) active = others.at(-1).el;
  layoutWorkspace();
}

// ── gutters ────────────────────────────────────────────────────────────────
function makeGutter(axis) {
  const g = document.createElement('div');
  g.className = `workspace-gutter workspace-gutter-${axis}`;
  g.setAttribute('role', 'separator');
  g.setAttribute('aria-orientation', axis === 'col' ? 'vertical' : 'horizontal');
  g.setAttribute('aria-label', axis === 'col' ? 'Resize columns' : 'Resize rows');
  g.title = 'Drag to resize. Double-click to reset.';
  g.tabIndex = 0;
  g.addEventListener('pointerdown', (e) => startDrag(e, axis, g));
  g.addEventListener('dblclick', () => { split[axis] = 0.5; saveSplit(); layoutWorkspace(); });
  g.addEventListener('keydown', (e) => {
    const step = { ArrowLeft: -0.02, ArrowUp: -0.02, ArrowRight: 0.02, ArrowDown: 0.02 }[e.key];
    if (step == null) return;
    e.preventDefault();
    split[axis] = clampRatio(split[axis] + step); saveSplit(); layoutWorkspace();
  });
  document.body.appendChild(g);
  return g;
}

function placeGutters(rects, count, left, width) {
  if (!gutters) gutters = { col: makeGutter('col'), row: makeGutter('row') };
  const multi = rects.length > 1 && count > 1;
  const col = multi && rects[1];
  if (gutters.col.hidden !== !col) gutters.col.hidden = !col;
  if (col) {
    const h = count === 3 ? rects[0].height : window.innerHeight - 42;
    Object.assign(gutters.col.style, { left: `${col.left - 4}px`, top: '42px', height: `${h}px` });
  }
  const row = multi && count > 2 && rects[2];
  if (gutters.row.hidden !== !row) gutters.row.hidden = !row;
  if (row) Object.assign(gutters.row.style, { top: `${row.top - 4}px`, left: `${left}px`, width: `${width}px` });
  gutters.bounds = { left, width, top: 42, height: window.innerHeight - 42 };
}

function startDrag(e, axis, g) {
  if (e.button !== 0 || !gutters?.bounds) return;
  e.preventDefault();
  const b = gutters.bounds;
  dragging = axis;
  g.setPointerCapture?.(e.pointerId);
  document.body.classList.add('workspace-resizing', `workspace-resizing-${axis}`);
  const move = (ev) => {
    split[axis] = clampRatio(axis === 'col' ? (ev.clientX - b.left) / b.width : (ev.clientY - b.top) / b.height);
    layoutWorkspace();
  };
  const up = () => {
    g.removeEventListener('pointermove', move);
    g.removeEventListener('pointerup', up);
    g.removeEventListener('pointercancel', up);
    document.body.classList.remove('workspace-resizing', `workspace-resizing-${axis}`);
    dragging = null;
    saveSplit();
    window.dispatchEvent(new Event('resize'));   // editors and lists re-measure once
  };
  g.addEventListener('pointermove', move);
  g.addEventListener('pointerup', up);
  g.addEventListener('pointercancel', up);
}
export function initWorkspaceLayout() {
  const schedule = () => { if (!frame) frame = requestAnimationFrame(() => { frame = 0; layoutWorkspace(); }); };
  new MutationObserver(records => {
    if (dragging) return;
    if (records.some(r => !r.target.closest?.('#workspace-section-tabs, .workspace-gutter') &&
      (r.type === 'childList' || r.attributeName === 'class'))) schedule();
  }).observe(document.body, { subtree: true, childList: true, attributes: true, attributeFilter: ['class'] });
  window.addEventListener('resize', schedule);
  window.addEventListener('odysseus:modal-opened', e => {
    const owner = e.detail?.modal || document.getElementById(e.detail?.id);
    const section = owner?.matches('.doc-editor-pane, #notes-pane') ? owner
      : owner?.querySelector('.modal-content, .research-pane');
    if (section) { active = section; explicitFocus = section; }
    schedule();
  });
  schedule();
}
if (typeof document !== 'undefined') {
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initWorkspaceLayout, { once: true });
  else initWorkspaceLayout();
}
