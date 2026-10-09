/** One geometry owner for multi-tool workspaces. Legacy two-pane docks remain
 * untouched until a third section opens. Extra tools stay open behind tabs,
 * not closed/minimized, so drafts and live jobs survive switching. */
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

export function sectionRects(count, width, height, left = 0) {
  const narrow = width < 680 || height < 460;
  if (narrow) return [{ left, top: 42, width, height: Math.max(0, height - 42) }];
  const n = Math.min(4, count);
  const columns = n > 1 ? 2 : 1;
  const rows = n > 2 ? 2 : 1;
  const w = width / columns, h = (height - 42) / rows;
  return Array.from({ length: n }, (_, i) => ({
    left: left + (i % columns) * w, top: 42 + Math.floor(i / columns) * h,
    width: n === 3 && i === 2 ? width : w, height: h,
  }));
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
  if (document.body.classList.contains('workspace-split')) document.body.classList.remove('workspace-split');
  bar?.remove(); bar = null;
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
  if (!all.some(s => s.el === active)) active = all.at(-1).el;
  const nav = [document.getElementById('sidebar'), document.querySelector('.icon-rail')];
  const left = window.innerWidth <= 768 ? 0 : Math.max(0, ...nav.filter(available).map(el => el.getBoundingClientRect().right));
  const width = Math.max(0, window.innerWidth - left);
  const fullscreen = all.find(s => [...s.el.classList, ...s.owner.classList].some(c => c.endsWith('-fullscreen')));
  if (fullscreen && fullscreen.el !== previousFullscreen) active = fullscreen.el;
  previousFullscreen = fullscreen?.el || null;
  const rects = fullscreen?.el === active ? [{ left, top: 42, width, height: window.innerHeight - 42 }]
    : sectionRects(all.length, width, window.innerHeight, left);
  const visible = rects.length === 1 ? all.filter(s => s.el === active)
    : [all[0], ...all.slice(1).filter(s => s.el !== active).slice(-(active === all[0].el ? 3 : 2)), ...all.slice(1).filter(s => s.el === active)].slice(0, 4);
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
  const signature = all.map(s => `${s.name}:${s.el === active}:${visible.includes(s)}`).join('|');
  if (bar.dataset.signature !== signature) {
    bar.dataset.signature = signature;
    bar.replaceChildren(...all.map(s => {
      const btn = document.createElement('button'); btn.type = 'button'; btn.textContent = s.name;
      btn.setAttribute('aria-pressed', String(s.el === active));
      btn.title = visible.includes(s) ? `Focus ${s.name}` : `Show ${s.name}`;
      btn.onclick = () => { active = s.el; layoutWorkspace(); s.el.querySelector('textarea, input, button')?.focus(); };
      return btn;
    }));
  }
}
export function initWorkspaceLayout() {
  const schedule = () => { if (!frame) frame = requestAnimationFrame(() => { frame = 0; layoutWorkspace(); }); };
  new MutationObserver(records => {
    if (records.some(r => !r.target.closest?.('#workspace-section-tabs') &&
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
