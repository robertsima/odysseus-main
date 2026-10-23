"""The skill catalog routes must not walk the skills tree on the event loop.

Production logged ``event loop blocked 1.23s ... os.walk <- skills.py
_iter_skill_files <- load_all <- index_for <- GET /api/skills/slash-catalog``.
Every other request, including streaming chat, stalled behind it.
"""
import asyncio
import os
import time

import pytest
from fastapi import Request
from fastapi.datastructures import State

from routes.skills_routes import setup_skills_routes
from services.memory.skills import SkillsManager


def _write(root, name, owner="alice"):
    d = root / "skills" / "general" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(chr(10).join([
        "---", f"name: {name}", f"description: {name} helper", "version: 1.0.0",
        "category: general", "tags: []", "status: published", "confidence: 0.8",
        "source: learned", f"owner: {owner}", "---", "", "# When to use", "testing",
        "", "# Procedure", "- step", ""]), encoding="utf-8")


def _handler(sm, path):
    router = setup_skills_routes(sm)
    return next(r.endpoint for r in router.routes if r.path == path and "GET" in r.methods)


def _request(user="alice"):
    class DummyApp:
        state = State()
    return Request(scope={"type": "http", "app": DummyApp(), "state": {"current_user": user}})


async def _max_gap_while(coro, interval=0.01):
    gaps = []
    stop = False

    async def beat():
        last = time.monotonic()
        while not stop:
            await asyncio.sleep(interval)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    task = asyncio.create_task(beat())
    await asyncio.sleep(0.05)
    try:
        result = await coro
    finally:
        stop = True
        await task
    return result, max(gaps)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/skills/slash-catalog", "/api/skills/index", "/api/skills"])
async def test_catalog_walk_does_not_stall_the_loop(tmp_path, monkeypatch, path):
    _write(tmp_path, "deploy-app")
    sm = SkillsManager(str(tmp_path))
    handler = _handler(sm, path)
    real_walk = os.walk

    def slow_walk(*a, **kw):
        time.sleep(0.4)
        return real_walk(*a, **kw)

    monkeypatch.setattr(os, "walk", slow_walk)

    result, gap = await _max_gap_while(handler(_request()))

    assert gap < 0.25, f"event loop stalled {gap:.2f}s"
    assert result["count"] == 1


@pytest.mark.asyncio
async def test_new_skill_shows_up_in_the_catalog(tmp_path):
    _write(tmp_path, "deploy-app")
    sm = SkillsManager(str(tmp_path))
    handler = _handler(sm, "/api/skills/slash-catalog")
    first = await handler(_request())
    assert [s["name"] for s in first["skills"]] == ["deploy-app"]

    _write(tmp_path, "rollback-app")
    second = await handler(_request())

    assert [s["name"] for s in second["skills"]] == ["deploy-app", "rollback-app"]


@pytest.mark.asyncio
@pytest.mark.security
async def test_catalog_still_hides_other_users_skills(tmp_path):
    _write(tmp_path, "alice-only", owner="alice")
    _write(tmp_path, "bob-only", owner="bob")
    handler = _handler(SkillsManager(str(tmp_path)), "/api/skills/slash-catalog")

    result = await handler(_request("bob"))

    assert [s["name"] for s in result["skills"]] == ["bob-only"]
