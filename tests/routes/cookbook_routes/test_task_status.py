"""GET /api/cookbook/tasks/status for download tasks that have finished.

A dependency install runs as a download task but prints only the runner's
exit line, not the Hugging Face DOWNLOAD_OK marker. The Running tab has to
show it as completed when it exited 0 and as an error otherwise; reading it
as "stopped" leaves a clean install looking like a crash.
"""
from routes import cookbook_routes


def _task(session_id):
    return {"id": session_id, "sessionId": session_id, "type": "download", "modelId": "package",
            "status": "running", "payload": {"_dep": "diffusers"}}


def test_a_finished_dependency_install_is_completed_or_error_by_its_exit_code(api, monkeypatch, tmp_path):
    # The local Windows host keeps each task's output in a log file that
    # outlives the process, so a finished task can be classified from it.
    monkeypatch.setattr(cookbook_routes, "IS_WINDOWS", True)
    monkeypatch.setattr(cookbook_routes, "TMUX_LOG_DIR", tmp_path)
    (tmp_path / "dep-ok.log").write_text("Successfully installed diffusers\n=== Process exited with code 0 ===\n")
    (tmp_path / "dep-bad.log").write_text("pip failed\n=== Process exited with code 1 ===\n")
    admin = api.as_admin()
    saved = admin.post("/api/cookbook/state", json={"tasks": [_task("dep-ok"), _task("dep-bad")]})
    assert saved.status_code == 200, saved.text

    response = admin.get("/api/cookbook/tasks/status")

    assert response.status_code == 200, response.text
    statuses = {t["session_id"]: (t["status"], t["exit_code"]) for t in response.json()["tasks"]}
    assert statuses == {"dep-ok": ("completed", 0), "dep-bad": ("error", 1)}
