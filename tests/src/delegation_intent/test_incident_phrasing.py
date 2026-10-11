"""The wordings people used to hand work to a named role count as a request for an agent.

Bug (2026-10-10): "have the Lead Engineer worker implement X" and "use the lead
engineer to finish this" were read as ordinary prose, so the explicit delegation
policy never recorded consent and the worker could not be resumed later.
"""
import pytest

from src.delegation_intent import explicit_delegation_requested


@pytest.mark.parametrize("text", [
    "Use the engineering lead agent to implement all of those highest value next changes",
    "have the Lead Engineer worker implement X",
    "use the lead engineer to finish this",
    "ask the Lead Engineer agent to fix it",
    "delegate this to a worker",
    "get the code reviewer to look at the diff",
    "let the designer agent draft the mockup",
])
def test_asking_a_named_role_to_do_the_work_is_explicit_delegation(text):
    assert explicit_delegation_requested(text) is True


@pytest.mark.parametrize("text", [
    "how does the agent loop work?",
    "what did the worker change?",
    "the lead engineer said tests pass",
    "use the agent loop to debug this prompt",
    "use the worker pool size from the config",
    "do not use the lead engineer to finish this",
])
def test_talking_about_agents_is_not_a_request_for_one(text):
    assert explicit_delegation_requested(text) is False
