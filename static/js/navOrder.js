// Shared navigation order for the icon rail and the sidebar Tools list.
//
// The module deliberately owns no tool click handlers. It only moves the
// existing DOM nodes, so every tool keeps the listeners installed by app.js
// and its feature module. Importing this file is enough to initialize it; the
// exported API is useful to the Settings/UI customizer and for tests.
//
// The order belongs to the signed-in account, not to the browser. localStorage
// stays the fast path so the rail is in the right order on first paint, but the
// authoritative copy lives in /api/prefs so a second browser (or a new device)
// picks up the same arrangement. See serverPrefs.js for the reconcile rules.

import { reconcile, writePref } from './serverPrefs.js';

export const NAV_ORDER_KEY = 'odysseus-nav-order-v1';
export const NAV_ORDER_PREF = 'nav-order';

// Core controls, dynamic indicators, and Settings stay fixed. One-sided tools
// (Email on the rail, Lotus in the sidebar) still participate where present.
export const NAV_ITEMS = Object.freeze([
  Object.freeze({ key: 'calendar', rail: 'rail-calendar', sidebar: 'tool-calendar-btn' }),
  Object.freeze({ key: 'compare', rail: 'rail-compare', sidebar: 'tool-compare-btn' }),
  Object.freeze({ key: 'cookbook', rail: 'rail-cookbook', sidebar: 'tool-cookbook-btn' }),
  Object.freeze({ key: 'research', rail: 'rail-research', sidebar: 'tool-research-btn' }),
  Object.freeze({ key: 'email', rail: 'rail-email', sidebar: null }),
  Object.freeze({ key: 'gallery', rail: 'rail-gallery', sidebar: 'tool-gallery-btn' }),
  Object.freeze({ key: 'library', rail: 'rail-archive', sidebar: 'tool-library-btn' }),
  Object.freeze({ key: 'memory', rail: 'rail-memory', sidebar: 'tool-memory-btn' }),
  Object.freeze({ key: 'lotus', rail: null, sidebar: 'tool-lotus-btn' }),
  Object.freeze({ key: 'agents', rail: 'rail-agents', sidebar: 'tool-agents-btn' }),
  Object.freeze({ key: 'workbench', rail: 'rail-workbench', sidebar: 'tool-workbench-btn' }),
  Object.freeze({ key: 'notes', rail: 'rail-notes', sidebar: 'tool-notes-btn' }),
  Object.freeze({ key: 'tasks', rail: 'rail-tasks', sidebar: 'tool-tasks-btn' }),
  Object.freeze({ key: 'theme', rail: 'rail-theme', sidebar: 'tool-theme-btn' }),
]);

const KEYS = new Set(NAV_ITEMS.map((item) => item.key));
const BY_RAIL = new Map(NAV_ITEMS.map((item) => [item.rail, item]));
const BY_SIDEBAR = new Map(NAV_ITEMS.map((item) => [item.sidebar, item]));
let _initialized = false;
let _dragKey = null;
let _suppressClickUntil = 0;

function storage() {
  try { return window.localStorage; } catch (_) { return null; }
}

function normalize(order) {
  const result = [];
  const seen = new Set();
  for (const key of Array.isArray(order) ? order : []) {
    const value = String(key);
    if (KEYS.has(value) && !seen.has(value)) { seen.add(value); result.push(value); }
  }
  for (const item of NAV_ITEMS) if (!seen.has(item.key)) result.push(item.key);
  return result;
}

export function defaultNavOrder() { return NAV_ITEMS.map((item) => item.key); }

// Stored locally as { value: [...keys], updated_at }. A bare array is what
// older builds wrote; it still reads, and counts as older than any stamped
// order so an arrangement made on another browser takes precedence over it.
function readLocalEntry() {
  const store = storage();
  if (!store) return null;
  try {
    const raw = JSON.parse(store.getItem(NAV_ORDER_KEY) || 'null');
    if (Array.isArray(raw)) return raw.length ? { value: raw, updated_at: 0 } : null;
    if (raw && Array.isArray(raw.value)) return { value: raw.value, updated_at: Number(raw.updated_at) || 0 };
  } catch (_) {}
  return null;
}

export function readNavOrder() {
  const entry = readLocalEntry();
  return entry ? normalize(entry.value) : defaultNavOrder();
}

/** Persist an order. `sync: false` records a value that came *from* the
 *  account, so applying it does not bounce straight back to the server. */
export function writeNavOrder(order, { sync = true, at } = {}) {
  const normalized = normalize(order);
  const stamped = Number(at) || Date.now();
  try { storage()?.setItem(NAV_ORDER_KEY, JSON.stringify({ value: normalized, updated_at: stamped })); } catch (_) {}
  if (sync) writePref(NAV_ORDER_PREF, normalized, stamped);
  return normalized;
}

/** Ask the account for its navigation order and adopt it when it is newer than
 *  what this browser last stored. Safe to call before or after the first paint;
 *  applyNavOrder only moves nodes, so re-running it is not disruptive. */
export async function syncNavOrderWithAccount(doc = document) {
  const local = readLocalEntry();
  const { source, entry } = await reconcile(NAV_ORDER_PREF, local);
  if (source !== 'remote' || !entry || !Array.isArray(entry.value)) return readNavOrder();
  const normalized = writeNavOrder(entry.value, { sync: false, at: entry.updated_at || Date.now() });
  applyNavOrder(normalized, doc);
  return normalized;
}

function nodeFor(container, id) {
  if (!container) return null;
  return Array.from(container.children).find((node) => node.id === id) || null;
}

function reorderContainer(container, ids, lookup) {
  if (!container) return;
  // Insert before a stable non-movable child (for example the rail spacer),
  // or append when the movable tools are the final children. Existing hidden
  // nodes are still moved; only their order changes, never their visibility.
  const desired = ids.map((key) => nodeFor(container, lookup(key))).filter(Boolean);
  if (!desired.length) return;
  const movableIds = new Set(desired.map((node) => node.id));
  const current = Array.from(container.children).filter((node) => movableIds.has(node.id));
  // Keep non-movable children (dynamic indicators, the rail spacer, Settings,
  // etc.) in their existing slots. This matters when a feature is
  // hidden or not available for the current account.
  const slots = current.map((node) => {
    const marker = container.ownerDocument.createComment('nav-order-slot');
    container.insertBefore(marker, node);
    node.remove();
    return marker;
  });
  desired.forEach((node, index) => {
    slots[index].parentNode.insertBefore(node, slots[index]);
    slots[index].remove();
  });
}

export function applyNavOrder(order = readNavOrder(), doc = document) {
  const normalized = normalize(order);
  const rail = doc.getElementById('icon-rail');
  const tools = doc.getElementById('tools-section');
  reorderContainer(rail, normalized, (key) => NAV_ITEMS.find((item) => item.key === key)?.rail);
  reorderContainer(tools, normalized, (key) => NAV_ITEMS.find((item) => item.key === key)?.sidebar);
  return normalized;
}

function keyFromNode(node) { return BY_RAIL.get(node?.id)?.key || BY_SIDEBAR.get(node?.id)?.key || null; }

function clearDragUi(doc) {
  doc.querySelectorAll('.nav-order-dragging, .nav-order-target').forEach((node) => node.classList.remove('nav-order-dragging', 'nav-order-target'));
  doc.getElementById('icon-rail')?.classList.remove('nav-order-active');
  doc.getElementById('tools-section')?.classList.remove('nav-order-active');
}

function move(key, delta, doc = document, root = doc.getElementById('icon-rail')) {
  const order = readNavOrder();
  const surface = root?.id === 'tools-section' ? 'sidebar' : 'rail';
  const represented = order.filter((value) => nodeFor(root, NAV_ITEMS.find((item) => item.key === value)?.[surface]));
  const position = represented.indexOf(key);
  const nextKey = represented[position + delta];
  if (position < 0 || !nextKey) return false;
  const index = order.indexOf(key);
  const next = order.indexOf(nextKey);
  [order[index], order[next]] = [order[next], order[index]];
  writeNavOrder(order);
  applyNavOrder(order, doc);
  const item = NAV_ITEMS.find((entry) => entry.key === key);
  doc.getElementById(item?.[surface])?.focus?.();
  return true;
}

function closeMenu(menu) { if (menu?.isConnected) menu.remove(); }

function showContextMenu(event, doc) {
  event.preventDefault();
  closeMenu(doc.getElementById('nav-order-context-menu'));
  const menu = doc.createElement('div');
  menu.id = 'nav-order-context-menu';
  menu.setAttribute('role', 'menu');
  Object.assign(menu.style, { position: 'fixed', zIndex: '10000', left: `${event.clientX}px`, top: `${event.clientY}px`, padding: '4px', background: 'var(--panel, #222)', color: 'var(--fg, #fff)', border: '1px solid var(--border, #555)', borderRadius: '6px', boxShadow: '0 6px 20px rgba(0,0,0,.35)' });
  const reset = doc.createElement('button');
  reset.type = 'button'; reset.textContent = 'Reset navigation order'; reset.setAttribute('role', 'menuitem');
  Object.assign(reset.style, { display: 'block', border: '0', background: 'transparent', color: 'inherit', padding: '6px 9px', cursor: 'pointer', font: 'inherit', whiteSpace: 'nowrap' });
  reset.addEventListener('click', () => { resetNavOrder(doc); closeMenu(menu); });
  menu.appendChild(reset); doc.body.appendChild(menu); reset.focus();
  setTimeout(() => doc.addEventListener('pointerdown', () => closeMenu(menu), { once: true }), 0);
}

export function resetNavOrder(doc = document) {
  const order = defaultNavOrder();
  // Record the reset rather than just dropping the local copy: the account
  // still holds the old arrangement, and an unrecorded reset would be undone
  // by the next page load (or by any other browser signed into this account).
  writeNavOrder(order);
  applyNavOrder(order, doc);
  return order;
}

export function initNavOrder(doc = document) {
  if (_initialized && doc === document) return;
  _initialized = true;
  const order = readNavOrder();
  applyNavOrder(order, doc);
  syncNavOrderWithAccount(doc).catch(() => {});
  const roots = [doc.getElementById('icon-rail'), doc.getElementById('tools-section')].filter(Boolean);
  for (const root of roots) {
    root.addEventListener('dragstart', (event) => {
      const node = event.target.closest?.('.icon-rail-btn, .list-item');
      const key = keyFromNode(node);
      if (!key) return;
      _dragKey = key; event.dataTransfer?.setData('text/plain', key);
      if (event.dataTransfer) event.dataTransfer.effectAllowed = 'move';
      node.classList.add('nav-order-dragging'); root.classList.add('nav-order-active');
    });
    root.addEventListener('dragover', (event) => {
      const target = event.target.closest?.('.icon-rail-btn, .list-item');
      if (_dragKey && keyFromNode(target)) {
        event.preventDefault();
        root.querySelectorAll('.nav-order-target').forEach((node) => node.classList.remove('nav-order-target'));
        target.classList.add('nav-order-target');
        if (event.dataTransfer) event.dataTransfer.dropEffect = 'move';
      }
    });
    root.addEventListener('drop', (event) => {
      const targetKey = keyFromNode(event.target.closest?.('.icon-rail-btn, .list-item'));
      if (!_dragKey || !targetKey || targetKey === _dragKey) return;
      event.preventDefault();
      const current = readNavOrder();
      const next = current.filter((key) => key !== _dragKey);
      next.splice(Math.max(0, next.indexOf(targetKey) + (current.indexOf(_dragKey) < current.indexOf(targetKey) ? 1 : 0)), 0, _dragKey);
      writeNavOrder(next); applyNavOrder(next, doc); _dragKey = null; clearDragUi(doc);
      // Browsers may synthesize a click after dropping a draggable button.
      // Briefly suppress it so reorganizing navigation never opens a tool.
      _suppressClickUntil = Date.now() + 300;
    });
    root.addEventListener('dragend', () => {
      _suppressClickUntil = Date.now() + 300;
      _dragKey = null; clearDragUi(doc);
    });
    root.addEventListener('click', (event) => {
      if (Date.now() >= _suppressClickUntil) return;
      if (!keyFromNode(event.target.closest?.('.icon-rail-btn, .list-item'))) return;
      event.preventDefault();
      event.stopImmediatePropagation();
    }, true);
    root.addEventListener('keydown', (event) => {
      if (!event.altKey || !['ArrowUp', 'ArrowDown'].includes(event.key)) return;
      const key = keyFromNode(event.target.closest?.('.icon-rail-btn, .list-item'));
      if (!key) return;
      event.preventDefault(); move(key, event.key === 'ArrowUp' ? -1 : 1, doc, root);
    });
    root.addEventListener('contextmenu', (event) => {
      if (keyFromNode(event.target.closest?.('.icon-rail-btn, .list-item'))) showContextMenu(event, doc);
    });
    for (const item of NAV_ITEMS) {
      for (const id of [item.rail, item.sidebar]) {
        const node = doc.getElementById(id);
        if (node) {
          node.draggable = true;
          node.dataset.navReorderable = 'true';
          node.setAttribute('aria-keyshortcuts', 'Alt+ArrowUp Alt+ArrowDown');
          const hint = 'Drag to reorder · Alt+↑/↓ · right-click to reset';
          if (!node.title.includes('Drag to reorder')) node.title = `${node.title || item.key} · ${hint}`;
        }
      }
    }
  }
}

if (typeof document !== 'undefined') {
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => initNavOrder());
  else initNavOrder();
}

export default { initNavOrder, readNavOrder, writeNavOrder, applyNavOrder, resetNavOrder, defaultNavOrder, syncNavOrderWithAccount, NAV_ORDER_KEY, NAV_ORDER_PREF, NAV_ITEMS };
