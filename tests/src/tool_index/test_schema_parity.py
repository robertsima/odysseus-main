"""Every FUNCTION_TOOL_SCHEMAS tool must have a ToolIndex description.

Agent mode selects tools by embedding BUILTIN_TOOL_DESCRIPTIONS and
retrieving the top-K per message. A tool that exists in tool_schemas but has
no description entry can never be retrieved, so the agent advertises the
capability (e.g. API integrations in the system prompt) while the schema is
never actually sent to the model. api_call was missing exactly this way.
"""
from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS


def test_every_schema_tool_has_an_index_description():
    schema_names = {item["function"]["name"] for item in FUNCTION_TOOL_SCHEMAS}

    missing = schema_names - set(BUILTIN_TOOL_DESCRIPTIONS)

    assert not missing, (
        "Tools defined in FUNCTION_TOOL_SCHEMAS but absent from "
        f"BUILTIN_TOOL_DESCRIPTIONS (RAG can never select them): {sorted(missing)}"
    )


def test_api_call_is_indexed_with_a_real_description():
    assert len(BUILTIN_TOOL_DESCRIPTIONS["api_call"]) > 50
