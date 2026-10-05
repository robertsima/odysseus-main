# Working in Odysseus

## Tests

Linux CI decides whether the suite is green. `tests/TESTING_STANDARD.md` is the reference for fixtures, markers and CI jobs; read it before adding a fixture or a test kind not described here.

Run tests through `python -m tests.run <lane>`:

- `affected` while you work: the tests that cover what you changed.
- `full` once before a push or a hand-back.
- `browser` as well when `static/` or a page-serving route changed.

On a workstation the lanes run 4 test processes at a time (`ODYSSEUS_TEST_WORKERS` changes that). When several agents share one machine, run only `affected` locally and let CI run `full`: parallel full runs exhausted 64 GB of RAM once.

Writing a test:

1. Name the realistic production bug it catches that no other test catches. If you can't name one, the test doesn't earn its run time.
2. Put it at the **mirror** of the production file: tests for `src/agent_loop.py` go in `tests/src/agent_loop/test_<behavior>.py`, tests for `static/js/chat.js` in `tests/static/js/chat/<behavior>.test.mjs`. Each new Python test folder gets an `__init__.py`.
3. See it go **red** without your fix, for the reason you expect, then green with it. Put the red line in the commit message or hand-back. On a PR, CI reruns new tests against the base commit and reports any that pass there.
4. Assert on behavior: call the function, route or module and check what it returns, stores or renders.
   - Routes go through HTTP with the `api` fixture (`api.as_user("alice")`, `api.as_user("bob")`, `api.as_admin()`).
   - Databases come from `app_db`.
   - Fake only what the code calls out to (network, model, clock); the function the test is named after stays real.
5. Mark a test that guards owner scope, auth, confinement, SSRF, sensitivity or approval with `pytest.mark.security`.

The hygiene guard (`tests/suite/test_hygiene.py`) accepts tests that use `monkeypatch` for environment variables, module attributes and `sys.modules`, and that read production behavior rather than production source text. The network guard turns DNS lookups and non-loopback connections into immediate errors; a test that truly needs the network carries `@pytest.mark.allow_network` and says why.

The repo's `data/` folder can hold a real deployment's data. Run the app and tests against a temporary data folder.
