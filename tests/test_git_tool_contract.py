

def test_fetch_drops_a_redundant_origin_remote_but_fetch_branch_keeps_it():
    """2026-09-28: `fetch` with remote="origin" was refused as an unsupported
    argument; it is the default, so it is dropped instead."""
    from src.git_tool_contract import normalize_git_arguments

    assert normalize_git_arguments({"action": "fetch", "repository": "/r", "remote": "origin"}) == {
        "action": "fetch", "repository": "/r"}
    # A different remote is still an unsupported override, not silently ignored.
    assert "remote" in normalize_git_arguments({"action": "fetch", "repository": "/r", "remote": "upstream"})
    # fetch_branch takes a remote; any other remote than its origin default is kept.
    assert normalize_git_arguments(
        {"action": "fetch_branch", "repository": "/r", "remote": "upstream", "remote_branch": "main"}
    )["remote"] == "upstream"
