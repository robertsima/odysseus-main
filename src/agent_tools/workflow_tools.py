"""Agent-facing controls for scoped research workflows."""
from src import agent_workflows
from src.tool_utils import _parse_tool_args


class OrchestrateAgentsTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        try:
            args = _parse_tool_args(content)
            if not isinstance(args, dict):
                raise ValueError("JSON object required")
            requested_action = str(args.get("action") or "").strip().lower()
            has_start_payload = bool(
                args.get("task") or args.get("specialists") or args.get("synthesis")
            )
            # Native model calls can omit a required enum or accidentally retain
            # a follow-up action while providing a complete launch payload.  A
            # start-shaped request is more reliable evidence than the missing or
            # contradictory verb; treating it as status produces the misleading
            # "Workflow not found" failure before any worker can launch.
            if has_start_payload and (
                not requested_action
                or (requested_action in {"status", "wait", "cancel"} and not args.get("workflow_id"))
            ):
                action = "start"
            else:
                action = requested_action or "status"
            common = {"session_id": ctx.get("session_id"), "owner": ctx.get("owner")}
            if action == "start":
                result = await agent_workflows.start(
                    **common, args=args, delegation_authorized=ctx.get("delegation_authorized"),
                    allow_private=bool(ctx.get("allow_private")),
                )
                blocked = result.get("preflight_blocked") or []
                return {**result, "action": "start", "response": (
                    f"Workflow {result['workflow_id']}: {result['launched_agents']} of "
                    f"{result['requested_agents']} child runs launched; status {result['status']}. "
                    + (f"Preflight blockers on {', '.join(blocked)} — see preflight for the reason; "
                       "do not report their branches as researched. " if blocked else "")
                    + "Use wait/status to collect actual results; queued or running is not completed."
                )}
            if action == "resume":
                result = await agent_workflows.resume(**common, args=args)
                return {**result, "action": "resume", "response": (
                    f"Workflow {result['workflow_id']} resumes {args.get('workflow_id') or 'the latest workflow'}: "
                    f"reusing {len(result['record'].get('reused_handoffs') or [])} completed handoff(s), "
                    f"{result['launched_agents']} child run(s) launched. Use wait/status on the new ID."
                )}
            if action not in {"status", "wait", "cancel"}:
                raise ValueError("action must be start, status, wait, cancel, or resume")
            workflow_id = agent_workflows.resolve_workflow_id(
                args.get("workflow_id"), **common,
            )
            result = await agent_workflows.inspect(
                **common, workflow_id=workflow_id, action=action,
                wait_seconds=args.get("wait_seconds", 30),
            )
            return {**result, "action": action, "response": agent_workflows.render_result(result)}
        except (ValueError, TypeError, LookupError, RuntimeError) as exc:
            return {"error": f"orchestrate_agents: {exc}", "launched_agents": 0, "exit_code": 1}
