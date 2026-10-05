// The Agent Control Room (static/js/agentsDashboard.js) on a node page: the
// room's window markup, the agents routes it polls, a stand-in for the
// browser's EventSource that the test feeds, and the open chat. Each test
// file gets one room (node runs every file in its own process).
//
//   const room = await openRoom({ rows: [...] });
//   room.dashboard.open();
//   room.feed({ kind: 'run_started', ... });   // one event on /api/agents/stream
import { after } from 'node:test';

import { installDom, waitFor } from '../_support/dom.mjs';
import { installFetchFake } from '../_support/fetchFake.mjs';

export { waitFor };

class FakeEventSource {
  static last = null;
  constructor(url) {
    this.url = url;
    this.onmessage = null;
    FakeEventSource.last = this;
  }
  close() {}
}

const ROOM = `
  <div id="agents-dashboard" class="modal hidden" hidden tabindex="-1">
    <div class="agents-modal-content">
      <div class="agents-window-header"><button id="close-agents-dashboard" type="button">x</button></div>
      <div id="agents-dashboard-body"></div>
    </div>
  </div>
  <div id="toast"></div>`;

export async function openRoom({ rows = [], profiles = [], chats = [], currentSession = null, personaTemplates = [] } = {}) {
  const dom = installDom();
  const fake = installFetchFake();
  after(async () => {
    fake.restore();
    await dom.restore();
  });
  globalThis.EventSource = FakeEventSource;
  document.body.innerHTML = ROOM;

  const overview = { rows, profiles, chats, totals: {}, profile_problems: [], can_edit_loadouts: true };
  // Headers of every request, which the fetch fake does not record.
  const headers = [];
  const fakeFetch = globalThis.fetch;
  globalThis.fetch = (input, init = {}) => {
    headers.push({ url: String(input), headers: init.headers || {} });
    return fakeFetch(input, init);
  };
  fake.route('GET', '/api/agents/overview', () => overview);
  fake.route('GET', '/api/agents/approvals', () => ({ approvals: [] }));
  fake.route('GET', '/api/agents/history', () => ({ chats: [], total: 0 }));
  fake.route('GET', '/api/agents/catalog', () => ({ tools: [], skills: [], mcp_servers: [], models: [], provider_limits: {} }));
  fake.route('GET', '/api/presets/templates', () => personaTemplates);
  fake.route('GET', '/api/agents/profiles/templates', () => ({ templates: [] }));

  const session = { current: currentSession };
  window.sessionModule = { getCurrentSessionId: () => session.current };

  const { default: dashboard } = await import('../../../../static/js/agentsDashboard.js');
  const root = document.getElementById('agents-dashboard');
  await waitFor(() => fake.calls.some((c) => c.url.pathname === '/api/agents/overview'), { what: 'the first poll' });

  return {
    fake,
    dashboard,
    root,
    overview,
    headers,
    session,
    isOpen: () => !root.hidden && !root.classList.contains('hidden'),
    // One event on the user's activity stream.
    feed(event) {
      FakeEventSource.last.onmessage({ data: JSON.stringify(event) });
    },
    click(selector) {
      const el = root.querySelector(selector);
      if (!el) throw new Error(`no ${selector} in the room`);
      el.click();
    },
  };
}
