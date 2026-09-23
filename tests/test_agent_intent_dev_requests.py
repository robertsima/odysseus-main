"""Coding / source-control turns must not be routed as model serving.

2026-09-13 logs: "Push changes and start with the next slice implement as much
as would make sense to finish MVP ASAP" matched the Cookbook domain on the bare
word "start". The turn got serve_model/download_model instead of shell and
Claude Code tools, and the agent answered that it could not push.
"""

import pytest

pytest.skip(
    "Re-port backlog: exercises the fork's agent loop, replaced by upstream's in the 2026-09-18 sync (website/upstream-sync-2026-09-18.md)",
    allow_module_level=True,
)

import pytest

from src.agent_loop import (
    _DOMAIN_RULES,
    _DOMAIN_TOOL_MAP,
    _classify_agent_request,
    _looks_like_a_request,
    document_tools_to_drop,
)
from src.tool_index import ToolIndex


def _domains(text):
    return _classify_agent_request([{"role": "user", "content": text}], text)["domains"]


def test_push_and_implement_request_is_files_not_cookbook():
    d = _domains("Push changes and start with the next slice implement as much as would make sense to finish MVP ASAP")
    assert "cookbook" not in d
    assert "files" in d


def test_git_followups_and_work_continuations_keep_the_shell():
    # 2026-09-13: "Push changes up" and "Continue with next splice ..." both
    # dropped bash ("suppressed generic retained tools") in a chat that had
    # been using it, and the agent said it could not push / only planned.
    from src.agent_loop import _retained_tools_for_turn

    history = [{"role": "user", "content": "implement the homework slice in the dog-trainer repo"},
               {"role": "assistant", "content": "done, committed"}]
    for text in ("Push changes up", "ship it",
                 "Continue with next splice if MVP is not complete - finish as much as possibe",
                 "keep going with the next slice"):
        messages = history + [{"role": "user", "content": text}]
        intent = _classify_agent_request(messages, text)
        assert not intent["low_signal"], text
        kept, suppressed = _retained_tools_for_turn(
            {"bash", "read_file"}, query=intent["retrieval_query"], domains=intent["domains"],
            workspace=None, continuation=intent["continuation"])
        assert "bash" in kept and not suppressed, text

    # A continuation of other work still does not drag the shell along.
    kept, suppressed = _retained_tools_for_turn({"bash", "audit_emails"}, query="continue the inbox audit",
                                                domains={"email"}, workspace=None, continuation=True)
    assert suppressed == {"bash"}


def test_generic_verbs_do_not_imply_model_serving():
    for text in ("start the next feature", "pull the latest changes", "restart the server and try again",
                 "download the report and summarise it"):
        assert "cookbook" not in _domains(text), text


def test_email_implementation_audit_is_source_work_not_mailbox_work():
    domains = _domains("run a few agents to audit the email subsystem")
    assert "files" in domains
    assert "email" not in domains


def test_email_about_software_and_mixed_delivery_remain_mailbox_work():
    assert "email" in _domains("read my email about security tests")
    assert "email" in _domains("audit the email subsystem and email me the results")


def test_notification_keyword_fallback_does_not_add_email_suite():
    from src.agent_loop import keyword_fallback_tools

    selected = keyword_fallback_tools("use ntfy to send a notification for odysseus")
    assert not selected & {"send_email", "reply_to_email", "read_email", "list_emails"}


def test_model_serving_requests_still_route_to_cookbook():
    for text in ("start qwen on the workstation", "download the gemma model", "launch a vllm server",
                 "what models are running", "serve the preset on my gpu box", "stop the model server"):
        assert "cookbook" in _domains(text), text


class _FakeLane:
    name = "fake"

    def __init__(self):
        self.upserts = []

    class collection:  # noqa: N801 - mimics the chroma attribute
        pass

    def encode(self, docs):
        return [[0.0] for _ in docs]


def test_mcp_index_ignores_bullets_inside_descriptions():
    lane = _FakeLane()
    calls = {}

    class Collection:
        def get(self, where=None):
            return {"ids": []}

        def delete(self, ids=None):
            pass

        def upsert(self, ids, documents, embeddings, metadatas):
            calls.update(ids=ids, metadatas=metadatas)

    lane.collection = Collection()

    class Mgr:
        _generation = 7

        def get_tool_descriptions_for_prompt(self, disabled):
            return ("\n**Built-in: Todoist:**\n"
                    "  - mcp__todoist__todoist: Manage tasks. Actions:\n"
                    "- create: add a task\n"
                    "- close: complete a task\n")

    index = ToolIndex.__new__(ToolIndex)
    index._lanes = [lane]
    index._mcp_generation = 0
    index.index_mcp_tools(Mgr())
    assert [m["tool_name"] for m in calls["metadatas"]] == ["mcp__todoist__todoist"]


# ── Self-diagnosis domain ────────────────────────────────────────────────
#
# 2026-09-16 production logs: "analyze your own logs and fix this issue"
# classified as `low_signal=True domains=[]`, so embedding retrieval was
# skipped in favour of keyword hints, `read_app_logs` was neither retrieved
# nor selected, and the turn ended with the agent saying "I do not have
# application-log access."


def _intent(text):
    return _classify_agent_request([{"role": "user", "content": text}], text)


SELF_DIAGNOSIS_REQUESTS = [
    # The reproducer, verbatim.
    "analyze your own logs and fix this issue",
    "read your application logs and fix the bug",
    "check the app logs for errors",
    "debug this crash",
    # Same intent, other phrasings.
    "tail the server logs",
    "show me the stack trace",
    "there's a traceback in the output",
    "why did the export fail",
    "what went wrong with the last run",
    "the deploy is failing again",
    "this is broken, sort it out",
    "the upload keeps crashing",
    "trace where that exception comes from",
    "troubleshoot the email sync",
    "i'm getting an error when i save",
    "the preview isn't working",
    "look at the error log",
    "read the log file",
]


@pytest.mark.parametrize("text", SELF_DIAGNOSIS_REQUESTS)
def test_debugging_requests_detect_the_self_diagnosis_domain(text):
    assert "self_diagnosis" in _domains(text), text


@pytest.mark.parametrize("text", SELF_DIAGNOSIS_REQUESTS)
def test_debugging_requests_are_never_low_signal(text):
    # The whole point: a substantive debugging request must not land in the
    # same bucket as "hey" and skip embedding retrieval.
    assert _intent(text)["low_signal"] is False, text


def test_self_diagnosis_domain_seeds_the_log_reader():
    assert _DOMAIN_TOOL_MAP["self_diagnosis"] == {"read_app_logs"}
    # Every domain key needs a rule pack: _domain_rules_for_tools indexes
    # _DOMAIN_RULES directly and would KeyError on a missing one.
    assert set(_DOMAIN_TOOL_MAP) <= set(_DOMAIN_RULES)


# "log" is a stem inside a lot of ordinary words, and as a bare word it is the
# verb in "log in". Every one of these must stay out of the domain — the same
# hazard class as the one documented at _ADMIN_STEM_MIN_LEN.
@pytest.mark.parametrize(
    "text",
    [
        "log in to my gitea account",
        "help me log in",
        "the login page keeps redirecting me",
        "my login is wrong",
        "design a logo for my startup",
        "make the logo bigger",
        "explain the logic behind this sorting",
        "the business logic lives in the service layer",
        "write a blog post about rust",
        "read my blog",
        "add a dialogue between the two characters",
        "open the dialogue box",
        "find that item in the catalog",
        "update the product catalogue",
        "look into logistics companies",
        "the logistics of the move",
        # Vault content, not application logs.
        "what did i write in my voice logs",
        # The example the hints-only retrieval branch was originally added for.
        "i like Umni",
    ],
)
def test_log_lookalikes_do_not_trigger_self_diagnosis(text):
    assert "self_diagnosis" not in _domains(text), text


# ── The low-signal gate itself ───────────────────────────────────────────
#
# Domain coverage used to be the ONLY gate on embedding retrieval, so any
# request phrased outside the enumerated domain regexes was handled exactly
# like chit-chat: the direct-reply path on a first turn, keyword hints only
# afterwards. Adding a domain fixes one phrasing; the gate is what fixes the
# class. `low_signal` now also requires the turn to carry no request signal.


@pytest.mark.parametrize(
    "text",
    [
        # None of these match any domain, and all of them are real work.
        "fix the failing test",
        "rename the columns in that export",
        "compare these two approaches for me",
        "summarise what happened yesterday",
        "install the missing dependency",
        "how do i wire this up",
    ],
)
def test_domainless_work_requests_still_reach_embedding_retrieval(text):
    assert _intent(text)["low_signal"] is False, text


@pytest.mark.parametrize(
    "text",
    ["hey", "thanks!", "lol", "sounds good", "yeah do that", "hey man",
     "haha", "meh", "i like Umni", "how are you", "who are you",
     "that works"],
)
def test_chit_chat_and_terse_replies_still_skip_retrieval(text):
    # A terse acknowledgement can carry an action verb ("yeah do that") but no
    # content word for the index to match against. Note the ones that ARE
    # recognised continuations ("run it", "go ahead") are deliberately absent:
    # those already retrieve, against the inherited recent context rather than
    # against their own two words, which is the right handling for them.
    assert _intent(text)["low_signal"] is True, text


def test_request_detection_only_ever_clears_low_signal():
    # The gate change is monotone by construction: `_looks_like_a_request` is
    # ANDed into `low_signal`, so no turn that retrieves today stops doing so.
    # Guard the property rather than the wording of the helper.
    for text in ("hey", "thanks!", "fix the failing test", "check the app logs"):
        if _looks_like_a_request(text):
            assert _intent(text)["low_signal"] is False, text


# ── Document tools on a turn that is not about a document ────────────────


def test_untargeted_document_tools_are_dropped():
    # The second half of the 2026-09-16 incident: active_doc_relevant=False,
    # yet create/edit/suggest/update_document were all selected. The three that
    # need a target document go; create_document stays, because the file and
    # bash rules tell the model to use it for long output on any kind of turn.
    selected = {"read_app_logs", "create_document", "edit_document",
                "update_document", "suggest_document"}
    assert document_tools_to_drop(
        selected, active_document_relevant=False, domains=set(),
    ) == {"edit_document", "update_document", "suggest_document"}


def test_real_document_turns_keep_their_tools():
    selected = {"edit_document", "update_document", "suggest_document"}
    assert document_tools_to_drop(
        selected, active_document_relevant=True, domains=set(),
    ) == set()
    assert document_tools_to_drop(
        selected, active_document_relevant=False, domains={"documents"},
    ) == set()


def test_forced_document_tools_outrank_the_heuristic():
    assert document_tools_to_drop(
        {"edit_document", "suggest_document"},
        active_document_relevant=False,
        domains=set(),
        forced_tools={"suggest_document"},
    ) == {"edit_document"}
