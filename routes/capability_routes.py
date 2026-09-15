"""Admin API for the capability registry and the settings schema.

Two things an operator of a redistributed build needs that a single-machine
deployment never did:

* **What can this install actually do, and why not?** The capability list
  separates "you turned this off" from "this host cannot do it yet", and names
  the missing requirement plus what to do about it. Without that, a disabled
  feature and an impossible one look identical.
* **Every user-facing option, in one place.** The settings page used to be
  hand-maintained alongside ``DEFAULT_SETTINGS``, which is why 17 of 83 keys had
  no control at all. This serves the declared schema instead, so a new setting
  gets a control by virtue of being declared.

Both are admin-only. Capability state names host paths and missing binaries, and
the settings schema spans every user's defaults — neither is a non-admin's
business. Writes go through the same validation the schema declares, so a
hand-rolled request cannot store a value the UI would have refused.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request

from src.auth_helpers import require_user

logger = logging.getLogger(__name__)


def _require_admin(request: Request) -> str:
    """Admin, or the single-user deployment where there is nobody else."""
    user = require_user(request)
    from src.tool_security import owner_is_admin_or_single_user

    if not owner_is_admin_or_single_user(user):
        raise HTTPException(403, "Only an admin can change core configuration.")
    return user or ""


def _coerce(spec, raw: Any) -> Any:
    """Coerce an incoming value to the declared type, rejecting what will not fit.

    The UI sends strings for most controls. Storing "false" where a bool belongs
    is how a disabled feature silently reads as enabled, so this is strict about
    booleans and numbers and explicit about choices.
    """
    from src import settings_schema

    kind = spec.type
    if kind == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}
    if kind == "int":
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError):
            raise HTTPException(400, f"{spec.key} must be a whole number")
    if kind == "float":
        try:
            return float(str(raw).strip())
        except (TypeError, ValueError):
            raise HTTPException(400, f"{spec.key} must be a number")
    if kind == "choice":
        value = str(raw).strip()
        if spec.choices and value not in spec.choices:
            raise HTTPException(400, f"{spec.key} must be one of: {', '.join(spec.choices)}")
        return value
    if kind == "list":
        if isinstance(raw, (list, tuple)):
            return [str(v) for v in raw]
        # A textarea of one-per-line entries is the usual shape from the UI.
        return [line.strip() for line in str(raw).splitlines() if line.strip()]
    if kind == "json":
        if isinstance(raw, (dict, list)):
            return raw
        import json

        try:
            return json.loads(str(raw) or "null")
        except json.JSONDecodeError as exc:
            raise HTTPException(400, f"{spec.key} must be valid JSON: {exc}")
    # string / text / path / secret
    return str(raw)


def setup_capability_routes() -> APIRouter:
    router = APIRouter(tags=["configuration"])

    @router.get("/api/capabilities")
    async def list_capabilities(request: Request) -> Dict[str, Any]:
        """Every capability, whether it is on, and what it is still missing."""
        _require_admin(request)
        # Import for the registration side effect: the declarations live in
        # capabilities_builtin, and nothing else guarantees it has been imported
        # by the time an operator opens the settings page.
        import src.capabilities_builtin  # noqa: F401
        from src import capabilities

        return {"capabilities": [st.as_dict() for st in capabilities.all_status()]}

    @router.post("/api/capabilities/{name}")
    async def set_capability(request: Request, name: str) -> Dict[str, Any]:
        """Enable or disable one capability.

        Enabling something the host cannot satisfy is allowed and deliberate: an
        operator may switch it on before installing the dependency, and the
        status then shows exactly what is still missing. It stays out of the tool
        schema until the requirement is met, so nothing is advertised early.
        """
        _require_admin(request)
        import src.capabilities_builtin  # noqa: F401
        from src import capabilities
        from src.settings import load_features, save_features

        cap = capabilities.get(name)
        if cap is None:
            raise HTTPException(404, f"No capability named {name!r}")
        body = await request.json()
        enabled = bool((body or {}).get("enabled"))
        features = dict(load_features())
        features[cap.key] = enabled
        save_features(features)
        # A newly-installed dependency should show up immediately rather than
        # after the probe TTL.
        capabilities.invalidate_probes()
        st = capabilities.status(name)
        logger.info("capability %s set enabled=%s by admin", name, enabled)
        return {"capability": st.as_dict() if st else None}

    @router.post("/api/capabilities/recheck")
    async def recheck(request: Request) -> Dict[str, Any]:
        """Re-probe requirements now — after installing a binary, say."""
        _require_admin(request)
        import src.capabilities_builtin  # noqa: F401
        from src import capabilities

        capabilities.invalidate_probes()
        return {"capabilities": [st.as_dict() for st in capabilities.all_status()]}

    @router.get("/api/settings/schema")
    async def settings_schema_payload(request: Request) -> Dict[str, Any]:
        """The declared settings, grouped, with current values.

        Serves the same payload to admins and non-admins, minus what a
        non-admin may not change — so the page never renders a control whose
        save will be refused.
        """
        user = require_user(request)
        from src.tool_security import owner_is_admin_or_single_user
        from src import settings_schema

        is_admin = owner_is_admin_or_single_user(user)
        return {
            "is_admin": is_admin,
            "groups": settings_schema.ui_payload(owner=user or "", is_admin=is_admin),
        }

    @router.post("/api/settings/schema")
    async def save_settings_payload(request: Request) -> Dict[str, Any]:
        """Write declared settings, validating each against its schema entry.

        Unknown keys are rejected rather than stored: a typo that silently
        persists is a setting that looks saved and does nothing. Values pinned by
        the environment are skipped and reported, because accepting them would
        tell the operator a change took effect when the deployment overrides it.
        """
        user = _require_admin(request)
        from src import settings_schema
        from src.settings import load_settings, save_settings

        body = await request.json()
        incoming = (body or {}).get("settings")
        if not isinstance(incoming, dict):
            raise HTTPException(400, "settings must be an object of key -> value")

        unknown: List[str] = []
        locked: List[str] = []
        updates: Dict[str, Any] = {}
        for key, raw in incoming.items():
            spec = settings_schema.get_spec(key)
            if spec is None:
                unknown.append(key)
                continue
            if settings_schema.env_locked(spec):
                locked.append(key)
                continue
            updates[key] = _coerce(spec, raw)

        if unknown:
            raise HTTPException(400, f"Unknown setting(s): {', '.join(sorted(unknown))}")

        if updates:
            current = dict(load_settings())
            current.update(updates)
            save_settings(current)
            # A changed path or credential can change what a capability can do.
            from src import capabilities

            capabilities.invalidate_probes()
            logger.info("admin %s updated settings: %s", user or "(single-user)", ", ".join(sorted(updates)))

        return {"saved": sorted(updates), "locked_by_environment": sorted(locked)}

    return router
