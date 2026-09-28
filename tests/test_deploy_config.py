"""Deployment configuration contracts: compose files, .env.example, entrypoint.

Pure file parsing, no Docker required. These pin the configuration cleanup:
what reaches the container, what the ZimaOS template leaves to code defaults,
the shell-sandbox security options, and that the image no longer turns
.env.example into a hidden configuration layer.
"""

import importlib.util
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
GENERIC_COMPOSE = (
    ROOT / "docker-compose.yml",
    ROOT / "docker-compose.gpu-nvidia.yml",
    ROOT / "docker-compose.gpu-amd.yml",
)
ZIMAOS_TEMPLATE = ROOT / "docker-compose.zimaos-local.yml"
ALL_COMPOSE = GENERIC_COMPOSE + (ZIMAOS_TEMPLATE,)
ENV_EXAMPLE = ROOT / ".env.example"

# Read by nothing in the application (or, for EMBEDDING_*, deliberately not
# consulted), so forwarding or documenting them only misleads.
DEAD_VARIABLES = (
    "RESEARCH_LLM_ENDPOINT",
    "ODYSSEUS_FASTEMBED_LANE",
    "EMBEDDING_URL",
    "EMBEDDING_MODEL",
    "EMBEDDING_API_KEY",
)


def _odysseus(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["services"]["odysseus"]


def _env(path: Path) -> dict:
    """The odysseus environment as {name: value}; a bare name maps to None."""
    environment = _odysseus(path)["environment"]
    if isinstance(environment, dict):
        return dict(environment)
    out = {}
    for entry in environment:
        name, sep, value = str(entry).partition("=")
        out[name] = value if sep else None
    return out


# ── shell sandbox security options ───────────────────────────────


@pytest.mark.parametrize("path", ALL_COMPOSE, ids=lambda p: p.name)
def test_compose_security_opt_lets_bubblewrap_mount_its_own_proc(path):
    security_opt = _odysseus(path)["security_opt"]
    # User namespaces (seccomp/apparmor) AND an unmasked /proc (systempaths):
    # without the latter the sandbox cannot mount a fresh procfs on kernels
    # that enforce the fully-visible-procfs rule.
    for option in ("seccomp=unconfined", "apparmor=unconfined", "systempaths=unconfined"):
        assert option in security_opt, (path.name, option)
    # Paired with systempaths: nothing inside may regain root via setuid.
    assert "no-new-privileges:true" in security_opt, path.name


@pytest.mark.parametrize("path", ALL_COMPOSE, ids=lambda p: p.name)
def test_compose_security_opt_documents_the_verification(path):
    text = path.read_text(encoding="utf-8")
    assert "{{json .HostConfig.MaskedPaths}} {{json .HostConfig.ReadonlyPaths}}" in text
    assert "[shell-sandbox] available" in text
    assert "kernel.core_pattern" in text, "the systempaths trade-off must be stated"


# ── what the compose files forward ───────────────────────────────


@pytest.mark.parametrize("path", ALL_COMPOSE, ids=lambda p: p.name)
def test_compose_files_do_not_forward_dead_variables(path):
    env = _env(path)
    for name in DEAD_VARIABLES:
        assert name not in env, (path.name, name)


@pytest.mark.parametrize("path", ALL_COMPOSE, ids=lambda p: p.name)
def test_pi_worker_script_and_root_are_passed_only_when_set(path):
    # The Pi worker rejects an EMPTY script/root as an invalid path instead of
    # falling back to its default, so `${VAR:-}` would break it. A bare name
    # is omitted from the container when unset.
    env = _env(path)
    assert env["ODYSSEUS_PI_WORKER_SCRIPT"] is None, path.name
    assert env["ODYSSEUS_PI_WORKER_ROOT"] is None, path.name


@pytest.mark.parametrize("path", GENERIC_COMPOSE, ids=lambda p: p.name)
def test_generic_compose_forwards_calendar_redirect_with_mail_redirect(path):
    env = _env(path)
    assert env["GOOGLE_CALENDAR_OAUTH_REDIRECT_URI"] == "${GOOGLE_CALENDAR_OAUTH_REDIRECT_URI:-}"


def test_zimaos_template_lists_only_required_and_non_default_values():
    env = _env(ZIMAOS_TEMPLATE)
    # Equal to code defaults, dead, or (SECURE_COOKIES=false) harmful: it
    # forced non-Secure cookies over HTTPS, while the code derives the flag
    # from the request scheme.
    for name in (
        "ALLOWED_ORIGINS", "AUTH_ENABLED", "DATABASE_URL", "FASTEMBED_CACHE_PATH",
        "FASTEMBED_MODEL", "ODYSSEUS_TOOL_EXTRA_ROOTS", "LOCALHOST_BYPASS",
        "ODYSSEUS_ADMIN_USER", "ODYSSEUS_INPROCESS_POLLERS", "ODYSSEUS_INPROCESS_TASKS",
        "ODYSSEUS_SCRIPT_HOST", "GITHUB_HOST", "LOTUS_LOG_LEVEL", "LOTUS_CONFIG",
        "LOTUS_DATA_DIR", "LOTUS_IMPORT_ROOT", "LOTUS_TOOL_NAME_STYLE",
        "ODYSSEUS_STT_DEVICE", "ODYSSEUS_STT_COMPUTE_TYPE", "ODYSSEUS_STT_CONCURRENCY",
        "SECURE_COOKIES",
    ):
        assert name not in env, name
    for name in (
        "PUID", "PGID", "CHROMADB_HOST", "CHROMADB_PORT", "SEARXNG_INSTANCE",
        "ODYSSEUS_PERSONAL_DIRS", "ODYSSEUS_ADMIN_PASSWORD",
    ):
        assert name in env, name
    assert env["CHROMADB_HOST"] == "chromadb"
    assert env["SEARXNG_INSTANCE"] == "http://searxng:8080"
    assert "Journal:private" in env["ODYSSEUS_PERSONAL_DIRS"]


@pytest.mark.parametrize(
    "name",
    (
        # Agent worktree publishing and the Claude Code cloud runner.
        "ODYSSEUS_AGENT_PUBLISH_ENABLED",
        "ODYSSEUS_AGENT_REPO",
        "ODYSSEUS_AGENT_SOURCE_REPO",
        "ODYSSEUS_GITHUB_APP_ID",
        "ODYSSEUS_GITHUB_APP_INSTALLATION_ID",
        "ODYSSEUS_GITHUB_APP_PRIVATE_KEY_PATH",
        "GOOGLE_OAUTH_CLIENT_ID",
        "GOOGLE_OAUTH_CLIENT_SECRET",
    ),
)
def test_zimaos_template_passes_optional_feature_variables_through(name):
    assert _env(ZIMAOS_TEMPLATE)[name] == f"${{{name}:-}}"


# ── no hidden .env inside the image ──────────────────────────────


def _load_setup_module():
    spec = importlib.util.spec_from_file_location("odysseus_setup_env_test", ROOT / "setup.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_entrypoint_runs_setup_without_creating_an_env_file():
    script = (ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
    assert re.search(
        r'^ODYSSEUS_SKIP_ENV_FILE=1 "\$GOSU_BIN" "\$ODY_USER" "\$PYTHON_BIN" /app/setup\.py',
        script,
        re.MULTILINE,
    )
    # Scoped to setup.py: the app itself must not inherit it via export.
    assert "export ODYSSEUS_SKIP_ENV_FILE" not in script


def test_create_env_is_a_no_op_when_skip_env_file_is_set(tmp_path, monkeypatch):
    setup_module = _load_setup_module()
    (tmp_path / ".env.example").write_text("LLM_HOST=localhost\n", encoding="utf-8")
    monkeypatch.setattr(setup_module, "BASE_DIR", str(tmp_path))
    monkeypatch.setenv("ODYSSEUS_SKIP_ENV_FILE", "1")

    setup_module.create_env()

    assert not (tmp_path / ".env").exists()


def test_create_env_still_copies_the_example_on_a_native_install(tmp_path, monkeypatch):
    setup_module = _load_setup_module()
    (tmp_path / ".env.example").write_text("LLM_HOST=localhost\n", encoding="utf-8")
    monkeypatch.setattr(setup_module, "BASE_DIR", str(tmp_path))
    monkeypatch.delenv("ODYSSEUS_SKIP_ENV_FILE", raising=False)

    setup_module.create_env()

    assert (tmp_path / ".env").read_text(encoding="utf-8") == "LLM_HOST=localhost\n"
