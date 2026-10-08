"""Upgrade contracts: old inputs still work; new exports/setup are canonical."""
import io
import json
from pathlib import Path
import runpy
import subprocess
import sys
import zipfile

import pytest
from tests import REPO_ROOT
from src.agent_profile_transfer import _check_document, FORMAT
from src.client_bundle import add_client_member


@pytest.mark.parametrize('brand', ['odysseus', 'agamemnon'])
def test_profile_formats_import(brand):
    assert _check_document({'format': brand + '-agent-profiles', 'version': 1, 'profiles': []}) == []
    assert FORMAT == 'agamemnon-agent-profiles'


def test_bundle_has_legacy_and_canonical_skill_and_helper_paths():
    root = REPO_ROOT / 'clients' / 'codex'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as archive:
        for path in root.rglob('*'):
            if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
                add_client_member(archive, path, Path('odysseus') / path.relative_to(root))
    with zipfile.ZipFile(buf) as archive:
        assert 'odysseus/scripts/odysseus_api.py' in archive.namelist()
        assert 'agamemnon/scripts/agamemnon_api.py' in archive.namelist()
        skill = archive.read('agamemnon/skills/agamemnon/SKILL.md').decode()
        assert 'name: agamemnon' in skill
        assert 'AGAMEMNON_URL' in skill
        assert 'plugins/agamemnon/scripts/agamemnon_api.py' in skill
        assert json.loads(archive.read('agamemnon/.codex-plugin/plugin.json'))['name'] == 'agamemnon'
        assert json.loads(archive.read('odysseus/.codex-plugin/plugin.json'))['name'] == 'odysseus'


@pytest.mark.parametrize('helper', ['clients/codex/scripts/odysseus_api.py',
                                  'clients/claude/skills/odysseus/scripts/odysseus_api.py'])
def test_client_env_aliases(helper, monkeypatch):
    config = runpy.run_path(str(REPO_ROOT / helper))['_config']
    for name in ['AGAMEMNON_URL', 'AGAMEMNON_API_TOKEN']:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('ODYSSEUS_URL', 'http://legacy/')
    monkeypatch.setenv('ODYSSEUS_API_TOKEN', 'legacy-token')
    assert config() == ('http://legacy', 'legacy-token')
    monkeypatch.setenv('AGAMEMNON_URL', 'http://canonical/')
    monkeypatch.setenv('AGAMEMNON_API_TOKEN', 'canonical-token')
    assert config() == ('http://canonical', 'canonical-token')


def test_canonical_dispatcher_lists_same_commands_and_rejects_traversal():
    cmd = [sys.executable, str(REPO_ROOT / 'scripts/agamemnon')]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    assert 'Agamemnon' in result.stdout and 'notes' in result.stdout
    assert subprocess.run([*cmd, '../app.py'], capture_output=True, timeout=10).returncode == 1
