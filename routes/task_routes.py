"""Backward-compat shim — canonical location is routes/task/task_routes.py."""

import sys as _sys

from routes.task import task_routes as _canonical  # noqa: F401

_sys.modules[__name__] = _canonical
