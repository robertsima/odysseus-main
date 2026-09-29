import json
import logging

logger = logging.getLogger(__name__)

class AskUserTool:
    async def execute(self, content, ctx):
        """
        ask_user: the agent poses a multiple-choice question to the user to get a
        decision/clarification. This is a pure UI-control marker — no subprocess,
        no filesystem. It returns an `ask_user` payload that the agent loop turns
        into an `ask_user` SSE event and then ENDS the turn, so the chat waits for
        the user's selection (their choice arrives as the next message).
        """
        question, options, multi = "", [], False
        raw = (content or "").strip()
        try:
            parsed = json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            parsed = {}

        if isinstance(parsed, dict):
            question = str(parsed.get("question", "")).strip()
            multi = bool(parsed.get("multi") or parsed.get("multiSelect"))
            for opt in (parsed.get("options") or []):
                if isinstance(opt, dict):
                    label = str(opt.get("label", "")).strip()
                    descr = str(opt.get("description", "")).strip()
                elif isinstance(opt, str):
                    label, descr = opt.strip(), ""
                else:
                    continue
                if label:
                    options.append({"label": label, "description": descr})
        else:
            question = raw

        if not question or len(options) < 2:
            return "ask_user: invalid", {
                "error": (
                    "ask_user needs a non-empty `question` and at least 2 `options` "
                    "(each an object with a `label`, optional `description`)."
                ),
                "exit_code": 1,
            }

        options = options[:6]  # keep the choice list sane
        desc = f"ask_user: {question[:80]}"
        labels = ", ".join(o["label"] for o in options)
        result = {
            "ask_user": {"question": question, "options": options, "multi": multi},
            "output": f"Asked the user: {question}\nOptions: {labels}\nAwaiting their selection.",
            "exit_code": 0,
        }
        logger.info("Tool executed: %s (%d options, multi=%s)", desc, len(options), multi)
        return desc, result

class UpdatePlanTool:
    async def execute(self, content, ctx):
        """
        update_plan: write the chat's task checklist -- an approved plan being
        executed, or the steps of any multi-part request. It is saved on the
        chat (src.task_checklist) and shown to the agent on later turns while
        items are open; an empty plan clears it. The `plan_update` payload also
        becomes a `plan_update` SSE event. Does NOT end the turn.
        """
        from src import task_checklist

        raw = (content or "").strip()
        plan = ""
        try:
            parsed = json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            parsed = {}

        if isinstance(parsed, dict) and "plan" in parsed:
            plan = str(parsed.get("plan") or "").strip()
        else:
            plan = raw

        session_id = (ctx or {}).get("session_id")
        if task_checklist.is_clear_request(plan):
            task_checklist.save(session_id, "")
            logger.info("Tool executed: update_plan: cleared")
            return "update_plan: cleared", {
                "plan_update": {"plan": ""},
                "output": "Checklist cleared.",
                "exit_code": 0,
            }

        plan = plan[:task_checklist.MAX_CHARS]
        done, total = task_checklist.counts(plan)
        task_checklist.save(session_id, plan)
        desc = f"update_plan: {done}/{total} done" if total else "update_plan"
        result = {
            "plan_update": {"plan": plan},
            "output": (f"Checklist saved ({done}/{total} done):\n{plan}" if total else "Checklist saved."),
            "exit_code": 0,
        }
        logger.info("Tool executed: %s", desc)
        return desc, result