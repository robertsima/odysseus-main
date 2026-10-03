"""Playwright fixtures for the browser tier (``pytest -m browser``).

One Chrome per test process, a fresh browser context and page per test. The
browser is the system Chrome (``channel="chrome"``), so neither CI nor a
developer machine downloads one. ``ODYSSEUS_TEST_CHROMIUM`` points at another
Chromium build instead.

Without Playwright or Chrome the browser tests skip with the reason. CI sets
``ODYSSEUS_REQUIRE_BROWSER=1`` so the browser job fails rather than passes
with every test skipped.

Nothing here runs unless a test asks for one of these fixtures, so the
default ``-m "not browser"`` run never imports Playwright.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

# Actions and expect() wait up to this long. Generous for a cold CI runner,
# short enough that a covered or missing control fails in seconds.
DEFAULT_TIMEOUT_MS = 10_000
# Height used when a test gives only a width.
DEFAULT_HEIGHTS = {1440: 920, 1280: 800, 1024: 768, 700: 844, 390: 844, 320: 740}


def _unavailable(reason: str):
    if os.environ.get("ODYSSEUS_REQUIRE_BROWSER") == "1":
        pytest.fail(f"browser required (ODYSSEUS_REQUIRE_BROWSER=1) but {reason}", pytrace=False)
    pytest.skip(reason)


@pytest.fixture(scope="session")
def _playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _unavailable("needs Playwright (pip install -r requirements-test.txt)")
    pw = sync_playwright().start()
    try:
        yield pw
    finally:
        pw.stop()


@pytest.fixture(scope="session")
def browser(_playwright):
    """The process's one headless Chrome."""
    from playwright.sync_api import Error, expect

    expect.set_options(timeout=DEFAULT_TIMEOUT_MS)
    exe = os.environ.get("ODYSSEUS_TEST_CHROMIUM")
    options = {"executable_path": exe} if exe else {"channel": "chrome"}
    try:
        b = _playwright.chromium.launch(headless=True, args=["--force-color-profile=srgb"], **options)
    except Error as exc:
        message = str(exc)
        if re.search(r"is not found|doesn't exist|not installed", message, re.I):
            _unavailable(f"no Chrome found ({message.splitlines()[0]}); install Chrome "
                         "or set ODYSSEUS_TEST_CHROMIUM")
        raise
    try:
        yield b
    finally:
        b.close()


class _Pages:
    """Opens pages in fresh contexts and closes them after the test."""

    def __init__(self, browser, request) -> None:
        self._browser = browser
        self._request = request
        self.contexts = []
        self.pages = []

    def __call__(self, width: int = 1440, height: int | None = None, **context_options):
        height = height or DEFAULT_HEIGHTS.get(width, 900)
        options = {"viewport": {"width": width, "height": height}, "device_scale_factor": 1,
                   "is_mobile": width <= 480}
        options.update(context_options)
        context = self._browser.new_context(**options)
        context.set_default_timeout(DEFAULT_TIMEOUT_MS)
        self.contexts.append(context)
        page = context.new_page()
        page.errors = []
        page.on("pageerror", lambda exc, page=page: page.errors.append(str(exc)))
        self.pages.append(page)
        return page

    def close(self, failed: bool) -> None:
        shots = os.environ.get("ODYSSEUS_BROWSER_ARTIFACTS")
        if failed and shots:
            folder = Path(shots)
            folder.mkdir(parents=True, exist_ok=True)
            stem = re.sub(r"[^\w.-]+", "_", self._request.node.nodeid)[-150:]
            for i, page in enumerate(self.pages):
                try:
                    page.screenshot(path=str(folder / f"{stem}-{i}.png"))
                except Exception:
                    pass
        for context in self.contexts:
            try:
                context.close()
            except Exception:
                pass


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    report = yield
    if report.when == "call":
        item._browser_failed = report.failed
    return report


@pytest.fixture
def new_page(browser, request):
    """``new_page(width, height=None, **context_options)`` -> a page in its own context.

    Pages narrower than 481 px get a mobile viewport. Uncaught page
    errors collect in ``page.errors``. With ``ODYSSEUS_BROWSER_ARTIFACTS`` set,
    a failed test leaves a screenshot of each of its pages there.
    """
    pages = _Pages(browser, request)
    yield pages
    pages.close(getattr(request.node, "_browser_failed", False))
