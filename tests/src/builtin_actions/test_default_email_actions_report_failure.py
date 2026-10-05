"""A default email task that could not open the mailbox must fail the run.

`_auto_summarize_pass` catches everything and returns the message as a plain
string ("Error: IMAP is not configured for account 'work'"). That string has no
"processed 0"/"no new" marker, so `_result_has_work` called it work and the
action returned success — the Activity row went green while the task had done
nothing at all, run after run.
"""

import pytest


@pytest.mark.parametrize(
    "message",
    [
        "Error: IMAP is not configured for account 'work'",
        "Error: [Errno 104] Connection reset by peer",
        "IMAP not configured — add an Email Account in Settings or set env vars",
        "No LLM endpoint available",
    ],
)
def test_failure_strings_are_recognised(message):
    from src.builtin_actions import _result_is_config_error

    assert _result_is_config_error(message)


@pytest.mark.parametrize(
    "message",
    [
        "Processed 3 emails · created 1 calendar event(s)",
        # Summaries embed subject lines, which can say anything.
        "Processed 2 emails\n\nProcessed:\n- Re: printer not configured yet",
    ],
)
def test_real_work_is_not_mistaken_for_a_failure(message):
    from src.builtin_actions import _result_is_config_error

    assert not _result_is_config_error(message)


@pytest.mark.parametrize(
    "action_name",
    ["action_summarize_emails", "action_draft_email_replies", "action_extract_email_events"],
)
async def test_email_actions_fail_when_the_pass_errors(monkeypatch, action_name):
    import routes.email_pollers as pollers
    import src.builtin_actions as actions

    async def _erroring_pass(*a, **kw):
        return "Error: IMAP is not configured for account 'work'"

    monkeypatch.setattr(pollers, "_run_auto_summarize_once", _erroring_pass)

    result, success = await getattr(actions, action_name)(
        owner="admin", task_name=action_name, progress_cb=lambda m: None, manual=False
    )

    assert success is False, f"{action_name} reported a failed pass as a successful run"
    assert "IMAP is not configured" in result
