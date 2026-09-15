"""The contract a code-delegation provider satisfies.

Delegation — "hand this coding task to an agent that has a real checkout" —
arrived as one function that shelled out to one vendor's CLI. The work it does
is not vendor-specific: take a prompt plus a repository, run an agent there,
report what changed. Only the transport is. This module names that split so a
second transport can be added without another tool in the model's schema.

Why the CLI exists at all is worth stating, because it constrains every future
provider: it rides a *subscription*, not metered per-token API billing. A
provider that needs an API key is therefore not a drop-in alternative here, and
must not be reached for as a fallback when a subscription-backed one is absent.

Two rules keep the abstraction honest:

* ``is_available`` is a cheap, synchronous probe. It is called while the tool
  schema is being built — once per model round — so it may look at the
  filesystem and at settings, but it must not spawn a process or touch the
  network.
* ``delegate`` returns the *same* result shape the existing
  ``delegate_to_claude_code`` tool returns today, so the tool layer and the
  model's expectations do not change when the provider does.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Mapping, Optional, Tuple

# The result shape is a plain dict because that is what the tool layer already
# passes back to the model. The parts callers rely on:
#
#   exit_code: int    0 on success, non-zero on failure (always present)
#   error:     str    present only on failure
#
# Everything else is provider detail the model is allowed to read: the task id
# for a background run, the repository, changed files, a transcript summary.
DelegationResult = dict


class DelegationProvider(ABC):
    """One way of running a delegated coding task.

    Subclasses are cheap to construct — the registry builds one per provider at
    import time — so all real work belongs in ``is_available``/``delegate``,
    never in ``__init__``.
    """

    #: Stable identifier. Matches a value of the ``delegation_provider`` setting.
    id: str = ""
    #: Short human label for the admin UI.
    title: str = ""

    @abstractmethod
    def is_available(self) -> Tuple[bool, str]:
        """``(ok, detail)`` — whether this provider can run a task right now.

        ``detail`` is shown to an operator, so on failure it says what to *do*
        ("install X and set its path in Settings › Agents"), not merely what is
        missing. Must not raise; a probe that cannot decide reports ``False``.
        """

    @abstractmethod
    async def delegate(
        self,
        request: Mapping[str, Any],
        ctx: Optional[Mapping[str, Any]] = None,
    ) -> DelegationResult:
        """Run (or start, poll, cancel) a delegated task.

        ``request`` is the tool's own argument object — ``action``, ``prompt``,
        ``repository`` and friends — passed through unchanged so a provider can
        support the whole action set rather than just "run". ``ctx`` carries the
        calling agent's ``owner``/``session_id`` for attribution.

        Failures are *returned*, not raised: the model can act on
        ``{"error": ..., "exit_code": 1}`` but an exception only ends its turn.
        """

    def unavailable_result(self, detail: str = "") -> DelegationResult:
        """The failure a provider returns when asked to work while unusable.

        Reached only when something bypassed capability gating (a direct API
        call, a stale schema), so it names the provider — otherwise the model
        sees "not available" with no way to tell which of several providers
        meant it.
        """
        if not detail:
            detail = self.is_available()[1]
        return {
            "error": f"{self.title or self.id}: not available — {detail}",
            "provider": self.id,
            "exit_code": 1,
        }
