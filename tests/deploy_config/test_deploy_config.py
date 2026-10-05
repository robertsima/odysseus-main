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
from tests import REPO_ROOT

ROOT = REPO_ROOT
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


# ── what the compose files forward ───────────────────────────────


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


def test_zimaos_template_leaves_security_switches_to_code_defaults():
    env = _env(ZIMAOS_TEMPLATE)
    # SECURE_COOKIES=false forced non-Secure cookies over HTTPS, while the code
    # derives the flag from the request scheme; the others are security switches
    # whose code defaults must not be overridden by the template.
    for name in ("SECURE_COOKIES", "AUTH_ENABLED", "LOCALHOST_BYPASS", "ALLOWED_ORIGINS"):
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


# ── .env.example ─────────────────────────────────────────────────


def _env_example() -> str:
    if not ENV_EXAMPLE.exists():
        pytest.skip("this checkout does not include the optional .env.example file")
    return ENV_EXAMPLE.read_text(encoding="utf-8")


def test_env_example_explains_that_docker_only_forwards_listed_variables():
    header = _env_example().split("# LLM Configuration", 1)[0]
    assert "env_file" in header
    assert "reaches the container only if" in header
    for tag in ("[Docker: forwarded]", "[Docker: compose]", "[native only]"):
        assert tag in header, tag


def test_env_example_points_at_docs_that_exist():
    text = _env_example()
    assert "docs/configuration.md" not in text.replace("website/configuration.md", "")
    for ref in set(re.findall(r"website/[a-z0-9_-]+\.md", text)):
        assert (ROOT / ref).is_file(), ref


@pytest.mark.parametrize(
    "name",
    DEAD_VARIABLES + (
        "CLEANUP_ENABLED",
        "CLEANUP_INTERVAL_HOURS",
        "ODYSSEUS_STT_ENABLED",
        "ODYSSEUS_STT_DEFAULT_PROVIDER",
        "ODYSSEUS_STT_MODEL",
        "CLAUDE_CODE_AUTO_UPDATE",
        "CLAUDE_OAUTH_CLIENT_ID",
        "ODYSSEUS_PERSONAL_DIR",
    ),
)
def test_env_example_does_not_offer_dead_variables(name):
    assert not re.search(rf"^#?\s*{name}=", _env_example(), re.MULTILINE), name


def test_env_example_does_not_claim_rag_threshold_moved_to_settings():
    text = _env_example()
    # Still read only from the environment (src/chat_processor.py).
    assert "# RAG_SIMILARITY_THRESHOLD=" in text
    assert "RAG_SIMILARITY_THRESHOLD variables" not in text


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
