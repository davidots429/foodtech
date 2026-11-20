"""Approved synthetic 72-hour validation window; no evidence is verified here."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

RULE_VERSION = "synthetic-validation-window-v1"
MAX_WINDOW = timedelta(hours=72)


@dataclass(frozen=True)
class Invalidation:
    occurred_at: datetime
    reason: str


@dataclass(frozen=True)
class Window:
    status: Literal["VALIDATION_REQUIRED", "VALIDATED_ACTIVE", "VALIDATION_EXPIRED", "REVALIDATION_REQUIRED"]
    validated_at: datetime | None
    expires_at: datetime | None
    invalidation: Invalidation | None
    rule_version: str = RULE_VERSION


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return value.astimezone(timezone.utc)


def evaluate(validated_at: datetime | None, invalidations: list[Invalidation], *, now: datetime) -> Window:
    """Evaluate an already verified validation; callers own evidence and invalidation completeness."""
    now = _utc(now)
    if validated_at is None:
        if invalidations:
            raise ValueError("invalidations require a validation")
        return Window("VALIDATION_REQUIRED", None, None, None)
    validated = _utc(validated_at)
    if validated > now:
        raise ValueError("validation cannot be in the future")
    events = []
    for event in invalidations:
        at = _utc(event.occurred_at)
        if not isinstance(event.reason, str) or not event.reason.strip():
            raise ValueError("invalidation reason is required")
        if at < validated:
            raise ValueError("invalidation predates this validation")
        events.append(Invalidation(at, event.reason))
    # Tie-breaking by reason gives deterministic results when several events share a timestamp.
    due = sorted((event for event in events if event.occurred_at <= now),
                 key=lambda event: (event.occurred_at, event.reason))
    earliest = due[0] if due else None
    deadline = validated + MAX_WINDOW
    if earliest is not None and earliest.occurred_at <= deadline:
        return Window("REVALIDATION_REQUIRED", validated, earliest.occurred_at, earliest)
    if now >= deadline:
        return Window("VALIDATION_EXPIRED", validated, deadline, None)
    return Window("VALIDATED_ACTIVE", validated, deadline, None)
