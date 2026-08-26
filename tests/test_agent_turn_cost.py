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


# ── Admin intent must not fire on ordinary words ───────────────────────
#
# Admin intent unions _ADMIN_TOOLS into both the prompt sections and the schema
# list for EVERY round of the turn: ~2,000 tokens of extra schema per round,
# ~35k across a seventeen-round turn. It was a bare substring test, so "docker"
# matched "doc", "observer" matched "server", "multitasking" matched "task".

from src.agent_loop import _detect_admin_intent


def _asked(text):
    return _detect_admin_intent([{"role": "user", "content": text}])


@pytest.mark.parametrize("text", [
    "can you help me with docker compose?",        # doc
    "the observer pattern is confusing",           # server
    "I was multitasking and lost my place",        # task
    "resetting the counter each loop",             # setting
    "summarize this doctor's letter",              # doc
    "what does tokenize mean",                     # token
    "give them the notice",                        # theme stem must not be "them"
    "that is not what I meant",                    # note stem must not be "not"
    "write a poem about the ocean",
])
def test_an_ordinary_word_does_not_buy_the_admin_toolset(text):
    assert not _asked(text), "pays ~2k tokens of schema every round of the turn"


@pytest.mark.parametrize("text", [
    "delete that chat session",
    "list my endpoints",
    "rename this conversation",
    "show me my documents",
    "add a todo",
    "what are my tasks",
    "change my settings",
    "configure the mcp server",
    "set up a cron schedule",
    "switch model please",
    "my api key",
    "second opinion",
    "the 'email tags' task isnt very useful",
])
def test_real_admin_requests_still_fire(text):
    assert _asked(text)


@pytest.mark.parametrize("text", [
    "archiving old emails",
    "managing my webhooks",
    "deleting the token",
    "renaming this chat",
    "scheduling a reminder",
])
def test_inflected_forms_still_fire(text):
    """A silent-e keyword has to keep matching its -ing form."""
    assert _asked(text)


# ── The HTTP embedding lane must not build the fallback it discards ────


def test_the_custom_lane_never_builds_the_fastembed_fallback(monkeypatch):
    """It called a factory whose contract is "HTTP, else FastEmbed", then threw
    a FastEmbed result away for being the wrong type -- after paying for an ONNX
    load and a probe encode, on every offload and every stored-output search."""
    from src import embedding_lanes
    import src.embeddings as embeddings_mod

    embeddings_mod.reset_http_embed_state()
    http_probes, fastembed_builds = [], []

    class _DownHttp:
        def __init__(self, *a, **k):
            http_probes.append(1)

        def get_sentence_embedding_dimension(self):
            raise RuntimeError("endpoint down")

    def _fastembed(*a, **k):
        fastembed_builds.append(1)
        raise AssertionError("the HTTP lane must never build the FastEmbed fallback")

    monkeypatch.setattr(embeddings_mod, "EmbeddingClient", _DownHttp)
    monkeypatch.setattr(embeddings_mod, "FastEmbedClient", _fastembed)

    for _ in range(3):
        with pytest.raises(RuntimeError, match="HTTP embedding lane unavailable"):
            embedding_lanes._build_custom_client()

    assert not fastembed_builds
    # The existing once-per-process latch is preserved: probe once, then skip.
    assert len(http_probes) == 1
    embeddings_mod.reset_http_embed_state()


def test_a_healthy_http_lane_is_still_returned(monkeypatch):
    """The default EmbeddingClient URL counts as a lane worth probing, so this
    must not be short-circuited on "nothing configured"."""
    from src import embedding_lanes
    import src.embeddings as embeddings_mod

    embeddings_mod.reset_http_embed_state()
    monkeypatch.setattr(embedding_lanes, "_load_custom_endpoint", lambda: {}, raising=False)

    class _HealthyHttp:
        url = "http://localhost:11434/v1/embeddings"
        model = "all-minilm"

        def __init__(self, *a, **k):
            pass

        def get_sentence_embedding_dimension(self):
            return 768

    monkeypatch.setattr(embeddings_mod, "EmbeddingClient", _HealthyHttp)
    assert isinstance(embedding_lanes._build_custom_client(), _HealthyHttp)
    embeddings_mod.reset_http_embed_state()


def test_the_recall_schema_advertises_the_ceiling_it_actually_enforces():
    """It said "max 12000" after the ceiling dropped to 8000, so the model asked
    for more than it could get and silently received a shorter slice."""
    from src.agent_loop import FUNCTION_TOOL_SCHEMAS

    schema = next(
        s for s in FUNCTION_TOOL_SCHEMAS
        if s.get("function", {}).get("name") == "recall_tool_output"
    )
    described = schema["function"]["parameters"]["properties"]["limit"]["description"]
    assert str(_RECALL_MAX_SLICE_CHARS) in described
    assert str(_RECALL_SLICE_CHARS) in described
    assert "12000" not in described
