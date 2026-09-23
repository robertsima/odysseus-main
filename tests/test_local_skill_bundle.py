from pathlib import Path

import pytest

from services.memory.local_skill_bundle import read_local_skill_bundle
from services.memory.skill_importer import SkillImportError


def test_read_one_local_skill_and_relative_resources(tmp_path):
    root = tmp_path / '.promptscript' / 'skills' / 'example'
    root.mkdir(parents=True)
    (root / 'SKILL.md').write_text('---\nname: example\n---\nUse references/readme.md', encoding='utf-8')
    (root / 'references').mkdir()
    (root / 'references' / 'readme.md').write_text('review first', encoding='utf-8')
    assert set(read_local_skill_bundle(root)) == {'SKILL.md', 'references/readme.md'}
    with pytest.raises(SkillImportError, match='one skill'):
        read_local_skill_bundle(root.parent)


def test_no_silent_omission_of_local_binary_resource(tmp_path):
    (tmp_path / 'SKILL.md').write_text('example', encoding='utf-8')
    (tmp_path / 'program.exe').write_bytes(b'not supported')
    with pytest.raises(SkillImportError, match='non-text'):
        read_local_skill_bundle(tmp_path)


def test_symlink_resources_are_rejected(tmp_path):
    root = tmp_path / 'skill'
    root.mkdir()
    (root / 'SKILL.md').write_text('example', encoding='utf-8')
    outside = tmp_path / 'private.md'
    outside.write_text('private', encoding='utf-8')
    try:
        (root / 'ref.md').symlink_to(outside)
    except OSError:
        pytest.skip('Host cannot create symlinks')
    with pytest.raises(SkillImportError, match='Symlinks'):
        read_local_skill_bundle(root)


def test_toolchain_lockfile_and_image_contract():
    import json
    root = Path(__file__).resolve().parents[1]
    package = json.loads((root / 'package.json').read_text())
    lock = json.loads((root / 'package-lock.json').read_text())
    for name in ('@promptscript/cli', 'skills'):
        assert package['dependencies'][name] == lock['packages'][f'node_modules/{name}']['version']
    dockerfile = (root / 'Dockerfile').read_text()
    assert 'npm ci --omit=dev --ignore-scripts' in dockerfile
    assert 'PROMPTSCRIPT_TELEMETRY=false' in dockerfile
    assert '/opt/odysseus-skill-tools/node_modules/.bin' in dockerfile


def test_skill_cli_local_import_requires_review_and_owner(monkeypatch):
    from tests.helpers.cli_loader import load_script
    cli = load_script('odysseus-skills')
    with pytest.raises(SystemExit):
        cli._build_parser().parse_args(['import-local', 'somewhere'])
    args = cli._build_parser().parse_args(['import-local', 'somewhere', '--owner', 'alice'])
    assert args.reviewed is False
