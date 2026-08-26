"""Per-round cost of an agent turn: wasted rounds and repeated work.

Traced from a real turn (2026-08-26, 17 rounds, 17k -> 45k prompt tokens):

    round  4  read_file      -> offloaded 20,216 chars as ref A
    round  5  recall(A, query=...)
    round  6  recall(A, offset=0,     limit=12000) -> OFFLOADED (12,255 chars)
    round  7  recall(A, offset=12000, limit=12000) -> OFFLOADED (8,387 chars)
    rounds 11,12,13  read_file the same path, three more times

The recalls were offloaded because the recall tool's own ceiling (12,000) sat
ABOVE the widest inline budget (8,000), so asking for the biggest allowed slice
guaranteed it was too big to keep. The model never received the content it
asked for, so it went back and re-read the file.
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest

# Imported at collection time, before any test can mock out `src.agent_tools`
# (another suite does, and an in-test import would pick up the mock).
from src.agent_tools.rag_tools import _RECALL_SLICE_CHARS, _RECALL_MAX_SLICE_CHARS


# ── A recall must reach the model whole ────────────────────────────────


def test_a_recall_is_never_offloaded(tmp_path, monkeypatch):
    import src.constants as constants
    from src import tool_output_store as tos

    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path), raising=False)
    body = "line\n" * 4000  # ~20k chars, far past every inline limit

    # The control: an ordinary tool result this size IS offloaded. (`read_file`
    # would be a bad control -- it carries a 20k per-tool floor of its own.)
    trimmed, record = tos.maybe_offload(body, tool="bash", profile={"tool_output_inline_limit": 2000})
    assert record is not None and len(trimmed) < len(body)

    # The recall is not — otherwise the model gets an excerpt of an excerpt and
    # the paging loop cannot terminate.
    kept, record = tos.maybe_offload(body, tool="recall_tool_output", profile={"tool_output_inline_limit": 2000})
    assert record is None
    assert kept == body


def test_the_exemption_is_declared_not_hardcoded_at_one_call_site():
    from src.tool_output_store import _NEVER_OFFLOAD_TOOLS

    assert "recall_tool_output" in _NEVER_OFFLOAD_TOOLS


def test_recall_ceiling_stays_within_the_widest_inline_budget():
    """The rule the module states for the default has to hold for the max too."""
    from src.context_profiles import PRESETS

    widest = max(p["values"]["tool_output_inline_limit"] for p in PRESETS.values())
    assert _RECALL_MAX_SLICE_CHARS <= widest, (
        "a recall the model is allowed to request must not exceed what the loop "
        "would keep inline, or every such recall is re-offloaded"
    )
    assert _RECALL_SLICE_CHARS <= _RECALL_MAX_SLICE_CHARS


# ── Repeated per-call work ─────────────────────────────────────────────


def test_the_fastembed_fallback_client_is_built_once(monkeypatch):
    """It was rebuilt (ONNX load + probe encode) on every offload and search."""
    from src import embedding_lanes

    builds = []

    class _FakeClient:
        def get_sentence_embedding_dimension(self):
            return 384

    def _fake_import():
        builds.append(1)
        return _FakeClient()

    monkeypatch.setattr(embedding_lanes, "_fastembed_client", None, raising=False)
    monkeypatch.setattr(embedding_lanes, "_build_fastembed_client",
                        embedding_lanes._build_fastembed_client, raising=False)

    import src.embeddings as embeddings_mod
    monkeypatch.setattr(embeddings_mod, "FastEmbedClient",
                        lambda *a, **k: _fake_import(), raising=False)

    first = embedding_lanes._build_fastembed_client()
    second = embedding_lanes._build_fastembed_client()
    third = embedding_lanes._build_fastembed_client()

    assert first is second is third
    assert len(builds) == 1, f"rebuilt {len(builds)} times"

    # The documented reset hook must still drop it.
    embedding_lanes.reset_embedding_lane_state()
    embedding_lanes._build_fastembed_client()
    assert len(builds) == 2


def test_a_failed_fastembed_build_is_not_cached(monkeypatch):
    """A model still downloading must be retried, not remembered as broken."""
    from src import embedding_lanes
    import src.embeddings as embeddings_mod

    monkeypatch.setattr(embedding_lanes, "_fastembed_client", None, raising=False)

    def _boom(*a, **k):
        raise RuntimeError("fastembed not installed")

    monkeypatch.setattr(embeddings_mod, "FastEmbedClient", _boom, raising=False)
    with pytest.raises(RuntimeError):
        embedding_lanes._build_fastembed_client()
    assert embedding_lanes._fastembed_client is None


def test_the_admin_check_on_the_tool_hot_path_reuses_one_auth_manager(monkeypatch):
    """It ran per tool call and built a throwaway AuthManager each time."""
    import core.auth as auth_mod
    from src import tool_security

    built = []
    real_init = auth_mod.AuthManager.__init__

    def _counting_init(self, *a, **k):
        built.append(1)
        return real_init(self, *a, **k)

    monkeypatch.setattr(auth_mod.AuthManager, "__init__", _counting_init)
    auth_mod.reset_shared_auth_managers()

    for _ in range(5):
        tool_security.owner_is_admin_or_single_user("someone")

    assert len(built) <= 1, f"built {len(built)} AuthManagers for five tool calls"
