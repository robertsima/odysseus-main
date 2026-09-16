"""Regression for issue #1707 — the agent tool-RAG force-included the entire
email toolset on any "tell me ..." query, crowding out the relevant tools so the
model believed it only had email tools and refused web/other tasks.

Root cause: `_KEYWORD_HINTS` in src/tool_index.py listed "tell" under the email
intent, and `get_tools_for_query` force-includes a hint's tools whenever any of
its keywords appears (word-boundary match). "tell" appears in a huge fraction of
requests (the reporter's was "visit <url> and tell me the title"), so email tools
were force-included for non-email queries.

These hints are deterministic string matching — no embeddings — so we can test
`get_tools_for_query` directly with retrieval stubbed out (no ChromaDB needed).
"""

from src.tool_index import (
    ALWAYS_AVAILABLE,
    ToolIndex,
    _EMAIL_MUTATION_TOOLS,
    email_intent,
    filter_email_tools,
)

_EMAIL_TOOLS = {
    "list_emails", "read_email", "send_email", "reply_to_email",
    "bulk_email", "delete_email", "archive_email", "mark_email_read",
}


def _index_without_embeddings():
    """A ToolIndex whose retrieval returns nothing, so get_tools_for_query
    exercises only the deterministic base + keyword-hint logic."""
    ti = ToolIndex.__new__(ToolIndex)        # skip __init__ (no ChromaDB/fastembed)
    ti.retrieve = lambda query, k=8: []
    return ti


def test_tell_in_web_query_does_not_force_email_tools():
    """The #1707 repro: a web request that merely contains the word 'tell' must
    NOT drag in the email toolset."""
    ti = _index_without_embeddings()
    q = "visit https://www.youtube.com/user/example and tell me the title of the latest video"
    tools = ti.get_tools_for_query(q)
    leaked = _EMAIL_TOOLS & tools
    assert not leaked, f"'tell me' must not force-include email tools, got {sorted(leaked)}"
    # web_search / web_fetch are always-available and must remain present.
    assert "web_search" in tools and "web_fetch" in tools


def test_explicit_web_search_query_gets_web_tools_without_retrieval():
    """Explicit web-search phrasing must surface web tools even if embeddings
    return nothing."""
    ti = _index_without_embeddings()
    tools = ti.get_tools_for_query("use web search and find a recipe for chocolate chip cookies")
    assert "web_search" in tools and "web_fetch" in tools


def test_genuine_email_query_still_gets_email_tools():
    """Removing 'tell' must not break real email intent — the actual email
    keywords still force-include the toolset."""
    ti = _index_without_embeddings()
    tools = ti.get_tools_for_query("reply to the unread email in my inbox")
    assert {"reply_to_email", "send_email", "read_email"} <= tools


def test_review_mail_about_software_does_not_mean_review_its_implementation():
    ti = _index_without_embeddings()
    for query in ("review my emails about the backend", "audit my inbox for repository access requests"):
        assert {"read_email", "audit_emails"} <= ti.get_tools_for_query(query)


def test_singular_mailbox_and_inbox_are_email_context():
    ti = _index_without_embeddings()
    assert "audit_emails" in ti.get_tools_for_query("audit my mailbox")
    assert "reply_to_email" in ti.get_tools_for_query("reply to the last message in my inbox")


def test_plain_tell_request_stays_minimal():
    """A bare 'tell me a joke' must not pull in email tools either."""
    ti = _index_without_embeddings()
    tools = ti.get_tools_for_query("tell me a joke")
    assert not (_EMAIL_TOOLS & tools)
    # Always-available baseline is still there.
    assert set(ALWAYS_AVAILABLE) <= tools


def test_notification_retrieval_does_not_leak_email_neighbours():
    """Generic send/message language must not turn an ntfy request into mail."""
    ti = _index_without_embeddings()
    ti.retrieve = lambda query, k=8: [
        "mcp__ntfy__send",
        "send_email",
        "list_emails",
        "reply_to_email",
    ]

    tools = ti.get_tools_for_query(
        "use ntfy to send a notification for odysseus after reading the documentation"
    )

    assert "mcp__ntfy__send" in tools
    assert not (_EMAIL_TOOLS & tools)


def test_code_audit_email_word_does_not_select_mail_mutations():
    """"Email subsystem" means source code here, not the user's mailbox."""
    ti = _index_without_embeddings()
    ti.retrieve = lambda query, k=8: [
        "audit_emails",
        "list_emails",
        "send_email",
        "reply_to_email",
        "delete_email",
    ]

    tools = ti.get_tools_for_query("run a few agents to audit the email subsystem")

    assert not (_EMAIL_MUTATION_TOOLS & tools)
    assert "send_email" not in tools
    assert "reply_to_email" not in tools


def test_explicit_email_action_still_gets_mutation_tools():
    """Context gating must preserve ordinary user-directed email actions."""
    ti = _index_without_embeddings()
    for query in (
        "send an email to bob@example.com",
        "reply to the unread email in my inbox",
        "archive the email from Alice",
    ):
        tools = ti.get_tools_for_query(query)
        assert _EMAIL_MUTATION_TOOLS <= tools, query


def test_email_subject_about_code_is_still_a_mail_request():
    """Code words in the subject must not turn a mailbox read into a code audit."""
    ti = _index_without_embeddings()
    tools = ti.get_tools_for_query("read my email about security tests")
    assert "read_email" in tools
    assert "send_email" not in tools


def test_ui_control_is_not_part_of_the_email_filter():
    selected = filter_email_tools("open settings", {"ui_control", "send_email"})
    assert selected == {"ui_control"}


def test_code_audit_and_email_results_keeps_the_mail_action():
    query = "run agents to audit the email subsystem and email me the results"
    intent = email_intent(query)
    assert intent["result_action"] and not intent["code_context"]

    ti = _index_without_embeddings()
    tools = ti.get_tools_for_query(query)
    assert "send_email" in tools


def test_explicit_address_action_seeds_email_tools_without_retrieval():
    ti = _index_without_embeddings()
    tools = ti.get_tools_for_query("send a message to bob@example.com")
    assert "send_email" in tools


def test_drafting_email_does_not_surface_send_mutation():
    ti = _index_without_embeddings()
    tools = ti.get_tools_for_query("draft an email to bob@example.com")
    assert "create_document" in tools
    assert "send_email" not in tools


def test_shared_contact_tool_survives_non_email_filtering():
    selected = filter_email_tools(
        "find Alice's phone number",
        {"resolve_contact", "ui_control", "send_email"},
    )
    assert selected == {"resolve_contact", "ui_control"}


def test_plural_mailbox_targets_are_mutation_intent():
    ti = _index_without_embeddings()
    for query in ("archive all emails", "delete unread messages"):
        tools = ti.get_tools_for_query(query)
        assert _EMAIL_MUTATION_TOOLS <= tools, query


def test_ntfy_result_notification_is_not_email_delivery():
    query = "audit the email subsystem then send an ntfy notification of results"
    intent = email_intent(query)
    assert intent["code_context"] and not intent["result_action"]

    selected = filter_email_tools(query, {"mcp__ntfy__send", "send_email"})
    assert selected == {"mcp__ntfy__send"}


def test_explicit_builtin_tool_name_survives_context_filter():
    selected = filter_email_tools("use send_email for this step", set())
    assert selected == {"send_email"}
