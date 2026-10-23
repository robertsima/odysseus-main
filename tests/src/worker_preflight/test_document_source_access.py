"""Document preflight checks the tool that reads the named source.

Bug (2026-10): a reviewer with read_file was told to read editor documents
(`document <uuid>`) and the app logs. Preflight passed because read_file is in
the generic "can read documents" list, but read_file opens neither, and the
worker failed partway through a costly run.
"""
from src import worker_preflight as wp

DOC_TASK = ("Review document 9878fc53-cd4e-47ec-8b4a-a9f740692da7 and "
            "document 1a2b3c4d-0000-4111-8222-333344445555 for reliability gaps.")


def prof(*tools, **extra):
    return {"name": "Reviewer", "tool_access": "selected", "enabled_tools": list(tools), **extra}


def test_editor_document_needs_manage_documents_not_read_file():
    warning = wp.document_access_warning(DOC_TASK, prof("read_file", "web_search"))
    assert warning and "editor document" in warning
    assert "manage_documents" in warning and "extra_tools=[\"manage_documents\"]" in warning
    assert "excerpt in the task" in warning


def test_editor_document_with_manage_documents_passes():
    assert wp.document_access_warning(DOC_TASK, prof("manage_documents")) is None


def test_search_documents_does_not_read_editor_documents():
    assert wp.document_access_warning(DOC_TASK, prof("search_documents"))


def test_app_logs_need_read_app_logs():
    task = "Check the app logs for errors in the last hour."
    warning = wp.document_access_warning(task, prof("read_file"))
    assert warning and "read_app_logs" in warning
    assert wp.document_access_warning(task, prof("read_app_logs")) is None


def test_vault_note_with_read_file_needs_the_vault_grant():
    task = "Summarise the design doc in my vault."
    assert "vault" in wp.document_access_warning(task, prof("read_file"))
    assert wp.document_access_warning(task, prof("read_file", private_vault_access=True)) is None
    assert wp.document_access_warning(task, prof("manage_notes")) is None


def test_bitwarden_vault_tools_do_not_read_notes():
    assert wp.document_access_warning("Update the design doc in my vault", prof("vault_get", "vault_search"))


def test_first_blocker_only_in_task_order_of_kinds():
    warning = wp.document_access_warning(DOC_TASK + " Also read the app logs.", prof("read_file"))
    assert "editor document" in warning and "read_app_logs" not in warning


def test_pasted_excerpt_satisfies_the_need():
    pasted = DOC_TASK + "\n\n```\n" + "Finding: the retry loop swallows timeouts and never reports them. " * 3 + "\n```\n"
    assert wp.document_access_warning(pasted, prof("web_search")) is None
    quoted = DOC_TASK + "\n> line one\n> line two\n> line three\n"
    assert wp.document_access_warning(quoted, prof("web_search")) is None


def test_a_short_code_span_is_not_an_excerpt():
    assert wp.document_access_warning(DOC_TASK + " Use `foo`.", prof("web_search"))


def test_full_tool_access_never_warns():
    assert wp.document_access_warning(DOC_TASK + " and the app logs", {"tool_access": "all"}) is None


def test_ordinary_tasks_do_not_warn():
    for task in ("Fix the failing test in src/app.py", "Why do CI logs show a timeout?",
                 "Summarise the release notes of Python 3.14"):
        assert wp.document_access_warning(task, prof("web_search")) is None
