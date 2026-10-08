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

  // The exact line the server compares against before it installs anything.
  function commandLine(server) {
    return server.transport === 'stdio' ? [server.command].concat(server.args || []).join(' ') : server.url;
  }

  // v2 packages: show what would run and what it would create, then ask for env
  // values. Secrets are typed here and sent once; the manifest never holds them.
  function integrationHtml(plugin, install, canInstall) {
    const block = plugin.integration;
    if (!block) return '';
    const server = block.mcp_server;
    const skills = block.skills || [];
    const templates = (block.loadout_templates || []).flatMap((doc) => (doc.profiles || []).map((p) => p && p.name)).filter(Boolean);
    const rows = [];
    if (server) {
      rows.push(`<div>Server: <b>${esc(server.name)}</b> (${esc(server.transport)})</div>`);
      rows.push(`<div>${server.transport === 'stdio' ? 'Runs on this host' : 'Connects to'}: <code data-plugin-command>${esc(commandLine(server))}</code></div>`);
      rows.push(`<div>Environment: ${(server.env || []).length ? server.env.map((e) => `${esc(e.name)}${e.required ? '' : ' (optional)'}${e.description ? ` - ${esc(e.description)}` : ''}`).join('; ') : 'none'}</div>`);
    }
    if (block.instructions) rows.push(`<div>Prompt text (shown to the agent as untrusted context): ${esc(block.instructions)}</div>`);
    rows.push(`<div>Skills: ${skills.length ? skills.map((x) => esc(x.name)).join(', ') : 'none'}</div>`);
    rows.push(`<div>Loadout templates: ${templates.length ? templates.map(esc).join(', ') : 'none'}</div>`);
    let action = '';
    if (install) {
      action = `<div><small>Installed${install.server_id ? ` as server ${esc(install.server_id)}` : ''}; skills: ${esc((install.skills || []).join(', ') || 'none')}; loadouts: ${esc((install.loadouts || []).join(', ') || 'none')}.</small></div>
        ${canInstall ? `<button type="button" class="wb-btn" data-plugin-uninstall="${esc(plugin.id)}">Uninstall</button>` : ''}`;
    } else if (canInstall) {
      action = `<form data-plugin-install-form="${esc(plugin.id)}" autocomplete="off">
        ${(server && server.env || []).map((e) => `<label>${esc(e.name)} <input type="password" class="wb-input" autocomplete="off" data-env-name="${esc(e.name)}" ${e.required ? 'required' : ''}></label>`).join('')}
        <label><input type="checkbox" data-plugin-publish> Publish the skills (otherwise they stay drafts)</label>
        ${server ? '<label><input type="checkbox" data-plugin-approve required> I have read the command above and approve running it</label>' : ''}
        <button type="submit" class="wb-btn">Install</button></form>`;
    }
    return `<details data-plugin-integration="${esc(plugin.id)}"><summary>Integration: ${esc(block.name)}</summary>${rows.join('')}${action}</details>`;
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
          return `<label title="${esc(plugin.description)}"><input type="checkbox" data-plugin-id="${esc(plugin.id)}" ${enabled.has(plugin.id) ? 'checked' : ''}> ${esc(plugin.name)}<small>${esc(detail)}${mcpState ? ` · MCP status: ${esc(mcpState)}` : ''}</small></label>${integrationHtml(plugin, (data.installs || {})[plugin.id], !!data.can_import)}`;
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
      if (data.can_import) {
        const flash = (text) => { const m = container.querySelector('[data-plugin-message]'); if (m) m.textContent = text; };
        const remount = async () => { container.dataset.pluginCatalogMounted = '0'; await mount(container, options); };
        container.querySelectorAll('[data-plugin-install-form]').forEach((form) => form.addEventListener('submit', async (event) => {
          event.preventDefault();
          const id = form.dataset.pluginInstallForm;
          const plugin = data.plugins.find((p) => p.id === id);
          const server = plugin && plugin.integration && plugin.integration.mcp_server;
          const env = {};
          form.querySelectorAll('[data-env-name]').forEach((input) => { env[input.dataset.envName] = input.value; });
          const line = server ? commandLine(server) : '';
          if (!window.confirm(`Install ${plugin.name}?${server ? `

${server.transport === 'stdio' ? 'This will run on the host' : 'This will connect to'}:
${line}` : ''}`)) return;
          try {
            const result = await call(`/api/plugins/${encodeURIComponent(id)}/install`, {method: 'POST', headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({env, approved: line, publish_skills: !!form.querySelector('[data-plugin-publish]')?.checked})});
            const notes = (result.unresolved_references || []).concat((result.loadout_errors || []).map((e) => `${e.name}: ${e.error}`));
            await remount();
            flash(`Installed: server ${result.server ? (result.server.connected ? 'connected' : (result.server.error || result.server.status || 'not connected')) : 'none'}; ${result.skills.length} skill(s); ${result.loadouts.length} loadout(s).${notes.length ? ` Not resolved: ${notes.join(' | ')}` : ''}`);
          } catch (error) { flash(`Install failed: ${error.message || error}`); }
        }));
        container.querySelectorAll('[data-plugin-uninstall]').forEach((button) => button.addEventListener('click', async () => {
          const id = button.dataset.pluginUninstall;
          if (!window.confirm(`Uninstall ${id}? This removes the MCP server, skills and loadouts it created.`)) return;
          try {
            await call(`/api/plugins/${encodeURIComponent(id)}/install`, {method: 'DELETE'});
            await remount();
          } catch (error) { flash(`Uninstall failed: ${error.message || error}`); }
        }));
      }
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

  window.AgamemnonPluginCatalog = {mount};
  // Compatibility for installed extensions upgrading independently.
  window.OdysseusPluginCatalog = window.AgamemnonPluginCatalog;
})();
