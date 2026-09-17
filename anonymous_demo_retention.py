"""Retention timing helpers for anonymous website demos."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


CLEANUP_ZONE = ZoneInfo("America/Los_Angeles")


def overnight_deadline(eligible_at: datetime) -> datetime:
    """Return the first 03:00 California cleanup after eligibility."""
    local = eligible_at.astimezone(CLEANUP_ZONE)
    due = local.replace(hour=3, minute=0, second=0, microsecond=0)
    if due < local:
        due += timedelta(days=1)
    return due.astimezone(timezone.utc)
