// Navigation layout: which groups exist, what they are called, and which tools
// sit in each, in order. The sidebar groups, the collapsed rail's group icons
// and its flyouts are all drawn from this one model.
//
// Shape (stored per account, localStorage is only the fast path):
//   { version: 2, groups: [{ id, label, icon, collapsed, items: [itemKey...] }] }
//
// This module holds no DOM. It owns the model, the edit operations (pure
// functions: layout in, layout out), migration from the flat order older builds
// saved, and persistence through serverPrefs.js. navOrder.js, navRail.js and
// navEditor.js draw it.
//
// Two rules keep a person's layout safe:
//  - An item id this build does not know stays in the stored layout. A newer
//    build, or an account with other permissions, may know it. It is not drawn.
//  - A tool this build has but the saved layout lacks goes to its default group.

import { reconcile, readPref, writePref } from './serverPrefs.js';

export const LAYOUT_KEY = 'odysseus-nav-layout-v2';
export const LAYOUT_PREF = 'nav-layout';
// The flat order older builds saved. Read for migration, never written again.
export const LEGACY_ORDER_KEY = 'odysseus-nav-order-v1';
export const LEGACY_ORDER_PREF = 'nav-order';
const COLLAPSED_KEY = 'section-collapsed';

// Built-in groups and the sidebar container each one has always had.
export const NAV_GROUPS = Object.freeze({
  work: 'work-section',
  agents: 'agents-section',
  more: 'more-section',
});

// `group` is where a tool goes when a layout does not place it. `sidebar` and
// `rail` are the existing nodes that carry its click handlers. Email has only a
// rail node (its sidebar entry is the Email section above the groups); Lotus
// has only a sidebar node. A flyout row clicks whichever node exists.
export const NAV_ITEMS = Object.freeze([
  Object.freeze({ key: 'today', label: 'Today', rail: 'rail-today', sidebar: 'tool-today-btn', group: 'work' }),
  Object.freeze({ key: 'email', label: 'Email', rail: 'rail-email', sidebar: null, group: 'work' }),
  Object.freeze({ key: 'library', label: 'Library', rail: 'rail-archive', sidebar: 'tool-library-btn', group: 'work' }),
  Object.freeze({ key: 'notes', label: 'Notes', rail: 'rail-notes', sidebar: 'tool-notes-btn', group: 'work' }),
  Object.freeze({ key: 'tasks', label: 'Tasks', rail: 'rail-tasks', sidebar: 'tool-tasks-btn', group: 'work' }),
  Object.freeze({ key: 'calendar', label: 'Calendar', rail: 'rail-calendar', sidebar: 'tool-calendar-btn', group: 'work' }),
  Object.freeze({ key: 'agents', label: 'Agents', agLabel: 'Phalanx', rail: 'rail-agents', sidebar: 'tool-agents-btn', group: 'agents' }),
  Object.freeze({ key: 'workbench', label: 'Workbench', rail: 'rail-workbench', sidebar: 'tool-workbench-btn', group: 'agents' }),
  Object.freeze({ key: 'memory', label: 'Brain', rail: 'rail-memory', sidebar: 'tool-memory-btn', group: 'more' }),
  Object.freeze({ key: 'lotus', label: 'Lotus', rail: null, sidebar: 'tool-lotus-btn', group: 'more' }),
  Object.freeze({ key: 'compare', label: 'Compare', rail: 'rail-compare', sidebar: 'tool-compare-btn', group: 'more' }),
  Object.freeze({ key: 'cookbook', label: 'Cookbook', rail: 'rail-cookbook', sidebar: 'tool-cookbook-btn', group: 'more' }),
  Object.freeze({ key: 'research', label: 'Deep Research', rail: 'rail-research', sidebar: 'tool-research-btn', group: 'more' }),
  Object.freeze({ key: 'gallery', label: 'Gallery', rail: 'rail-gallery', sidebar: 'tool-gallery-btn', group: 'more' }),
]);

// Keys the v1 flat order carried that are fixed controls now (Search, New chat,
// Theme) or gone (the Command menu). They are dropped when migrating.
const RETIRED_KEYS = new Set(['search', 'new-chat', 'command', 'theme']);

export const BUILTIN_GROUPS = Object.freeze([
  Object.freeze({ id: 'work', label: 'Work', icon: 'work', collapsed: false }),
  Object.freeze({ id: 'agents', label: 'Agents', icon: 'agents', collapsed: false }),
  Object.freeze({ id: 'more', label: 'More', icon: 'more', collapsed: true }),
]);

// The icons a group can use. A group with no icon of its own shows the first
// letter of its label ('monogram').
export const GROUP_ICONS = Object.freeze({
  work: { label: 'Briefcase', svg: '<rect x="2" y="7" width="20" height="14" rx="2"/><path d="M16 7V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v2"/>' },
  agents: { label: 'People', svg: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c.6-3.6 3.2-5.5 6.5-5.5s5.9 1.9 6.5 5.5"/><circle cx="17.5" cy="9" r="2.5"/><path d="M17 14.5c2.6.2 4.1 1.8 4.5 4.5"/>' },
  more: { label: 'Dots', svg: '<circle cx="5" cy="12" r="1.6" fill="currentColor"/><circle cx="12" cy="12" r="1.6" fill="currentColor"/><circle cx="19" cy="12" r="1.6" fill="currentColor"/>' },
  star: { label: 'Star', svg: '<polygon points="12 2.5 14.9 9 22 9.6 16.6 14.3 18.2 21.3 12 17.6 5.8 21.3 7.4 14.3 2 9.6 9.1 9"/>' },
  folder: { label: 'Folder', svg: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>' },
  bolt: { label: 'Bolt', svg: '<polygon points="13 2 4 14 12 14 11 22 20 10 12 10"/>' },
  book: { label: 'Book', svg: '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>' },
  heart: { label: 'Heart', svg: '<path d="M20.8 5.6a5.5 5.5 0 0 0-7.8 0L12 6.7l-1-1.1a5.5 5.5 0 0 0-7.8 7.8l1 1.1L12 22l7.8-7.5 1-1.1a5.5 5.5 0 0 0 0-7.8z"/>' },
  flag: { label: 'Flag', svg: '<path d="M4 22V4"/><path d="M4 4h13l-2 4 2 4H4"/>' },
  tag: { label: 'Tag', svg: '<path d="M20 12l-8 8-9-9V3h8z"/><circle cx="7.5" cy="7.5" r="1.2"/>' },
  chat: { label: 'Chat', svg: '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>' },
});
export const MONOGRAM = 'monogram';

const ITEM_KEYS = new Set(NAV_ITEMS.map((item) => item.key));
const DEFAULT_GROUP_OF = new Map(NAV_ITEMS.map((item) => [item.key, item.group]));
export const MAX_LABEL = 32;

export function isKnownItem(key) { return ITEM_KEYS.has(key); }
export function itemInfo(key) { return NAV_ITEMS.find((item) => item.key === key) || null; }

// ── Model ────────────────────────────────────────────────────────────────────

export function defaultLayout() {
  return {
    version: 2,
    groups: BUILTIN_GROUPS.map((group) => ({
      ...group,
      items: NAV_ITEMS.filter((item) => item.group === group.id).map((item) => item.key),
    })),
  };
}

export function cleanLabel(value, fallback = '') {
  const text = String(value ?? '').replace(/\s+/g, ' ').trim();
  return Array.from(text).slice(0, MAX_LABEL).join('') || fallback;
}

export function isLayout(value) {
  return !!value && typeof value === 'object' && !Array.isArray(value) && Array.isArray(value.groups);
}

/** A tool the layout does not place goes to its default group, before the first
 *  later default-order tool already there (so a new tool lands where the default
 *  puts it, not last). With the default group gone it goes to the first group. */
function placeMissing(groups) {
  const placed = new Set(groups.flatMap((group) => group.items));
  for (const item of NAV_ITEMS) {
    if (placed.has(item.key)) continue;
    const group = groups.find((entry) => entry.id === item.group) || groups[0];
    if (!group) continue;
    const later = NAV_ITEMS.slice(NAV_ITEMS.indexOf(item) + 1)
      .filter((entry) => entry.group === item.group)
      .map((entry) => group.items.indexOf(entry.key))
      .filter((index) => index >= 0);
    if (later.length) group.items.splice(Math.min(...later), 0, item.key);
    else group.items.push(item.key);
    placed.add(item.key);
  }
}

/** Make anything read from storage a valid layout. Keeps unknown item ids; drops
 *  repeats; adds tools the layout does not place. Never returns zero groups. */
export function normalizeLayout(raw) {
  if (!isLayout(raw)) return defaultLayout();
  const seenGroups = new Set();
  const seenItems = new Set();
  const groups = [];
  for (const entry of raw.groups) {
    if (!entry || typeof entry !== 'object') continue;
    const id = String(entry.id ?? '').trim();
    if (!id || seenGroups.has(id)) continue;
    seenGroups.add(id);
    const builtin = BUILTIN_GROUPS.find((group) => group.id === id);
    const items = [];
    for (const key of Array.isArray(entry.items) ? entry.items : []) {
      const value = String(key);
      if (!value || seenItems.has(value) || RETIRED_KEYS.has(value)) continue;
      seenItems.add(value);
      items.push(value);
    }
    groups.push({
      id,
      label: cleanLabel(entry.label, builtin?.label || 'Group'),
      icon: typeof entry.icon === 'string' && entry.icon ? entry.icon : (builtin?.icon || MONOGRAM),
      collapsed: typeof entry.collapsed === 'boolean' ? entry.collapsed : !!builtin?.collapsed,
      items,
    });
  }
  if (!groups.length) return defaultLayout();
  placeMissing(groups);
  return { version: 2, groups };
}

export function sameLayout(a, b) { return JSON.stringify(a) === JSON.stringify(b); }
function clone(layout) { return JSON.parse(JSON.stringify(layout)); }

export function findGroup(layout, id) { return layout.groups.find((group) => group.id === id) || null; }
export function groupOfItem(layout, key) { return layout.groups.find((group) => group.items.includes(key)) || null; }

// ── Migration from the flat order ────────────────────────────────────────────

// Before groups existed the navigation was one flat list. A browser that saved
// that default unchanged (a reset wrote it) has no real preference.
const LEGACY_DEFAULT_ORDER = Object.freeze([
  'search', 'new-chat', 'command', 'today', 'calendar', 'compare', 'cookbook', 'research',
  'email', 'gallery', 'library', 'memory', 'lotus', 'agents', 'workbench', 'notes', 'tasks', 'theme',
]);
const LEGACY_DEFAULT_NO_TODAY = LEGACY_DEFAULT_ORDER.filter((key) => key !== 'today');

export function isLegacyDefaultOrder(order) {
  const list = Array.isArray(order) ? order.map(String) : [];
  return [LEGACY_DEFAULT_ORDER, LEGACY_DEFAULT_NO_TODAY]
    .some((legacy) => legacy.length === list.length && legacy.every((key, i) => key === list[i]));
}

/** Build a layout from a v1 flat order. The three built-in groups keep the
 *  membership they always had; inside each, tools follow the saved order.
 *  `collapsed` is the per-section fold state the sidebar kept separately. */
export function migrateFlatOrder(order, collapsed = {}) {
  const list = (Array.isArray(order) ? order : []).map(String);
  const layout = defaultLayout();
  for (const group of layout.groups) {
    const sectionId = NAV_GROUPS[group.id];
    if (sectionId && collapsed && typeof collapsed[sectionId] === 'boolean') group.collapsed = collapsed[sectionId];
    const rank = (key) => { const at = list.indexOf(key); return at < 0 ? Infinity : at; };
    // A stable sort keeps the default position for tools the old order never named.
    group.items = group.items
      .map((key, index) => ({ key, index }))
      .sort((a, b) => (rank(a.key) - rank(b.key)) || (a.index - b.index))
      .map((entry) => entry.key);
    // Infinity - Infinity is NaN; the comparator treats that as equal, which is
    // what we want for two unnamed tools.
  }
  return normalizeLayout(layout);
}

// ── Edit operations (pure) ───────────────────────────────────────────────────

let _idCounter = 0;
export function newGroupId(layout) {
  const taken = new Set(layout.groups.map((group) => group.id));
  let id;
  do {
    _idCounter += 1;
    id = `g${Date.now().toString(36)}${Math.floor(Math.random() * 1296).toString(36)}${_idCounter}`;
  } while (taken.has(id));
  return id;
}

export function createGroup(layout, { label = 'New group', icon = MONOGRAM, id } = {}) {
  const next = clone(layout);
  const groupId = id && !findGroup(next, id) ? id : newGroupId(next);
  next.groups.push({ id: groupId, label: cleanLabel(label, 'New group'), icon, collapsed: false, items: [] });
  return { layout: next, id: groupId };
}

function editGroup(layout, id, change) {
  const next = clone(layout);
  const group = findGroup(next, id);
  if (!group) return layout;
  change(group);
  return next;
}

export function renameGroup(layout, id, label) {
  const clean = cleanLabel(label);
  return clean ? editGroup(layout, id, (group) => { group.label = clean; }) : layout;
}
export function setGroupIcon(layout, id, icon) { return editGroup(layout, id, (group) => { group.icon = String(icon || MONOGRAM); }); }
export function setGroupCollapsed(layout, id, collapsed) { return editGroup(layout, id, (group) => { group.collapsed = !!collapsed; }); }

/** Move a group `delta` places. Only groups `isShown` accepts count as a place,
 *  so one press moves a group past one that is drawn. */
export function moveGroupBy(layout, id, delta, isShown = () => true) {
  const from = layout.groups.findIndex((group) => group.id === id);
  if (from < 0 || !delta) return layout;
  const step = delta > 0 ? 1 : -1;
  let to = from + step;
  while (to >= 0 && to < layout.groups.length && !isShown(layout.groups[to])) to += step;
  if (to < 0 || to >= layout.groups.length) return layout;
  const next = clone(layout);
  const [group] = next.groups.splice(from, 1);
  next.groups.splice(to, 0, group);
  return next;
}

/** Drop a group before or after another (drag and drop). */
export function moveGroupRelative(layout, id, refId, after = false) {
  if (id === refId || !findGroup(layout, id) || !findGroup(layout, refId)) return layout;
  const next = clone(layout);
  const [group] = next.groups.splice(next.groups.findIndex((entry) => entry.id === id), 1);
  const at = next.groups.findIndex((entry) => entry.id === refId);
  next.groups.splice(after ? at + 1 : at, 0, group);
  return sameLayout(next, layout) ? layout : next;
}

/** Move an item to a group. With `refKey` it lands before (or `after`) that item;
 *  otherwise at the end. The target may be empty. */
export function moveItem(layout, key, toGroupId, refKey = null, after = false) {
  if (!findGroup(layout, toGroupId) || !groupOfItem(layout, key)) return layout;
  const next = clone(layout);
  groupOfItem(next, key).items.splice(groupOfItem(next, key).items.indexOf(key), 1);
  const target = findGroup(next, toGroupId);
  const at = refKey && refKey !== key ? target.items.indexOf(refKey) : -1;
  if (at < 0) target.items.push(key);
  else target.items.splice(after ? at + 1 : at, 0, key);
  return sameLayout(next, layout) ? layout : next;
}

/** Move an item one drawn place up or down inside its group. Items `isShown`
 *  rejects (hidden for this account, or unknown to this build) are stepped over. */
export function moveItemBy(layout, key, delta, isShown = () => true) {
  const group = groupOfItem(layout, key);
  if (!group || !delta) return layout;
  const from = group.items.indexOf(key);
  const step = delta > 0 ? 1 : -1;
  let to = from + step;
  while (to >= 0 && to < group.items.length && !isShown(group.items[to])) to += step;
  if (to < 0 || to >= group.items.length) return layout;
  const next = clone(layout);
  const items = findGroup(next, group.id).items;
  items.splice(from, 1);
  items.splice(to, 0, key);
  return next;
}

/** Delete a group, moving its tools to the end of `moveTo`. Refused (the same
 *  layout comes back) for the last group, or an unknown or same target. */
export function deleteGroup(layout, id, moveTo) {
  if (layout.groups.length < 2 || !findGroup(layout, id)) return layout;
  const target = moveTo && moveTo !== id && findGroup(layout, moveTo)
    ? moveTo
    : layout.groups.find((group) => group.id !== id).id;
  const next = clone(layout);
  const gone = findGroup(next, id);
  findGroup(next, target).items.push(...gone.items);
  next.groups = next.groups.filter((group) => group.id !== id);
  return next;
}

// ── Storage ──────────────────────────────────────────────────────────────────

function storage() {
  try { return window.localStorage; } catch (_) { return null; }
}

function readEnvelope(key) {
  const store = storage();
  if (!store) return null;
  try {
    const raw = JSON.parse(store.getItem(key) || 'null');
    if (Array.isArray(raw)) return raw.length ? { value: raw, updated_at: 0 } : null;
    if (raw && typeof raw === 'object' && 'value' in raw) return { value: raw.value, updated_at: Number(raw.updated_at) || 0 };
  } catch (_) {}
  return null;
}

function readCollapsedMap() {
  const store = storage();
  try { return JSON.parse(store?.getItem(COLLAPSED_KEY) || 'null') || {}; } catch (_) { return {}; }
}

function localLayoutEntry() {
  const entry = readEnvelope(LAYOUT_KEY);
  return entry && isLayout(entry.value) ? entry : null;
}

function localLegacyEntry() {
  const entry = readEnvelope(LEGACY_ORDER_KEY);
  return entry && Array.isArray(entry.value) ? entry : null;
}

let _layout = null;
const _listeners = new Set();

/** The layout in force: the stored v2 layout, else the v1 order migrated in
 *  memory (nothing is written until the person changes something), else the
 *  default. */
export function readLayout() {
  const stored = localLayoutEntry();
  if (stored) return normalizeLayout(stored.value);
  const legacy = localLegacyEntry();
  if (legacy && !isLegacyDefaultOrder(legacy.value)) return migrateFlatOrder(legacy.value, readCollapsedMap());
  return defaultLayout();
}

export function getLayout() {
  if (!_layout) _layout = readLayout();
  return _layout;
}

export function subscribe(listener) {
  _listeners.add(listener);
  return () => _listeners.delete(listener);
}

function publish() {
  for (const listener of Array.from(_listeners)) {
    try { listener(_layout); } catch (error) { console.warn('[nav] layout listener failed', error); }
  }
}

/** Store a layout and tell the drawers. `sync: false` records a value that came
 *  from the account, so applying it does not bounce back to the server. */
export function writeLayout(layout, { sync = true, at } = {}) {
  const normalized = normalizeLayout(layout);
  const stamped = Number(at) > 0 ? Number(at) : Date.now();
  try { storage()?.setItem(LAYOUT_KEY, JSON.stringify({ value: normalized, updated_at: stamped })); } catch (_) {}
  if (sync) writePref(LAYOUT_PREF, normalized, stamped);
  _layout = normalized;
  publish();
  return normalized;
}

/** Apply an edit. `change` gets the current layout and returns the next; the
 *  same layout back means nothing changed and nothing is written. */
export function updateLayout(change) {
  const current = getLayout();
  const next = change(current);
  if (!next || next === current || sameLayout(next, current)) return current;
  return writeLayout(next);
}

/** Back to the default arrangement. Recorded rather than dropped, so another
 *  browser on the account does not restore the old one. */
export function resetLayout() {
  return writeLayout(defaultLayout());
}

/** Test hook: forget the in-memory layout and listeners. */
export function _resetForTests() {
  _layout = null;
  _listeners.clear();
}

/** Adopt the account's layout when it is newer than this browser's, push this
 *  browser's when it is newer, and when the account has no v2 layout yet carry a
 *  v1 order over. Safe to call before or after first paint. */
export async function syncLayoutWithAccount() {
  const { source, entry } = await reconcile(LAYOUT_PREF, localLayoutEntry());
  if (source === 'remote' && entry && isLayout(entry.value)) {
    return writeLayout(entry.value, { sync: false, at: entry.updated_at || Date.now() });
  }
  if (source !== 'none') return getLayout();
  // No v2 layout here or on the account: look for the v1 order.
  const local = localLegacyEntry();
  const remote = await readPref(LEGACY_ORDER_PREF);
  const remoteEntry = remote && Array.isArray(remote.value) ? remote : null;
  const pick = [local, remoteEntry].filter(Boolean).sort((a, b) => b.updated_at - a.updated_at)[0];
  if (!pick || isLegacyDefaultOrder(pick.value)) return getLayout();
  return writeLayout(migrateFlatOrder(pick.value, readCollapsedMap()), { at: pick.updated_at || Date.now() });
}

export default {
  LAYOUT_KEY, LAYOUT_PREF, NAV_GROUPS, NAV_ITEMS, BUILTIN_GROUPS, GROUP_ICONS, MONOGRAM,
  defaultLayout, normalizeLayout, migrateFlatOrder, readLayout, getLayout, writeLayout, updateLayout,
  resetLayout, subscribe, syncLayoutWithAccount,
  createGroup, renameGroup, setGroupIcon, setGroupCollapsed, moveGroupBy, moveGroupRelative,
  moveItem, moveItemBy, deleteGroup,
};
