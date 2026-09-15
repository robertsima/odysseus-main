"""The capabilities this build ships, and what each one needs from the host.

Importing this module registers them. Everything here is a *declaration* — the
mechanism lives in :mod:`src.capabilities`.

The default-enabled decision follows one rule: a capability is on by default only
if it works on a bare install with no host setup. Anything that shells out to a
binary the operator must install, reaches another machine, or hands a model
control of the host starts off. That is the difference between a fresh install
that works and one that advertises tools which fail on first use.
"""

from __future__ import annotations

from src.capabilities import Capability, Requirement, any_of, binary_on_path, env_flag, register


def _has_configured_remote_hosts() -> tuple[bool, str]:
    from src.settings import get_setting

    hosts = get_setting("remote_hosts", []) or []
    if isinstance(hosts, (list, tuple)) and len(hosts):
        return True, f"{len(hosts)} host(s) configured"
    return False, "no remote hosts configured"


def _delegation_provider_available() -> tuple[bool, str]:
    from src.delegation import selection

    provider, reason = selection()
    return provider is not None, reason


# ── knowledge ─────────────────────────────────────────────────────────────

register(Capability(
    name="vault",
    title="Vault",
    summary=(
        "One Markdown store for notes and documents, with folder-level privacy. "
        "Private folders are retrieved only by a locally-run model."
    ),
    feature_key="rag",
    default_enabled=True,
    settings=(
        "vault_directory", "notes_directory", "notes_archive_directory",
        "vault_default_sensitivity", "vault_folder_sensitivity",
    ),
    # RAG can be disabled without disabling the file-backed notes UI or the
    # separate document editor. Only retrieval belongs to the `rag` feature.
    tools=("search_documents",),
))

# ── delegation ────────────────────────────────────────────────────────────

register(Capability(
    name="code_delegation",
    title="Code delegation",
    summary=(
        "Hand a coding task to another agent in a real checkout. Any provider "
        "that reports itself usable can serve it — a local CLI, a "
        "subscription-backed network agent, or an MCP server."
    ),
    requirements=(
        Requirement(
            name="a usable provider",
            check=_delegation_provider_available,
            hint=(
                "Install a coding-agent CLI and set its path in Settings › Agents, "
                "or configure a connected coding-agent MCP tool. Claude "
                "subscriptions remain available through the unmodified Claude Code CLI; "
                "Odysseus does not collect Claude account tokens. No API key is required "
                "for a subscription-authenticated CLI or MCP server."
            ),
        ),
    ),
    default_enabled=True,
    settings=("delegation_provider", "delegation_mcp_tool", "delegation_mcp_default_arguments",
              "claude_code_binary", "claude_code_repository_roots"),
    tools=("delegate_to_agent", "delegate_to_claude_code"),
))

# ── remote hosts ──────────────────────────────────────────────────────────

register(Capability(
    name="remote_hosts",
    title="Remote hosts",
    summary=(
        "Machines Odysseus may reach over SSH, each with what it is allowed to "
        "do. Replaces depending on whatever the host's ~/.ssh/config contains."
    ),
    requirements=(
        Requirement(
            name="ssh client",
            check=binary_on_path("ssh"),
            hint="Install an SSH client on the machine running Odysseus.",
        ),
        Requirement(
            name="at least one host",
            check=_has_configured_remote_hosts,
            hint="Add a host under Settings › Remote hosts.",
        ),
    ),
    settings=("remote_hosts",),
))

# ── model serving (Cookbook) ──────────────────────────────────────────────
# Includes the MLX image-model runners (DDColor colorization, inpainting,
# diffusion): those are serve targets for image models on Apple Silicon, not
# separate features, so they live and die with this capability.

register(Capability(
    name="model_serving",
    title="Model serving (Cookbook)",
    summary=(
        "Start and supervise local or remote model servers — vLLM, SGLang, "
        "llama.cpp, and the MLX image runners on Apple Silicon."
    ),
    requirements=(
        Requirement(
            name="a serving runtime",
            check=any_of(
                binary_on_path("vllm"),
                binary_on_path("sglang"),
                binary_on_path("llama-server"),
                binary_on_path("mlx_lm.server"),
            ),
            hint=(
                "Install a serving runtime (vLLM, SGLang, llama.cpp or MLX) on "
                "this machine, or add a remote host that has one."
            ),
        ),
    ),
    tools=("cookbook",),
))

# ── host control ──────────────────────────────────────────────────────────

register(Capability(
    name="host_docker",
    title="Host Docker access",
    summary=(
        "Lets Odysseus drive the host's Docker daemon. High trust: this is "
        "effectively root on the host, so it is opt-in per deployment."
    ),
    requirements=(
        Requirement(
            name="socket mounted and opted in",
            check=env_flag("ODYSSEUS_ENABLE_HOST_DOCKER"),
            hint=(
                "Enable docker/host-docker.yml in your compose stack. Prefer a "
                "remote host over SSH where you can."
            ),
        ),
    ),
))

register(Capability(
    name="agent_worktrees",
    title="Agent worktrees",
    summary=(
        "Lets an agent work in its own git worktree and branch, so parallel "
        "changes never collide in one checkout."
    ),
    requirements=(
        Requirement(
            name="git",
            check=binary_on_path("git"),
            hint="Install git on the machine running Odysseus.",
        ),
    ),
    # On by default, and deliberately NOT gating the whole tool behind
    # publishing. Creating a local worktree and committing to a branch is
    # ordinary local work; what needs an explicit decision is *pushing* to a
    # remote nobody reviewed — and that already has its own gate,
    # ODYSSEUS_AGENT_PUBLISH_ENABLED, enforced in agent_worktree/config.py.
    # Gating the tool here too would have removed local worktrees from every
    # default install, which is a capability regression dressed up as caution.
    default_enabled=True,
    tools=("manage_agent_worktree",),
))
