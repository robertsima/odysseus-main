/** Required-route pickers cannot inherit an empty model. Custom IDs use an
 * explicitly chosen, already configured endpoint; never infer a provider. */
export function customModelRoute(models, onPick) {
  const wrap = document.createElement('details');
  wrap.className = 'custom-model-route';
  const summary = document.createElement('summary');
  summary.textContent = 'Custom model override';
  const endpoint = document.createElement('select');
  endpoint.className = 'cmp-form-control set-input';
  endpoint.setAttribute('aria-label', 'Custom model endpoint');
  const routes = new Map();
  models.forEach(m => { if (m.url && !routes.has(m.url)) routes.set(m.url, m); });
  routes.forEach((m, url) => {
    const option = document.createElement('option'); option.value = url;
    option.textContent = m.epName || m.endpointName || url; endpoint.appendChild(option);
  });
  const input = document.createElement('input'); input.type = 'text';
  input.className = 'cmp-form-control set-input';
  input.placeholder = 'Custom model ID'; input.setAttribute('aria-label', 'Custom model ID');
  const button = document.createElement('button'); button.type = 'button';
  button.textContent = 'Use custom model'; button.disabled = true;
  input.addEventListener('input', () => { button.disabled = !input.value.trim() || !routes.size; });
  button.addEventListener('click', () => {
    const route = routes.get(endpoint.value), id = input.value.trim();
    if (!route || !id) return;
    onPick({ ...route, id, mid: id, model: id, name: id, display: id,
      endpoint: route.url, endpointId: route.endpointId || route.endpoint_id });
  });
  wrap.append(summary, endpoint, input, button);
  if (!routes.size) {
    const status = document.createElement('small');
    status.textContent = 'Configure an endpoint before selecting a custom model.'; wrap.appendChild(status);
  }
  return wrap;
}
