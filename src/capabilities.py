"""Capability registry — what this deployment can actually do.

Odysseus grew on one machine, so a subsystem that needed a binary, an SSH host
or an Apple-Silicon GPU could simply assume it was there. On a fresh install
those assumptions are wrong, and the failure mode was the worst available one:
the tool stayed in the model's schema, the model chose it, and the call failed at
the far end. The model then retried, apologised, or invented a workaround.

A capability declares three separable things, and the distinction matters:

* **requirements** — facts about the host (a binary on PATH, a reachable
  service, credentials on file). Probed, never configured.
* **enabled** — an operator's choice, stored in ``features.json``.
* **available** — enabled *and* every requirement met. Only available
  capabilities reach the tool schema.

So "off" and "impossible here" stay distinct: an operator who disables Cookbook
sees a switch they can flip, while one whose host has no vLLM sees why it cannot
be flipped yet. Both are better than a tool that lies.

Probes are cached briefly because the tool schema is rebuilt on every round of
every turn; a probe that shells out must not run hundreds of times a minute.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Long enough that a round of tool-schema building costs one probe, short enough
# that installing a missing binary shows up without a restart.
_PROBE_TTL_SECONDS = 30.0

_probe_cache: Dict[str, Tuple[float, bool, str]] = {}
_probe_lock = threading.Lock()


@dataclass(frozen=True)
class Requirement:
    """One host fact a capability needs, and how to check it.

    ``check`` returns ``(ok, detail)``. It must not raise and must not block for
    long — anything that could hang belongs behind its own timeout. ``hint`` is
    shown to the operator when the check fails, so it says what to *do*, not
    what went wrong.
    """

    name: str
    check: Callable[[], Tuple[bool, str]]
    hint: str = ""


@dataclass
class Capability:
    """A coherent slice of functionality that may or may not work on this host."""

    name: str
    title: str
    summary: str
    # Requirements are ANDed. An empty tuple means "works anywhere".
    requirements: Tuple[Requirement, ...] = ()
    # features.json key. Defaults to the capability name.
    feature_key: Optional[str] = None
    # Off on a fresh install unless this is True. Anything that reaches the
    # network, the host shell, or another machine defaults to off.
    default_enabled: bool = False
    # Tool names this capability owns. While it is unavailable these are kept
    # out of the schema entirely rather than failing when called.
    tools: Tuple[str, ...] = ()
    # settings.json keys this capability owns, for grouping in the admin UI.
    settings: Tuple[str, ...] = ()
    # Shown in the admin UI as the place to configure this.
    docs_url: str = ""

    @property
    def key(self) -> str:
        return self.feature_key or self.name


@dataclass
class CapabilityStatus:
    """The resolved state of one capability, as the admin UI renders it."""

    name: str
    title: str
    summary: str
    enabled: bool
    satisfied: bool
    unmet: List[Dict[str, str]] = field(default_factory=list)
    tools: List[str] = field(default_factory=list)
    settings: List[str] = field(default_factory=list)
    docs_url: str = ""

    @property
    def available(self) -> bool:
        """Enabled by the operator *and* possible on this host."""
        return self.enabled and self.satisfied

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "title": self.title,
            "summary": self.summary,
            "enabled": self.enabled,
            "satisfied": self.satisfied,
            "available": self.available,
            "unmet": self.unmet,
            "tools": self.tools,
            "settings": self.settings,
            "docs_url": self.docs_url,
        }


_REGISTRY: Dict[str, Capability] = {}


def register(capability: Capability) -> Capability:
    """Add a capability. Re-registering the same name replaces it (import
    order in tests should not depend on who got there first)."""
    _REGISTRY[capability.name] = capability
    return capability


def all_capabilities() -> Tuple[Capability, ...]:
    return tuple(_REGISTRY[name] for name in sorted(_REGISTRY))


def get(name: str) -> Optional[Capability]:
    return _REGISTRY.get(name)


def _cached_check(cache_key: str, check: Callable[[], Tuple[bool, str]]) -> Tuple[bool, str]:
    now = time.monotonic()
    with _probe_lock:
        hit = _probe_cache.get(cache_key)
        if hit and (now - hit[0]) < _PROBE_TTL_SECONDS:
            return hit[1], hit[2]
    try:
        ok, detail = check()
    except Exception as exc:
        # A broken probe must not take the process with it, and must not
        # silently report success.
        logger.debug("capability probe %s raised", cache_key, exc_info=True)
        ok, detail = False, f"probe failed: {type(exc).__name__}: {exc}"
    with _probe_lock:
        _probe_cache[cache_key] = (now, bool(ok), str(detail or ""))
    return bool(ok), str(detail or "")


def invalidate_probes() -> None:
    """Forget cached probe results — call after an install or config change."""
    with _probe_lock:
        _probe_cache.clear()


def _feature_enabled(cap: Capability) -> bool:
    try:
        from src.settings import load_features

        features = load_features()
    except Exception:
        return cap.default_enabled
    value = features.get(cap.key)
    if value is None:
        return cap.default_enabled
    return bool(value)


def status(name: str) -> Optional[CapabilityStatus]:
    cap = _REGISTRY.get(name)
    if cap is None:
        return None
    unmet: List[Dict[str, str]] = []
    for req in cap.requirements:
        ok, detail = _cached_check(f"{cap.name}:{req.name}", req.check)
        if not ok:
            unmet.append({"requirement": req.name, "detail": detail, "hint": req.hint})
    return CapabilityStatus(
        name=cap.name,
        title=cap.title,
        summary=cap.summary,
        enabled=_feature_enabled(cap),
        satisfied=not unmet,
        unmet=unmet,
        tools=list(cap.tools),
        settings=list(cap.settings),
        docs_url=cap.docs_url,
    )


def all_status() -> List[CapabilityStatus]:
    out = []
    for cap in all_capabilities():
        st = status(cap.name)
        if st is not None:
            out.append(st)
    return out


def is_available(name: str) -> bool:
    """True when this capability is enabled and its host requirements are met.

    An unknown capability is available: callers should not have to register
    a capability just to keep working, and failing closed on a typo would
    silently delete tools.
    """
    st = status(name)
    return True if st is None else st.available


def unavailable_tools() -> frozenset[str]:
    """Tool names to keep out of the model's schema on this host.

    A tool is withheld only when its capability is registered *and* not
    available. Tools belonging to no capability are always offered, so this
    never hides anything by omission.
    """
    withheld: set[str] = set()
    for st in all_status():
        if not st.available:
            withheld.update(st.tools)
    return frozenset(withheld)


def capability_for_tool(tool: str) -> Optional[Capability]:
    for cap in all_capabilities():
        if tool in cap.tools:
            return cap
    return None


# ── requirement helpers ───────────────────────────────────────────────────
# Ordinary probes, written once here so a capability declaration stays a
# declaration instead of growing its own shell-outs.


def binary_on_path(*names: str) -> Callable[[], Tuple[bool, str]]:
    """Satisfied when any of ``names`` resolves on PATH."""

    def check() -> Tuple[bool, str]:
        for name in names:
            found = shutil.which(name)
            if found:
                return True, found
        return False, f"not on PATH: {', '.join(names)}"

    return check


def executable_file(path_getter: Callable[[], str]) -> Callable[[], Tuple[bool, str]]:
    """Satisfied when the resolved path exists and is executable.

    Takes a getter rather than a path because the path is usually a setting the
    operator can change while the process runs.
    """

    def check() -> Tuple[bool, str]:
        try:
            path = str(path_getter() or "").strip()
        except Exception as exc:
            return False, f"path unresolved: {exc}"
        if not path:
            return False, "no path configured"
        expanded = os.path.expanduser(path)
        if not os.path.isfile(expanded):
            return False, f"not found: {expanded}"
        if not os.access(expanded, os.X_OK):
            return False, f"not executable: {expanded}"
        return True, expanded

    return check


def env_flag(var: str) -> Callable[[], Tuple[bool, str]]:
    """Satisfied when a deployment has explicitly opted in via the environment.

    For capabilities whose risk is a property of the *deployment* rather than a
    user preference — mounting the host Docker socket, say.
    """

    def check() -> Tuple[bool, str]:
        raw = str(os.environ.get(var, "") or "").strip().lower()
        if raw in {"1", "true", "yes", "on"}:
            return True, f"{var}={raw}"
        return False, f"{var} is not set"

    return check


def setting_present(*keys: str) -> Callable[[], Tuple[bool, str]]:
    """Satisfied when every named setting holds a non-empty value."""

    def check() -> Tuple[bool, str]:
        from src.settings import get_setting

        missing = [k for k in keys if not str(get_setting(k, "") or "").strip()]
        if missing:
            return False, f"not configured: {', '.join(missing)}"
        return True, "configured"

    return check


def all_of(*checks: Callable[[], Tuple[bool, str]]) -> Callable[[], Tuple[bool, str]]:
    def check() -> Tuple[bool, str]:
        details = []
        for inner in checks:
            ok, detail = inner()
            if not ok:
                return False, detail
            details.append(detail)
        return True, "; ".join(details)

    return check


def any_of(*checks: Callable[[], Tuple[bool, str]]) -> Callable[[], Tuple[bool, str]]:
    def check() -> Tuple[bool, str]:
        details = []
        for inner in checks:
            ok, detail = inner()
            if ok:
                return True, detail
            details.append(detail)
        return False, " / ".join(details)

    return check


def iter_tool_names() -> Iterable[str]:
    for cap in all_capabilities():
        yield from cap.tools
