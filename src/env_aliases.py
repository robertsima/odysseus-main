"""``AGAMEMNON_*`` environment names, with ``ODYSSEUS_*`` kept as aliases.

The code reads ``ODYSSEUS_*`` names, which existing deployments, compose files
and scripts set. Any ``AGAMEMNON_<NAME>`` in the environment is copied to
``ODYSSEUS_<NAME>`` before configuration is read, so either spelling works.
When both are set to different values the Agamemnon name wins, and the
conflict is reported once so a stale value is not silently in force.

Docker Compose forwards only the names its ``environment:`` block lists, and
the stock files list the ``ODYSSEUS_*`` names. An ``AGAMEMNON_*`` name in
``.env`` therefore reaches a container only if compose forwards it.
"""
from __future__ import annotations

import logging
import os
from typing import List, MutableMapping, Optional

NEW_PREFIX = "AGAMEMNON_"
LEGACY_PREFIX = "ODYSSEUS_"

logger = logging.getLogger(__name__)


def apply(environ: Optional[MutableMapping[str, str]] = None) -> List[str]:
    """Copy each ``AGAMEMNON_*`` value to its ``ODYSSEUS_*`` name.

    Returns the legacy names whose differing value was overridden.
    """
    env = os.environ if environ is None else environ
    overridden: List[str] = []
    for name in [key for key in env if key.startswith(NEW_PREFIX)]:
        suffix = name[len(NEW_PREFIX):]
        if not suffix:
            continue
        legacy = LEGACY_PREFIX + suffix
        value = env[name]
        if legacy in env and env[legacy] != value:
            overridden.append(legacy)
        env[legacy] = value
    for legacy in overridden:
        logger.warning("%s and %s are both set; using %s", NEW_PREFIX + legacy[len(LEGACY_PREFIX):],
                       legacy, NEW_PREFIX + legacy[len(LEGACY_PREFIX):])
    return overridden
