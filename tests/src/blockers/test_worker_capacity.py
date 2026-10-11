"""The capacity blocker says whether the slot will free on its own."""
from src.blockers import worker_capacity


def test_a_full_chat_waits_on_its_own_workers_and_needs_nobody():
    blocker = worker_capacity(limit=1, running=1, session_id="chat-1")

    assert blocker.code == "WORKER_CAPACITY" and blocker.recovery == "auto"
    assert blocker.user_action is None
    assert blocker.detail == {"limit": 1, "running": 1}
    assert blocker.source == {"kind": "chat", "ref": "chat-1"}


def test_a_chat_that_allows_no_workers_needs_the_person_to_raise_the_limit():
    """Waiting changes nothing when the limit itself is 0; saying 'auto' would hide that."""
    blocker = worker_capacity(limit=0, running=0, session_id="chat-1")

    assert blocker.recovery == "user"
    assert "Child workers" in blocker.user_action
