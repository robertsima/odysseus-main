"""The nightly skill audit must not "rename" a skill onto an occupied slot.

Production logged ``Skill rename target exists: /app/data/skills/general/code-review``
after every scheduled audit. ``SkillsManager.update_skill`` recomputes the
canonical ``<category>/<name>`` path on every write and tries to move the
skill there; when the skill sits somewhere else and the canonical slot is
already taken (a same-named duplicate), each status/confidence write from the
audit — and the audit fixer's recategorization — failed with that warning and
the update was silently dropped, every night. Updates that don't ask for a
rename now land in place; the fixer keeps the current category instead of
colliding. An explicit rename onto an existing skill is still refused.
"""
import logging
import textwrap
from pathlib import Path

from services.memory.skill_format import Skill
from services.memory.skills import SkillsManager
from routes.skills_routes import _apply_skill_md


def _write(root: Path, rel_dir: str, name: str, owner: str, category: str = "general",
           status: str = "draft") -> Path:
    d = root / "skills" / rel_dir
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(textwrap.dedent(f"""\
        ---
        name: {name}
        description: {owner} review helper
        version: 1.0.0
        category: {category}
        tags: []
        status: {status}
        confidence: 0.8
        source: learned
        owner: {owner}
        ---

        # When to use
        reviewing code

        # Procedure
        - read the diff
        """), encoding="utf-8")
    return d / "SKILL.md"


def _read(path: Path) -> Skill:
    return Skill.from_markdown(path.read_text(encoding="utf-8"), path=str(path))


def _no_rename_warning(caplog):
    return not any("rename target exists" in r.getMessage() for r in caplog.records)


def test_status_update_on_noncanonical_skill_lands_in_place(tmp_path, caplog):
    # Bob owns the canonical general/code-review slot; Alice's same-named skill
    # was dropped in at a non-canonical path (no category dir).
    bob = _write(tmp_path, "general/code-review", "code-review", "bob")
    alice = _write(tmp_path, "code-review", "code-review", "alice")
    sm = SkillsManager(str(tmp_path))

    with caplog.at_level(logging.DEBUG, logger="services.memory.skills"):
        for _ in range(2):  # idempotent across nightly runs
            assert sm.update_skill("code-review", {"status": "published", "confidence": 0.95},
                                   owner="alice") is True

    assert _no_rename_warning(caplog)
    assert _read(alice).status == "published"
    assert _read(alice).confidence == 0.95
    # Other user's skill untouched, nothing moved or deleted.
    assert _read(bob).owner == "bob" and _read(bob).status == "draft"
    assert alice.exists() and bob.exists()


def test_noncanonical_skill_still_moves_when_slot_is_free(tmp_path):
    alice = _write(tmp_path, "code-review", "code-review", "alice")
    sm = SkillsManager(str(tmp_path))

    assert sm.update_skill("code-review", {"status": "published"}, owner="alice") is True

    moved = tmp_path / "skills" / "general" / "code-review" / "SKILL.md"
    assert moved.exists() and not alice.exists()
    assert _read(moved).status == "published"


def test_explicit_rename_onto_existing_skill_is_still_refused(tmp_path, caplog):
    _write(tmp_path, "general/code-review", "code-review", "bob")
    alice = _write(tmp_path, "general/review-helper", "review-helper", "alice")
    sm = SkillsManager(str(tmp_path))

    with caplog.at_level(logging.WARNING, logger="services.memory.skills"):
        assert sm.update_skill("review-helper", {"name": "code-review"}, owner="alice") is False

    assert not _no_rename_warning(caplog)
    assert alice.exists() and _read(alice).name == "review-helper"


def test_audit_fixer_recategorization_onto_occupied_slot_keeps_category(tmp_path, caplog):
    bob = _write(tmp_path, "general/code-review", "code-review", "bob")
    alice = _write(tmp_path, "imported/code-review", "code-review", "alice", category="imported")
    sm = SkillsManager(str(tmp_path))

    fixed = alice.read_text(encoding="utf-8").replace(
        "category: imported", "category: general"
    ).replace("description: alice review helper", "description: alice review helper, sharpened")

    with caplog.at_level(logging.WARNING, logger="services.memory.skills"):
        for _ in range(2):  # the audit re-proposes this every night
            assert _apply_skill_md(sm, "code-review", fixed, "alice") is True

    assert _no_rename_warning(caplog)
    updated = _read(alice)
    assert updated.category == "imported"
    assert updated.description == "alice review helper, sharpened"
    assert _read(bob).owner == "bob"
    assert _read(bob).description == "bob review helper"
