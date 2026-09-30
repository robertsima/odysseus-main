"""What format_tool_result shows the model (src/tool_execution.py)."""

from src.tool_execution import format_tool_result


def test_an_error_beside_output_reaches_the_model():
    text = format_tool_result("bash: npm test", {
        "output": "PASS a.test.js\n",
        "error": "stopped: printed nothing for 60 s; rerun with idle_timeout",
        "exit_code": 124,
    })
    assert "PASS a.test.js" in text
    assert "**Error:** stopped: printed nothing for 60 s" in text


def test_an_error_is_shown_once():
    text = format_tool_result("read_file: x", {"error": "not found", "exit_code": 1})
    assert text.count("not found") == 1
    failed_write = format_tool_result("write_file: x", {"success": False, "error": "denied"})
    assert failed_write.count("denied") == 1


def test_discover_tools_does_not_print_the_schemas_it_attaches():
    schema = {"type": "function", "function": {
        "name": "web_search", "description": "Search the public web " + "x" * 3000,
        "parameters": {"type": "object", "properties": {}},
    }}
    text = format_tool_result("discover_tools: search", {
        "output": "Loaded web_search.",
        "exit_code": 0,
        "continue_same_turn": True,
        "loaded_names": ["web_search"],
        "loaded_tools": [schema],
        "discovery": {"tools": [schema], "loaded_names": ["web_search"]},
    })
    assert "Loaded web_search." in text
    assert "x" * 100 not in text
    assert '"loaded_names"' in text  # small structured fields still reach the model
