"""Install and maintain the skills Odysseus ships.

Skill tiers (owner decision 2026-10-01: "I would only really want to keep the
core skills as something that auto installs; any other recommended or curated
skills are optional"):

  core         seeded into every deployment (`source: bundled`). Core skills use
               only core tools, and the harness coding rules name them.
  curated      shipped in the repository, installed on request from the
               "Recommended skills" catalog (`source: curated`), removable again.
  integration  owned by an integration package; installed and visible only with
               that integration (`source: integration`, `requires_integration`).
               See register_integration_skills.

Core and curated skills are declared in skills/catalog.json, the one place a
shipped skill is listed. Integration skills are not in it: they come from the
integration's own package.

All three are app-shipped: ownerless, readable by every user, upgraded by the
seeder while unedited, and never edited through manage_skills.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
from typing import Optional

from services.memory.skill_format import APP_SHIPPED_SOURCES, Skill
from src.runtime_paths import get_app_root


logger = logging.getLogger(__name__)

CATALOG_PATH = ("skills", "catalog.json")
TIER_CORE = "core"
TIER_CURATED = "curated"
_TIERS = (TIER_CORE, TIER_CURATED)

# Skills earlier releases bundled that now belong to an integration, mapped to
# that integration's id. Used only to re-stamp existing installs and to find the
# repository copy until the integration package carries it; the authoritative
# owner of a skill is the integration that registers it.
_LEGACY_INTEGRATION_SKILLS = {
    "penpot-design-workflow": "penpot",
    "claude-code-delegation": "claude-code",
    "local-pi-delegation": "pi-worker",
    "todoist-planning": "todoist",
    "todoist-retrospective": "todoist",
}

_integration_skill_dirs: dict[str, list[str]] = {}


class SkillCatalogError(Exception):
    """A catalog install/uninstall that cannot proceed. `status` is the HTTP
    status the route should answer with; `code` lets the UI offer a next step."""

    def __init__(self, message: str, status: int = 400, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code


# ---------------------------------------------------------------------------
# Integration hook
# ---------------------------------------------------------------------------

def register_integration_skills(integration_id: str, dirs) -> None:
    """Declare the skills an integration package ships.

    `dirs` is a list of paths; each is either one skill directory (it contains
    SKILL.md) or a parent whose children are skill directories. The next
    `seed_bundled_skills` call installs them as `source: integration` with
    `requires_integration: <integration_id>`, so
    `SkillsManager.index_for(available_integrations=...)` hides them while the
    integration is absent. Tags, platforms and requires_toolsets come from the
    skill's own frontmatter. Registering the same id again replaces its
    directories.
    """
    integration_id = (integration_id or "").strip()
    if not integration_id:
        raise ValueError("integration_id is required")
    _integration_skill_dirs[integration_id] = [os.fspath(d) for d in (dirs or ())]


def unregister_integration_skills(integration_id: str) -> None:
    _integration_skill_dirs.pop(integration_id, None)


def _expand_skill_dirs(dirs) -> list[str]:
    out: list[str] = []
    for d in dirs:
        if os.path.isfile(os.path.join(d, "SKILL.md")):
            out.append(d)
        elif os.path.isdir(d):
            for child in sorted(os.listdir(d)):
                if os.path.isfile(os.path.join(d, child, "SKILL.md")):
                    out.append(os.path.join(d, child))
    return out


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

def load_catalog(app_root: Optional[str] = None) -> list[dict]:
    """The core and curated skills this release ships, from skills/catalog.json.

    Each entry: name, category, tier (core|curated), tags, platforms,
    requires_toolsets, and for curated skills summary, license and source. A
    missing or malformed file yields an empty list (seeding then installs
    nothing instead of failing startup); malformed entries are skipped.
    """
    root = app_root or get_app_root()
    path = os.path.join(root, *CATALOG_PATH)
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        logger.warning("Skill catalog is missing or unreadable: %s", path)
        return []
    rows = data.get("skills") if isinstance(data, dict) else None
    out: list[dict] = []
    for row in rows if isinstance(rows, list) else ():
        if not isinstance(row, dict) or not row.get("name") or row.get("tier") not in _TIERS:
            logger.warning("Skipping malformed skill catalog entry: %r", row)
            continue
        out.append({
            "name": str(row["name"]),
            "category": str(row.get("category") or "general"),
            "tier": row["tier"],
            "tags": list(row.get("tags") or []),
            "platforms": list(row.get("platforms") or []),
            "requires_toolsets": list(row.get("requires_toolsets") or []),
            "summary": str(row.get("summary") or ""),
            "license": str(row.get("license") or ""),
            "source": str(row.get("source") or ""),
        })
    return out


def _catalog_entry(name: str, app_root: Optional[str] = None) -> Optional[dict]:
    return next((e for e in load_catalog(app_root) if e["name"] == name), None)


def _bundled_source(app_root: str, category: str, name: str) -> str:
    """Path of a shipped skill's source directory.

    Shipped skills live either directly under ``skills/<name>`` (the older
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


def _integration_specs(app_root: str) -> list[dict]:
    """Every integration-owned skill this process can locate, with its source.

    Registered packages first. Then the legacy names found in the repository
    with `install` False: an existing install is upgraded and re-stamped, but a
    deployment that never had the skill does not get it.
    """
    specs: list[dict] = []
    seen: set[str] = set()
    for integration_id, dirs in _integration_skill_dirs.items():
        for skill_dir in _expand_skill_dirs(dirs):
            try:
                with open(os.path.join(skill_dir, "SKILL.md"), encoding="utf-8") as handle:
                    sk = Skill.from_markdown(handle.read())
            except Exception:
                logger.warning("Integration skill is not parseable: %s", skill_dir)
                continue
            if sk.name in seen:
                continue
            seen.add(sk.name)
            specs.append({
                "name": sk.name, "category": sk.category, "source_dir": skill_dir,
                "integration": integration_id, "install": True,
                "tags": sk.tags, "platforms": sk.platforms, "requires_toolsets": sk.requires_toolsets,
            })
    for name, integration_id in _LEGACY_INTEGRATION_SKILLS.items():
        if name in seen:
            continue
        # The package copy first (2026-10-01: these skills moved from skills/ into
        # integrations/<id>/skills/); the old skills/ locations stay for a
        # checkout that still has them.
        for candidate in (
            os.path.join(app_root, "integrations", integration_id, "skills", name),
            *(os.path.join(app_root, "skills", c, name) for c in ("dev", "design", "general")),
            os.path.join(app_root, "skills", name),
        ):
            if os.path.isfile(os.path.join(candidate, "SKILL.md")):
                specs.append({
                    "name": name, "category": None, "source_dir": candidate,
                    "integration": integration_id, "install": False,
                    "tags": None, "platforms": None, "requires_toolsets": None,
                })
                break
    return specs


def shipped_source_dir(name: str, app_root: Optional[str] = None) -> Optional[str]:
    """Repository directory holding the shipped copy of skill `name`, if any."""
    root = app_root or get_app_root()
    entry = _catalog_entry(name, root)
    if entry:
        path = _bundled_source(root, entry["category"], name)
        return path if os.path.isfile(os.path.join(path, "SKILL.md")) else None
    for spec in _integration_specs(root):
        if spec["name"] == name:
            return spec["source_dir"]
    return None


# Fields that reach the model when a skill is indexed or injected. Metadata the
# seeder reconciles or usage bookkeeping updates (source, status, uses,
# requires_integration, ...) is deliberately not compared.
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


def skill_digest(text: str, path: Optional[str] = None) -> str:
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


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------

def _find_installed(skills_manager, name: str, sources=None) -> tuple[Optional[str], Optional[Skill]]:
    """Path and parsed skill of the installed `name`, preferring an app-shipped copy."""
    fallback: tuple[Optional[str], Optional[Skill]] = (None, None)
    for path in skills_manager._iter_skill_files():
        sk = skills_manager._read_skill(path)
        if not sk or sk.name != name:
            continue
        if sources is None or sk.source in sources:
            return path, sk
        if fallback[0] is None:
            fallback = (path, sk)
    return fallback if sources is None else (None, None)


def _restamp_legacy_installs(skills_manager, app_root: str) -> list[str]:
    """Re-stamp installs from before the tiers existed; never delete anything.

    2026-10-01: until now every shipped skill was installed as
    `source: bundled`. The owner's server has all of them. Core skills keep
    that stamp. A name that is now curated becomes `source: curated` (still
    installed, ownerless, readable by all, upgradable, protected from
    manage_skills); a name now owned by an integration becomes
    `source: integration` with `requires_integration` set. Only copies still
    stamped `bundled` move: an operator's own skill of the same name is left
    alone.
    """
    curated = {e["name"] for e in load_catalog(app_root) if e["tier"] == TIER_CURATED}
    owners = dict(_LEGACY_INTEGRATION_SKILLS)
    for spec in _integration_specs(app_root):
        owners[spec["name"]] = spec["integration"]
    changed: list[str] = []
    for path in list(skills_manager._iter_skill_files()):
        sk = skills_manager._read_skill(path)
        if not sk or sk.source != "bundled":
            continue
        if sk.name in owners:
            sk.source, sk.requires_integration = "integration", owners[sk.name]
        elif sk.name in curated:
            sk.source = "curated"
        else:
            continue
        sk.owner = None
        try:
            skills_manager._write_skill_at(sk, path)
        except Exception as exc:
            logger.warning("Could not re-stamp installed skill %s: %s", sk.name, exc)
            continue
        changed.append(sk.name)
        logger.info("Re-stamped installed skill %s as %s%s", sk.name, sk.source,
                    f" (requires_integration={sk.requires_integration})" if sk.requires_integration else "")
    return changed


def _reconcile_one(skills_manager, *, name: str, source_dir: str, destination: Optional[str],
                   history: dict, source_label: str, install: bool, fields: dict) -> bool:
    """Install (when `install`), upgrade if unedited, and re-stamp one skill.

    Returns True when an installed copy was reconciled. `destination` None
    means locate the installed copy by name.
    """
    source_md = os.path.join(source_dir, "SKILL.md")
    if not os.path.isfile(source_md):
        logger.warning("Shipped skill source is missing: %s", source_dir)
        return False
    if destination is None:
        found, _ = _find_installed(skills_manager, name, (source_label,))
        destination = os.path.dirname(found) if found else None
    if destination is None or not os.path.exists(destination):
        if not install:
            return False
        if destination is None:
            destination = os.path.join(skills_manager.skills_root, fields.get("category") or "general", name)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        shutil.copytree(source_dir, destination)
    skill_path = os.path.join(destination, "SKILL.md")
    try:
        with open(skill_path, encoding="utf-8") as handle:
            skill = Skill.from_markdown(handle.read(), path=skill_path)
    except Exception:
        # An operator may have deliberately replaced the installed file.
        # Do not overwrite content we can no longer identify safely.
        logger.warning("Existing shipped skill is not parseable; leaving it unchanged: %s", skill_path)
        return False
    if skill.name != name:
        logger.warning("Existing shipped skill has unexpected name %r; leaving it unchanged", skill.name)
        return False
    # Odysseus supports richer skill-index metadata than the portable SKILL.md
    # schema. Reconcile metadata on every startup so copies made by older
    # releases (which were incorrectly stamped source=user and assigned to one
    # account) become globally readable. Customised bodies and reference files
    # are preserved (see the upgrade below).
    try:
        current_digest = skill_file_digest(source_md)
        installed_digest = skill_file_digest(skill_path)
    except Exception:
        current_digest = installed_digest = ""
    if installed_digest and installed_digest != current_digest:
        if installed_digest in history.get(name, ()):
            # Unedited older release: replace the whole directory so
            # supporting files (references/, agents/) move with the body.
            shutil.rmtree(destination)
            shutil.copytree(source_dir, destination)
            with open(skill_path, encoding="utf-8") as handle:
                skill = Skill.from_markdown(handle.read(), path=skill_path)
            logger.info("upgraded %s skill %s", source_label, name)
        elif name not in _customised_logged:
            _customised_logged.add(name)
            logger.info("%s skill %s is customised; not upgraded", source_label, name)
    for key, value in fields.items():
        if value is not None:
            setattr(skill, key, value)
    skill.status = "published"
    skill.confidence = 0.9
    skill.source = source_label
    skill.owner = None
    with open(skill_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(skill.to_markdown())
    return True


def seed_bundled_skills(skills_manager) -> list[str]:
    """Install core skills, upgrade unedited shipped skills, reconcile metadata.

    Only core skills are installed on a fresh deployment. Curated skills are
    maintained (upgrade, metadata) only where the operator installed them from
    the catalog; integration skills are installed when their package is
    registered. Installs from before the tiers existed are re-stamped first, so
    a curated or integration skill on an existing server stays installed.

    2026-10-01: the body used to be kept forever once installed, so a revised
    shipped skill never reached an existing server, and `is_shipped_skill`
    (exactly-current only) stopped trusting every unedited install the moment
    the repository copy changed. Now an installed body whose digest appears in
    the release history is an old, unedited release and is replaced by the
    current one; anything else is an operator/agent edit and is kept.
    """
    installed: list[str] = []
    app_root = get_app_root()
    history = load_bundled_history(app_root)
    _restamp_legacy_installs(skills_manager, app_root)
    for entry in load_catalog(app_root):
        name, category = entry["name"], entry["category"]
        core = entry["tier"] == TIER_CORE
        fields = {
            "category": category, "tags": entry["tags"], "platforms": entry["platforms"],
            "requires_toolsets": entry["requires_toolsets"],
        }
        ok = _reconcile_one(
            skills_manager, name=name,
            source_dir=_bundled_source(app_root, category, name),
            # Core keeps the canonical path (and adopts a legacy copy there);
            # curated is located by its curated stamp so an operator's own
            # skill of the same name is never touched.
            destination=os.path.join(skills_manager.skills_root, category, name) if core else None,
            history=history, source_label="bundled" if core else "curated",
            install=core, fields=fields,
        )
        if ok:
            installed.append(name)
            logger.info("Installed/reconciled %s skill: %s", entry["tier"], name)
    for spec in _integration_specs(app_root):
        fields = {
            "category": spec["category"], "tags": spec["tags"], "platforms": spec["platforms"],
            "requires_toolsets": spec["requires_toolsets"], "requires_integration": spec["integration"],
        }
        if _reconcile_one(
            skills_manager, name=spec["name"], source_dir=spec["source_dir"], destination=None,
            history=history, source_label="integration", install=spec["install"], fields=fields,
        ):
            installed.append(spec["name"])
    return installed


def is_shipped_skill(entry) -> bool:
    """True when a loaded skill is app-shipped AND still exactly what this release ships.

    A shipped `source` alone proves nothing: the seeder re-stamps it on every
    start but keeps the installed body, which `manage_skills` (and so an agent)
    can edit. Only content that matches the repository copy is as trustworthy
    as the code, so only that may skip the prompt-injection tool gate. Both
    copies are parsed the same way, because the seeder rewrites the installed
    file through `Skill.to_markdown()`, and the shipped copy is normalized the
    same way before comparing.
    """
    if not isinstance(entry, dict) or entry.get("source") not in APP_SHIPPED_SOURCES:
        return False
    name = entry.get("name")
    installed = entry.get("path")
    shipped_dir = shipped_source_dir(name) if name else None
    if not shipped_dir or not installed:
        return False
    shipped = os.path.join(shipped_dir, "SKILL.md")
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


# ---------------------------------------------------------------------------
# Curated catalog: list, install, uninstall
# ---------------------------------------------------------------------------

def _install_state(skills_manager, entry: dict, app_root: str, history: dict) -> tuple[str, Optional[str]]:
    """(state, installed path) for a curated entry.

    State: "not_installed", "current", "outdated" (an older unedited release;
    the next start upgrades it), "edited", or "conflict" (a skill of the same
    name that did not come from the catalog).
    """
    path, sk = _find_installed(skills_manager, entry["name"])
    if not sk:
        return "not_installed", None
    if sk.source != "curated":
        return "conflict", path
    source_md = os.path.join(_bundled_source(app_root, entry["category"], entry["name"]), "SKILL.md")
    try:
        installed_digest = skill_file_digest(path)
        current_digest = skill_file_digest(source_md)
    except Exception:
        return "edited", path
    if installed_digest == current_digest:
        return "current", path
    if installed_digest in history.get(entry["name"], ()):
        return "outdated", path
    return "edited", path


def curated_catalog(skills_manager) -> list[dict]:
    """The curated skills with their install state, for the Recommended skills UI."""
    app_root = get_app_root()
    history = load_bundled_history(app_root)
    out = []
    for entry in load_catalog(app_root):
        if entry["tier"] != TIER_CURATED:
            continue
        state, _ = _install_state(skills_manager, entry, app_root, history)
        out.append({
            "name": entry["name"], "category": entry["category"], "tags": entry["tags"],
            "summary": entry["summary"], "license": entry["license"], "source": entry["source"],
            "installed": state in ("current", "outdated", "edited"),
            "state": state,
        })
    out.sort(key=lambda e: (e["category"], e["name"]))
    return out


def install_curated_skill(skills_manager, name: str) -> dict:
    """Copy a curated skill from the repository into the library as `source: curated`."""
    app_root = get_app_root()
    entry = _catalog_entry(name, app_root)
    if not entry or entry["tier"] != TIER_CURATED:
        raise SkillCatalogError(f"No recommended skill named {name!r}", 404, "unknown")
    source_dir = _bundled_source(app_root, entry["category"], name)
    if not os.path.isfile(os.path.join(source_dir, "SKILL.md")):
        raise SkillCatalogError(f"The shipped copy of {name!r} is missing", 500, "missing_source")
    existing, existing_skill = _find_installed(skills_manager, name)
    if existing_skill:
        code = "installed" if existing_skill.source == "curated" else "conflict"
        raise SkillCatalogError(f"A skill named {name!r} is already installed", 409, code)
    destination = os.path.join(skills_manager.skills_root, entry["category"], name)
    if os.path.lexists(destination):
        raise SkillCatalogError(f"{destination} already exists", 409, "conflict")
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    shutil.copytree(source_dir, destination)
    ok = _reconcile_one(
        skills_manager, name=name, source_dir=source_dir, destination=destination,
        history=load_bundled_history(app_root), source_label="curated", install=True,
        fields={"category": entry["category"], "tags": entry["tags"], "platforms": entry["platforms"],
                "requires_toolsets": entry["requires_toolsets"]},
    )
    if not ok:
        shutil.rmtree(destination, ignore_errors=True)
        raise SkillCatalogError(f"Could not install {name!r}", 500, "install_failed")
    logger.info("Installed curated skill: %s", name)
    return {"name": name, "installed": True}


def uninstall_curated_skill(skills_manager, name: str, *, keep_for: Optional[str] = None,
                            keep: bool = False) -> dict:
    """Remove an installed curated skill.

    Refuses when the installed copy was edited (error code "edited"): deleting
    would throw the edits away. With `keep=True` the edited copy is instead
    handed to `keep_for` as their own skill (`source: user`, owned, editable)
    and stays installed.
    """
    app_root = get_app_root()
    entry = _catalog_entry(name, app_root)
    if not entry or entry["tier"] != TIER_CURATED:
        raise SkillCatalogError(f"No recommended skill named {name!r}", 404, "unknown")
    state, path = _install_state(skills_manager, entry, app_root, load_bundled_history(app_root))
    if state == "not_installed":
        raise SkillCatalogError(f"{name!r} is not installed", 404, "not_installed")
    if state == "conflict":
        raise SkillCatalogError(
            f"The installed {name!r} was not installed from the catalog; delete it from the skills list instead",
            409, "conflict")
    skill_dir = os.path.dirname(path)
    if state == "edited":
        if not keep:
            raise SkillCatalogError(
                f"{name!r} has been edited. Keep it as your own skill, or remove it and lose the edits.",
                409, "edited")
        sk = skills_manager._read_skill(path)
        sk.source, sk.owner = "user", keep_for
        skills_manager._write_skill_at(sk, path)
        logger.info("Curated skill %s kept as %s's own skill", name, keep_for or "the operator")
        return {"name": name, "installed": True, "kept": True}
    shutil.rmtree(skill_dir)
    usage = skills_manager._load_usage()
    if usage.pop(name, None) is not None:
        skills_manager._save_usage(usage)
    logger.info("Uninstalled curated skill: %s", name)
    return {"name": name, "installed": False, "kept": False}
