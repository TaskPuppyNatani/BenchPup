"""Small presentation helpers for catalog rows."""

from __future__ import annotations

from datetime import datetime
from typing import Any


NOT_RECORDED = "Not recorded"


def display_optional(value: Any, *, missing: str = NOT_RECORDED) -> str:
    if value is None or value == "":
        return missing
    return str(value)


def display_timestamp(value: str | None) -> str:
    """Make stored timestamps easier to scan without changing their meaning."""

    if value in (None, ""):
        return NOT_RECORDED
    return value.replace("T", " ", 1)


def timestamp_sort_value(value: str | None) -> tuple[int, str]:
    if value in (None, ""):
        return (1, "")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return (0, str(value))
    return (0, parsed.isoformat())


def display_bool(value: bool, *, enabled: str = "Enabled", disabled: str = "Disabled") -> str:
    return enabled if value else disabled


__all__ = ("NOT_RECORDED", "display_bool", "display_optional", "display_timestamp", "timestamp_sort_value")
