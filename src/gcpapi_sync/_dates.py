"""Date argument handling and range splitting."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import TypeAlias

DateLike: TypeAlias = date | str
"""A calendar date: a :class:`datetime.date` (or ``datetime``) or a ``YYYY-MM-DD`` string."""


def to_date(value: object, name: str = "date") -> date:
    """Convert a :data:`DateLike` to a :class:`datetime.date`.

    Accepts any object so that untyped callers get a clear ``TypeError``.

    Raises:
        TypeError: ``value`` is neither a date nor a string.
        ValueError: ``value`` is a string but not an ISO ``YYYY-MM-DD`` date.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            raise ValueError(f"{name} must be a YYYY-MM-DD date, got {value!r}") from None
    raise TypeError(
        f"{name} must be a datetime.date or YYYY-MM-DD string, got {type(value).__name__}"
    )


def to_range(start: DateLike, end: DateLike) -> tuple[date, date]:
    """Convert and validate an inclusive date range.

    Raises:
        ValueError: ``start`` is after ``end``.
    """
    first, last = to_date(start, "start"), to_date(end, "end")
    if first > last:
        raise ValueError(f"start ({first}) must not be after end ({last})")
    return first, last


def windows(start: date, end: date, max_days: int) -> list[tuple[date, date]]:
    """Split the inclusive range ``start..end`` into consecutive windows of at most ``max_days``."""
    if max_days < 1:
        raise ValueError("max_days must be at least 1")
    result: list[tuple[date, date]] = []
    step = timedelta(days=max_days)
    current = start
    while current <= end:
        last = min(current + step - timedelta(days=1), end)
        result.append((current, last))
        current = last + timedelta(days=1)
    return result
