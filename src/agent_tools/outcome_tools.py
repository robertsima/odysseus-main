"""`manage_outcomes`: the agent reading and updating the person's outcomes.

An outcome is one intent the person wants closed, linked to the systems that
hold the real item (Todoist task, note, chat, run, PR). The tool is
owner-scoped and cannot overstate progress:

* **Owner from the context.** The tool layer fills `ctx["owner"]` from the
  authenticated session. No argument selects another owner, and one passed
  anyway is ignored.
* **No confirmation.** `confirmed_by_person` belongs to the browser route. The
  tool passes the actor `agent:<session_id>`, which the store never lets
  confirm, so `done` needs evidence the agent attached through `add_evidence`.
* **No priority.** The store refuses an agent-set priority, and a due date the
  person already set.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

from src.tool_utils import _parse_tool_args

logger = logging.getLogger(__name__)

_ACTIONS = ("list", "get", "create", "update", "set_state", "link", "unlink",
            "add_evidence", "set_blocker", "clear_blocker")
_EDITABLE_ARGS = ("title", "detail", "next_action", "waiting_on", "follow_up_at", "due_at",
                  "snoozed_until", "priority", "kind")
_EVENTS_SHOWN = 20


def _err(message: str, **extra: Any) -> Dict[str, Any]:
    return {"error": message, "exit_code": 1, **extra}


def _ok(**payload: Any) -> Dict[str, Any]:
    return {"exit_code": 0, "output": json.dumps(payload, ensure_ascii=False, default=str), **payload}


class ManageOutcomesTool:
    """manage_outcomes: list, create and update outcomes, with evidence-gated completion."""

    async def execute(self, content: str, ctx: dict) -> dict:
        try:
            args = _parse_tool_args(content)
        except ValueError as exc:
            return _err(f"manage_outcomes: invalid JSON arguments ({exc})")
        action = str(args.get("action") or "list").strip().lower()
        if action not in _ACTIONS:
            return _err(f"manage_outcomes: unknown action {action!r} (use one of {', '.join(_ACTIONS)})")

        from src.owner_identity import effective_storage_owner

        owner = effective_storage_owner((ctx or {}).get("owner"))
        if not owner:
            return _err("manage_outcomes: no signed-in user for this call")
        session_id = str((ctx or {}).get("session_id") or "")
        actor = f"agent:{session_id}" if session_id else "agent:unknown"

        try:
            return self._run(action, args, owner, actor, session_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("manage_outcomes failed: %s", exc, exc_info=True)
            return _err(f"manage_outcomes: {exc}")

    @staticmethod
    def _run(action: str, args: Dict[str, Any], owner: str, actor: str, session_id: str) -> Dict[str, Any]:
        from src import outcomes as store
        from src.blockers import Blocker

        def result(res: Dict[str, Any]) -> Dict[str, Any]:
            if not res.get("ok"):
                return _err(f"manage_outcomes: {res.get('error') or 'failed'}", code=res.get("code"))
            return _ok(**{k: v for k, v in res.items() if k in ("outcome", "link", "created")})

        outcome_id = str(args.get("id") or args.get("outcome_id") or "").strip()

        if action == "list":
            states = args.get("states") or args.get("state")
            if isinstance(states, str):
                states = [s.strip() for s in states.split(",") if s.strip()]
            return _ok(outcomes=store.list_for(owner, states or None, limit=int(args.get("limit") or 50)))

        if action == "create":
            fields = {k: args[k] for k in _EDITABLE_ARGS if k in args and k != "title"}
            if "links" in args and isinstance(args["links"], list):
                fields["links"] = args["links"]
            if args.get("state"):
                fields["state"] = args["state"]
            return result(store.create(owner, str(args.get("title") or ""), source="agent", actor=actor, **fields))

        if not outcome_id:
            return _err(f"manage_outcomes: id is required for action={action!r}")

        if action == "get":
            data = store.get(owner, outcome_id, events=_EVENTS_SHOWN)
            return _ok(outcome=data) if data else _err("manage_outcomes: outcome not found", code="not_found")
        if action == "update":
            return result(store.update(owner, outcome_id, {k: args[k] for k in _EDITABLE_ARGS if k in args}, actor))
        if action == "set_state":
            return result(store.set_state(owner, outcome_id, str(args.get("state") or ""), actor,
                                          waiting_on=args.get("waiting_on"), follow_up_at=args.get("follow_up_at")))
        if action == "link":
            return result(store.add_link(owner, outcome_id, str(args.get("link_kind") or ""),
                                         str(args.get("ref") or ""), label=args.get("label"), actor=actor))
        if action == "unlink":
            link_id = str(args.get("link_id") or "")
            return result(store.remove_link(owner, outcome_id, link_id, actor)) if link_id else \
                _err("manage_outcomes: link_id is required for action='unlink'")
        if action == "add_evidence":
            return result(store.add_evidence(owner, outcome_id, {
                "kind": args.get("evidence_kind"), "ref": args.get("ref"), "detail": args.get("detail")}, actor))
        if action == "set_blocker":
            recovery = str(args.get("recovery") or "user")
            if recovery not in ("user", "retry"):
                return _err("manage_outcomes: recovery must be 'user' or 'retry'")
            try:
                blocker = Blocker(code="AGENT_REPORTED", summary=str(args.get("summary") or "").strip(),
                                  recovery=recovery, user_action=args.get("user_action"),
                                  source={"kind": "chat", "ref": session_id})
            except ValueError:
                return _err("manage_outcomes: a blocker that needs the person must give user_action, "
                            "the one concrete step they should take")
            if not blocker.summary:
                return _err("manage_outcomes: summary is required for action='set_blocker'")
            return result(store.set_blocker(owner, outcome_id, blocker, actor))
        return result(store.set_blocker(owner, outcome_id, None, actor))   # clear_blocker


__all__ = ["ManageOutcomesTool"]
