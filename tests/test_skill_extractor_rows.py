from services.memory import skill_extractor


def test_duplicate_title_skips_invalid_skill_rows():
    rows = [
        "bad-row",
        None,
        {"title": 123},
        {"title": "Small PR workflow"},
    ]

    assert skill_extractor._has_duplicate_title(rows, "small pr workflow")
    assert not skill_extractor._has_duplicate_title(rows, "release checklist")


def test_near_reworded_titles_count_as_duplicates():
    rows = [{"title": "Verify and Push Feature Slices"}]
    assert skill_extractor._has_duplicate_title(rows, "Verify and Push Completed Feature Slices")
    assert skill_extractor._has_duplicate_title(rows, "Verify and Push MVP Slices")
    assert not skill_extractor._has_duplicate_title(rows, "Create Liquibase Seed SQL")
    assert not skill_extractor._has_duplicate_title(rows, "Push Changes")


def test_launched_workers_are_not_a_completed_procedure():
    events = [
        {"tool": "manage_agent_loadout", "output": "loadouts", "exit_code": 0},
        {"tool": "delegate_to_agent", "output": "task started", "exit_code": 0},
        {"tool": "delegate_to_agent", "output": "worker limit reached", "exit_code": 1},
    ]
    assert not skill_extractor._has_procedure_evidence(events)


def test_failed_held_and_duplicate_calls_do_not_supply_evidence():
    for extra in ({"exit_code": 1}, {"blocked": True}, {"approval_required": True},
                  {"duplicate_call": True}, {"error": True}, {"status": "running"}):
        assert not skill_extractor._has_procedure_evidence([
            {"tool": "bash", "output": "test attempt", **extra}
        ])
    assert skill_extractor._has_procedure_evidence([
        {"tool": "bash", "output": "tests failed", "exit_code": 1},
        {"tool": "edit_file", "output": "applied fix", "exit_code": 0},
        {"tool": "bash", "output": "tests passed", "exit_code": 0},
    ])


def test_extractor_skips_launch_only_turn_before_calling_model():
    import asyncio
    from types import SimpleNamespace

    session = SimpleNamespace()  # no history read or model call should occur
    result = asyncio.run(skill_extractor.maybe_extract_skill(
        session, None, "http://unused", "model", {}, 5, 4,
        tool_events=[{"tool": "delegate_to_agent", "output": "started", "exit_code": 0}],
    ))
    assert result is None


def test_execution_metadata_is_bounded_and_excludes_tool_contents():
    import json

    events = [{"tool": "read_email", "output": "PRIVATE-MAIL", "exit_code": 0}] * 20
    events += [{"tool": "read_file", "output": "SECRET-FILE", "arguments": {"token": "SECRET-ARG"},
                "doc_id": "PRIVATE-DOC-ID", "status": "SECRET-STATUS", "exit_code": 1}]
    metadata = skill_extractor._execution_evidence_metadata(events)
    assert len(metadata) == 12
    assert metadata[0]["procedure_evidence"] is False  # mailbox lookup is not a learned procedure
    assert metadata[-1]["failed"] is True
    assert metadata[-1]["procedure_evidence"] is False
    serialized = json.dumps(metadata)
    assert all(secret not in serialized for secret in (
        "PRIVATE-MAIL", "SECRET-FILE", "SECRET-ARG", "PRIVATE-DOC-ID", "SECRET-STATUS",
    ))


def test_extraction_request_never_appends_raw_tool_output(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    import src.llm_core

    captured = []

    async def fake_call(endpoint, model, messages, **kwargs):
        captured.extend(messages)
        return "null"

    monkeypatch.setattr(src.llm_core, "llm_call_async", fake_call)
    session = SimpleNamespace(get_context_messages=lambda: [
        {"role": "user", "content": "Run the verification procedure"},
        {"role": "assistant", "content": "Verification completed"},
    ])
    asyncio.run(skill_extractor.maybe_extract_skill(
        session, None, "http://unused", "model", {}, 2, 2,
        tool_events=[{"tool": "bash", "output": "PRIVATE-TOOL-OUTPUT", "exit_code": 0}],
    ))
    assert len(captured) == 2
    assert "Execution outcome metadata" in captured[1]["content"]
    assert "PRIVATE-TOOL-OUTPUT" not in captured[1]["content"]


def test_documentation_lookup_is_not_a_learned_procedure():
    assert not skill_extractor._has_procedure_evidence([
        {"tool": "web_search", "output": "official docs", "exit_code": 0},
        {"tool": "web_fetch", "output": "curl example", "exit_code": 0},
    ])


def test_procedure_evidence_rejects_canceled_and_error_statuses():
    for status in ("error", "canceled", "interrupted", "timed_out"):
        assert not skill_extractor._has_procedure_evidence([
            {"tool": "bash", "output": "partial output", "status": status},
        ])
