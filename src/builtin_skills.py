"""Install bundled Odysseus skills into the persistent skill library."""

from __future__ import annotations

import logging
import os
import shutil

from services.memory.skill_format import Skill
from src.runtime_paths import get_app_root


logger = logging.getLogger(__name__)

_BUNDLED_SKILLS = (
    ("dev", "local-pi-delegation"),
)


def seed_bundled_skills(skills_manager) -> list[str]:
    """Copy missing bundled skills without overwriting operator edits."""
    installed: list[str] = []
    app_root = get_app_root()
    for category, name in _BUNDLED_SKILLS:
        source = os.path.join(app_root, "skills", name)
        destination = os.path.join(skills_manager.skills_root, category, name)
        if os.path.exists(destination):
            continue
        if not os.path.isfile(os.path.join(source, "SKILL.md")):
            logger.warning("Bundled skill source is missing: %s", source)
            continue
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        shutil.copytree(source, destination)
        skill_path = os.path.join(destination, "SKILL.md")
        with open(skill_path, encoding="utf-8") as handle:
            skill = Skill.from_markdown(handle.read(), path=skill_path)
        # Odysseus supports richer skill-index metadata than the portable
        # SKILL.md schema. Enrich only the persistent installed copy.
        skill.category = category
        skill.tags = ["delegation", "local-model", "pi", "qwen", "coding", "context-efficiency"]
        skill.platforms = ["linux", "windows"]
        skill.requires_toolsets = ["mcp__pi_worker__run_pi_task"]
        skill.status = "published"
        skill.confidence = 0.9
        skill.source = "user"
        with open(skill_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(skill.to_markdown())
        installed.append(name)
        logger.info("Installed bundled skill: %s", name)
    return installed
