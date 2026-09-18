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

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

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
    # Eligible for a per-user override (see settings._PER_USER_KEYS).
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
    from src.settings import DEFAULT_SETTINGS

    return sorted(k for k in DEFAULT_SETTINGS if k not in _SPECS and k not in EXEMPT)


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
    "slow_request_log_seconds": "ODYSSEUS_SLOW_REQUEST_LOG_SECONDS",
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


VALIDATORS: Dict[str, Any] = {
    "vault_folder_sensitivity": _validate_folder_sensitivity,
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


def ui_payload(owner: str = "", is_admin: bool = True) -> List[dict]:
    """Everything the settings UI needs to render, grouped, in one payload.

    Non-admins see only the settings they may change, so the page does not show
    controls that will be refused on save.
    """
    from src.settings import DEFAULT_SETTINGS, get_setting, get_user_setting

    groups: Dict[str, List[dict]] = {}
    for spec in all_specs():
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
        value = get_user_setting(spec.key, owner) if (spec.per_user and owner) else get_setting(spec.key)
        groups.setdefault(spec.group, []).append(spec.as_dict(
            value,
            locked=env_locked(spec),
            default=DEFAULT_SETTINGS.get(spec.key),
        ))

    group_help = {
        "Agents": "Delegation, approvals, collaboration, and agent runtime limits.",
        "Knowledge": "Markdown notes, retrieval behavior, and vault privacy.",
        "Models": "Model behavior shared across chats and background work.",
        "Limits & uploads": "File-size and storage limits. Values are shown in MiB.",
        "System": "Startup, diagnostics, and host resource behavior.",
        "Voice": "Speech recognition, speech output, and their resource limits.",
        "Images": "Image generation, vision, gallery analysis, and model choices.",
        "Search": "Web-search providers, filtering, and result behavior.",
        "Remote hosts": "Machines this installation may use for remote work.",
    }
    order = (
        "General", "Agents", "Knowledge", "Models", "Voice", "Images",
        "Search", "Research", "Reminders", "Email", "Chat", "Documents",
        "Skills", "Tasks", "Teacher", "Tools", "Remote hosts",
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
            ("teacher_model", "Teacher model", "Model used for teacher and critique calls.", "Teacher"),
            ("image_model", "Image model", "Configured image-generation model.", "Images"),
            ("vision_model", "Vision model", "Configured image-understanding model.", "Images"),
            ("claude_code_model", "Claude Code model", "Optional model override for Claude Code delegation.", "Agents"),
        )
    ],
    SettingSpec(
        key="tts_provider", type="string", label="Text-to-speech provider",
        help="Speech provider or compatible configured endpoint.", group="Voice",
        options_source="tts_providers",
    ),
    SettingSpec(
        key="stt_provider", type="string", label="Speech-to-text provider",
        help="Transcription provider or compatible configured endpoint.", group="Voice",
        options_source="stt_providers",
    ),
    SettingSpec(
        key="tts_model", type="string", label="Text-to-speech model",
        help="Speech model exposed by the selected provider.", group="Voice",
        options_source="tts_models",
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
    ),
    SettingSpec(
        key="stt_language", type="string", label="Speech recognition language",
        help="Language code for transcription; leave empty to detect automatically.", group="Voice",
        suggestions=("en", "es", "fr", "de", "it", "pt", "nl", "pl", "ja", "ko", "zh"),
    ),
    SettingSpec(
        key="tts_speed", type="choice", label="Speech speed",
        help="Playback speed for generated speech.", group="Voice",
        choices=("0.75", "1", "1.25", "1.5", "2"),
        choice_labels=("0.75× — slower", "1× — normal", "1.25×", "1.5×", "2× — faster"),
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
        key="agent_approval_ttl_seconds", type="int", label="Approval link lifetime",
        help="How long a one-time agent publishing approval remains valid.",
        group="Agents", min_value=60, max_value=3600, step=60, unit="seconds",
        env_override="ODYSSEUS_AGENT_APPROVAL_TTL_SECONDS", advanced=True,
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
        key="agent_input_token_hard_max", type="int", label="Automatic budget ceiling",
        help="Maximum context budget chosen by automatic scaling; explicit custom budgets can exceed it.",
        group="Agents", min_value=1000, max_value=2_000_000, step=1000, unit="tokens",
        advanced=True,
    ),
    SettingSpec(
        key="agent_max_rounds", type="int", label="Maximum agent rounds",
        help="Safety limit on reasoning/tool rounds in one turn before the agent must finish.",
        group="Agents", min_value=1, max_value=500, unit="rounds", advanced=True,
    ),
    SettingSpec(
        key="agent_max_tool_calls", type="int", label="Maximum tool calls",
        help="Safety limit per turn. Use 0 for no fixed ceiling; stall detection still applies.",
        group="Agents", min_value=0, max_value=2000, unit="calls", advanced=True,
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
        per_user=True,
    ),
    SettingSpec(
        key="search_safesearch", type="choice", label="SafeSearch",
        help="Adult-content filtering level translated to the equivalent supported by each search provider.",
        group="Search", choices=("strict", "moderate", "off"),
        choice_labels=("Strict", "Moderate", "Off"), per_user=True,
    ),
    SettingSpec(
        key="research_search_provider", type="choice", label="Research search provider",
        help="Provider used only for Deep Research. ‘Same as web search’ follows the primary choice above.",
        group="Research", choices=("", "searxng", "duckduckgo", "tavily", "brave", "google", "serper"),
        choice_labels=("Same as web search", "SearXNG", "DuckDuckGo", "Tavily", "Brave", "Google", "Serper"),
    ),
    SettingSpec(
        key="reminder_channel", type="choice", label="Default reminder delivery",
        help="Where reminders are sent unless a particular reminder chooses a different channel.",
        group="Reminders", choices=("browser", "email", "ntfy", "webhook"),
        choice_labels=("Browser notification", "Email", "ntfy", "Webhook"), per_user=True,
    ),

    SettingSpec(
        key="browser_isolated", type="bool", label="Use an isolated browser profile",
        help="Start browser automation in a clean temporary profile instead of reusing persistent cookies and history.",
        group="Tools", env_override="ODYSSEUS_BROWSER_ISOLATED",
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
        key="slow_request_log_seconds", type="float", label="Slow request warning",
        help="Log requests that take longer than this threshold. Use 0 to log every request as slow.",
        group="System", min_value=0, max_value=3600, step=0.05, unit="seconds",
        env_override="ODYSSEUS_SLOW_REQUEST_LOG_SECONDS", advanced=True,
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
        env_override="ODYSSEUS_TTS_CACHE_MAX_BYTES", advanced=True,
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

    # ── Remote hosts: one inventory instead of scattered ssh calls ──
    SettingSpec(
        key="remote_hosts",
        type="json",
        label="Remote hosts",
        help=(
            "Machines Odysseus may reach over SSH, each with what it is allowed "
            "to do. One inventory replaces relying on whatever ~/.ssh/config "
            "happens to contain on the host."
        ),
        group="Remote hosts",
        capability="remote_hosts",
        advanced=True,
    ),
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
    from src.settings import DEFAULT_SETTINGS, _PER_USER_KEYS

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
        if key in EXEMPT or key in _SPECS:
            continue
        sensitive = key in sensitive_keys
        register(SettingSpec(
            key=key,
            type="secret" if sensitive else infer_type(value),
            label=key.replace("_", " ").strip().capitalize(),
            group=infer_group(key),
            per_user=key in _PER_USER_KEYS,
            sensitive=sensitive,
            env_override=_MIGRATED_ENV_OVERRIDES.get(key, ""),
            advanced=True,
        ))


# Fill the gaps at import time so `missing_specs()` is empty for the settings
# that predate this module. Curated specs above are already registered, and
# this never overwrites them.
register_existing_defaults()
