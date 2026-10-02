"""Images a tool returns must reach the model, not just the UI (2026-10-01).

preview_file, Penpot render_preview and browser screenshots all return
``images``; every route's tool message is text-only, so only the user ever saw
them and a designer told to verify visually could not. `_append_tool_results`
now adds one harness-sourced user message with the pixels, the Codex input
builder emits ``input_image`` for it, and old ones are pruned in batches.
"""
import json

import src.agent_loop as al
from src.agent_tools.preview_tools import model_image_followup
from src.chatgpt_subscription import build_responses_input
from src.context_compactor import prune_tool_images

B64 = "iVBORw0KGgo" + "A" * 40


def _img(n=1):
    return [{"data": B64, "mimeType": "image/png"} for _ in range(n)]


def _round(messages, n, images=1, accept=True, name="preview_file"):
    call = {"id": f"call_{n}", "name": name, "arguments": "{}"}
    record = {"tool_name": name, "content": "{}", "result": {"output": "ok", "images": _img(images)}}
    al._append_tool_results(
        messages, "", [call], ["ok"], ["ok"], True, n,
        tool_result_records=[record], accept_tool_images=accept,
    )


def _image_msgs(messages):
    return [m for m in messages if (m.get("metadata") or {}).get("source") == "tool_images"]


def _live(messages):
    return [m for m in _image_msgs(messages)
            if any(b.get("type") == "image_url" for b in m["content"])]


def test_follow_up_message_carries_the_image_after_the_tool_message():
    messages = [{"role": "user", "content": "draw a helmet logo"}]
    _round(messages, 1)
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "user"]
    parts = messages[-1]["content"]
    assert parts[0]["type"] == "text" and "preview_file (call call_1)" in parts[0]["text"]
    assert parts[1] == {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{B64}"}}
    # The tool message itself is unchanged text.
    assert messages[2]["content"] == "ok"


def test_text_only_round_adds_nothing():
    messages = [{"role": "user", "content": "hi"}]
    al._append_tool_results(
        messages, "", [{"id": "c", "name": "ls", "arguments": "{}"}], ["r"], ["r"], True, 1,
        tool_result_records=[{"tool_name": "ls", "content": "", "result": {"output": "r"}}],
    )
    assert _image_msgs(messages) == []


def test_fenced_branch_also_gets_the_follow_up():
    messages = [{"role": "user", "content": "go"}]
    al._append_tool_results(
        messages, "running", [], ["shot"], ["shot"], False, 1,
        tool_result_records=[{"tool_name": "screenshot", "content": "",
                              "result": {"images": _img()}}],
    )
    assert len(_live(messages)) == 1
    assert messages[-1] is _live(messages)[0]


def test_per_round_cap_and_size_cap_are_named_in_the_note():
    follow = model_image_followup(
        [{"tool_name": "t", "result": {"images": _img(5)}}], max_images=3,
    )
    assert sum(1 for b in follow["content"] if b["type"] == "image_url") == 3
    assert "2 more image(s) not shown" in follow["content"][0]["text"]
    big = model_image_followup(
        [{"tool_name": "t", "result": {"images": _img(1)}}], max_chars=10,
    )
    assert all(b["type"] == "text" for b in big["content"])
    assert "not shown" in big["content"][0]["text"]


def test_follow_up_is_not_a_person_request():
    messages = [{"role": "user", "content": "build the logo"}]
    _round(messages, 1)
    follow = messages[-1]
    assert follow["metadata"]["source"] in al.HARNESS_USER_SOURCES
    assert al._is_context_envelope(follow)
    assert al._person_request_text(messages) == "build the logo"
    assert al._latest_user_message(messages)["content"] == "build the logo"
    assert al._extract_last_user_message(messages) == "build the logo"


def test_person_request_for_worker_skips_it(monkeypatch):
    from src.agent_tools import loadout_tools
    import src.ai_interaction as ai

    class Sess:
        owner = None
        history = [
            {"role": "user", "content": "build the logo", "metadata": {}},
            {"role": "user", "content": [{"type": "text", "text": "[Tool images - x]"}],
             "metadata": {"source": "tool_images", "trusted": False}},
        ]

    class Mgr:
        def get_session(self, _sid):
            return Sess()

    monkeypatch.setattr(ai, "get_session_manager", lambda: Mgr())
    monkeypatch.setattr(loadout_tools, "_root_session_id", lambda sid: sid)
    assert loadout_tools.person_request_for_worker("s", None) == "build the logo"


def test_codex_input_has_input_image_and_text_only_shape_is_unchanged():
    messages = [{"role": "user", "content": "go"}]
    _round(messages, 1)
    items = build_responses_input(messages)
    image_item = items[-1]
    assert image_item["role"] == "user"
    assert image_item["content"][0]["type"] == "input_text"
    assert image_item["content"][1] == {
        "type": "input_image", "image_url": f"data:image/png;base64,{B64}",
    }
    # function_call_output stays text.
    assert any(i.get("type") == "function_call_output" and i["output"] == "ok" for i in items)
    # Text-only messages keep the exact old shape, including list content.
    assert build_responses_input([
        {"role": "user", "content": "plain"},
        {"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]},
    ]) == [
        {"role": "user", "content": [{"type": "input_text", "text": "plain"}]},
        {"role": "user", "content": [{"type": "input_text", "text": "a\nb"}]},
    ]


def test_non_vision_model_gets_a_text_note_not_pixels():
    messages = [{"role": "user", "content": "go"}]
    _round(messages, 1, accept=False)
    follow = messages[-1]
    assert follow["metadata"]["source"] == "tool_images"
    assert all(b["type"] == "text" for b in follow["content"])
    assert "does not accept images" in follow["content"][0]["text"]
    assert B64 not in json.dumps(follow)
    assert al._model_takes_tool_images("gpt-6-luna")
    assert al._model_takes_tool_images("claude-sonnet-5-5")
    assert not al._model_takes_tool_images("qwen2.5-coder-7b", "")


def test_images_are_not_pruned_on_a_schedule_only_past_the_cap():
    messages = [{"role": "user", "content": "go"}]
    live_counts = []
    for n in range(1, 16):
        _round(messages, n)
        live_counts.append(len(_live(messages)))
    # No schedule: every image stays until the carried count passes the cap (12).
    assert live_counts[:12] == list(range(1, 13))
    assert live_counts[12] == 2          # the 13th image cuts back to keep=2 at once
    # Placeholders name what they were and carry no pixels.
    live = _live(messages)
    pruned = [m for m in _image_msgs(messages) if all(m is not x for x in live)]
    assert pruned and all("dropped to save context" in m["content"][0]["text"] for m in pruned)
    assert "preview_file (call call_1)" in pruned[0]["content"][0]["text"]
    assert B64 not in json.dumps(pruned)
    # Nothing already sent is rewritten a second time while the window refills.
    before = json.dumps(messages[:12])
    _round(messages, 16)
    assert json.dumps(messages[:12]) == before
    assert prune_tool_images(messages) == 0


def test_prune_does_nothing_inside_the_window():
    messages = [{"role": "user", "content": "go"}]
    for n in range(1, 4):
        _round(messages, n)
    assert prune_tool_images(messages) == 0
    assert len(_live(messages)) == 3


def test_loop_never_writes_its_messages_to_chat_history():
    # The follow-up lives only in the in-turn list; history is written by the
    # chat handler from the final response, so no base64 reaches the database.
    import inspect

    assert "history.append" not in inspect.getsource(al)


def test_prune_tool_images_force_cuts_below_the_cap():
    from src.context_compactor import TOOL_IMAGES_SOURCE, prune_tool_images

    def img(i):
        return {"role": "user", "metadata": {"source": TOOL_IMAGES_SOURCE},
                "content": [{"type": "text", "text": f"{i}. shot"},
                            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}]}

    msgs = [img(i) for i in range(1, 5)]  # 4 images, well under the cap of 12
    assert prune_tool_images(msgs, keep=2) == 0
    assert prune_tool_images(msgs, keep=2, force=True) == 2
