"""Recording a skill use must not walk the whole skills tree.

Production blocked the event loop for 2.01s while the prompt builder called
``record_use`` for each matched skill: ``_usage_owner`` walked every directory
under the skills root (on a NAS-backed data folder) to find who owns the name.
The owner is now found at the skill's canonical path first; the full walk is
only the fallback for a skill stored somewhere unusual.
"""
import os
from pathlib import Path

import pytest

from services.memory.skills import SkillsManager


def _write(root: Path, rel_dir: str, name: str, owner: str = "", source: str = "learned") -> None:
    d = root / "skills" / rel_dir
    d.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"name: {name}", "description: helper", "version: 1.0.0",
             "category: general", "tags: []", "status: published", "confidence: 0.8",
             f"source: {source}"]
    lines.append(f"owner: {owner}" if owner else "created: 2026-01-01T00:00:00Z")
    lines += ["---", "", "# When to use", "testing", "", "# Procedure", "- step", ""]
    (d / "SKILL.md").write_text(chr(10).join(lines), encoding="utf-8")


@pytest.fixture
def walks(monkeypatch):
    calls = []
    real = os.walk

    def counting_walk(top, *a, **kw):
        calls.append(str(top))
        return real(top, *a, **kw)

    monkeypatch.setattr(os, "walk", counting_walk)
    return calls


def test_record_use_does_not_walk_the_tree(tmp_path, walks):
    for i in range(10):
        _write(tmp_path, f"cat{i}/filler-{i}", f"filler-{i}", owner="alice")
    _write(tmp_path, "general/deploy-app", "deploy-app", owner="alice")
    sm = SkillsManager(str(tmp_path))
    walks.clear()

    sm.record_use("deploy-app", owner="alice")

    assert walks == []
    usage = sm._load_usage()
    assert usage["alice::deploy-app"]["uses"] == 1


def test_shipped_skill_without_user_copy_counts_under_bare_name(tmp_path, walks):
    _write(tmp_path, "general/shipped-skill", "shipped-skill", source="bundled")
    sm = SkillsManager(str(tmp_path))
    walks.clear()

    sm.record_use("shipped-skill", owner="alice")

    assert walks == []
    usage = sm._load_usage()
    assert usage["shipped-skill"]["uses"] == 1
    assert "alice::shipped-skill" not in usage


def test_user_copy_wins_over_shipped_skill_of_the_same_name(tmp_path):
    _write(tmp_path, "general/twin", "twin", source="bundled")
    _write(tmp_path, "mine/twin", "twin", owner="alice")
    sm = SkillsManager(str(tmp_path))

    sm.record_use("twin", owner="alice")

    assert sm._load_usage()["alice::twin"]["uses"] == 1


def test_skill_stored_at_a_non_canonical_path_is_still_found(tmp_path):
    # An imported bundle can nest a skill deeper than <category>/<name>.
    _write(tmp_path, "imports/pack/nested/odd-dir", "odd-skill", owner="alice")
    sm = SkillsManager(str(tmp_path))

    sm.record_use("odd-skill", owner="alice")

    assert sm._load_usage()["alice::odd-skill"]["uses"] == 1


def test_background_usage_recording_returns_before_the_write_finishes(tmp_path, monkeypatch):
    """The prompt builder runs on the event loop; it must only schedule the write."""
    import threading
    import time

    _write(tmp_path, "general/deploy-app", "deploy-app", owner="alice")
    sm = SkillsManager(str(tmp_path))
    gate = threading.Event()
    real_save = sm._save_usage

    def slow_save(usage):
        gate.wait(5)
        real_save(usage)

    monkeypatch.setattr(sm, "_save_usage", slow_save)

    started = time.monotonic()
    thread = sm.record_uses_in_background(["deploy-app"], owner="alice")
    assert time.monotonic() - started < 0.5
    assert not sm._load_usage()  # nothing written yet

    gate.set()
    thread.join(5)
    assert sm._load_usage()["alice::deploy-app"]["uses"] == 1


def test_background_usage_recording_logs_instead_of_raising(tmp_path, monkeypatch, caplog):
    sm = SkillsManager(str(tmp_path))

    def boom(*a, **kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(sm, "record_use", boom)
    with caplog.at_level("WARNING"):
        sm.record_uses_in_background(["x"], owner="alice").join(5)
    assert "disk full" in caplog.text


def test_concurrent_uses_are_not_lost(tmp_path, monkeypatch):
    """Two writers racing on the sidecar must both count."""
    import threading
    import time

    _write(tmp_path, "general/deploy-app", "deploy-app", owner="alice")
    sm = SkillsManager(str(tmp_path))
    real_save = sm._save_usage

    def slow_save(usage):
        time.sleep(0.05)
        real_save(usage)

    monkeypatch.setattr(sm, "_save_usage", slow_save)
    threads = [threading.Thread(target=sm.record_use, args=("deploy-app", "alice")) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert sm._load_usage()["alice::deploy-app"]["uses"] == 4
