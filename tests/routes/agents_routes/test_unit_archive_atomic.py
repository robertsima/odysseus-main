"""Archive safety with real approval records and transaction fault injection."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session as SqlSession

from core import database
from src import agent_control
from src.tool_approvals import tool_approval_store
from src.tool_capabilities import capabilities_for_action
from tests.routes.agents_routes.test_archive_takes_the_unit import unit, overview


def test_pending_approval_after_finished_turn_blocks_archive(unit):
    alice, parent, workers = unit
    sid = workers[0]
    approval = tool_approval_store.create(
        owner='alice', session_id=sid, origin_run_id='finished-turn',
        tool_name='bash', content='echo reviewed', workspace=None,
        external_untrusted_context_seen=True,
        capabilities=capabilities_for_action('bash', 'echo reviewed'))
    try:
        assert alice.post(f'/api/agents/sessions/{parent}/archive').status_code == 409
        assert overview(alice, archived=True) == {}
    finally:
        tool_approval_store.retire_for_session(owner='alice', session_id=sid)


def test_write_failure_rolls_back_flags_markers_and_cache(unit, api):
    alice, parent, workers = unit
    def fail(db, context, instances):
        if any(isinstance(row, database.Session) and row.archived for row in db.dirty):
            raise RuntimeError('injected archive write failure')
    event.listen(SqlSession, 'before_flush', fail)
    try:
        assert alice.post(f'/api/agents/sessions/{parent}/archive').status_code == 500
    finally:
        event.remove(SqlSession, 'before_flush', fail)
    with database.get_db_session() as db:
        rows = db.query(database.Session).filter(database.Session.id.in_({parent, *workers})).all()
        assert all(not row.archived for row in rows)
    assert all(not api.session_manager.sessions[sid].archived for sid in (parent, *workers))
    assert all('archived_with' not in database.get_session_settings(sid) for sid in workers)


def test_nested_and_independently_archived_subunits_keep_restore_decisions(unit, api):
    alice, parent, workers = unit
    grandchild = alice.post('/api/session', data={'name': 'Nested child', 'skip_validation': 'true'}).json()['id']
    database.update_session_settings(grandchild, {'parent_session': workers[0]})
    assert alice.post(f'/api/agents/sessions/{workers[0]}/archive').status_code == 200
    # Simulate restart: independently archived rows no longer resident.
    api.session_manager.sessions.pop(workers[0], None)
    api.session_manager.sessions.pop(grandchild, None)
    assert alice.post(f'/api/agents/sessions/{parent}/archive').status_code == 200
    assert database.get_session_settings(grandchild)['archived_with'] == workers[0]
    assert alice.post(f'/api/agents/sessions/{parent}/unarchive').status_code == 200
    archived = overview(alice, archived=True)
    assert workers[0] in archived and grandchild in archived
    assert alice.post(f'/api/agents/sessions/{workers[0]}/unarchive').status_code == 200
    assert overview(alice, archived=True) == {}


def test_launch_waits_for_archive_commit_then_refuses_archived_parent(unit, monkeypatch):
    alice, parent, workers = unit
    entered, release, launching = Event(), Event(), Event()
    real = database.archive_session_unit
    def paused(*args):
        entered.set()
        assert release.wait(5)
        return real(*args)
    monkeypatch.setattr(database, 'archive_session_unit', paused)

    def launch():
        launching.set()
        return asyncio.run(agent_control.launch_worker(owner='alice', parent_session=parent, task='test'))
    with ThreadPoolExecutor(2) as pool:
        archive = pool.submit(alice.post, f'/api/agents/sessions/{parent}/archive')
        assert entered.wait(5)
        worker = pool.submit(launch)
        assert launching.wait(5)
        assert not worker.done()
        release.set()
        assert archive.result(timeout=5).status_code == 200
        with pytest.raises(ValueError, match='Restore the parent'):
            worker.result(timeout=5)
