import copy
import json

# agent_tools defines ToolBlock before importing tool_schemas, which is the
# production import order for this intentionally split legacy module pair.
from src.agent_tools import FUNCTION_TOOL_SCHEMAS  # noqa: F401
from src.tool_schemas import compact_function_tool_schemas


def test_compact_function_tool_schemas_preserves_callable_json_shape():
    canonical = [{
        "type": "function",
        "function": {
            "name": "search_private_vault",
            "description": "Search the private Vault Mind. This sentence is not needed by the provider.",
            "parameters": {
                "type": "object",
                "required": ["query", "filters"],
                "properties": {
                    "query": {"type": "string", "description": "Natural language search query."},
                    "filters": {
                        "type": "object",
                        "description": "Privacy-safe filters.",
                        "required": ["visibility"],
                        "properties": {
                            "visibility": {"type": "string", "enum": ["private", "shared"], "description": "Scope."},
                            "tags": {"type": "array", "items": {"type": "string", "description": "One tag."}},
                        },
                    },
                },
            },
        },
    }]
    before = copy.deepcopy(canonical)

    compact = compact_function_tool_schemas(canonical)

    assert canonical == before  # canonical validation/execution contract is untouched
    assert compact[0]["function"]["name"] == "search_private_vault"
    params = compact[0]["function"]["parameters"]
    assert params["required"] == ["query", "filters"]
    assert params["properties"]["filters"]["required"] == ["visibility"]
    assert params["properties"]["filters"]["properties"]["visibility"]["enum"] == ["private", "shared"]
    assert '"description"' not in json.dumps(params)
    assert len(json.dumps(compact)) < len(json.dumps(canonical))


def test_compact_function_tool_schemas_leaves_non_function_tools_unchanged():
    schema = {"type": "custom", "name": "opaque", "description": "Provider-owned payload"}
    assert compact_function_tool_schemas([schema]) == [schema]
