"""Capability gating of the outgoing tool schema.

The failure this closes: on a host with no coding-agent binary,
`delegate_to_claude_code` was still in the schema, the model picked it, and the
call failed at the far end — after which the model retried or invented a
workaround. A tool the host cannot run should never be advertised.

The gate is deliberately timid, and these tests pin each way it refuses to
overreach: no capability means never withheld, an unknown capability removes
nothing, connected MCP tools are untouched, and emptying the tool list entirely
is treated as a bug rather than an outcome.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

import pytest

import src.agent_loop as al
import src.capabilities as capabilities

# Imported for its side effect *before* the fixture swaps the registry, so the
# `import src.capabilities_builtin` inside the gate is a no-op and each test
# sees only the capabilities it declared itself.
import src.capabilities_builtin  # noqa: F401  (registers the shipped declarations)


def _schema(name: str) -> Dict:
    return {
        "type": "function",
        "function": {"name": name, "description": name, "parameters": {"type": "object", "properties": {}}},
    }


def _names(schemas: List[Dict]) -> set:
    return {s.get("function", {}).get("name") for s in schemas}


def _unmet(detail: str = "no binary on this host"):
    def check() -> Tuple[bool, str]:
        return False, detail

    return check


def _met(detail: str = "ready"):
    def check() -> Tuple[bool, str]:
        return True, detail

    return check


@pytest.fixture
def caps(monkeypatch):
    """An empty capability registry, with probe caching and the per-turn log
    signature reset so tests cannot leak state into each other."""
    monkeypatch.setattr(capabilities, "_REGISTRY", {}, raising=True)
    monkeypatch.setattr(al, "_last_withheld_sig", None, raising=True)
    capabilities.invalidate_probes()
    yield capabilities
    capabilities.invalidate_probes()


def _declare(name: str, tools: Tuple[str, ...], *, satisfied: bool) -> None:
    capabilities.register(capabilities.Capability(
        name=name,
        title=name,
        summary=name,
        requirements=(capabilities.Requirement(name=f"{name}-req", check=_met() if satisfied else _unmet()),),
        default_enabled=True,
        tools=tools,
    ))


# ── the gate itself ───────────────────────────────────────────────────────


def test_a_tool_of_an_unavailable_capability_is_withheld(caps):
    _declare("code_delegation", ("delegate_to_claude_code",), satisfied=False)
    kept = al._withhold_unavailable_tools([_schema("delegate_to_claude_code"), _schema("read_file")])
    assert _names(kept) == {"read_file"}


def test_a_tool_of_an_available_capability_is_kept(caps):
    _declare("code_delegation", ("delegate_to_claude_code",), satisfied=True)
    kept = al._withhold_unavailable_tools([_schema("delegate_to_claude_code"), _schema("read_file")])
    assert _names(kept) == {"delegate_to_claude_code", "read_file"}


def test_a_capability_switched_off_by_the_operator_also_withholds(caps, monkeypatch):
    """"Off" and "impossible here" are different reasons with the same effect on
    the schema — an operator's no is as binding as a missing binary."""
    import src.settings as settings

    _declare("code_delegation", ("delegate_to_claude_code",), satisfied=True)
    monkeypatch.setattr(settings, "load_features", lambda: {"code_delegation": False})
    kept = al._withhold_unavailable_tools([_schema("delegate_to_claude_code"), _schema("read_file")])
    assert _names(kept) == {"read_file"}


def test_a_tool_owned_by_no_capability_is_never_withheld(caps):
    """The registry is not an allowlist. Most of Odysseus's ~78 tools belong to
    no capability, and gating must not hide anything by omission."""
    _declare("code_delegation", ("delegate_to_claude_code",), satisfied=False)
    schemas = [_schema(n) for n in ("read_file", "grep", "ask_user", "manage_memory")]
    assert _names(al._withhold_unavailable_tools(schemas)) == _names(schemas)


def test_an_unknown_capability_name_removes_nothing(caps):
    """A capability nobody registered is available by definition — a typo in a
    declaration must not silently delete tools."""
    assert capabilities.is_available("no_such_capability") is True
    schemas = [_schema(n) for n in ("read_file", "delegate_to_claude_code")]
    assert _names(al._withhold_unavailable_tools(schemas)) == _names(schemas)


def test_an_empty_registry_leaves_the_list_untouched(caps):
    schemas = [_schema("read_file")]
    assert al._withhold_unavailable_tools(schemas) is schemas


def test_withholding_every_tool_falls_back_to_the_unfiltered_list(caps, caplog):
    """A model with no tools fails worse than one holding a tool that errors:
    it cannot even report why. So this case is loud and inert."""
    _declare("code_delegation", ("delegate_to_claude_code", "delegate_to_agent"), satisfied=False)
    schemas = [_schema("delegate_to_claude_code"), _schema("delegate_to_agent")]
    with caplog.at_level("ERROR", logger=al.logger.name):
        kept = al._withhold_unavailable_tools(schemas)
    assert kept is schemas
    assert any(r.levelname == "ERROR" and "empty tool list" in r.getMessage() for r in caplog.records)


def test_an_already_empty_list_is_not_treated_as_the_fallback_case(caps):
    """force_answer sends no tools on purpose; that is not a gating failure."""
    _declare("code_delegation", ("delegate_to_claude_code",), satisfied=False)
    assert al._withhold_unavailable_tools([]) == []


def test_a_broken_registry_does_not_cost_the_turn_its_tools(caps, monkeypatch):
    def _boom():
        raise RuntimeError("registry is wedged")

    monkeypatch.setattr(capabilities, "unavailable_tools", _boom)
    schemas = [_schema("delegate_to_claude_code")]
    assert al._withhold_unavailable_tools(schemas) is schemas


# ── logging: once per change, not once per round ──────────────────────────


def test_the_withheld_line_is_logged_once_not_per_round(caps, caplog):
    """21 rounds of the same 3 withheld names is the noise that buries the round
    that actually failed. Mirrors `_last_tool_debug_sig` in the round loop."""
    _declare("code_delegation", ("delegate_to_claude_code",), satisfied=False)
    schemas = [_schema("delegate_to_claude_code"), _schema("read_file")]
    with caplog.at_level("INFO", logger=al.logger.name):
        for _ in range(5):
            al._withhold_unavailable_tools(schemas)
    withheld_lines = [r for r in caplog.records if "withheld" in r.getMessage() and r.levelname == "INFO"]
    assert len(withheld_lines) == 1
    # Names the owning capability, so the reader knows which switch did it.
    assert "code_delegation" in withheld_lines[0].getMessage()


# ── integration: the one site that builds the round's payload ─────────────


def _round(relevant_tools, mcp_schemas=(), **overrides) -> List[Dict]:
    kwargs = dict(
        force_answer=False,
        is_api_model=True,
        relevant_tools=relevant_tools,
        needs_admin=False,
        mcp_schemas=list(mcp_schemas),
        disabled_tools=set(),
        ody_qwen_finetune_model=False,
        last_user="",
    )
    kwargs.update(overrides)
    return al._tool_schemas_for_round(**kwargs)


def test_the_round_payload_drops_an_unavailable_tool(caps):
    _declare("code_delegation", ("delegate_to_claude_code",), satisfied=False)
    names = _names(_round({"delegate_to_claude_code", "read_file", "ask_user"}))
    assert "delegate_to_claude_code" not in names
    assert {"read_file", "ask_user"} <= names


def test_the_round_payload_keeps_an_available_tool(caps):
    _declare("code_delegation", ("delegate_to_claude_code",), satisfied=True)
    assert "delegate_to_claude_code" in _names(_round({"delegate_to_claude_code", "read_file"}))


def test_gating_does_not_unbind_connected_external_mcp_tools(caps):
    """Regression guard for the Penpot incident (commit 9f7062d): a connected
    server's tools bind regardless of RAG selection. Capability gating matches
    exact builtin tool names, so it must leave them alone — including when the
    turn's own selection missed them entirely."""
    _declare("code_delegation", ("delegate_to_claude_code",), satisfied=False)
    penpot = [_schema("mcp__penpot__execute_code"), _schema("mcp__penpot__get_page")]
    names = _names(_round({"delegate_to_claude_code", "ask_user"}, mcp_schemas=penpot, mcp_gated_names=set()))
    assert {"mcp__penpot__execute_code", "mcp__penpot__get_page"} <= names
    assert "delegate_to_claude_code" not in names


def test_force_answer_still_sends_no_tools(caps):
    _declare("code_delegation", ("delegate_to_claude_code",), satisfied=False)
    assert _round({"read_file"}, force_answer=True) == []
