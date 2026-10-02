"""Persistent, owner-scoped Agamemnon parent appearance contract."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest
from fastapi import HTTPException

from routes import agents_routes

ROOT = Path(__file__).resolve().parents[1]


class Manager:
    def get_sessions_for_user(self, user):
        return {'mine': SimpleNamespace(id='mine', name='Commander', model='claude', archived=False)}


def request(appearance):
    async def json():
        return {'appearance': appearance}
    return SimpleNamespace(json=json)


def endpoint(router):
    return next(route.endpoint for route in router.routes if route.path == '/api/agents/sessions/{session_id}/appearance')


def test_appearance_is_durable_owner_scoped_and_resettable(monkeypatch):
    from core import database
    settings = {'mine': {}}
    monkeypatch.setattr(agents_routes, 'effective_user', lambda req: 'alice')
    monkeypatch.setattr(database, 'get_session_settings', lambda sid: settings[sid])
    def update(sid, patch):
        settings[sid].update(patch)
        return settings[sid]
    monkeypatch.setattr(database, 'update_session_settings', update)
    handler = endpoint(agents_routes.setup_agents_routes(Manager()))
    assert asyncio.run(handler(request('sword'), 'mine')) == {'appearance': 'sword'}
    assert settings['mine']['agamemnon_soldier_appearance'] == 'sword'
    assert asyncio.run(handler(request('helmet'), 'mine')) == {'appearance': 'helmet'}
    with pytest.raises(HTTPException) as bad:
        asyncio.run(handler(request('not-a-soldier'), 'mine'))
    assert bad.value.status_code == 400
    with pytest.raises(HTTPException) as other:
        asyncio.run(handler(request('sword'), 'someone-else'))
    assert other.value.status_code == 404
    settings['mine']['parent_session'] = 'parent'
    with pytest.raises(HTTPException) as child:
        asyncio.run(handler(request('sword'), 'mine'))
    assert child.value.status_code == 400
    settings['mine'].pop('parent_session')
    assert asyncio.run(handler(request(None), 'mine')) == {'appearance': None}
    assert settings['mine']['agamemnon_soldier_appearance'] is None


def test_artwork_and_runtime_picker_contract():
    ns = '{http://www.w3.org/2000/svg}'
    sheet = ET.parse(ROOT / 'static/branding/agamemnon-agent-marks.svg').getroot()
    symbols = {node.attrib['id']: ET.tostring(node) for node in sheet if node.tag == ns + 'symbol'}
    assert {f'soldier-{variant}' for variant in agents_routes.SOLDIER_APPEARANCES} <= symbols.keys()
    assert symbols['soldier-sword'] != symbols['soldier-helmet']
    markup = (ROOT / 'static/index.html').read_text()
    for variant in agents_routes.SOLDIER_APPEARANCES:
        assert f'id="soldier-{variant}"' in markup
    js = (ROOT / 'static/js/agentsDashboard.js').read_text()
    assert 'row.soldier_appearance = choice' in js
    assert 'agent?.soldier_appearance' in js
    assert 'appearancePickerHtml(r)' in js
    assert 'input[name="ag-appearance"]' in js
    assert 'aria-live="polite"' in js
    assert 'if (agent.parent_session) return' in js
    assert "document.documentElement.dataset.theme !== 'dark'" in js
