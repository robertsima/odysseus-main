"""Backward-compat shim — canonical location is routes/mcp/mcp_routes.py."""

import sys as _sys

from routes.mcp import mcp_routes as _canonical  # noqa: F401

_sys.modules[__name__] = _canonical
