import pytest

from services.memory.skill_import_review import SkillImportReviews


FILES = {'SKILL.md': '---\nname: example\n---\nReview me', 'references/a.md': 'reference'}


def test_review_is_owner_bound_single_use_snapshot():
    store = SkillImportReviews()
    files = dict(FILES)
    preview = store.inspect(files, owner='alice', source_url='https://github.com/a/b')
    files['SKILL.md'] = 'changed remote source'
    with pytest.raises(ValueError, match='this user'):
        store.consume(preview['review_token'], owner='bob')
    snapshot = store.consume(preview['review_token'], owner='alice')
    assert snapshot['files'] == FILES
    assert preview['sha256']
    with pytest.raises(ValueError):
        store.consume(preview['review_token'], owner='alice')


def test_review_capacity_and_expiry():
    now = [10]
    store = SkillImportReviews(ttl=5, capacity=2, clock=lambda: now[0])
    first = store.inspect(FILES, owner='alice', source_url='one')
    second = store.inspect(FILES, owner='bob', source_url='two')
    store.inspect(FILES, owner='charlie', source_url='three')
    with pytest.raises(ValueError):
        store.consume(first['review_token'], owner='alice')
    now[0] = 16
    with pytest.raises(ValueError, match='expired'):
        store.consume(second['review_token'], owner='bob')


@pytest.mark.asyncio
async def test_routes_preview_does_not_install_or_audit_confirm_is_draft(tmp_path, monkeypatch):
    from fastapi import HTTPException, Request
    from routes.skills_routes import setup_skills_routes, SkillImportUrlRequest, SkillImportConfirmRequest
    from services.memory.skills import SkillsManager
    from services.memory import skill_importer
    import routes.skills_routes as routes
    import src.event_bus as events
    from types import SimpleNamespace

    monkeypatch.setattr(routes, 'require_admin', lambda request: None)
    monkeypatch.setattr(skill_importer, 'fetch_skill_bundle', lambda url, skill=None: (FILES, None))
    fired = []
    monkeypatch.setattr(events, 'fire_event', lambda *args: fired.append(args))
    manager = SkillsManager(str(tmp_path))
    router = setup_skills_routes(manager)
    handlers = {route.path: route.endpoint for route in router.routes if 'POST' in route.methods}
    def request(owner):
        return Request({'type': 'http', 'headers': [], 'app': SimpleNamespace(), 'state': {'current_user': owner}})
    preview = await handlers['/api/skills/imports/inspect'](request('alice'), SkillImportUrlRequest(url='https://github.com/a/b'))
    assert not manager.load_all()
    confirm = SkillImportConfirmRequest(review_token=preview['review_token'])
    with pytest.raises(HTTPException) as exc:
        await handlers['/api/skills/imports/confirm'](request('bob'), confirm)
    assert exc.value.status_code == 400
    result = await handlers['/api/skills/imports/confirm'](request('alice'), confirm)
    assert result['skill']['status'] == 'draft'
    assert result['skill']['owner'] == 'alice'
    assert fired == []


@pytest.mark.asyncio
async def test_preview_requires_admin_before_network(tmp_path, monkeypatch):
    from fastapi import HTTPException, Request
    from routes.skills_routes import setup_skills_routes, SkillImportUrlRequest
    from services.memory.skills import SkillsManager
    import routes.skills_routes as routes
    def deny(request):
        raise HTTPException(403, 'Admin only')
    monkeypatch.setattr(routes, 'require_admin', deny)
    router = setup_skills_routes(SkillsManager(str(tmp_path)))
    handler = next(route.endpoint for route in router.routes if route.path == '/api/skills/imports/inspect')
    with pytest.raises(HTTPException) as exc:
        await handler(Request({'type': 'http', 'headers': []}), SkillImportUrlRequest(url='https://github.com/a/b'))
    assert exc.value.status_code == 403
