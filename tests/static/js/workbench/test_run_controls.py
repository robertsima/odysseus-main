"""Run cards and the agent strip in the Workbench (static/js/workbench.js):
which controls a run offers while it is live, how it reads when it ends, and
what the composer's agent strip keeps on screen.

The classic page style draws the plain run cards; the Agamemnon style folds
them into its own run summary.
"""
from __future__ import annotations

import re

import pytest

from tests.helpers.static_app import RUN_ID, expect, open_workbench

pytestmark = pytest.mark.browser

RUN_CARD = f'.wb-run[data-run="{RUN_ID}"]'
STRIP_ROW = f'#agent-strip .agent-strip-row[data-run="{RUN_ID}"]'


def test_a_live_run_offers_wrap_up_beside_stop_and_each_reaches_the_server(open_app):
    page = open_app(1440, style="classic")
    open_workbench(page)
    card = page.locator(RUN_CARD)
    expect(card).to_have_count(1)
    wrap_up, stop = card.get_by_role("button", name="Wrap up"), card.get_by_role("button", name="Stop")
    expect(wrap_up).to_have_count(1)
    expect(stop).to_have_count(1)
    with page.expect_request(lambda r: r.method == "POST" and r.url.endswith(f"/api/workbench/runs/{RUN_ID}/wrap-up")):
        wrap_up.click()
    with page.expect_request(lambda r: r.method == "POST" and r.url.endswith(f"/api/workbench/runs/{RUN_ID}/stop")):
        stop.click()


def test_a_finished_run_offers_neither_and_a_cut_off_one_reads_as_partial_not_failed(open_app, static_app):
    static_app.state.run_status = "incomplete"
    page = open_app(1440, style="classic")
    open_workbench(page)
    card = page.locator(RUN_CARD)
    expect(card).to_have_count(1)
    expect(card.get_by_role("button", name="Stop")).to_have_count(0)
    expect(card.get_by_role("button", name="Wrap up")).to_have_count(0)
    pill = card.locator(".wb-pill").first
    expect(pill).to_have_class(re.compile(r"\bwarn\b"))
    expect(pill).not_to_have_class(re.compile(r"\bbad\b"))


@pytest.mark.parametrize("status, live", [("running", True), ("queued", True), ("failed", False), ("cancelled", False)])
def test_the_agent_strip_keeps_a_run_that_ends_without_a_finish_time(open_app, static_app, status, live):
    # The runs list gives no finished_at for these rows. A run is live by its
    # status alone: a failed one stays on the strip as finished instead of
    # vanishing, and offers no Stop.
    static_app.state.run_status = status
    page = open_app(1440, style="classic")
    row = page.locator(STRIP_ROW)
    expect(row).to_have_count(1)
    expect(row.get_by_role("button", name="Stop")).to_have_count(1 if live else 0)
    expect(row.get_by_role("button", name="Wrap up")).to_have_count(1 if live else 0)
    if live:
        expect(row).not_to_have_class(re.compile(r"\bdone\b"))
    else:
        expect(row).to_have_class(re.compile(r"\bdone\b"))
    expect(page.locator("#agent-strip .agent-strip-toggle .wb-count")).to_have_text("1 running" if live else "finished")
