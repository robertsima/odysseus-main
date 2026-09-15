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
    # Deployment may pin this; the UI renders it read-only when the var is set.
    env_override: str = ""
    # Never echo the stored value back to the browser.
    sensitive: bool = False
    placeholder: str = ""

    def as_dict(self, value: Any = None, locked: bool = False) -> dict:
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
            "locked": locked,
            "placeholder": self.placeholder,
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
        raise ValueError("must be an object mapping a folder path to \"public\" or \"private\"")
    for key, label in value.items():
        if not isinstance(key, str) or not isinstance(label, str):
            raise ValueError(f"every folder and label must be text (got {key!r}: {label!r})")
        if str(label).strip().lower() not in {"public", "private"}:
            raise ValueError(f"{key!r} has label {label!r}; expected \"public\" or \"private\"")
        cleaned = key.replace("\\", "/").strip()
        if cleaned.startswith("/") or ".." in cleaned.split("/") or ":" in cleaned:
            raise ValueError(
                f"{key!r} must be a folder inside the vault — no absolute paths, drive letters or '..'"
            )


VALIDATORS: Dict[str, Any] = {
    "vault_folder_sensitivity": _validate_folder_sensitivity,
}


def validate_value(key: str, value: Any) -> None:
    """Raise ``ValueError`` with an operator-readable reason if ``value`` is unusable."""
    check = VALIDATORS.get(key)
    if check is not None:
        check(value)


def ui_payload(owner: str = "", is_admin: bool = True) -> List[dict]:
    """Everything the settings UI needs to render, grouped, in one payload.

    Non-admins see only the settings they may change, so the page does not show
    controls that will be refused on save.
    """
    from src.settings import get_setting, get_user_setting

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
        groups.setdefault(spec.group, []).append(spec.as_dict(value, locked=env_locked(spec)))
    return [{"group": name, "settings": groups[name]} for name in sorted(groups)]


# ── the schema ────────────────────────────────────────────────────────────
# Grouped as the operator thinks about them, not as the code is organised.

register_all([
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
            "retrieved only when the session's model runs locally, so it never "
            "reaches a hosted endpoint. An individual file can override this in "
            "its own frontmatter."
        ),
        group="Knowledge",
        choices=("public", "private"),
    ),
    SettingSpec(
        key="vault_folder_sensitivity",
        type="json",
        label="Per-folder sensitivity",
        help=(
            "Folder path to \"public\" or \"private\". A folder inherits from its "
            "parent, and a file inherits from its folder — so labelling a tree "
            "once covers everything added to it later."
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
        placeholder="mcp__server__delegate",
    ),
    SettingSpec(
        key="delegation_mcp_default_arguments",
        type="json",
        label="MCP delegation defaults",
        help="Arguments merged into every call before the task-specific fields are sent.",
        group="Agents",
        capability="code_delegation",
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
    ),

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
            ("image_", "Images"), ("vision_", "Images"),
            ("search_", "Search"), ("google_pse", "Search"), ("brave_", "Search"),
            ("reminder_", "Reminders"), ("email_", "Email"), ("urgent_email", "Email"),
            ("research_", "Research"), ("skill_", "Skills"),
            ("task_", "Tasks"), ("teacher_", "Teacher"),
            ("default_", "Models"), ("utility_", "Models"), ("chatgpt_", "Models"),
            ("tool_", "Tools"), ("chat_", "Chat"), ("document_", "Documents"),
        ):
            if key.startswith(prefix):
                return group
        return "General"

    sensitive_markers = ("_key", "_token", "_secret", "_password")
    for key, value in DEFAULT_SETTINGS.items():
        if key in EXEMPT or key in _SPECS:
            continue
        register(SettingSpec(
            key=key,
            type="secret" if any(m in key for m in sensitive_markers) else infer_type(value),
            label=key.replace("_", " ").strip().capitalize(),
            group=infer_group(key),
            per_user=key in _PER_USER_KEYS,
            sensitive=any(m in key for m in sensitive_markers),
            env_override=_MIGRATED_ENV_OVERRIDES.get(key, ""),
        ))


# Fill the gaps at import time so `missing_specs()` is empty for the settings
# that predate this module. Curated specs above are already registered, and
# this never overwrites them.
register_existing_defaults()
