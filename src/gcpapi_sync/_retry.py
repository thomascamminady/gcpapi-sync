"""Retry policy for transient failures."""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

__all__ = ["RetryConfig"]

# SystemRandom keeps security linters quiet; jitter does not need it, but it costs nothing.
_RANDOM = random.SystemRandom()


@dataclass(frozen=True, slots=True)
class RetryConfig:
    """How the client retries failed requests.

    Connection errors, timeouts and the statuses in ``retry_statuses`` are retried
    with exponential backoff and jitter. A ``Retry-After`` header is honored, unless
    it asks for more than ``max_retry_after`` seconds, in which case the error is
    raised immediately.

    Attributes:
        max_retries: Retries after the first attempt. ``0`` disables retrying.
        initial_delay: Delay before the first retry, in seconds.
        max_delay: Upper bound for the computed backoff, in seconds.
        jitter: Random fraction (``0..1``) added to or removed from each delay.
        max_retry_after: Longest ``Retry-After`` the client is willing to wait.
        retry_statuses: HTTP statuses considered transient.
    """

    max_retries: int = 2
    initial_delay: float = 0.5
    max_delay: float = 8.0
    jitter: float = 0.25
    max_retry_after: float = 60.0
    retry_statuses: frozenset[int] = field(
        default_factory=lambda: frozenset({408, 429, 500, 502, 503, 504})
    )

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if self.initial_delay < 0 or self.max_delay < 0 or self.max_retry_after < 0:
            raise ValueError("delays must be >= 0")
        if not 0 <= self.jitter <= 1:
            raise ValueError("jitter must be between 0 and 1")

    def backoff(self, attempt: int) -> float:
        """Delay before retry number ``attempt`` (0-based), including jitter."""
        delay = min(self.initial_delay * 2.0**attempt, self.max_delay)
        if self.jitter:
            delay *= 1 + _RANDOM.uniform(-self.jitter, self.jitter)
        return max(delay, 0.0)


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Parse a ``Retry-After`` header (delta-seconds or HTTP date) into seconds."""
    if value is None:
        return None
    value = value.strip()
    try:
        return max(float(value), 0.0)
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max((when - (now or datetime.now(UTC))).total_seconds(), 0.0)
