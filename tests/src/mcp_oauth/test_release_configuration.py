"""Release setup contracts: runtime public URL and closed credential routing."""
from pathlib import Path
import pytest
import yaml
from src import mcp_oauth, github_credentials, settings


def test_mcp_redirect_uses_saved_public_url_at_provider_creation(monkeypatch):
    monkeypatch.delenv('OAUTH_REDIRECT_BASE_URL', raising=False)
    monkeypatch.setenv('APP_PUBLIC_URL', 'https://old.example')
    saved = {'url': 'https://first.example'}
    monkeypatch.setattr(settings, 'get_setting_or_env', lambda *a: saved['url'])
    first = mcp_oauth.build_provider('fresh', 'https://server.example/mcp')
    saved['url'] = 'https://second.example/'
    second = mcp_oauth.build_provider('fresh', 'https://server.example/mcp')
    assert str(first.context.client_metadata.redirect_uris[0]) == 'https://first.example/api/mcp/oauth/callback'
    assert str(second.context.client_metadata.redirect_uris[0]) == 'https://second.example/api/mcp/oauth/callback'


@pytest.mark.parametrize('host', ['http://enterprise.example', 'https://user:secret@enterprise.example', 'https://enterprise.example/path', 'enterprise.example:bad'])
def test_invalid_github_host_never_routes_to_public_api(monkeypatch, host):
    monkeypatch.setenv('GITHUB_HOST', host)
    with pytest.raises(ValueError, match='Invalid GITHUB_HOST'):
        github_credentials.github_api_base()


@pytest.mark.parametrize('filename', ['docker-compose.yml', 'docker-compose.gpu-amd.yml', 'docker-compose.gpu-nvidia.yml'])
def test_compose_forwards_canonical_alias_for_every_legacy_name(filename):
    root = Path(__file__).resolve().parents[3]
    env = yaml.safe_load((root / filename).read_text())['services']['odysseus']['environment']
    names = {entry.split('=')[0] for entry in env}
    for name in names:
        if name.startswith('ODYSSEUS_'):
            assert 'AGAMEMNON_' + name[len('ODYSSEUS_'):] in names, name
