/* Declarative plugin picker for the Agent Control Room.
 * Mount is intentionally inert until a human selects plugins and confirms.
 */
(function () {
  async function request(url, options) {
    if (typeof window.api === 'function') return window.api(url, options);
    const response = await fetch(url, Object.assign({headers: {'Content-Type': 'application/json'}}, options));
    if (!response.ok) throw new Error((await response.text()) || `HTTP ${response.status}`);
    return response.json();
  }

  function esc(value) {
    const node = document.createElement('span');
    node.textContent = String(value == null ? '' : value);
    // textContent escapes markup, but not quotes used in title attributes.
    return node.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  async function mount(container, options) {
    if (!container || container.dataset.pluginCatalogMounted === '1') return;
    const sessionId = options && options.sessionId;
    if (!sessionId) return;
    const call = options && typeof options.api === 'function' ? options.api : request;
    container.dataset.pluginCatalogMounted = '1';
    container.innerHTML = '<small>Loading plugins…</small>';
    try {
      const base = `/api/plugins/sessions/${encodeURIComponent(sessionId)}`;
      const data = await call(`${base}/catalog`);
      const enabled = new Set(data.enabled_plugins || []);
      container.innerHTML = `<details class="ag-connection-section"><summary><b>Plugins</b> <small>Capability bundles; never installers</small></summary>
        <div class="ag-cap-grid">${data.plugins.length ? data.plugins.map((plugin) => {
          const caps = plugin.capabilities || {};
          const detail = ['skills', 'mcp_servers', 'tools', 'models'].map((key) =>
            `${key.replace('_', ' ')}: ${(caps[key] || []).join(', ') || 'none'}`).join(' · ');
          const mcpState = (caps.mcp_servers || []).map((id) => `${id}: ${(data.mcp_status || {})[id] || 'unknown'}`).join(', ');
          return `<label title="${esc(plugin.description)}"><input type="checkbox" data-plugin-id="${esc(plugin.id)}" ${enabled.has(plugin.id) ? 'checked' : ''}> ${esc(plugin.name)}<small>${esc(detail)}${mcpState ? ` · MCP status: ${esc(mcpState)}` : ''}</small></label>`;
        }).join('') : '<small>No plugins configured. An administrator can import a manifest below.</small>'}</div>
        <div data-plugin-warnings>${(data.policy_warnings || []).map((warning) => `<small>⚠ ${esc(warning)}</small>`).join('<br>')}</div>
        <div><button type="button" class="wb-btn" data-plugin-preview>Preview & apply</button> <span data-plugin-message></span></div>
        ${data.can_import ? `<details><summary>Import manifest (admin)</summary><textarea class="wb-input ag-textarea" data-plugin-import rows="5" aria-label="Plugin JSON manifest" placeholder='{"schema_version":1,"id":"my-plugin",...}'></textarea><button type="button" class="wb-btn" data-plugin-import-button>Import</button></details>` : ''}</details>`;
      container.querySelector('[data-plugin-preview]').addEventListener('click', async () => {
        const message = container.querySelector('[data-plugin-message]');
        try {
          const ids = [...container.querySelectorAll('[data-plugin-id]:checked')].map((node) => node.dataset.pluginId);
          const selected = data.plugins.filter((plugin) => ids.includes(plugin.id));
          const summary = ['skills', 'mcp_servers', 'tools', 'models'].map((key) => {
            const names = [...new Set(selected.flatMap((plugin) => plugin.capabilities[key] || []))];
            return `${key.replace('_', ' ')}: ${names.length ? names.join(', ') : 'none'}`;
          }).join('\n');
          if (!window.confirm(`Apply these plugin capability references to this agent?\n\n${summary}\n\nNo installers or MCP tools will run.`)) return;
          const result = await call(`${base}/enabled`, {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({plugin_ids: ids})});
          message.textContent = `Saved ${result.plugin_ids.length} plugin selection(s).`;
          container.querySelector('[data-plugin-warnings]').innerHTML = (result.policy_warnings || []).map((warning) => `<small>⚠ ${esc(warning)}</small>`).join('<br>');
          window.dispatchEvent(new CustomEvent('odysseus:plugin-loadout-applied', {detail: {sessionId, result}}));
          if (typeof options.onApplied === 'function') await options.onApplied(result);
        } catch (error) { message.textContent = `Apply failed: ${error.message || error}`; }
      });
      container.querySelector('[data-plugin-import-button]')?.addEventListener('click', async () => {
        const message = container.querySelector('[data-plugin-message]');
        try {
          const manifest = JSON.parse(container.querySelector('[data-plugin-import]').value);
          await call(`/api/plugins/${encodeURIComponent(manifest.id || '')}`, {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(manifest)});
          container.dataset.pluginCatalogMounted = '0'; await mount(container, options);
        } catch (error) { message.textContent = `Import failed: ${error.message || error}`; }
      });
    } catch (error) {
      container.innerHTML = `<small>Plugins unavailable: ${esc(error.message || error)}</small>`;
    }
  }

  window.OdysseusPluginCatalog = {mount};
})();
