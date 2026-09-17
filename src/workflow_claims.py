"""Execution receipts, not model prose, decide whether research agents ran."""

from typing import Any


def record_execution(receipts: dict, tool: str, result: dict[str, Any]) -> None:
    if tool == "orchestrate_agents" and result.get("workflow_id"):
        receipts[str(result["workflow_id"])] = {
            key: result.get(key) for key in
            ("workflow_id", "status", "requested_agents", "launched_agents", "synthesis_status")
        }
    elif tool in {"manage_agent_loadout", "send_to_session"} and not result.get("error") and result.get("exit_code") in (None, 0):
        # Loading/creating a profile or a skill is not a launched agent.
        if result.get("run_id") and result.get("session_id"):
            receipts[str(result["run_id"])] = {
                "status": result.get("status") or "running", "requested_agents": 1,
                "launched_agents": 1, "workflow_id": result["run_id"],
            }


def incomplete_execution_notice(receipts: dict) -> str | None:
    if not receipts:
        return ("The requested agent workflow was not run: no successful agent-launch call "
                "was recorded. Loading skills or starting one generic Deep Research job "
                "does not execute the specialist workflow.")
    unfinished = [item for item in receipts.values() if item.get("status") != "completed"]
    if not unfinished:
        return None
    rows = []
    for item in unfinished:
        rows.append(f"{item.get('workflow_id')}: {item.get('status') or 'unknown'}; "
                    f"{item.get('launched_agents') or 0}/{item.get('requested_agents') or 0} agents launched")
    return ("The agent workflow is not complete. " + "; ".join(rows) +
            ". Use the workflow status/wait control to collect verified handoffs; "
            "do not treat a launch receipt as finished research.")
