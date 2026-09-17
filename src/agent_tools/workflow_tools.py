"""Agent-facing controls for scoped research workflows."""
from src import agent_workflows
from src.tool_utils import _parse_tool_args


class OrchestrateAgentsTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        try:
            args = _parse_tool_args(content)
            if not isinstance(args, dict):
                raise ValueError("JSON object required")
            action = str(args.get("action") or "status").strip().lower()
            common = {"session_id": ctx.get("session_id"), "owner": ctx.get("owner")}
            if action == "start":
                result = await agent_workflows.start(
                    **common, args=args, delegation_authorized=ctx.get("delegation_authorized"),
                    allow_private=bool(ctx.get("allow_private")),
                )
                return {**result, "action": "start", "response": (
                    f"Workflow {result['workflow_id']}: {result['launched_agents']} of "
                    f"{result['requested_agents']} agents launched; status {result['status']}. "
                    "Use wait/status to collect actual results; queued or running is not completed."
                )}
            if action not in {"status", "wait", "cancel"}:
                raise ValueError("action must be start, status, wait, or cancel")
            result = await agent_workflows.inspect(
                **common, workflow_id=str(args.get("workflow_id") or ""), action=action,
                wait_seconds=args.get("wait_seconds", 30),
            )
            return {**result, "action": action, "response": agent_workflows.render_result(result)}
        except (ValueError, TypeError, LookupError, RuntimeError) as exc:
            return {"error": f"orchestrate_agents: {exc}", "launched_agents": 0, "exit_code": 1}
