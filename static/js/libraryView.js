// static/js/libraryView.js
/**
 * The Library's "All" view: one list of notes, documents and files, with a
 * card per document group (working master, current export, signed copy,
 * history) and one card per project.
 *
 * It reads GET /api/library/items and GET /api/library/projects and never
 * works out a file's role itself; the backend classifier does. Everything it
 * needs from the app arrives through `deps`, so the module has no imports.
 *
 *   const view = createLibraryView({
 *     api, openDocument, openNote, openVaultFile, showMenu, toast,
 *     onUnavailable, onChange,
 *   });
 *   container.appendChild(view.el);
 *   view.activate();
 */

const PAGE_SIZE = 50;

const TYPE_LABELS = {
  markdown: 'Markdown', pdf: 'PDF', docx: 'Word', image: 'Image',
  other: 'File', document: 'Document', note: 'Note',
};

const SVG_NS = 'http://www.w3.org/2000/svg';

export function typeLabel(type) {
  return TYPE_LABELS[type] || 'File';
}

function openLabel(item) {
  if (!item) return 'Open';
  if (item.type === 'pdf') return 'Open PDF';
  if (item.type === 'docx') return 'Open Word';
  return 'Open';
}

export function timeAgo(iso, now = Date.now()) {
  if (!iso) return '';
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return '';
  const s = Math.max(0, Math.floor((now - then) / 1000));
  if (s < 60) return 'just now';
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const hr = Math.floor(m / 60);
  if (hr < 24) return `${hr}h ago`;
  const d = Math.floor(hr / 24);
  if (d === 1) return 'yesterday';
  if (d < 14) return `${d}d ago`;
  const w = Math.floor(d / 7);
  if (w < 8) return `${w}w ago`;
  return new Date(then).toLocaleDateString();
}

/** Breadcrumb for display: the leading "Projects" folder is noise. */
export function crumbText(item) {
  const parts = (item.breadcrumb || []).filter(Boolean);
  if (parts.length && String(parts[0]).toLowerCase() === 'projects') parts.shift();
  return parts.join(' › ');
}

function itemPath(item) {
  const g = item.group;
  return item.path
    || (g && ((g.master && g.master.path) || (g.current_export && g.current_export.path)
      || (g.signed && g.signed[0] && g.signed[0].path)))
    || '';
}

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v == null) continue;
    if (k === 'class') el.className = v;
    else if (k === 'text') el.textContent = v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children) {
    if (c == null || c === false) continue;
    el.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
  }
  return el;
}

function svgIcon(paths, size = 14) {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('width', String(size));
  svg.setAttribute('height', String(size));
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '2');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  svg.setAttribute('aria-hidden', 'true');
  svg.innerHTML = paths;
  return svg;
}

const LOCK_PATHS = '<rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>';
const DOTS_PATHS = '<circle cx="5" cy="12" r="1.2"/><circle cx="12" cy="12" r="1.2"/><circle cx="19" cy="12" r="1.2"/>';

export function createLibraryView(deps = {}) {
  const api = deps.api || '';
  const state = {
    kind: 'all',        // all | note | file | project
    project: null,
    q: '',
    items: [],
    total: 0,
    projects: [],
    loaded: false,
    unavailable: false,
    token: 0,
  };

  const search = h('input', {
    type: 'text', class: 'memory-search-input lib-search', id: 'doclib-all-search',
    placeholder: 'Search the Library…', 'aria-label': 'Search the Library',
  });
  const projectChipSlot = h('div', { class: 'lib-project-chip-slot' });
  const bar = h('div', { class: 'lib-view-bar' }, search, projectChipSlot);
  const list = h('div', { class: 'lib-list', id: 'doclib-all-list' });
  const more = h('button', { type: 'button', class: 'doclib-load-more lib-load-more', hidden: true }, 'Load more');
  const el = h('div', { class: 'lib-view' }, bar, list, more);

  let debounce = null;
  search.addEventListener('input', () => {
    clearTimeout(debounce);
    debounce = setTimeout(() => { state.q = search.value.trim(); reload(); }, 300);
  });
  more.addEventListener('click', () => load(true));

  function notify() {
    if (typeof deps.onChange === 'function') deps.onChange({ kind: state.kind, project: state.project });
  }

  function toast(msg) {
    if (typeof deps.toast === 'function') deps.toast(msg);
  }

  // ── Opening things ──
  function rawUrl(path, download) {
    return `${api}/api/personal/vault/raw?path=${encodeURIComponent(path)}${download ? '&download=1' : ''}`;
  }

  function download(path) {
    const a = document.createElement('a');
    a.href = rawUrl(path, true);
    a.download = String(path).split('/').pop();
    a.rel = 'noopener';
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  function openFile(file) {
    if (!file || !file.path) return;
    if (file.type === 'markdown') {
      if (deps.openVaultFile) deps.openVaultFile(file.path);
    } else if (file.type === 'pdf' || file.type === 'image') {
      window.open(rawUrl(file.path), '_blank', 'noopener');
    } else {
      download(file.path);
    }
  }

  function stripPrefix(id, prefix) {
    return String(id || '').startsWith(prefix) ? String(id).slice(prefix.length) : String(id || '');
  }

  function canEdit(item) {
    const g = item.group;
    return !!(g && g.master && g.master.type === 'markdown' && !item.locked
      && (item.actions || []).includes('edit'));
  }

  /** The copy a person most likely wants to read: export, then signed, then master. */
  function viewTarget(item) {
    const g = item.group;
    if (!g) return null;
    return g.current_export || (g.signed && g.signed[0]) || g.master || null;
  }

  function copyPath(item) {
    const p = itemPath(item);
    if (!p) return;
    const done = () => toast('Path copied');
    try {
      Promise.resolve(navigator.clipboard.writeText(p)).then(done, () => toast('Could not copy the path'));
    } catch (_) { toast('Could not copy the path'); }
  }

  // ── Cards ──
  function rolesLine(item) {
    const g = item.group;
    if (item.kind === 'group' && g) {
      const parts = [];
      if (g.master) parts.push(`Working master: ${typeLabel(g.master.type)}`);
      if (g.current_export) parts.push(`Current export: ${typeLabel(g.current_export.type)}`);
      if (g.signed && g.signed.length) parts.push(`Signed copy: ${typeLabel(g.signed[0].type)}`);
      if (parts.length) return parts.join(' · ');
    }
    if (item.kind === 'note') return 'Note';
    if (item.kind === 'document') return 'Document';
    if (item.role === 'signed' || item.locked) return `Signed copy: ${typeLabel(item.type)}`;
    return `${typeLabel(item.type)} file`;
  }

  function isLocked(item) {
    const g = item.group;
    return !!(item.locked || item.role === 'signed'
      || (g && g.signed && g.signed.length && !g.master));
  }

  function primaryActions(item) {
    // Returns [{label, run, primary}] with at most two entries.
    if (item.kind === 'document') {
      return [{ label: 'Open', primary: true, run: () => deps.openDocument && deps.openDocument(stripPrefix(item.id, 'doc:')) }];
    }
    if (item.kind === 'note') {
      return [{ label: 'Open', primary: true, run: () => deps.openNote && deps.openNote(stripPrefix(item.id, 'note:')) }];
    }
    if (item.kind === 'group' && item.group) {
      const out = [];
      const target = viewTarget(item);
      if (canEdit(item)) {
        out.push({ label: 'Edit', primary: true, run: () => openFile(item.group.master) });
        if (target && target !== item.group.master) {
          out.push({ label: openLabel(target), run: () => openFile(target) });
        }
      } else if (target) {
        out.push({ label: openLabel(target), primary: true, run: () => openFile(target) });
      }
      return out;
    }
    return [{ label: openLabel(item), primary: true, run: () => openFile(item) }];
  }

  function overflowItems(item) {
    const items = [];
    const g = item.group;
    const dl = item.kind === 'group' && g ? (g.current_export || g.master || (g.signed && g.signed[0])) : (item.kind === 'file' ? item : null);
    if (dl && dl.path) items.push({ label: 'Download', action: () => download(dl.path) });
    if (itemPath(item)) items.push({ label: 'Copy path', icon: 'copy', action: () => copyPath(item) });
    return items;
  }

  function historyRow(file, quiet) {
    const name = file.title || String(file.path || '').split('/').pop() || 'Untitled';
    const btn = h('button', { type: 'button', class: 'lib-history-row' + (quiet ? ' quiet' : ''), title: file.path || false },
      h('span', { class: 'lib-history-name' }, name),
      h('span', { class: 'lib-history-when' }, [typeLabel(file.type), timeAgo(file.updated_at)].filter(Boolean).join(' · ')));
    btn.addEventListener('click', () => openFile(file));
    return btn;
  }

  function historyDisclosure(item) {
    const g = item.group;
    if (!g) return null;
    const hist = g.history || [];
    const supp = g.supporting || [];
    if (!hist.length && !supp.length) return null;
    const label = hist.length ? `History (${hist.length})` : `Supporting files (${supp.length})`;
    const toggle = h('button', { type: 'button', class: 'lib-link-btn lib-history-toggle', 'aria-expanded': 'false' }, label);
    const panel = h('div', { class: 'lib-history', hidden: true });
    if (hist.length) {
      panel.appendChild(h('div', { class: 'lib-history-list' }, ...hist.map((f) => historyRow(f, false))));
    }
    if (supp.length) {
      panel.appendChild(h('div', { class: 'lib-history-heading' }, 'Supporting files'));
      panel.appendChild(h('div', { class: 'lib-history-list' }, ...supp.map((f) => historyRow(f, true))));
    }
    toggle.addEventListener('click', () => {
      const open = panel.hidden;
      panel.hidden = !open;
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
    return { toggle, panel };
  }

  function renderCard(item) {
    const locked = isLocked(item);
    const acts = primaryActions(item);
    const crumb = crumbText(item);
    const when = timeAgo(item.updated_at);
    const warnings = (item.group && item.group.warnings) || [];

    const card = h('article', { class: 'lib-card' + (locked ? ' locked' : ''), 'data-item-id': item.id, 'data-kind': item.kind });

    const title = h('h3', { class: 'lib-card-title' }, item.title || 'Untitled');
    const titleRow = h('div', { class: 'lib-card-titlerow' },
      locked ? h('span', { class: 'lib-lock', title: 'Signed copy', role: 'img', 'aria-label': 'Signed copy' }, svgIcon(LOCK_PATHS)) : null,
      title);
    card.appendChild(titleRow);

    const subBits = [];
    if (crumb) {
      subBits.push(h('span', { class: 'lib-crumb', title: itemPath(item) || false }, crumb));
    }
    if (when) subBits.push(h('span', { class: 'lib-when' }, `updated ${when}`));
    if (subBits.length) {
      const sub = h('div', { class: 'lib-card-sub' });
      subBits.forEach((b, i) => {
        if (i) sub.appendChild(h('span', { class: 'lib-sep', 'aria-hidden': 'true' }, '·'));
        sub.appendChild(b);
      });
      card.appendChild(sub);
    }

    card.appendChild(h('div', { class: 'lib-card-roles' }, rolesLine(item)));

    if (warnings.includes('word_newer_than_master')) {
      card.appendChild(h('div', { class: 'lib-notice', role: 'note' },
        'The Word copy is newer than the master. Open both to compare.'));
    }

    const row = h('div', { class: 'lib-card-actions' });
    for (const a of acts) {
      const b = h('button', { type: 'button', class: 'memory-toolbar-btn lib-btn' + (a.primary ? ' primary' : '') }, a.label);
      b.addEventListener('click', a.run);
      row.appendChild(b);
    }
    const hist = historyDisclosure(item);
    if (hist) row.appendChild(hist.toggle);
    const menuItems = overflowItems(item);
    if (menuItems.length) {
      const dots = h('button', { type: 'button', class: 'lib-overflow', 'aria-label': 'More actions', title: 'More actions' }, svgIcon(DOTS_PATHS, 16));
      dots.addEventListener('click', (e) => {
        e.stopPropagation();
        if (typeof deps.showMenu === 'function') deps.showMenu(dots, menuItems);
      });
      row.appendChild(dots);
    }
    card.appendChild(row);
    if (hist) card.appendChild(hist.panel);
    return card;
  }

  function renderProjectCard(p) {
    const c = p.counts || {};
    const bits = [];
    if (c.documents) bits.push(`${c.documents} document${c.documents === 1 ? '' : 's'}`);
    if (c.files) bits.push(`${c.files} file${c.files === 1 ? '' : 's'}`);
    if (c.notes) bits.push(`${c.notes} note${c.notes === 1 ? '' : 's'}`);
    const when = timeAgo(p.updated_at);
    const card = h('article', { class: 'lib-card lib-project-card', 'data-project': p.name, tabindex: '0', role: 'button', 'aria-label': `Open project ${p.name}` },
      h('div', { class: 'lib-card-titlerow' }, h('h3', { class: 'lib-card-title' }, p.name)),
      h('div', { class: 'lib-card-roles' }, bits.join(' · ') || 'Empty'),
      when ? h('div', { class: 'lib-card-sub' }, h('span', { class: 'lib-when' }, `updated ${when}`)) : null);
    const go = () => setProject(p.name);
    card.addEventListener('click', go);
    card.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); }
    });
    return card;
  }

  // ── Rendering the list ──
  function renderProjectChip() {
    projectChipSlot.textContent = '';
    if (!state.project) return;
    const x = h('button', { type: 'button', class: 'lib-chip-x', 'aria-label': `Remove project filter ${state.project}` }, '×');
    x.addEventListener('click', () => setProject(null));
    projectChipSlot.appendChild(h('span', { class: 'lib-project-chip' }, h('span', { class: 'lib-project-chip-text' }, `Project: ${state.project}`), x));
  }

  function emptyText() {
    if (state.q) return 'Nothing matches that search.';
    if (state.kind === 'project') return 'No projects yet.';
    if (state.project) return 'Nothing in this project yet.';
    if (state.kind === 'note') return 'No notes yet.';
    if (state.kind === 'file') return 'No files yet.';
    return 'Nothing in the Library yet.';
  }

  function render() {
    list.textContent = '';
    renderProjectChip();
    if (state.kind === 'project') {
      const q = state.q.toLowerCase();
      const ps = state.projects.filter((p) => !q || String(p.name).toLowerCase().includes(q));
      if (!ps.length) list.appendChild(h('div', { class: 'lib-empty' }, emptyText()));
      ps.forEach((p) => list.appendChild(renderProjectCard(p)));
      more.hidden = true;
      return;
    }
    if (!state.items.length) list.appendChild(h('div', { class: 'lib-empty' }, emptyText()));
    state.items.forEach((it) => list.appendChild(renderCard(it)));
    more.hidden = state.items.length >= state.total;
  }

  function showError(msg) {
    list.textContent = '';
    const retry = h('button', { type: 'button', class: 'memory-toolbar-btn' }, 'Try again');
    retry.addEventListener('click', () => reload());
    list.appendChild(h('div', { class: 'lib-empty' }, msg, ' ', retry));
    more.hidden = true;
  }

  // ── Loading ──
  async function load(append) {
    const token = ++state.token;
    if (!append) {
      list.textContent = '';
      list.appendChild(h('div', { class: 'lib-empty' }, 'Loading…'));
    }
    try {
      if (state.kind === 'project') {
        const res = await fetch(`${api}/api/library/projects`, { credentials: 'same-origin' });
        if (token !== state.token) return;
        if (res.status === 404 || res.status === 405) return markUnavailable();
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        state.projects = data.projects || [];
      } else {
        const params = new URLSearchParams({ kind: state.kind, limit: String(PAGE_SIZE), offset: String(append ? state.items.length : 0) });
        if (state.project) params.set('project', state.project);
        if (state.q) params.set('q', state.q);
        const res = await fetch(`${api}/api/library/items?${params}`, { credentials: 'same-origin' });
        if (token !== state.token) return;
        if (res.status === 404 || res.status === 405) return markUnavailable();
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        state.items = append ? state.items.concat(data.items || []) : (data.items || []);
        state.total = typeof data.total === 'number' ? data.total : state.items.length;
      }
      state.loaded = true;
      render();
    } catch (e) {
      if (token !== state.token) return;
      showError('The Library could not be loaded.');
    }
  }

  function markUnavailable() {
    state.unavailable = true;
    list.textContent = '';
    if (typeof deps.onUnavailable === 'function') deps.onUnavailable();
  }

  function reload() { return load(false); }

  function setKind(kind) {
    const next = ['all', 'note', 'file', 'project'].includes(kind) ? kind : 'all';
    if (next === state.kind && state.loaded) return;
    state.kind = next;
    state.loaded = false;
    notify();
  }

  function setProject(name) {
    state.project = name || null;
    // A project card was clicked in the Projects filter: show its contents.
    if (name && state.kind === 'project') state.kind = 'all';
    state.loaded = false;
    notify();
    reload();
  }

  /** Called whenever the view becomes visible; loads when filters changed. */
  function activate() {
    if (state.unavailable) return;
    if (!state.loaded) reload();
  }

  return {
    el,
    get kind() { return state.kind; },
    get project() { return state.project; },
    get unavailable() { return state.unavailable; },
    setKind, setProject, activate, reload, renderCard,
  };
}
