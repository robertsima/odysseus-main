"""Today must explain its scope and keep source navigation after local snoozes."""
from datetime import datetime

import pytest

from .conftest import task


def test_snoozed_todoist_outcome_opens_its_source_even_without_live_task(world):
    admin = world.api.as_admin()
    world.td.tasks = [task('t1', 'Pay rent', '2026-10-06')]
    result = world.act(admin, key='todoist:t1', action='defer').json()
    world.td.tasks = []
    world.clock['now'] = datetime(2026, 10, 12, 10)
    opened = world.act(admin, key=f"outcome:{result['outcome_id']}", action='open')
    assert opened.status_code == 200
    assert opened.json()['open_url'] == 'https://app.todoist.com/app/task/t1'


def test_shortlist_reports_hidden_eligible_items_not_a_full_backlog(world):
    world.td.tasks = [task(str(i), f'Task {i}', '2026-10-06') for i in range(12)]
    result = world.get(world.api.as_admin())
    assert len(result['today']) == 5
    assert len(result['next']) == 3
    assert result['scope']['remaining'] == 4
    assert result['sources']['todoist'] == 'ready'


@pytest.mark.security
def test_todoist_status_does_not_offer_another_accounts_integration(world):
    result = world.get(world.api.as_user('alice'))
    assert result['sources']['todoist'] == 'restricted'
    assert world.td.calls == []


@pytest.mark.parametrize('stderr,status', [
    ('td is not configured', 'not_configured'),
    ('authentication expired', 'unavailable'),
])
def test_todoist_status_distinguishes_setup_from_a_failed_read(world, stderr, status):
    world.td.returncode = 1
    world.td.stderr = stderr
    result = world.get(world.api.as_admin())
    assert result['sources']['todoist'] == status
    assert ('todoist' in result['degraded']) == (status == 'unavailable')
