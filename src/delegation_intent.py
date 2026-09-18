"""Whether the human asked for other agents to be started.

Moved out of the fork's agent loop when the loop was replaced by upstream's
(2026-09-18). Pure text recognisers with no loop state, so they can serve
`src.agent_workflows` now and the re-ported routing later from one place.

* `explicit_delegation_requested(text)` -- the human asked to hand work to
  another agent, in any of the hand-off or start-an-agent wordings.
* `delegation_intent_text(messages)` -- the human text that authorization is
  judged on, carried across a bare "continue".
* `orchestration_requested(text)` -- the broader orchestration recogniser the
  explicit check falls back to.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional


# Nouns that already mean "some other agent" without a verb in front.
_AGENT_NOUN_RE = re.compile(
    r"\b(?:sub[\s-]?agents?|another\s+agent|other\s+agents?|worker\s+agents?|"
    r"agent\s+loadouts?|loadouts?|claude\s*code|claude\s+agent|"
    r"coding\s+agent|coding\s+harness)\b",
    re.IGNORECASE,
)


# A start/hand-off verb followed, within a few words, by a bare agent noun.
# The filler is capped at three words so "the agent asked me to start the
# server" cannot pair a distant verb with an unrelated "agent".
#
# The `re-` prefixes are not decoration. The second half of a failed run is the
# RESTART, and "restart the two research agents" / "relaunch the specialists"
# read as no request at all without them: in the 2026-09-17 logs every child
# died on a provider 401, and each retry the user asked for was then refused by
# the delegation gate for lack of an "explicit specialist request". `specialist`
# joins the noun list for the same reason -- it is the word this tool's own
# `specialists` parameter uses, so it is the word people type back at it.
_ORCHESTRATION_VERB = (
    r"(?:kick(?:ing|ed|s)?\s+off|spin(?:ning|s)?\s+up|fir(?:e|es|ing)\s+up|"
    r"stand(?:ing|s)?\s+up|boot(?:ing|s)?\s+up|set(?:ting|s)?\s+up|"
    r"(?:re[\s-]?)?start(?:ing|ed|s)?|creat(?:e|es|ing|ed)\s+an?|mak(?:e|es|ing)\s+an?|"
    r"(?:re[\s-]?)?launch(?:ing|ed|es)?|re[\s-]?tr(?:y|ies|ying)|"
    r"spawn(?:ing|ed|s)?|dispatch(?:ing|ed|es)?|"
    r"deleg\w+|farm(?:ing|ed|s)?\s+out|assign(?:ing|ed|s)?|"
    r"hand(?:ing|ed|s)?(?:\s+\w+){0,3}?\s+(?:to|off)|"
    r"have\s+an?|ask\s+an?|get\s+an?|give\s+(?:it|this|that)\s+to\s+an?)"
)


_AGENT_NOUN_TAIL = r"(?:agent|worker|specialist)s?\b"


_AGENT_ORCHESTRATION_RE = re.compile(
    r"\b" + _ORCHESTRATION_VERB + r"\W+(?:\w+\W+){0,3}?" + _AGENT_NOUN_TAIL,
    re.IGNORECASE,
)


# "run some agents" is the same request as "kick off some agents", but `run` is
# far too common to sit in _ORCHESTRATION_VERB: "run the agent tests", "running
# the agent loop", "we run several worker threads" would all unlock the
# delegation toolset. What separates the two is whether the agent noun is the
# HEAD of its phrase or a MODIFIER of the next one — you run *an agent*, but you
# run *agent tests*. So `run` gets its own pattern requiring the noun to be
# followed by a clause boundary or a function word, never by another noun.
#
# This is deliberately a separate regex rather than another alternative in
# _ORCHESTRATION_VERB: applying the head-noun lookahead to the existing verbs
# would break "spin up a worker agent", where the first noun matched ("worker")
# is legitimately followed by another ("agent").
_HEAD_NOUN_FOLLOWERS = (
    r"(?=\s*(?:$|[,.;:!?)\]]|\b(?:that|which|who|to|on|for|with|and|or|in|at|"
    r"so|then|about|from|until|while|please|now|here|again|instead)\b))"
)


_AGENT_RUN_RE = re.compile(
    r"\brun(?:ning|s)?\W+(?:\w+\W+){0,3}?(?:agent|worker)s?\b" + _HEAD_NOUN_FOLLOWERS,
    re.IGNORECASE,
)


_USING_AGENTS_RE = re.compile(
    r"\b(?:launch|start|run|perform|conduct|execute|complete|audit|review|"
    r"investigate|analy[sz]e|check|do)\b.{0,180}?"
    r"\busing\s+(?:\*{1,2})?(?:ai\s+)?agents?\b",
    re.IGNORECASE | re.DOTALL,
)


_USE_AGENTS_RE = re.compile(
    r"\b(?:use|using|with|employ|coordinate)\s+(?:[\w-]+\s+){0,4}"
    r"(?:agents|specialists|workers)\b" + _HEAD_NOUN_FOLLOWERS,
    re.IGNORECASE,
)


# Prohibition, not mere co-occurrence of a negative word and an agent noun.
#
# The old form was `(do not|don't|never|without|no) ... {0,65} ... agents`
# applied to the WHOLE message, which made two very ordinary sentences read as
# "the user forbade delegation": "no agents actually ran" and "there's no
# evidence the agents did anything" are reports about a failed run, and they
# arrive in exactly the turn where the user is asking for a restart. Two
# narrowings:
#
#   1. Only a directive negation ("do not"/"don't"/"never") may bind to a
#      delegation word across filler. "didn't", "isn't", "no longer" and the
#      rest are descriptions of what happened, not instructions.
#   2. Bare "no"/"without" must govern the agent noun DIRECTLY ("no sub-agents",
#      "without workers"), and not when that noun is the subject of a verb
#      saying what the agents did or failed to do.
_NO_DELEGATION_RE = re.compile(
    r"(?:"
    r"\b(?:do\s+not|do\s*n'?t|never|please\s+do\s*n'?t)\b[^.!?\n]{0,65}"
    r"\b(?:delegat\w*|sub[ -]?agents?|agents|workers|specialists)\b"
    r"|"
    r"\b(?:without|no)\s+(?:any\s+|more\s+|further\s+|additional\s+|other\s+|new\s+)*"
    r"(?:delegation|sub[ -]?agents?|agents|workers|specialists)\b"
    r"(?!\s+(?:ran|run|launched|started|fired|were|was|have|has|had|did|"
    r"actually|ever|even|executed|completed|finished|failed|made|produced|"
    r"reported|returned|appear\w*|show\w*|exist\w*))"
    r")",
    re.IGNORECASE,
)


_DELEGATION_GUIDANCE_RE = re.compile(
    r"^\s*(?:(?:what|why|when|where|who|which)\b|how\s+(?:do|can|would|should|to)\b|"
    r"(?:explain|describe|discuss)\b)", re.I
)


_WORKFLOW_CONTROL_RE = re.compile(
    r"^\s*(?:(?:please|can you|could you)\s+)?"
    r"(?:cancel|stop|pause|wait|status|progress|results|"
    r"(?:show|check|get)(?:\s+me)?(?:\s+the)?\s+(?:status|progress|results))\b",
    re.I,
)


# Sentence/contrast boundaries. Negation and the "is this a question about
# delegation" test are both properties of ONE clause, not of the message, and a
# real request routinely arrives in the same message as an unrelated negative:
# "The last run produced nothing usable -- no agents ran. Relaunch the two
# research specialists." Evaluating that as one blob vetoed the request.
#
# The contrast words use a fixed-width lookbehind rather than a leading `\s+`.
# `\s+(?:but|...)` makes the engine rescan a whitespace run from every position
# in it -- quadratic, and measurably so: 20k spaces took 5.7 seconds, on a path
# that runs against arbitrary user text on every turn.
_CLAUSE_SPLIT_RE = re.compile(
    r"[.!?\n]+|(?<=\s)(?:but|however|instead|although|though)\b", re.I
)


def _clauses(text: str) -> List[str]:
    return [part for part in _CLAUSE_SPLIT_RE.split(str(text or "")) if part and part.strip()]


def _clause_orchestration(clause: str) -> bool:
    """Does THIS clause ask for another agent, rather than forbid or ask about one?"""
    if _NO_DELEGATION_RE.search(clause) or _DELEGATION_GUIDANCE_RE.search(clause):
        return False
    return bool(
        _AGENT_NOUN_RE.search(clause)
        or _AGENT_ORCHESTRATION_RE.search(clause)
        or _AGENT_RUN_RE.search(clause)
        or _USING_AGENTS_RE.search(clause)
        or _USE_AGENTS_RE.search(clause)
    )


def orchestration_requested(text: str) -> bool:
    """True when the words name another agent, or ask for one to be started.

    A prohibition still wins over a request inside the same clause (that is
    what `_clause_orchestration` checks), but it no longer reaches across a
    sentence boundary to cancel a request the user made in plain words.
    """
    text = str(text or "")
    # `_USING_AGENTS_RE` deliberately spans up to 180 characters of filler
    # ("audit the pipeline end to end ... using agents"), which a clause split
    # can cut in half, so it also gets a whole-message pass -- guarded by the
    # message-level prohibition check that has always applied to it.
    if not (_NO_DELEGATION_RE.search(text) or _DELEGATION_GUIDANCE_RE.search(text)):
        if _USING_AGENTS_RE.search(text):
            return True
    return any(_clause_orchestration(clause) for clause in _clauses(text))


_EXPLICIT_DELEGATION_RE = re.compile(
    r"\b(?:delegate|hand\s+off|sub[ -]?agent|another\s+agent|other\s+agent|"
    r"ask\s+(?:claude\s+code|a\s+worker|another\s+agent)|"
    r"have\s+(?:claude\s+code|an?\s+agent|a\s+worker)|"
    r"send\s+(?:this|it)\s+to\s+(?:another\s+chat|an?\s+agent))\b",
    re.IGNORECASE,
)


def explicit_delegation_requested(text: str) -> bool:
    """True only when the human asked to hand work to another agent.

    The literal-phrase list above was written for the hand-off wordings and
    missed the start-an-agent ones entirely: "ok just kick off a claude agent
    then and have it do it" read as no delegation request, so the default
    `explicit` policy disabled all of _DELEGATION_TOOLS -- after retrieval had
    already found them -- and the turn answered that it could not launch an
    agent. orchestration_requested is the same recogniser admin routing uses,
    so the two gates can no longer disagree about the same sentence.
    """
    text = str(text or "")
    # Anchored at the start of the message: "cancel the workflow", "status?".
    # Those are controls over work already authorized, not new authorization.
    if _WORKFLOW_CONTROL_RE.search(text):
        return False
    for clause in _clauses(text):
        if _NO_DELEGATION_RE.search(clause) or _DELEGATION_GUIDANCE_RE.search(clause):
            continue
        if _EXPLICIT_DELEGATION_RE.search(clause):
            return True
    return orchestration_requested(text)


def delegation_intent_text(messages: List[Dict]) -> str:
    """Carry explicit human authorization across 'continue', never skill prose."""
    human = [text for msg in messages if (text := _user_intent_text(msg)) is not None]
    latest = human[-1] if human else ""
    if not _is_explicit_continuation(latest):
        return latest
    for prior in reversed(human[:-1]):
        if _is_explicit_continuation(prior):
            continue
        return prior if explicit_delegation_requested(prior) else latest
    return latest


def _user_intent_text(msg: Dict) -> Optional[str]:
    """Separate human intent from user-role context and runtime envelopes."""
    from src.intent_assessment import human_user_text
    return human_user_text(msg)


def _is_explicit_continuation(text: str) -> bool:
    """Only these terse replies may inherit older user turns for tool retrieval."""
    from src.intent_assessment import is_explicit_continuation
    return is_explicit_continuation(text)
