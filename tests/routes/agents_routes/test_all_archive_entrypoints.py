"""Sidebar bulk actions and tools must not bypass unit lifecycle safety."""
import asyncio

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session as SqlSession

from core import database
from src import agent_activity as activity
from src.agent_tools import session_tools
from src.tool_approvals import tool_approval_store
from src.tool_capabilities import capabilities_for_action
from tests.routes.agents_routes.test_archive_takes_the_unit import unit, overview


@pytest.fixture(params=['sidebar', 'tool'])
def invoke(request, unit, api, monkeypatch):
    alice, parent, workers = unit
    monkeypatch.setattr(session_tools, 'get_session_manager', lambda: api.session_manager)
    def call(action, sid=parent):
        if request.param == 'sidebar':
            return alice.post(f'/api/session/{sid}/{action}').status_code
        result = asyncio.run(session_tools.manage_session(f'{action}\n{sid}', owner='alice'))
        return 409 if result.get('exit_code') else 200
    return call


@pytest.mark.security
def test_finished_turn_approval_blocks_sidebar_bulk_and_tool(unit, invoke):
    alice, parent, workers = unit
    sid = workers[0]
    tool_approval_store.create(owner='alice', session_id=sid, origin_run_id='finished',
        tool_name='bash', content='echo reviewed', workspace=None,
        external_untrusted_context_seen=True,
        capabilities=capabilities_for_action('bash', 'echo reviewed'))
    try:
        assert invoke('archive') == 409
        assert overview(alice, archived=True) == {}
    finally:
        tool_approval_store.retire_for_session(owner='alice', session_id=sid)


def test_active_child_blocks_sidebar_and_tool(unit, invoke):
    alice, parent, workers = unit
    activity.run_started(workers[0], 'session', 'Working', owner='alice')
    assert invoke('archive') == 409
    assert overview(alice, archived=True) == {}


def test_sidebar_and_tool_restore_whole_unit(unit, invoke):
    alice, parent, workers = unit
    assert invoke('archive') == 200
    assert set(overview(alice, archived=True)) == {parent, *workers}
    assert invoke('unarchive', workers[0]) == 200
    assert overview(alice, archived=True) == {}


def test_sidebar_and_tool_write_failure_is_atomic(unit, invoke, api):
    alice, parent, workers = unit
    def fail(db, context, instances):
        if any(isinstance(row, database.Session) and row.archived for row in db.dirty):
            raise RuntimeError('write failed')
    event.listen(SqlSession, 'before_flush', fail)
    try:
        assert invoke('archive') in (409, 500)
    finally:
        event.remove(SqlSession, 'before_flush', fail)
    assert overview(alice, archived=True) == {}
    assert all(not api.session_manager.sessions[sid].archived for sid in (parent, *workers))
