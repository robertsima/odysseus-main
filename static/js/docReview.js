// Review changes before export, for the document editor.
//
// The first half is pure: split Markdown into sections, diff two texts by
// section, find placeholders, word the summary. The second half
// (mountReviewPanel) draws a read-only side sheet from those. Nothing here
// writes to the document.

const FENCE = /^\s{0,3}(```|~~~)/;
const HEADING = /^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$/;
const PLACEHOLDER = /\[\[[^\]\n]*\]\]|\{\{[^}\n]*\}\}|\b(?:TODO|TBD|FIXME)\b|_{4,}/gi;
// Contract-style blanks: [CLIENT], [PROCESSOR NAME]. Case-sensitive, so link
// text like [the terms] is not one; links, reference links, [!NOTE] callouts
// and [[…]] (matched above) are skipped by the lookarounds.
const CAPS_BLANK = /(?<![[!\w])\[[A-Z][A-Z0-9 _'.&/-]*[A-Z0-9]\](?![([:\]])/g;
const MAX_CELLS = 4_000_000;

function lines(md) {
  return String(md ?? '').replace(/\r\n?/g, '\n').split('\n');
}

/** Split into [{heading, level, body: string[], startLine}]; code fences never start a section. */
export function splitSections(md) {
  const out = [{ heading: '', level: 0, body: [], startLine: 1 }];
  let fence = null;
  lines(md).forEach((raw, i) => {
    const line = raw.replace(/[ \t]+$/, '');
    const f = FENCE.exec(line);
    if (f) {
      if (!fence) fence = f[1];
      else if (fence === f[1]) fence = null;
    }
    const h = !fence && !f ? HEADING.exec(line) : null;
    if (h) out.push({ heading: h[2].trim(), level: h[1].length, body: [], startLine: i + 1 });
    else out[out.length - 1].body.push(line);
  });
  const pre = out[0];
  if (!pre.body.some((l) => l.trim())) out.shift();
  for (const s of out) {
    while (s.body.length && !s.body[s.body.length - 1].trim()) s.body.pop();
  }
  return out;
}

function paragraphs(body) {
  const paras = [];
  let cur = [];
  for (const l of body) {
    if (l.trim()) cur.push(l);
    else if (cur.length) { paras.push(cur.join('\n')); cur = []; }
  }
  if (cur.length) paras.push(cur.join('\n'));
  return paras;
}

function lcsDiff(a, b) {
  if (a.length * b.length > MAX_CELLS) {
    return [...a.map((text) => ({ type: 'del', text })), ...b.map((text) => ({ type: 'add', text }))];
  }
  const w = b.length + 1;
  const t = new Uint32Array((a.length + 1) * w);
  for (let i = a.length - 1; i >= 0; i--) {
    for (let j = b.length - 1; j >= 0; j--) {
      t[i * w + j] = a[i] === b[j] ? t[(i + 1) * w + j + 1] + 1 : Math.max(t[(i + 1) * w + j], t[i * w + j + 1]);
    }
  }
  const ops = [];
  let i = 0, j = 0;
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) { ops.push({ type: 'same', text: a[i] }); i++; j++; }
    else if (t[(i + 1) * w + j] >= t[i * w + j + 1]) ops.push({ type: 'del', text: a[i++] });
    else ops.push({ type: 'add', text: b[j++] });
  }
  while (i < a.length) ops.push({ type: 'del', text: a[i++] });
  while (j < b.length) ops.push({ type: 'add', text: b[j++] });
  return ops;
}

/** Keep `ctx` unchanged lines around each change; collapse the rest to one gap marker. */
function trimContext(ops, ctx = 1) {
  const keep = new Array(ops.length).fill(false);
  ops.forEach((op, i) => {
    if (op.type === 'same') return;
    for (let k = Math.max(0, i - ctx); k <= Math.min(ops.length - 1, i + ctx); k++) keep[k] = true;
  });
  const out = [];
  ops.forEach((op, i) => {
    if (keep[i]) out.push(op);
    else if (out.length && out[out.length - 1].type !== 'gap') out.push({ type: 'gap', text: '' });
  });
  while (out.length && out[out.length - 1].type === 'gap') out.pop();
  while (out.length && out[0].type === 'gap') out.shift();
  return out;
}

function keyed(sections) {
  const seen = new Map();
  const map = new Map();
  for (const s of sections) {
    const base = s.heading.toLowerCase();
    const n = seen.get(base) || 0;
    seen.set(base, n + 1);
    map.set(`${base}#${n}`, s);
  }
  return map;
}

/** Compare two Markdown texts section by section. */
export function diffSections(oldMd, newMd) {
  const a = splitSections(oldMd);
  const b = splitSections(newMd);
  const byParagraph = a.every((s) => !s.heading) && b.every((s) => !s.heading);
  const units = (s) => (byParagraph ? paragraphs(s.body) : s.body);
  const am = keyed(a);
  const bm = keyed(b);
  const result = { added: [], removed: [], changed: [] };
  for (const [key, s] of bm) {
    const o = am.get(key);
    if (!o) {
      const diff = trimContext(units(s).map((text) => ({ type: 'add', text })), 0);
      result.added.push({ heading: s.heading, line: s.startLine, diff });
      continue;
    }
    const ops = lcsDiff(units(o), units(s));
    if (ops.every((op) => op.type === 'same')) continue;
    result.changed.push({
      heading: s.heading,
      line: s.startLine,
      added: ops.filter((op) => op.type === 'add').length,
      removed: ops.filter((op) => op.type === 'del').length,
      diff: trimContext(ops),
    });
  }
  for (const [key, s] of am) {
    if (!bm.has(key)) {
      result.removed.push({ heading: s.heading, line: s.startLine, diff: trimContext(units(s).map((text) => ({ type: 'del', text })), 0) });
    }
  }
  return result;
}

/** Placeholders outside code fences: [{text, line, section}] (line is 1-based). */
export function findPlaceholders(md) {
  const found = [];
  let fence = null;
  let section = '';
  lines(md).forEach((raw, i) => {
    const f = FENCE.exec(raw);
    if (f) {
      if (!fence) fence = f[1];
      else if (fence === f[1]) fence = null;
      return;
    }
    if (fence) return;
    const h = HEADING.exec(raw);
    if (h) section = h[2].trim();
    const hits = [];
    for (const re of [PLACEHOLDER, CAPS_BLANK]) {
      re.lastIndex = 0;
      let m;
      while ((m = re.exec(raw))) hits.push({ at: m.index, text: m[0] });
    }
    hits.sort((a, b) => a.at - b.at);
    for (const h of hits) found.push({ text: h.text, line: i + 1, section });
  });
  return found;
}

const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;

/** The lines the panel shows, and whether the person has a decision to make. */
export function reviewSummary({ diff, placeholders = [], unresolvedSuggestions = 0, checks = 0 } = {}) {
  const out = [];
  if (!diff) {
    out.push({ key: 'changed', text: 'Changed: nothing to compare yet' });
  } else {
    const n = diff.added.length + diff.removed.length + diff.changed.length;
    out.push({ key: 'changed', text: n ? `Changed: ${plural(n, 'section', 'sections')}` : 'Changed: nothing' });
  }
  const p = placeholders.length;
  out.push({ key: 'placeholders', text: p ? `Placeholders: ${p} ${p === 1 ? 'remains' : 'remain'}` : 'Placeholders: none' });
  out.push({ key: 'suggestions', text: unresolvedSuggestions ? `Suggestions: ${unresolvedSuggestions} unresolved` : 'Suggestions: none unresolved' });
  out.push({ key: 'checks', text: checks ? `Checks: ${checks}` : 'Checks: none configured' });
  return { lines: out, needsDecision: unresolvedSuggestions > 0 };
}

function whenMs(iso) {
  if (!iso) return NaN;
  const s = /(Z|[+-]\d\d:?\d\d)$/.test(iso) ? iso : iso + 'Z'; // the server stores naive UTC
  return Date.parse(s);
}

function ago(iso, now) {
  const t = whenMs(iso);
  if (Number.isNaN(t)) return '';
  const min = Math.max(0, Math.round((now - t) / 60000));
  if (min < 1) return 'just now';
  if (min < 60) return `${min} min ago`;
  const h = Math.round(min / 60);
  if (h < 24) return `${h} h ago`;
  const d = Math.round(h / 24);
  return d === 1 ? 'yesterday' : `${d} days ago`;
}

/** "v12, 2 h ago, by agent" */
export function versionLabel(v, now = Date.now()) {
  const parts = [`v${v.version_number}`];
  const when = ago(v.created_at, now);
  if (when) parts.push(when);
  parts.push(`by ${v.source === 'user' ? 'you' : 'agent'}`);
  return parts.join(', ');
}

/**
 * Versions arrive newest first. Compare against the one before the latest when
 * the live text is the latest; against the latest when there are unsaved edits.
 * Null when there is nothing earlier to compare with.
 */
export function defaultBaseline(versions, liveText) {
  if (!versions || versions.length < 2) return null;
  const norm = (s) => String(s ?? '').replace(/\r\n?/g, '\n');
  return norm(liveText) === norm(versions[0].content) ? versions[1] : versions[0];
}

// ── Panel ───────────────────────────────────────────────────────────────

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

const MARK = { add: '+', del: '−', same: ' ' };

function renderDiff(diff) {
  const box = el('div', 'dr-diff');
  for (const l of diff) {
    if (l.type === 'gap') { box.appendChild(el('div', 'dr-line dr-gap', '…')); continue; }
    const row = el('div', `dr-line dr-${l.type}`);
    row.appendChild(el('span', 'dr-mark', MARK[l.type]));
    row.appendChild(el('span', 'dr-text', l.text));
    box.appendChild(row);
  }
  return box;
}

function sectionBlock(kind, s) {
  const d = el('details', `dr-section dr-${kind}`);
  const sum = el('summary');
  sum.appendChild(el('span', 'dr-badge', kind === 'added' ? 'Added' : kind === 'removed' ? 'Removed' : 'Changed'));
  sum.appendChild(el('span', 'dr-heading', s.heading || 'Top of document'));
  d.appendChild(sum);
  d.appendChild(renderDiff(s.diff));
  return d;
}

/**
 * Open the read-only review sheet.
 * deps: { apiBase, getDocId, getContent, getSuggestionCount, jumpToLine(line),
 *         showSuggestions(), previewPdf(), openExport(rect), fetchImpl?, now? }
 * Returns { close, root, ready } (ready resolves once the versions have loaded).
 */
export function mountReviewPanel(deps) {
  document.getElementById('doc-review-panel')?.remove();
  const root = el('aside', 'doc-review');
  root.id = 'doc-review-panel';
  root.setAttribute('role', 'dialog');
  root.setAttribute('aria-label', 'Review changes');

  const head = el('div', 'dr-head');
  head.appendChild(el('h2', 'dr-title', 'Review changes'));
  const closeBtn = el('button', 'dr-close', '×');
  closeBtn.type = 'button';
  closeBtn.setAttribute('aria-label', 'Close review');
  head.appendChild(closeBtn);
  const body = el('div', 'dr-body');
  const foot = el('div', 'dr-foot');
  const previewBtn = el('button', 'dr-btn', 'Preview PDF');
  const exportBtn = el('button', 'dr-btn dr-primary', 'Export…');
  previewBtn.type = exportBtn.type = 'button';
  foot.append(previewBtn, exportBtn);
  root.append(head, body, foot);
  document.body.appendChild(root);

  const onKey = (e) => { if (e.key === 'Escape' && root.isConnected) { e.stopPropagation(); close(); } };
  function close() {
    document.removeEventListener('keydown', onKey, true);
    root.remove();
  }
  document.addEventListener('keydown', onKey, true);
  closeBtn.addEventListener('click', close);
  previewBtn.addEventListener('click', () => deps.previewPdf?.());
  exportBtn.addEventListener('click', () => deps.openExport?.(exportBtn.getBoundingClientRect()));

  const now = () => (deps.now ? deps.now() : Date.now());
  let versions = [];

  function render(baseline) {
    body.replaceChildren();
    const live = deps.getContent();
    const placeholders = findPlaceholders(live);
    const diff = baseline ? diffSections(baseline.content || '', live) : null;
    const unresolved = deps.getSuggestionCount?.() || 0;
    const summary = reviewSummary({ diff, placeholders, unresolvedSuggestions: unresolved });

    if (summary.needsDecision) body.appendChild(el('div', 'dr-decision', 'Needs your decision'));
    const ul = el('ul', 'dr-summary');
    for (const l of summary.lines) ul.appendChild(el('li', `dr-sum-${l.key}`, l.text));
    body.appendChild(ul);

    if (versions.length < 2) {
      body.appendChild(el('p', 'dr-note', 'This is the first version. Nothing to compare yet.'));
    } else {
      const wrap = el('label', 'dr-compare');
      wrap.appendChild(el('span', null, 'Compare with'));
      const sel = el('select');
      sel.id = 'doc-review-compare';
      const def = defaultBaseline(versions, live);
      versions.forEach((v) => {
        const label = versionLabel(v, now());
        const o = el('option', null, v === def ? `Previous version (${label})` : label);
        o.value = String(v.version_number);
        if (v === baseline) o.selected = true;
        sel.appendChild(o);
      });
      sel.addEventListener('change', () => render(versions.find((v) => String(v.version_number) === sel.value)));
      wrap.appendChild(sel);
      body.appendChild(wrap);

      const changes = diff ? [
        ...diff.changed.map((s) => sectionBlock('changed', s)),
        ...diff.added.map((s) => sectionBlock('added', s)),
        ...diff.removed.map((s) => sectionBlock('removed', s)),
      ] : [];
      body.appendChild(el('h3', 'dr-h', 'Changed sections'));
      if (changes.length) changes.forEach((c) => body.appendChild(c));
      else body.appendChild(el('p', 'dr-note', 'No sections differ from this version.'));
    }

    body.appendChild(el('h3', 'dr-h', 'Placeholders'));
    if (!placeholders.length) body.appendChild(el('p', 'dr-note', 'None left.'));
    else {
      const list = el('ul', 'dr-placeholders');
      for (const p of placeholders) {
        const li = el('li');
        const b = el('button', 'dr-ph');
        b.type = 'button';
        b.dataset.line = String(p.line);
        b.appendChild(el('code', null, p.text));
        b.appendChild(el('span', 'dr-where', `line ${p.line}${p.section ? ' · ' + p.section : ''}`));
        b.addEventListener('click', () => deps.jumpToLine?.(p.line));
        li.appendChild(b);
        list.appendChild(li);
      }
      body.appendChild(list);
    }

    if (unresolved) {
      body.appendChild(el('h3', 'dr-h', 'Suggestions'));
      const b = el('button', 'dr-btn dr-link', `Show ${plural(unresolved, 'unresolved suggestion', 'unresolved suggestions')}`);
      b.type = 'button';
      b.addEventListener('click', () => deps.showSuggestions?.());
      body.appendChild(b);
    }
  }

  body.appendChild(el('p', 'dr-note', 'Loading…'));
  const doFetch = deps.fetchImpl || ((...a) => fetch(...a));
  const ready = (async () => {
    try {
      const res = await doFetch(`${deps.apiBase || ''}/api/document/${deps.getDocId()}/versions`);
      if (!res.ok) throw new Error(String(res.status));
      versions = await res.json();
    } catch {
      versions = [];
      render(null);
      body.prepend(el('p', 'dr-note', 'Could not load earlier versions.'));
      return;
    }
    render(defaultBaseline(versions, deps.getContent()));
  })();
  return { close, root, ready };
}
