"""Declarative schema for user-facing settings.

Two rules decide where a piece of configuration lives, and the split had drifted
badly — 121 ``ODYSSEUS_*`` environment variables against 83 settings keys, only
66 of which had a control anywhere in the UI:

* **settings.json** holds *choices* — anything a person might reasonably want
  different, on any host. These get a schema entry here and therefore a control.
* **environment** holds *placement* — what changes when the same software runs
  somewhere else: mount paths, ports, service URLs, secrets. No UI, because
  editing it in the app would be editing the deployment.

A setting with no schema entry has no control, which is how the UI fell 17 keys
behind. So the schema is the source of truth for the admin UI rather than a
parallel description of it, and ``tests/test_settings_schema.py`` fails when a
key in ``DEFAULT_SETTINGS`` has no entry here. Adding a setting without a control
is now a test failure rather than something noticed months later.

``env_override`` names a variable a deployment may still use to force a value.
That is deliberate: a container image wants to pin the data directory without a
human clicking anything, and the UI shows such a setting as locked rather than
pretending the click will stick.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# Coarse types, chosen for what the UI must render rather than for Python.
TYPES = ("bool", "int", "float", "string", "text", "choice", "list", "json", "path", "secret")


@dataclass(frozen=True)
class SettingSpec:
    key: str
    type: str
    label: str
    help: str = ""
    group: str = "General"
    # Only an admin may change it. Everything touching the host, another
    # machine, or other users' data is admin-only.
    admin_only: bool = True
    # Eligible for a per-user override. Derived from settings._PER_USER_KEYS
    # when the spec is registered, so it cannot drift from the resolver.
    per_user: bool = False
    # The capability this belongs to; the UI hides the group when unavailable.
    capability: str = ""
    choices: Tuple[str, ...] = ()
    # Optional display labels matching ``choices`` by position. Values sent to
    # the server remain the stable machine names above.
    choice_labels: Tuple[str, ...] = ()
    # Populate a select from the live installation rather than asking the user
    # to type an opaque endpoint/model identifier. Supported sources are
    # interpreted by capabilitiesPanel.js.
    options_source: str = ""
    # Suggested finite values for a setting that must still accept custom text
    # (for example provider-defined voices or ISO language codes).
    suggestions: Tuple[str, ...] = ()
    # Deployment may pin this; the UI renders it read-only when the var is set.
    env_override: str = ""
    # Never echo the stored value back to the browser.
    sensitive: bool = False
    placeholder: str = ""
    # Presentation and generic validation metadata. A scale of 1_048_576, for
    # example, lets the UI show a byte value as human-sized MiB without changing
    # what is stored in settings.json.
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    step: Optional[float] = None
    unit: str = ""
    scale: float = 1.0
    advanced: bool = False
    # Declared (so the completeness test holds and saves still validate) but
    # left out of the generic Configuration panel: a dormant feature, or one
    # whose only control lives in its own tab. The backend still reads it.
    hidden: bool = False

    def as_dict(self, value: Any = None, locked: bool = False, default: Any = None) -> dict:
        out = {
            "key": self.key,
            "type": self.type,
            "label": self.label,
            "help": self.help,
            "group": self.group,
            "admin_only": self.admin_only,
            "per_user": self.per_user,
            "capability": self.capability,
            "choices": list(self.choices),
            "choice_labels": list(self.choice_labels),
            "options_source": self.options_source,
            "suggestions": list(self.suggestions),
            "locked": locked,
            "placeholder": self.placeholder,
            "default": "" if self.sensitive else default,
            "min": self.min_value,
            "max": self.max_value,
            "step": self.step,
            "unit": self.unit,
            "scale": self.scale,
            "advanced": self.advanced,
        }
        if not self.sensitive:
            out["value"] = value
        else:
            out["value"] = "" if value in (None, "") else "********"
            out["sensitive"] = True
        return out


_SPECS: Dict[str, SettingSpec] = {}


def register(spec: SettingSpec) -> SettingSpec:
    # `per_user` follows the resolver rather than the declaration. On
    # 2026-09-28 the hand-set flags were wrong both ways: search_provider,
    # search_safesearch and reminder_channel claimed a per-user override that
    # get_user_setting never consults, while the default/utility/research/
    # vision/image/STT model specs lacked the flag although their keys are
    # resolved per user.
    try:
        from src.settings import _PER_USER_KEYS

        per_user = spec.key in _PER_USER_KEYS
    except Exception:  # pragma: no cover - settings unimportable at boot
        per_user = spec.per_user
    if per_user != spec.per_user:
        spec = replace(spec, per_user=per_user)
    _SPECS[spec.key] = spec
    return spec


def register_all(specs: Sequence[SettingSpec]) -> None:
    for spec in specs:
        register(spec)


def get_spec(key: str) -> Optional[SettingSpec]:
    return _SPECS.get(key)


def all_specs() -> Tuple[SettingSpec, ...]:
    return tuple(_SPECS[k] for k in sorted(_SPECS))


def known_keys() -> frozenset[str]:
    return frozenset(_SPECS)


def missing_specs() -> List[str]:
    """DEFAULT_SETTINGS keys with no schema entry — i.e. no UI control.

    The completeness test asserts this is empty. Keys that are structured
    editor state rather than a single control are listed in EXEMPT below.
    """
    from src.settings import DEFAULT_SETTINGS, RETIRED_SETTING_KEYS

    return sorted(
        k for k in DEFAULT_SETTINGS
        if k not in _SPECS and k not in EXEMPT and k not in RETIRED_SETTING_KEYS
    )


# Keys that are edited through a dedicated UI of their own rather than a
# generic control, so a schema entry would describe a form that does not exist.
EXEMPT: frozenset[str] = frozenset({
    "agent_profiles",      # Settings › Workbench › Agent profiles
    "context_profiles",    # Settings › Context profiles
    "keybinds",            # Settings › Keyboard shortcuts
})

# Choice controls that used to be deployment-file knobs. Keep the old names
# visible as compatibility overrides in the UI while the saved Settings value
# remains the canonical control for normal installs.
_MIGRATED_ENV_OVERRIDES = {
    "agent_approval_ttl_seconds": "ODYSSEUS_AGENT_APPROVAL_TTL_SECONDS",
    "agent_base_branch": "ODYSSEUS_AGENT_BASE_BRANCH",
    "browser_isolated": "ODYSSEUS_BROWSER_ISOLATED",
    "chat_upload_max_bytes": "ODYSSEUS_CHAT_UPLOAD_MAX_BYTES",
    "email_compose_upload_max_bytes": "ODYSSEUS_EMAIL_COMPOSE_UPLOAD_MAX_BYTES",
    "gallery_upload_max_bytes": "ODYSSEUS_GALLERY_UPLOAD_MAX_BYTES",
    "gallery_transform_upload_max_bytes": "ODYSSEUS_GALLERY_TRANSFORM_UPLOAD_MAX_BYTES",
    "ics_import_max_bytes": "ODYSSEUS_ICS_MAX_BYTES",
    "imap_timeout_seconds": "ODYSSEUS_IMAP_TIMEOUT_SECONDS",
    "memory_import_max_bytes": "ODYSSEUS_MEMORY_IMPORT_MAX_BYTES",
    "mistral_reasoning_effort": "ODYSSEUS_MISTRAL_REASONING_EFFORT",
    "model_keepalive_enabled": "ODYSSEUS_MODEL_KEEPALIVE",
    "personal_upload_max_bytes": "ODYSSEUS_PERSONAL_UPLOAD_MAX_BYTES",
    "rag_focused_cap_multiplier": "ODYSSEUS_RAG_FOCUSED_CAP_MULTIPLIER",
    "rag_link_expansion": "ODYSSEUS_RAG_LINK_EXPANSION",
    "rag_max_chunks_per_doc": "ODYSSEUS_RAG_MAX_CHUNKS_PER_DOC",
    "rag_recency_halflife_days": "ODYSSEUS_RAG_RECENCY_HALFLIFE_DAYS",
    "rag_tag_credit": "ODYSSEUS_RAG_TAG_CREDIT",
    "rag_temporal_intent_weight": "ODYSSEUS_RAG_TEMPORAL_INTENT_WEIGHT",
    "rag_temporal_weight": "ODYSSEUS_RAG_TEMPORAL_WEIGHT",
    "stt_beam_size": "ODYSSEUS_STT_BEAM_SIZE",
    "stt_max_audio_bytes": "ODYSSEUS_STT_MAX_AUDIO_BYTES",
    "stt_max_audio_seconds": "ODYSSEUS_STT_MAX_AUDIO_SECONDS",
    "startup_warmups_enabled": "ODYSSEUS_STARTUP_WARMUPS",
    "tts_cache_max_bytes": "ODYSSEUS_TTS_CACHE_MAX_BYTES",
    "vault_date_order": "ODYSSEUS_VAULT_DATE_ORDER",
    "vault_scan_seconds": "ODYSSEUS_VAULT_SCAN_SECONDS",
    "gallery_sam_model": "ODYSSEUS_SAM_MODEL",
    "gallery_grounding_model": "ODYSSEUS_GROUNDING_MODEL",
}


def env_locked(spec: SettingSpec) -> bool:
    import os

    # These names are retained only as a one-way migration fallback. A saved
    # Settings value deliberately wins, so showing the control as deployment-
    # locked would be misleading and would prevent the user from completing
    # the migration in the UI.
    if spec.env_override in _MIGRATED_ENV_OVERRIDES.values():
        return False
    return bool(spec.env_override and str(os.environ.get(spec.env_override, "") or "").strip())


# ── write-time validation ─────────────────────────────────────────────────
# Some settings are security policy, and their readers are deliberately
# fail-closed: `rag_sensitivity` discards a malformed `vault_folder_sensitivity`
# wholesale and treats everything as private. That is the right read-time
# behaviour, but on its own it means one typo silently turns the whole vault
# private and the operator experiences "search stopped working" with the reason
# only in a log line. Validating on write makes that state nearly unreachable and
# reports exactly which entry is wrong, while the fail-closed reader stays as the
# backstop for a hand-edited settings.json.


def _validate_folder_sensitivity(value: Any) -> None:
    if not isinstance(value, dict):
        raise ValueError("must be an object mapping folder paths to privacy/access rules")
    normalized_keys: set[str] = set()
    for key, rule in value.items():
        if not isinstance(key, str):
            raise ValueError(f"every folder path must be text (got {key!r})")
        cleaned = key.replace("\\", "/").strip()
        is_root = key == ""
        if ((not cleaned and not is_root) or cleaned.startswith("/")
                or ".." in cleaned.split("/") or ":" in cleaned):
            raise ValueError(
                f"{key!r} must be a folder inside the vault — no absolute paths, drive letters or '..'"
            )
        parts = [part for part in cleaned.split("/") if part not in ("", ".")]
        normalized = "/".join(parts).casefold()
        if normalized in normalized_keys:
            raise ValueError(f"{key!r} duplicates another folder rule after normalization")
        normalized_keys.add(normalized)
        if isinstance(rule, str):
            if rule.strip().lower() not in {"public", "private", "readonly"}:
                raise ValueError(
                    f"{key!r} has rule {rule!r}; expected \"public\", \"private\" or \"readonly\""
                )
            continue
        if not isinstance(rule, dict):
            raise ValueError(f"{key!r} must be a text rule or an object")
        unknown = set(rule) - {"sensitivity", "readonly"}
        if unknown:
            raise ValueError(f"{key!r} has unknown option(s): {', '.join(sorted(map(str, unknown)))}")
        if not rule:
            raise ValueError(f"{key!r} has an empty rule")
        if "sensitivity" in rule:
            sensitivity = rule["sensitivity"]
            if not isinstance(sensitivity, str) or sensitivity.strip().lower() not in {"public", "private"}:
                raise ValueError(f"{key!r}.sensitivity must be \"public\" or \"private\"")
        if "readonly" in rule and not isinstance(rule["readonly"], bool):
            raise ValueError(f"{key!r}.readonly must be true or false")


def _validate_model_fallbacks(value: Any) -> None:
    if not isinstance(value, list):
        raise ValueError("must be a list of {\"endpoint_id\": ..., \"model\": ...} objects")
    for index, entry in enumerate(value):
        if not isinstance(entry, dict):
            raise ValueError(f"entry {index + 1} must be an object with endpoint_id and model")
        if not all(isinstance(entry.get(field, ""), str) for field in ("endpoint_id", "model")):
            raise ValueError(f"entry {index + 1}: endpoint_id and model must be text")


VALIDATORS: Dict[str, Any] = {
    "vault_folder_sensitivity": _validate_folder_sensitivity,
    "vision_model_fallbacks": _validate_model_fallbacks,
    "utility_model_fallbacks": _validate_model_fallbacks,
}


def validate_value(key: str, value: Any) -> None:
    """Raise ``ValueError`` with an operator-readable reason if ``value`` is unusable."""
    spec = get_spec(key)
    if spec and spec.type in {"int", "float"}:
        if spec.min_value is not None and value < spec.min_value:
            raise ValueError(f"must be at least {spec.min_value:g}")
        if spec.max_value is not None and value > spec.max_value:
            raise ValueError(f"must be no more than {spec.max_value:g}")
    check = VALIDATORS.get(key)
    if check is not None:
        check(value)


# ── write-time normalisation shared by both settings routes ───────────────
# Until 2026-09-28 the two write paths disagreed. POST /api/auth/settings (the
# hand-built Settings tabs) checked paths, repository slugs, enums and ranges
# key by key but never the schema's validators or choice lists, so a malformed
# `vault_folder_sensitivity` saved from a tab slipped past the check written to
# catch it. POST /api/settings/schema (the Configuration panel) checked the
# schema and nothing else, so a gpt-* model saved there as `claude_code_model`
# broke every delegation with invalid_model. One function now holds the rules;
# the routes differ only in what they do with an out-of-range number.

_GITHUB_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}")
_WORKFLOW_FILE = re.compile(r"[A-Za-z0-9._-]{1,100}\.ya?ml")
_ANY_REPOSITORY = "*"
# Search provider names that older builds (and the Research tab until
# 2026-09-28) stored, mapped to the name the dispatcher knows. "google" matched
# no provider, so Deep Research silently fell back to the default engine.
# services/search/core.normalize_provider_name applies the same map on read.
_SEARCH_PROVIDER_ALIASES = {"google": "google_pse"}
# Per choice setting: stored spellings accepted and saved as the declared one.
CHOICE_ALIASES: Dict[str, Dict[str, str]] = {
    "research_search_provider": _SEARCH_PROVIDER_ALIASES,
}
# "No fallback at all" for search_fallback_chain; an empty chain keeps meaning
# "the built-in default chain" (services/search/core._build_provider_chain).
SEARCH_FALLBACK_NONE = "none"


def _github_slug(value: Any) -> str:
    slug = str(value or "").strip().strip("/")
    if slug.lower().startswith("https://github.com/"):
        slug = slug[len("https://github.com/"):].removesuffix(".git").strip("/")
    return slug


def _norm_repository_roots(value: Any) -> List[str]:
    if not isinstance(value, list):
        raise ValueError("must be a list of absolute paths")
    cleaned = []
    for item in value:
        item = str(item or "").strip()
        if not item:
            continue
        if not os.path.isabs(item):
            raise ValueError(f"{item!r} is not an absolute path")
        cleaned.append(os.path.normpath(item))
    return cleaned


def _norm_absolute_path(value: Any) -> str:
    # Absolute paths only (or empty = unset), so a settings write cannot point
    # the delegation at a relative or shell-expanded location.
    text = str(value or "").strip()
    if text and not os.path.isabs(os.path.expanduser(text)):
        raise ValueError("must be an absolute path (or empty)")
    return text


def _norm_http_url(value: Any) -> str:
    text = str(value or "").strip()
    if text and not text.lower().startswith(("http://", "https://")):
        raise ValueError("must be an http(s) URL (or empty)")
    return text.rstrip("/")


def _norm_claude_code_model(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    # Same rules the delegation applies, so a saved model never fails later at
    # run time ("Opus 5.5" is stored as claude-opus-5-5, "default" as unset,
    # and a gpt-* name is refused here instead of on every delegation).
    from src.agent_tools.claude_code_tools import normalize_claude_model

    normalized, error = normalize_claude_model(text)
    if error:
        raise ValueError(error)
    return normalized or ""


def _norm_reasoning_effort(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text and text not in ("minimal", "low", "medium", "high"):
        raise ValueError("must be minimal, low, medium or high (or empty)")
    return text


def _norm_claude_code_backend(value: Any) -> str:
    text = str(value or "local").strip().lower()
    if text not in ("local", "cloud"):
        raise ValueError("must be local or cloud")
    return text


def _split_names(value: Any) -> List[str]:
    """A list, or text separated by commas/whitespace (one per line from a
    textarea, "a, b" typed into one line); names never contain either."""
    items = value if isinstance(value, (list, tuple)) else [value]
    return [part for item in items for part in re.split(r"[\s,]+", str(item or "")) if part]


def _norm_cloud_repositories(value: Any) -> List[str]:
    items = _split_names(value)
    cleaned: List[str] = []
    for item in items:
        if str(item or "").strip().strip("/") == _ANY_REPOSITORY:
            # Any repository the GitHub credential can see.
            if _ANY_REPOSITORY not in cleaned:
                cleaned.append(_ANY_REPOSITORY)
            continue
        slug = _github_slug(item)
        if not slug:
            continue
        if not _GITHUB_SLUG.fullmatch(slug):
            raise ValueError(f"{slug!r} is not an owner/repo slug")
        if slug.lower() not in {c.lower() for c in cleaned}:
            cleaned.append(slug)
    return cleaned[:50]


def _norm_cloud_hub(value: Any) -> str:
    slug = _github_slug(value)
    if slug and not _GITHUB_SLUG.fullmatch(slug):
        raise ValueError(f"{slug!r} is not an owner/repo slug")
    return slug


def _norm_cloud_workflow(value: Any) -> str:
    text = str(value or "").strip() or "odysseus-claude.yml"
    if not _WORKFLOW_FILE.fullmatch(text):
        raise ValueError("must be a workflow file name like odysseus-claude.yml")
    return text


def _norm_approval_mode(value: Any) -> Any:
    from src.approval_modes import MODES

    if value not in MODES:
        raise ValueError(f"must be one of {', '.join(MODES)}")
    return value


def _norm_agent_profiles(value: Any) -> Any:
    from src.agent_profiles import validate_profiles

    return validate_profiles(value)


def _norm_context_profiles(value: Any) -> Any:
    # A preferences blob, not a scalar: clamp what is out of range and drop
    # what is unknown rather than refusing the whole save and costing the user
    # every other field on the form.
    from src.context_profiles import sanitize

    return sanitize(value)


def _norm_search_provider_name(value: Any) -> str:
    text = str(value or "").strip().lower()
    return _SEARCH_PROVIDER_ALIASES.get(text, text)


def _norm_search_fallback_chain(value: Any) -> List[str]:
    cleaned: List[str] = []
    for item in _split_names(value):
        name = _norm_search_provider_name(item)
        if name and name not in cleaned:
            cleaned.append(name)
    # "none" cannot share a chain with a provider: it means there is no chain.
    return [SEARCH_FALLBACK_NONE] if SEARCH_FALLBACK_NONE in cleaned else cleaned


# Per-key rules that go beyond a type and a range. Each returns the value to
# store or raises ValueError with an operator-readable reason.
NORMALIZERS: Dict[str, Callable[[Any], Any]] = {
    "claude_code_repository_roots": _norm_repository_roots,
    "claude_code_binary": _norm_absolute_path,
    "claude_code_home": _norm_absolute_path,
    "claude_code_default_repository": _norm_absolute_path,
    "claude_code_odysseus_token_file": _norm_absolute_path,
    "claude_code_odysseus_url": _norm_http_url,
    "claude_code_model": _norm_claude_code_model,
    "chatgpt_reasoning_effort": _norm_reasoning_effort,
    "claude_code_backend": _norm_claude_code_backend,
    "claude_cloud_repositories": _norm_cloud_repositories,
    "claude_cloud_hub_repository": _norm_cloud_hub,
    "claude_cloud_workflow": _norm_cloud_workflow,
    "agent_approval_mode": _norm_approval_mode,
    "agent_profiles": _norm_agent_profiles,
    "context_profiles": _norm_context_profiles,
    "research_search_provider": _norm_search_provider_name,
    "search_fallback_chain": _norm_search_fallback_chain,
}


def _coerce_declared_type(spec: SettingSpec, value: Any) -> Any:
    """Bring a JSON value to the declared type, as far as that is unambiguous."""
    kind = spec.type
    if kind == "bool":
        if isinstance(value, str):
            # bool("false") is True: getting this wrong turns a switched-off
            # feature back on.
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if kind in ("int", "float"):
        if value is None or (isinstance(value, str) and not value.strip()):
            # A cleared field means "back to the default", not a crash later.
            from src.settings import DEFAULT_SETTINGS

            value = DEFAULT_SETTINGS.get(spec.key, 0)
        try:
            return int(value) if kind == "int" else float(value)
        except (TypeError, ValueError):
            raise ValueError("must be an integer" if kind == "int" else "must be a number")
    if kind in ("string", "text", "path", "secret") and value is None:
        return ""
    return value


def normalize_value(key: str, value: Any, *, clamp: bool = False) -> Any:
    """The value to store for ``key``, or ``ValueError`` saying what is wrong.

    Applies, in order: the key's own rule (``NORMALIZERS``), the declared type,
    the numeric range, the choice list, then ``validate_value``. With ``clamp``
    an out-of-range number is pulled to the nearest bound instead of refused —
    the Settings tabs have always clamped, and their inputs mirror it.
    """
    rule = NORMALIZERS.get(key)
    if rule is not None:
        value = rule(value)
    spec = get_spec(key)
    if spec is not None:
        if rule is None:
            value = _coerce_declared_type(spec, value)
        if clamp and spec.type in ("int", "float"):
            if spec.min_value is not None and value < spec.min_value:
                value = type(value)(spec.min_value)
            if spec.max_value is not None and value > spec.max_value:
                value = type(value)(spec.max_value)
        if spec.type == "choice" and spec.choices:
            text = str(value).strip()
            text = CHOICE_ALIASES.get(key, {}).get(text, text)
            if text not in spec.choices:
                raise ValueError(f"must be one of: {', '.join(repr(c) for c in spec.choices)}")
            value = text
    validate_value(key, value)
    return value


def ui_payload(owner: str = "", is_admin: bool = True) -> List[dict]:
    """Everything the settings UI needs to render, grouped, in one payload.

    Non-admins see only the settings they may change, so the page does not show
    controls that will be refused on save. Hidden specs are left out.

    ``value`` is always the global setting, because that is what a save from
    the panel writes (as the dedicated Settings tabs do). Until 2026-09-28 a
    per-user key showed the admin's own override here instead, so the panel
    displayed one value and saved another. A per-user key now also carries
    ``user_value``: the viewer's own override, or None when they have none.
    """
    from src.settings import DEFAULT_SETTINGS, get_setting

    personal: Dict[str, Any] = {}
    if owner:
        try:
            from routes.prefs_routes import _load_for_user

            personal = _load_for_user(owner) or {}
        except Exception:
            personal = {}

    groups: Dict[str, List[dict]] = {}
    for spec in all_specs():
        if spec.hidden:
            continue
        if spec.admin_only and not is_admin:
            continue
        if spec.capability:
            try:
                from src import capabilities

                st = capabilities.status(spec.capability)
                if st is not None and not st.enabled:
                    continue
            except Exception:
                pass
        item = spec.as_dict(
            get_setting(spec.key),
            locked=env_locked(spec),
            default=DEFAULT_SETTINGS.get(spec.key),
        )
        if spec.per_user:
            override = personal.get(spec.key)
            item["user_value"] = None if override in (None, "") else override
        groups.setdefault(spec.group, []).append(item)

    group_help = {
        "Agents": "Delegation, approvals, collaboration, and agent runtime limits.",
        "Knowledge": "Markdown notes, retrieval behavior, and vault privacy.",
        "Models": "Model behavior shared across chats and background work.",
        "Limits & uploads": "File-size and storage limits. Values are shown in MiB.",
        "System": "Startup, diagnostics, and host resource behavior.",
        "Voice": "Speech recognition, speech output, and their resource limits.",
        "Images": "Image generation, vision, gallery analysis, and model choices.",
        "Search": "Web-search providers, filtering, and result behavior.",
    }
    order = (
        "General", "Agents", "Knowledge", "Models", "Voice", "Images",
        "Search", "Research", "Reminders", "Email", "Chat", "Documents",
        "Skills", "Tasks", "Teacher", "Tools",
        "Limits & uploads", "System",
    )
    rank = {name: i for i, name in enumerate(order)}
    names = sorted(groups, key=lambda name: (rank.get(name, len(rank)), name.lower()))
    return [
        {"group": name, "help": group_help.get(name, ""), "settings": groups[name]}
        for name in names
    ]


# ── the schema ────────────────────────────────────────────────────────────
# Grouped as the operator thinks about them, not as the code is organised.

register_all([
    # ── Installed endpoints and models ──
    *[
        SettingSpec(key=key, type="string", label=label, help=help_text,
                    group=group, options_source="endpoints")
        for key, label, help_text, group in (
            ("default_endpoint_id", "Default endpoint", "Endpoint used for new chats unless a chat chooses another.", "Models"),
            ("utility_endpoint_id", "Utility endpoint", "Endpoint used for lightweight summaries and utility calls.", "Models"),
            ("research_endpoint_id", "Research endpoint", "Endpoint used by Deep Research.", "Research"),
            ("task_endpoint_id", "Task endpoint", "Endpoint used by scheduled and background tasks.", "Tasks"),
        )
    ],
    *[
        SettingSpec(key=key, type="string", label=label, help=help_text,
                    group=group, options_source="models")
        for key, label, help_text, group in (
            ("default_model", "Default model", "Model used for new chats.", "Models"),
            ("utility_model", "Utility model", "Model used for lightweight summaries and utility calls.", "Models"),
            ("research_model", "Research model", "Model used by Deep Research.", "Research"),
            ("task_model", "Task model", "Model used by scheduled and background tasks.", "Tasks"),
            ("image_model", "Image model", "Configured image-generation model.", "Images"),
            ("vision_model", "Vision model", "Configured image-understanding model.", "Images"),
        )
    ],
    # The Claude Code CLI runs Claude models only. This select used to list
    # the Odysseus endpoints' models (gpt-*), and saving one broke every
    # delegation with invalid_model; saves now go through
    # normalize_claude_model (see NORMALIZERS), which also accepts full
    # claude-* IDs typed in, so this is a suggestion list, not a closed one.
    # Keep in step with claude_code_tools._CLAUDE_MODEL_ALIASES.
    SettingSpec(
        key="claude_code_model", type="string", label="Claude Code model",
        help=("Model for delegated Claude Code runs: an alias (opus, sonnet, haiku, fable, best, "
              "opusplan) or a Claude model ID such as claude-opus-5-5; append [1m] for the "
              "1M-token context. Empty uses the signed-in account's default."),
        group="Agents", placeholder="Account default",
        suggestions=("opus", "sonnet", "haiku", "fable", "best", "opusplan"),
    ),
    SettingSpec(
        key="claude_code_backend", type="choice", label="Claude Code runs on",
        help=("Where a delegation that names no backend runs. The cloud runner needs at least one "
              "repository under Settings › Claude Code › Cloud runner; without one every "
              "delegation still runs locally."),
        group="Agents", choices=("local", "cloud"),
        choice_labels=("This machine", "Cloud runner (GitHub Actions)"), advanced=True,
    ),
    SettingSpec(
        key="claude_code_test_commands", type="bool", label="Claude Code may run tests",
        help=("Delegated Claude Code runs may run the repository's own tests and builds (npm "
              "scripts, Maven, Gradle, pytest; in the root folder and one level down) and are told "
              "which. These run the project's code as the Claude Code process, outside the agent's "
              "sandbox. Off: Claude Code only reads, edits and commits, and the agent tests."),
        group="Agents", advanced=True,
    ),
    SettingSpec(
        key="claude_code_max_concurrent_tasks", type="int", label="Claude Code task slots",
        help="Delegated Claude Code runs that may execute at once. 0 uses CLAUDE_CODE_MAX_CONCURRENT_TASKS or the built-in default.",
        group="Agents", min_value=0, max_value=16, unit="tasks", advanced=True,
    ),
    SettingSpec(
        key="chatgpt_reasoning_effort", type="choice", label="ChatGPT reasoning effort",
        help="Reasoning depth for ChatGPT-subscription chats whose agent profile sets none.",
        group="Models", choices=("", "minimal", "low", "medium", "high"),
        choice_labels=("Provider default", "Minimal", "Low", "Medium", "High"),
    ),
    # ── Voice output and Teacher: dormant features ──
    # Declared so saves still validate and the backend keeps reading them, but
    # hidden from the generic panel (2026-09-28): speech output ships with the
    # provider "disabled" and Teacher is off, so nine controls for features
    # nobody has switched on crowded out the ones that do something.
    SettingSpec(
        key="tts_enabled", type="bool", label="Speech output",
        help="Read assistant replies aloud.", group="Voice", hidden=True,
    ),
    SettingSpec(
        key="teacher_model", type="string", label="Teacher model",
        help="Model used for teacher and critique calls.", group="Teacher",
        options_source="models", hidden=True,
    ),
    SettingSpec(
        key="teacher_enabled", type="bool", label="Teacher",
        help="Critique and correct agent answers with the teacher model.", group="Teacher", hidden=True,
    ),
    SettingSpec(
        key="teacher_tier2_enabled", type="bool", label="Teacher second tier",
        help="Escalate to the teacher's second review tier.", group="Teacher", hidden=True,
    ),
    SettingSpec(
        key="tts_provider", type="string", label="Text-to-speech provider",
        help="Speech provider or compatible configured endpoint.", group="Voice",
        options_source="tts_providers", hidden=True,
    ),
    SettingSpec(
        key="stt_provider", type="string", label="Speech-to-text provider",
        help="Transcription provider or compatible configured endpoint.", group="Voice",
        options_source="stt_providers",
    ),
    SettingSpec(
        key="tts_model", type="string", label="Text-to-speech model",
        help="Speech model exposed by the selected provider.", group="Voice",
        options_source="tts_models", hidden=True,
    ),
    SettingSpec(
        key="stt_model", type="string", label="Speech-to-text model",
        help="Whisper size or transcription model exposed by the selected provider.", group="Voice",
        options_source="stt_models",
    ),
    SettingSpec(
        key="tts_voice", type="string", label="Text-to-speech voice",
        help="Provider voice name. Choose a common voice or enter a provider-specific one.", group="Voice",
        suggestions=("alloy", "ash", "ballad", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer", "verse", "af_heart"),
        hidden=True,
    ),
    SettingSpec(
        key="stt_language", type="string", label="Speech recognition language",
        help="Language code for transcription; leave empty to detect automatically.", group="Voice",
        suggestions=("en", "es", "fr", "de", "it", "pt", "nl", "pl", "ja", "ko", "zh"),
    ),
    SettingSpec(
        key="tts_speed", type="choice", label="Speech speed",
        help="Playback speed for generated speech.", group="Voice",
        # 0.5 is what the Voice tab offers; without it here a save from that
        # tab would be refused now that both routes check the choice list.
        choices=("0.5", "0.75", "1", "1.25", "1.5", "2"),
        choice_labels=("0.5× — slowest", "0.75× — slower", "1× — normal", "1.25×", "1.5×", "2× — faster"),
        hidden=True,
    ),
    # ── Knowledge: one store for notes and vault documents ──
    SettingSpec(
        key="vault_directory",
        type="path",
        label="Vault folder",
        help=(
            "Root of the Markdown knowledge base — the second mind. Notes and "
            "documents are files in here, so anything that edits Markdown "
            "(Obsidian, an editor, git) stays a first-class way to work."
        ),
        group="Knowledge",
        env_override="ODYSSEUS_PERSONAL_DIR",
        placeholder="/app/data/personal_docs",
    ),
    SettingSpec(
        key="notes_directory",
        type="string",
        label="Notes folder",
        help=(
            "Where notes are written, relative to the vault folder. Notes are "
            "Markdown files with YAML frontmatter, so they are readable and "
            "editable outside Odysseus. Changing this moves where new notes "
            "land; it does not move existing ones."
        ),
        group="Knowledge",
        placeholder="Notes",
    ),
    SettingSpec(
        key="notes_archive_directory",
        type="string",
        label="Archive folder",
        help=(
            "Where archived notes move to, relative to the vault folder. "
            "Archiving is a file move rather than a flag, so the folder tree "
            "tells the truth about what is active."
        ),
        group="Knowledge",
        placeholder="Notes/Archive",
    ),
    SettingSpec(
        key="vault_default_sensitivity",
        type="choice",
        label="Default sensitivity",
        help=(
            "Applied to a folder that declares nothing. Private content is "
            "hidden from models and agents until that specific chat is explicitly "
            "granted private-vault reads. Once granted, excerpts may be sent to "
            "the selected model endpoint. An individual file can override this "
            "in its own frontmatter."
        ),
        group="Knowledge",
        choices=("public", "private"),
    ),
    SettingSpec(
        key="vault_folder_sensitivity",
        type="json",
        label="Folder privacy & access",
        help=(
            "Map each vault-relative folder to \"public\", \"private\" or \"readonly\". "
            "For both privacy and write protection, use an object such as "
            "{\"Journal\": {\"sensitivity\": \"private\", \"readonly\": true}}. "
            "These rules constrain LLMs and agents only; signed-in humans can still use the vault editor. "
            "Rules inherit into subfolders; {\"readonly\": false} creates a model-writable child exception. "
            "Use an empty folder key to apply a rule to the vault root."
        ),
        group="Knowledge",
    ),

    # ── Agents: delegation and peer messaging ──
    SettingSpec(
        key="delegation_provider",
        type="choice",
        label="Code delegation provider",
        help=(
            "Which coding agent handles delegated work. Providers report whether "
            "they are usable on this host; unusable ones are never offered to the "
            "model, so it cannot pick a tool that will fail."
        ),
        group="Agents",
        choices=("auto", "claude_code_cli", "mcp", "none"),
        choice_labels=("Automatic", "Claude Code", "Connected MCP agent", "Disabled"),
        capability="code_delegation",
    ),
    SettingSpec(
        key="delegation_mcp_tool",
        type="string",
        label="MCP delegation tool",
        help=(
            "Qualified tool exposed by a connected coding-agent MCP server, "
            "such as mcp__server__delegate. The server owns authentication and billing."
        ),
        group="Agents",
        capability="code_delegation",
        advanced=True,
        placeholder="mcp__server__delegate",
    ),
    SettingSpec(
        key="delegation_mcp_default_arguments",
        type="json",
        label="MCP delegation defaults",
        help="Arguments merged into every call before the task-specific fields are sent.",
        group="Agents",
        capability="code_delegation",
        advanced=True,
    ),
    SettingSpec(
        key="agent_peer_messaging",
        type="bool",
        label="Let agents message each other",
        help=(
            "Agents can send a message into another agent's running turn, which "
            "lands between its rounds instead of waiting for it to finish. "
            "Without this, one agent can only call another and block."
        ),
        group="Agents",
    ),
    SettingSpec(
        key="agent_peer_message_budget",
        type="int",
        label="Peer messages per turn",
        help=(
            "How many messages one turn may send to other agents. Two agents "
            "that can each wake the other are a feedback loop with a token bill, "
            "so this cap is what makes peer messaging safe to leave on."
        ),
        group="Agents",
        min_value=0,
        max_value=64,
        unit="messages",
    ),
    SettingSpec(
        key="agent_max_worker_depth",
        type="int",
        label="Worker nesting depth",
        help=(
            "How many levels of workers may sit below a chat you started: 1 means "
            "workers never start workers of their own, 2 lets a worker start one "
            "more level. Each level multiplies what a single request can spend."
        ),
        group="Agents",
        min_value=1,
        max_value=4,
        unit="levels",
        advanced=True,
    ),
    SettingSpec(
        key="agent_auto_continue_limit",
        type="int",
        label="Automatic follow-ups per request",
        help=(
            "When a worker reports back without finishing what you asked, the chat "
            "that started it may fix what blocked it and send it back, or start "
            "another, this many times before it stops and asks you. 0 means it "
            "always reports back and waits."
        ),
        group="Agents",
        min_value=0,
        max_value=10,
        unit="follow-ups",
    ),

    SettingSpec(
        key="agent_approval_mode", type="choice", label="Approval prompts",
        help=("Choose when an agent must ask before acting. This is the default for "
              "every chat and sub-agent; a chat or agent profile can pick its own. "
              "‘Risky actions’ covers destructive shell commands, publishing, sending "
              "email, deletes and admin changes. ‘Every change’ also asks for any write "
              "or shell command, and for anything high-impact once web, email or file "
              "content has entered the run."),
        group="Agents", choices=("auto", "ask_risky", "ask_all"),
        choice_labels=("Run automatically", "Ask for risky actions", "Ask for every change"),
    ),
    SettingSpec(
        key="shell_sandbox", type="choice", label="Workspace shell sandbox",
        help=("Lets bash and python run in chats without private vault access, confined to "
              "the chat's workspace: the sandbox cannot see the app's data, the vault, other "
              "folders or the app's environment. Needs bubblewrap and a container allowed to "
              "create user namespaces. Off, or when it is unavailable, bash and python run only in "
              "chats with 'Allow private vault reads' on (Chat settings > Vault privacy)."),
        group="Agents", choices=("auto", "off"),
        choice_labels=("On when available", "Off"),
    ),
    SettingSpec(
        key="shell_sandbox_network", type="bool", label="Sandbox network access",
        help=("Whether the sandboxed shell may use the network (pip, npm, git fetch). Off also "
              "stops it reaching services next to Odysseus, such as ChromaDB, whose index "
              "holds vault excerpts."),
        group="Agents", advanced=True,
    ),
    SettingSpec(
        key="shell_sandbox_package_cache", type="bool", label="Keep package caches",
        help=("Keeps npm, Maven, Gradle and pip downloads between sandboxed shells, one cache "
              "per repository (its worktrees share it), so installing dependencies in a fresh "
              "worktree takes seconds instead of minutes. Stored under the data folder in "
              "agent_cache/."),
        group="Agents", advanced=True,
    ),
    SettingSpec(
        key="bash_idle_timeout_seconds", type="int", label="Stop silent commands after",
        help=("An agent's bash command that prints nothing for this long is stopped, and the "
              "agent is told why and how to rerun it: with more time for that one command, "
              "without quiet flags, or in the background. A command that keeps printing runs up "
              "to an hour. 0 = only the hour limit."),
        group="Agents", min_value=0, max_value=3600, unit="seconds", advanced=True,
    ),
    SettingSpec(
        key="agent_tool_budget", type="int", label="Tools per turn",
        help=("The most tools one agent turn is offered. Broad messages match many "
              "keyword domains; past this limit the domains retrieval agrees with "
              "least are dropped first. Tools you forced on, and the ones retrieval "
              "picked, always stay. 0 = no limit."),
        group="Agents", min_value=0, max_value=200, unit="tools", advanced=True,
    ),
    SettingSpec(
        key="agent_sticky_tools_max", type="int", label="Tools kept across turns",
        help=("A chat keeps offering the tools earlier turns used, so the model's "
              "prompt cache stays valid; past this many, a turn that needs more starts "
              "the set over. 0 = automatic: 48, or 96 (128 from a 256k window) for API "
              "models with a 128k+ context window, which also add whole tool domains at "
              "once so the set changes less often."),
        group="Agents", min_value=0, max_value=400, unit="tools", advanced=True,
    ),
    SettingSpec(
        key="agent_approval_ttl_seconds", type="int", label="Approval link lifetime",
        help="How long a one-time agent publishing approval remains valid.",
        group="Agents", min_value=60, max_value=3600, step=60, unit="seconds",
        env_override="ODYSSEUS_AGENT_APPROVAL_TTL_SECONDS", advanced=True,
    ),
    SettingSpec(
        key="tool_approval_ttl_seconds", type="int", label="Tool approval lifetime",
        help=("How long an approval card for a gated tool call stays answerable "
              "before it expires. A new message or a newer card ends it sooner."),
        group="Agents", min_value=30, max_value=86400, step=30, unit="seconds",
        advanced=True,
    ),
    SettingSpec(
        key="agent_base_branch", type="string", label="Default target branch",
        help="Branch used as the base for agent diffs and draft pull requests.",
        group="Agents", placeholder="dev", env_override="ODYSSEUS_AGENT_BASE_BRANCH",
    ),
    SettingSpec(
        key="agent_input_token_budget", type="int", label="Agent context budget",
        help=("Soft input-context limit per agent turn. The default 6,000 means automatic "
              "scaling for the selected model; 0 disables soft trimming."),
        group="Agents", min_value=0, max_value=2_000_000, step=1000, unit="tokens",
        advanced=True,
    ),
    SettingSpec(
        key="agent_input_token_hard_max", type="int", label="Agent context cap",
        help=("Most context the agent sends the model per step when the context budget is automatic: "
              "85% of the model's window, up to this cap. Explicit custom budgets can exceed it."),
        # 1,000,000, the ceiling the Agents tab offers; this said 2,000,000
        # while that route clamped to 1,000,000 (2026-09-28).
        group="Agents", min_value=16_000, max_value=1_000_000, step=1000, unit="tokens",
        advanced=True,
    ),
    SettingSpec(
        key="agent_max_tool_calls", type="int", label="Maximum tool calls",
        help="Safety limit per turn. Use 0 for no fixed ceiling; stall detection still applies.",
        group="Agents", min_value=0, max_value=2000, unit="calls", advanced=True,
    ),
    SettingSpec(
        key="chat_tool_fold_after", type="int", label="Fold tool timeline after",
        help="Collapse an agent's tool timeline in chat after this many calls in one turn. 0 never folds.",
        group="Chat", min_value=0, max_value=500, unit="calls", advanced=True,
    ),
    SettingSpec(
        key="agent_workflow_launch_stagger_seconds", type="float", label="Specialist launch spacing",
        help=("Pause between starting two specialists of one research workflow, so their first "
              "requests do not reach the model provider in a single burst. Use 0 to start them together."),
        group="Agents", min_value=0, max_value=10, step=0.5, unit="seconds", advanced=True,
    ),

    # ── Human-readable choices and the phase-five settings migration ──
    SettingSpec(
        key="mistral_reasoning_effort", type="choice", label="Mistral reasoning effort",
        help="Default reasoning depth for Mistral thinking models. Higher can improve difficult answers but costs more time and tokens.",
        group="Models", choices=("high", "medium", "low", "none"),
        choice_labels=("High", "Medium", "Low", "Off"),
        env_override="ODYSSEUS_MISTRAL_REASONING_EFFORT",
    ),
    SettingSpec(
        key="chatgpt_prompt_cache_key", type="bool", label="Reuse ChatGPT prompt cache",
        help="Keep consecutive ChatGPT subscription rounds on the same prompt cache for lower latency and repeated-input cost.",
        group="Models", advanced=True,
    ),
    SettingSpec(
        key="chatgpt_stable_tools", type="bool", label="Keep the tool list stable (GPT-5.6+)",
        help=("Send a chat's tools unchanged on every request and mark which ones may be called this turn, "
              "instead of re-sending a different list. A changed tool list makes the provider re-read the "
              "whole conversation uncached; this keeps long agent chats cached between turns."),
        group="Models", advanced=True,
    ),
    SettingSpec(
        key="image_quality", type="choice", label="Default image quality",
        help="Quality used when an image request does not specify one. Higher quality can take longer and cost more on hosted providers.",
        group="Images", choices=("low", "medium", "high", "auto"),
        choice_labels=("Low — fastest", "Medium", "High — most detail", "Provider default"),
        per_user=True,
    ),
    SettingSpec(
        key="gallery_sam_model", type="string", label="Segmentation model",
        help="Hugging Face model used to select and mask objects in Gallery editing tools.",
        group="Images", env_override="ODYSSEUS_SAM_MODEL", advanced=True,
    ),
    SettingSpec(
        key="gallery_grounding_model", type="string", label="Object detection model",
        help="Hugging Face model used to locate objects from text descriptions in Gallery tools.",
        group="Images", env_override="ODYSSEUS_GROUNDING_MODEL", advanced=True,
    ),
    SettingSpec(
        key="vault_date_order", type="choice", label="Ambiguous date order",
        help="How dates such as 03-04-2026 in Markdown filenames are interpreted.",
        group="Knowledge", choices=("day", "month"),
        choice_labels=("Day first — 3 April", "Month first — March 4"),
        env_override="ODYSSEUS_VAULT_DATE_ORDER",
    ),
    SettingSpec(
        key="vault_scan_seconds", type="int", label="Vault refresh interval",
        help="How often external Markdown edits are discovered. Set 0 to disable automatic rescans.",
        group="Knowledge", min_value=0, max_value=86400, unit="seconds",
        env_override="ODYSSEUS_VAULT_SCAN_SECONDS",
    ),
    SettingSpec(
        key="rag_link_expansion", type="bool", label="Follow linked notes",
        help="Include directly linked [[wiki notes]] when retrieving relevant vault context.",
        group="Knowledge", env_override="ODYSSEUS_RAG_LINK_EXPANSION", advanced=True,
    ),
    SettingSpec(
        key="rag_max_chunks_per_doc", type="int", label="Results per document",
        help="Maximum chunks one file may contribute to a retrieval result. Set 0 for no cap.",
        group="Knowledge", min_value=0, max_value=50, unit="chunks",
        env_override="ODYSSEUS_RAG_MAX_CHUNKS_PER_DOC", advanced=True,
    ),
    SettingSpec(
        key="rag_recency_halflife_days", type="float", label="Recency half-life",
        help="Age at which the ranking boost for a note is cut in half. Larger values favor older notes for longer.",
        group="Knowledge", min_value=1, max_value=36500, unit="days",
        env_override="ODYSSEUS_RAG_RECENCY_HALFLIFE_DAYS", advanced=True,
    ),
    SettingSpec(
        key="rag_temporal_weight", type="float", label="Everyday recency weight",
        help="How strongly newer notes break ties for normal queries. 0 ignores age; 0.9 heavily favors recent material.",
        group="Knowledge", min_value=0, max_value=0.9, step=0.01,
        env_override="ODYSSEUS_RAG_TEMPORAL_WEIGHT", advanced=True,
    ),
    SettingSpec(
        key="rag_temporal_intent_weight", type="float", label="Time-sensitive recency weight",
        help="Recency boost when a query says current, latest, today, or otherwise asks about time.",
        group="Knowledge", min_value=0, max_value=0.9, step=0.01,
        env_override="ODYSSEUS_RAG_TEMPORAL_INTENT_WEIGHT", advanced=True,
    ),
    SettingSpec(
        key="rag_tag_credit", type="float", label="Tag match boost",
        help="Ranking credit for matching a note tag or alias. Set 0 to disable the boost.",
        group="Knowledge", min_value=0, max_value=1, step=0.05,
        env_override="ODYSSEUS_RAG_TAG_CREDIT", advanced=True,
    ),
    SettingSpec(
        key="rag_focused_cap_multiplier", type="int", label="Focused-query result multiplier",
        help="Temporarily relaxes the per-document result cap when the query explicitly names a file or tag.",
        group="Knowledge", min_value=1, max_value=10,
        env_override="ODYSSEUS_RAG_FOCUSED_CAP_MULTIPLIER", advanced=True,
    ),

    SettingSpec(
        key="search_provider", type="choice", label="Web search provider",
        help="Primary service used by web search. Some providers require credentials configured in the Search tab.",
        group="Search",
        choices=("searxng", "duckduckgo", "brave", "google_pse", "tavily", "serper", "disabled"),
        choice_labels=("SearXNG — self-hosted", "DuckDuckGo — no key", "Brave Search", "Google PSE", "Tavily", "Serper.dev", "Disabled"),
    ),
    SettingSpec(
        key="search_fallback_chain", type="list", label="Search fallback providers",
        help=("Providers tried in order when the primary fails or is rate-limited, one per line. "
              "Leave empty for the default fallback (DuckDuckGo); enter none for no fallback at all."),
        group="Search", advanced=True,
    ),
    SettingSpec(
        key="search_safesearch", type="choice", label="SafeSearch",
        help="Adult-content filtering level translated to the equivalent supported by each search provider.",
        group="Search", choices=("strict", "moderate", "off"),
        choice_labels=("Strict", "Moderate", "Off"),
    ),
    SettingSpec(
        key="research_search_provider", type="choice", label="Research search provider",
        help="Provider used only for Deep Research. ‘Same as web search’ follows the primary choice above.",
        # google_pse is the dispatcher's name; "google" matched no provider and
        # Deep Research silently used the fallback engine (2026-09-28). A
        # stored "google" is read as google_pse (CHOICE_ALIASES).
        group="Research", choices=("", "searxng", "duckduckgo", "tavily", "brave", "google_pse", "serper"),
        choice_labels=("Same as web search", "SearXNG", "DuckDuckGo", "Tavily", "Brave", "Google PSE", "Serper"),
    ),
    # Deep Research limits, together under Research › advanced. The ranges are
    # the ones src/research_handler.py clamps to when it reads them; the
    # planning and query timeouts have no control in the Research tab, so this
    # is their only one.
    *[
        SettingSpec(key=key, type="int", label=label, help=help_text, group="Research",
                    min_value=low, max_value=high, unit=unit, advanced=True)
        for key, label, help_text, low, high, unit in (
            ("research_max_tokens", "Report length limit",
             "Most tokens the final research report may use.", 1024, None, "tokens"),
            ("research_run_timeout_seconds", "Research time limit",
             "Wall-clock cap on one research run. 0 = no limit; otherwise at least 60 seconds.",
             0, 86400, "seconds"),
            ("research_planning_timeout_seconds", "Planning call timeout",
             "How long one research-planning model call may take before it is abandoned.",
             15, 3600, "seconds"),
            ("research_query_timeout_seconds", "Query-writing call timeout",
             "How long one search-query model call may take before it is abandoned.",
             15, 3600, "seconds"),
            ("research_extraction_timeout_seconds", "Page extraction timeout",
             "How long reading one source page may take.", 15, 3600, "seconds"),
            ("research_extraction_concurrency", "Pages read at once",
             "Source pages extracted in parallel during one research run.", 1, 12, "pages"),
        )
    ],
    SettingSpec(
        key="reminder_channel", type="choice", label="Default reminder delivery",
        help="Where reminders are sent unless a particular reminder chooses a different channel.",
        group="Reminders", choices=("browser", "email", "ntfy", "webhook"),
        choice_labels=("Browser notification", "Email", "ntfy", "Webhook"),
    ),
    SettingSpec(
        key="reminder_email_account_id", type="string", label="Reminder email account",
        help="ID of the configured email account that sends email reminders. Empty uses the default account.",
        group="Reminders", advanced=True,
    ),
    # Edited through the fallback-chain widgets of the Models and Images tabs.
    # Lists of {endpoint_id, model} objects, so a one-name-per-line list
    # control would have saved "[object Object]" (2026-09-28).
    SettingSpec(
        key="utility_model_fallbacks", type="json", label="Utility model fallbacks",
        help="Ordered [{\"endpoint_id\": ..., \"model\": ...}] tried when the utility model fails.",
        group="Models", advanced=True,
    ),
    SettingSpec(
        key="vision_model_fallbacks", type="json", label="Vision model fallbacks",
        help="Ordered [{\"endpoint_id\": ..., \"model\": ...}] tried when the vision model fails.",
        group="Images", advanced=True,
    ),

    SettingSpec(
        key="browser_isolated", type="bool", label="Use an isolated browser profile",
        help="Start browser automation in a clean temporary profile instead of reusing persistent cookies and history.",
        group="Tools", env_override="ODYSSEUS_BROWSER_ISOLATED",
    ),
    SettingSpec(
        key="penpot_api_url", type="string", label="Penpot URL",
        help="Base URL of your Penpot instance as this server reaches it, for example http://penpot-frontend:8080. Used by the Penpot Studio tools.",
        group="Penpot", placeholder="http://penpot-frontend:8080",
    ),
    SettingSpec(
        key="penpot_access_token", type="secret", label="Penpot access token",
        help="A Penpot access token (Penpot > Profile > Access tokens). Stored here and never shown again.",
        group="Penpot", sensitive=True,
    ),
    SettingSpec(
        key="penpot_public_url", type="string", label="Penpot public URL",
        help="Address you open Penpot at in a browser, when it differs from the URL above. Used for links the agent shows you.",
        group="Penpot", placeholder="https://penpot.example.com",
    ),
    SettingSpec(
        key="startup_warmups_enabled", type="bool", label="Warm services at startup",
        help="Prepare the tool index and model endpoints during startup for a faster first request, at the cost of a slower boot.",
        group="System", env_override="ODYSSEUS_STARTUP_WARMUPS",
    ),
    SettingSpec(
        key="model_keepalive_enabled", type="bool", label="Keep local models warm",
        help="Reduce repeat-request latency by keeping local models loaded. This uses memory or VRAM while idle.",
        group="System", env_override="ODYSSEUS_MODEL_KEEPALIVE",
    ),

    SettingSpec(
        key="stt_beam_size", type="int", label="Speech recognition beam size",
        help="Number of candidate transcriptions considered by local Whisper. Higher can improve accuracy but is slower.",
        group="Voice", min_value=1, max_value=64, env_override="ODYSSEUS_STT_BEAM_SIZE", advanced=True,
    ),
    SettingSpec(
        key="stt_max_audio_seconds", type="int", label="Maximum recording length",
        help="Longest audio clip accepted for one speech-to-text request.",
        group="Voice", min_value=1, max_value=86400, unit="seconds",
        env_override="ODYSSEUS_STT_MAX_AUDIO_SECONDS",
    ),
    SettingSpec(
        key="tts_cache_max_bytes", type="int", label="Speech cache size",
        help="Disk budget for generated speech audio. Older cached files are removed when this limit is exceeded.",
        group="Voice", min_value=1, max_value=10**12, unit="MiB", scale=1_048_576,
        env_override="ODYSSEUS_TTS_CACHE_MAX_BYTES", advanced=True, hidden=True,
    ),
    SettingSpec(
        key="imap_timeout_seconds", type="int", label="Mail server timeout",
        help="How long Odysseus waits for an IMAP mail server before treating it as unavailable.",
        group="Email", min_value=5, max_value=300, unit="seconds",
        env_override="ODYSSEUS_IMAP_TIMEOUT_SECONDS", advanced=True,
    ),

    # Upload/storage limits use MiB in the browser and bytes in settings.json.
    *[
        SettingSpec(
            key=key, type="int", label=label, help=help_text,
            group="Limits & uploads", min_value=1, max_value=10**12,
            unit="MiB", scale=1_048_576, env_override=env_name,
        )
        for key, label, help_text, env_name in (
            ("chat_upload_max_bytes", "Chat attachments", "Maximum size of a file attached to chat or an agent task.", "ODYSSEUS_CHAT_UPLOAD_MAX_BYTES"),
            ("gallery_upload_max_bytes", "Gallery uploads", "Maximum original image size accepted by Gallery.", "ODYSSEUS_GALLERY_UPLOAD_MAX_BYTES"),
            ("gallery_transform_upload_max_bytes", "Gallery edit inputs", "Maximum image size accepted by Gallery transformation tools.", "ODYSSEUS_GALLERY_TRANSFORM_UPLOAD_MAX_BYTES"),
            ("memory_import_max_bytes", "Memory imports", "Maximum file size accepted by Memory import.", "ODYSSEUS_MEMORY_IMPORT_MAX_BYTES"),
            ("personal_upload_max_bytes", "Vault documents", "Maximum file size uploaded to the personal document vault.", "ODYSSEUS_PERSONAL_UPLOAD_MAX_BYTES"),
            ("email_compose_upload_max_bytes", "Email attachments", "Maximum file size attached through the email composer.", "ODYSSEUS_EMAIL_COMPOSE_UPLOAD_MAX_BYTES"),
            ("stt_max_audio_bytes", "Speech-to-text audio", "Maximum audio upload size accepted for transcription.", "ODYSSEUS_STT_MAX_AUDIO_BYTES"),
            ("ics_import_max_bytes", "Calendar imports", "Maximum .ics calendar file size accepted for import.", "ODYSSEUS_ICS_MAX_BYTES"),
        )
    ],
])


def register_existing_defaults() -> None:
    """Give every remaining DEFAULT_SETTINGS key a spec.

    The 83 pre-existing keys were never declared anywhere; most already have a
    hand-built control in the settings page. Rather than hand-write 83 entries
    and get their labels subtly wrong, infer type and label and let the curated
    entries above take precedence — ``register`` is last-write-wins, so this
    runs first and never clobbers a curated spec.

    This exists so ``missing_specs()`` can be empty today, which is what makes
    the completeness test meaningful for *new* settings from here on.
    """
    from src.settings import DEFAULT_SETTINGS, RETIRED_SETTING_KEYS

    def infer_type(value: Any) -> str:
        if isinstance(value, bool):
            return "bool"
        if isinstance(value, int):
            return "int"
        if isinstance(value, float):
            return "float"
        if isinstance(value, (list, tuple)):
            return "list"
        if isinstance(value, dict):
            return "json"
        text = str(value or "")
        return "text" if len(text) > 120 else "string"

    def infer_group(key: str) -> str:
        for prefix, group in (
            ("agent_", "Agents"), ("claude_code_", "Agents"),
            ("tts_", "Voice"), ("stt_", "Voice"),
            ("image_", "Images"), ("vision_", "Images"), ("gallery_", "Images"),
            ("rag_", "Knowledge"), ("vault_", "Knowledge"), ("notes_", "Knowledge"),
            ("search_", "Search"), ("google_pse", "Search"), ("brave_", "Search"),
            ("serper_", "Search"), ("tavily_", "Search"),
            ("reminder_", "Reminders"), ("email_", "Email"), ("urgent_email", "Email"),
            ("research_", "Research"), ("skill_", "Skills"),
            ("task_", "Tasks"), ("teacher_", "Teacher"),
            ("workbench_", "Agents"),
            ("default_", "Models"), ("utility_", "Models"), ("chatgpt_", "Models"),
            ("tool_", "Tools"), ("chat_", "Chat"), ("document_", "Documents"),
        ):
            if key.startswith(prefix):
                return group
        return "General"

    sensitive_keys = {
        "brave_api_key", "google_pse_key", "serper_api_key", "tavily_api_key",
        "claude_code_odysseus_token_file",
    }
    for key, value in DEFAULT_SETTINGS.items():
        # A retired key is kept in the store only for rollback; declaring it
        # would put it back in the panel and make it savable again, which is
        # what happened to default_model_fallbacks until 2026-09-28.
        if key in EXEMPT or key in _SPECS or key in RETIRED_SETTING_KEYS:
            continue
        sensitive = key in sensitive_keys
        register(SettingSpec(
            key=key,
            type="secret" if sensitive else infer_type(value),
            label=key.replace("_", " ").strip().capitalize(),
            group=infer_group(key),
            sensitive=sensitive,
            env_override=_MIGRATED_ENV_OVERRIDES.get(key, ""),
            advanced=True,
        ))


# Fill the gaps at import time so `missing_specs()` is empty for the settings
# that predate this module. Curated specs above are already registered, and
# this never overwrites them.
register_existing_defaults()
