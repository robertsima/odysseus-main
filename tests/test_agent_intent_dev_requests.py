"""Coding / source-control turns must not be routed as model serving.

2026-09-13 logs: "Push changes and start with the next slice implement as much
as would make sense to finish MVP ASAP" matched the Cookbook domain on the bare
word "start". The turn got serve_model/download_model instead of shell and
Claude Code tools, and the agent answered that it could not push.
"""
from src.agent_loop import _classify_agent_request
from src.tool_index import ToolIndex


def _domains(text):
    return _classify_agent_request([{"role": "user", "content": text}], text)["domains"]


def test_push_and_implement_request_is_files_not_cookbook():
    d = _domains("Push changes and start with the next slice implement as much as would make sense to finish MVP ASAP")
    assert "cookbook" not in d
    assert "files" in d


def test_generic_verbs_do_not_imply_model_serving():
    for text in ("start the next feature", "pull the latest changes", "restart the server and try again",
                 "download the report and summarise it"):
        assert "cookbook" not in _domains(text), text


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
