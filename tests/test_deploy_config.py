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
