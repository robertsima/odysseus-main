"""Consent enforcement.

Every rule here is applied server-side. A client argument can narrow access
(ask for fewer results, omit notes) but can never widen it: ``include_notes``
is combined with the server setting using AND, and an explicit request for
something the policy forbids raises :class:`PolicyError` instead of silently
returning less.
"""

from __future__ import annotations

import ipaddress
import socket
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import PrivacyPolicySettings

__all__ = ["PolicyEngine", "PolicyError", "install_network_guard"]


class PolicyError(Exception):
    """A request was refused by server-side consent policy.

    The message names the setting an operator would have to change, so the
    refusal is actionable without exposing any data.
    """


class PolicyEngine:
    """Applies :class:`PrivacyPolicySettings` to individual requests."""

    def __init__(self, settings: PrivacyPolicySettings) -> None:
        self.settings = settings

    # ---- capability gates --------------------------------------------------

    def require_aggregate_summaries(self) -> None:
        if not self.settings.expose_aggregate_summaries:
            raise PolicyError(
                "Aggregate summaries are disabled by server policy "
                "(privacy.expose_aggregate_summaries = false)."
            )

    def require_raw_entries(self) -> None:
        if not self.settings.expose_raw_entries:
            raise PolicyError(
                "Raw mood entries are not exposed by this server. Aggregate summaries remain "
                "available via summarize_period. To allow raw entries an operator must set "
                "privacy.expose_raw_entries = true in the server configuration."
            )

    def resolve_include_notes(self, requested: bool) -> bool:
        """Effective note visibility = client request AND server permission."""
        if not requested:
            return False
        if not self.settings.expose_notes:
            raise PolicyError(
                "Journal notes are not exposed by this server "
                "(privacy.expose_notes = false). The request was refused rather than "
                "silently returning entries without notes."
            )
        return True

    def resolve_note_themes(self, requested: bool) -> bool:
        if not requested:
            return False
        if not (self.settings.include_note_themes and self.settings.expose_notes):
            raise PolicyError(
                "Note-derived themes are disabled by server policy "
                "(privacy.include_note_themes / privacy.expose_notes)."
            )
        return True

    def resolve_import_notes(self, requested: bool) -> bool:
        """Notes are only read off disk when client *and* server agree."""
        if not requested:
            return False
        if not self.settings.import_notes:
            raise PolicyError(
                "Importing journal notes is disabled by server policy "
                "(privacy.import_notes = false). Re-run without include_notes to import "
                "everything except the note column."
            )
        return True

    def require_emotion_labels(self) -> None:
        if not self.settings.expose_emotion_labels:
            raise PolicyError(
                "Emotion labels are not exposed by this server "
                "(privacy.expose_emotion_labels = false)."
            )

    def require_cross_domain_correlation(self) -> None:
        """Guard for any future join against calendar/sleep/email/location."""
        if not self.settings.allow_cross_domain_correlation:
            raise PolicyError(
                "Correlating mood data with other domains requires a separate explicit opt-in "
                "(privacy.allow_cross_domain_correlation = false)."
            )

    def require_writeback(self) -> None:
        if not self.settings.allow_writeback_to_source:
            raise PolicyError("Writeback to any external source is disabled and unimplemented.")

    # ---- quantitative limits ----------------------------------------------

    def clamp_limit(self, requested: int | None, *, default: int, hard_max: int) -> int:
        """Bound a result limit by both the tool's cap and the policy cap."""
        ceiling = min(hard_max, self.settings.maximum_entry_result_limit)
        if requested is None:
            return min(default, ceiling)
        try:
            value = int(requested)
        except (TypeError, ValueError):
            raise PolicyError("limit must be an integer.") from None
        if value < 1:
            raise PolicyError("limit must be at least 1.")
        return min(value, ceiling)

    def validate_range(
        self, start: datetime, end: datetime, *, confirmed: bool = False
    ) -> tuple[datetime, datetime]:
        """Require a bounded, forward range within the confirmation window.

        Open-ended queries are refused so a client cannot discover the total
        historical extent of the data in one call.
        """
        if start.tzinfo is None or end.tzinfo is None:
            raise PolicyError("start and end must include a timezone offset.")
        if end <= start:
            raise PolicyError("end must be after start.")
        span_days = (end - start) / timedelta(days=1)
        allowed = self.settings.max_query_days_without_confirmation
        if span_days > allowed and not confirmed:
            raise PolicyError(
                f"Requested range spans {span_days:.0f} days, above the {allowed}-day limit. "
                "Re-send with confirm_extended_range = true to acknowledge the wider query."
            )
        return start, end

    def clamp_lookback_days(self, days: int, *, hard_max: int, confirmed: bool) -> int:
        if days < 1:
            raise PolicyError("lookback_days must be at least 1.")
        days = min(int(days), hard_max)
        allowed = self.settings.max_query_days_without_confirmation
        if days > allowed and not confirmed:
            raise PolicyError(
                f"lookback_days of {days} exceeds the {allowed}-day limit. "
                "Re-send with confirm_extended_range = true to acknowledge the wider query."
            )
        return days

    # ---- introspection -----------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """The active policy, safe to return to a client and to persist."""
        return self.settings.model_dump()


class _NetworkBlocked(OSError):
    """Raised in place of an outbound connection when the policy forbids one."""


def install_network_guard(settings: PrivacyPolicySettings) -> bool:
    """Make ``allow_external_network_calls = false`` mechanically true.

    The adapter has no network code, but a future dependency or a mistaken
    import should not be able to quietly reach the internet with a database of
    journal entries in memory. Loopback and Unix sockets stay available so
    local tooling still works.

    Returns True when the guard was installed.
    """
    if settings.allow_external_network_calls:
        return False
    if getattr(socket.socket, "_lotus_guarded", False):
        return True

    original_connect = socket.socket.connect

    def _is_local(address: Any) -> bool:
        if not isinstance(address, tuple) or not address:
            return True  # AF_UNIX and friends
        host = address[0]
        try:
            return ipaddress.ip_address(str(host)).is_loopback
        except ValueError:
            return str(host) in ("localhost", "")

    def guarded_connect(self: socket.socket, address: Any):
        if not _is_local(address):
            raise _NetworkBlocked(
                "External network calls are disabled by privacy policy "
                "(privacy.allow_external_network_calls = false)."
            )
        return original_connect(self, address)

    socket.socket.connect = guarded_connect  # type: ignore[method-assign]
    socket.socket._lotus_guarded = True  # type: ignore[attr-defined]
    return True


def utc_now() -> datetime:
    return datetime.now(UTC)
