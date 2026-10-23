// The sidebar's navigation groups, drawn from the layout in navLayout.js.
//
// The module owns no tool click handlers. It moves the existing row nodes into
// the group they belong to (and builds a container for a group the person
// created), so every tool keeps the listeners app.js and its feature module
// installed. Importing this file initializes it. The collapsed rail's group
// icons and flyouts are navRail.js; the Edit navigation dialog is navEditor.js.
//
// Layout belongs to the signed-in account (see navLayout.js and serverPrefs.js).

import {
  NAV_GROUPS, NAV_ITEMS, defaultLayout, getLayout, subscribe, syncLayoutWithAccount, updateLayout,
  setGroupCollapsed, moveItem, moveGroupRelative, moveItemBy, moveGroupBy, renameGroup, createGroup,
  findGroup, resetLayout, readLayout, writeLayout, migrateFlatOrder,
} from './navLayout.js';
import { askText, isGroupContainerHidden, isRowShown, showMenu, sidebarNode } from './navDom.js';
import { initNavRail } from './navRail.js';
import { openNavEditor } from './navEditor.js';

export { NAV_GROUPS, NAV_ITEMS };

const ROW = '.list-item[data-nav-item]';
const CHEVRON = '<svg class="section-collapse-chevron" width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="6 9 12 15 18 9"/></svg>';
const DOTS = '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="5" cy="12" r="1.7"/><circle cx="12" cy="12" r="1.7"/><circle cx="19" cy="12" r="1.7"/></svg>';

const _detached = new WeakMap();
let _initialized = false;
let _drag = null;
let _suppressClickUntil = 0;

function detachedFor(doc) {
  if (!_detached.has(doc)) _detached.set(doc, new Map());
  return _detached.get(doc);
}

function groupElements(doc) {
  return Array.from(doc.querySelectorAll('.nav-group[data-nav-group]'));
}

function buildHeader(el, doc) {
  let header = el.querySelector(':scope > .section-header-flex');
  if (!header) {
    header = doc.createElement('div');
    el.insertBefore(header, el.firstChild);
  }
  if (header.dataset.navHeader === '1') return header;
  header.dataset.navHeader = '1';
  header.className = 'section-header-flex nav-group-header';
  header.draggable = true;
  header.innerHTML = `<button type="button" class="nav-group-toggle" aria-expanded="true"><span class="section-title nav-group-title"></span><span class="nav-group-dot" aria-hidden="true"></span>${CHEVRON}</button>`
    + `<button type="button" class="nav-group-menu-btn" aria-haspopup="menu" aria-expanded="false">${DOTS}</button>`;
  return header;
}

function buildGroupElement(group, doc) {
  const el = doc.createElement('div');
  el.className = 'section nav-group';
  el.id = NAV_GROUPS[group.id] || `nav-group-${group.id}`;
  el.dataset.navGroup = group.id;
  return el;
}

function elementFor(group, doc) {
  const live = groupElements(doc).find((el) => el.dataset.navGroup === group.id);
  if (live) return live;
  const cache = detachedFor(doc);
  if (cache.has(group.id)) return cache.get(group.id);
  return buildGroupElement(group, doc);
}

function decorateRow(node, key) {
  if (node.dataset.navItem === key) return;
  node.dataset.navItem = key;
  node.draggable = true;
  node.dataset.navReorderable = 'true';
  if (!node.title.includes('Drag to reorder')) node.title = node.title ? `${node.title} · Drag to reorder` : 'Drag to reorder';
}

function fillGroup(el, group, doc) {
  const header = buildHeader(el, doc);
  const title = header.querySelector('.nav-group-title');
  if (title.textContent !== group.label) title.textContent = group.label;
  const toggle = header.querySelector('.nav-group-toggle');
  toggle.setAttribute('aria-expanded', String(!group.collapsed));
  toggle.setAttribute('aria-label', group.label);
  header.querySelector('.nav-group-menu-btn').setAttribute('aria-label', `${group.label} options`);
  el.classList.toggle('collapsed', !!group.collapsed);

  let drop = el.querySelector(':scope > .nav-group-empty');
  if (!drop) {
    drop = doc.createElement('div');
    drop.className = 'nav-group-empty';
    drop.textContent = 'No tools yet. Drop one here.';
    el.appendChild(drop);
  }
  // Tools go before the empty-state line, which stays the last child. A node is
  // only moved when it is not already in its place, so focus and animations on
  // the rows survive a redraw.
  let ref = drop;
  const rows = group.items.map((key) => ({ key, node: sidebarNode(key, doc) })).filter((row) => row.node);
  for (let i = rows.length - 1; i >= 0; i -= 1) {
    const { key, node } = rows[i];
    decorateRow(node, key);
    if (node.parentNode !== el || node.nextSibling !== ref) el.insertBefore(node, ref);
    ref = node;
  }
  // A group is "empty" (and stays drawn, as a drop target) only when the layout
  // places nothing in it. A group whose tools are all hidden for this account
  // is hidden by nav-groups.css instead.
  el.dataset.empty = String(group.items.length === 0);
}

/** Draw the layout into the sidebar: create or retire group containers, order
 *  them, and put each tool's existing row in its group. */
export function applyNavLayout(layout = getLayout(), doc = document) {
  const existing = groupElements(doc);
  const parent = existing[0]?.parentNode || doc.querySelector('.sidebar-inner');
  if (!parent) return layout;
  const desired = layout.groups.map((group) => elementFor(group, doc));
  let ref = existing.length ? existing[existing.length - 1].nextSibling : null;
  // Fill first: a row still inside a group that is about to be retired is only
  // found while that group is in the page.
  layout.groups.forEach((group, index) => fillGroup(desired[index], group, doc));
  for (const el of existing) {
    if (desired.includes(el)) continue;
    detachedFor(doc).set(el.dataset.navGroup, el);
    el.remove();
  }
  for (let i = desired.length - 1; i >= 0; i -= 1) {
    const el = desired[i];
    if (el.parentNode !== parent || el.nextSibling !== ref) parent.insertBefore(el, ref);
    ref = el;
  }
  return layout;
}

// ── Group header actions ─────────────────────────────────────────────────────

function groupId(el) { return el?.closest?.('.nav-group')?.dataset.navGroup || null; }

function toggleGroup(id) {
  const group = findGroup(getLayout(), id);
  if (group) updateLayout((layout) => setGroupCollapsed(layout, id, !group.collapsed));
}

function shownGroup(doc) {
  return (group) => {
    const el = groupElements(doc).find((node) => node.dataset.navGroup === group.id);
    return !!el && !isGroupContainerHidden(el) && (group.items.length === 0 || group.items.some((key) => isRowShown(key, doc)));
  };
}

export async function promptNewGroup() {
  const label = await askText('Name the group.', { title: 'New group', placeholder: 'For example, Clients', confirmText: 'Create', maxLength: 32 });
  if (!label || !label.trim()) return null;
  let created = null;
  updateLayout((layout) => {
    const made = createGroup(layout, { label });
    created = made.id;
    return made.layout;
  });
  return created;
}

export async function promptRename(id) {
  const group = findGroup(getLayout(), id);
  if (!group) return;
  const label = await askText('Group name', { title: 'Rename group', defaultValue: group.label, confirmText: 'Rename', maxLength: 32 });
  if (label && label.trim()) updateLayout((layout) => renameGroup(layout, id, label));
}

function openGroupMenu(id, anchor, doc) {
  const layout = getLayout();
  const group = findGroup(layout, id);
  if (!group) return;
  const shown = shownGroup(doc);
  const canMove = (delta) => moveGroupBy(layout, id, delta, shown) !== layout;
  showMenu(anchor, [
    { label: 'Rename…', onSelect: () => promptRename(id) },
    { label: 'Move group up', disabled: !canMove(-1), onSelect: () => updateLayout((l) => moveGroupBy(l, id, -1, shown)) },
    { label: 'Move group down', disabled: !canMove(1), onSelect: () => updateLayout((l) => moveGroupBy(l, id, 1, shown)) },
    { label: 'New group…', onSelect: () => { promptNewGroup(); } },
    { label: 'Edit navigation…', onSelect: () => openNavEditor({ doc, group: id }) },
    { label: 'Delete group…', danger: true, disabled: layout.groups.length < 2, onSelect: () => openNavEditor({ doc, group: id, deleting: true }) },
  ], { doc, id: 'nav-group-menu', label: `${group.label} options` });
}

// ── Drag and drop ────────────────────────────────────────────────────────────

function clearDragUi(doc) {
  doc.querySelectorAll('.nav-dragging, .nav-drop-before, .nav-drop-after, .nav-drop-into')
    .forEach((node) => node.classList.remove('nav-dragging', 'nav-drop-before', 'nav-drop-after', 'nav-drop-into'));
}

function visibleRows(el, skipKey) {
  return Array.from(el.querySelectorAll(`:scope > ${ROW}`))
    .filter((row) => row.dataset.navItem !== skipKey && row.style.display !== 'none' && !row.hidden);
}

/** Where a drag at `event` would drop. For a tool: a group, and the row it goes
 *  before or after (none for an empty or folded group: it goes last). For a
 *  group: the group it goes before or after. */
function locateDrop(event) {
  const el = event.target.closest?.('.nav-group[data-nav-group]');
  if (!el || !_drag) return null;
  const id = el.dataset.navGroup;
  if (_drag.kind === 'group') {
    if (id === _drag.id) return null;
    const rect = el.querySelector(':scope > .nav-group-header').getBoundingClientRect();
    return { kind: 'group', ref: id, after: event.clientY > rect.top + rect.height / 2, el };
  }
  const rows = el.classList.contains('collapsed') ? [] : visibleRows(el, _drag.key);
  for (const row of rows) {
    const rect = row.getBoundingClientRect();
    if (event.clientY < rect.top + rect.height / 2) return { kind: 'item', group: id, ref: row.dataset.navItem, after: false, el, row };
  }
  const last = rows[rows.length - 1];
  return { kind: 'item', group: id, ref: last ? last.dataset.navItem : null, after: !!last, el, row: last || null };
}

function showDrop(target, doc) {
  doc.querySelectorAll('.nav-drop-before, .nav-drop-after, .nav-drop-into')
    .forEach((node) => node.classList.remove('nav-drop-before', 'nav-drop-after', 'nav-drop-into'));
  if (!target) return;
  if (target.kind === 'group') target.el.classList.add(target.after ? 'nav-drop-after' : 'nav-drop-before');
  else if (target.row) target.row.classList.add(target.after ? 'nav-drop-after' : 'nav-drop-before');
  else target.el.classList.add('nav-drop-into');
}

function bindDrag(root, doc) {
  root.addEventListener('dragstart', (event) => {
    const row = event.target.closest?.(ROW);
    const header = !row && event.target.closest?.('.nav-group-header');
    if (row) _drag = { kind: 'item', key: row.dataset.navItem };
    else if (header) _drag = { kind: 'group', id: groupId(header) };
    else return;
    event.dataTransfer?.setData('text/plain', _drag.key || _drag.id);
    if (event.dataTransfer) event.dataTransfer.effectAllowed = 'move';
    (row || header).classList.add('nav-dragging');
  });
  root.addEventListener('dragover', (event) => {
    if (!_drag) return;
    const target = locateDrop(event);
    if (!target) { showDrop(null, doc); return; }
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = 'move';
    showDrop(target, doc);
  });
  root.addEventListener('dragleave', (event) => {
    if (!event.relatedTarget || !root.contains(event.relatedTarget)) showDrop(null, doc);
  });
  root.addEventListener('drop', (event) => {
    if (!_drag) return;
    const target = locateDrop(event);
    const drag = _drag;
    _drag = null;
    clearDragUi(doc);
    _suppressClickUntil = Date.now() + 300;
    if (!target) return;
    event.preventDefault();
    if (drag.kind === 'group') updateLayout((layout) => moveGroupRelative(layout, drag.id, target.ref, target.after));
    else updateLayout((layout) => moveItem(layout, drag.key, target.group, target.ref, target.after));
  });
  root.addEventListener('dragend', () => {
    // Browsers can follow a drop with a click on the dragged button. Swallow
    // it briefly so rearranging never opens a tool.
    _suppressClickUntil = Date.now() + 300;
    _drag = null;
    clearDragUi(doc);
  });
}

// ── Events ───────────────────────────────────────────────────────────────────

function bindSidebar(root, doc) {
  root.addEventListener('click', (event) => {
    if (Date.now() < _suppressClickUntil && event.target.closest?.(`${ROW}, .nav-group-header`)) {
      event.preventDefault();
      event.stopImmediatePropagation();
      return;
    }
    const menuBtn = event.target.closest?.('.nav-group-menu-btn');
    if (menuBtn) { event.stopPropagation(); openGroupMenu(groupId(menuBtn), menuBtn, doc); return; }
    const toggle = event.target.closest?.('.nav-group-toggle');
    if (toggle) { event.stopPropagation(); toggleGroup(groupId(toggle)); return; }
    // A folded group opens when its body is clicked, as the sections always did.
    const folded = event.target.closest?.('.nav-group.collapsed');
    if (folded && !event.target.closest('button')) toggleGroup(folded.dataset.navGroup);
  }, true);
  root.addEventListener('keydown', (event) => {
    if (!event.altKey || !['ArrowUp', 'ArrowDown'].includes(event.key)) return;
    const row = event.target.closest?.(ROW);
    if (!row) return;
    event.preventDefault();
    const key = row.dataset.navItem;
    updateLayout((layout) => moveItemBy(layout, key, event.key === 'ArrowUp' ? -1 : 1, (other) => isRowShown(other, doc)));
    sidebarNode(key, doc)?.focus?.();
  });
  bindDrag(root, doc);
}

export function resetNavLayout() { return resetLayout(); }

export function initNavOrder(doc = document) {
  if (_initialized && doc === document) return;
  _initialized = true;
  applyNavLayout(getLayout(), doc);
  subscribe((layout) => applyNavLayout(layout, doc));
  syncLayoutWithAccount().catch(() => {});
  const root = doc.querySelector('.sidebar-inner') || doc.getElementById('sidebar');
  if (root) bindSidebar(root, doc);
  doc.getElementById('edit-navigation-btn')?.addEventListener('click', () => openNavEditor({ doc }));
  initNavRail(doc);
}

if (typeof document !== 'undefined') {
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => initNavOrder());
  else initNavOrder();
}

export { defaultLayout, getLayout, readLayout, writeLayout, migrateFlatOrder, syncLayoutWithAccount, openNavEditor };
export default { initNavOrder, applyNavLayout, getLayout, defaultLayout, resetNavLayout, NAV_GROUPS, NAV_ITEMS };
