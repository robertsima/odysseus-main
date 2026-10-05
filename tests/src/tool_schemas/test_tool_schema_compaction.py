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
    assert len(json.dumps(compact)) <= len(json.dumps(canonical))

    lean = compact_function_tool_schemas(canonical, lean=True)
    assert '"description": "' not in json.dumps(lean[0]["function"]["parameters"])
    assert lean[0]["function"]["parameters"]["required"] == ["query", "filters"]
    assert canonical == before


def _tool(description, properties):
    return {"type": "function", "function": {
        "name": "t", "description": description,
        "parameters": {"type": "object", "properties": properties},
    }}


def test_a_parameter_named_description_survives_both_levels():
    # The first stripper popped every "description" key, including the
    # manage_calendar/manage_skills/manage_agent_loadout parameter of that name.
    canonical = [_tool("Create an event.", {
        "title": {"type": "string"},
        "description": {"type": "string", "description": "Event notes shown in the calendar."},
    })]
    for lean in (False, True):
        params = compact_function_tool_schemas(canonical, lean=lean)[0]["function"]["parameters"]
        assert set(params["properties"]) == {"title", "description"}
        assert params["properties"]["description"]["type"] == "string"


def test_descriptions_keep_whole_sentences_and_do_not_break_on_abbreviations():
    text = (
        "Ask the user a multiple-choice question when the answer changes what you do "
        "next (e.g. pick between approaches, confirm an assumption). The user sees "
        "clickable option buttons; calling this ENDS your turn. " + "Filler sentence. " * 40
    )
    fn = compact_function_tool_schemas([_tool(text, {})])[0]["function"]
    assert "(e.g. pick between approaches, confirm an assumption)" in fn["description"]
    assert "calling this ENDS your turn." in fn["description"]
    assert len(fn["description"]) <= 400 and fn["description"].endswith("Filler sentence.")
    lean_fn = compact_function_tool_schemas([_tool(text, {})], lean=True)[0]["function"]
    assert len(lean_fn["description"]) <= 220
    assert "(e.g" in lean_fn["description"] and not lean_fn["description"].endswith("(e.g")


def test_parameter_prose_is_kept_on_the_standard_level():
    canonical = [_tool("Run a command.", {"idle_timeout": {
        "type": "integer",
        "description": "Seconds this command may print nothing before it is stopped (default 60, max 3600).",
    }})]
    prop = compact_function_tool_schemas(canonical)[0]["function"]["parameters"]["properties"]["idle_timeout"]
    assert "default 60, max 3600" in prop["description"]


def test_compact_function_tool_schemas_leaves_non_function_tools_unchanged():
    schema = {"type": "custom", "name": "opaque", "description": "Provider-owned payload"}
    assert compact_function_tool_schemas([schema]) == [schema]
