"""Export and import agent profiles (loadouts) as a portable JSON document.

The document is deliberately small and versioned::

    {"format": "odysseus-agent-profiles", "version": 1,
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
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from src import agent_loadouts, agent_profiles

FORMAT = "odysseus-agent-profiles"
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
    if doc.get("format") != FORMAT:
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


def _validate_only(profile: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    return agent_profiles.validate_profiles([profile])[0], []


def import_profiles(doc: Any, mode: str = "merge", rename_conflicts: bool = False, *,
                    prepare: Optional[Prepare] = None,
                    check_model: Optional[ModelCheck] = None) -> Dict[str, Any]:
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
    raw_profiles = _check_document(doc)
    prepare = prepare or _validate_only

    report: Dict[str, Any] = {
        "mode": mode, "added": [], "updated": [], "removed": [], "skipped": [],
        "errors": [], "warnings": [], "narrowed": {}, "written": False,
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
