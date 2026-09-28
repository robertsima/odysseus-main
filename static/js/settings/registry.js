// Canonical metadata for the existing Settings information architecture.
//
// This module describes Settings; it does not render the sidebar, load panel
// data, or own panel behavior. Keeping those concerns separate lets the
// current markup remain stable while navigation/search code shares one source
// of truth for panel identity and ownership.

function defineGroup(definition) {
  return Object.freeze({ ...definition });
}

function definePanel(definition) {
  return Object.freeze({
    controller: 'settings',
    adminOnly: false,
    ...definition,
    keywords: Object.freeze([...(definition.keywords || [])]),
    aliases: Object.freeze([...(definition.aliases || [])]),
  });
}

export const SETTINGS_GROUPS = Object.freeze([
  defineGroup({
    id: 'models',
    label: 'Models & AI',
  }),
  defineGroup({
    id: 'communications',
    label: 'Communications',
  }),
  defineGroup({
    id: 'experience',
    label: 'Experience',
  }),
  defineGroup({
    id: 'account',
    label: 'Account',
  }),
  defineGroup({
    id: 'administration',
    label: 'Administration',
    adminOnly: true,
  }),
]);

// Order intentionally mirrors the Settings sidebar.
//
// `aliases` keeps retired tab ids working: code (and muscle memory, e.g.
// `/settings ai`) that still opens an old id lands on the tab that now owns
// those controls instead of on nothing.
export const SETTINGS_PANELS = Object.freeze([
  definePanel({
    id: 'models',
    label: 'Models',
    group: 'models',
    controller: 'admin',
    aliases: ['services', 'added-models', 'ai', 'context'],
    keywords: [
      'models', 'model roles', 'chat model', 'default model', 'utility', 'vision',
      'research model', 'provider', 'providers', 'endpoint', 'add models',
      'api key', 'ollama', 'local', 'scan network', 'voice', 'dictation', 'stt',
      'whisper', 'image generation', 'fallback', 'context', 'window',
      'compaction', 'tokens',
    ],
  }),
  definePanel({
    id: 'search',
    label: 'Search & Research',
    group: 'models',
    keywords: [
      'search', 'web search', 'research', 'deep research', 'provider', 'searxng',
      'duckduckgo', 'brave', 'google', 'tavily', 'serper', 'safesearch',
      'safe search', 'results', 'fallback', 'timeout', 'max tokens',
    ],
  }),

  definePanel({
    id: 'agents',
    label: 'Agents',
    group: 'models',
    controller: 'admin',
    adminOnly: true,
    aliases: ['tools'],
    keywords: [
      'agent', 'agents', 'agent tools', 'tools', 'built-in tools', 'approvals',
      'approval mode', 'tool calls', 'context cap', 'reasoning effort',
      'peer messaging', 'workspace', 'development folders', 'repository roots',
      'branch', 'shell', 'sandbox', 'workbench', 'profiles', 'loadouts',
      'control room', 'capabilities',
    ],
  }),
  definePanel({
    id: 'claude-code',
    label: 'Claude Code',
    group: 'models',
    adminOnly: true,
    keywords: [
      'claude', 'claude code', 'delegation', 'sign in', 'cloud runner',
      'github actions', 'hub repository', 'workflow', 'transcript', 'auto-update',
    ],
  }),

  definePanel({
    id: 'integrations',
    label: 'Integrations',
    group: 'communications',
    controller: 'admin',
    keywords: ['integrations', 'connections', 'services'],
  }),
  definePanel({
    id: 'email',
    label: 'Email',
    group: 'communications',
    keywords: ['email', 'imap', 'smtp', 'oauth'],
  }),
  definePanel({
    id: 'reminders',
    label: 'Reminders',
    group: 'communications',
    keywords: ['reminders', 'notifications', 'alerts'],
  }),

  definePanel({
    id: 'appearance',
    label: 'Appearance',
    group: 'experience',
    keywords: ['appearance', 'theme', 'font', 'density', 'peek'],
  }),
  definePanel({
    id: 'shortcuts',
    label: 'Shortcuts',
    group: 'experience',
    keywords: ['shortcuts', 'keyboard', 'hotkeys'],
  }),

  definePanel({
    id: 'privacy',
    label: 'Privacy',
    group: 'account',
    controller: 'admin',
    keywords: ['privacy', 'sensitive', 'blur', 'private', 'vault'],
  }),
  definePanel({
    id: 'account',
    label: 'Account',
    group: 'account',
    keywords: ['account', 'password', 'logout'],
  }),

  definePanel({
    id: 'users',
    label: 'Users',
    group: 'administration',
    controller: 'admin',
    adminOnly: true,
    keywords: ['users', 'accounts', 'admin'],
  }),
  definePanel({
    id: 'capabilities',
    label: 'Configuration',
    group: 'administration',
    controller: 'admin',
    adminOnly: true,
    keywords: ['configuration', 'capabilities', 'skills', 'plugins', 'mcp'],
  }),
  definePanel({
    id: 'system',
    label: 'System',
    group: 'administration',
    controller: 'admin',
    adminOnly: true,
    keywords: ['system', 'admin', 'server'],
  }),
]);

export const DEFAULT_SETTINGS_PANEL_ID = 'models';

const _panelsById = new Map(
  SETTINGS_PANELS.map(panel => [panel.id, panel]),
);

const _panelIdByAlias = new Map(
  SETTINGS_PANELS.flatMap(panel => panel.aliases.map(alias => [alias, panel.id])),
);

/**
 * Map any tab id, current or retired, to the id of the panel that owns it now.
 * Unknown ids come back unchanged so callers can still report them.
 */
export function resolveSettingsPanelId(id) {
  const key = String(id || '').trim().toLowerCase();
  if (!key) return '';
  if (_panelsById.has(key)) return key;
  return _panelIdByAlias.get(key) || key;
}

export function getSettingsPanel(id) {
  return _panelsById.get(resolveSettingsPanelId(id)) || null;
}

export function getSettingsPanelsForGroup(groupId) {
  return SETTINGS_PANELS.filter(panel => panel.group === groupId);
}

export function isAdminManagedSettingsTab(id) {
  return getSettingsPanel(id)?.controller === 'admin';
}

export function isAdminOnlySettingsTab(id) {
  return getSettingsPanel(id)?.adminOnly === true;
}

export function getSettingsPanelSearchText(panelOrId) {
  const panel = typeof panelOrId === 'string'
    ? getSettingsPanel(panelOrId)
    : panelOrId;

  if (!panel) return '';

  return [
    panel.label,
    ...(panel.keywords || []),
    ...(panel.aliases || []),
  ].join(' ').toLowerCase();
}

function normalizeSettingsSearch(value) {
  return String(value || '')
    .trim()
    .toLowerCase()
    .replace(/\s+/g, ' ');
}

export function searchSettingsPanels(query, options = {}) {
  const normalized = normalizeSettingsSearch(query);
  if (!normalized) return [];

  const terms = normalized.split(' ');
  const isAdmin = options.isAdmin === true;

  return SETTINGS_PANELS.filter(panel => {
    if (panel.adminOnly && !isAdmin) return false;

    const haystack = getSettingsPanelSearchText(panel);
    return terms.every(term => haystack.includes(term));
  });
}

export function getSettingsRegistryIssues(modalEl) {
  if (!modalEl) return ['Settings modal is unavailable'];

  const tabIds = Array.from(
    modalEl.querySelectorAll('[data-settings-tab]'),
    element => element.dataset.settingsTab,
  ).filter(Boolean);

  const panelIds = Array.from(
    modalEl.querySelectorAll('[data-settings-panel]'),
    element => element.dataset.settingsPanel,
  ).filter(Boolean);

  const registryIds = SETTINGS_PANELS.map(panel => panel.id);
  const issues = [];

  const duplicates = ids => ids.filter(
    (id, index) => ids.indexOf(id) !== index,
  );

  for (const id of new Set(duplicates(tabIds))) {
    issues.push(`Duplicate Settings tab: ${id}`);
  }
  for (const id of new Set(duplicates(panelIds))) {
    issues.push(`Duplicate Settings panel: ${id}`);
  }

  for (const id of registryIds) {
    if (!tabIds.includes(id)) issues.push(`Registry tab missing from DOM: ${id}`);
    if (!panelIds.includes(id)) issues.push(`Registry panel missing from DOM: ${id}`);
  }

  for (const id of tabIds) {
    if (!registryIds.includes(id)) issues.push(`DOM tab missing from registry: ${id}`);
  }
  for (const id of panelIds) {
    if (!registryIds.includes(id)) issues.push(`DOM panel missing from registry: ${id}`);
  }

  return issues;
}
