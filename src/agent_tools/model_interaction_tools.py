"""model_interaction_tools.py - agent tools for talking to other models.

Owns the model-interaction tool implementations (chat_with_model, ask_teacher,
list_models) and their handler classes, registered in ``TOOL_HANDLERS``. Part
of the tool -> registry migration (#3629): the implementations were moved here
out of ``src.ai_interaction`` so dispatch flows through the registry instead of
the elif chain / dispatch_ai_tool in tool_execution.py.

Shared helpers that still live in ``src.ai_interaction`` and are used by tools
not yet migrated (``_resolve_model``, ``AI_CHAT_TIMEOUT``) are imported lazily
inside the functions to avoid an import cycle at module load.
"""
import asyncio
import logging
import re
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Model specs that mean "whatever teacher is configured". The agent has been
# seen sending 'default' — which used to be looked up as a literal model id,
# fail with "Model 'default' not found", and cost a list_models round.
_TEACHER_AUTO_ALIASES = frozenset({"", "auto", "default", "teacher", "teacher_model", "configured"})


def _keyword_terms(keyword: Optional[str]) -> List[str]:
    """Split a list_models filter into lowercase terms ('claude opus' → ['claude', 'opus'])."""
    return [t for t in re.split(r"[\s,]+", (keyword or "").strip().lower()) if t]


def _matches_terms(model_id: str, endpoint_name: str, terms: List[str]) -> bool:
    """True when every term appears in the model id or the endpoint name.

    The old filter needed the whole phrase as one substring, so 'claude opus'
    matched nothing (ids are hyphenated: `claude-opus-4-7`) and the agent had
    to call list_models a second time with no filter.
    """
    hay = f"{model_id or ''} {endpoint_name or ''}".lower()
    return all(t in hay for t in terms)


_TEACHER_SYSTEM_PROMPT = (
    "You are a senior AI mentor. A less capable model is stuck on a problem and asking for help. "
    "Provide clear, actionable guidance:\n"
    "1. Brief analysis of the problem\n"
    "2. Recommended approach (step by step)\n"
    "3. Key things to watch out for\n\n"
    "Be concise and practical. No preamble."
)


def _claude_code_hint(spec: str) -> str:
    """Point a 'claude' request at the right tool.

    Claude Code is integrated as a coding agent (a local CLI subprocess run by
    ``delegate_to_claude_code``), not as a chat endpoint, and it authenticates
    with the operator's own Claude login rather than an API key held by
    Odysseus. Asking for it through chat_with_model/list_models always fails,
    so say where it actually lives instead of just 'not found'.
    """
    if "claude" not in (spec or "").lower():
        return ""
    return (" Note: Claude Code is not a chat model here. It is the coding agent reached with "
            "delegate_to_claude_code (action=status to check install/sign-in and approved repositories, "
            "then action=run or start with a repository and prompt).")


async def chat_with_model(content: str, session_id: Optional[str] = None, owner: Optional[str] = None) -> Dict:
    """Send a message to a specific model and return its response.

    Content format:
      Line 1: model_name (or model_name@endpoint_name)
      Line 2+: the message to send
    """
    from src.ai_interaction import _resolve_model, AI_CHAT_TIMEOUT
    from src.llm_core import llm_call_async

    lines = content.strip().split("\n", 1)
    if not lines or not lines[0].strip():
        return {"error": "First line must be the model name"}

    model_spec = lines[0].strip()
    message = lines[1].strip() if len(lines) > 1 else ""
    if not message:
        return {"error": "No message provided (line 2+ is the message)"}

    try:
        url, model, headers = await asyncio.to_thread(_resolve_model, model_spec, owner=owner)
    except ValueError as e:
        return {"error": str(e) + _claude_code_hint(model_spec)}

    try:
        response = await llm_call_async(
            url, model,
            [{"role": "user", "content": message}],
            headers=headers,
            timeout=AI_CHAT_TIMEOUT,
        )
        # Truncate very long responses
        if len(response) > 10000:
            response = response[:10000] + "\n... (truncated)"
        return {"model": model, "response": response}
    except Exception as e:
        logger.error(f"chat_with_model failed: {e}")
        return {"error": f"Failed to get response from {model_spec}: {e}"}


async def ask_teacher(content: str, session_id: Optional[str] = None, owner: Optional[str] = None) -> Dict:
    """Ask a more capable model for help.

    Content format:
      Line 1: model_name (or 'auto')
      Line 2+: the problem description

    The requested model is tried first; if it cannot be resolved or its call
    fails, the configured `teacher_model` answers instead (when it is a
    different model). One failed teacher used to be the end of it — the agent
    saw "Teacher call failed (gpt-6-astra): ..." and carried on without help.
    """
    from src.ai_interaction import _resolve_model, AI_CHAT_TIMEOUT
    from src.llm_core import llm_call_async
    from src.settings import get_setting

    lines = content.strip().split("\n", 1)
    model_spec = lines[0].strip() if lines else "auto"
    problem = lines[1].strip() if len(lines) > 1 else ""

    if not problem:
        return {"error": "No problem description provided"}

    configured = str(get_setting("teacher_model", "") or "").strip()
    if model_spec.lower() in _TEACHER_AUTO_ALIASES:
        if not configured:
            return {"error": ("No teacher model configured. Specify a model name from list_models "
                              "on line 1, or set teacher_model in settings.")}
        candidates = [configured]
    else:
        candidates = [model_spec]
        if configured and configured.lower() != model_spec.lower():
            candidates.append(configured)

    failures: List[str] = []
    for spec in candidates:
        try:
            url, model, headers = await asyncio.to_thread(_resolve_model, spec, owner=owner)
        except ValueError as e:
            failures.append(f"{spec}: {e}{_claude_code_hint(spec)}")
            continue
        try:
            response = await llm_call_async(
                url, model,
                [
                    {"role": "system", "content": _TEACHER_SYSTEM_PROMPT},
                    {"role": "user", "content": f"Problem:\n{problem}"},
                ],
                headers=headers,
                timeout=AI_CHAT_TIMEOUT,
            )
        except Exception as e:
            logger.error(f"ask_teacher failed ({spec}): {e}")
            failures.append(f"{spec}: {e}")
            continue
        if len(response) > 8000:
            response = response[:8000] + "\n... (truncated)"
        result = {"model": model, "response": response, "teacher": True}
        if failures:
            result["note"] = (
                f"Requested teacher {candidates[0]!r} was unavailable ({failures[-1]}); "
                f"the configured teacher {model!r} answered instead."
            )
        return result

    tried = "; ".join(failures) if failures else "no candidate model"
    if configured:
        hint = " Call list_models for exact model ids and retry with one of them."
    else:
        hint = (" Call list_models for exact model ids and retry with one of them, "
                "or set teacher_model in settings and pass 'auto'.")
    return {"error": f"Teacher call failed — tried {tried}.{hint}"}


async def list_models(content: str, session_id: Optional[str] = None, owner: Optional[str] = None) -> Dict:
    """List all available models across configured endpoints.

    Content = optional filter keyword.
    """
    import json
    import httpx
    from src.database import SessionLocal, ModelEndpoint
    from src.llm_core import _detect_provider, ANTHROPIC_MODELS
    from src.auth_helpers import owner_filter
    from src.endpoint_resolver import resolve_endpoint_runtime, build_headers, build_models_url

    keyword = content.strip().lower() if content.strip() else None
    terms = _keyword_terms(keyword)

    db = SessionLocal()
    try:
        query = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)
        if owner:
            query = owner_filter(query, ModelEndpoint, owner)
        endpoints = query.all()
        if not endpoints:
            return {"results": "No enabled model endpoints configured."}

        result_lines = []
        total_models = 0

        for ep in endpoints:
            try:
                base, api_key = resolve_endpoint_runtime(ep, owner=owner)
            except Exception:
                continue
            provider = _detect_provider(base)
            headers = build_headers(api_key, base)

            model_ids = []
            if provider == "anthropic":
                model_ids = list(ANTHROPIC_MODELS)
            else:
                try:
                    models_url = build_models_url(base)
                    if models_url:
                        r = httpx.get(models_url, headers=headers, timeout=5)
                        r.raise_for_status()
                        data = r.json()
                        model_ids = [m.get("id") for m in (data.get("data") or []) if m.get("id")]
                        if not model_ids:
                            model_ids = [
                                m.get("name") or m.get("model")
                                for m in (data.get("models") or [])
                                if m.get("name") or m.get("model")
                            ]
                    else:
                        model_ids = json.loads(ep.cached_models or "[]")
                except Exception:
                    model_ids = ["(endpoint offline)"]

            if terms:
                model_ids = [m for m in model_ids if _matches_terms(m, ep.name or "", terms)]

            if model_ids:
                result_lines.append(f"\n**{ep.name or base}** ({provider}):")
                for mid in model_ids:
                    result_lines.append(f"  - `{mid}`")
                    total_models += 1

        if not result_lines:
            return {"results": "No models found" + (f" matching '{keyword}'" if keyword else "") + "."
                    + (" Try a shorter keyword (any word of the model id or endpoint name), "
                       "or no filter to see everything." if keyword else "")
                    + _claude_code_hint(keyword or "")}

        header = f"Available models ({total_models} total):"
        return {"results": header + "\n".join(result_lines)}
    except Exception as e:
        logger.error(f"list_models failed: {e}")
        return {"error": str(e)}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Handler classes registered in TOOL_HANDLERS
# ---------------------------------------------------------------------------

class ChatWithModelTool:
    async def execute(self, content: str, ctx: dict) -> Dict:
        return await chat_with_model(content, ctx.get("session_id"), owner=ctx.get("owner"))


class AskTeacherTool:
    async def execute(self, content: str, ctx: dict) -> Dict:
        return await ask_teacher(content, ctx.get("session_id"), owner=ctx.get("owner"))


class ListModelsTool:
    async def execute(self, content: str, ctx: dict) -> Dict:
        return await list_models(content, ctx.get("session_id"), owner=ctx.get("owner"))
