# Test suite helpers

The reference for the shared fixtures and helpers. The rules (where a test goes,
what a test must be, CI jobs, markers) are in [`TESTING_STANDARD.md`](./TESTING_STANDARD.md);
`AGENTS.md` at the repo root carries the short version.

Run tests with `python -m tests.run <lane>` (`affected`, `full`, `browser`,
`security`, `nightly`); see `TESTING_STANDARD.md`.

`tests/run_order_report.py` runs pytest with the collected tests shuffled by a
printed seed, to find tests that depend on what ran before them. The nightly
workflow runs it; locally: `python -m tests.run_order_report --seed 123 -- tests/scripts/ -q`.

## Fixtures in `tests/plugins/`

Loaded for every test through `pytest_plugins` in `tests/conftest.py`.

### `api`: the real app over HTTP (`http_app.py`)

The real FastAPI app with its real auth middleware, three accounts and a fresh
per-test state. Each call returns a `TestClient` carrying that user's session
cookie.

```python
def test_bob_does_not_see_alice_sessions(api):
    alice, bob = api.as_user("alice"), api.as_user("bob")
    alice.post("/api/session", data={"name": "plans", "skip_validation": "true"})
    assert bob.get("/api/sessions").json() == []
```

- `api.as_user("alice")`, `api.as_user("bob")`, `api.as_admin()`, `api.anonymous()`.
- `api.app`, `api.module` (app.py), `api.auth`, `api.session_manager`.
- Every path derived from the data directory points at a per-process temp
  folder, and an audit hook fails a test that touches the repo's real `data/`.

### Network guard (`network_guard.py`)

DNS lookups of names and connections beyond loopback fail at once with an error
naming the guard. Address literals still resolve. Mark a test that needs the
network `@pytest.mark.allow_network` and say why.

### Browser tests (`browser.py`)

Python Playwright driving the system Chrome (`channel="chrome"`); nothing is
downloaded. `pip install -r requirements-test.txt` installs Playwright.
`ODYSSEUS_TEST_CHROMIUM=<path>` picks another Chromium build.

- `browser`: one Chrome per test process.
- `new_page(width, height=None, **context_options)`: a page in a fresh context,
  closed after the test. Under 481 px the viewport is mobile. Uncaught page
  errors collect in `page.errors`.
- Without Playwright or Chrome the tests skip. With `ODYSSEUS_REQUIRE_BROWSER=1`
  (set in CI) they fail instead. With `ODYSSEUS_BROWSER_ARTIFACTS=<dir>` a failed
  test leaves a screenshot of each of its pages there.

Browser tests live at the mirror of what they exercise, under `tests/static/`
(`tests/static/js/workbench/` for `static/js/workbench.js`,
`tests/static/index/` for the page shell in `static/index.html`). Their fixtures
are in `tests/static/conftest.py`:

- `open_app(width, theme=None, style=None, workbench_prefs=None, chat=True)`:
  the shipped `static/` served with a canned `/api/*`
  (`tests/helpers/static_app.py`), open on a fixture chat with a running agent
  fleet and Workbench run. `static_app.state` records what the page posted and
  lets a test change the canned data (`run_status`, `repo_activity`,
  `appearance`); it is reset before each test.
- `live_app` and `live_page(width=1440, path="/")`: the real app (`python app.py`)
  on a free port with scratch data, signed in as an admin, with a scripted
  OpenAI-compatible model (`tests/helpers/live_app.py`). The model answers by
  probe markers in the conversation and can hold a reply at a gate
  (`live_app.model.arm("stream")`, `wait_reached`, `release`), so a test can
  look at a reply mid-stream. Tests that use it carry
  `pytest.mark.xdist_group("live_app")`; CI runs with `--dist loadgroup`, so
  one app serves them all.

`tests/helpers/static_app.py` also has the measuring helpers: `probe` (box,
paint and whether a click at the centre reaches the element), `assert_usable`,
`assert_no_sideways_scroll`, `contrast`, and `settle`, which waits for CSS
transitions to end and a box to stop moving. Wait with `expect(...)`,
`wait_for_function` or `settle`, never a fixed sleep.

Widths are 1440, 700 and 390 px. Another width (320 px for the narrowest phone
row, 1024 px for the half-width dock) needs a test that is about that width.

## Helper conventions

The helpers below live under `tests/helpers/`. They exist to remove repeated
boilerplate that already appeared across multiple tests. Reach for one only when
your test matches its intended use; do not stretch a helper to cover a new case.

### `tests.helpers.cli_loader.load_script`

Use when a test needs to import a script under `scripts/` without repeating
`SourceFileLoader` / `importlib.util` boilerplate.

- Intended for script/CLI tests that load a single file from `scripts/`.
- Not for arbitrary package imports - use a normal `import` for those.
- When migrating an existing test to it, keep the existing stubs and assertions
  unchanged. Any `sys.modules` stubs the script needs at import time must still
  be injected (e.g. via `monkeypatch`) before calling `load_script`.

### `tests.helpers.import_state.clear_module`

Use when a test must drop one cached module and its parent-package attribute
before a fresh import.

- Clears `sys.modules[name]`.
- Clears the parent-package attribute when present.
- Good replacement for local `sys.modules.pop(...)` + `delattr(parent, child)`
  blocks.

### `tests.helpers.import_state.preserve_import_state`

Use when a test temporarily installs stubs into `sys.modules` and needs
deterministic cleanup afterward.

- Context manager: restores both `sys.modules` entries and parent-package
  attributes on exit (normal or exception).
- Useful around module-level stubs or temporary imports.
- Prefer narrow, explicit module names over broad ones.

### `tests.helpers.import_state.clear_fake_database_modules`

Use only for the guarded fake/stub database cleanup pattern.

- Preserves a real-looking `core.database` (one with a string `__file__`).
- Removes a fake/stub `core.database` and the related `src.database` state.
- Do not use as a general database reset fixture.

### `tests.helpers.import_state.clear_fake_endpoint_resolver_modules`

Use only for the guarded fake/stub `src.endpoint_resolver` cleanup pattern.

- Preserves real resolver modules (those with a truthy `__file__`).
- Evicts fake/stub resolver modules and the dependent route modules that were
  cached against them.
- Accepts explicit extra dependent module names to evict alongside the defaults.

### Test databases: `app_db` and `make_test_db`

`tests/plugins/database.py` builds each schema once per test process and gives
each test a fresh copy, deleted after the test. Never call `create_all` in a
test module, and never at import time.

- `app_db`: a file copy of `core.database`'s schema. `app_db.SessionLocal` is
  a sessionmaker bound to `app_db.engine` (NullPool, autoflush off);
  `app_db.path` and `app_db.url` name the file.
- `make_test_db(metadata=None, *, memory=False, **engine_kwargs)`: the factory
  behind `app_db`. Use it for a test-local declarative base, more than one
  database, an in-memory copy (`memory=True`, one connection that every thread
  shares), or engine options such as `poolclass=QueuePool, pool_size=1`.
- Bind `SessionLocal` onto the module the code under test reads with
  `monkeypatch.setattr`. Never set `DATABASE_URL` and reload `core.database`.
- Each test process also has its own SQLite file as `core.database`'s default
  database, shared by all its threads, unless `DATABASE_URL` is set.

### `tests.helpers.sqlite_db.make_temp_sqlite`

For code that cannot take a fixture. Returns `(SessionLocal, engine, path)` for
a fresh file copy of a schema (`core.database`'s by default); the file is
deleted when the test process exits. Prefer the fixtures above.

### `tests.helpers.db_stubs.make_core_db_stub`

Use for small import-time `core.database` stubs with a placeholder
`SessionLocal`.

- Pass model names via `models` when MagicMock attributes are sufficient.
- Pass `attributes` when an import needs exact placeholder values.
- Set `install_core_package=True` only when the test also needs a fake parent
  `core` module stub.
- Keep custom fake sessions and route-specific database behavior local.

### `tests.helpers.node`

Runs node for the `*_js.py` wrappers: `module_url(path)` gives the `file://`
URL node's ESM loader needs on every platform, and `run_module(...)` runs a
snippet and decodes its output as UTF-8.
