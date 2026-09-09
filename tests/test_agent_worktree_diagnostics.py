"""The doctor command must name the actual mistake, not just say "unauthorized".

Each test pins one real misconfiguration to the specific message an operator
needs, and asserts that no credential value is ever echoed back.
"""

import os

import pytest

from src.agent_worktree import diagnostics
from src.agent_worktree.config import load_config

pytestmark = pytest.mark.area_security


_ENV = (
    "ODYSSEUS_AGENT_PUBLISH_ENABLED", "ODYSSEUS_AGENT_REPO", "ODYSSEUS_AGENT_SOURCE_REPO",
    "ODYSSEUS_AGENT_WORKTREE_ROOT", "ODYSSEUS_AGENT_STATE_DIR", "ODYSSEUS_AGENT_GITHUB_TOKEN",
    "ODYSSEUS_GITHUB_APP_ID", "ODYSSEUS_GITHUB_APP_INSTALLATION_ID",
    "ODYSSEUS_GITHUB_APP_PRIVATE_KEY_PATH",
)


@pytest.fixture
def env(monkeypatch, tmp_path):
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setenv("ODYSSEUS_AGENT_SOURCE_REPO", str(repo))
    monkeypatch.setenv("ODYSSEUS_AGENT_WORKTREE_ROOT", str(tmp_path / "wt"))
    monkeypatch.setenv("ODYSSEUS_AGENT_STATE_DIR", str(tmp_path / "state"))
    return monkeypatch


def _by_name(report, name):
    for check in report["checks"]:
        if check["name"] == name:
            return check
    raise AssertionError(f"no check named {name!r}: {[c['name'] for c in report['checks']]}")


async def test_nothing_configured_reports_the_flag_and_the_repo(env):
    report = await diagnostics.run_diagnostics(load_config(), offline=True)
    assert report["ok"] is False
    assert _by_name(report, "publish flag")["status"] == "fail"
    assert _by_name(report, "repository")["status"] == "fail"


async def test_a_client_id_in_the_app_id_slot_is_named(env):
    env.setenv("ODYSSEUS_AGENT_PUBLISH_ENABLED", "1")
    env.setenv("ODYSSEUS_AGENT_REPO", "acme/widgets")
    env.setenv("ODYSSEUS_GITHUB_APP_ID", "Iv23liAbCdEf")
    env.setenv("ODYSSEUS_GITHUB_APP_INSTALLATION_ID", "123")
    env.setenv("ODYSSEUS_GITHUB_APP_PRIVATE_KEY_PATH", "/nope.pem")
    report = await diagnostics.run_diagnostics(load_config(), offline=True)
    check = _by_name(report, "app id")
    assert check["status"] == "fail"
    assert "Client ID" in check["hint"]


async def test_a_missing_key_file_points_at_the_mount(env):
    env.setenv("ODYSSEUS_AGENT_PUBLISH_ENABLED", "1")
    env.setenv("ODYSSEUS_AGENT_REPO", "acme/widgets")
    env.setenv("ODYSSEUS_GITHUB_APP_ID", "123456")
    env.setenv("ODYSSEUS_GITHUB_APP_INSTALLATION_ID", "789")
    env.setenv("ODYSSEUS_GITHUB_APP_PRIVATE_KEY_PATH", "/etc/odysseus/missing.pem")
    check = _by_name(await diagnostics.run_diagnostics(load_config(), offline=True), "private key")
    assert check["status"] == "fail"
    assert "mounted volume" in check["hint"]


async def test_a_public_key_file_is_diagnosed(env, tmp_path):
    key = tmp_path / "wrong.pem"
    key.write_bytes(b"-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----\n")
    env.setenv("ODYSSEUS_AGENT_PUBLISH_ENABLED", "1")
    env.setenv("ODYSSEUS_AGENT_REPO", "acme/widgets")
    env.setenv("ODYSSEUS_GITHUB_APP_ID", "123456")
    env.setenv("ODYSSEUS_GITHUB_APP_INSTALLATION_ID", "789")
    env.setenv("ODYSSEUS_GITHUB_APP_PRIVATE_KEY_PATH", str(key))
    check = _by_name(await diagnostics.run_diagnostics(load_config(), offline=True), "private key")
    assert check["status"] == "fail"
    assert "public key" in check["detail"]


async def test_a_real_rsa_key_passes_and_reports_its_size(env, tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp_path / "app.pem"
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    os.chmod(path, 0o600)
    env.setenv("ODYSSEUS_AGENT_PUBLISH_ENABLED", "1")
    env.setenv("ODYSSEUS_AGENT_REPO", "acme/widgets")
    env.setenv("ODYSSEUS_GITHUB_APP_ID", "123456")
    env.setenv("ODYSSEUS_GITHUB_APP_INSTALLATION_ID", "789")
    env.setenv("ODYSSEUS_GITHUB_APP_PRIVATE_KEY_PATH", str(path))
    check = _by_name(await diagnostics.run_diagnostics(load_config(), offline=True), "private key")
    assert check["status"] == "ok"
    assert check["data"]["bits"] == 2048


async def test_a_world_readable_key_warns_but_does_not_fail(env, tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp_path / "app.pem"
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    os.chmod(path, 0o644)
    env.setenv("ODYSSEUS_AGENT_PUBLISH_ENABLED", "1")
    env.setenv("ODYSSEUS_AGENT_REPO", "acme/widgets")
    env.setenv("ODYSSEUS_GITHUB_APP_ID", "123456")
    env.setenv("ODYSSEUS_GITHUB_APP_INSTALLATION_ID", "789")
    env.setenv("ODYSSEUS_GITHUB_APP_PRIVATE_KEY_PATH", str(path))
    check = _by_name(await diagnostics.run_diagnostics(load_config(), offline=True), "private key")
    assert check["status"] == "warn"


@pytest.mark.parametrize(
    "value,expected_fragment",
    [
        ("github_pat_" + "a" * 30, "fine-grained"),
        ("ghp_" + "a" * 36, "classic"),
        ("ghs_" + "a" * 36, "installation token"),
        ("Iv23liAbCdEf", "CLIENT ID"),
        ("a" * 40, "client SECRET"),
        ("", "empty"),
    ],
)
def test_token_shapes_are_named_without_printing_the_value(value, expected_fragment):
    result = diagnostics.classify_token(value)
    assert expected_fragment in result
    if value:
        assert value not in result


async def test_a_client_secret_pasted_as_the_token_is_rejected(env):
    env.setenv("ODYSSEUS_AGENT_PUBLISH_ENABLED", "1")
    env.setenv("ODYSSEUS_AGENT_REPO", "acme/widgets")
    env.setenv("ODYSSEUS_AGENT_GITHUB_TOKEN", "b" * 40)
    check = _by_name(
        await diagnostics.run_diagnostics(load_config(), offline=True), "credential mode"
    )
    assert check["status"] == "fail"
    assert "client SECRET" in check["detail"]


async def test_the_report_never_contains_the_token(env):
    import json

    secret = "github_pat_" + "z" * 30
    env.setenv("ODYSSEUS_AGENT_PUBLISH_ENABLED", "1")
    env.setenv("ODYSSEUS_AGENT_REPO", "acme/widgets")
    env.setenv("ODYSSEUS_AGENT_GITHUB_TOKEN", secret)
    report = await diagnostics.run_diagnostics(load_config(), offline=True)
    assert secret not in json.dumps(report)


async def test_network_checks_are_skipped_when_credentials_are_broken(env):
    env.setenv("ODYSSEUS_AGENT_PUBLISH_ENABLED", "1")
    env.setenv("ODYSSEUS_AGENT_REPO", "acme/widgets")
    report = await diagnostics.run_diagnostics(load_config())
    assert _by_name(report, "github")["status"] == "skip"


async def test_a_non_checkout_app_root_names_real_candidates(env, tmp_path, monkeypatch):
    """The container image has no .git, so doctor must point at the data-dir
    checkout the agent actually works in rather than just saying "not a repo"."""
    from src import constants

    data = tmp_path / "data"
    (data / "development" / "odysseus-main" / ".git").mkdir(parents=True)
    (data / "development" / "odysseus-main" / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = https://github.com/acme/widgets.git\n'
    )
    (data / "development" / "other-repo" / ".git").mkdir(parents=True)
    monkeypatch.setattr(constants, "DATA_DIR", str(data))

    env.setenv("ODYSSEUS_AGENT_PUBLISH_ENABLED", "1")
    env.setenv("ODYSSEUS_AGENT_REPO", "acme/widgets")
    env.setenv("ODYSSEUS_AGENT_SOURCE_REPO", str(tmp_path / "not-a-repo"))

    check = _by_name(
        await diagnostics.run_diagnostics(load_config(), offline=True), "source repository"
    )
    assert check["status"] == "fail"
    candidates = check["data"]["candidates"]
    assert any("odysseus-main" in c for c in candidates)
    # The checkout whose origin matches the configured repo is offered first.
    assert "odysseus-main" in candidates[0]
    assert "ODYSSEUS_AGENT_SOURCE_REPO=" in check["hint"]


def test_checkout_discovery_ignores_directories_that_are_not_repos(tmp_path, monkeypatch):
    from src import constants

    data = tmp_path / "data"
    (data / "development" / "real" / ".git").mkdir(parents=True)
    (data / "development" / "just-a-folder").mkdir(parents=True)
    monkeypatch.setattr(constants, "DATA_DIR", str(data))

    found = diagnostics.find_checkouts()
    assert any(p.endswith("real") for p in found)
    assert not any(p.endswith("just-a-folder") for p in found)
