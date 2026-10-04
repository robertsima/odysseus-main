"""Brain skill package resources stay owner-scoped, safe and versioned."""
import pytest
from fastapi import HTTPException
from services.memory.skill_format import Skill
from services.memory.skills import SkillsManager
from routes.skills_routes import setup_skills_routes
from tests.test_skills_routes_owner_update import _write_skill_md, _request, _route_handler


@pytest.mark.asyncio
async def test_package_roundtrip_routes_and_single_file_compatibility(tmp_path):
    _write_skill_md(tmp_path / 'skills', 'personal', 'my-skill', 'alice')
    sm = SkillsManager(str(tmp_path))
    router = setup_skills_routes(sm)
    listing = _route_handler(router, '/api/skills/{skill_id}/package', 'GET')
    reader = _route_handler(router, '/api/skills/{skill_id}/package/file', 'GET')
    writer = _route_handler(router, '/api/skills/{skill_id}/package/file', 'PUT')
    assert await listing(_request('alice'), 'my-skill') == {'files': []}
    saved = await writer(_request('alice', {'content': 'Use #skill-other-skill\n', 'version': 'new'}), 'my-skill', 'references/guide.md')
    assert saved['content'] == 'Use #skill-other-skill\n'
    assert (await reader(_request('alice'), 'my-skill', 'references/guide.md'))['version'] == saved['version']
    updated = await writer(_request('alice', {'content': 'revised', 'version': saved['version']}), 'my-skill', 'references/guide.md')
    assert updated['content'] == 'revised'
    assert (await listing(_request('alice'), 'my-skill'))['files'] == [{'path': 'references/guide.md', 'bytes': 7}]
    assert sm.read_skill_reference('my-skill', 'references/guide.md', owner='alice') == 'revised'
    assert sm.read_skill_md('my-skill', owner='alice') is not None
    with pytest.raises(HTTPException) as conflict:
        await writer(_request('alice', {'content': 'lost update', 'version': saved['version']}), 'my-skill', 'references/guide.md')
    assert conflict.value.status_code == 409


@pytest.mark.asyncio
async def test_package_rejects_foreign_owner_bundled_and_unsafe_paths(tmp_path):
    _write_skill_md(tmp_path / 'skills', 'personal', 'mine', 'alice')
    _write_skill_md(tmp_path / 'skills', 'personal', 'other', 'bob')
    sm = SkillsManager(str(tmp_path))
    router = setup_skills_routes(sm)
    reader = _route_handler(router, '/api/skills/{skill_id}/package/file', 'GET')
    writer = _route_handler(router, '/api/skills/{skill_id}/package/file', 'PUT')
    for name in ('other', 'missing'):
        with pytest.raises(HTTPException) as error:
            await writer(_request('alice', {'content': 'x', 'version': 'new'}), name, 'scripts/check.py')
        assert error.value.status_code == 404
    for path in ('../escape.md', '/tmp/escape.md', 'SKILL.md', 'scripts/../../escape.py', '.private.txt', 'images/photo.png'):
        with pytest.raises(HTTPException) as error:
            await writer(_request('alice', {'content': 'x', 'version': 'new'}), 'mine', path)
        assert error.value.status_code == 400
    folder = tmp_path / 'skills' / 'personal' / 'mine'
    (folder / 'scripts').symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(HTTPException) as error:
        await writer(_request('alice', {'content': 'x', 'version': 'new'}), 'mine', 'scripts/escape.py')
    assert error.value.status_code == 400
    with pytest.raises(HTTPException) as error:
        await reader(_request('alice'), 'other', 'references/guide.md')
    assert error.value.status_code == 404


def test_package_survives_skill_frontmatter_update_and_directory_move(tmp_path):
    _write_skill_md(tmp_path / 'skills', 'personal', 'mine', 'alice')
    sm = SkillsManager(str(tmp_path))
    sm.save_package_file('mine', 'scripts/check.py', 'print(1)\n', 'new', owner='alice')
    assert sm.update_skill('mine', {'category': 'updated', 'version': '1.1.0'}, owner='alice')
    assert sm.package_file('mine', 'scripts/check.py', owner='alice')['content'] == 'print(1)\n'


@pytest.mark.asyncio
async def test_related_links_roundtrip_and_conflict(tmp_path):
    _write_skill_md(tmp_path / 'skills', 'personal', 'mine', 'alice')
    _write_skill_md(tmp_path / 'skills', 'personal', 'other', 'alice')
    sm = SkillsManager(str(tmp_path))
    sm.save_package_file('mine', 'scripts/check.py', 'print(1)', 'new', owner='alice')
    router = setup_skills_routes(sm)
    read = _route_handler(router, '/api/skills/{skill_id}/markdown', 'GET')
    write = _route_handler(router, '/api/skills/{skill_id}/links', 'PUT')
    md = await read(_request('alice'), 'mine')
    payload = {'version': md['version'], 'related_skills': ['other'], 'related_scripts': ['scripts/check.py']}
    saved = await write(_request('alice', payload), 'mine')
    assert saved['version'] != md['version']
    assert saved['related_skills'] == ['other']
    assert next(x for x in sm.load(owner='alice') if x['name'] == 'mine')['related_scripts'] == ['scripts/check.py']
    parsed = Skill.from_markdown(saved['markdown'])
    assert parsed.related_skills == ['other']
    assert parsed.related_scripts == ['scripts/check.py']
    with pytest.raises(HTTPException) as error:
        await write(_request('alice', payload), 'mine')
    assert error.value.status_code == 409
    assert sm.update_skill('mine', {'category': 'updated'}, owner='alice')
    assert next(x for x in sm.load(owner='alice') if x['name'] == 'mine')['related_scripts'] == ['scripts/check.py']
    # A deleted destination is shown as missing in the UI and can be unlinked.
    assert sm.delete_skill('other', owner='alice')
    (tmp_path / 'skills' / 'updated' / 'mine' / 'scripts' / 'check.py').unlink()
    latest = await read(_request('alice'), 'mine')
    cleared = await write(_request('alice', {'version': latest['version'], 'related_skills': [],
                                               'related_scripts': []}), 'mine')
    assert cleared['related_scripts'] == []


@pytest.mark.asyncio
async def test_related_links_validate_destinations_and_markdown_guard(tmp_path):
    _write_skill_md(tmp_path / 'skills', 'personal', 'mine', 'alice')
    _write_skill_md(tmp_path / 'skills', 'personal', 'other', 'bob')
    sm = SkillsManager(str(tmp_path))
    sm.save_package_file('mine', 'scripts/check.py', '# safe', 'new', owner='alice')
    router = setup_skills_routes(sm)
    read = _route_handler(router, '/api/skills/{skill_id}/markdown', 'GET')
    links = _route_handler(router, '/api/skills/{skill_id}/links', 'PUT')
    markdown = _route_handler(router, '/api/skills/{skill_id}/markdown', 'POST')
    md = await read(_request('alice'), 'mine')
    for bad_skills, bad_scripts in [(['other'], []), (['mine'], []), ([], ['scripts/missing.py']),
                                    ([], ['../other.py']), ([], ['references/guide.md']),
                                    (['other'] * 33, []), ([], ['scripts/check.py'] * 2)]:
        with pytest.raises(HTTPException) as error:
            await links(_request('alice', {'version': md['version'], 'related_skills': bad_skills,
                                           'related_scripts': bad_scripts}), 'mine')
        assert error.value.status_code == 400
    skill_dir = tmp_path / 'skills' / 'personal' / 'mine'
    (skill_dir / 'scripts' / 'linked.py').symlink_to(skill_dir / 'scripts' / 'check.py')
    with pytest.raises(HTTPException) as error:
        await links(_request('alice', {'version': md['version'], 'related_skills': [],
                                       'related_scripts': ['scripts/linked.py']}), 'mine')
    assert error.value.status_code == 400
    forged = md['markdown'].replace('source: learned', 'source: bundled').replace('owner: alice', 'owner: bob')
    await markdown(_request('alice', {'markdown': forged, 'version': md['version']}), 'mine')
    assert next(x for x in sm.load(owner='alice') if x['name'] == 'mine')['owner'] == 'alice'
    assert 'source: learned' in sm.read_skill_md('mine', owner='alice')
    with pytest.raises(HTTPException) as error:
        await markdown(_request('alice', {'markdown': forged, 'version': md['version']}), 'mine')
    assert error.value.status_code == 409
    with pytest.raises(HTTPException) as error:
        await links(_request('bob', {'version': md['version'], 'related_skills': [], 'related_scripts': []}), 'mine')
    assert error.value.status_code == 404
