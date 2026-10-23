"""Account preferences follow the account, not the browser.

The theme was localStorage-only in practice: it consulted the server only when
localStorage was completely empty, so a browser that had ever picked a theme
never saw a change made anywhere else. These tests drive the reconcile rules in
`serverPrefs.js` under Node with a stubbed `fetch`. The navigation layout's
agreement with the account is tested in tests/static/js/navLayout/, the theme
boot in tests/static/js/theme/ and the route in tests/routes/prefs_routes/.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from tests import REPO_ROOT

ROOT = REPO_ROOT
JS = ROOT / "static" / "js"


def run_node(script: str) -> subprocess.CompletedProcess:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is not installed")
    return subprocess.run([node, "--input-type=module"], input=script, text=True, capture_output=True)


def url(name: str) -> str:
    return json.dumps((JS / name).resolve().as_uri())


# A localStorage + fetch stand-in. `store` seeds the browser copy, `server`
# seeds the account copy, and `puts` records everything written back.
HARNESS = """
const store = new Map(Object.entries(SEED_LOCAL));
globalThis.window = { localStorage: null };
const localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
};
globalThis.localStorage = localStorage;
globalThis.window.localStorage = localStorage;
const server = new Map(Object.entries(SEED_SERVER));
const puts = [];
globalThis.fetch = async (path, opts = {}) => {
  const key = decodeURIComponent(String(path).replace('/api/prefs/', ''));
  if ((opts.method || 'GET') === 'GET') {
    return { ok: true, json: async () => ({ key, value: server.has(key) ? server.get(key) : null }) };
  }
  const body = JSON.parse(opts.body);
  puts.push([key, body.value]);
  server.set(key, body.value);
  return { ok: true, json: async () => body };
};
const settled = () => new Promise((r) => setTimeout(r, 260));
const report = (extra) => console.log(JSON.stringify({ puts, store: Object.fromEntries(store), ...extra }));
"""


def node_case(seed_local: dict, seed_server: dict, body: str) -> dict:
    script = (
        HARNESS.replace("SEED_LOCAL", json.dumps(seed_local)).replace("SEED_SERVER", json.dumps(seed_server))
        + body
    )
    result = run_node(script)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


# ── reconcile ────────────────────────────────────────────────────────────────

RECONCILE = "const { reconcile } = await import(%s);\n" % url("serverPrefs.js")


def test_a_newer_account_value_wins_over_this_browsers_copy():
    out = node_case({}, {"demo": {"value": "remote", "updated_at": 200}},
                    RECONCILE + """
const r = await reconcile('demo', { value: 'local', updated_at: 100 });
await settled(); report({ source: r.source, value: r.entry.value });
""")
    assert out["source"] == "remote"
    assert out["value"] == "remote"
    assert out["puts"] == []


def test_a_newer_local_value_is_pushed_up():
    out = node_case({}, {"demo": {"value": "remote", "updated_at": 100}},
                    RECONCILE + """
const r = await reconcile('demo', { value: 'local', updated_at: 300 });
await settled(); report({ source: r.source });
""")
    assert out["source"] == "local"
    assert out["puts"] == [["demo", {"value": "local", "updated_at": 300}]]


def test_an_empty_account_is_seeded_from_this_browser():
    out = node_case({}, {}, RECONCILE + """
const r = await reconcile('demo', { value: 'local', updated_at: 50 });
await settled(); report({ source: r.source });
""")
    assert out["source"] == "local"
    assert out["puts"] == [["demo", {"value": "local", "updated_at": 50}]]


def test_two_unstamped_legacy_values_converge_on_the_account_copy():
    """Both sides predate stamping. Preferring the server is what makes the
    preference an account setting rather than a per-browser one."""
    out = node_case({}, {"demo": "from-account"}, RECONCILE + """
const r = await reconcile('demo', 'from-this-browser');
await settled(); report({ source: r.source, value: r.entry.value });
""")
    assert out["source"] == "remote"
    assert out["value"] == "from-account"


def test_an_unreachable_server_leaves_the_local_value_alone():
    out = node_case({}, {}, RECONCILE + """
globalThis.fetch = async () => { throw new Error('offline'); };
const r = await reconcile('demo', { value: 'local', updated_at: 10 });
await settled(); report({ source: r.source, value: r.entry.value });
""")
    assert out["source"] == "local"
    assert out["value"] == "local"


def test_bursts_of_writes_collapse_into_one_request():
    out = node_case({}, {}, """
const { writePref } = await import(%s);
writePref('demo', ['a'], 1); writePref('demo', ['a', 'b'], 2); writePref('demo', ['a', 'b', 'c'], 3);
await settled(); report({});
""" % url("serverPrefs.js"))
    assert out["puts"] == [["demo", {"value": ["a", "b", "c"], "updated_at": 3}]]


# ── ownership: a stale local copy must not become the account's ──────────────

AUTH = """
globalThis.fetch = (function (inner) {
  return async (path, opts) => {
    if (String(path).startsWith('/api/auth/status')) {
      return { ok: true, json: async () => ({ username: LIVE_USER }) };
    }
    return inner(path, opts);
  };
})(globalThis.fetch);
"""


def auth_case(live_user, seed_local, seed_server, body):
    return node_case(seed_local, seed_server,
                     AUTH.replace("LIVE_USER", json.dumps(live_user)) + body)


def test_a_previous_users_local_copy_is_never_pushed_up_as_this_accounts():
    """Signing in as a second user on a shared browser must not overwrite that
    user's stored preference with the first user's leftover copy."""
    out = auth_case("bea", {"odysseus-auth-user": "al"},
                    {"demo": {"value": "beas", "updated_at": 1}},
                    RECONCILE + """
const r = await reconcile('demo', { value: 'als', updated_at: 999 });
await settled(); report({ source: r.source, value: r.entry.value });
""")
    assert out["source"] == "remote"
    assert out["value"] == "beas"
    assert out["puts"] == []


def test_a_previous_users_copy_does_not_seed_an_empty_account():
    out = auth_case("bea", {"odysseus-auth-user": "al"}, {}, RECONCILE + """
const r = await reconcile('demo', { value: 'als', updated_at: 999 });
await settled(); report({ source: r.source });
""")
    assert out["source"] == "none"
    assert out["puts"] == []


def test_the_signed_in_users_own_copy_still_syncs_normally():
    out = auth_case("al", {"odysseus-auth-user": "al"},
                    {"demo": {"value": "old", "updated_at": 1}},
                    RECONCILE + """
const r = await reconcile('demo', { value: 'fresh', updated_at: 999 });
await settled(); report({ source: r.source });
""")
    assert out["source"] == "local"
    assert out["puts"] == [["demo", {"value": "fresh", "updated_at": 999}]]
