"""Install bundled Odysseus skills into the persistent skill library."""

from __future__ import annotations

import logging
import os
import shutil

from services.memory.skill_format import Skill
from src.runtime_paths import get_app_root


logger = logging.getLogger(__name__)

_BUNDLED_SKILLS = (
    ("dev", "local-pi-delegation", ["delegation", "local-model", "pi", "qwen", "coding", "context-efficiency"], ["linux", "windows"], ["mcp__pi_worker__run_pi_task"]),
    ("general", "harness-context-and-tool-routing", ["harness", "tool-routing", "context", "paths", "reliability"], ["linux", "windows", "macos"], []),
    ("dev", "claude-code-delegation", ["delegation", "claude-code", "coding", "multi-agent", "worktree"], ["linux", "windows", "macos"], ["delegate_to_claude_code"]),
)


def _bundled_source(app_root: str, category: str, name: str) -> str:
    """Path of a bundled skill's source directory.

    Bundled skills live either directly under ``skills/<name>`` (the older
    layout) or under ``skills/<category>/<name>``. The seeder used to look only
    at the first, so a skill checked in under its category directory was
    logged as "source is missing" and never installed.
    """
    for candidate in (
        os.path.join(app_root, "skills", category, name),
        os.path.join(app_root, "skills", name),
    ):
        if os.path.isfile(os.path.join(candidate, "SKILL.md")):
            return candidate
    return os.path.join(app_root, "skills", name)


def seed_bundled_skills(skills_manager) -> list[str]:
    """Install and reconcile bundled skills without replacing their body."""
    installed: list[str] = []
    app_root = get_app_root()
    for category, name, tags, platforms, requires_toolsets in _BUNDLED_SKILLS:
        source = _bundled_source(app_root, category, name)
        destination = os.path.join(skills_manager.skills_root, category, name)
        if not os.path.isfile(os.path.join(source, "SKILL.md")):
            logger.warning("Bundled skill source is missing: %s", source)
            continue
        if not os.path.exists(destination):
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            shutil.copytree(source, destination)
        skill_path = os.path.join(destination, "SKILL.md")
        try:
            with open(skill_path, encoding="utf-8") as handle:
                skill = Skill.from_markdown(handle.read(), path=skill_path)
        except Exception:
            # An operator may have deliberately replaced the installed file.
            # Do not overwrite content we can no longer identify safely.
            logger.warning("Existing bundled skill is not parseable; leaving it unchanged: %s", skill_path)
            continue
        if skill.name != name:
            logger.warning("Existing bundled skill has unexpected name %r; leaving it unchanged", skill.name)
            continue
        # Odysseus supports richer skill-index metadata than the portable
        # SKILL.md schema. Reconcile metadata on every startup so copies made
        # by older releases (which were incorrectly stamped source=user and
        # assigned to one account) become globally readable. Preserve the
        # operator-editable instruction body and reference files.
        skill.category = category
        skill.tags = tags
        skill.platforms = platforms
        skill.requires_toolsets = requires_toolsets
        skill.status = "published"
        skill.confidence = 0.9
        skill.source = "bundled"
        skill.owner = None
        with open(skill_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(skill.to_markdown())
        installed.append(name)
        logger.info("Installed/reconciled bundled skill: %s", name)
    return installed
