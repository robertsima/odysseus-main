"""Regression guard for issue #1414 — a broken upstream `searxng:latest` tag
(2026.6.2 crashed on boot with KeyError: 'default_doi_resolver') failed the
searxng healthcheck, and because `odysseus` waits on it via
`depends_on: condition: service_healthy`, the whole app never started on fresh
Docker installs.

Pin the SearXNG image to a known-good tag so a bad upstream `latest` can't block
startup. This guards that the pin stays in place.
"""
import re
from pathlib import Path
from tests import REPO_ROOT

COMPOSE = REPO_ROOT / "docker-compose.yml"


def test_searxng_image_is_pinned_not_latest():
    text = COMPOSE.read_text(encoding="utf-8")
    m = re.search(r"image:\s*\S*searxng/searxng:(\S+)", text)
    assert m, "searxng image line not found in docker-compose.yml"
    tag = m.group(1)
    assert tag != "latest", (
        "SearXNG must be pinned, not ':latest' — odysseus startup depends on its "
        "healthcheck, so a broken upstream latest tag blocks the app (issue #1414)"
    )
    # A real version tag (date-based, e.g. 2026.5.31-7159b8aed), not a moving ref.
    assert re.match(r"\d{4}\.\d", tag), f"expected a versioned tag, got {tag!r}"


def test_every_compose_file_pins_the_same_searxng_tag():
    """The CPU, GPU and ZimaOS compose files must not drift apart: the server
    runs 2026.9.25-12f8b6515 (Bing first-word fix, searxng#6671) and the
    engine pin in services/search/providers.py assumes that image's engine
    set (mojeek inactive, presearch removed)."""
    root = COMPOSE.parent
    tags = {}
    for path in sorted(root.glob("docker-compose*.yml")):
        m = re.search(r"image:\s*\S*searxng/searxng:(\S+)", path.read_text(encoding="utf-8"))
        if m:
            tags[path.name] = m.group(1)
    assert "docker-compose.yml" in tags
    assert len(set(tags.values())) == 1, tags
    assert tags["docker-compose.yml"] == "2026.9.25-12f8b6515"
