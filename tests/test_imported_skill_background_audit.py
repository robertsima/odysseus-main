import pytest

import routes.skills_routes as skills_routes
import src.builtin_actions as actions
import services.memory.skills as skills_module


IMPORTED_DRAFT = {
    "name": "unreviewed-import",
    "source": "imported",
    "status": "draft",
    "audit_verdict": None,
}


class Manager:
    def __init__(self, rows):
        self.rows = list(rows)

    def load(self, owner=None):
        return list(self.rows)


@pytest.mark.asyncio
async def test_nightly_audit_skips_imported_drafts_before_model_resolution(monkeypatch):
    monkeypatch.setattr(
        skills_routes, "_resolve_audit_models",
        lambda **kwargs: pytest.fail("model resolution must not run for imported drafts"),
    )
    result = await skills_routes.run_scheduled_skill_audit(
        Manager([IMPORTED_DRAFT]), owner="alice",
    )
    assert result == {"status": "done", "total": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize("action", [actions.action_test_skills, actions.action_audit_skills])
async def test_background_actions_do_not_run_unreviewed_imports(monkeypatch, action):
    manager = Manager([IMPORTED_DRAFT])
    monkeypatch.setattr(skills_module, "SkillsManager", lambda data_dir: manager)
    monkeypatch.setattr(
        skills_routes, "_resolve_audit_models",
        lambda **kwargs: pytest.fail("audit model must not resolve for imported drafts"),
    )
    import src.task_endpoint as task_endpoint
    monkeypatch.setattr(
        task_endpoint, "resolve_task_candidates",
        lambda **kwargs: pytest.fail("test model must not resolve for imported drafts"),
    )
    with pytest.raises(actions.TaskNoop):
        await action("alice")


@pytest.mark.asyncio
async def test_published_import_remains_eligible_for_nightly_audit(monkeypatch):
    row = dict(IMPORTED_DRAFT, status="published")
    captured = []
    monkeypatch.setattr(
        skills_routes, "_resolve_audit_models",
        lambda **kwargs: ("https://model.test/v1", "model", {}, None),
    )

    async def run_job(key, manager, names, *args):
        captured.extend(names)

    monkeypatch.setattr(skills_routes, "_run_audit_all_job", run_job)
    skills_routes._skill_audit_jobs.pop(("alice",), None)
    result = await skills_routes.run_scheduled_skill_audit(
        Manager([row]), owner="alice",
    )
    assert result["total"] == 1
    assert captured == ["unreviewed-import"]
