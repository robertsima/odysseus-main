"""
context_profiles.py

Per-endpoint/model tuning for how much context an agent turn spends.

The knobs that decide whether an agent run feels sharp or flat — how much of a
tool result stays inline, how deep a trim cuts, whether a reasoning model gets
its own thinking back on the next round — were spread across env vars, module
constants and one global setting. None of them could differ between a 400k
hosted model and an 8k local one, even though the right answer is close to
opposite for the two:

- On a long-context hosted model, tokens are cheap and round-trips are not. A
  recall costs a whole extra request; keeping 2k tokens of a log inline costs
  almost nothing. Bias toward keeping things.
- On a small local model, one 10k-character log is a third of the window. Bias
  hard toward offloading, and trim deeper so the next round does not re-trim.

So: a profile is resolved per (endpoint, model), and the resolution is explicit
about where each value came from, because "why is it doing that" is the whole
reason this exists.
"""

import logging
import os
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

SETTING_KEY = "context_profiles"
GLOBAL_KEY = "*"

# Every tunable, with the range it is clamped to. A value outside the range is
# not an error worth failing a request over — it is clamped and logged, so a
# fat-fingered 999999 degrades to "the maximum" rather than breaking the loop.
KNOBS: Dict[str, Dict[str, Any]] = {
    "tool_output_inline_limit": {
        "type": int, "min": 500, "max": 200_000, "default": 4000,
        "label": "Tool output kept inline",
        "unit": "characters",
        "help": (
            "A tool result over this size is stored and replaced by a head/tail "
            "excerpt the agent can search with recall_tool_output. Note bash, "
            "python and web tools are already capped at 10,000 characters "
            "upstream, so a value above ~10,500 only affects MCP, email and "
            "document results."
        ),
    },
    "tool_output_head_chars": {
        "type": int, "min": 200, "max": 100_000, "default": 2000,
        "label": "Excerpt head", "unit": "characters",
        "help": "How much of the start of an offloaded result stays in view.",
    },
    "tool_output_tail_chars": {
        "type": int, "min": 200, "max": 100_000, "default": 800,
        "label": "Excerpt tail", "unit": "characters",
        "help": (
            "How much of the end stays in view. Keep at least ~300: exit codes "
            "and error lines live at the end of a shell result."
        ),
    },
    "input_token_budget": {
        "type": int, "min": 0, "max": 1_000_000, "default": 0,
        "label": "Input token budget", "unit": "tokens (0 = auto)",
        "help": (
            "Soft cap on prompt size before older turns are trimmed. 0 scales "
            "to 85% of the model's window, capped at 200k."
        ),
    },
    "trim_target_ratio": {
        "type": float, "min": 0.4, "max": 0.95, "default": 0.8,
        "label": "Trim depth", "unit": "fraction of budget",
        "help": (
            "When a trim is unavoidable, cut to this fraction of the budget. "
            "Trimming rewrites the prompt prefix and costs a full re-prefill, "
            "so cutting deeper buys several cheap rounds."
        ),
    },
    "reasoning_replay": {
        "type": bool, "default": True,
        "label": "Reasoning continuity",
        "help": (
            "Hand a reasoning model its own encrypted thinking back on the next "
            "round so it keeps its plan across tool calls instead of re-deriving "
            "it. Responses API models only; ignored elsewhere."
        ),
    },
    "reasoning_replay_rounds": {
        "type": int, "min": 1, "max": 8, "default": 3,
        "label": "Reasoning rounds kept", "unit": "rounds",
        "help": "How many recent rounds keep their reasoning. Opaque payload, so bounded.",
    },
}

PRESETS: Dict[str, Dict[str, Any]] = {
    "long_context": {
        "label": "Long context (hosted)",
        "hint": "200k+ windows — GPT-5.x, Claude, Gemini. Favours fewer round-trips over fewer tokens.",
        "values": {
            "tool_output_inline_limit": 8000,
            "tool_output_head_chars": 4000,
            "tool_output_tail_chars": 1500,
            "input_token_budget": 0,
            "trim_target_ratio": 0.8,
            "reasoning_replay": True,
            "reasoning_replay_rounds": 3,
        },
    },
    "balanced": {
        "label": "Balanced",
        "hint": "32k–200k windows. The shipped default.",
        "values": {
            "tool_output_inline_limit": 4000,
            "tool_output_head_chars": 2000,
            "tool_output_tail_chars": 800,
            "input_token_budget": 0,
            "trim_target_ratio": 0.8,
            "reasoning_replay": True,
            "reasoning_replay_rounds": 3,
        },
    },
    "compact": {
        "label": "Compact (small local)",
        "hint": "Under 32k. Offloads aggressively and trims deep so a run survives the window.",
        "values": {
            "tool_output_inline_limit": 2000,
            "tool_output_head_chars": 1200,
            "tool_output_tail_chars": 400,
            "input_token_budget": 0,
            "trim_target_ratio": 0.7,
            "reasoning_replay": True,
            "reasoning_replay_rounds": 2,
        },
    },
}

# Which env var, if any, backs each knob. Env stays honoured for deployments
# that set it before this tab existed — but an explicit choice in the UI wins,
# or the tab would appear to do nothing on those installs.
_ENV_KEYS = {
    "tool_output_inline_limit": "ODYSSEUS_TOOL_OUTPUT_INLINE_LIMIT",
    "tool_output_head_chars": "ODYSSEUS_TOOL_OUTPUT_HEAD_CHARS",
    "tool_output_tail_chars": "ODYSSEUS_TOOL_OUTPUT_TAIL_CHARS",
}

_LONG_CONTEXT_FLOOR = 200_000
_COMPACT_CEILING = 32_000


def preset_for_window(context_length: int) -> str:
    """The recommended preset for a model with this window."""
    if context_length and context_length >= _LONG_CONTEXT_FLOOR:
        return "long_context"
    if context_length and context_length < _COMPACT_CEILING:
        return "compact"
    return "balanced"


def profile_key(endpoint_url: str = "", model: str = "") -> str:
    """The settings key one endpoint/model pair is stored under.

    Neither set is not "some nameless endpoint" — it is the settings tab's
    "All endpoints / All models", which has to resolve to the global profile.
    Keyed literally it would land on "|", a key `resolve` consults only for a
    caller that itself has no endpoint, so a profile saved there would apply to
    nothing and look like the tab had done nothing.
    """
    endpoint_url = (endpoint_url or "").strip()
    model = (model or "").strip()
    if not endpoint_url and not model:
        return GLOBAL_KEY
    return f"{endpoint_url}|{model}"


# What "All endpoints / All models" was keyed as before it folded into
# GLOBAL_KEY. Normalised on read so a settings.json written by hand, or by an
# older build, keeps working.
_LEGACY_GLOBAL_KEY = "|"


def _clamp(name: str, value: Any) -> Optional[Any]:
    spec = KNOBS.get(name)
    if not spec:
        return None
    try:
        if spec["type"] is bool:
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        coerced = spec["type"](value)
    except (TypeError, ValueError):
        return None
    lo, hi = spec.get("min"), spec.get("max")
    if lo is not None and coerced < lo:
        coerced = lo
    if hi is not None and coerced > hi:
        coerced = hi
    return coerced


def sanitize(stored: Any) -> Dict[str, Dict[str, Any]]:
    """Validate the whole `context_profiles` setting coming from the API.

    Unknown keys, unknown presets and out-of-range numbers are dropped or
    clamped rather than rejected: this is a preferences blob, and one bad field
    must not cost the user the rest of their settings save.
    """
    out: Dict[str, Dict[str, Any]] = {}
    if not isinstance(stored, dict):
        return out
    for key, entry in stored.items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            continue
        preset = str(entry.get("preset") or "").strip()
        if preset not in PRESETS and preset != "custom":
            continue
        if key == _LEGACY_GLOBAL_KEY:
            key = GLOBAL_KEY
        clean: Dict[str, Any] = {"preset": preset}
        if preset == "custom":
            values = entry.get("values")
            clean_values = {}
            if isinstance(values, dict):
                for name, value in values.items():
                    coerced = _clamp(name, value)
                    if coerced is not None:
                        clean_values[name] = coerced
            if not clean_values:
                continue
            clean["values"] = clean_values
        out[key[:400]] = clean
    return out


def _stored_profiles() -> Dict[str, Any]:
    try:
        from src.settings import get_setting

        stored = get_setting(SETTING_KEY, {}) or {}
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("context profile lookup failed: %s", exc)
        return {}
    if not isinstance(stored, dict):
        return {}
    if _LEGACY_GLOBAL_KEY in stored and GLOBAL_KEY not in stored:
        stored = dict(stored)
        stored[GLOBAL_KEY] = stored.pop(_LEGACY_GLOBAL_KEY)
    return stored


def _entry_values(entry: Dict[str, Any]) -> Dict[str, Any]:
    preset = entry.get("preset")
    if preset == "custom":
        return dict(entry.get("values") or {})
    return dict(PRESETS.get(preset, {}).get("values") or {})


def resolve(
    endpoint_url: str = "",
    model: str = "",
    context_length: int = 0,
) -> Dict[str, Any]:
    """The effective profile for this endpoint/model.

    Precedence, most specific first:
      1. a profile saved for exactly this endpoint + model
      2. a profile saved for this endpoint, any model
      3. the global profile
      4. the matching env var, where one exists
      5. the preset recommended for the model's context window
    """
    stored = _stored_profiles()
    source = "auto"
    chosen: Dict[str, Any] = {}

    # Two tiers collapse onto one key when the more specific half is unset (no
    # model named, or neither). Search each key once, and let the most GENERAL
    # tier that maps to it name the source, so the settings tab reports "the
    # global profile" rather than claiming a model-specific one won.
    tiers = (
        (profile_key(endpoint_url, model), "model"),
        (profile_key(endpoint_url, ""), "endpoint"),
        (GLOBAL_KEY, "global"),
    )
    labels = {key: label for key, label in tiers}
    ordered = []
    for key, _label in tiers:
        if key not in ordered:
            ordered.append(key)

    for key in ordered:
        entry = stored.get(key)
        if isinstance(entry, dict):
            values = _entry_values(entry)
            if values:
                chosen, source = values, labels[key]
                break

    auto_preset = preset_for_window(context_length)
    resolved = dict(PRESETS[auto_preset]["values"])

    # Env fills in only where no explicit profile spoke.
    for name, env_key in _ENV_KEYS.items():
        raw = os.getenv(env_key)
        if raw:
            coerced = _clamp(name, raw)
            if coerced is not None:
                resolved[name] = coerced

    for name, value in chosen.items():
        coerced = _clamp(name, value)
        if coerced is not None:
            resolved[name] = coerced

    resolved["_source"] = source
    resolved["_auto_preset"] = auto_preset
    return resolved


def describe(endpoint_url: str = "", model: str = "", context_length: int = 0) -> Dict[str, Any]:
    """Everything the settings tab needs to render one endpoint/model."""
    stored = _stored_profiles()
    key = profile_key(endpoint_url, model)
    entry = stored.get(key) or {}
    return {
        "key": key,
        "endpoint_url": endpoint_url,
        "model": model,
        "context_length": context_length,
        "recommended": preset_for_window(context_length),
        "selected": entry.get("preset") or "",
        "custom_values": entry.get("values") or {},
        "effective": resolve(endpoint_url, model, context_length),
        "presets": {
            name: {"label": p["label"], "hint": p["hint"], "values": p["values"]}
            for name, p in PRESETS.items()
        },
        "knobs": {
            name: {k: v for k, v in spec.items() if k != "type"}
            for name, spec in KNOBS.items()
        },
    }


def value_for(
    name: str,
    endpoint_url: str = "",
    model: str = "",
    context_length: int = 0,
    profile: Optional[Dict[str, Any]] = None,
) -> Any:
    """One knob, from an already-resolved profile or by resolving now."""
    source = profile if isinstance(profile, dict) else resolve(endpoint_url, model, context_length)
    if name in source:
        return source[name]
    return KNOBS.get(name, {}).get("default")
