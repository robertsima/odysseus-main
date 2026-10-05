"""Persisted research reports belong to their owner.

A report written before ownership was recorded has no owner. It must not be
readable by whoever asks, and the visual report must not be generated for them.
"""
import json
from pathlib import Path

import pytest

import routes.research.research_routes as research_routes

pytestmark = pytest.mark.security

SECRET = "the findings nobody else may read"


def _persist(session_id, **data):
    root = Path(research_routes.DEEP_RESEARCH_DIR)
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{session_id}.json").write_text(
        json.dumps({"query": "q", "result": SECRET, **data}), encoding="utf-8"
    )


@pytest.mark.parametrize("path", ["report", "detail"])
@pytest.mark.parametrize("owner", [None, "bob"])
def test_a_report_with_no_owner_or_another_owner_is_not_served(api, path, owner):
    _persist("rp-legacy", **({"owner": owner} if owner else {}))

    response = api.as_user("alice").get(f"/api/research/{path}/rp-legacy")

    assert response.status_code == 404
    assert SECRET not in response.text


def test_the_owner_gets_their_visual_report(api):
    _persist("rp-mine", owner="alice")

    response = api.as_user("alice").get("/api/research/report/rp-mine")

    assert response.status_code == 200, response.text
    assert SECRET in response.text
