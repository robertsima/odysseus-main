import asyncio
import json
import sys
from types import SimpleNamespace

import src.agent_loop as al
from src.agent_tools import ToolBlock
from src.tool_execution import NO_TOOL_SECURITY_CONTEXT, execute_tool_block
from src.tool_policy import (
    WEB_TOOL_NAMES,
    build_effective_tool_policy,
    detect_guide_only_turn,
    web_search_enabled_for_turn,
)


def _collect(gen):
    async def _run():
        return [c async for c in gen]

    return asyncio.run(_run())


def _events(chunks):
    out = []
    for chunk in chunks:
        if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]"):
            try:
                out.append(json.loads(chunk[6:]))
            except Exception:
                pass
    return out


def _delta_chunk(text):
    return "data: " + json.dumps({"delta": text}) + "\n\n"


def _patch_loop_basics(monkeypatch):
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)


def test_detects_strong_guide_only_turns():
    assert detect_guide_only_turn("GUIDE-ONLY MODE. DO NOT USE TOOLS.")
    assert detect_guide_only_turn("NO-TOOLS MODE.")
    assert detect_guide_only_turn("Ask me before using tools.")
    assert detect_guide_only_turn("You are not allowed to:\n- use tools\n- execute commands")


def test_does_not_treat_ordinary_guidance_as_no_tools():
    assert detect_guide_only_turn("Can you guide me through fixing this bug?") is None
    assert detect_guide_only_turn("I have no tools installed in this project.") is None
    assert detect_guide_only_turn("Write the script in the repo; I'll run it locally.") is None
    assert detect_guide_only_turn("Do not run commands that write files; inspect the repo first.") is None
    assert detect_guide_only_turn("Don't execute shell commands unless I approve them.") is None


def test_guide_only_policy_blocks_and_hides_tools():
    policy = build_effective_tool_policy(
        disabled_tools={"web_search"},
        last_user_message="GUIDE-ONLY MODE. DO NOT USE TOOLS.",
    )
    assert policy.mode == "guide_only"
    assert policy.disable_mcp is True
    assert policy.block_all_tool_calls is True
    for tool in ("bash", "python", "web_search", "read_file"):
        assert tool in policy.disabled_tools
        assert tool in policy.hidden_tools
        assert policy.blocks(tool)


def test_normal_policy_preserves_existing_disabled_tools():
    policy = build_effective_tool_policy(
        disabled_tools={"web_search"},
        last_user_message="Please check this normally.",
    )
    assert policy.mode == "normal"
    assert policy.blocks("web_search")
    assert not policy.blocks("bash")


def test_web_search_enabled_for_turn_requires_explicit_enable():
    assert web_search_enabled_for_turn(None, None) is False
    assert web_search_enabled_for_turn("true", None) is True
    assert web_search_enabled_for_turn(None, "true") is True
    assert web_search_enabled_for_turn(True, None) is True
    assert web_search_enabled_for_turn("false", "true") is False
    assert web_search_enabled_for_turn(False, "true") is False


def _schema_names(tools):
    return {
        tool.get("function", {}).get("name") or tool.get("name")
        for tool in (tools or [])
    }


def test_agent_loop_web_intent_preserves_disabled_web_tools(monkeypatch):
    _patch_loop_basics(monkeypatch)
    sent_tools = []

    async def _fake_stream(_candidates, messages, **kwargs):
        sent_tools.append(kwargs.get("tools"))
        yield _delta_chunk("ok")
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    _collect(
        al.stream_agent_loop(
            "https://api.openai.com/v1",
            "gpt-test",
            [{"role": "user", "content": "please look up the latest CVEs"}],
            max_rounds=1,
            relevant_tools=set(),
            disabled_tools=set(WEB_TOOL_NAMES),
        )
    )

    assert sent_tools
    assert WEB_TOOL_NAMES.isdisjoint(_schema_names(sent_tools[0]))


def test_agent_loop_forced_web_tools_filtered_by_disabled_tools(monkeypatch):
    _patch_loop_basics(monkeypatch)
    sent_tools = []

    async def _fake_stream(_candidates, messages, **kwargs):
        sent_tools.append(kwargs.get("tools"))
        yield _delta_chunk("ok")
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    _collect(
        al.stream_agent_loop(
            "https://api.openai.com/v1",
            "gpt-test",
            [{"role": "user", "content": "latest Kubernetes release"}],
            max_rounds=1,
            relevant_tools=set(),
            forced_tools=set(WEB_TOOL_NAMES),
            disabled_tools=set(WEB_TOOL_NAMES),
        )
    )

    assert sent_tools
    assert WEB_TOOL_NAMES.isdisjoint(_schema_names(sent_tools[0]))


def test_agent_loop_policy_blocks_disabled_web_tool_call_before_execution(monkeypatch):
    _patch_loop_basics(monkeypatch)
    called = False

    async def _fake_exec(*args, **kwargs):
        nonlocal called
        called = True
        return ("web_search", {"output": "ran", "exit_code": 0})

    async def _fake_stream(_candidates, messages, **kwargs):
        yield _delta_chunk('```web_search\n{"query":"current CVEs"}\n```')
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    policy = build_effective_tool_policy(
        disabled_tools=WEB_TOOL_NAMES,
        last_user_message="please look up the latest CVEs",
    )
    chunks = _collect(
        al.stream_agent_loop(
            "http://local.test/v1",
            "local-model",
            [{"role": "user", "content": "please look up the latest CVEs"}],
            max_rounds=1,
            relevant_tools={"web_search"},
            disabled_tools=set(policy.all_disabled_names()),
            tool_policy=policy,
        )
    )
    events = _events(chunks)
    blocked = [event for event in events if event.get("type") == "tool_output"]

    assert called is False
    assert not any(event.get("type") == "tool_start" for event in events)
    assert blocked
    assert blocked[0]["tool"] == "web_search"
    assert blocked[0]["exit_code"] == 1


def test_executor_policy_backstop_blocks_tools():
    policy = build_effective_tool_policy(last_user_message="Do not use tools.")
    desc, result = asyncio.run(
        execute_tool_block(
            ToolBlock("bash", "echo should-not-run"),
            tool_policy=policy,
            security_context=NO_TOOL_SECURITY_CONTEXT,
        )
    )
    assert desc == "bash: BLOCKED"
    assert result["exit_code"] == 1
    assert "forbade" in result["error"]


def test_agent_loop_blocks_guide_only_fenced_tool_before_start(monkeypatch):
    _patch_loop_basics(monkeypatch)
    called = False

    async def _fake_exec(*args, **kwargs):
        nonlocal called
        called = True
        return ("bash", {"output": "ran", "exit_code": 0})

    async def _fake_stream(_candidates, messages, **kwargs):
        yield _delta_chunk("```bash\necho should-not-run\n```")
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    policy = build_effective_tool_policy(last_user_message="GUIDE-ONLY MODE. DO NOT USE TOOLS.")
    chunks = _collect(
        al.stream_agent_loop(
            "http://local.test/v1",
            "local-model",
            [{"role": "user", "content": "GUIDE-ONLY MODE. DO NOT USE TOOLS."}],
            max_rounds=1,
            relevant_tools={"bash"},
            tool_policy=policy,
        )
    )
    events = _events(chunks)
    assert called is False
    assert not any(event.get("type") == "tool_start" for event in events)
    blocked = [event for event in events if event.get("type") == "tool_output"]
    assert blocked
    assert blocked[0]["tool"] == "bash"
    assert blocked[0]["exit_code"] == 1


def test_guide_only_hides_api_function_schemas(monkeypatch):
    _patch_loop_basics(monkeypatch)
    sent_tools = []

    async def _fake_stream(_candidates, messages, **kwargs):
        sent_tools.append(kwargs.get("tools"))
        yield _delta_chunk("ok")
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    policy = build_effective_tool_policy(last_user_message="Do not use tools.")

    _collect(
        al.stream_agent_loop(
            "https://api.openai.com/v1",
            "gpt-test",
            [{"role": "user", "content": "Do not use tools."}],
            max_rounds=1,
            relevant_tools={"bash", "web_search"},
            tool_policy=policy,
        )
    )

    assert sent_tools == [None]


def test_guide_only_skips_tool_retrieval(monkeypatch):
    _patch_loop_basics(monkeypatch)
    sent_tools = []

    async def _fake_stream(_candidates, messages, **kwargs):
        sent_tools.append(kwargs.get("tools"))
        yield _delta_chunk("ok")
        yield "data: [DONE]\n\n"

    def _fail_tool_index():
        raise AssertionError("guide-only mode must not retrieve tool candidates")

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    from src.tool_index import email_intent
    monkeypatch.setitem(
        sys.modules,
        "src.tool_index",
        SimpleNamespace(get_tool_index=_fail_tool_index, ALWAYS_AVAILABLE=set(), email_intent=email_intent),
    )
    policy = build_effective_tool_policy(last_user_message="Do not use tools.")

    _collect(
        al.stream_agent_loop(
            "https://api.openai.com/v1",
            "gpt-test",
            [{"role": "user", "content": "Do not use tools."}],
            max_rounds=1,
            relevant_tools=None,
            tool_policy=policy,
        )
    )

    assert sent_tools == [None]


def test_guide_only_blocks_document_prestream(monkeypatch):
    _patch_loop_basics(monkeypatch)

    async def _fake_stream(_candidates, messages, **kwargs):
        yield _delta_chunk("```create_document\nTitle\nmd\nBody\n```")
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    policy = build_effective_tool_policy(last_user_message="Do not use tools.")
    chunks = _collect(
        al.stream_agent_loop(
            "http://local.test/v1",
            "local-model",
            [{"role": "user", "content": "Do not use tools."}],
            max_rounds=1,
            relevant_tools={"create_document"},
            tool_policy=policy,
        )
    )
    events = _events(chunks)
    assert not any(event.get("type") == "doc_stream_open" for event in events)
    assert not any(event.get("type") == "tool_start" for event in events)
    assert any(event.get("type") == "tool_output" and event.get("tool") == "create_document" for event in events)


def test_guide_only_blocks_later_round_document_streaming(monkeypatch):
    _patch_loop_basics(monkeypatch)
    calls = 0

    async def _fake_stream(_candidates, messages, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield _delta_chunk("```bash\necho blocked\n```")
        else:
            yield _delta_chunk("```create_document\nTitle\nmd\nBody\n```")
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    policy = build_effective_tool_policy(last_user_message="Do not use tools.")
    chunks = _collect(
        al.stream_agent_loop(
            "http://local.test/v1",
            "local-model",
            [{"role": "user", "content": "Do not use tools."}],
            max_rounds=2,
            relevant_tools={"bash", "create_document"},
            tool_policy=policy,
        )
    )
    events = _events(chunks)
    # A later round has to have been reached — the block under test is the one
    # that fires after round 1. The exact count is not the property: a round
    # ceiling no longer ends a run, so the loop runs on until the loop-breaker
    # trips, and pinning it to 2 was pinning the old cap.
    assert calls >= 2
    assert not any(event.get("type") == "doc_stream_open" for event in events)
    assert not any(event.get("type") == "doc_stream_delta" for event in events)


def test_guide_only_skips_intent_without_action_nudge(monkeypatch):
    _patch_loop_basics(monkeypatch)

    async def _fake_stream(_candidates, messages, **kwargs):
        yield _delta_chunk("I will check the logs.")
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    policy = build_effective_tool_policy(last_user_message="Do not use tools.")
    chunks = _collect(
        al.stream_agent_loop(
            "http://local.test/v1",
            "local-model",
            [{"role": "user", "content": "Do not use tools."}],
            max_rounds=2,
            relevant_tools={"bash"},
            tool_policy=policy,
        )
    )
    events = _events(chunks)
    assert not any(event.get("type") == "agent_step" for event in events)


def test_guide_only_suppresses_active_document_context(monkeypatch):
    _patch_loop_basics(monkeypatch)
    prompt_payloads = []

    async def _fake_stream(_candidates, messages, **kwargs):
        prompt_payloads.append("\n\n".join(str(msg.get("content", "")) for msg in messages))
        yield _delta_chunk("ok")
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    policy = build_effective_tool_policy(last_user_message="Do not use tools.")
    active_doc = SimpleNamespace(
        id="doc-1",
        current_content="SECRET ACTIVE DOCUMENT CONTENT",
        title="Secret Doc",
        language="markdown",
    )

    _collect(
        al.stream_agent_loop(
            "http://local.test/v1",
            "local-model",
            [{"role": "user", "content": "Do not use tools."}],
            max_rounds=1,
            relevant_tools={"edit_document"},
            tool_policy=policy,
            active_document=active_doc,
        )
    )

    assert prompt_payloads
    assert "SECRET ACTIVE DOCUMENT CONTENT" not in prompt_payloads[0]
    assert "ACTIVE DOCUMENT" not in prompt_payloads[0]
    assert "Relevant skills" not in prompt_payloads[0]


def test_document_my_style_does_not_infer_public_persona(monkeypatch):
    _patch_loop_basics(monkeypatch)
    monkeypatch.setattr(al, "_build_base_prompt", lambda *a, **k: ("BASE", ""), raising=False)
    monkeypatch.setattr(al, "_cached_base_prompt", None, raising=False)
    monkeypatch.setattr(al, "_cached_base_prompt_key", None, raising=False)

    import src.settings as settings
    monkeypatch.setattr(settings, "load_settings", lambda: {"document_writing_style": ""}, raising=False)

    active_doc = SimpleNamespace(
        id="doc-style",
        current_content="A short poem already exists here.",
        title="Morning Poem",
        language="markdown",
    )

    messages, _ = al._build_system_prompt(
        [{"role": "user", "content": "Write as my style"}],
        model="local-model",
        active_document=active_doc,
        mcp_mgr=None,
        relevant_tools={"edit_document", "update_document"},
        suppress_skills=True,
    )
    payload = "\n\n".join(str(msg.get("content", "")) for msg in messages)

    assert "There is no saved document writing style" in payload
    assert "do NOT infer that style from memories, identity, public persona" in payload


def test_guide_only_skips_teacher_escalation(monkeypatch):
    _patch_loop_basics(monkeypatch)

    async def _fake_stream(_candidates, messages, **kwargs):
        yield _delta_chunk("Could you tell me what output you see?")
        yield "data: [DONE]\n\n"

    async def _fail_teacher(*_args, **_kwargs):
        raise AssertionError("teacher escalation must not run in guide-only mode")
        yield ""

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    monkeypatch.setitem(
        sys.modules,
        "src.teacher_escalation",
        SimpleNamespace(run_teacher_inline=_fail_teacher),
    )
    policy = build_effective_tool_policy(last_user_message="Do not use tools.")

    chunks = _collect(
        al.stream_agent_loop(
            "http://local.test/v1",
            "local-model",
            [{"role": "user", "content": "Do not use tools."}],
            max_rounds=1,
            relevant_tools={"bash"},
            tool_policy=policy,
        )
    )

    assert any("Could you tell me" in chunk for chunk in chunks)


# ── tool allowlists: stored as allowlists, inverted at evaluation ────────────
#
# Each of these fails against the previous implementation, which inverted a
# role's allowlist into a denylist in `agent_profiles.session_patch` and
# persisted the result on the session.


class _FakeMCP:
    """Enough of MCPManager for the loop's gates, with two servers connected."""

    TOOLS = [
        {"server_id": "email", "server_name": "Email", "name": "list_emails"},
        {"server_id": "email", "server_name": "Email", "name": "send_email"},
        {"server_id": "github", "server_name": "GitHub", "name": "merge_pr"},
    ]

    def get_all_tools(self, disabled_map=None):
        return [
            dict(tool, qualified_name=f"mcp__{tool['server_id']}__{tool['name']}",
                 description="", input_schema={"type": "object", "properties": {}},
                 is_disabled=tool["name"] in (disabled_map or {}).get(tool["server_id"], set()))
            for tool in self.TOOLS
        ]

    def get_all_openai_schemas(self, disabled_map=None):
        return [
            {"type": "function",
             "function": {"name": tool["qualified_name"], "description": "",
                          "parameters": {"type": "object", "properties": {}}}}
            for tool in self.get_all_tools(disabled_map) if not tool["is_disabled"]
        ]

    def get_tool_descriptions_for_prompt(self, disabled_map=None):
        return ""

    def gated_tool_names(self, disabled_map=None):
        return set()

    def demoted_servers(self, disabled_map=None):
        return []

    def plan_mode_blocked_mcp(self):
        return {}, set()


def _run_loop_with_settings(monkeypatch, settings, *, relevant_tools, mcp=None):
    """Run one round with `settings` as the chat's stored policy; return the schemas sent."""
    import core.database as core_db

    _patch_loop_basics(monkeypatch)
    if mcp is not None:
        monkeypatch.setattr(al, "get_mcp_manager", lambda: mcp, raising=False)
        # The per-server MCP toggle map comes from the database; this test is
        # about policy, not about whichever schema the rest of the suite left
        # behind.
        monkeypatch.setattr(al, "_load_mcp_disabled_map", lambda: {}, raising=False)
    # Without this the loop treats an owner-less turn as public, blocks half the
    # registry and drops the MCP manager entirely — which would make the MCP
    # assertions below pass for the wrong reason.
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set(), raising=False)
    monkeypatch.setattr(core_db, "get_session_settings", lambda sid: dict(settings), raising=False)
    sent_tools = []

    async def _fake_stream(_candidates, messages, **kwargs):
        sent_tools.append(kwargs.get("tools"))
        yield _delta_chunk("ok")
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    _collect(
        al.stream_agent_loop(
            "https://api.openai.com/v1",
            "gpt-test",
            [{"role": "user", "content": "read my inbox and search the web"}],
            max_rounds=1,
            session_id="chat-1",
            relevant_tools=set(relevant_tools),
        )
    )
    assert sent_tools
    return _schema_names(sent_tools[0])


def test_selected_tools_does_not_leave_every_mcp_tool_reachable(monkeypatch):
    """Defect 1. A role narrowed to three native tools kept every tool of every
    connected MCP server, because the denylist was built from a native-only
    registry — `known_tool_names()` holds no `mcp__` name at all."""
    from src.tool_policy import known_tool_names

    assert not any(name.startswith("mcp__") for name in known_tool_names())

    selection = {"web_search", "read_file", "mcp__github__merge_pr"}
    # Control: with no allowlist the connected server's tools really are sent,
    # so the assertion below cannot pass for the wrong reason.
    unrestricted = _run_loop_with_settings(
        monkeypatch, {"allowed_mcp_servers": ["*"]},
        relevant_tools=selection, mcp=_FakeMCP(),
    )
    assert "mcp__github__merge_pr" in unrestricted

    sent = _run_loop_with_settings(
        monkeypatch,
        {"tool_access": "selected",
         "enabled_tools": ["web_search", "create_document", "manage_memory"],
         "allowed_mcp_servers": ["*"]},
        relevant_tools=selection,
        mcp=_FakeMCP(),
    )
    assert "web_search" in sent
    assert not any(name.startswith("mcp__") for name in sent), sent
    assert "read_file" not in sent


def test_selected_tools_ignores_a_wide_open_mcp_access():
    """Defect 2. `mcp_access` defaults to "all", so `tool_access="selected"`
    produced `allowed_mcp_servers: ["*"]`. A default is not consent: the tool
    allowlist decides, and `["*"]` cannot widen past it."""
    from src.agent_profiles import session_patch, validate_profiles
    from src.tool_policy import allowlist_permits

    profile = validate_profiles([{
        "name": "researcher", "tool_access": "selected",
        "enabled_tools": ["web_search", "create_document", "manage_memory"],
    }])[0]
    assert profile["mcp_access"] == "all"  # the default nobody chose
    patch = session_patch(profile)
    assert patch["allowed_mcp_servers"] == []
    assert allowlist_permits("mcp__email__list_emails", patch["tool_access"], patch["enabled_tools"]) is False


def test_an_explicitly_chosen_mcp_server_survives_the_upgrade():
    """The other side of defect 2: `mcp_access="selected"` with named servers is
    someone ticking boxes, so it is translated into the allowlist rather than
    dropped. An install upgrading keeps exactly the MCP reach it had."""
    from src.agent_profiles import session_patch, validate_profiles
    from src.tool_policy import allowlist_permits

    patch = session_patch(validate_profiles([{
        "name": "browser", "tool_access": "selected", "enabled_tools": ["web_fetch"],
        "mcp_access": "selected", "allowed_mcp_servers": ["builtin_browser"],
    }])[0])
    assert patch["allowed_mcp_servers"] == ["builtin_browser"]
    assert allowlist_permits("mcp__builtin_browser__open", patch["tool_access"], patch["enabled_tools"]) is True
    assert allowlist_permits("mcp__email__list_emails", patch["tool_access"], patch["enabled_tools"]) is False


def test_an_allowlist_excludes_tools_that_did_not_exist_when_it_was_saved():
    """Defect 3. The denylist was a snapshot taken at apply time, so a builtin
    added by an upgrade or a server connected the next day was absent from it
    and therefore allowed — policy failing open."""
    from src.agent_profiles import session_patch, validate_profiles
    from src.tool_policy import allowlist_permits

    patch = session_patch(validate_profiles([{
        "name": "narrow", "tool_access": "selected", "enabled_tools": ["web_search"],
    }])[0])
    # Stored as an allowlist, not as its inverse.
    assert patch["tool_access"] == "selected"
    assert patch["enabled_tools"] == ["web_search"]
    assert not patch["disabled_tools"]

    assert allowlist_permits("web_search", patch["tool_access"], patch["enabled_tools"]) is True
    for newcomer in ("a_builtin_shipped_next_release", "mcp__newly_connected__anything"):
        assert allowlist_permits(newcomer, patch["tool_access"], patch["enabled_tools"]) is False


def test_an_allowlist_can_grant_one_mcp_tool_without_its_server(monkeypatch):
    from src.tool_policy import allowlist_permits

    access, enabled = "selected", ["web_search", "mcp__email__list_emails"]
    assert allowlist_permits("mcp__email__list_emails", access, enabled) is True
    assert allowlist_permits("mcp__email__send_email", access, enabled) is False
    assert allowlist_permits("mcp__github__merge_pr", access, enabled) is False

    sent = _run_loop_with_settings(
        monkeypatch,
        {"tool_access": access, "enabled_tools": enabled, "allowed_mcp_servers": ["*"]},
        relevant_tools={"web_search", "mcp__email__list_emails", "mcp__email__send_email",
                        "mcp__github__merge_pr"},
        mcp=_FakeMCP(),
    )
    assert "mcp__email__list_emails" in sent
    assert "mcp__email__send_email" not in sent
    assert "mcp__github__merge_pr" not in sent


def test_wildcards_are_how_an_allowlist_widens_to_mcp():
    """MCP names are generated at runtime and can never be enumerated, so the
    allowlist grants a whole server, or all of them, by shape."""
    from src.tool_policy import allowlist_permits

    one_server = ["mcp__email__*"]
    assert allowlist_permits("mcp__email__send_email", "selected", one_server) is True
    assert allowlist_permits("mcp__github__merge_pr", "selected", one_server) is False
    assert allowlist_permits("web_search", "selected", one_server) is False

    everything = ["mcp__*"]
    assert allowlist_permits("mcp__github__merge_pr", "selected", everything) is True
    assert allowlist_permits("mcp__anything_at_all__new_tool", "selected", everything) is True
    assert allowlist_permits("web_search", "selected", everything) is False


def test_allowlist_fails_closed_on_an_access_mode_it_does_not_know():
    from src.tool_policy import allowlist_permits

    assert allowlist_permits("web_search", "none", ["web_search"]) is False
    assert allowlist_permits("web_search", "everything_please", ["web_search"]) is False
    assert allowlist_permits("web_search", "selected", []) is False
    # "all" is the absence of an allowlist, including for a chat that has none
    # stored at all.
    assert allowlist_permits("anything", "all", []) is True
    assert allowlist_permits("anything", None, None) is True


def test_allowlist_matches_both_spellings_of_an_email_tool():
    """A bare built-in email name and its mcp__email__ form dispatch to the same
    thing; an allowlist written in one spelling must not be bypassable by the
    other, nor defeated by it."""
    from src.tool_policy import allowlist_permits

    assert allowlist_permits("mcp__email__list_emails", "selected", ["list_emails"]) is True
    assert allowlist_permits("list_emails", "selected", ["mcp__email__list_emails"]) is True


def test_executor_blocks_a_tool_the_chats_allowlist_does_not_name(monkeypatch):
    """Defense in depth: a stale or hand-written call must be refused even when
    the schema layer already hid the tool."""
    import core.database as core_db

    # The executor reads the chat's policy with strict=True and fails closed if
    # it cannot, so the stub has to accept that keyword.
    monkeypatch.setattr(core_db, "get_session_settings", lambda sid, **kw: {
        "tool_access": "selected", "enabled_tools": ["web_search"], "allowed_mcp_servers": ["*"],
    }, raising=False)

    # Both names are read off the module object at call time. Another test in
    # the suite reloads `src.tool_execution`, and the executor checks the
    # security context by identity/isinstance -- a sentinel captured at import
    # time would then belong to a different module instance and be rejected
    # before this test's own assertion could run.
    import src.tool_execution as te

    desc, result = asyncio.run(te.execute_tool_block(
        ToolBlock("bash", "echo should-not-run"), session_id="chat-1",
        security_context=te.NO_TOOL_SECURITY_CONTEXT))
    assert desc == "bash: BLOCKED" and result["exit_code"] == 1
    assert "allowlist" in result["error"]

    desc, result = asyncio.run(te.execute_tool_block(
        ToolBlock("mcp__email__send_email", '{"to":"x"}'), session_id="chat-1",
        security_context=te.NO_TOOL_SECURITY_CONTEXT))
    assert desc == "mcp__email__send_email: BLOCKED" and result["exit_code"] == 1


def test_a_chat_with_no_stored_allowlist_keeps_its_denylist(monkeypatch):
    """Backward compatibility. Chats saved before allowlists were stored carry
    only the inverted snapshot; they must be neither widened nor crippled."""
    sent = _run_loop_with_settings(
        monkeypatch,
        {"disabled_tools": ["read_file", "bash"]},
        relevant_tools={"web_search", "read_file", "bash"},
    )
    assert "web_search" in sent
    assert "read_file" not in sent and "bash" not in sent


def test_a_profiles_own_disabled_tools_still_deny_on_top_of_an_allowlist():
    """A profile saved with a persisted denylist — including a complement an
    earlier version of `agent_loadouts` wrote — keeps denying what it denied."""
    from src.agent_profiles import session_patch, validate_profiles

    patch = session_patch(validate_profiles([{
        "name": "legacy", "tool_access": "selected",
        "enabled_tools": ["web_search", "bash"], "disabled_tools": ["bash"],
    }])[0])
    assert patch["disabled_tools"] == ["bash"]
    assert patch["enabled_tools"] == ["bash", "web_search"]


def test_task_scheduler_and_profiles_share_one_inversion(monkeypatch):
    """Two copies of a rule are two rules: the scheduler inverted crew
    allowlists against BUILTIN_TOOL_DESCRIPTIONS while profiles used
    known_tool_names(), and neither registry held an MCP name."""
    import src.tool_policy as tp

    monkeypatch.setattr(tp, "connected_mcp_tool_names",
                        lambda: {"mcp__email__list_emails", "mcp__github__merge_pr"}, raising=False)
    denied = tp.denied_by_allowlist(
        tp.live_tool_names(), tool_access="selected", enabled_tools=["web_search"])
    assert "mcp__email__list_emails" in denied
    assert "mcp__github__merge_pr" in denied
    assert "bash" in denied
    assert "web_search" not in denied
