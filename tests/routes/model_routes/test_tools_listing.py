"""GET /api/tools lists every executable tool, disabled ones included.

chatRenderer.js builds its live exec-fence stripper from this list (#3993).
A tool missing from it leaves that tool's executed fence in the live chat
bubble until the page reloads.
"""
from src.agent_tools import TOOL_TAGS


def _listed(client):
    response = client.get("/api/tools")
    assert response.status_code == 200, response.text
    return response.json()["tools"]


def test_every_tool_tag_is_listed(api):
    tools = _listed(api.as_user("alice"))

    assert [tool["id"] for tool in tools] == sorted(TOOL_TAGS)


def test_a_disabled_tool_is_still_listed(api):
    disabled = sorted(TOOL_TAGS)[0]
    response = api.as_admin().post("/api/tools", json={"disabled": [disabled]})
    assert response.status_code == 200, response.text

    tools = {tool["id"]: tool["enabled"] for tool in _listed(api.as_user("alice"))}

    assert tools[disabled] is False
    assert set(tools) == set(TOOL_TAGS)
