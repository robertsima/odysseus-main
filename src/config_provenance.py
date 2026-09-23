"""Where an effective configuration value actually came from.

Odysseus reads configuration out of five places and no caller can tell them
apart from the value alone:

1. a ``ODYSSEUS_*`` environment variable that *pins* a setting
   (``settings_schema.env_locked``);
2. an explicit entry in ``data/settings.json``
   (``settings.is_setting_overridden``);
3. a per-user override in ``data/user_prefs.json`` — only for the small
   whitelist in ``settings._PER_USER_KEYS``;
4. a legacy environment variable kept as a one-way migration fallback
   (``settings.get_setting_or_env`` / ``settings_schema._MIGRATED_ENV_OVERRIDES``);
5. the code default in ``settings.DEFAULT_SETTINGS``.

"The model is X" is unanswerable operationally; "the model is X because *your*
user prefs say so, while the global setting says Y and a container pinned
neither" is. ``docs/configuration.md`` documents the two systems that drifted
apart; this module is the runtime answer to *which one won*, computed by
calling the same readers the app calls rather than by re-deriving the rule.

Secrets
-------
Settings hold API keys. This module NEVER returns a secret value. A key is
classified as secret by name shape and by its schema entry, and a secret key is
reported as ``set`` / ``not set`` with the value replaced by
:data:`REDACTED`. Everything else is additionally passed through
``src.agent_logs.redact_line`` (the same scrubber the log-reading tool uses)
so an ``https://user:pass@host`` or a bearer token that ended up inside an
innocuous-looking value is removed too. Redaction is applied on the way out,
once, in :func:`safe_value`, and every reader in this module goes through it.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: What the caller sees instead of a credential. Deliberately not the empty
#: string: "redacted" and "unset" are different facts and an operator chasing a
#: broken integration needs to tell them apart.
REDACTED = "<redacted>"

#: Source labels, most authoritative first. The order is the resolution order
#: the readers implement, so `sources[0]` of a provenance record is the layer
#: that produced the effective value.
SOURCE_ENV_LOCK = "env_lock"          # deployment pinned it; the UI shows it read-only
SOURCE_USER_PREFS = "user_prefs"      # this user's own override
SOURCE_SETTINGS_FILE = "settings_file"  # explicit entry in data/settings.json
SOURCE_ENV_LEGACY = "env_legacy"      # migration fallback, only while unsaved
SOURCE_CODE_DEFAULT = "code_default"  # settings.DEFAULT_SETTINGS

_SECRET_NAME_PARTS = ("api_key", "_key", "secret", "password", "passwd", "credential")


def is_secret_key(key: str) -> bool:
    """True when a settings key names credential material.

    Shared with ``src.agent_tools.admin_tools`` so the read side and the write
    side cannot disagree about what a credential is — a mismatch there means
    either a key the agent may write but not read back, or worse.

    ``token`` must be a *suffix*, not a substring, or the int setting
    ``agent_input_token_budget`` reads as a credential.
    """
    k = (key or "").strip().lower()
    if not k:
        return False
    if k.endswith("token") or any(part in k for part in _SECRET_NAME_PARTS):
        return True
    try:
        from src.settings_schema import get_spec

        spec = get_spec(k)
        if spec is not None and (spec.sensitive or spec.type == "secret"):
            return True
    except Exception:
        logger.debug("settings schema unreadable while classifying %r", key, exc_info=True)
    return False


def scrub_text(text: Any, *, limit: int = 4000) -> str:
    """Defence in depth for any free text this module emits.

    The field allowlists above are the real guarantee; this catches a
    credential that arrived inside a value nobody expected to hold one (a
    ``base_url`` with userinfo, an ``Authorization:`` header pasted into a
    prompt). Same scrubber as the log-reading tool: one definition, imported.
    """
    if text is None:
        return ""
    raw = text if isinstance(text, str) else str(text)
    try:
        from src.agent_logs import redact_text

        # `redact_text`, not `redact_line`: the line variant clips at
        # MAX_LINE_CHARS, and clipping the thing you are redacting is data
        # loss, not redaction. `limit` below is the only length policy here.
        cleaned = redact_text(raw)
    except Exception:
        logger.debug("redaction unavailable; withholding text", exc_info=True)
        # Fails closed: a scrubber we cannot run means we do not know the text
        # is safe, and this is a policy decision (docs/design-patterns.md).
        return REDACTED
    if limit and len(cleaned) > limit:
        return cleaned[:limit] + f"… (+{len(cleaned) - limit} chars)"
    return cleaned


def safe_value(key: str, value: Any) -> Any:
    """The value as it may be shown. Secrets never survive this function."""
    if is_secret_key(key):
        return REDACTED if value not in (None, "", [], {}) else None
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, dict):
        return {str(k): safe_value(str(k), v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe_value(key, v) for v in value]
    return value


def _env_name_for(key: str) -> tuple[str, bool]:
    """(env var name, is_a_hard_lock) for a settings key, ("" , False) if none."""
    try:
        from src.settings_schema import _MIGRATED_ENV_OVERRIDES, env_locked, get_spec

        spec = get_spec(key)
        if spec is not None and spec.env_override:
            return spec.env_override, env_locked(spec)
        migrated = _MIGRATED_ENV_OVERRIDES.get(key)
        if migrated:
            return migrated, False
    except Exception:
        logger.debug("env override lookup failed for %r", key, exc_info=True)
    return "", False


def _user_pref_value(key: str, owner: str):
    """(present, value) for this user's own override of `key`."""
    try:
        from src.settings import _PER_USER_KEYS
    except Exception:
        return False, None
    if not owner or key not in _PER_USER_KEYS:
        return False, None
    try:
        from routes.prefs_routes import _load_for_user

        prefs = _load_for_user(owner) or {}
    except Exception:
        logger.debug("user prefs unreadable for %r", owner, exc_info=True)
        return False, None
    if key in prefs and prefs[key] not in (None, ""):
        return True, prefs[key]
    return False, None


def provenance(key: str, owner: str = "") -> Dict[str, Any]:
    """Where the effective value of one setting came from.

    The effective value is obtained by calling the app's own readers, not by
    re-implementing precedence: a report that disagrees with the running code
    is worse than no report. ``sources`` lists every layer that holds a value
    for this key, most authoritative first; ``source`` is the one that won.
    """
    from src.settings import (
        DEFAULT_SETTINGS,
        get_setting,
        get_setting_or_env,
        get_user_setting,
        is_setting_overridden,
    )

    key = (key or "").strip()
    known = key in DEFAULT_SETTINGS
    env_name, env_lock = _env_name_for(key)
    env_present = bool(env_name and str(os.environ.get(env_name, "") or "").strip())
    saved = is_setting_overridden(key)
    pref_present, pref_value = _user_pref_value(key, owner)

    # Resolve through the reader that actually governs this key.
    if pref_present:
        effective = get_user_setting(key, owner, DEFAULT_SETTINGS.get(key))
    elif env_name:
        effective = get_setting_or_env(key, env_name, DEFAULT_SETTINGS.get(key))
    else:
        effective = get_setting(key, DEFAULT_SETTINGS.get(key))

    sources: List[str] = []
    if env_present and env_lock:
        sources.append(SOURCE_ENV_LOCK)
    if pref_present:
        sources.append(SOURCE_USER_PREFS)
    if saved:
        sources.append(SOURCE_SETTINGS_FILE)
    if env_present and not env_lock:
        sources.append(SOURCE_ENV_LEGACY)
    if known:
        sources.append(SOURCE_CODE_DEFAULT)

    # Which layer produced the value the app is using. env_lock wins outright;
    # otherwise a per-user override beats the file, the file beats the legacy
    # env fallback (get_setting_or_env returns early when the key is saved),
    # and the default is what is left.
    if env_present and env_lock:
        winner = SOURCE_ENV_LOCK
    elif pref_present:
        winner = SOURCE_USER_PREFS
    elif saved:
        winner = SOURCE_SETTINGS_FILE
    elif env_present:
        winner = SOURCE_ENV_LEGACY
    else:
        winner = SOURCE_CODE_DEFAULT

    record: Dict[str, Any] = {
        "key": key,
        "known": known,
        "value": safe_value(key, effective),
        "secret": is_secret_key(key),
        "source": winner if known or sources else "unknown",
        "sources": sources,
        "env_var": env_name or None,
        "env_set": env_present,
        "env_pins_the_value": bool(env_present and env_lock),
        "in_settings_file": saved,
        "per_user_override": pref_present,
        "default": safe_value(key, DEFAULT_SETTINGS.get(key)) if known else None,
        "differs_from_default": (
            bool(known and effective != DEFAULT_SETTINGS.get(key))
        ),
    }
    if is_secret_key(key):
        # The one fact about a credential worth reporting.
        record["is_set"] = effective not in (None, "", [], {})
    return record


def report(keys: Optional[List[str]] = None, owner: str = "",
           *, only_non_default: bool = False) -> Dict[str, Any]:
    """Provenance for many keys at once.

    With no ``keys`` this covers every key in ``DEFAULT_SETTINGS``, which is
    the "what is this install actually configured with" question. Secrets are
    listed — with their values redacted — rather than omitted, because a
    missing key reads as "not a thing" when the real answer is "set, and I am
    not showing you".
    """
    from src.settings import DEFAULT_SETTINGS

    wanted = [k.strip() for k in (keys or sorted(DEFAULT_SETTINGS)) if (k or "").strip()]
    records = [provenance(k, owner) for k in wanted]
    if only_non_default:
        records = [r for r in records if r["source"] != SOURCE_CODE_DEFAULT]
    counts: Dict[str, int] = {}
    for rec in records:
        counts[rec["source"]] = counts.get(rec["source"], 0) + 1
    return {
        "owner": owner or None,
        "count": len(records),
        "by_source": counts,
        "settings": records,
        "note": (
            "settings.json is global on a multi-user install: a value whose "
            "source is settings_file is shared by every user "
            "(docs/architecture-runtime.md §4)."
        ),
    }
