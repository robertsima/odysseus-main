"""Install bundled Odysseus skills into the persistent skill library."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil

from services.memory.skill_format import Skill
from src.runtime_paths import get_app_root


logger = logging.getLogger(__name__)

_BUNDLED_SKILLS = (
    ("dev", "local-pi-delegation", ["delegation", "local-model", "pi", "qwen", "coding", "context-efficiency"], ["linux", "windows"], ["mcp__pi_worker__run_pi_task"]),
    ("general", "harness-context-and-tool-routing", ["harness", "tool-routing", "context", "paths", "reliability"], ["linux", "windows", "macos"], []),
    ("design", "penpot-design-workflow", ["design", "penpot", "mockup", "icons", "neo-brutalism", "visual-verification"], ["linux", "windows", "macos"], [
        "mcp__penpot_studio__build_design", "mcp__penpot_studio__render_preview", "mcp__penpot_studio__inspect_design", "mcp__penpot_studio__search_icons",
    ]),
    ("dev", "claude-code-delegation", ["delegation", "claude-code", "coding", "multi-agent", "worktree"], ["linux", "windows", "macos"], ["delegate_to_claude_code"]),
    # No toolset required: without Penpot's search_icons it fetches from the
    # Iconify API directly.
    ("design", "visual-asset-sourcing", ["design", "logo", "icons", "sprites", "illustration", "mascot", "licensing"], ["linux", "windows", "macos"], []),
    # Community skills (see ACKNOWLEDGMENTS.md, "Agent skills"); none needs a toolset.
    ("general", "grilling", ["grilling", "interview", "planning", "decision-making"], ["linux", "windows", "macos"], []),
    ("general", "writing-for-agents", ["writing", "skills", "prompts", "context"], ["linux", "windows", "macos"], []),
    ("general", "unslop", ["writing", "editing", "style"], ["linux", "windows", "macos"], []),
    ("dev", "diagnosing-bugs", ["debugging", "diagnosis", "regression", "performance"], ["linux", "windows", "macos"], []),
    ("dev", "codebase-design", ["architecture", "modules", "design"], ["linux", "windows", "macos"], []),
    ("dev", "domain-modeling", ["glossary", "adr", "domain"], ["linux", "windows", "macos"], []),
    ("dev", "improve-codebase-architecture", ["architecture", "refactoring", "testability"], ["linux", "windows", "macos"], []),
    ("dev", "triage", ["triage", "issues", "pull-requests", "backlog"], ["linux", "windows", "macos"], []),
    ("dev", "resolving-merge-conflicts", ["git", "merge", "rebase", "conflicts"], ["linux", "windows", "macos"], []),
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


# Fields that reach the model when a skill is indexed or injected. Metadata the
# seeder reconciles or usage bookkeeping updates (source, status, uses, ...) is
# deliberately not compared.
_PROMPT_VISIBLE_FIELDS = (
    "name", "title", "description", "when_to_use", "procedure", "pitfalls",
    "verification", "problem", "solution", "steps", "body_extra",
)


# Digests of every SKILL.md body any release has shipped, per skill name. Built
# from git history by scripts/update_bundled_skill_history.py. It is how the
# seeder tells "the operator never touched this, it is just an older release"
# (safe to upgrade) from "someone edited it" (leave alone).
_HISTORY_PATH = ("skills", ".bundled-history.json")
_customised_logged: set[str] = set()


def skill_digest(text: str, path: str | None = None) -> str:
    """SHA-256 of a SKILL.md's prompt-visible content, stable across installs.

    The installed file is not the repository file: the seeder rewrites it via
    `Skill.to_markdown()` (new frontmatter, re-flowed body), and checkouts on
    Windows may carry CRLF. Hashing raw bytes would call every install
    "edited". So the text is parsed, round-tripped once through to_markdown()
    (the same normalization `is_shipped_skill` uses) and only the prompt-visible
    fields are hashed, with line endings folded to LF. Metadata the seeder or
    usage bookkeeping rewrites (source, status, uses, ...) cannot change it.
    """
    skill = Skill.from_markdown(text.replace("\r\n", "\n"), path=path)
    skill = Skill.from_markdown(skill.to_markdown(), path=path)
    payload = {
        field: getattr(skill, field, None) for field in _PROMPT_VISIBLE_FIELDS
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def skill_file_digest(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return skill_digest(handle.read(), path=path)


def load_bundled_history(app_root: str) -> dict[str, list[str]]:
    try:
        with open(os.path.join(app_root, *_HISTORY_PATH), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return {k: list(v) for k, v in data.items() if isinstance(v, list)} if isinstance(data, dict) else {}


def seed_bundled_skills(skills_manager) -> list[str]:
    """Install bundled skills, upgrade unedited ones, and reconcile metadata.

    2026-10-01: the body used to be kept forever once installed, so a revised
    bundled skill never reached an existing server, and `is_shipped_skill`
    (exactly-current only) stopped trusting every unedited install the moment
    the repository copy changed. Now an installed body whose digest appears in
    the release history is an old, unedited release and is replaced by the
    current one; anything else is an operator/agent edit and is kept.
    """
    installed: list[str] = []
    app_root = get_app_root()
    history = load_bundled_history(app_root)
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
        # assigned to one account) become globally readable. Customised
        # bodies and reference files are preserved (see the upgrade above).
        try:
            current_digest = skill_file_digest(os.path.join(source, "SKILL.md"))
            installed_digest = skill_file_digest(skill_path)
        except Exception:
            current_digest = installed_digest = ""
        if installed_digest and installed_digest != current_digest:
            if installed_digest in history.get(name, ()):
                # Unedited older release: replace the whole directory so
                # supporting files (references/, agents/) move with the body.
                shutil.rmtree(destination)
                shutil.copytree(source, destination)
                with open(skill_path, encoding="utf-8") as handle:
                    skill = Skill.from_markdown(handle.read(), path=skill_path)
                logger.info("upgraded bundled skill %s", name)
            elif name not in _customised_logged:
                _customised_logged.add(name)
                logger.info("bundled skill %s is customised; not upgraded", name)
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


def is_shipped_skill(entry) -> bool:
    """True when a loaded skill is bundled AND still exactly what this release ships.

    `source: bundled` alone proves nothing: the seeder re-stamps it on every
    start but keeps the installed body, which `manage_skills` (and so an agent)
    can edit. Only content that matches the repository copy is as trustworthy
    as the code, so only that may skip the prompt-injection tool gate. Both
    copies are parsed the same way, because the seeder rewrites the installed
    file through `Skill.to_markdown()`, and the shipped copy is normalized the
    same way before comparing.
    """
    if not isinstance(entry, dict) or entry.get("source") != "bundled":
        return False
    name = entry.get("name")
    spec = next((item for item in _BUNDLED_SKILLS if item[1] == name), None)
    installed = entry.get("path")
    if spec is None or not installed:
        return False
    shipped = os.path.join(_bundled_source(get_app_root(), spec[0], name), "SKILL.md")
    try:
        with open(shipped, encoding="utf-8") as handle:
            shipped_skill = Skill.from_markdown(handle.read(), path=shipped)
        # The seeder writes the installed copy through to_markdown(), which is
        # not a lossless round trip; compare against the same normalization.
        shipped_skill = Skill.from_markdown(shipped_skill.to_markdown(), path=shipped)
        with open(installed, encoding="utf-8") as handle:
            installed_skill = Skill.from_markdown(handle.read(), path=installed)
    except Exception:
        return False
    return all(
        getattr(shipped_skill, field, None) == getattr(installed_skill, field, None)
        for field in _PROMPT_VISIBLE_FIELDS
    )
