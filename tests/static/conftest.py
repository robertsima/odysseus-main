"""Fixtures for browser tests of the shipped static UI.

Most tests run against a canned API: one
:class:`~tests.helpers.static_app.StaticAppServer` serves every test in a
process, its recorded state reset before each test, and ``open_app`` opens the
app in a fresh browser context (see tests/plugins/browser.py). The few flows
that need the real backend use ``live_app`` and ``live_page``
(tests/helpers/live_app.py).
"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest

from tests.helpers.live_app import ADMIN, PASSWORD, LiveApp, MockModel
from tests.helpers.static_app import SESSION_ID, StaticAppServer, wait_ready


@pytest.fixture(scope="session")
def _static_server():
    with StaticAppServer() as srv:
        yield srv


@pytest.fixture
def static_app(_static_server):
    _static_server.state.reset()
    yield _static_server
    _static_server.state.reset()


@pytest.fixture
def open_app(static_app, new_page):
    """``open_app(width, theme=None, style=None, workbench_prefs=None, chat=True)``.

    Opens the app with optional stored theme, page style and Workbench
    preferences. With ``chat`` it opens the fixture chat and waits for its four
    messages; without, the start page. Either way it returns once the app's
    modules are up and the layout has stopped moving.
    """
    def _open(width: int, theme: str | None = None, style: str | None = None,
              workbench_prefs: dict | None = None, chat: bool = True, height: int | None = None):
        page = new_page(width, height)
        seed = []
        if workbench_prefs is not None:
            seed.append(f"localStorage.setItem('odysseus-workbench-prefs', {json.dumps(json.dumps(workbench_prefs))});")
        # Seeds apply to the first load only: a test that changes the theme
        # or style and reloads must see its own choice.
        if theme:
            seed.append(f"if (!localStorage.getItem('odysseus-theme')) "
                        f"localStorage.setItem('odysseus-theme', {json.dumps(theme)});")
        if style:
            value = json.dumps(json.dumps({"value": style, "updated_at": 1}))
            seed.append(f"if (!localStorage.getItem('odysseus-page-style-v1')) "
                        f"localStorage.setItem('odysseus-page-style-v1', {value});")
        if seed:
            page.add_init_script("try {" + " ".join(seed) + "} catch (_) {}")
        page.goto(static_app.url + (f"/#{SESSION_ID}" if chat else "/"))
        wait_ready(page, chat=chat)
        return page

    return _open


@pytest.fixture(scope="session")
def live_app():
    """The real app on a free port with scratch data and the scripted model.

    One per test process. Tests that use it carry
    ``pytest.mark.xdist_group("live_app")``, so ``--dist loadgroup`` keeps them
    on one worker and only one app starts. The scratch folder is not under
    pytest's basetemp: another pytest run on the machine prunes old basetemps,
    which deleted a running app's data in a local run on 2026-10-04.
    """
    root = Path(tempfile.mkdtemp(prefix="odysseus-live-app-"))
    model = MockModel().start()
    app = LiveApp(root, model)
    try:
        app.start()
        yield app
    finally:
        app.stop()
        model.stop()
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def live_page(live_app, new_page):
    """``live_page(width=1440, path="/")`` -> a page signed in as the admin."""
    def _open(width: int = 1440, path: str = "/"):
        page = new_page(width)
        resp = page.request.post(live_app.url + "/api/auth/login",
                                 data={"username": ADMIN, "password": PASSWORD})
        assert resp.ok, resp.text()
        # The real app does more per page load than the canned API.
        page.set_default_navigation_timeout(30_000)
        try:
            page.goto(live_app.url + path, wait_until="domcontentloaded")
        except Exception as exc:
            raise AssertionError(f"{exc}\n--- app log ---\n{live_app.log_tail(80)}") from exc
        return page

    return _open
