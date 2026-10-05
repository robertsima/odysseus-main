"""The grounding helper must not mistake appended retrieval for the request.

Dynamic context (memories, vault hits) is appended after the user's request
as an "UNTRUSTED SOURCE DATA" user message so the cached prefix stays stable.
Before this fix the helper read that block as the latest user message, found
it did not match the request, and appended the request again — duplicating
it (and the persona-sized prompts some users send) on every turn that had
retrieval, and logging a "latest user context mismatch" warning each time.
"""
from routes.chat_routes import _ensure_current_request_is_latest_user, _last_user_plain_text
from src.prompt_security import UNTRUSTED_CONTEXT_HEADER

REQUEST = "Help me think of a name for an AI app based on Pavlovian conditioning."


def _untrusted(text: str) -> str:
    return UNTRUSTED_CONTEXT_HEADER + "\n<<<UNTRUSTED_SOURCE_DATA>>>\n" + text + "\n<<<END_UNTRUSTED_SOURCE_DATA>>>"


def test_appended_retrieval_is_not_the_latest_user_message():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": REQUEST},
        {"role": "user", "content": _untrusted("[memory] user likes short names")},
    ]
    assert _last_user_plain_text(messages) == REQUEST
    assert _ensure_current_request_is_latest_user(messages, REQUEST) is messages


def test_multimodal_untrusted_block_is_skipped_too():
    messages = [
        {"role": "user", "content": [{"type": "text", "text": REQUEST}]},
        {"role": "user", "content": [{"type": "text", "text": _untrusted("doc excerpt")}]},
    ]
    assert _last_user_plain_text(messages) == REQUEST
    assert len(_ensure_current_request_is_latest_user(messages, REQUEST)) == 2


def test_request_is_still_appended_when_genuinely_missing():
    messages = [
        {"role": "user", "content": "an older question"},
        {"role": "assistant", "content": "an older answer"},
    ]
    repaired = _ensure_current_request_is_latest_user(messages, REQUEST)
    assert repaired[-1] == {"role": "user", "content": REQUEST}
    assert len(repaired) == 3


def test_context_prefixed_messages_are_skipped():
    messages = [
        {"role": "user", "content": REQUEST},
        {"role": "user", "content": "[Context — active document]\n..."},
    ]
    assert _last_user_plain_text(messages) == REQUEST
