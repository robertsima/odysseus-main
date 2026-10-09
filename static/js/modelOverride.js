/** Adds catalog/default/custom choices without changing the submitted control.
 * The original input/select keeps its id, listeners, disabled state and value.
 * Custom select values are real options, never a sentinel sent to the server. */
const CUSTOM = '__odysseus_custom_model__';
let catalogRequest;
let catalogTime = 0;
export function catalogChoices(data, modelType = 'llm', qualified = true) {
  const choices = [];
  for (const ep of data.items || []) {
    if (ep.offline || (ep.model_type || 'llm') !== modelType) continue;
    for (const model of [...(ep.models || []), ...(ep.models_extra || [])]) {
      const id = typeof model === 'string' ? model : model.id;
      if (!id) continue;
      const name = ep.endpoint_name || ep.name;
      const value = qualified && name ? `${id}@${name}` : id;
      if (!choices.some(c => c.value === value)) choices.push({ value, label: name ? `${id} -- ${name}` : id });
    }
  }
  return choices;
}
function catalog() {
  if (!catalogRequest || Date.now() - catalogTime > 30000) {
    catalogTime = Date.now();
    catalogRequest = fetch('/api/models', { credentials: 'same-origin' })
    .then(r => { if (!r.ok) throw new Error('Model catalog unavailable'); return r.json(); })
    .catch(e => { catalogRequest = null; throw e; });
  }
  return catalogRequest;
}
export function retainModel(select, value) {
  if (value && !Array.from(select.options).some(o => o.value === value)) {
    const option = document.createElement('option'); option.value = value;
    option.textContent = `${value} (custom / unlisted)`; select.appendChild(option);
  }
  select.value = value || '';
}
export function enhanceModelControl(control, { defaultLabel = 'Default / inherit', loadCatalog = false, modelType = 'llm', qualified = true } = {}) {
  if (!control || control.dataset.modelOverride) return;
  control.dataset.modelOverride = '1';
  const isList = control.tagName === 'TEXTAREA';
  const isInput = control.tagName === 'INPUT' || isList;
  const wrap = document.createElement('span'); wrap.className = 'model-override-control';
  control.before(wrap); wrap.appendChild(control);
  const select = isInput ? document.createElement('select') : control;
  const custom = isInput ? control : document.createElement('input');
  select.className = control.className; custom.className = control.className;
  const label = control.getAttribute('aria-label') || control.closest('label')?.querySelector('span')?.textContent || 'Model';
  select.setAttribute('aria-label', `${label} choices`);
  custom.setAttribute('aria-label', `${label} custom override`);
  custom.placeholder = control.id === 'task-form-model' ? 'Model ID or endpoint_url::model_id' : 'Custom model ID or model@endpoint';
  if (isInput) wrap.prepend(select); else wrap.appendChild(custom);
  let value = control.value || '';
  let customMode = false;
  function sync() {
    for (const el of [select, custom]) if (el.disabled !== control.disabled) el.disabled = control.disabled;
    if (!Array.from(select.options).some(o => o.value === '')) {
      const blank = document.createElement('option'); blank.value = ''; blank.textContent = defaultLabel; select.prepend(blank);
    }
    if (!Array.from(select.options).some(o => o.value === CUSTOM)) {
      const option = document.createElement('option'); option.value = CUSTOM; option.textContent = 'Custom text override…'; select.appendChild(option);
    }
    if (isInput) {
      const listed = Array.from(select.options).some(o => o.value === control.value && o.value !== CUSTOM);
      customMode = customMode || (!listed && !!control.value);
      select.value = customMode ? CUSTOM : control.value;
    } else {
      value = control.value === CUSTOM ? value : control.value;
      if (customMode) custom.value = value;
    }
    custom.hidden = !customMode;
  }
  select.addEventListener('change', e => {
    if (select.value === CUSTOM) {
      e.stopImmediatePropagation(); customMode = true;
      if (!isInput) retainModel(control, value);
      custom.value = value; sync(); custom.focus(); return;
    }
    customMode = false; value = select.value;
    if (isList && value) {
      const entries = control.value.split(/\n/).map(s => s.trim()).filter(Boolean);
      if (!entries.includes(value)) entries.push(value);
      value = entries.join('\n'); customMode = true;
    }
    if (isInput) {
      control.value = value;
      control.dispatchEvent(new Event('input', { bubbles: true }));
      control.dispatchEvent(new Event('change', { bubbles: true }));
    }
    sync();
  }, true);
  custom.addEventListener('input', () => {
    value = custom.value;
    if (!isInput) { retainModel(control, value); control.dispatchEvent(new Event('change', { bubbles: true })); customMode = true; }
  });
  new MutationObserver(sync).observe(control, { attributes: true, attributeFilter: ['disabled'] });
  if (!isInput) {
    control.addEventListener('change', () => { value = control.value; });
    new MutationObserver(sync).observe(control, { childList: true, subtree: true });
  }
  sync();
  if (isInput && loadCatalog) {
    const status = document.createElement('small'); status.className = 'model-override-status'; status.textContent = 'Loading models…'; wrap.appendChild(status);
    catalog().then(data => {
      const choices = catalogChoices(data, modelType, qualified);
      choices.forEach(c => { const o = document.createElement('option'); o.value = c.value; o.textContent = c.label; select.insertBefore(o, select.lastElementChild); });
      value = control.value; sync(); status.remove();
    }).catch(() => { status.textContent = 'Catalog unavailable; default and custom override still work.'; });
  }
}
// Dynamic forms (Phalanx, task/research/assistant dialogs). Endpoint-specific
// forms own catalog refresh; this adapter only adds the custom/default choice.
export function initModelOverrides() {
  const scan = () => {
    document.querySelectorAll('#ag-model, #set-ccModel, input[data-model-override-control]').forEach(el => enhanceModelControl(el, { loadCatalog: true }));
    document.querySelectorAll('textarea[data-model-list-control]').forEach(el => enhanceModelControl(el, { loadCatalog: true, defaultLabel: 'Default / empty list (pick models to add)' }));
    document.querySelectorAll('#task-form-model, #research-model, #assistant-model, select[data-model-select-control]').forEach(el => enhanceModelControl(el));
  };
  new MutationObserver(scan).observe(document.body, { childList: true, subtree: true }); scan();
}
if (typeof document !== 'undefined') {
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initModelOverrides, { once: true });
  else initModelOverrides();
}
