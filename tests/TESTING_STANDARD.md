# Testing standard

How tests are written, placed and run in Odysseus. `AGENTS.md` carries the short version; this file is the reference behind it. The reasoning and the measurements are in `website/testing-restructure-2026-10-03.md`.

Linux CI decides whether the suite is green. A Windows run skips POSIX-only tests, so it is green but partial.

## Running tests

| Lane | Command | When |
|---|---|---|
| affected | `python -m tests.run affected` | While working: the tests covering what you changed. |
| full | `python -m tests.run full` | Once before a push or a hand-back. Everything except browser and nightly tests, in parallel, plus the node tests. |
| browser | `python -m tests.run browser` | When `static/` or a page-serving route changed. |
| security | `python -m tests.run security` | Owner scope, auth, confinement, SSRF, sensitivity, approval. `full` includes it. |
| nightly | `python -m tests.run nightly` | The slow tier; the nightly workflow runs it. |

Arguments after `--` go to pytest: `python -m tests.run full -- -x --lf`.

`affected` reads, in order: the coverage map the nightly workflow publishes on the `test-map` branch, the mirrored test folder of each changed file, and tests that mention the changed module. A change to `tests/conftest.py`, `tests/plugins/`, `tests/helpers/`, `pyproject.toml` or a requirements file selects everything (`scripts/affected_tests.py`).

### CI

On every push to dev and every PR (`.github/workflows/ci.yml`):

- `python-tests`: the full lane, `-n auto`.
- `browser-tests`: the browser lane.
- `node-syntax`: `node --check` on the frontend and the node tests.
- `red-evidence` (PRs, report-only until calibrated): reruns the PR's new and changed tests against the base commit's production code; each must fail there and pass on head.
- `affected-tests` (PRs, report-only): lists the tests that cover the change.

Nightly (`.github/workflows/nightly.yml`): random test order, the coverage map, nightly-marked and all browser tests, the orchestration scenarios, a Windows run, and on Sundays the mutation benchmark. A failure on dev opens or updates one issue labelled `nightly-red`.

## Where a test goes

A test lives at the mirror of the production file it exercises, one folder per production module:

| Production file | Tests |
|---|---|
| `src/agent_loop.py` | `tests/src/agent_loop/test_<behavior>.py` |
| `routes/document/document_routes.py` | `tests/routes/document/document_routes/test_<behavior>.py` |
| `static/js/chat.js` | `tests/static/js/chat/<behavior>.test.mjs` |
| `scripts/affected_tests.py` | `tests/scripts/affected_tests/test_<behavior>.py` |
| `.github/scripts/red_evidence.py` | `tests/github/scripts/red_evidence/test_<behavior>.py` |

Every Python test folder has an `__init__.py`; `tests/` is a package, so `tests/routes/` never shadows the production `routes` package. A test that drives several modules goes with the one whose behavior it asserts. Tests about the suite itself (guards, the lane runner) live in `tests/suite/`.

Kind and speed are markers, not folders (`pyproject.toml` lists them): `security`, `browser`, `nightly`, `allow_network`.

## What a test must be

**Worth keeping.** Before writing a test, name the realistic production bug it catches and no other test catches. A test whose only failure mode is a change of wording, formatting or file layout is not worth its run time.

**Red first.** Watch the test fail without the fix, for the reason you expect, then pass with it. A test written after the code that passes on its first run shows nothing. On a PR, CI checks this: the `red-evidence` job reruns new and changed tests against the base commit. A behavior-preserving refactor carries the `refactor` label and one line in the PR description instead.

**Behavior, not source.** Call the function, route or module and assert on what it returns, stores or renders. A test that reads production source as text (`read_text`, `open`, `inspect.getsource`, `ast.parse` of a file) breaks on harmless reformatting and passes when behavior breaks; the hygiene guard rejects new ones.

**Real code under test.** Fake what the code calls out to: the network, the model, the clock, the filesystem outside `tmp_path`. Keep the function the test is named after real. A test that mocks `get_sessions_for_user` cannot catch a broken owner filter in `get_sessions_for_user`.

**Routes through HTTP.** Test a route with the `api` fixture (`tests/plugins/http_app.py`): the real app, its real auth middleware, and three users (alice, bob and an admin). A direct call to the route function skips auth, validation and serialization, which is where owner scope and admin gates live.

**Frontend logic in node.** Import the real module in a `*.test.mjs` file run by `node --test`, with the shared DOM and fetch fakes in `tests/static/js/_support/`. A browser test is for layout and flows that need a real page.

**Browser tests assert on structure.** Roles, state, geometry and data attributes. Visible label text only when the label is what the test is about.

**One behavior per test**, named for that behavior.

## Isolation

- `monkeypatch` for environment variables, module attributes and `sys.modules` entries; a fixture for any stub a module needs at import.
- The database: `app_db` (and `make_test_db` for other schemas or an in-memory copy) gives each test a fresh copy of a schema built once per process, from `tests/plugins/database.py`. Without them, each test process still shares one SQLite file across its threads.
- The network: `tests/plugins/network_guard.py` fails DNS lookups of names and connections beyond loopback at once. A test that needs the network carries `@pytest.mark.allow_network` and a comment saying why.
- The repo's `data/` folder can hold a real deployment's data. Tests and local app runs use a temporary data dir.

The hygiene guard (`tests/suite/test_hygiene.py`) fails on module-scope `sys.modules` writes, `importlib.reload`, raw `os.environ` writes and source-text reads. Today's offenders are listed in `tests/suite/hygiene_allowlist.json`, which only shrinks: when a listed file is fixed, moved or deleted, run `python -m tests.suite.hygiene --prune`. A new entry needs a specific reason.

## Windows

A test that cannot run on Windows (POSIX permissions, bwrap, tmux) carries `pytest.mark.skipif(sys.platform == "win32", reason=...)` with the specific reason. A failure that looks like a real Windows bug in production code gets fixed or reported, not skipped.

## The mutation benchmark

`tests/mutants/` holds patches that plant a realistic bug (`B*.patch`) or make a behavior-preserving refactor (`N*.patch`), and `expected.json` says what each should do: a planted bug must make some test fail, a refactor must not. `scripts/mutation_benchmark.py` applies each patch, runs the suite and reverts it; the nightly workflow runs it on Sundays. When CI catches a real regression, add it here as a new `B*` patch (`git diff > tests/mutants/B14.patch` on the buggy change) with an entry in `expected.json`.

## Tests from upstream

On an upstream sync, take upstream's production code and adopt its tests one at a time: move each into the mirrored layout, hold it to this standard, or drop it.
