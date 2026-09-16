"""Navigation order and theme follow the account, not the browser.

Both were localStorage-only in practice: the navigation order never reached the
server at all, and the theme consulted it only when localStorage was completely
empty — so a browser that had ever picked a theme never saw a change made
anywhere else. These tests drive the reconcile rules in `serverPrefs.js` and
`navOrder.js` under Node with a stubbed `fetch`, and pin the boot behavior the
theme module now depends on.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "static" / "js"
SERVER_PREFS = (JS / "serverPrefs.js").read_text(encoding="utf-8")
NAV_ORDER = (JS / "navOrder.js").read_text(encoding="utf-8")
THEME = (JS / "theme.js").read_text(encoding="utf-8")


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


def test_theme_boot_applies_the_same_ownership_guard():
    guard = THEME.split("async function _syncThemeWithAccount()", 1)[1].split("\n}", 1)[0]
    assert "await localCopyBelongsToAccount()" in guard
    assert "const local = mine ? getSaved() : null;" in guard
    custom = THEME.split("async function _syncCustomThemesWithAccount()", 1)[1].split("\n}", 1)[0]
    assert "await localCopyBelongsToAccount()" in custom


# ── navigation order ─────────────────────────────────────────────────────────

NAV_DOM = """
const nodes = new Map();
const doc = {
  getElementById: (id) => nodes.get(id) || null,
  createComment: () => ({ comment: true, remove() { this.parentNode.remove(this); } }),
};
function container(id, ids) {
  const root = { id, ownerDocument: doc, all: [],
    get children() { return this.all.filter((n) => !n.comment); },
    insertBefore(n, before) { this.all.splice(this.all.indexOf(before), 0, n); n.parentNode = this; },
    remove(n) { this.all.splice(this.all.indexOf(n), 1); n.parentNode = null; },
  };
  nodes.set(id, root);
  root.all = ids.map((childId) => {
    const n = { id: childId, parentNode: root, remove() { this.parentNode.remove(this); } };
    nodes.set(childId, n); return n;
  });
  return root;
}
const rail = container('icon-rail', ['rail-notes', 'rail-agents', 'rail-tasks']);
container('tools-section', []);
"""
NAV_IMPORT = "const nav = await import(%s);\n" % url("navOrder.js")


def test_a_reordered_rail_is_written_to_the_account():
    out = node_case({}, {}, NAV_IMPORT + NAV_DOM + """
nav.writeNavOrder(['tasks', 'agents', 'notes']);
await settled(); report({});
""")
    key, entry = out["puts"][0]
    assert key == "nav-order"
    assert entry["value"][:3] == ["tasks", "agents", "notes"]


def test_a_second_browser_adopts_the_accounts_order():
    """The whole point: this browser has its own stored order, and the account
    has a newer one. The account's wins and the rail is re-laid-out."""
    out = node_case(
        {"odysseus-nav-order-v1": json.dumps({"value": ["notes", "agents", "tasks"], "updated_at": 100})},
        {"nav-order": {"value": ["tasks", "agents", "notes"], "updated_at": 900}},
        NAV_IMPORT + NAV_DOM + """
const applied = await nav.syncNavOrderWithAccount(doc);
await settled();
report({ applied: applied.slice(0, 3), rail: rail.children.map((n) => n.id) });
""")
    assert out["applied"] == ["tasks", "agents", "notes"]
    assert out["rail"] == ["rail-tasks", "rail-agents", "rail-notes"]
    # Adopting the account's order must not bounce straight back to the server.
    assert out["puts"] == []


def test_a_stale_account_order_does_not_override_a_fresh_local_one():
    out = node_case(
        {"odysseus-nav-order-v1": json.dumps({"value": ["tasks", "agents", "notes"], "updated_at": 900})},
        {"nav-order": {"value": ["notes", "agents", "tasks"], "updated_at": 100}},
        NAV_IMPORT + NAV_DOM + """
const applied = await nav.syncNavOrderWithAccount(doc);
await settled(); report({ applied: applied.slice(0, 3) });
""")
    assert out["applied"] == ["tasks", "agents", "notes"]
    assert out["puts"][0][1]["value"][:3] == ["tasks", "agents", "notes"]


def test_an_order_stored_by_an_older_build_still_reads():
    out = node_case({"odysseus-nav-order-v1": json.dumps(["tasks", "notes"])}, {},
                    NAV_IMPORT + NAV_DOM + "report({ order: nav.readNavOrder().slice(0, 2) });")
    assert out["order"] == ["tasks", "notes"]


def test_resetting_the_order_is_recorded_so_it_is_not_undone_on_reload():
    """Dropping only the local copy left the account holding the old order,
    which the next page load restored."""
    out = node_case({}, {"nav-order": {"value": ["tasks", "agents", "notes"], "updated_at": 100}},
                    NAV_IMPORT + NAV_DOM + """
nav.resetNavOrder(doc);
await settled(); report({});
""")
    assert out["puts"], "reset must reach the account"
    assert out["puts"][0][1]["value"][0] == "calendar"  # the default order's first item


# ── theme boot ───────────────────────────────────────────────────────────────

def test_theme_is_reconciled_on_every_boot_not_only_when_local_is_empty():
    boot = THEME.split("async function _initWithSync()", 1)[1]
    assert "if (!getSaved())" not in boot
    assert "_syncThemeWithAccount()" in boot
    assert "_syncCustomThemesWithAccount()" in boot


def test_an_adopted_theme_is_applied_in_full_not_just_its_colors():
    """The old hydration called applyColors() only, leaving a second browser
    with the right palette and the default font, density, pattern and effects.
    Writing the account theme into the first-paint cache lets the single
    initThemeUI() pass apply all of it."""
    adopt = THEME.split("function _adoptServerTheme(", 1)[1].split("\n}", 1)[0]
    assert "Storage.setJSON(LS_KEY" in adopt
    assert "applyColors" not in adopt
    init = THEME.split("function initThemeUI()", 1)[1]
    for applied in ("applyColors(currentColors)", "applyFontDensity(_initFont, _initDensity)",
                    "applyBgPattern(_initPattern)", "applyFrostedGlass(_initFrosted)"):
        assert applied in init


def test_a_saved_theme_carries_a_write_time_so_browsers_can_be_ordered():
    save = THEME.split("export function save(name, colors, opts)", 1)[1].split("\n}", 1)[0]
    assert "obj.updated_at = Date.now()" in save
    assert "writePref(THEME_PREF, obj, obj.updated_at)" in save


def test_text_size_travels_with_the_theme():
    assert "opts.uiScale = ts.value" in THEME
    assert "(saved && saved.uiScale)" in THEME


def test_the_local_copy_still_paints_first():
    """Reconciling must not put a network round trip in front of a theme this
    browser already has."""
    boot = THEME.split("async function _initWithSync()", 1)[1]
    assert boot.index("if (hadLocal) initThemeUI();") < boot.index("await _syncCustomThemesWithAccount()")


def test_prefs_are_account_scoped_server_side():
    """The route these modules write to keys by signed-in user; without that,
    'follows the account' would be 'follows the deployment'."""
    routes = (ROOT / "routes" / "prefs_routes.py").read_text(encoding="utf-8")
    assert "user = get_current_user(request)" in routes
    assert "_save_for_user(user, prefs)" in routes


def test_the_sync_module_is_precached_with_the_rest_of_the_shell():
    sw = (ROOT / "static" / "sw.js").read_text(encoding="utf-8")
    assert "'/static/js/serverPrefs.js'," in sw
