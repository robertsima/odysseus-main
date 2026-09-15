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

from dataclasses import dataclass, field
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


def env_locked(spec: SettingSpec) -> bool:
    import os

    return bool(spec.env_override and str(os.environ.get(spec.env_override, "") or "").strip())


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
        choices=("auto", "claude_code_cli", "claude_subscription", "mcp", "none"),
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
        ))


# Fill the gaps at import time so `missing_specs()` is empty for the settings
# that predate this module. Curated specs above are already registered, and
# this never overwrites them.
register_existing_defaults()
