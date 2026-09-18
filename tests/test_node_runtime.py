"""Keep the deployed MCP runtime and frontend CI on the same supported Node."""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def test_docker_node_release_matches_local_and_ci_pin():
    version = (ROOT / ".nvmrc").read_text(encoding="utf-8").strip()
    assert re.fullmatch(r"\d+\.\d+\.\d+", version)
    assert tuple(map(int, version.split("."))) >= (22, 6, 0)
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert f"FROM node:{version}-bookworm-slim AS node-distribution" in dockerfile
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert 'node-version-file: ".nvmrc"' in workflow


def test_application_inherits_verified_node_runtime_not_debian_packages():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM python:3.14-slim AS node-runtime" in dockerfile
    assert "FROM node-runtime AS app" in dockerfile
    assert "node --version && npm --version && npx --version" in dockerfile
    assert "npm/bin/npm-cli.js /usr/local/bin/npm" in dockerfile
    assert "npm/bin/npx-cli.js /usr/local/bin/npx" in dockerfile
    assert not re.search(r"^\s+(nodejs|npm)\s+\\$", dockerfile, re.MULTILINE)
