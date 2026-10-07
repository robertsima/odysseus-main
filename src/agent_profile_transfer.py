"""Export and import agent profiles (loadouts) as a portable JSON document.

The document is deliberately small and versioned::

    {"format": "agamemnon-agent-profiles", "version": 1,
     "exported_at": "2026-09-23T12:00:00Z", "profiles": [...]}

Each entry in ``profiles`` is exactly what
:func:`src.agent_profiles.validate_profiles` returns, so the whitelist of
fields is the validator's, not this module's: anything else a stored profile
might carry never leaves the machine. Free-text fields go through the same
credential redaction as the log viewer, because a pasted token in a loadout's
instructions is the one secret a profile can hold.

Importing is not a side door. Every profile goes through ``validate_profiles``
again, then through ``prepare`` (the caller's own save rule: the Settings
route validates, the agent tool clamps to the calling chat's policy exactly as
``create`` does), and the combined list is written through the same
``agent_loadouts._write`` a normal save uses.

Templates (2026-10-01): a file may name an MCP server by what it is instead of
by the id one machine gave it. ``mcp__{server:penpot}__create_frame`` and
``{server:penpot}`` in ``allowed_mcp_servers`` resolve on import to the id of
the connected or saved server with that name (see :func:`resolve_template`).
The shipped Penpot loadout used to hard-code ``c5ec6d7a``, an id that exists on
one install, so on any other machine the designer was bound to tools that were
never there. Integration packages ship such templates under
``integrations/<id>/loadouts/``; :func:`list_templates` and
:func:`install_template` expose the ones whose integration is available.
"""

from __future__ import annotations

import copy
import datetime as _dt
import json
import re
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from src import agent_loadouts, agent_profiles

FORMAT = "agamemnon-agent-profiles"
LEGACY_FORMAT = "odysseus-agent-profiles"
VERSION = 1
MODES = ("merge", "replace")
_TEXT_FIELDS = ("description", "instructions", "persona_name")

# (validated profile) -> (profile to store, narrowing notes); raises ValueError.
Prepare = Callable[[Dict[str, Any]], Tuple[Dict[str, Any], List[str]]]
# model spec -> why it is unavailable here, or None.
ModelCheck = Callable[[str], Optional[str]]


def _redact(text: str) -> str:
    from src.agent_logs import redact_text

    return redact_text(text)


def _portable(profile: Dict[str, Any]) -> Dict[str, Any]:
    """One profile with secrets redacted and machine-specific snapshots dropped."""
    # Re-validated here rather than trusted: the validator's field list is the
    # export's whitelist, whatever else a stored entry has picked up.
    out = agent_profiles.validate_profiles([profile])[0]
    for field in _TEXT_FIELDS:
        out[field] = _redact(str(out.get(field) or ""))
    # For selected/none tool access, disabled_tools is mostly a snapshot of this
    # machine's tool inventory (known tools minus the grant), which the positive
    # policy already implies. Keep only denials that still mean something.
    access = out.get("tool_access")
    if access == "none":
        out["disabled_tools"] = []
    elif access == "selected":
        out["disabled_tools"] = sorted(set(out.get("disabled_tools") or []) & set(out.get("enabled_tools") or []))
    return out


def _wanted_names(names: Any) -> Optional[List[str]]:
    if names is None:
        return None
    if isinstance(names, str):
        names = names.split(",")
    if not isinstance(names, (list, tuple)):
        raise ValueError("names must be a list or a comma-separated string")
    cleaned = [str(n).strip() for n in names if str(n).strip()]
    return cleaned or None


def export_profiles(names: Any = None) -> Dict[str, Any]:
    """The stored profiles (all, or only ``names``) as a portable document."""
    profiles = agent_profiles.load_profiles()
    wanted = _wanted_names(names)
    if wanted is not None:
        by_key = {p["name"].casefold(): p for p in profiles}
        missing = [n for n in wanted if n.casefold() not in by_key]
        if missing:
            raise ValueError(f"no profile named {', '.join(repr(n) for n in missing)}")
        seen = set()
        profiles = []
        for n in wanted:
            if n.casefold() not in seen:
                seen.add(n.casefold())
                profiles.append(by_key[n.casefold()])
    return {
        "format": FORMAT,
        "version": VERSION,
        "exported_at": _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "profiles": [_portable(p) for p in profiles],
    }


def _check_document(doc: Any) -> List[Any]:
    if not isinstance(doc, dict):
        raise ValueError("import: expected a JSON object")
    if doc.get("format") not in (FORMAT, LEGACY_FORMAT):
        raise ValueError(f"import: not an agent profile export (format must be {FORMAT!r})")
    version = doc.get("version")
    if isinstance(version, bool) or version != VERSION:
        raise ValueError(f"import: unsupported version {version!r} (this server reads version {VERSION})")
    raw = doc.get("profiles")
    if not isinstance(raw, list):
        raise ValueError("import: 'profiles' must be a list")
    if len(raw) > agent_profiles.MAX_PROFILES:
        raise ValueError(f"import: {len(raw)} profiles in the file; at most {agent_profiles.MAX_PROFILES}")
    return raw


def _free_name(name: str, taken: Iterable[str]) -> Optional[str]:
    """``name 2``, ``name 3``… — the first that is free and still a valid name."""
    taken = {t.casefold() for t in taken}
    for n in range(2, 100):
        suffix = f" {n}"
        candidate = name[: 40 - len(suffix)].rstrip() + suffix
        if candidate.casefold() not in taken and agent_profiles._NAME_RE.match(candidate):
            return candidate
    return None


# ---------------------------------------------------------------------------
# Templates: {server:<name>} references
# ---------------------------------------------------------------------------

_SERVER_REF = re.compile(r"\{server:([^{}]+)\}")
# Profile list fields that may carry a {server:...} reference.
_TEMPLATE_FIELDS = ("enabled_tools", "disabled_tools", "allowed_mcp_servers")
_BUILTIN_PREFIX = "built-in:"


def _server_candidates(manager: Any) -> List[Dict[str, Any]]:
    """Every MCP server this install knows: ``{id, name, builtin, connected, enabled}``.

    Three sources: the built-in integrations' servers, the saved rows (the
    admin's own servers) and the manager's live connections.
    """
    out: Dict[str, Dict[str, Any]] = {}

    def add(server_id: str, name: str, *, builtin: bool = False, connected: bool = False,
            enabled: bool = True) -> None:
        if not server_id:
            return
        row = out.setdefault(server_id, {"id": server_id, "name": name or server_id, "builtin": builtin,
                                         "connected": False, "enabled": enabled})
        row["connected"] = row["connected"] or connected
        row["builtin"] = row["builtin"] or builtin
        if name and row["name"] == server_id:
            row["name"] = name

    try:
        from src import integration_registry

        for integration in integration_registry.all():
            for server in integration.servers:
                add(server.id, server.name, builtin=True)
    except Exception:  # pragma: no cover - registry unreadable
        pass
    try:
        from core.database import McpServer, SessionLocal

        db = SessionLocal()
        try:
            for row in db.query(McpServer).all():
                add(str(row.id), str(row.name or ""), enabled=bool(row.is_enabled))
        finally:
            db.close()
    except Exception:
        pass
    if manager is None:
        try:
            from src.tool_utils import get_mcp_manager

            manager = get_mcp_manager()
        except Exception:
            manager = None
    if manager is not None:
        try:
            configs = getattr(manager, "_configs", {}) or {}
            for sid, st in dict(manager.get_all_statuses()).items():
                add(str(sid), str((configs.get(sid) or {}).get("name") or ""),
                    connected=(st or {}).get("status") == "connected")
        except Exception:
            pass
    return list(out.values())


def _match_server(ref: str, candidates: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The server a template reference names, or None.

    Order: an exact id (so built-in ids such as ``penpot_studio`` work), an
    exact name or built-in label ("Built-in: Penpot Studio"), then a name that
    contains the reference when exactly one non-built-in server does (a stock
    server saved as "Penpot MCP" answers ``{server:penpot}``). Among equals a
    connected server wins over a saved one, an enabled one over a disabled one.
    """
    key = ref.strip().casefold()
    if not key:
        return None

    def best(rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        return sorted(rows, key=lambda r: (not r["connected"], not r["enabled"]))[0] if rows else None

    exact = [c for c in candidates if c["id"].casefold() == key]
    if exact:
        return best(exact)
    named = [c for c in candidates
             if c["name"].casefold() == key or c["name"].casefold().removeprefix(_BUILTIN_PREFIX).strip() == key]
    if named:
        # A user's own server named like a built-in is the one they meant.
        own = [c for c in named if not c["builtin"]]
        return best(own or named)
    partial = [c for c in candidates if not c["builtin"] and key in c["name"].casefold()]
    return partial[0] if len(partial) == 1 else None


def resolve_template(doc: Any, manager: Any = None) -> Tuple[Any, List[str]]:
    """Replace ``{server:<name>}`` references in a profile document.

    Returns ``(resolved copy, notes)``; the input is not modified. A reference
    is looked up in ``enabled_tools``, ``disabled_tools`` and
    ``allowed_mcp_servers``. One that resolves is replaced by the server id; one
    that does not has its whole entry dropped, and a note says which server was
    missing and how many entries went with it, so the imported loadout is
    narrower rather than bound to tools that do not exist. A document without
    references comes back unchanged.
    """
    if not isinstance(doc, dict) or not isinstance(doc.get("profiles"), list):
        return doc, []
    if not _SERVER_REF.search(json.dumps(doc, ensure_ascii=False)):
        return doc, []
    candidates = _server_candidates(manager)
    out = copy.deepcopy(doc)
    notes: List[str] = []
    cache: Dict[str, Optional[str]] = {}

    def lookup(ref: str) -> Optional[str]:
        if ref not in cache:
            found = _match_server(ref, candidates)
            cache[ref] = found["id"] if found else None
        return cache[ref]

    for profile in out["profiles"]:
        if not isinstance(profile, dict):
            continue
        label = str(profile.get("name") or "a profile")
        dropped: Dict[str, int] = {}
        for field in _TEMPLATE_FIELDS:
            values = profile.get(field)
            if not isinstance(values, list):
                continue
            kept = []
            for value in values:
                if not isinstance(value, str) or "{server:" not in value:
                    kept.append(value)
                    continue
                missing = [r for r in _SERVER_REF.findall(value) if lookup(r) is None]
                if missing:
                    for ref in missing:
                        dropped[ref] = dropped.get(ref, 0) + 1
                    continue
                kept.append(_SERVER_REF.sub(lambda m: lookup(m.group(1)) or "", value))
            profile[field] = list(dict.fromkeys(kept))
        for ref, count in dropped.items():
            notes.append(f"{label}: no MCP server named {ref!r} is connected or saved here, so "
                         f"{count} entr{'y' if count == 1 else 'ies'} that need it were dropped")
    return out, notes


# ---------------------------------------------------------------------------
# Templates shipped by integrations
# ---------------------------------------------------------------------------

def _template_documents(manager: Any = None) -> List[Dict[str, Any]]:
    from src import integration_registry

    try:
        available = integration_registry.available_ids(manager)
    except Exception:
        available = set()
    rows: List[Dict[str, Any]] = []
    for integration in integration_registry.all():
        if integration.id not in available:
            continue
        for path in integration_registry.loadout_templates(integration.id):
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
                _check_document(doc)
            except (OSError, ValueError):
                continue
            rows.append({"integration": integration.id, "integration_name": integration.name,
                         "template": path.stem, "document": doc})
    return rows


def list_templates(manager: Any = None) -> List[Dict[str, Any]]:
    """Loadout templates of the integrations that are available now.

    Each row: ``integration``, ``integration_name``, ``template`` (the file's
    stem, what :func:`install_template` takes), ``profiles`` (name and
    description of each) and ``installed`` (every profile name already exists).
    """
    existing = {p["name"].casefold() for p in agent_profiles.load_profiles()}
    out = []
    for row in _template_documents(manager):
        profiles = [{"name": str(p.get("name") or ""), "description": str(p.get("description") or "")}
                    for p in row["document"]["profiles"] if isinstance(p, dict)]
        out.append({"integration": row["integration"], "integration_name": row["integration_name"],
                    "template": row["template"], "profiles": profiles,
                    "installed": bool(profiles) and all(p["name"].casefold() in existing for p in profiles)})
    return out


def install_template(integration_id: str, template: str, *, overwrite: bool = False, manager: Any = None,
                     prepare: Optional[Prepare] = None,
                     check_model: Optional[ModelCheck] = None) -> Dict[str, Any]:
    """Import one shipped template through the normal import path.

    The template is found by integration id and file stem among the available
    integrations' templates, never by a client-supplied path. A profile that
    already exists is not overwritten unless ``overwrite`` is set: it may carry
    the owner's edits. Raises ``ValueError`` (unknown template, name conflict).
    """
    for row in _template_documents(manager):
        if row["integration"] == integration_id and row["template"] == template:
            break
    else:
        raise ValueError(f"no loadout template {template!r} for an available integration {integration_id!r}")
    if not overwrite:
        existing = {p["name"].casefold() for p in agent_profiles.load_profiles()}
        clash = [str(p.get("name")) for p in row["document"]["profiles"]
                 if isinstance(p, dict) and str(p.get("name") or "").casefold() in existing]
        if clash:
            raise ValueError(f"a loadout named {', '.join(repr(n) for n in clash)} already exists; "
                             "install again with overwrite to replace it")
    return import_profiles(row["document"], mode="merge", prepare=prepare, check_model=check_model,
                           manager=manager)


def _validate_only(profile: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    return agent_profiles.validate_profiles([profile])[0], []


def import_profiles(doc: Any, mode: str = "merge", rename_conflicts: bool = False, *,
                    prepare: Optional[Prepare] = None,
                    check_model: Optional[ModelCheck] = None,
                    manager: Any = None) -> Dict[str, Any]:
    """Validate ``doc`` and store its profiles; return what happened.

    ``merge`` adds new names and overwrites same-named profiles (or, with
    ``rename_conflicts``, stores the import under a free name instead).
    ``replace`` makes the file the whole profile list, and is all-or-nothing:
    a file with any invalid profile writes nothing rather than deleting the
    existing profiles and keeping only part of the file.

    Raises ``ValueError`` for a document that is not an export this server
    reads (wrong format or version, not a list, too many profiles). A bad
    individual profile is reported under ``errors`` instead.
    """
    mode = str(mode or "merge").strip().lower()
    if mode not in MODES:
        raise ValueError(f"import: mode must be one of {', '.join(MODES)}")
    _check_document(doc)
    # Before validation: the validator would reject "mcp__{server:x}__tool".
    doc, template_notes = resolve_template(doc, manager)
    raw_profiles = _check_document(doc)
    prepare = prepare or _validate_only

    report: Dict[str, Any] = {
        "mode": mode, "added": [], "updated": [], "removed": [], "skipped": [],
        "errors": [], "warnings": list(template_notes), "narrowed": {}, "written": False,
    }
    incoming: List[Dict[str, Any]] = []
    seen = set()
    for index, raw in enumerate(raw_profiles):
        label = str(raw.get("name") or "").strip() if isinstance(raw, dict) else ""
        try:
            profile = agent_profiles.validate_profiles([raw])[0]
            profile, notes = prepare(profile)
        except ValueError as exc:
            report["errors"].append({"index": index, "name": label, "error": str(exc)})
            continue
        key = profile["name"].casefold()
        if key in seen:
            report["errors"].append({"index": index, "name": profile["name"],
                                     "error": "duplicate name in the file"})
            continue
        seen.add(key)
        if notes:
            report["narrowed"][profile["name"]] = list(notes)
        if check_model:
            for spec in [profile.get("model"), *(profile.get("model_fallbacks") or [])]:
                problem = check_model(str(spec)) if spec else None
                if problem:
                    report["warnings"].append(
                        f"{profile['name']}: model {spec!r} is not available here ({problem})")
        incoming.append(profile)

    existing = agent_profiles.load_profiles()
    existing_keys = {p["name"].casefold() for p in existing}

    if mode == "replace":
        if report["errors"]:
            report["skipped"] = [{"name": p["name"], "reason": "replace writes nothing while the file has errors"}
                                 for p in incoming]
            return report
        final = incoming
        incoming_keys = {p["name"].casefold() for p in incoming}
        report["added"] = [p["name"] for p in incoming if p["name"].casefold() not in existing_keys]
        report["updated"] = [p["name"] for p in incoming if p["name"].casefold() in existing_keys]
        report["removed"] = [p["name"] for p in existing if p["name"].casefold() not in incoming_keys]
    else:
        final = list(existing)
        index_of = {p["name"].casefold(): i for i, p in enumerate(final)}
        for profile in incoming:
            key = profile["name"].casefold()
            if key in index_of and not rename_conflicts:
                final[index_of[key]] = profile
                report["updated"].append(profile["name"])
                continue
            if key in index_of:
                new_name = _free_name(profile["name"], [p["name"] for p in final])
                if new_name is None:
                    report["skipped"].append({"name": profile["name"], "reason": "no free name to rename it to"})
                    continue
                if profile["name"] in report["narrowed"]:
                    report["narrowed"][new_name] = report["narrowed"].pop(profile["name"])
                profile = {**profile, "name": new_name}
            if len(final) >= agent_profiles.MAX_PROFILES:
                report["skipped"].append({"name": profile["name"],
                                          "reason": f"at most {agent_profiles.MAX_PROFILES} profiles"})
                continue
            index_of[profile["name"].casefold()] = len(final)
            final.append(profile)
            report["added"].append(profile["name"])

    if report["added"] or report["updated"] or report["removed"]:
        agent_loadouts._write(final)
        report["written"] = True
    return report


def summary_line(report: Dict[str, Any]) -> str:
    """One human-readable sentence for a report."""
    parts = []
    for key in ("added", "updated", "removed"):
        if report.get(key):
            parts.append(f"{len(report[key])} {key}")
    for key in ("skipped", "errors"):
        if report.get(key):
            parts.append(f"{len(report[key])} {key if key != 'errors' else 'failed'}")
    text = ", ".join(parts) or "nothing to import"
    if not report.get("written"):
        text += "; nothing was saved"
    return text[:1].upper() + text[1:]
